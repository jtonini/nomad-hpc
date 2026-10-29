#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Read-only probe of nomad's policy watch (1.19): what runs on this host
that a cluster's rules usually forbid. Standard library only, Python 3.6 or
later: it is piped over ssh to a fixed interpreter (`/usr/bin/python3 -I
-S -`: no PATH, no site-packages; nothing is installed on the host) or run
here, and prints one JSON object.

From /proc, as the account running it (no root needed, unless /proc hides
other accounts' processes: hidepid):

- programs that open a way into the cluster from outside, or out of it:
  tunnels (ngrok, cloudflared, frp, chisel, Tailscale, ...), `ssh -R` (a
  reverse forward) and `ssh -D` (a SOCKS proxy), `code tunnel`; by the
  program's name (argv[0] and /proc/PID/comm, a download's version or
  platform suffix aside), or the script an interpreter runs;
- miners (by name, xmrig's own options, or a stratum pool on the command
  line of a program that isn't a text tool);
- relays and proxies (socat listening, 3proxy, v2ray, shadowsocks, ...;
  generic names only with a listening or proxy argument);
- TCP sockets listening, by owner, on loopback or open to the network
  (/proc/net/tcp and tcp6, readable by every account); a port in the
  kernel's ephemeral range is marked so (MPI, Dask, Ray pick those); at
  most five per account and scope, then +N more.

Only accounts from --min-uid up (1000) count: system services aren't
people; --ignore-users and --ignore-programs (a site's own services) are
left out before anything is counted. Nothing of a command line is kept but
what names the finding: a program's name (a short word: letters, digits,
. _ + -), a forwarded or listening port, and whether an ssh forward's
destination is one of --inside's names (the cluster's own, given by the
collector) or a private address, with no proxy of its own -- a hint, not
proof: a user's own ssh config can map any name anywhere. Never a host, an
address or an argument.

    /usr/bin/python3 -I -S - [--min-uid N] [--inside a,b] [--ignore-users a,b]
        [--ignore-programs a,b] [--proc DIR] < watch_probe.py
"""
import json
import os
import pwd
import re
import socket
import sys

TUNNELS = ("ngrok", "cloudflared", "frpc", "frps", "chisel", "rathole", "inlets", "zrok", "pinggy",
           "localtunnel", "tailscaled", "tailscale", "zerotier-one", "sish", "ligolo", "ligolo-ng",
           "tunnelto", "pagekite.py", "pagekite", "telebit", "devtunnel", "playit-agent", "cloudflare-warp",
           "sshuttle", "wstunnel", "iodine", "dnscat2", "hans", "ptunnel", "ptunnel-ng", "udp2raw", "kcptun")
# Names that are also ordinary words or coursework: a tunnel or relay only
# with an argument that listens or proxies.
WEAK = ("gost", "bore", "naive", "trojan", "clash", "mihomo", "xray", "loophole", "brook", "playit")
RELAYS = ("3proxy", "v2ray", "ss-server", "ss-local", "sslocal", "ssserver", "ss-redir", "trojan-go",
          "hysteria", "sing-box", "microsocks", "tinyproxy", "privoxy", "sockd", "danted", "squid")
MINERS = ("xmrig", "xmr-stak", "t-rex", "lolminer", "nbminer", "gminer", "teamredminer", "ethminer",
          "phoenixminer", "nanominer", "cpuminer", "minerd", "bzminer", "srbminer", "srbminer-multi",
          "rigel", "onezerominer", "wildrig", "ccminer", "bminer", "kawpowminer", "nheqminer", "cgminer",
          "bfgminer", "xmrig-cuda", "hellminer", "verus-solver")
INTERPRETERS = re.compile(r"^(python[0-9.]*|pypy[0-9.]*|node|nodejs|java|perl|ruby|sh|bash|dash|zsh|ksh)$")
TEXT_TOOLS = ("grep", "egrep", "fgrep", "rg", "ag", "ack", "less", "more", "cat", "tail", "head", "vi",
              "vim", "nvim", "view", "nano", "emacs", "sed", "awk", "gawk", "find", "git", "man", "zgrep")
STRATUM = re.compile(r"stratum(\+tcp|\+ssl|\+tls|2)?://", re.I)
LISTENS = re.compile(r"(?i)(^-[lL]$|listen|^--?(server|bind|socks|to)\b|(socks5?h?|https?|tcp|udp|relay|ws|wss)://"
                     r"|^:\d{2,5}$)")
ARG = 1024                       # of each argument, what signatures look at
NAME_OK = re.compile(r"^[A-Za-z0-9._+-]{1,32}$")
MAX_ARGV = 64 * 1024
MAX_PER = 5                      # programs kept per account and kind (the rest: "+N more")


def _args():
    out = {"min_uid": 1000, "proc": "/proc", "inside": (), "ignore_users": frozenset(),
           "ignore_programs": frozenset()}
    a = sys.argv[1:]
    i = 0
    while i < len(a):
        if a[i] == "--min-uid" and i + 1 < len(a):
            out["min_uid"] = int(a[i + 1])
            i += 2
        elif a[i] == "--proc" and i + 1 < len(a):
            out["proc"] = a[i + 1]
            i += 2
        elif a[i] == "--inside" and i + 1 < len(a):
            out["inside"] = tuple(x.lower() for x in a[i + 1].split(",") if x)
            i += 2
        elif a[i] in ("--ignore-users", "--ignore-programs") and i + 1 < len(a):
            out[a[i][2:].replace("-", "_")] = frozenset(x for x in a[i + 1].split(",") if x)
            i += 2
        else:
            i += 1
    return out


_NAMES = {}


def _name(uid):
    if uid not in _NAMES:
        try:
            _NAMES[uid] = pwd.getpwuid(uid).pw_name
        except (KeyError, OverflowError):
            _NAMES[uid] = None
    return _NAMES[uid]


def safe(word):
    """A program's name as kept: its first word's basename, letters, digits
    and . _ + - only, at most 32; else '?'."""
    w = os.path.basename((word or "").split(" ")[0].split("\t")[0])
    return w if NAME_OK.match(w) else "?"


def _is(name, names, script=False):
    """name is one of names; a program's name also with a download's suffix
    (cloudflared-linux-amd64, chisel_1.10.1_linux_amd64; comm cuts at 15);
    a script's only exactly, or with its extension (pagekite.py)."""
    n = (name or "").lower()
    if script:
        base = re.sub(r"\.(py|js|mjs|pl|rb|sh|jar)$", "", n)
        return next((x for x in names if n == x or base == x), None)
    for x in names:
        if n == x or (len(n) > len(x) and n.startswith(x) and n[len(x)] in "-_."):
            return x
    return None


_PRIVATE4 = re.compile(r"^(10|127)\.\d+\.\d+\.\d+$|^192\.168\.\d+\.\d+$|^172\.(1[6-9]|2\d|3[01])\.\d+\.\d+$")


def _private(ip):
    """A private, loopback or link-local address, written as an address
    (not a name)."""
    ip = ip.lower().strip("[]")
    if _PRIVATE4.match(ip) or re.match(r"^169\.254\.\d+\.\d+$", ip):
        return True
    if not re.match(r"^[0-9a-f:]+$", ip) or ":" not in ip:
        return False
    if re.match(r"^(0+:){7}0*1$", ip) or ip == "::1":
        return True
    return bool(re.match(r"^(f[cd][0-9a-f]{2}|fe[89ab][0-9a-f]):", ip))


SSH_VALUE = "bcDeEFIiJlLmOopPQRSwW"            # ssh's options that take a value


def _forwards(argv, domains=()):
    """An ssh client's remote (-R) and dynamic (-D) forwards and tunnel
    devices (-w): (kind, port, inside) each, the port as given (no host);
    -o RemoteForward/DynamicForward too. As ssh reads them: options before
    and after the destination, up to its command. ``domains``: the names
    that are the cluster's own (from the collector): inside is one of them
    or a private address, with no -J, -W, -F or -o HostName/ProxyJump/
    ProxyCommand (a hint for the reader: it lowers nothing by itself)."""
    out = []
    dest = None
    proxied = False
    i, n = 1, len(argv)
    while i < n:
        a = argv[i]
        if a == "--":
            if dest is None and i + 1 < n:
                dest = argv[i + 1]
            break
        if a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                c = a[j]
                if c in SSH_VALUE:
                    val = a[j + 1:]
                    if not val:
                        val = argv[i + 1] if i + 1 < n else ""
                        i += 1
                    val = " ".join(val.split())[:ARG]          # as ssh splits it; padding can't hide one
                    if c in "RDw":
                        out.append((c, _port(val, first=(c != "D"))))
                    elif c in "JWF":
                        proxied = True
                    elif c == "o":
                        m = re.match(r"(?i)(RemoteForward|DynamicForward)(?: ?= ?| )(\S+)", val)
                        if m:
                            k = "R" if m.group(1).lower().startswith("remote") else "D"
                            out.append((k, _port(m.group(2), first=True)))
                        elif re.match(r"(?i)(HostName|ProxyJump|ProxyCommand)\b", val):
                            proxied = True
                    break
                j += 1
            i += 1
            continue
        if dest is None:
            dest = a                                  # options may follow the destination
            i += 1
            continue
        break                                         # its command: ssh's options end
    host = (dest or "").split("@")[-1].lower()
    if host.startswith("ssh://"):
        host, proxied = host[6:], True
    # inside: only a name the collector gave as the cluster's own, or a private
    # address written as one, and only with no proxy or host name of its own
    where = (not proxied) and bool(host) and (host in domains or _private(host))
    return [(k, p, where) for k, p in out]


def _port(spec, first=True):
    """The port of a forward spec ([bind:]port[:host:hostport], or a socket
    path): the listening one, without any host."""
    parts = [p for p in re.split(r"[: ]", spec.strip("[]")) if p]
    nums = [p for p in parts if p.isdigit()]
    if not nums:
        return "socket" if "/" in spec else ""
    return nums[0] if first else nums[-1]


def _candidates(argv, comm):
    """The names a process goes by, (name, is a script): argv[0], its comm
    (what exec -a doesn't change), and for an interpreter the script or
    module it runs."""
    names = [(os.path.basename(argv[0]).lower(), False), ((comm or "").lower(), False)]
    if any(INTERPRETERS.match(n) for n, _ in names) and len(argv) > 1:
        k = 1
        while k < len(argv):
            a = argv[k][:ARG]
            if a in ("-m", "-jar") and k + 1 < len(argv):
                names.append((os.path.basename(argv[k + 1][:ARG]).lower(), True))
                break
            if a in ("-c", "-e", "-x", "--eval"):
                break
            if not a.startswith("-"):
                names.append((os.path.basename(a).lower(), True))
                break
            k += 1
        if any("/node_modules/localtunnel/" in a[:ARG] for a in argv[1:3]):
            names.append(("localtunnel", True))
    return [(n, sc) for n, sc in names if n]


def _classify(argv, comm, domains):
    """(kind, program, detail, inside, aka) for each thing a process is;
    aka: every name it goes by (an ignore list leaves it out only if it
    names them all)."""
    names = _candidates(argv, comm)
    progs = {n for n, sc in names if not sc}                # argv[0] and comm
    args = [x[:ARG] for x in argv[1:]]
    aka = sorted({safe(n) for n, _ in names} - {"?"})
    shown = safe(argv[0]) if safe(argv[0]) != "?" else safe(comm)
    out = []
    for nm, sc in names:
        t = _is(nm, TUNNELS, sc)
        if t:
            out.append(("tunnel", t, "", None))
            break
        w = _is(nm, WEAK, sc)
        if w and any(LISTENS.search(x) for x in args):
            out.append(("tunnel", w, "", None))
            break
    ssh = next((p for p in ("ssh", "autossh") if p in progs), None)
    if ssh:
        for kind, port, inside in _forwards([argv[0]] + args, domains):
            if kind == "R":
                out.append(("ssh_reverse", ssh, port, inside))
            elif kind == "D":
                out.append(("ssh_socks", ssh, port, inside))
            else:
                out.append(("tunnel", ssh + " -w", "", inside))
    code = next((p for p in progs if re.match(r"^code(-insiders)?(-tunnel)?$", p)), None)
    if code and (code.endswith("-tunnel") or "tunnel" in args):
        out.append(("code_tunnel", code, "", None))
    m = next((_is(nm, MINERS, sc) for nm, sc in names if _is(nm, MINERS, sc)), None)
    stratum = any(STRATUM.search(x) for x in args)
    if m:
        out.append(("miner", m, "", None))
    elif (stratum or "--donate-level" in args) and not (progs and progs <= set(TEXT_TOOLS)):
        out.append(("miner", shown, "stratum" if stratum else "", None))
    r = next((_is(nm, RELAYS, sc) for nm, sc in names if _is(nm, RELAYS, sc)), None)
    if r:
        out.append(("relay", r, "", None))
    elif "socat" in progs and any(re.search(r"(?i)-listen|^listen", x) for x in args):
        out.append(("relay", "socat", "", None))
    return [(k, p, d, i, aka) for k, p, d, i in out]


def _read(path, limit=MAX_ARGV):
    with open(path, "rb") as f:
        return f.read(limit)


def processes(proc, min_uid, domains=(), ignore_users=frozenset(), ignore_programs=frozenset()):
    """(findings, visible, of others, of others with a command line to read)."""
    found = {}
    seen = others = others_cmd = 0
    me = os.getuid()
    try:
        names = os.listdir(proc)
    except OSError:
        names = []
    for d in names:
        if not d.isdigit():
            continue
        path = os.path.join(proc, d)
        try:
            uid = os.stat(path).st_uid
        except OSError:
            continue
        seen += 1
        try:
            argv = [x.decode("utf-8", "replace") for x in _read(os.path.join(path, "cmdline")).split(b"\0") if x]
        except OSError:
            argv = []
        if uid != me:
            others += 1
            if argv:
                others_cmd += 1
        if uid < min_uid or not argv:
            continue
        try:
            comm = _read(os.path.join(path, "comm"), 64).decode("utf-8", "replace").strip()
        except OSError:
            comm = ""
        if ignore_users and _name(uid) in ignore_users:
            continue
        for kind, program, detail, inside, aka in _classify(argv, comm, domains):
            if ignore_programs and aka and set(aka) <= ignore_programs:
                continue
            key = (uid, kind, safe(program), str(detail), inside)
            n, names = found.get(key, (0, set()))
            found[key] = (n + 1, names | set(aka))
    rows, per, dropped_out = [], {}, {}
    # within an account and kind: what isn't known to be inside first
    for k, (n, aka) in sorted(found.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][4] is True, str(kv[0]))):
        group = (k[0], k[1])
        per[group] = per.get(group, 0) + 1
        if per[group] > MAX_PER:
            if k[4] is not True:
                dropped_out[group] = True
            continue
        rows.append({"uid": k[0], "user": _name(k[0]), "kind": k[1], "program": k[2], "detail": k[3],
                     "inside": k[4], "aka": sorted(aka), "processes": n})
    for (uid, kind), total in per.items():
        if total > MAX_PER:
            rows.append({"uid": uid, "user": _name(uid), "kind": kind, "program": f"+{total - MAX_PER} more",
                         "detail": "", "inside": None if dropped_out.get((uid, kind)) else True, "aka": [],
                         "processes": 0})
    return rows, seen, others, others_cmd


def _loopback(hexaddr):
    a = hexaddr.upper()
    if len(a) == 8:
        return a.endswith("7F")                        # 127.x.x.x, as the kernel writes it
    return a == "00000000000000000000000001000000" or (
        a.startswith("0000000000000000FFFF0000") and a.endswith("7F"))


def _ephemeral(proc):
    try:
        with open(os.path.join(proc, "sys", "net", "ipv4", "ip_local_port_range")) as f:
            lo, hi = f.read().split()[:2]
            return int(lo), int(hi)
    except (OSError, ValueError):
        return 32768, 60999


def listeners(proc, min_uid, ignore_users=frozenset()):
    """TCP sockets listening, by owner from --min-uid: port, scope, whether
    the port is an ephemeral one, and the program when this account may read
    the owning process (its own, or root: every one)."""
    owner = {}
    me = os.getuid()
    inode_pid = {}
    try:
        names = os.listdir(proc)
    except OSError:
        names = []
    for d in names:
        if not d.isdigit():
            continue
        if me != 0:
            try:
                if os.stat(os.path.join(proc, d)).st_uid != me:
                    continue                           # another's fds: unreadable without root
            except OSError:
                continue
        fd = os.path.join(proc, d, "fd")
        try:
            for f in os.listdir(fd):
                try:
                    t = os.readlink(os.path.join(fd, f))
                except OSError:
                    continue
                if t.startswith("socket:["):
                    inode_pid[t[8:-1]] = d
        except OSError:
            continue
    lo, hi = _ephemeral(proc)
    for name in ("tcp", "tcp6"):
        try:
            with open(os.path.join(proc, "net", name)) as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            p = row.split()
            if len(p) < 10 or p[3] != "0A":
                continue
            try:
                uid = int(p[7])
                lip, lport = p[1].split(":")
                port = int(lport, 16)
            except ValueError:
                continue
            if uid < min_uid or (ignore_users and _name(uid) in ignore_users):
                continue
            scope = "loopback" if _loopback(lip) else "network"
            prog, aka = "", []
            pid = inode_pid.get(p[9])
            if pid:
                try:
                    prog = safe(_read(os.path.join(proc, pid, "cmdline"), 4096).split(b"\0")[0]
                                .decode("utf-8", "replace"))
                except OSError:
                    prog = ""
                try:
                    comm = safe(_read(os.path.join(proc, pid, "comm"), 64).decode("utf-8", "replace").strip())
                except OSError:
                    comm = ""
                aka = sorted({prog, comm} - {"?", ""})
            if (uid, port, scope) not in owner or prog:
                owner[(uid, port, scope)] = (prog, aka)
    rows, per = [], {}
    # per account and scope: fixed ports first, at most MAX_PER, then +N more
    for k, v in sorted(owner.items(), key=lambda kv: (kv[0][0], kv[0][2], lo <= kv[0][1] <= hi, kv[0][1])):
        g = (k[0], k[2])
        per[g] = per.get(g, 0) + 1
        if per[g] <= MAX_PER:
            rows.append({"uid": k[0], "user": _name(k[0]), "port": k[1], "scope": k[2], "program": v[0],
                         "aka": v[1], "ephemeral": lo <= k[1] <= hi})
    for (uid, scope), n in per.items():
        if n > MAX_PER:
            rows.append({"uid": uid, "user": _name(uid), "port": f"+{n - MAX_PER} more", "scope": scope,
                         "program": "", "aka": [], "ephemeral": False})
    return rows


def _ip(hexaddr):
    """An address as the kernel writes it in /proc/net/tcp(6), as text."""
    h = hexaddr.upper()
    if len(h) == 8:
        return ".".join(str(int(h[i:i + 2], 16)) for i in (6, 4, 2, 0))
    words = [h[i:i + 8] for i in range(0, 32, 8)]
    b = "".join(w[6:8] + w[4:6] + w[2:4] + w[0:2] for w in words)
    if b.startswith("00000000000000000000FFFF"):
        v4 = b[24:]
        return ".".join(str(int(v4[i:i + 2], 16)) for i in (0, 2, 4, 6))
    return ":".join(b[i:i + 4].lstrip("0") or "0" for i in range(0, 32, 4))


def public_peers(proc, min_uid):
    """The accounts with an established TCP connection to a public address."""
    out = set()
    for name in ("tcp", "tcp6"):
        try:
            with open(os.path.join(proc, "net", name)) as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            p = row.split()
            if len(p) < 8 or p[3] != "01":
                continue
            try:
                uid = int(p[7])
                ip = _ip(p[2].split(":")[0])
            except ValueError:
                continue
            if uid >= min_uid and not _private(ip):
                out.add(uid)
    return out


def hidepid(proc):
    try:
        with open(os.path.join(proc, "mounts")) as f:
            for line in f:
                p = line.split()
                if len(p) > 3 and p[1] == "/proc":
                    return bool(re.search(r"hidepid=(2|invisible|1|noaccess)", p[3]))
    except OSError:
        pass
    return False


def main():
    a = _args()
    found, seen, others, others_cmd = processes(a["proc"], a["min_uid"], a["inside"], a["ignore_users"],
                                                a["ignore_programs"])
    # an ssh forward is inside only if its account reaches nothing public from here
    out_there = public_peers(a["proc"], a["min_uid"])
    for f in found:
        if f.get("inside") and f.get("uid") in out_there:
            f["inside"] = False
    out = {
        "host": socket.gethostname().split(".")[0],
        "uid": os.getuid(),
        "hidepid": hidepid(a["proc"]),
        "processes": seen,
        "others": others,
        "others_readable": others_cmd,
        "programs_of_listeners": os.getuid() == 0,
        "findings": found,
        "listeners": listeners(a["proc"], a["min_uid"], a["ignore_users"]),
    }
    sys.stdout.write(json.dumps(out) + "\n")


if __name__ == "__main__":
    main()

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
  most five per account and scope, then +N more;
- AI agent command-line programs (Claude Code, Codex, aider, ...): listed,
  not alerted (--agents adds to the built-in list; AI use is not misuse);
- a process running as root whose parent is owned by a regular account and
  is not a sanctioned privilege helper (sudo, su, pkexec, ...): the shape
  of a process that reached root outside the usual path (escalation);
- with --gpu, GPU compute processes (nvidia-smi, unprivileged): each listed
  (gpu), or flagged (gpu_served) when it listens on a port open to the
  network or its account also runs a tunnel/relay/forward -- a GPU reachable
  from outside, the shape of reselling or an open inference server. Graphics
  (Xorg, a browser) are not compute apps, so they don't show; a context below
  --gpu-min-mem (256 MiB) is a probe, not a workload, and is left out;
- with --heavy, user processes using real CPU by lifetime average (their whole
  CPU time / their age) of at least --heavy-cpu cores, having used at least
  --heavy-min CPU-seconds (heavy): the shape of a computation run where it
  shouldn't be. The collector keeps it only for a login host.

With --root-report, findings a root helper (watch_root_probe.py) left in a
JSON file are read here too and travel the same pipeline: counts only, of
failed privilege attempts per account and of new setuid files in
user-writable places.

Only accounts from --min-uid up (1000) count: system services aren't
people; --ignore-users and --ignore-programs (a site's own services) are
left out before anything is counted. Nothing of a command line is kept but
what names the finding: a program's name (a short word: letters, digits,
. _ + -), a forwarded or listening port, and an ssh forward's shape (what
its far end listens on, whether it leads to this host). Never a host, an
address or an argument.

An ssh forward's account is checked against what the kernel shows, not
what anyone wrote (1.19.1): where every socket of that account on this host
goes -- this host, this cluster's own networks (private ones reached
directly, not through the default route), a server the collector lists
(--trusted: addresses it resolved, with a port, or 0 for ports below 1024,
which only root can open), campus (private addresses beyond the cluster,
and --campus), or outside -- and whether any ssh of that account has a
child process (ProxyCommand and ProxyJump start one) or relays (-W, -J, a
proxy, a local forward from a socket). A reverse forward is verified when
it opens one port leading to this host, asks for loopback on the far end,
has no proxy of its own, and its account has nothing else: every connection
goes to a listed server (on its port), into the cluster below port 1024,
or to a service of a system account on this host, at least one to a server
or into the cluster; no ssh of it has a child or relays; no UDP socket open
beyond loopback, no raw socket.

Each forward's processes are named by a key (a hash of the boot, their
pids and start times), when each is ssh by its kernel name (as root: its
program file is the system's ssh): an ssh keeps the connection it made for
as long as it runs, so the collector keeps a forward verified while its key
holds (within limits: policy_watch.keep).

    /usr/bin/python3 -I -S - [--min-uid N] [--trusted NET@PORT,...]
        [--campus NET,...] [--cluster NET,...] [--ignore-users a,b]
        [--ignore-programs a,b] [--agents a,b] [--root-report PATH]
        [--gpu] [--gpu-min-mem MiB] [--heavy] [--heavy-cpu CORES]
        [--heavy-min SECONDS] [--proc DIR] < watch_probe.py
"""
import hashlib
import ipaddress
import json
import os
import pwd
import re
import socket
import subprocess
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
# AI agent command-line programs, listed (info), not alerted: AI use is not misuse. The site
# adds its own with --agents; the list grows, so it is config, not fixed here.
# Distinctive agent CLI names only; ambiguous dictionary words (goose, amp, ...) are left out so an
# unrelated tool isn't labelled an agent. Info-only and config-extendable (--agents), so being
# conservative costs little.
AGENTS = ("claude", "claude-code", "codex", "aider", "opencode", "cursor-agent", "open-interpreter",
          "gemini-cli", "copilot", "gh-copilot", "ollama", "llamafile", "sweagent", "openhands", "gptme",
          "qwen-code", "smolagent", "autogpt", "metagpt", "devin")
# Parents that legitimately start a process as root (the sanctioned way up, or an ordinary daemon
# tree): a root child under one of these is not flagged as an escalation.
SANCTIONED_PARENTS = ("sudo", "su", "runuser", "pkexec", "doas", "sshd", "systemd", "init", "login",
                      "crond", "cron", "atd", "slurmstepd", "slurmd", "run-parts", "gdm", "gdm-session-wor",
                      "lightdm", "sddm", "systemd-logind", "dbus-daemon", "polkitd", "machinectl",
                      "systemd-run", "agetty", "getty", "container")
INTERPRETERS = re.compile(r"^(python[0-9.]*|pypy[0-9.]*|node|nodejs|java|perl|ruby|sh|bash|dash|zsh|ksh)$")
TEXT_TOOLS = ("grep", "egrep", "fgrep", "rg", "ag", "ack", "less", "more", "cat", "tail", "head", "vi",
              "vim", "nvim", "view", "nano", "emacs", "sed", "awk", "gawk", "find", "git", "man", "zgrep")
STRATUM = re.compile(r"stratum(\+tcp|\+ssl|\+tls|2)?://", re.I)
LISTENS = re.compile(r"(?i)(^-[lL]$|listen|^--?(server|bind|socks|to)\b|(socks5?h?|https?|tcp|udp|relay|ws|wss)://"
                     r"|^:\d{2,5}$)")
ARG = 1024                       # of each argument, what signatures look at
NAME_OK = re.compile(r"^[A-Za-z0-9._+-]{1,32}\Z")
MAX_ARGV = 64 * 1024
MAX_PER = 5                      # programs kept per account and kind (the rest: "+N more")
SYSTEM_SSH = ("/usr/bin/ssh", "/bin/ssh")    # as root, a forward's key only for these
PRIVATE = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))
LINK_LOCAL = tuple(ipaddress.ip_network(n) for n in ("169.254.0.0/16", "fe80::/10"))
# The GPU watch (1.21): nvidia-smi, unprivileged, where it is found. A compute
# context smaller than this (MiB) is a probe, not a workload, and is left out.
NVIDIA = ("nvidia-smi", "/usr/bin/nvidia-smi", "/bin/nvidia-smi")
GPU_MIN_MEM = 256
# Findings that make an account's doings reachable from outside: a GPU held by
# such an account is served, even when its own listener can't be read (no root).
# A verified reverse forward is the admin's own and doesn't count (see main).
EXPOSURE_KINDS = ("tunnel", "relay", "code_tunnel", "ssh_reverse", "ssh_socks")
# Heavy use (1.22): a user process whose lifetime-average CPU (its whole CPU
# time / its age) is at least this many cores, having used at least this many
# CPU-seconds -- real compute, the shape of work run where it shouldn't be (a
# login or head node). The collector keeps it only for a login host.
HEAVY_CORES = 2.0
HEAVY_MIN_SECONDS = 600
HZ = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") and "SC_CLK_TCK" in os.sysconf_names else 100
PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names else 4096


def _nets(text, ports=False):
    """NET[@PORT],... as given by the collector: (network, port) each, or
    None where one doesn't read (the indexes are the collector's)."""
    out = []
    for item in (text or "").split(","):
        port = 0
        if ports and "@" in item:
            item, _, p = item.rpartition("@")
            port = int(p) if p.isdigit() and int(p) < 65536 else -1
        try:
            out.append((ipaddress.ip_network(item.strip(), strict=False), port) if port >= 0 else None)
        except ValueError:
            out.append(None)
    return out


def _args():
    out = {"min_uid": 1000, "proc": "/proc", "trusted": [], "campus": [], "cluster": [],
           "ignore_users": frozenset(), "ignore_programs": frozenset(), "agents": list(AGENTS),
           "root_report": "", "gpu": False, "gpu_min_mem": GPU_MIN_MEM,
           "heavy": False, "heavy_cores": HEAVY_CORES, "heavy_min": HEAVY_MIN_SECONDS}
    a = sys.argv[1:]
    i = 0
    while i < len(a):
        if a[i] == "--min-uid" and i + 1 < len(a):
            out["min_uid"] = int(a[i + 1])
            i += 2
        elif a[i] == "--proc" and i + 1 < len(a):
            out["proc"] = a[i + 1]
            i += 2
        elif a[i] == "--trusted" and i + 1 < len(a):
            out["trusted"] = _nets(a[i + 1], ports=True)
            i += 2
        elif a[i] in ("--campus", "--cluster") and i + 1 < len(a):
            out[a[i][2:]] = [n for n, _p in (x for x in _nets(a[i + 1]) if x)]
            i += 2
        elif a[i] in ("--ignore-users", "--ignore-programs") and i + 1 < len(a):
            out[a[i][2:].replace("-", "_")] = frozenset(x for x in a[i + 1].split(",") if x)
            i += 2
        elif a[i] == "--agents" and i + 1 < len(a):
            out["agents"] = list(AGENTS) + [x.lower() for x in a[i + 1].split(",") if x]
            i += 2
        elif a[i] == "--root-report" and i + 1 < len(a):
            out["root_report"] = a[i + 1]
            i += 2
        elif a[i] == "--gpu":
            out["gpu"] = True
            i += 1
        elif a[i] == "--gpu-min-mem" and i + 1 < len(a):
            try:
                out["gpu_min_mem"] = max(0, int(a[i + 1]))
            except ValueError:
                pass
            i += 2
        elif a[i] == "--heavy":
            out["heavy"] = True
            i += 1
        elif a[i] == "--heavy-cpu" and i + 1 < len(a):
            try:
                out["heavy_cores"] = max(0.1, float(a[i + 1]))
            except ValueError:
                pass
            i += 2
        elif a[i] == "--heavy-min" and i + 1 < len(a):
            try:
                out["heavy_min"] = max(0, int(a[i + 1]))
            except ValueError:
                pass
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


# -- ssh's own command line ---------------------------------------------------------

SSH_VALUE = "bcBDeEFIiJlLmOopPQRSwW"           # ssh's options that take a value (its getopt string)
FAR = ("loopback", "an address", "every address", "a socket", "unknown")    # worst last
TO = ("this host", "another host", "a socket", "socks", "unknown")
_LOOPBACK_NAME = re.compile(r"^(localhost|::1|127\.\d{1,3}\.\d{1,3}\.\d{1,3})$", re.I)


def _fields(spec):
    """A forward's fields as ssh splits them (readconf.c, parse_fwd_field):
    at colons; a field in [brackets] whole; a backslash makes the next
    character plain. (text, is a path: has a / not escaped) each; None
    where ssh refuses it."""
    out, i, n = [], 0, len(spec)
    while i < n and spec[i] in " \t\r\n\f\v":
        i += 1
    while i < n:
        if len(out) == 4:
            return None                                  # trailing garbage
        if spec[i] == "[":
            j = spec.find("]", i + 1)
            if j < 0 or (j + 1 < n and spec[j + 1] != ":"):
                return None
            field = spec[i + 1:j]
            out.append((field, "/" in field))
            i = j + 2
            continue
        buf, path = [], False
        while i < n:
            c = spec[i]
            if c == "\\":
                if i + 1 >= n:
                    return None
                buf.append(spec[i + 1])
                i += 2
                continue
            i += 1
            if c == ":":
                break
            path = path or c == "/"
            buf.append(c)
        out.append(("".join(buf), path))
    return out


def _shape(kind, spec, dynamic=False):
    """A forward's shape, as ssh reads it (readconf.c, parse_forward):
    port (the one it listens on), far (what it listens on: for -R the far
    end, as asked; its sshd may still narrow or widen it), to (where a
    connection to it leads; socks: anywhere) for -R; for -D, far is here.
    ``dynamic``: RemoteForward with one word, a SOCKS proxy or nothing."""
    f = _fields(spec) if "${" not in spec else None
    out = {"port": "", "far": "unknown", "to": "unknown"}
    if not f or (dynamic and len(f) > 2):
        return out
    n, bind, listen_path, host, to_path = len(f), None, False, None, False
    if kind == "D":
        if n == 1:
            listen_path, port = f[0][1], f[0][0]
        elif n == 2:
            bind, port = f[0][0], f[1][0]
        else:
            return out
        out["to"] = "socks"
    else:
        if n == 1:
            listen_path, port = f[0][1], f[0][0]
        elif n == 2 and f[1][1]:
            listen_path, port, to_path = f[0][1], f[0][0], True
        elif n == 2:
            bind, port = f[0][0], f[1][0]
        elif n == 3 and f[0][1]:
            listen_path, port, host = True, f[0][0], f[1][0]
        elif n == 3 and f[2][1]:
            bind, port, to_path = f[0][0], f[1][0], True
        elif n == 3:
            port, host = f[0][0], f[1][0]
        else:
            bind, port, host = f[0][0], f[1][0], f[2][0]
        out["to"] = "a socket" if to_path else "socks" if host is None else \
            "this host" if _LOOPBACK_NAME.match(host) else "another host"
    if listen_path:
        out["far"], out["port"] = "a socket", "socket"
        return out
    # as ssh's a2port reads a number (022, +22); a service's name isn't one here
    out["port"] = str(int(port)) if re.match(r"^\+?\d{1,5}$", port) and int(port) < 65536 else ""
    out["far"] = "loopback" if bind is None or _LOOPBACK_NAME.match(bind) else \
        "every address" if bind in ("", "*", "0.0.0.0", "::") else "an address"
    return out


def _ssh(argv):
    """An ssh client's command line as ssh reads it, options before and after
    the destination up to its command: (forwards, flags). Forwards: -R, -D
    and -w (tunnel devices), -o RemoteForward/DynamicForward too, each a
    dict (kind, port, far, to, proxied). Flags: 'proxied' (-J, -W, -F,
    -o HostName/ProxyJump/ProxyCommand/ProxyUseFdpass, ssh://: it may not go
    where it says) and 'relay' (-W, -J, a proxy, a local forward from a
    socket: it may carry another ssh elsewhere)."""
    out, flags = [], set()
    dest = None
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
                    if c in "RD":
                        out.append(dict(_shape(c, val), kind=c))
                    elif c == "w":
                        out.append({"kind": "w", "port": "".join(x for x in val.split(":")[0] if x.isdigit()),
                                    "far": "", "to": ""})
                    elif c in "JW":
                        flags.update(("proxied", "relay"))
                    elif c == "F":
                        flags.add("proxied")
                    elif c == "L":
                        g = _fields(val)
                        if g is None or (g and g[0][1]):
                            flags.add("relay")
                    elif c == "o":
                        m = re.match(r"(?i)(RemoteForward|DynamicForward|LocalForward)(?: ?= ?| )(\S*) ?(\S*)", val)
                        if m and m.group(1).lower() == "localforward":
                            g = _fields(m.group(2))
                            if g is None or (g and g[0][1]):
                                flags.add("relay")
                        elif m:
                            k = "R" if m.group(1).lower().startswith("remote") else "D"
                            spec = m.group(2) + (":" + m.group(3) if k == "R" and m.group(3) else "")
                            out.append(dict(_shape(k, spec, dynamic=not m.group(3)), kind=k))
                        elif re.match(r"(?i)(ProxyJump|ProxyCommand|ProxyUseFdpass)\b", val):
                            flags.update(("proxied", "relay"))
                        elif re.match(r"(?i)HostName\b", val):
                            flags.add("proxied")
                    break
                j += 1
            i += 1
            continue
        if dest is None:
            dest = a                                  # options may follow the destination
            i += 1
            continue
        break                                         # its command: ssh's options end
    if (dest or "").lower().startswith("ssh://"):
        flags.add("proxied")
    for f in out:
        f["proxied"] = "proxied" in flags
    return out, flags


def _forwards(argv):
    """An ssh client's -R, -D and -w forwards (see _ssh)."""
    return _ssh(argv)[0]


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


def _classify(argv, comm, agents=AGENTS):
    """(kind, program, detail, shape, aka) for each thing a process is;
    shape: an ssh forward's (see _ssh), else None; aka: every name it goes
    by (an ignore list leaves it out only if it names them all)."""
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
        for f in _forwards([argv[0]] + args):
            if f["kind"] == "R":
                out.append(("ssh_reverse", ssh, f["port"], f))
            elif f["kind"] == "D":
                out.append(("ssh_socks", ssh, f["port"], f))
            else:
                out.append(("tunnel", ssh + " -w", "", None))
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
    g = next((_is(nm, agents, sc) for nm, sc in names if _is(nm, agents, sc)), None)
    if g:
        out.append(("agent", g, "", None))
    return [(k, p, d, s, aka) for k, p, d, s in out]


def _read(path, limit=MAX_ARGV):
    with open(path, "rb") as f:
        return f.read(limit)


def _merge(a, b):
    """Two forwards under one finding: the worse of each."""
    if not a or not b:
        return a or b
    return {"far": max(a["far"], b["far"], key=FAR.index), "to": max(a["to"], b["to"], key=TO.index),
            "proxied": bool(a["proxied"] or b["proxied"])}


def processes(proc, min_uid, ignore_users=frozenset(), ignore_programs=frozenset(), ssh=None, agents=AGENTS):
    """(findings, visible, of others, of others with a command line to read).
    ``ssh``, a dict, gets each account's ssh clients: {uid: {"pids": [...],
    "child": an ssh of it has a child process, "relay": one relays}}."""
    found = {}
    seen = others = others_cmd = 0
    me = os.getuid()
    parents = {}                                    # pid -> its children's names
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
        start = ""
        try:
            stat = _read(os.path.join(path, "stat"), 4096).decode("utf-8", "replace")
            after = stat.rsplit(")", 1)[1].split()
            parents.setdefault(after[1], []).append(safe(stat[stat.find("(") + 1:stat.rfind(")")]))
            start = after[19] if len(after) > 19 else ""
        except (OSError, IndexError, ValueError):
            pass
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
        if ssh is not None and "ssh" in (os.path.basename(argv[0]).lower(), comm.lower()):
            acc = ssh.setdefault(uid, {"pids": [], "child": False, "relay": False})
            acc["pids"].append(d)
            acc["relay"] = acc["relay"] or "relay" in _ssh([argv[0]] + [x[:ARG] for x in argv[1:]])[1]
        real = None
        for kind, program, detail, shape, aka in _classify(argv, comm, agents):
            if ignore_programs and aka and set(aka) <= ignore_programs:
                continue
            if shape and real is None:
                # ssh by its kernel name (a user can set that too); as root, by the program file itself
                real = comm == "ssh"
                if real and me == 0:
                    try:
                        real = os.readlink(os.path.join(path, "exe")) in SYSTEM_SSH
                    except OSError:
                        real = False
            key = (uid, kind, safe(program), str(detail))
            n, names_, sh, ids = found.get(key, (0, set(), None, []))
            found[key] = (n + 1, names_ | set(aka), _merge(sh, shape), ids + [(d + ":" + start, bool(real))])
    if ssh is not None:
        for acc in ssh.values():
            kids = sorted({c for p in acc["pids"] for c in parents.get(p, [])})
            acc["child"] = bool(kids)
            acc["children"] = kids[:3]
    boot = _boot_id(proc)
    rows, per = [], {}
    for k, (n, aka, sh, ids) in sorted(found.items(), key=lambda kv: str(kv[0])):
        group = (k[0], k[1])
        per[group] = per.get(group, 0) + 1
        if per[group] > MAX_PER:
            continue
        row = {"uid": k[0], "user": _name(k[0]), "kind": k[1], "program": k[2], "detail": k[3],
               "aka": sorted(aka), "processes": n}
        if sh:
            row.update(far=sh["far"], to=sh["to"], proxied=sh["proxied"])
            # which processes these are (the same ones, while they live: an ssh keeps its connection),
            # only when each is ssh (exec -a changes argv[0] alone)
            if all(ok for _i, ok in ids):
                row["procs"] = hashlib.sha1((boot + "|" + ",".join(sorted(i for i, _c in ids))).encode()
                                            ).hexdigest()[:16]
        rows.append(row)
    for (uid, kind), total in per.items():
        if total > MAX_PER:
            rows.append({"uid": uid, "user": _name(uid), "kind": kind, "program": f"+{total - MAX_PER} more",
                         "detail": "", "aka": [], "processes": 0})
    return rows, seen, others, others_cmd


def _boot_id(proc):
    try:
        with open(os.path.join(proc, "sys", "kernel", "random", "boot_id")) as f:
            return f.read().strip()
    except OSError:
        return ""


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


# -- where an account's sockets go ------------------------------------------------------

def _addr(hexaddr):
    """An address as the kernel writes it in /proc/net (host byte order per
    32-bit word); an IPv4-mapped IPv6 one as IPv4."""
    h = hexaddr.upper()
    if len(h) == 8:
        return ipaddress.IPv4Address(bytes.fromhex(h)[::-1])
    a = ipaddress.IPv6Address(b"".join(bytes.fromhex(h[i:i + 8])[::-1] for i in range(0, 32, 8)))
    return a.ipv4_mapped or a


def _ip(hexaddr):
    """An address as the kernel writes it in /proc/net/tcp(6), as text."""
    return str(_addr(hexaddr))


def _in(a, nets):
    return any(a.version == n.version and a in n for n in nets)


def _within(net, nets):
    return any(net.version == n.version and n.network_address <= net.network_address
               and net.broadcast_address <= n.broadcast_address for n in nets)


def own_addresses(proc):
    """This host's own addresses (/proc/net/fib_trie, /proc/net/if_inet6)."""
    out, last = set(), None
    try:
        with open(os.path.join(proc, "net", "fib_trie")) as f:
            for line in f:
                s = line.strip()
                if s.startswith("|--"):
                    last = s[3:].strip()
                elif last and s.startswith("/32 host LOCAL"):
                    try:
                        out.add(ipaddress.IPv4Address(last))
                    except ValueError:
                        pass
    except OSError:
        pass
    try:
        with open(os.path.join(proc, "net", "if_inet6")) as f:
            for line in f:
                try:
                    out.add(ipaddress.IPv6Address(bytes.fromhex(line.split()[0])))
                except (ValueError, IndexError):
                    pass
    except OSError:
        pass
    return out


def cluster_networks(proc, extra=()):
    """The cluster's own networks as this host sees them: private ones it
    reaches directly (no gateway), on an interface with no default route
    (that one is a way out, shared with whatever else is there); and
    ``extra``, the site's own (cluster_networks: where a node's way out is
    the cluster network, or a spare default route is on it)."""
    direct, default = [], set()
    try:
        with open(os.path.join(proc, "net", "route")) as f:
            for line in f.read().splitlines()[1:]:
                p = line.split()
                if len(p) < 8:
                    continue
                try:
                    flags = int(p[3], 16)
                    dest = int.from_bytes(bytes.fromhex(p[1])[::-1], "big")
                    mask = bin(int.from_bytes(bytes.fromhex(p[7])[::-1], "big")).count("1")
                except ValueError:
                    continue
                if mask == 0:
                    default.add((4, p[0]))
                elif p[2] == "00000000" and flags & 1 and not flags & 2:
                    direct.append((p[0], ipaddress.ip_network((dest, mask), strict=False)))
    except OSError:
        pass
    try:
        with open(os.path.join(proc, "net", "ipv6_route")) as f:
            for line in f:
                p = line.split()
                if len(p) < 10:
                    continue
                try:
                    plen, flags = int(p[1], 16), int(p[8], 16)
                    net = ipaddress.IPv6Network((int(p[0], 16), plen), strict=False)
                except ValueError:
                    continue
                if plen == 0 and p[9] != "lo" and not flags & 0x200:       # not a reject route
                    default.add((6, p[9]))
                elif p[4] == "0" * 32 and flags & 1 and not flags & 0x80000002 and p[9] != "lo":   # nor local
                    direct.append((p[9], net))
    except OSError:
        pass
    out = []
    for iface, n in direct:
        if (n.version, iface) in default or not _within(n, PRIVATE) or n in out:
            continue
        out.append(n)
    return out + [n for n in extra if n not in out]


SOCKET_FILES = (("tcp", "tcp"), ("tcp6", "tcp"), ("udp", "udp"), ("udp6", "udp"), ("raw", "raw"),
                ("raw6", "raw"), ("icmp", "raw"), ("icmp6", "raw"))


def sockets(proc):
    """Every socket /proc/net shows: (proto, uid, state, local address,
    local port, remote address, remote port)."""
    for name, proto in SOCKET_FILES:
        try:
            with open(os.path.join(proc, "net", name)) as f:
                rows = f.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            p = row.split()
            if len(p) < 8:
                continue
            try:
                la, lp = p[1].split(":")
                ra, rp = p[2].split(":")
                yield proto, int(p[7]), p[3], _addr(la), int(lp, 16), _addr(ra), int(rp, 16)
            except ValueError:
                continue


def reach(proc, uids, trusted=(), campus=(), system=lambda uid: False, extra=()):
    """Where each of ``uids``' sockets on this host go: {uid: {"reach":
    inside (this host, the cluster, a listed server), campus or outside;
    "why": what keeps a forward of it from being verified, by kind (counts);
    "servers": the listed servers (indexes) it reaches}}. ``trusted``:
    (network, port) or None each (port 0: below 1024); ``system``: an
    account whose services anyone may use (not a person's); ``extra``: the
    site's own cluster networks, besides those found (cluster_networks)."""
    rows = list(sockets(proc))
    own, cluster = own_addresses(proc), cluster_networks(proc, extra)
    bound = {}
    for proto, uid, st, la, lp, ra, rp in rows:
        if (proto == "tcp" and st == "0A") or (proto == "udp" and rp == 0 and ra.is_unspecified):
            bound.setdefault((proto, lp), []).append((la, uid))

    def owners(proto, a, port):
        return [u for la, u in bound.get((proto, port), []) if la == a or la.is_unspecified]

    def where(a):
        if a.is_loopback or a in own:
            return "host", []
        hit = [i for i, t in enumerate(trusted) if t and a.version == t[0].version and a in t[0]]
        if hit:
            return "server", hit
        if _in(a, cluster):
            return "cluster", []
        if _in(a, PRIVATE) or _in(a, LINK_LOCAL) or _in(a, campus):
            return "campus", []
        return "outside", []

    rank = ("", "inside", "campus", "outside")
    out = {u: {"reach": "", "why": {}, "servers": set(), "carriers": 0} for u in uids}
    for proto, uid, st, la, lp, ra, rp in rows:
        acc = out.get(uid)
        if acc is None:
            continue
        why = acc["why"]
        if proto == "raw":
            why["raw"] = why.get("raw", 0) + 1
            continue
        if proto == "tcp" and st == "0A":
            continue
        if ra.is_unspecified:
            if proto == "udp" and not la.is_loopback:
                why["udp"] = why.get("udp", 0) + 1     # bound, not connected: it may send anywhere
            continue
        w, idx = where(ra)
        label = "inside" if w in ("host", "server", "cluster") else w
        acc["reach"] = max(acc["reach"], label, key=rank.index)
        acc["servers"].update(idx)
        inbound = proto == "tcp" and uid in owners("tcp", la, lp)
        bad = None
        if w in ("campus", "outside"):
            bad = w
        elif inbound:
            pass                                       # a connection to its own port, from inside
        elif w == "host":
            o = owners(proto, ra, rp)
            if not o or not all(system(u) for u in o):
                bad = "host_port"                      # a person's port here: maybe a relay
        elif w == "server":
            if not any(trusted[i][1] == rp or (trusted[i][1] == 0 and rp < 1024) for i in idx):
                bad = "server_port"
        elif rp >= 1024:
            bad = "cluster_port"                       # a person's port on a node: maybe a relay
        if bad:
            why[bad] = why.get(bad, 0) + 1
        elif proto == "tcp" and st == "01" and not inbound and w in ("server", "cluster"):
            acc["carriers"] += 1
    for acc in out.values():
        if not acc["carriers"]:
            acc["why"]["none"] = 1                      # nothing a forward could be going through
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


FORWARDS = ("ssh_reverse", "ssh_socks")


def check(found, accounts, ssh):
    """Each ssh forward's reach and why it isn't verified (nothing: it is):
    its account's (``accounts``, from reach(); ``ssh``, from processes()),
    and, for -R, its own shape."""
    for f in found:
        if f["kind"] not in FORWARDS:
            continue
        acc = accounts.get(f["uid"]) or {"reach": "", "why": {"none": 1}, "servers": set(), "carriers": 0}
        s = ssh.get(f["uid"]) or {}
        why = dict(acc["why"])
        if 0 < acc.get("carriers", 0) < len(s.get("pids") or []):
            why["carrier"] = 1          # an ssh with no connection of its own: between attempts, or a mux client
        for k in ("child", "relay"):
            if s.get(k):
                why[k] = 1
        if s.get("children"):
            f["children"] = s["children"]
        if f["kind"] == "ssh_reverse":
            if not f["detail"].isdigit() or "far" not in f:
                why["unchecked"] = 1
            else:
                if f["far"] != "loopback":
                    why["far"] = 1
                if f["to"] != "this host":
                    why["to"] = 1
                if f["proxied"]:
                    why["proxied"] = 1
        else:
            why["unchecked"] = 1                        # a SOCKS proxy: never
        f["reach"] = acc["reach"]
        f["why"] = why
        f["servers"] = sorted(acc["servers"])
        f["verified"] = not why


def _status(path):
    """A process's real uid, parent pid and name from /proc/<pid>/status
    (world-readable): (ruid, ppid, comm), or None. The REAL uid, not the
    directory's owner -- the kernel makes /proc/<pid> root-owned for any
    setuid program (ping, mount, Xorg, ...), whose real uid is still the
    user, so the directory owner would call every setuid helper an
    escalation. The real uid is 0 only for a process that truly runs as
    root."""
    try:
        txt = _read(os.path.join(path, "status"), 8192).decode("utf-8", "replace")
    except OSError:
        return None
    uid = ppid = None
    comm = ""
    for line in txt.splitlines():
        if line.startswith("Uid:"):
            parts = line[4:].split()
            if parts and parts[0].isdigit():
                uid = int(parts[0])                      # the real uid (not effective)
        elif line.startswith("PPid:"):
            ppid = line[5:].strip()
        elif line.startswith("Name:"):
            comm = safe(line[5:].strip())
    return None if uid is None or ppid is None else (uid, ppid, comm)


def escalations(proc, min_uid, ignore_programs=()):
    """Processes whose REAL uid is root (0) and whose parent's real uid is a
    regular account (>= min_uid) where the parent is not one of
    SANCTIONED_PARENTS (sudo, su, pkexec, ...): the shape of a process that
    reached root outside the usual path, for an administrator to review.
    From /proc/<pid>/status, no root needed, though under hidepid only this
    account's own tree is seen (nomad watch status says so). A setuid-root
    helper keeps its user real uid, so it is not an escalation. The account
    named is the one whose session owns the parent; kept are the two
    programs' names, never a command line. ignore_programs leaves out a
    site's own (a sanctioned root tool)."""
    tab = {}
    try:
        names = os.listdir(proc)
    except OSError:
        names = []
    for d in names:
        if not d.isdigit():
            continue
        st = _status(os.path.join(proc, d))
        if st is not None:
            tab[d] = st
    return _escalations(tab, min_uid, ignore_programs)


def _escalations(tab, min_uid, ignore_programs=()):
    """The escalation findings from a process table {pid: (uid, ppid, comm)}:
    a uid-0 process whose parent is a regular account's and is not a
    sanctioned helper. Separated from the /proc walk so it can be tested
    without having to own processes as several accounts."""
    ignore = set(ignore_programs)
    out = {}
    for uid, ppid, comm in tab.values():
        if uid != 0:
            continue
        parent = tab.get(ppid)
        if not parent or parent[0] < min_uid:
            continue                                    # parent is root or a system account: an ordinary tree
        if parent[2] in SANCTIONED_PARENTS or comm in SANCTIONED_PARENTS:
            continue                                    # the sanctioned way up (sudo, su, pkexec, ...)
        if comm in ignore or parent[2] in ignore:
            continue
        key = (parent[0], comm or "?", parent[2] or "?")
        out[key] = out.get(key, 0) + 1
    rows = []
    for (uid, comm, parent), n in sorted(out.items(), key=lambda kv: str(kv[0])):
        rows.append({"uid": uid, "user": _name(uid), "kind": "escalation", "program": comm,
                     "detail": parent, "processes": n})
    return rows


def root_signals(path):
    """Findings a root helper (watch_root_probe.py) left in a JSON file, read
    here as the collector so they travel the same pipeline. What it writes is
    counts and short names only: failed privilege attempts per account
    (auth_failures), and new setuid/setgid files in user-writable places
    (setuid_new). A file that doesn't read, or isn't the shape expected, is
    ignored."""
    if not path:
        return []
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(d, dict):
        return []
    rows = [{"kind": "root_report", "generated": str(d.get("generated", ""))[:32],
             "stale": bool(d.get("stale"))}]
    for a in (d.get("auth_failures") or [])[:200]:
        if not isinstance(a, dict) or not a.get("user"):
            continue
        uid = a.get("uid") if isinstance(a.get("uid"), int) else None
        n = a.get("count") if isinstance(a.get("count"), int) and 0 < a.get("count") < 10 ** 7 else 0
        rows.append({"uid": uid, "user": str(a.get("user", ""))[:64], "kind": "auth_failures",
                     "program": "", "detail": str(n), "processes": n})
    for a in (d.get("setuid_new") or [])[:200]:
        if not isinstance(a, dict) or not a.get("user"):
            continue
        uid = a.get("uid") if isinstance(a.get("uid"), int) else None
        rows.append({"uid": uid, "user": str(a.get("user", ""))[:64], "kind": "setuid_new",
                     "program": safe(str(a.get("name", ""))), "detail": "", "processes": 1})
    return rows


# -- the GPU watch (1.21) -------------------------------------------------------------

def _nvidia_smi(query):
    """One ``nvidia-smi --query-... --format=csv,noheader,nounits`` as a list
    of stripped lines, or [] when there is no GPU here (the tool is missing,
    fails, or says nothing). No GPU -> no GPU findings, so a host without one
    costs the watch nothing. Unprivileged: nvidia-smi shows every account's
    compute apps."""
    for exe in NVIDIA:
        try:
            r = subprocess.run([exe, query, "--format=csv,noheader,nounits"],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue                                     # this name isn't here: try the next
        if r.returncode != 0:
            return []                                    # the tool is here but failed: no GPU, a driver error
        return [ln.strip() for ln in r.stdout.decode("utf-8", "replace").splitlines() if ln.strip()]
    return []


def gpu_compute():
    """({pid: mem_MiB} for GPU *compute* processes, [(index, util%, used, total)]
    per GPU). Compute apps are CUDA contexts -- training, inference, miners --
    not graphics (Xorg, a browser), which --query-compute-apps never lists.
    Empty when there is no GPU."""
    per_pid = {}
    for line in _nvidia_smi("--query-compute-apps=pid,used_gpu_memory"):
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 2 and p[0].isdigit() and p[1].isdigit():
            per_pid[p[0]] = int(p[1])
    gpus = []
    for line in _nvidia_smi("--query-gpu=index,utilization.gpu,memory.used,memory.total"):
        p = [x.strip() for x in line.split(",")]
        try:
            gpus.append((int(p[0]), int(p[1]), int(p[2]), int(p[3])))
        except (ValueError, IndexError):
            continue                                     # a field is [N/A] (MIG, an older driver)
    return per_pid, gpus


def _serving_pids(proc, pids):
    """Of ``pids``, which listen on a TCP port open to the network, and on
    which ports: {pid: [(port, is the port an ephemeral one)]}. The pid's own
    socket inodes (its fds, readable as the owner or root) against
    /proc/net/tcp(6) LISTEN rows with a non-loopback local address. An
    ephemeral port is the kernel's own range (where a distributed job's
    rendezvous lands); it is marked so a compute node's training isn't taken
    for an exposed service. Without root, another account's fds can't be read,
    so a GPU it serves is caught by its account's exposure instead."""
    inode_pid, want = {}, {}
    for pid in pids:
        try:
            for f in os.listdir(os.path.join(proc, pid, "fd")):
                try:
                    t = os.readlink(os.path.join(proc, pid, "fd", f))
                except OSError:
                    continue
                if t.startswith("socket:["):
                    inode_pid[t[8:-1]] = pid
        except OSError:
            continue
    if not inode_pid:
        return want
    lo, hi = _ephemeral(proc)
    for name in ("tcp", "tcp6"):
        try:
            with open(os.path.join(proc, "net", name)) as fh:
                rows = fh.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            p = row.split()
            if len(p) < 10 or p[3] != "0A":
                continue
            pid = inode_pid.get(p[9])
            if not pid:
                continue
            try:
                lip, lport = p[1].split(":")
                port = int(lport, 16)
            except ValueError:
                continue
            if not _loopback(lip):
                want.setdefault(pid, []).append((port, lo <= port <= hi))
    return want


def _detached(proc, pid):
    """A process with no controlling terminal whose parent is init or systemd:
    the shape of a job running unattended (nohup, a service, a detached agent),
    not from a login shell. A heuristic -- an overnight training run looks the
    same -- so it only annotates a GPU finding, never alerts on its own."""
    try:
        stat = _read(os.path.join(proc, pid, "stat"), 4096).decode("utf-8", "replace")
        after = stat.rsplit(")", 1)[1].split()
        tty, ppid = (after[4] if len(after) > 4 else "0"), after[1]
    except (OSError, IndexError, ValueError):
        return False
    if tty != "0":
        return False
    parent = _status(os.path.join(proc, ppid))
    return ppid == "1" or (parent is not None and parent[2] in ("systemd", "init"))


def gpu_findings(proc, min_uid, agents=AGENTS, ignore_users=frozenset(), ignore_programs=frozenset(),
                 exposure_uids=frozenset(), min_mem=GPU_MIN_MEM):
    """GPU compute processes (nvidia-smi), placed by owner and by whether each
    is served to the network. A process holding a compute context of at least
    ``min_mem`` MiB is listed as ``gpu`` (info), or ``gpu_served`` (a warning)
    when it listens on a port open to the network, or its account also runs a
    tunnel, relay or forward -- a GPU reachable from outside, the shape of
    reselling or an open inference server. The mem is kept in the note, not the
    key, so a finding doesn't churn as it moves; at most five per account and
    kind, then +N more. Nothing when there is no GPU; never kills."""
    per_pid, gpus = gpu_compute()
    big = {pid: mem for pid, mem in per_pid.items() if mem >= min_mem}
    if not big:
        return []
    serving = _serving_pids(proc, set(big))
    found = {}
    for pid, mem in big.items():
        st = _status(os.path.join(proc, pid))
        if st is None:
            continue
        uid, _ppid, comm = st
        if uid < min_uid or (ignore_users and _name(uid) in ignore_users):
            continue
        try:
            argv = [x.decode("utf-8", "replace")
                    for x in _read(os.path.join(proc, pid, "cmdline")).split(b"\0") if x]
        except OSError:
            argv = []
        names = _candidates(argv, comm) if argv else [((comm or "").lower(), False)]
        aka = sorted({safe(n) for n, _ in names} - {"?"})
        if ignore_programs and aka and set(aka) <= set(ignore_programs):
            continue
        program = safe(argv[0]) if argv and safe(argv[0]) != "?" else safe(comm)
        agent = next((_is(nm, agents, sc) for nm, sc in names if _is(nm, agents, sc)), "")
        ports = serving.get(pid, [])
        listens = bool(ports)
        served = listens or uid in exposure_uids
        # "via": a port of its own open to the network, or its account's exposure (a tunnel)
        via = "listen" if listens else ("account" if served else "")
        ephemeral = listens and all(e for _p, e in ports)   # every open port is an ephemeral one
        kind = "gpu_served" if served else "gpu"
        detail = str(min(p for p, _e in ports)) if ports else ""
        key = (uid, kind, program, detail)
        n, a2, memmax, ag, det, vv, ep = found.get(key, (0, set(), 0, "", False, "", True))
        found[key] = (n + 1, a2 | set(aka), max(memmax, mem), ag or (agent or ""),
                      det or _detached(proc, pid), vv or via, ep and ephemeral)
    rows, per = [], {}
    for (uid, kind, program, detail), (n, aka, mem, agent, det, via, ephemeral) in sorted(
            found.items(), key=lambda kv: str(kv[0])):
        g = (uid, kind)
        per[g] = per.get(g, 0) + 1
        if per[g] > MAX_PER:
            continue
        rows.append({"uid": uid, "user": _name(uid), "kind": kind, "program": program, "detail": detail,
                     "aka": sorted(aka), "processes": n,
                     "gpu": {"mem_mb": mem, "agent": agent or "", "detached": bool(det),
                             "served": kind == "gpu_served", "via": via,
                             "ephemeral": bool(ephemeral and via == "listen")}})
    for (uid, kind), total in per.items():
        if total > MAX_PER:
            rows.append({"uid": uid, "user": _name(uid), "kind": kind, "program": f"+{total - MAX_PER} more",
                         "detail": "", "aka": [], "processes": 0, "gpu": {}})
    return rows


# -- heavy use on a login node (1.22) -------------------------------------------------

def _uptime(proc):
    try:
        with open(os.path.join(proc, "uptime")) as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def heavy(proc, min_uid, cores=HEAVY_CORES, min_seconds=HEAVY_MIN_SECONDS,
          ignore_users=frozenset(), ignore_programs=frozenset()):
    """User processes using real CPU by lifetime average -- their whole CPU
    time (utime+stime) over their age -- of at least ``cores`` cores, having
    used at least ``min_seconds`` CPU-seconds (so a brief spike doesn't count).
    The shape of a computation run where it shouldn't be; the collector keeps
    it only for a login host (a compute node is where jobs are meant to be
    heavy). Single-shot, from /proc/<pid>/stat (real uid from status, as the
    escalation check does). At most five per account, then +N more."""
    up = _uptime(proc)
    if up <= 0:
        return []
    hz = HZ or 100
    found = {}
    try:
        names = os.listdir(proc)
    except OSError:
        names = []
    for d in names:
        if not d.isdigit():
            continue
        st = _status(os.path.join(proc, d))
        if st is None:
            continue
        uid, _ppid, comm = st
        if uid < min_uid or (ignore_users and _name(uid) in ignore_users):
            continue
        try:
            stat = _read(os.path.join(proc, d, "stat"), 4096).decode("utf-8", "replace")
            after = stat.rsplit(")", 1)[1].split()
            cpu_sec = (int(after[11]) + int(after[12])) / hz        # utime + stime
            age = up - int(after[19]) / hz                          # now - starttime
            rss_pages = int(after[21])
        except (OSError, IndexError, ValueError, ZeroDivisionError):
            continue
        if age <= 0 or cpu_sec < min_seconds:
            continue
        avg = cpu_sec / age
        if avg < cores:
            continue
        try:
            argv0 = _read(os.path.join(proc, d, "cmdline"), 4096).split(b"\0")[0].decode("utf-8", "replace")
        except OSError:
            argv0 = ""
        aka = sorted({safe(argv0), safe(comm)} - {"?", ""})
        if ignore_programs and aka and set(aka) <= set(ignore_programs):
            continue
        program = safe(argv0) if safe(argv0) != "?" else safe(comm)
        key = (uid, program)
        n, a2, mx_cpu, mx_rss = found.get(key, (0, set(), 0.0, 0))
        found[key] = (n + 1, a2 | set(aka), max(mx_cpu, avg), max(mx_rss, rss_pages))
    rows, per = [], {}
    for (uid, program), (n, aka, avg, rss) in sorted(found.items(), key=lambda kv: str(kv[0])):
        per[uid] = per.get(uid, 0) + 1
        if per[uid] > MAX_PER:
            continue
        rows.append({"uid": uid, "user": _name(uid), "kind": "heavy", "program": program, "detail": "",
                     "aka": sorted(aka), "processes": n,
                     "heavy": {"cores": round(avg, 1), "rss_mb": int(rss * PAGE / (1024 * 1024))}})
    for uid, total in per.items():
        if total > MAX_PER:
            rows.append({"uid": uid, "user": _name(uid), "kind": "heavy", "program": f"+{total - MAX_PER} more",
                         "detail": "", "aka": [], "processes": 0, "heavy": {}})
    return rows


def main():
    a = _args()
    ssh = {}
    found, seen, others, others_cmd = processes(a["proc"], a["min_uid"], a["ignore_users"], a["ignore_programs"],
                                                ssh=ssh, agents=a["agents"])
    uids = {f["uid"] for f in found if f["kind"] in FORWARDS}

    def system(uid):
        return uid < a["min_uid"] or _name(uid) in a["ignore_users"]

    accounts = reach(a["proc"], uids, a["trusted"], a["campus"], system, a["cluster"]) if uids else {}
    check(found, accounts, ssh)
    found = found + escalations(a["proc"], a["min_uid"], a["ignore_programs"])
    if a["gpu"]:
        # An account that makes its traffic reachable from outside -- a tunnel,
        # relay, SOCKS or an unverified reverse forward (a verified -R is the
        # admin's own) -- serves any GPU it holds, even where its own listener
        # can't be read.
        exposure = {f["uid"] for f in found if f["kind"] in EXPOSURE_KINDS
                    and not (f["kind"] == "ssh_reverse" and f.get("verified"))}
        found = found + gpu_findings(a["proc"], a["min_uid"], a["agents"], a["ignore_users"],
                                     a["ignore_programs"], exposure, a["gpu_min_mem"])
    if a["heavy"]:
        found = found + heavy(a["proc"], a["min_uid"], a["heavy_cores"], a["heavy_min"],
                              a["ignore_users"], a["ignore_programs"])
    out = {
        "host": socket.gethostname().split(".")[0],
        "uid": os.getuid(),
        "hidepid": hidepid(a["proc"]),
        "processes": seen,
        "others": others,
        "others_readable": others_cmd,
        "programs_of_listeners": os.getuid() == 0,
        "addresses": sorted(str(x) for x in own_addresses(a["proc"]) if not (x.is_loopback or x.is_link_local)),
        "cluster": sorted(str(n) for n in cluster_networks(a["proc"], a["cluster"])),
        "findings": found,
        "listeners": listeners(a["proc"], a["min_uid"], a["ignore_users"]),
        "root": root_signals(a["root_report"]),
    }
    sys.stdout.write(json.dumps(out) + "\n")


if __name__ == "__main__":
    main()

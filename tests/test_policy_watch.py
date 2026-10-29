# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The policy watch (1.19): tunnels, ssh reverse forwards and SOCKS
proxies, VS Code tunnels, miners, relays and services open to the network,
from /proc, on login and compute nodes; kept without command lines."""
import json
import os
import re
import sqlite3
import subprocess
import sys

import pytest
from click.testing import CliRunner

from nomad.collectors import policy_watch as pw
from nomad.collectors import watch_probe as probe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(ROOT, "nomad", "collectors", "watch_probe.py")

CMDLINES = {
    101: ["ngrok", "http", "8080"],
    102: ["/usr/bin/ssh", "-N", "-R", "8080:localhost:22", "me@outside.example.org"],
    103: ["ssh", "-fND", "1080", "me@outside.example.org"],
    104: ["ssh", "-L", "8888:spdr12:8888", "spydur"],                 # a local forward: fine
    105: ["/opt/xmrig/xmrig", "-o", "pool.example:3333"],
    106: ["python3", "run.py", "--url", "stratum+tcp://pool.example:3333"],
    107: ["socat", "TCP-LISTEN:9000,fork", "TCP:inside:22"],
    108: ["socat", "-", "TCP:inside:80"],                              # not listening: fine
    109: ["/home/amy/bin/code", "tunnel", "--accept-server-license-terms"],
    110: ["/home/amy/.vscode-server/bin/abc/node", "server.js"],     # Remote-SSH: fine
    111: ["tailscaled", "--tun=userspace-networking"],
    112: ["ssh", "-o", "RemoteForward=2222 localhost:22", "me@outside.example.org"],
    113: ["ssh", "-oDynamicForward=1081", "me@outside.example.org"],
    114: ["sshd:", "amy@pts/3"],
    115: ["python3", "-m", "jupyter", "lab", "--ip=0.0.0.0"],
}
# /proc/net/tcp: sl local remote st ... uid timeout inode
TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:22B8 00000000:0000 0A 00000000:00000000 00:00000000 00000000  {uid}        0 5001 1
   1: 0100007F:2382 00000000:0000 0A 00000000:00000000 00:00000000 00000000  {uid}        0 5002 1
   2: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 5003 1
   3: 0A00000B:A1B2 08080808:01BB 01 00000000:00000000 00:00000000 00000000  {uid}        0 5004 1
"""
TCP6 = """  sl  local_address                         remote_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000000000000000000001000000:1F90 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  {uid}        0 6001 1
"""


@pytest.fixture
def proc(tmp_path):
    p = tmp_path / "proc"
    (p / "net").mkdir(parents=True)
    uid = os.getuid()
    for pid, argv in CMDLINES.items():
        (p / str(pid)).mkdir()
        (p / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (p / "net" / "tcp").write_text(TCP.format(uid=1500))      # a person's sockets (sshd's is root's)
    (p / "net" / "tcp6").write_text(TCP6.format(uid=1500))
    (p / "mounts").write_text("proc /proc proc rw,nosuid,nodev,noexec,relatime 0 0\n")
    # the jupyter process holds the socket open to the network
    (p / "115" / "fd").mkdir()
    os.symlink("socket:[5001]", p / "115" / "fd" / "3")
    return p


def test_the_probe_finds_what_it_should(proc):
    found, seen, others, readable = probe.processes(str(proc), 0)
    got = {(f["kind"], f["program"], f["detail"]) for f in found}
    assert got == {
        ("tunnel", "ngrok", ""), ("ssh_reverse", "ssh", "8080"), ("ssh_socks", "ssh", "1080"),
        ("miner", "xmrig", ""), ("miner", "python3", "stratum"), ("relay", "socat", ""),
        ("code_tunnel", "code", ""), ("tunnel", "tailscaled", ""), ("ssh_reverse", "ssh", "2222"),
        ("ssh_socks", "ssh", "1081"),
    }
    assert seen == len(CMDLINES)
    # nothing of a command line is kept but the program and a port
    assert "outside.example.org" not in json.dumps(found) and "pool.example" not in json.dumps(found)


def test_the_probe_reads_listening_sockets_by_owner(proc):
    ls = probe.listeners(str(proc), 1000)
    by = {(s["port"], s["scope"]): s["program"] for s in ls}
    # root reads every process's fds; another account only its own
    want_prog = "python3" if os.getuid() == 0 else ""
    assert by == {(8888, "network"): want_prog, (9090, "loopback"): "", (8080, "loopback"): ""}
    assert not any(s["ephemeral"] for s in ls)
    assert probe.listeners(str(proc), 2000) == []                    # below min_uid: not people


def test_hidepid_is_told(proc):
    assert not probe.hidepid(str(proc))
    (proc / "mounts").write_text("proc /proc proc rw,relatime,hidepid=invisible 0 0\n")
    assert probe.hidepid(str(proc))


def _fw(port, far="loopback", to="this host", kind="R", proxied=False):
    return {"kind": kind, "port": port, "far": far, "to": to, "proxied": proxied}


@pytest.mark.parametrize("argv, want", [
    (["ssh", "-R", "0:localhost:22", "me@out.example.org"], [_fw("0")]),
    (["ssh", "-R8022:localhost:22", "login01"], [_fw("8022")]),
    (["ssh", "-p", "2200", "-R", "[::]:9000:localhost:22", "10.1.2.3"], [_fw("9000", "every address")]),
    (["ssh", "-D", "127.0.0.1:1080", "10.0.0.5"], [_fw("1080", kind="D", to="socks")]),
    (["ssh", "me@out.example.org", "-R", "2222:localhost:22"], [_fw("2222")]),     # after the host
    (["ssh", "h", "uptime", "-R", "9"], []),                                    # the command's, not ssh's
    (["ssh", "-NoRemoteForward=2222:localhost:22", "h"], [_fw("", "unknown", "unknown")]),   # ssh refuses it
    (["ssh", "-No", "RemoteForward 2222 localhost:22", "h"], [_fw("2222")]),
    (["ssh", "-fNo", "DynamicForward=1080", "h"], [_fw("1080", kind="D", to="socks")]),
    (["ssh", "-o", "RemoteForward\t2223 localhost:22", "h"], [_fw("2223")]),
    (["ssh", "-w", "0:0", "h"], [{"kind": "w", "port": "0", "far": "", "to": "", "proxied": False}]),
    (["ssh", "-R", "/tmp/sock:/run/x.sock", "h"], [_fw("socket", "a socket", "a socket")]),
    (["ssh", "--", "h", "-R", "9"], []),
    (["ssh", "-R", "2222", "h"], [_fw("2222", to="socks")]),                     # a SOCKS proxy on the far end
    (["ssh", "-R", "*:2222:localhost:22", "h"], [_fw("2222", "every address")]),
    (["ssh", "-R", "10.0.0.9:2222:localhost:22", "h"], [_fw("2222", "an address")]),
    (["ssh", "-R", "2222:node05:22", "h"], [_fw("2222", to="another host")]),
    # what a user controls: a proxy or a jump, a host name of its own
    (["ssh", "-o", "HostName=evil.example.com", "-R", "9:localhost:22", "login01"], [_fw("9", proxied=True)]),
    (["ssh", "-J", "me@evil.example.com", "-R", "9:localhost:22", "login01"], [_fw("9", proxied=True)]),
    (["ssh", "-o", "ProxyCommand=nc evil 22", "-R", "9:localhost:22", "login01"], [_fw("9", proxied=True)]),
    (["ssh", "-o", "ProxyUseFdpass=yes", "-R", "9:localhost:22", "login01"], [_fw("9", proxied=True)]),
    (["ssh", "-R", "9:localhost:22", "ssh://login01"], [_fw("9", proxied=True)]),
])
def test_ssh_forwards(argv, want):
    assert probe._forwards(argv) == want


def test_ssh_forwards_as_ssh_itself_reads_them():
    """Against ssh -G (what ssh would set up, without connecting), where ssh is here."""
    import itertools
    import shutil
    if not shutil.which("ssh"):
        pytest.skip("no ssh here")

    def ssh_says(args):
        r = subprocess.run(["ssh", "-G", "-F", "/dev/null", *args], capture_output=True, text=True, timeout=10,
                           env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})
        if r.returncode != 0:
            return None
        out = []
        for line in r.stdout.splitlines():
            k, _, v = line.partition(" ")
            if k in ("remoteforward", "dynamicforward"):
                nums = re.findall(r"\d+", v.split()[0])
                out.append(("R" if k == "remoteforward" else "D", nums[-1] if nums else "socket"))
        return sorted(out)

    forms = [["-R", "8080:localhost:22"], ["-R8080:localhost:22"], ["-R", "[::]:8082:localhost:22"],
             ["-D", "1080"], ["-D1081"], ["-o", "RemoteForward=2222 localhost:22"],
             ["-o", "RemoteForward 2223 localhost:22"], ["-oRemoteForward=2224 localhost:22"],
             ["-o", "dynamicforward 1084"], ["-NfR", "9000:localhost:22"], ["-fND", "1085"],
             ["-NoRemoteForward=2225:localhost:22"], ["-No", "RemoteForward 2226 localhost:22"],
             ["-o", "RemoteForward" + " " * 3000 + "2227 localhost:22"], ["-p", "2200", "-R", "9001:l:22"],
             ["-L", "8888:node:8888"], ["-o", "RemoteForward\t2228 localhost:22"]]
    checked = 0
    for f, after in itertools.product(forms, (False, True)):
        args = ["h", *f] if after else [*f, "h"]
        want = ssh_says(args)
        if want is None:
            continue
        assert sorted((f["kind"], f["port"]) for f in probe._forwards(["ssh", *args]) if f["kind"] in "RD") == want, \
            args[:4]
        checked += 1
    assert checked > 20


def test_a_long_option_takes_no_time():
    import time
    t = time.time()
    for n in (16000, 65000, 200000):
        probe._classify(["ssh", "-o", "RemoteForward" + " " * n, "h"], "ssh")
    assert time.time() - t < 0.5


def test_addresses_as_the_kernel_writes_them():
    assert probe._ip("0100007F") == "127.0.0.1" and probe._ip("0A00000B") == "11.0.0.10"
    assert probe._ip("0000000000000000FFFF00000B00000A") == "10.0.0.11"          # IPv4-mapped
    assert probe._ip("00000000000000000000000001000000") == "::1"


@pytest.mark.parametrize("argv, comm, kind, program", [
    (["foo"], "ngrok", "tunnel", "ngrok"),                                     # exec -a foo ./ngrok
    (["/bin/sh", "/home/a/pagekite.py", "8080"], "pagekite.py", "tunnel", "pagekite.py"),
    (["./cloudflared-linux-amd64", "tunnel", "run"], "cloudflared-lin", "tunnel", "cloudflared"),
    (["./chisel_1.10.1_linux_amd64", "server"], "chisel_1.10.1_l", "tunnel", "chisel"),
    (["python3", "-m", "sshuttle", "-r", "h"], "python3", "tunnel", "sshuttle"),   # a module
    (["python3", "train.py", "--epochs", "3"], "python3", None, None),
    (["python3", "rigel_fit.py"], "python3", None, None),                       # a script: exact names only
    (["python3", "squid_assembly.py"], "python3", None, None),
    (["python3", "naive.py", "100"], "python3", None, None),
    (["myjob", "-N", "-R", "7001:localhost:80", "evil.example.com"], "ssh", "ssh_reverse", "ssh"),  # exec -a
    (["grep", "-o", "stratum+tcp://pool:3333"], "myminer", "miner", "grep"),     # not a text tool, by comm
    (["myjob", "TCP-LISTEN:9000,fork", "TCP:in:22"], "socat", "relay", "socat"),
    (["node", "/usr/lib/node_modules/localtunnel/bin/lt.js", "--port", "8000"], "node", "tunnel", "localtunnel"),
    (["code", "--verbose", "tunnel"], "code", "code_tunnel", "code"),
    (["code", "--cli-data-dir", "x", "tunnel"], "code", "code_tunnel", "code"),
    (["./x", "--donate-level", "1", "-a", "rx/0"], "x", "miner", "x"),
    (["grep", "-r", "stratum+tcp://", "logs/"], "grep", None, None),            # a search, not a miner
    (["./naive", "data.csv"], "naive", None, None),                             # coursework
    (["./naive", "--listen=socks://127.0.0.1:1080"], "naive", "tunnel", "naive"),
    (["./bore", "local", "8000", "--to", "bore.pub"], "bore", "tunnel", "bore"),
    (["./naive", "100"], "naive", None, None),
    (["sing-box", "run"], "sing-box", "relay", "sing-box"),
])
def test_names_as_a_process_goes_by_them(argv, comm, kind, program):
    got = [(k, p) for k, p, _d, _s, _a in probe._classify(argv, comm)]
    assert got == ([] if kind is None else [(kind, program)]), got


def test_names_kept_are_short_safe_words():
    assert probe.safe("redis-server 10.1.2.3:6379") == "redis-server"
    assert probe.safe("x\rwarning  oknode  nobody") == "?"
    assert probe.safe("\x1b[2Jboom") == "?"
    assert probe.safe("/opt/a/very-long-name-that-goes-on-and-on-and-on") == "?"


def test_many_renamed_processes_are_one_line_more(tmp_path):
    p = tmp_path / "proc"
    p.mkdir()
    for i in range(30):
        (p / str(200 + i)).mkdir()
        (p / str(200 + i) / "cmdline").write_bytes(f"m{i}\0stratum+tcp://x:1\0".encode())
    found = probe.processes(str(p), 0)[0]
    assert len(found) == probe.MAX_PER + 1 and found[-1]["program"] == "+25 more"


def test_the_probe_runs_as_python_minus_I(proc):
    r = subprocess.run([sys.executable, "-I", "-", "--min-uid", "0", "--proc", str(proc)],
                       input=open(PROBE).read(), capture_output=True, text=True, timeout=60)
    d = json.loads(r.stdout)
    assert r.returncode == 0 and len(d["findings"]) == 10 and d["processes"] == len(CMDLINES)
    r = subprocess.run([sys.executable, "-I", "-S", "-", "--min-uid", "0", "--proc", str(proc)],
                       input=open(PROBE).read(), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and json.loads(r.stdout)["findings"]        # without site-packages too


def test_the_probe_is_python_36():
    """Compute nodes and old login nodes have Python 3.6: no syntax after it."""
    import ast
    tree = ast.parse(open(PROBE).read())
    for node in ast.walk(tree):
        assert not isinstance(node, getattr(ast, "NamedExpr", ())), "walrus: 3.8"
        if isinstance(node, ast.FunctionDef):
            assert not node.args.posonlyargs, "positional-only parameters: 3.8"
    src = open(PROBE).read()
    for later in ("capture_output", "text=True", "from dataclasses", "removeprefix", "match "):
        assert later not in src, later


# ── the collector ───────────────────────────────────────────────────────────

def answer(user="amy", uid=1500, host="spdr12", extra=()):
    return {"host": host, "uid": 990, "hidepid": False, "processes": 300, "others": 290,
            "others_readable": 200,
            "findings": [{"uid": uid, "user": user, "kind": "tunnel", "program": "cloudflared", "detail": "",
                          "aka": ["cloudflared"], "processes": 1},
                         {"uid": uid, "user": user, "kind": "ssh_socks", "program": "ssh", "detail": "1080",
                          "processes": 1}, *extra],
            "listeners": [{"uid": uid, "user": user, "port": 8888, "scope": "network", "program": "python3"},
                          {"uid": uid, "user": user, "port": 41234, "scope": "loopback", "program": ""}]}


def test_an_ignored_name_leaves_out_only_what_goes_by_it_alone():
    a = answer(extra=[{"uid": 1500, "user": "amy", "kind": "tunnel", "program": "tailscaled", "detail": "",
                       "aka": ["cloudflared", "tailscaled"], "processes": 1}])     # exec -a tailscaled ./cloudflared
    kept = pw.findings_of(a, "spydur", "login", ignore_programs=["tailscaled"])
    assert ("tunnel", "tailscaled") in {(f["kind"], f["program"]) for f in kept}
    a["findings"][-1]["aka"] = ["tailscaled"]
    assert ("tunnel", "tailscaled") not in {(f["kind"], f["program"]) for f in
                                            pw.findings_of(a, "spydur", "login", ignore_programs=["tailscaled"])}


def test_what_a_user_writes_lowers_nothing():
    a = answer(extra=[{"uid": 1500, "user": "amy", "kind": "ssh_reverse", "program": "ssh", "detail": "8888",
                       "inside": True, "processes": 1},                          # 1.19.0's hint: no pass
                      {"uid": 1500, "user": "amy", "kind": "ssh_reverse", "program": "ssh", "detail": "2222",
                       "verified": True, "why": {}, "far": "every address", "to": "this host", "proxied": False,
                       "processes": 1}])                                          # a shape that's never verified
    a["listeners"].append({"uid": 1500, "user": "amy", "port": 45123, "scope": "network", "program": "",
                           "ephemeral": True})
    for host, role in (("spdr12", "node"), ("spydur", "login")):
        sev = {(f["kind"], f["detail"]): f["severity"] for f in pw.findings_of(a, host, role, listeners_on_nodes=True)}
        assert sev[("ssh_reverse", "8888")] == "warning"
        assert sev[("ssh_reverse", "2222")] == "warning"
        assert sev[("ssh_socks", "1080")] == "warning"                           # traffic out through another
        assert sev[("listening", "45123")] == ("info" if role == "node" else "warning")


def test_service_accounts_and_this_one_are_left_out():
    import pwd
    me = pwd.getpwuid(os.getuid()).pw_name
    c = pw.PolicyWatchCollector({}, ":memory:")
    assert {"slurm", "munge", me} <= set(c.ignore_users)
    assert me not in pw.PolicyWatchCollector({"watch_self": True, "ignore_users": []}, ":memory:").ignore_users
    args = c._args()
    assert "--ignore-users" in args and "slurm" in args[args.index("--ignore-users") + 1]


def test_listeners_are_capped_per_account(tmp_path):
    p = tmp_path / "proc"
    (p / "net").mkdir(parents=True)
    rows = ["  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"]
    for i in range(40):
        rows.append(f"   {i}: 00000000:{20000 + i:04X} 00000000:0000 0A 00000000:00000000 00:00000000 00000000"
                    f"  1500        0 {9000 + i} 1")
    (p / "net" / "tcp").write_text("\n".join(rows) + "\n")
    ls = probe.listeners(str(p), 1000)
    assert len(ls) == probe.MAX_PER + 1 and ls[-1]["port"] == "+35 more"
    f = pw.findings_of({"findings": [], "listeners": ls}, "spydur", "login")
    assert len(f) == probe.MAX_PER + 1 and f[-1]["program"] == "+35 more" and f[-1]["severity"] == "warning"


def test_ipv6_loopback_and_link_local_are_never_outside():
    import ipaddress
    assert probe._addr("00000000000000000000000001000000").is_loopback
    assert probe._in(ipaddress.ip_address("fe80::1"), probe.LINK_LOCAL)
    assert probe._in(ipaddress.ip_address("169.254.1.2"), probe.LINK_LOCAL)
    assert not probe._in(ipaddress.ip_address("2606:4700::1111"), probe.PRIVATE + probe.LINK_LOCAL)


def test_a_fixed_interpreter_without_site_packages_over_plain_ssh(monkeypatch):
    c = pw.PolicyWatchCollector({}, ":memory:")
    cmd = c.remote_command()
    assert "exec /usr/bin/python3 -I -S -" in cmd and "exec /usr/libexec/platform-python -I -S -" in cmd
    assert "python3 -I -S" in cmd and " python3 -" not in cmd.replace("/usr/bin/python3", "")
    seen = {}
    monkeypatch.setattr(pw.subprocess, "run", lambda argv, **k: seen.setdefault("argv", argv) and
                        subprocess.CompletedProcess(argv, 0, pw.MARK + "\n" + json.dumps(answer()), ""))
    c.probe_over_ssh("/usr/bin/ssh", "spdr12")
    a = seen["argv"]
    assert "-a" in a and "-x" in a and "ClearAllForwardings=yes" in a and a[a.index("--") + 1] == "spdr12"
    assert pw.PolicyWatchCollector({"python": ["/opt/py; rm -rf ~", "/usr/bin/python3\nrm -rf ~",
                                               "/usr/bin/python3"]}, ":memory:").pythons == ["/usr/bin/python3"]
    assert "[ -f /usr/bin/python3 ] && [ -x /usr/bin/python3 ]" in cmd


def test_findings_on_login_and_compute_nodes():
    login = pw.findings_of(answer(), "spydur", "login")
    node = pw.findings_of(answer(), "spdr12", "node")
    assert {f["kind"] for f in login} == {"tunnel", "ssh_socks", "listening"}
    assert {f["kind"] for f in node} == {"tunnel", "ssh_socks"}       # MPI, Dask, Ray listen on nodes
    assert {f["kind"] for f in pw.findings_of(answer(), "spdr12", "node", listeners_on_nodes=True)} == \
        {"tunnel", "ssh_socks", "listening"}
    assert pw.findings_of(answer(), "spydur", "login", ignore_users=["amy"]) == []
    assert {f["kind"] for f in pw.findings_of(answer(), "spydur", "login", ignore_programs=["cloudflared"],
                                              ignore_ports=[8888])} == {"ssh_socks"}


def test_parse_probe_past_a_banner():
    out = "Welcome to spydur!\nlast login ...\n" + pw.MARK + "\n" + json.dumps(answer()) + "\n"
    assert pw.parse_probe(out)["host"] == "spdr12"
    assert pw.parse_probe("no json here") is None


def dispatcher_like(sent):
    """send_alert as the dispatcher answers within an episode: held back
    (Held) once a subject has been raised."""
    from nomad.alerts.dispatcher import Held
    raised = set()

    def send(*a, **k):
        sent.append((a, k))
        if k.get("subject") in raised:
            return Held()
        raised.add(k.get("subject"))
        return {"slack": True}
    return send


@pytest.fixture
def watch(db_path, monkeypatch):
    c = pw.PolicyWatchCollector({"enabled": True, "nodes": ["spdr12", "spdr13"]}, db_path)
    assert "amy" not in c.ignore_users
    monkeypatch.setattr(c, "probe_here", lambda: (answer(host="spydur"), ""))
    answers = {"spdr12": (answer(), ""), "spdr13": (None, "Permission denied (publickey)")}
    monkeypatch.setattr(c, "probe_over_ssh", lambda ssh, h: answers[h])
    monkeypatch.setattr(pw, "find_tool", lambda name: "/usr/bin/" + name)
    sent = []
    import nomad.alerts as alerts
    monkeypatch.setattr(alerts, "send_alert", dispatcher_like(sent))
    return c, sent, db_path


def _rows(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT host, role, username, kind, program, detail, sweeps, first_seen, last_seen "
                            "FROM policy_findings ORDER BY host, kind").fetchall()


def test_a_sweep_keeps_findings_and_what_each_host_could_see(watch):
    c, sent, db = watch
    data = c.collect()
    c.store(data)
    rows = _rows(db)
    assert {(r[0], r[3]) for r in rows} == {("spydur", "tunnel"), ("spydur", "ssh_socks"), ("spydur", "listening"),
                                            ("spdr12", "tunnel"), ("spdr12", "ssh_socks")}
    with sqlite3.connect(db) as conn:
        hosts = conn.execute("SELECT host, role, answered, error FROM policy_watch_hosts ORDER BY host").fetchall()
    assert hosts == [("spdr12", "node", 1, None), ("spdr13", "node", 0, "Permission denied (publickey)"),
                     ("spydur", "login", 1, None)]
    # alerts: one per account and kind; naming no one; keyed by finding number
    msgs = [k.get("subject") or "" for a, k in sent]
    assert not any("amy" in str(a) + str(k) or "1500" in str(k) for a, k in sent)
    assert all(re.fullmatch(r"(tunnel|listening|ssh_socks):f\d+(:new)?", m) for m in msgs)
    assert all(k["details"]["episode_gap_minutes"] >= 60 for a, k in sent)
    # the same tunnel on two hosts: one alert (and one for what was new in it)
    assert sum(1 for m in msgs if m.startswith("tunnel:") and ":new" not in m) == 1


def test_a_second_sweep_counts_rather_than_repeats(watch, monkeypatch):
    c, sent, db = watch
    c.store(c.collect())
    first = {(r[0], r[3]): r[7] for r in _rows(db)}
    with sqlite3.connect(db) as conn:                                # as if an interval had passed
        conn.execute("UPDATE policy_watch_sweeps SET timestamp = '2000-01-01T00:00:00'")
    c.store(c.collect())
    rows = _rows(db)
    assert len(rows) == 5 and all(r[6] == 2 for r in rows)
    assert {(r[0], r[3]): r[7] for r in rows} == first and len(c.new) == 0


def test_a_new_finding_in_a_known_group_alerts(watch, monkeypatch):
    c, sent, db = watch
    c.store(c.collect())
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE policy_watch_sweeps SET timestamp = '2000-01-01T00:00:00'")
    a = answer(host="spydur")
    a["listeners"].append({"uid": 1500, "user": "amy", "port": 7000, "scope": "network", "program": ""})
    monkeypatch.setattr(c, "probe_here", lambda: (a, ""))
    sent.clear()
    c.store(c.collect())
    news = [(a, k) for a, k in sent if ":new" in k["subject"]]
    assert len(news) == 1 and "port 7000" in news[0][0][2] and "port 8888" not in news[0][0][2]
    assert news[0][1]["subject"].endswith(":new")             # one key: the dispatcher spaces them out


def test_a_host_gone_quiet_alerts(watch, monkeypatch):
    c, sent, db = watch
    for i in range(4):
        with sqlite3.connect(db) as conn:
            conn.execute("UPDATE policy_watch_sweeps SET timestamp = '2000-01-01T00:00:00'")
        if i == 1:
            monkeypatch.setattr(c, "probe_over_ssh", lambda ssh, h: (None, "timed out"))
        sent.clear()
        c.store(c.collect())
    quiet = [k["subject"] for a, k in sent if k["subject"].startswith("silent:")]
    assert quiet == ["silent:spdr12"]                     # spdr13 never answered: not news
    with sqlite3.connect(db) as conn:                      # a week later: its last answer is still known
        conn.execute("UPDATE policy_watch_hosts SET timestamp = '2000-01-01T00:00:00'")
        conn.execute("UPDATE policy_watch_sweeps SET timestamp = '2000-01-01T00:00:00'")
    c.store(c.collect())
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM policy_watch_hosts WHERE host = 'spdr12' AND answered = 1"
                            ).fetchone()[0] == 1


def test_not_again_within_the_interval(watch):
    c, sent, db = watch
    c.store(c.collect())
    assert c.collect() == [] and "swept less than" in c.note


def test_owners_in_alerts_when_the_site_wants(watch):
    c, sent, db = watch
    c.owners_in_alerts = True
    c.store(c.collect())
    assert any("by amy" in a[2] for a, k in sent)


# ── nomad watch ─────────────────────────────────────────────────────────────

def test_watch_list_and_status(watch):
    from nomad.watch.cli import watch_group
    c, sent, db = watch
    c.store(c.collect())
    r = CliRunner().invoke(watch_group, ["list", "--db", db])
    assert r.exit_code == 0, r.output
    assert "amy" in r.output and "a tunnel program cloudflared" in r.output and "port 8888" in r.output
    r = CliRunner().invoke(watch_group, ["list", "--db", db, "--mask"])
    assert "amy" not in r.output and "spydur" not in r.output and "user1" in r.output
    r = CliRunner().invoke(watch_group, ["list", "--db", db, "--json"])
    assert {f["kind"] for f in json.loads(r.output)} >= {"tunnel", "listening"}
    r = CliRunner().invoke(watch_group, ["status", "--db", db])
    assert "2 of 3 hosts answered" in r.output and "Permission denied" in r.output
    assert "Not root: which program listens" in r.output
    r = CliRunner().invoke(watch_group, ["list", "--db", db])
    assert "an ssh SOCKS proxy" in r.output


def test_printed_fields_have_no_control_characters(watch, monkeypatch):
    from nomad.watch.cli import watch_group
    c, sent, db = watch
    c.store(c.collect())
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE policy_findings SET program = 'x\rfake line\x1b[2J'")
    r = CliRunner().invoke(watch_group, ["list", "--db", db])
    assert "\r" not in r.output and "\x1b" not in r.output


def test_probe_json_masked():
    from nomad.watch.cli import watch_group
    r = CliRunner().invoke(watch_group, ["probe", "--json", "--mask"], obj={"config": {}})
    d = json.loads(r.output)
    assert d["host"] == "host1" and all(f["uid"] is None for f in d["findings"] + d["listeners"])


def test_watch_probe_here():
    from nomad.watch.cli import watch_group
    r = CliRunner().invoke(watch_group, ["probe"], obj={"config": {}})
    assert r.exit_code == 0, r.output
    assert "processes" in r.output


def test_the_watch_is_planned_migrated_and_its_sweeps_stay_home(db_path):
    from nomad.collectors.plan import NAMES, SPECS
    from nomad.db.delta_hub import SKIP_TABLES
    from nomad.db.migrations import MIGRATIONS
    assert "policy_watch" in NAMES
    assert not next(s for s in SPECS if s.name == "policy_watch").default_on        # off by default
    assert any(v == 26 and "policy_watch" in d for v, d, _ in MIGRATIONS)
    assert any(v == 27 and "policy_findings" in d for v, d, _ in MIGRATIONS)
    assert {"policy_watch_sweeps", "policy_watch_hosts"} <= SKIP_TABLES and "policy_findings" not in SKIP_TABLES
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM policy_findings").fetchone() == (0,)

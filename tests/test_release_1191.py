# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.19.1: an ssh reverse forward to a server the site lists, verified from
what the kernel shows (where every socket of its account goes, which ssh
of it has a child), never from what a user wrote; listed, not alerted. A
forward's shape as ssh reads it; reach (inside, campus, outside) as a
label; `nomad watch probe` without the collector's INFO line."""
import ipaddress
import json
import logging
import os
import re
import sqlite3
import subprocess

import pytest
from click.testing import CliRunner

from nomad.collectors import policy_watch as pw
from nomad.collectors import watch_probe as probe

ME = os.getuid()
SSHD = 99                     # a system account's sshd (system=: uid 99)
VADUR = "172.28.122.224"


def hex4(a):
    return "".join(f"{b:02X}" for b in ipaddress.IPv4Address(a).packed[::-1])


def hex6(a):
    p = ipaddress.IPv6Address(a).packed
    return "".join("".join(f"{b:02X}" for b in p[i:i + 4][::-1]) for i in range(0, 16, 4))


HEAD = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def row(i, local, remote, st, uid, six=False):
    (la, lp), (ra, rp) = local, remote
    h = hex6 if six else hex4
    return (f"   {i}: {h(la)}:{lp:04X} {h(ra)}:{rp:04X} {st} 00000000:00000000 00:00000000 00000000 "
            f" {uid}        0 {7000 + i} 1\n")


def make_proc(tmp_path, tcp=(), udp=(), raw=(), tcp6=(), procs=None, sshd=SSHD):
    """A /proc like spydur's: 172.28.8.105 on en0 (the default route), the
    cluster's 10.1.1.0/24 on en1; sshd (uid 99) on port 22."""
    p = tmp_path / "proc"
    (p / "net").mkdir(parents=True)
    (p / "net" / "route").write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        f"en0\t00000000\t{hex4('172.28.8.1')}\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        f"en0\t{hex4('172.28.8.0')}\t00000000\t0001\t0\t0\t0\t{hex4('255.255.252.0')}\t0\t0\t0\n"
        f"en1\t{hex4('10.1.1.0')}\t00000000\t0001\t0\t0\t0\t{hex4('255.255.255.0')}\t0\t0\t0\n")
    (p / "net" / "fib_trie").write_text(
        "Main:\n  +-- 0.0.0.0/0 3 0 5\n     |-- 10.1.1.254\n        /32 host LOCAL\n"
        "     |-- 172.28.8.105\n        /32 host LOCAL\n     |-- 127.0.0.1\n        /32 host LOCAL\n")
    lines = [row(0, ("0.0.0.0", 22), ("0.0.0.0", 0), "0A", sshd)]
    lines += [row(i + 1, *r) for i, r in enumerate(tcp)]
    (p / "net" / "tcp").write_text(HEAD + "".join(lines))
    (p / "net" / "tcp6").write_text(HEAD + "".join(row(50 + i, *r, six=True) for i, r in enumerate(tcp6)))
    (p / "net" / "udp").write_text(HEAD + "".join(row(80 + i, *r) for i, r in enumerate(udp)))
    (p / "net" / "icmp").write_text(HEAD + "".join(row(90 + i, *r) for i, r in enumerate(raw)))
    (p / "mounts").write_text("proc /proc proc rw,relatime 0 0\n")
    for pid, (argv, ppid) in (procs or {}).items():
        (p / str(pid)).mkdir()
        (p / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        comm = os.path.basename(argv[0])[:15]
        (p / str(pid) / "comm").write_text(comm + "\n")
        os.symlink("/usr/bin/" + os.path.basename(argv[0]), p / str(pid) / "exe")      # read as root
        (p / str(pid) / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 " + " ".join(
            ["0"] * 12) + f" {pid * 7}\n")                       # field 22: its start time
    return p


TUNNEL = (("172.28.8.105", 41000), (VADUR, 22), "01", ME)        # the forward's own connection
USED = (("127.0.0.1", 41001), ("127.0.0.1", 22), "01", ME)       # someone on vadur using it
TRUSTED = [(ipaddress.ip_network(VADUR + "/32"), 0)]


def accounts(p, uid=ME, trusted=TRUSTED, campus=()):
    return probe.reach(str(p), {uid}, trusted, campus, system=lambda u: u == SSHD)[uid]


# ── where an account's sockets go ───────────────────────────────────────────

def test_a_forward_to_a_listed_server_and_nothing_else(tmp_path):
    a = accounts(make_proc(tmp_path, tcp=[TUNNEL, USED]))
    assert a == {"reach": "inside", "why": {}, "servers": {0}, "carriers": 1}


def test_ipv4_mapped_ipv6_reads_as_ipv4(tmp_path):
    p = make_proc(tmp_path, tcp6=[(("::ffff:172.28.8.105", 41000), ("::ffff:" + VADUR, 22), "01", ME)])
    assert accounts(p)["why"] == {}


@pytest.mark.parametrize("extra, why, reach", [
    ((("172.28.8.105", 41010), ("140.82.112.3", 443), "01", ME), {"outside": 1}, "outside"),
    ((("172.28.8.105", 41010), ("172.29.3.4", 22), "01", ME), {"campus": 1}, "campus"),      # private, beyond
    ((("172.28.8.105", 41010), ("141.166.180.43", 22), "01", ME), {"campus": 1}, "campus"),  # --campus
    ((("10.1.1.254", 41010), ("10.1.1.5", 6818), "01", ME), {"cluster_port": 1}, "inside"),   # a node's high port
    ((("172.28.8.105", 41010), (VADUR, 2200), "01", ME), {"server_port": 1}, "inside"),
    ((("127.0.0.1", 41010), ("127.0.0.1", 7000), "01", ME), {"host_port": 1}, "inside"),      # its own -L, say
    ((("127.0.0.1", 41010), ("127.0.0.1", 5000), "01", ME), {"host_port": 1}, "inside"),      # another's port
    ((("127.0.0.1", 41010), ("127.0.0.1", 6000), "02", ME), {"host_port": 1}, "inside"),      # nothing there
])
def test_anything_else_it_reaches_keeps_it_unverified(tmp_path, extra, why, reach):
    listeners = [(("127.0.0.1", 7000), ("0.0.0.0", 0), "0A", ME), (("127.0.0.1", 5000), ("0.0.0.0", 0), "0A", 1600)]
    p = make_proc(tmp_path, tcp=[TUNNEL, *listeners, extra])
    a = accounts(p, campus=[ipaddress.ip_network("141.166.0.0/16")])
    assert a["why"] == why and a["reach"] == reach


def test_a_node_below_1024_and_a_system_service_here_are_fine(tmp_path):
    p = make_proc(tmp_path, tcp=[TUNNEL, (("10.1.1.254", 41020), ("10.1.1.5", 22), "01", ME),
                                 (("10.1.1.254", 41021), ("10.1.1.254", 22), "01", ME)])     # its own address
    assert accounts(p)["why"] == {}


def test_connections_into_its_own_port(tmp_path):
    listen = (("0.0.0.0", 8888), ("0.0.0.0", 0), "0A", ME)
    from_node = (("10.1.1.254", 8888), ("10.1.1.7", 51000), "01", ME)
    from_desk = (("172.28.8.105", 8888), ("172.29.3.4", 51000), "01", ME)
    assert accounts(make_proc(tmp_path, tcp=[TUNNEL, listen, from_node]))["why"] == {}
    a = accounts(make_proc(tmp_path / "b", tcp=[TUNNEL, listen, from_desk]))
    assert a["why"] == {"campus": 1}


def test_open_udp_and_raw_sockets(tmp_path):
    udp_any = (("0.0.0.0", 5353), ("0.0.0.0", 0), "07", ME)
    udp_lo = (("127.0.0.1", 5354), ("0.0.0.0", 0), "07", ME)
    assert accounts(make_proc(tmp_path, tcp=[TUNNEL], udp=[udp_any, udp_lo]))["why"] == {"udp": 1}
    ping = (("0.0.0.0", 0), ("0.0.0.0", 0), "07", ME)
    assert accounts(make_proc(tmp_path / "b", tcp=[TUNNEL], raw=[ping]))["why"] == {"raw": 1}


def test_no_connection_to_a_listed_server_or_into_the_cluster(tmp_path):
    assert accounts(make_proc(tmp_path))["why"] == {"none": 1}
    assert accounts(make_proc(tmp_path / "b", tcp=[TUNNEL]), trusted=[])["why"] == {"campus": 1, "none": 1}
    syn = (("172.28.8.105", 41000), (VADUR, 22), "02", ME)              # not connected yet
    assert accounts(make_proc(tmp_path / "c", tcp=[syn]))["why"] == {"none": 1}


def test_cluster_networks_leave_out_the_way_out(tmp_path):
    p = make_proc(tmp_path)
    assert [str(n) for n in probe.cluster_networks(str(p))] == ["10.1.1.0/24"]
    assert {str(a) for a in probe.own_addresses(str(p))} == {"10.1.1.254", "172.28.8.105", "127.0.0.1"}


# ── ssh's processes and the forward's shape ─────────────────────────────────

def _found(p, uid=ME):
    info = {}
    found = probe.processes(str(p), 0, ssh=info)[0]
    acc = probe.reach(str(p), {f["uid"] for f in found}, TRUSTED, (), system=lambda u: u == SSHD)
    probe.check(found, acc, info)
    return [f for f in found if f["kind"] == "ssh_reverse"]


def test_the_vadur_case_is_verified(tmp_path):
    p = make_proc(tmp_path, tcp=[TUNNEL, USED],
                  procs={300: (["ssh", "-fN", "-R", "2222:localhost:22", "vadur"], 1)})
    [f] = _found(p)
    assert f["verified"] is True and f["why"] == {} and f["servers"] == [0] and f["reach"] == "inside"
    assert (f["far"], f["to"], f["proxied"], f["detail"]) == ("loopback", "this host", False, "2222")


@pytest.mark.parametrize("argv, why", [
    (["ssh", "-N", "-R", ":2222:localhost:22", "vadur"], {"far"}),              # every address on vadur
    (["ssh", "-N", "-R", "10.9.9.9:2222:localhost:22", "vadur"], {"far"}),
    (["ssh", "-N", "-R", "2222", "vadur"], {"to"}),                             # SOCKS on vadur, out from here
    (["ssh", "-N", "-R", "2222:spdr12:22", "vadur"], {"to"}),                   # to a node
    (["ssh", "-N", "-R", "2222:/run/docker.sock", "vadur"], {"to"}),
    (["ssh", "-N", "-R", "/tmp/s:localhost:22", "vadur"], {"unchecked"}),
    (["ssh", "-N", "-o", "ProxyJump=vadur", "-R", "2222:localhost:22", "evil"], {"proxied", "relay"}),
    (["ssh", "-N", "-o", "HostName=evil.example.com", "-R", "2222:localhost:22", "vadur"], {"proxied"}),
    (["ssh", "-N", "-R", "${P}:localhost:22", "vadur"], {"unchecked"}),
])
def test_a_shape_that_is_never_verified(tmp_path, argv, why):
    p = make_proc(tmp_path, tcp=[TUNNEL], procs={300: (argv, 1)})
    [f] = _found(p)
    assert f["verified"] is False and set(f["why"]) == why


def test_an_ssh_with_a_child_or_that_relays(tmp_path):
    # ~/.ssh/config: ProxyJump vadur for evil; the jump is a child, its connection only to vadur
    procs = {300: (["ssh", "-N", "-R", "2222:localhost:22", "evil"], 1),
             301: (["ssh", "-W", "[evil]:22", "vadur"], 300)}
    [f] = _found(make_proc(tmp_path, tcp=[TUNNEL], procs=procs))
    assert f["verified"] is False and set(f["why"]) == {"child", "relay", "carrier"}     # two ssh, one connection
    # a relay of its own, not a child: ssh -W reparented, or a local forward from a socket
    for i, relay in enumerate((["ssh", "-W", "evil:22", "vadur"], ["ssh", "-NL", "/tmp/s:evil:22", "vadur"])):
        procs = {300: (["ssh", "-N", "-R", "2222:localhost:22", "vadur"], 1), 302: (relay, 1)}
        [f] = _found(make_proc(tmp_path / str(i), tcp=[TUNNEL, (("172.28.8.105", 41001), (VADUR, 22), "01", ME)],
                               procs=procs))
        assert f["verified"] is False and set(f["why"]) == {"relay"}
    # autossh is ssh's parent, not a child of it; a TCP -L (autossh -M) is fine
    procs = {300: (["autossh", "-M", "20000", "-N", "-R", "2222:localhost:22", "vadur"], 1),
             301: (["/usr/bin/ssh", "-L", "20000:127.0.0.1:20000", "-N", "-R", "2222:localhost:22", "vadur"], 300)}
    found = _found(make_proc(tmp_path / "a", tcp=[TUNNEL], procs=procs))
    assert {f["program"] for f in found} == {"autossh", "ssh"} and all(f["verified"] for f in found)


def test_forward_shapes_as_ssh_itself_reads_them():
    """Against ssh -G: where each forward listens and where it leads."""
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
            if k not in ("remoteforward", "dynamicforward"):
                continue
            listen, _, connect = v.partition(" ")
            m = re.fullmatch(r"\[(.*)\]:(\d+)", listen)
            if listen.startswith("/"):
                far, port = "a socket", "socket"
            elif m:
                b = m.group(1)
                far = "loopback" if b.lower() == "localhost" or b == "::1" or b.startswith("127.") else \
                    "every address" if b in ("", "*", "0.0.0.0", "::") else "an address"
                port = m.group(2)
            else:
                far, port = "loopback", listen
            if k == "dynamicforward":
                to = "socks"
            elif connect == "[socks]:0":
                to = "socks"
            elif connect.startswith("/"):
                to = "a socket"
            else:
                h = re.fullmatch(r"\[(.*)\]:\d+", connect).group(1)
                to = "this host" if h.lower() == "localhost" or h == "::1" or re.fullmatch(r"127\.[\d.]+", h) \
                    else "another host"
            out.append(("R" if k == "remoteforward" else "D", port, far, to))
        return sorted(out)

    forms = [["-R", "2222:localhost:22"], ["-R", ":2222:localhost:22"], ["-R", "*:2222:localhost:22"],
             ["-R", "0.0.0.0:2222:l:22"], ["-R", "localhost:2222:l:22"], ["-R", "127.0.0.1:2222:127.0.0.1:22"],
             ["-R", "[::1]:2222:[::1]:22"], ["-R", "[::]:2222:localhost:22"], ["-R", "2222"], ["-R", "[::]:2222"],
             ["-R", "10.0.0.1:2222"], ["-R", "2222:/tmp/s"], ["-R", "/tmp/a:/tmp/b"], ["-R", "/tmp/a:l:22"],
             ["-R", "10.0.0.1:2222:/tmp/s"], ["-R", "[/tmp/x]:l:22"], ["-R", "2222:"], ["-R", "2222:local\\host:22"],
             ["-R", "2222:node05:22"], ["-R", "1:2:3:4:5"], ["-D", "1080"], ["-D", "*:1080"], ["-D", "/tmp/s"],
             ["-o", "RemoteForward=2222 localhost:22"], ["-o", "RemoteForward 2223"], ["-oRemoteForward=[::1]:2224"],
             ["-o", "RemoteForward=2225 /tmp/s"], ["-o", "RemoteForward=2226:localhost:22"],
             ["-o", "DynamicForward=*:1081"], ["-NR", "2227:LOCALHOST:22"], ["-R", " 2228:localhost:22"]]
    checked = 0
    for f in forms:
        for args in ([*f, "h"], ["h", *f]):
            want = ssh_says(args)
            got = sorted((x["kind"], x["port"], x["far"], x["to"]) for x in probe._forwards(["ssh", *args])
                         if x["kind"] in "RD")
            if want is None:
                assert all(t[2] == "unknown" for t in got), (args, got)      # ssh would refuse to run
                continue
            assert got == want, (args, got, want)
            checked += 1
    assert checked > 40


# ── the collector ───────────────────────────────────────────────────────────

def fake_resolve(name):
    return {"vadur": [VADUR], "login02": ["10.1.1.253", "fd00::253"], "spydur": ["127.0.1.1", "10.1.1.254"]
            }.get(name, [])


def test_trusted_entries():
    out, notes = pw.trusted_entries(
        ["vadur", "vadur:2222", "10.1.2.0/24", "[fd00::1]:22", "fd00::/8", "bad name!", "nowhere", "127.0.0.1",
         "0.0.0.0/0", "vadur:0", "vadur:70000"],
        here="spydur", login_hosts=["login02"], resolve=fake_resolve)
    assert out == [("vadur", VADUR + "/32", 0), ("vadur", VADUR + "/32", 2222), ("10.1.2.0/24", "10.1.2.0/24", 0),
                   ("fd00::1", "fd00::1/128", 22), ("fd00::/8", "fd00::/8", 0),
                   ("login02", "10.1.1.253/32", 0), ("login02", "fd00::253/128", 0),
                   ("spydur", "10.1.1.254/32", 0)]                     # its 127.0.1.1 isn't a server
    assert any("nowhere: not found" in n for n in notes) and any("fd00::/8: a very wide network (/8)" in n
                                                                 for n in notes)
    assert sum("not a name, an address or a network" in n for n in notes) == 3
    assert any("127.0.0.1: not used" in n for n in notes) and any("0.0.0.0/0: not used" in n for n in notes)
    assert len(notes) == 7
    _out, notes = pw.trusted_entries(["vadur", "vadur-alias", VADUR], resolve=lambda n: [VADUR])
    assert notes == ["trusted host vadur-alias: already listed", f"trusted host {VADUR}: already listed"]
    assert pw.trusted_entries("vadur", resolve=fake_resolve)[0] == [("vadur", VADUR + "/32", 0)]    # one string


def test_the_probe_is_given_the_list_and_here_s_addresses_come_after(monkeypatch):
    monkeypatch.setattr(pw, "_resolve", fake_resolve)
    monkeypatch.setattr(pw.os, "uname", lambda: os.uname_result(("Linux", "spydur", "", "", "")))
    c = pw.PolicyWatchCollector({"trusted_hosts": ["vadur"], "campus_networks": ["141.166.0.0/16", "x"]}, ":memory:")
    args = c._args()
    assert args[args.index("--trusted") + 1] == f"{VADUR}/32@0,10.1.1.254/32@0"
    assert args[args.index("--campus") + 1] == "141.166.0.0/16" and any("'x'" in n for n in c.trust_notes)
    first = c.servers()
    c.here_addresses = ["172.28.8.105", "127.0.0.1", "10.1.1.254"]
    assert c.servers()[:len(first)] == first and c.servers() == ["vadur", "spydur", "spydur"]
    assert "--inside" not in args


def verified_answer(**over):
    f = {"uid": 1500, "user": "amy", "kind": "ssh_reverse", "program": "ssh", "detail": "2222", "aka": ["ssh"],
         "processes": 1, "far": "loopback", "to": "this host", "proxied": False, "reach": "inside", "why": {},
         "servers": [0], "verified": True}
    f.update(over)
    return {"host": "spydur", "uid": 990, "hidepid": False, "processes": 300, "others": 290, "others_readable": 200,
            "findings": [f], "listeners": []}


def test_findings_of_a_verified_forward():
    [f] = pw.findings_of(verified_answer(), "spydur", "login", servers=["vadur"])
    assert (f["severity"], f["verified"], f["reach"]) == ("info", True, "inside")
    assert f["note"] == "verified: to vadur; far end loopback"
    [f] = pw.findings_of(verified_answer(servers=[]), "spdr12", "node", servers=["vadur"])
    assert f["note"] == "verified: to the cluster; far end loopback"


@pytest.mark.parametrize("over, note", [
    ({"verified": False, "why": {"outside": 2, "none": 1}, "reach": "outside"},
     "not verified: 2 connections outside; no connection to a listed server or into the cluster; far end loopback"),
    ({"verified": False, "why": {"far": 1}, "far": "every address"},
     "not verified: asks for every address on the far end"),
    ({"verified": False, "why": {"child": 1, "relay": 1}},
     "not verified: an ssh with a child process (a proxy or jump); an ssh that relays (-W, -J, a proxy); "
     "far end loopback"),
    ({"why": {"outside": 1}}, "not verified: 1 connection outside; far end loopback"),     # says verified, isn't
    ({"far": "every address"}, "not verified: not checked; far end every address"),
    ({"proxied": True}, "not verified: not checked; far end loopback"),
    ({"why": {"made up": 1}}, "not verified: not checked; far end loopback"),
    ({"detail": "+3 more", "program": "+3 more"}, None),
])
def test_findings_of_an_unverified_forward(over, note):
    [f] = pw.findings_of(verified_answer(**over), "spydur", "login", servers=["vadur"])
    assert f["severity"] == "warning" and f["verified"] is False
    if note:
        assert f["note"] == note


def test_a_socks_proxy_is_never_verified():
    [f] = pw.findings_of(verified_answer(kind="ssh_socks", to="socks"), "spydur", "login", servers=["vadur"])
    assert f["severity"] == "warning" and f["note"] == "a SOCKS proxy here, on loopback"


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
    monkeypatch.setattr(pw, "_resolve", fake_resolve)
    c = pw.PolicyWatchCollector({"enabled": True, "nodes": [], "trusted_hosts": ["vadur"]}, db_path)
    state = {"a": verified_answer()}
    monkeypatch.setattr(c, "probe_here", lambda: (state["a"], ""))
    sent = []
    import nomad.alerts as alerts
    monkeypatch.setattr(alerts, "send_alert", dispatcher_like(sent))
    return c, sent, db_path, state


def _sweep(c, db):
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE policy_watch_sweeps SET timestamp = '2000-01-01T00:00:00'")
    c.store(c.collect())


def test_a_verified_forward_is_listed_not_alerted_and_one_finding_as_it_changes(watch):
    from nomad.watch.cli import watch_group
    c, sent, db, state = watch
    c.store(c.collect())
    assert sent == []
    r = CliRunner().invoke(watch_group, ["list", "--db", db])
    assert "ssh port 2222, verified" in r.output and "verified: to vadur; far end loopback" in r.output
    state["a"] = verified_answer(verified=False, why={"outside": 1}, reach="outside")
    _sweep(c, db)
    [(args, kw)] = sent
    assert args[0] == "warning" and kw["subject"].startswith("ssh_reverse:f")
    assert "connects outside (not verified: 1 connection outside; far end loopback)" in args[2]
    assert "amy" not in args[2]
    state["a"] = verified_answer()
    sent.clear()
    _sweep(c, db)
    assert sent == []
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT sweeps, severity, verified, reach, note FROM policy_findings").fetchall()
    assert rows == [(3, "info", 1, "inside", "verified: to vadur; far end loopback")]


def test_an_old_database_gains_the_columns(tmp_path):
    from nomad.watch.cli import watch_group
    db = str(tmp_path / "old.db")
    with sqlite3.connect(db) as conn:
        conn.executescript(pw.SCHEMA.replace("    verified INTEGER,\n    note TEXT,\n", ""))
        conn.execute("INSERT INTO policy_findings (host, kind, program, detail, reach, first_seen, last_seen, "
                     "severity) VALUES ('spydur', 'tunnel', 'ngrok', '', '', '2099-01-01', '2099-01-01', 'warning')")
    r = CliRunner().invoke(watch_group, ["list", "--db", db])                  # a hub before its sites sync
    assert r.exit_code == 0 and "ngrok" in r.output, r.output
    from nomad.db.migrations import MIGRATIONS
    fn = next(f for v, _d, f in MIGRATIONS if v == 27)
    with sqlite3.connect(db) as conn:
        fn(conn)
        fn(conn)                                                               # twice: nothing to do
        assert {"verified", "note"} <= {r[1] for r in conn.execute("PRAGMA table_info(policy_findings)")}


def test_watch_probe_without_the_collectors_info_line(caplog):
    from nomad.watch.cli import watch_group
    was = logging.getLogger("nomad.collectors.base").level
    with caplog.at_level(logging.INFO):
        r = CliRunner().invoke(watch_group, ["probe"], obj={"config": {}})
    assert r.exit_code == 0, r.output
    assert "Initialized" not in caplog.text and "Initialized" not in r.output
    assert logging.getLogger("nomad.collectors.base").level == was               # as it was


def test_the_probe_says_where_it_is_and_is_still_python_36(tmp_path):
    import pwd
    try:
        nobody = pwd.getpwuid(65534).pw_name
    except KeyError:
        pytest.skip("no uid 65534 here")
    tunnel = {300: (["ssh", "-fN", "-R", "2222:localhost:22", "vadur"], 1)}
    src = open(probe.__file__).read()
    for later in ("subnet_of", "supernet_of", "removeprefix", "capture_output", "text=True"):
        assert later not in src, later

    def run(p, *extra):
        r = subprocess.run(["python3", "-I", "-S", "-", "--min-uid", "0", "--proc", str(p), "--trusted",
                            f"{VADUR}/32@0", *extra], input=src, capture_output=True, text=True, timeout=60)
        d = json.loads(r.stdout)
        return d, [f for f in d["findings"] if f["kind"] == "ssh_reverse"]

    # sshd a service's (ignored, so a system account's): verified
    d, [f] = run(make_proc(tmp_path, tcp=[TUNNEL, USED], procs=tunnel, sshd=65534), "--ignore-users", nobody)
    assert d["addresses"] == ["10.1.1.254", "172.28.8.105"] and d["cluster"] == ["10.1.1.0/24"]
    assert f["verified"] is True and f["servers"] == [0] and f["why"] == {}
    # a person's port 22 here: not
    _d, [f] = run(make_proc(tmp_path / "b", tcp=[TUNNEL, USED], procs=tunnel, sshd=1600))
    assert f["verified"] is False and f["why"] == {"host_port": 1}


def test_set_up_in_nomad_toml(tmp_path, monkeypatch):
    """trusted_hosts and campus_networks are the site's own, in nomad.toml;
    `nomad config check` says what it doesn't know and what they resolve to."""
    tomllib = pytest.importorskip("tomllib")                # 3.11 on; nomad's own reader is tried below
    from nomad.cli import cli
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    example = open(os.path.join(root, "nomad.toml.example")).read()
    block = example[example.index("# [collectors.policy_watch]"):].split("\n\n")[0]
    conf = tomllib.loads("\n".join(ln[2:] for ln in block.splitlines() if ln.startswith("# ") and "=" in ln
                                   or ln.startswith("# [")))
    pwc = conf["collectors"]["policy_watch"]
    assert pwc["trusted_hosts"] == ["vadur"] and pwc["campus_networks"] == ["141.166.0.0/16", "192.67.49.0/24"]
    c = pw.PolicyWatchCollector(pwc, ":memory:")
    assert c.trusted_hosts == ["vadur"] and c.campus == ["141.166.0.0/16", "192.67.49.0/24"]
    monkeypatch.setattr(pw, "_resolve", fake_resolve)
    problems, info = pw.config_problems({**pwc, "trusted_hosts": ["vadur", "vadur:2222", "nowhere"],
                                         "inside_hosts": ["x"], "campus_networks": ["141.166.0.0/16", "nope"]})
    assert info[0] == f"trusted: vadur = {VADUR} (ports below 1024, port 2222)"
    assert info[1].startswith("campus: 141.166.0.0/16 (and")
    assert any("inside_hosts: not a setting (1.19.1: trusted_hosts" in p for p in problems)
    assert any("nowhere: not found" in p for p in problems) and any("'nope': not a network" in p for p in problems)
    assert pw.config_problems({"trusted_hosts": "vadur"}, resolve=fake_resolve) == \
        ([], [f"trusted: vadur = {VADUR} (ports below 1024)"])                       # one string, as the collector
    assert pw.config_problems({"trusted_hosts": 3, "cluster_networks": [1]})[0] == \
        ['[collectors.policy_watch] trusted_hosts: a list of strings, like ["vadur"]',
         '[collectors.policy_watch] cluster_networks: a list of strings, like ["10.1.1.0/24"]']
    toml = tmp_path / "nomad.toml"
    toml.write_text('[collectors.policy_watch]\nenabled = true\ntrusted_hosts = ["vadur"]\ninside_hosts = []\n')
    r = CliRunner().invoke(cli, ["-c", str(toml), "config", "check"])
    assert f"policy_watch: trusted: vadur = {VADUR} (ports below 1024)" in r.output, r.output
    assert "inside_hosts: not a setting" in r.output


# ── after review ────────────────────────────────────────────────────────────

def test_ssh_options_that_take_a_value_are_ssh_s_own():
    """-B takes an interface: without it, what follows is read wrong."""
    assert probe._forwards(["ssh", "-B", "eth0", "h", "-R", "2222:localhost:22"]) == \
        [{"kind": "R", "port": "2222", "far": "loopback", "to": "this host", "proxied": False}]
    r = subprocess.run(["ssh", "-G", "-F", "/dev/null", "-B", "eth0", "h", "-R", "2222:localhost:22"],
                       capture_output=True, text=True, timeout=10)
    if r.returncode == 0:
        assert "remoteforward 2222 [localhost]:22" in r.stdout


@pytest.mark.parametrize("spec, port", [("022:localhost:22", "22"), ("+2222:localhost:22", "2222"),
                                        ("ssh:localhost:22", ""), ("99999:localhost:22", "")])
def test_ports_as_ssh_reads_them(spec, port):
    assert probe._shape("R", spec)["port"] == port


def test_every_way_out_is_left_out_and_the_site_can_add(tmp_path):
    p = make_proc(tmp_path)
    route = (p / "net" / "route").read_text() + \
        f"en1\t00000000\t{hex4('10.1.1.254')}\t0003\t0\t0\t200\t00000000\t0\t0\t0\n"   # a spare way out
    (p / "net" / "route").write_text(route)
    assert probe.cluster_networks(str(p)) == []                          # not the cluster's, unless said
    node = make_proc(tmp_path / "node")                                  # out through the head node, and
    (node / "net" / "route").write_text(                                 # a spare default on the campus NIC
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        f"en1\t00000000\t{hex4('10.1.1.254')}\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
        f"en1\t{hex4('10.1.1.0')}\t00000000\t0001\t0\t0\t0\t{hex4('255.255.255.0')}\t0\t0\t0\n"
        f"en0\t00000000\t{hex4('172.28.8.1')}\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
        f"en0\t{hex4('172.28.8.0')}\t00000000\t0001\t0\t0\t0\t{hex4('255.255.252.0')}\t0\t0\t0\n")
    assert probe.cluster_networks(str(node)) == []                       # never the campus /22
    assert [str(n) for n in probe.cluster_networks(str(node), [ipaddress.ip_network("10.1.1.0/24")])] == \
        ["10.1.1.0/24"]
    (node / "net" / "ipv6_route").write_text(                            # default dev wg0, no gateway
        "00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
        "00000000000000000000000000000000 00000400 00000001 00000000 00000001 wg0\n"
        "fd000000000000000000000000000000 40 00000000000000000000000000000000 00 "
        "00000000000000000000000000000000 00000100 00000001 00000000 00000001 wg0\n")
    assert [str(n) for n in probe.cluster_networks(str(node))] == []


def test_a_forward_s_processes_have_a_key_that_holds_while_they_run(tmp_path):
    procs = {300: (["ssh", "-fN", "-R", "2222:localhost:22", "vadur"], 1)}
    [a] = _found(make_proc(tmp_path / "a", tcp=[TUNNEL], procs=procs))
    [b] = _found(make_proc(tmp_path / "b", tcp=[TUNNEL], procs=procs))
    [c] = _found(make_proc(tmp_path / "c", tcp=[TUNNEL], procs={301: procs[300]}))     # a new ssh
    assert re.fullmatch(r"[0-9a-f]{16}", a["procs"]) and a["procs"] == b["procs"] != c["procs"]


def test_a_verified_forward_stays_so_while_the_same_ssh_runs(watch):
    c, sent, db, state = watch
    state["a"] = verified_answer(procs="a" * 16)
    c.store(c.collect())
    # its account busy now (a Jupyter through ssh -L): the same ssh, still verified, no alert
    state["a"] = verified_answer(procs="a" * 16, verified=False, why={"host_port": 1})
    _sweep(c, db)
    assert sent == []
    with sqlite3.connect(db) as conn:
        note, verified, sev = conn.execute("SELECT note, verified, severity FROM policy_findings").fetchone()
    assert (verified, sev) == (1, "info")
    assert note == ("verified: to vadur; far end loopback (the same ssh since; now: 1 connection to a person's "
                    "port on this host; far end loopback)")
    _sweep(c, db)                                                           # and again: the note doesn't grow
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT note FROM policy_findings").fetchone()[0] == note
    # a new ssh on the same port, while busy: checked afresh, and alerted
    state["a"] = verified_answer(procs="b" * 16, verified=False, why={"host_port": 1})
    _sweep(c, db)
    assert len(sent) == 1 and sent[0][0][0] == "warning"
    # back to the first key after a new one: no (it wasn't kept)
    sent.clear()
    state["a"] = verified_answer(procs="a" * 16, verified=False, why={"host_port": 1})
    _sweep(c, db)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT verified FROM policy_findings").fetchone()[0] == 0


def test_no_key_no_keeping(watch):
    c, sent, db, state = watch
    c.store(c.collect())                                                   # verified, without a key
    state["a"] = verified_answer(verified=False, why={"outside": 1})
    _sweep(c, db)
    assert len(sent) == 1


def test_one_alert_when_a_verified_account_s_first_warning_comes(watch):
    c, sent, db, state = watch
    c.store(c.collect())
    a = verified_answer()
    a["findings"].append(dict(a["findings"][0], detail="3333", verified=False, why={"outside": 1}))
    state["a"] = a
    _sweep(c, db)
    assert [k["subject"] for _a, k in sent] == ["ssh_reverse:f1"]


def test_the_child_is_named(tmp_path):
    procs = {300: (["ssh", "-N", "-R", "2222:localhost:22", "vadur"], 1),
             301: (["/usr/bin/sss_ssh_knownhostsproxy", "-p", "22", "vadur"], 300)}
    [f] = _found(make_proc(tmp_path, tcp=[TUNNEL], procs=procs))
    assert f["children"] == ["sss_ssh_knownho"] and "child" in f["why"]          # comm: 15 characters
    [g] = pw.findings_of(verified_answer(**{k: f[k] for k in ("verified", "why", "children")}), "spydur", "login",
                         servers=["vadur"])
    assert "an ssh with a child process (sss_ssh_knownho: a proxy or jump)" in g["note"]


def test_mask_covers_notes_and_the_probe_s_addresses(watch, monkeypatch):
    from nomad.watch.cli import watch_group
    c, sent, db, state = watch
    c.store(c.collect())
    r = CliRunner().invoke(watch_group, ["list", "--db", db, "--mask"])
    assert "vadur" not in r.output and "verified: to host" in r.output, r.output
    r = CliRunner().invoke(watch_group, ["probe", "--json", "--mask"], obj={"config": {}})
    d = json.loads(r.output)
    assert d["addresses"] == [] and d["cluster"] == []


def test_settings_given_as_one_string(monkeypatch):
    monkeypatch.setattr(pw, "_resolve", fake_resolve)
    c = pw.PolicyWatchCollector({"trusted_hosts": "vadur", "campus_networks": "141.166.0.0/16",
                                 "cluster_networks": ["10.1.1.0/24", "nope"]}, ":memory:")
    assert c.trusted_hosts == ["vadur"] and c.campus == ["141.166.0.0/16"] and c.cluster == ["10.1.1.0/24"]
    assert any("'nope': not a network" in n for n in c.trust_notes)
    args = c._args()
    assert args[args.index("--cluster") + 1] == "10.1.1.0/24"


def test_an_ssh_without_a_connection_of_its_own(tmp_path):
    """An idle ssh to vadur is the only connection; the -R between connection attempts has none."""
    procs = {300: (["ssh", "-o", "ConnectionAttempts=1000", "-N", "-R", "2222:localhost:22", "evil"], 1),
             301: (["ssh", "vadur"], 1)}
    [f] = _found(make_proc(tmp_path, tcp=[TUNNEL], procs=procs))
    assert f["verified"] is False and set(f["why"]) == {"carrier"}


def test_only_ssh_by_its_kernel_name_gets_a_key(tmp_path):
    p = make_proc(tmp_path, tcp=[TUNNEL], procs={300: (["ssh", "-N", "-R", "2222:localhost:22", "vadur"], 1)})
    (p / "300" / "comm").write_text("python3\n")                        # exec -a ssh python3 relay.py ...
    [f] = _found(p)
    assert "procs" not in f
    if ME == 0:                                                          # root reads the program file
        q = make_proc(tmp_path / "r", tcp=[TUNNEL], procs={300: (["ssh", "-N", "-R", "2222:localhost:22", "v"], 1)})
        (q / "300" / "exe").unlink()
        os.symlink("/home/amy/bin/ssh", q / "300" / "exe")              # comm "ssh", but not the system's
        [f] = _found(q)
        assert "procs" not in f
    procs = {300: (["autossh", "-M", "0", "-N", "-R", "2222:localhost:22", "vadur"], 1),
             301: (["ssh", "-N", "-R", "2222:localhost:22", "vadur"], 300)}
    found = {f["program"]: f for f in _found(make_proc(tmp_path / "a", tcp=[TUNNEL], procs=procs))}
    assert "procs" not in found["autossh"] and "procs" in found["ssh"]


def test_a_new_forward_after_the_first_was_verified_is_told(watch):
    """Alerted, then verified, then a new forward that connects outside: the group's alert stands, so the
    new one is said (':new')."""
    c, sent, db, state = watch
    state["a"] = verified_answer(verified=False, why={"outside": 1})
    c.store(c.collect())
    assert [k["subject"] for _a, k in sent] == ["ssh_reverse:f1"]
    state["a"] = verified_answer()
    _sweep(c, db)
    a = verified_answer()
    a["findings"].append(dict(a["findings"][0], detail="3333", verified=False, why={"outside": 1}))
    state["a"] = a
    sent.clear()
    _sweep(c, db)
    subjects = [k["subject"] for _a, k in sent]
    assert subjects == ["ssh_reverse:f1", "ssh_reverse:f1:new"] and "port 3333" in sent[1][0][2]


def test_kept_only_while_the_site_names_its_server(watch):
    c, sent, db, state = watch
    state["a"] = verified_answer(procs="a" * 16)
    c.store(c.collect())
    c.trusted_hosts = ["other"]                                           # vadur taken off the list
    state["a"] = verified_answer(procs="a" * 16, verified=False, why={"campus": 1, "none": 1})
    _sweep(c, db)
    assert len(sent) == 1


def test_kept_while_its_account_reaches_elsewhere_at_most_once_in_four_sweeps(watch):
    c, sent, db, state = watch
    state["a"] = verified_answer(procs="a" * 16)
    c.store(c.collect())
    sweeps = [{"outside": 1}, {"host_port": 1}, {"host_port": 1}, {"host_port": 1}, {"udp": 1}]
    for why in sweeps:                                                    # a git pull, a Jupyter, a ping
        state["a"] = verified_answer(procs="a" * 16, verified=False, why=why)
        _sweep(c, db)
    assert sent == []
    state["a"] = verified_answer(procs="a" * 16, verified=False, why={"server_port": 1, "none": 1})
    _sweep(c, db)
    assert len(sent) == 1                                                 # two of the last four: not kept


def test_every_other_sweep_elsewhere_is_not_kept(watch):
    c, sent, db, state = watch
    state["a"] = verified_answer(procs="a" * 16)
    c.store(c.collect())
    for why in ({"outside": 1}, {"host_port": 1}, {"outside": 1}):
        state["a"] = verified_answer(procs="a" * 16, verified=False, why=why)
        _sweep(c, db)
    assert len(sent) == 1


def test_a_kept_note_stays_whole():
    f = {"kind": "ssh_reverse", "verified": False, "procs": "a" * 16, "why": {"host_port": 1},
         "note": "not verified: " + "x" * 600}
    pw.keep(f, (1, 1, "a" * 16, "verified: to vadur; far end loopback", "info", 0))
    assert f["verified"] and len(f["note"]) <= 400 and f["note"].endswith("...)")
    assert pw.KEPT_THROUGH >= {"host_port", "outside"} and not pw.KEPT_THROUGH & {"far", "to", "proxied"}


def test_an_autossh_row_is_verified_as_its_ssh(watch):
    c, sent, db, state = watch
    a = verified_answer(procs="a" * 16)
    a["findings"].insert(0, dict(a["findings"][0], program="autossh", procs=None, verified=False,
                                 why={"none": 1}))
    state["a"] = a
    c.store(c.collect())
    assert sent == []
    with sqlite3.connect(db) as conn:
        rows = dict(conn.execute("SELECT program, note FROM policy_findings").fetchall())
    assert rows["autossh"] == "as its ssh: verified: to vadur; far end loopback"


def test_names_with_a_newline_are_not_names():
    assert probe.safe("kid\n") == "?"
    [g] = pw.findings_of(verified_answer(verified=False, why={"child": 1}, children=["kid\n"]), "spydur", "login")
    assert "kid\n" not in g["note"] and "(?: a proxy or jump)" in g["note"]


def test_networks_refuse_everything_and_say_wide():
    nets, notes = pw.networks(["0.0.0.0/0", "10.0.0.0/8", "10.1.1.0/24"], "cluster network")
    assert nets == ["10.0.0.0/8", "10.1.1.0/24"]
    assert notes == ["cluster network 0.0.0.0/0: everything, not used",
                     "cluster network 10.0.0.0/8: a very wide network (/8)"]
    out, notes = pw.trusted_entries(["localhost"], resolve=lambda n: ["127.0.0.1", "::1"])
    assert out == [] and notes == ["trusted host localhost: not used (loopback, or everything)"]


def test_a_v6_default_leaves_the_v4_cluster_network(tmp_path):
    p = make_proc(tmp_path)
    (p / "net" / "ipv6_route").write_text(                               # a router on the cluster's VLAN
        "00000000000000000000000000000000 00 00000000000000000000000000000000 00 "
        "fe800000000000000000000000000001 00000400 00000001 00000000 00000003 en1\n")
    assert [str(n) for n in probe.cluster_networks(str(p))] == ["10.1.1.0/24"]


def test_reasons_kept_are_the_collectors_own():
    [f] = pw.findings_of(verified_answer(verified=False, why="garbage"), "spydur", "login", servers=["vadur"])
    assert f["why"] == {"unchecked": 1}
    g = dict(f, procs="a" * 16)
    pw.keep(g, (1, 1, "a" * 16, "verified: to vadur; far end loopback", "info", 0), {"vadur"})
    assert g["verified"] is False                                         # "not checked" isn't ridden through

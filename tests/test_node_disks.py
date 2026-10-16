"""Compute nodes' local disks, read from the head node (nomad 1.14)."""
import sqlite3
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from nomad.alerts import thresholds as th
from nomad.collectors import node_disks as ND
from nomad.db.migrations import ensure_database

TB = 10 ** 12


def answer(*lines, banner="Welcome to the cluster\n"):
    return banner + ND.OUTPUT_MARK + "\n" + "".join(line + "\n" for line in lines)


def df(path, used, avail, mount, device="/dev/dm-1"):
    return f"{path}\t{device} {used + avail} {used} {avail} 2% {mount}"


def test_a_reading_says_which_filesystem_holds_the_path():
    out = answer(df("/localscratch", 28 * 10 ** 9, 1839 * 10 ** 9, "/"),
                 "/scratch2\t",                                        # not on this node
                 df("/data", TB, TB, "/mnt/local data", device="/dev/sdb1"))
    r = ND.parse("cn01", out, "2026-10-16T14:00:00")
    assert [x["path"] for x in r] == ["/localscratch", "/data"]
    a, b = r
    assert (a["node"], a["mount"], a["device"]) == ("cn01", "/", "/dev/dm-1")
    assert a["used_percent"] == pytest.approx(1.5, abs=0.05)
    assert b["mount"] == "/mnt/local data" and b["used_percent"] == 50.0
    # A banner that looks like a reading is never read.
    fake = df("/localscratch", 1, 0, "/")
    assert ND.parse("cn01", answer(df("/x", 1, 1, "/x"), banner=fake + "\n"), "t")[0]["path"] == "/x"


def test_the_remote_command_is_sh_and_quotes_the_paths():
    cmd = ND.remote_command(["/localscratch", "/odd path"])
    assert cmd.startswith("sh -c ")
    run = subprocess.run(["bash", "-c", ND.remote_command(["/", "/nonexistent-path"])],
                         capture_output=True, text=True)
    got = ND.parse("here", run.stdout, "t")
    assert [x["path"] for x in got] == ["/"] and got[0]["total_bytes"] > 0


def test_nodes_that_wont_answer_are_left_out():
    out = "\n".join(["cn01 cn01 idle", "cn01 cn01 idle", "cn02 cn02-ib mix", "cn03 cn03 drain",
                     "cn04 cn04 down*", "cn05 cn05 idle*", "cn06 cn06 idle~", "cn07 cn07 pow_dn",
                     "cn08 cn08 alloc", "bad;name x idle", "cn09 cn09 futr", "cn10 cn10 fail",
                     "cn11 cn11 unk", "cn12 cn12 drng", "cn13 cn13 boot", "cn14 bad;host mix"])
    assert ND.nodes_from_sinfo(out) == [
        ("cn01", "cn01"), ("cn02", "cn02-ib"), ("cn03", "cn03"), ("cn08", "cn08"),
        ("cn10", "cn10"), ("cn11", "cn11"), ("cn12", "cn12"), ("cn14", "cn14")]


def test_paths_must_be_absolute():
    assert ND.paths_from({}) == ["/localscratch"]
    assert ND.paths_from({"paths": ["/a", "rel", "/b\nc", 3]}) == ["/a"]
    assert ND.paths_from({"paths": "/x"}) == ["/x"]


@pytest.fixture
def site(tmp_path, monkeypatch):
    db = tmp_path / "site.db"
    ensure_database(db)
    calls = []

    answers = {"cn02": SimpleNamespace(returncode=255, stdout="",
                                       stderr="cn02: Permission denied (publickey).")}

    st = SimpleNamespace(sinfo="cn01 cn01 idle\ncn02 cn02 mix\ncn03 cn03 down*\n")

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[0].endswith("sinfo"):
            return SimpleNamespace(returncode=0, stdout=st.sinfo, stderr="")
        host = argv[argv.index("--") + 1]
        got = answers.get(host)
        if callable(got):
            return got(argv, **kw)
        return got or SimpleNamespace(returncode=0, stderr="", stdout=answer(
            df("/localscratch", 900 * 10 ** 9, 100 * 10 ** 9, "/")))
    monkeypatch.setattr(ND.subprocess, "run", fake_run)
    monkeypatch.setattr(ND, "find_tool", lambda name: "/usr/bin/" + name)
    return SimpleNamespace(db=db, calls=calls, answers=answers, st=st)


def ssh_calls(site):
    return [c for c in site.calls if c[0].endswith("/ssh")]


def test_a_run_reads_the_nodes_and_says_which_didnt_answer(site):
    col = ND.NodeDisksCollector({"enabled": True, "alerts_enabled": False}, site.db)
    r = col.run()
    assert r.success and r.records_collected == 1
    assert "1 of 2 nodes didn't answer: cn02 (cn02: Permission denied (publickey).)" in r.note
    row = sqlite3.connect(site.db).execute(
        "SELECT node, path, mount, used_percent FROM node_disks").fetchone()
    assert row == ("cn01", "/localscratch", "/", 90.0)
    ssh = ssh_calls(site)
    assert {c[c.index("--") + 1] for c in ssh} == {"cn01", "cn02"}           # not the down one
    assert "BatchMode=yes" in ssh[0] and "-n" in ssh[0]
    assert sqlite3.connect(site.db).execute(
        "SELECT asked, answered, readings FROM node_disks_sweeps").fetchall() == [(2, 1, 1)]
    # Within interval_minutes: nothing asked again.
    before = len(site.calls)
    r = col.run()
    assert r.success and r.records_collected == 0 and "every 30 minutes" in r.note
    assert len(site.calls) == before


def test_a_sweep_where_no_node_answers_fails_once_and_waits(site):
    col = ND.NodeDisksCollector({"nodes": ["cn02"], "alerts_enabled": False, "retry_delay": 0},
                                site.db)
    r = col.run()
    assert not r.success and "didn't answer" in r.error_message
    assert len(ssh_calls(site)) == 1                                  # not retried
    # The next cron run doesn't sweep again: the interval runs from the sweep.
    r = col.run()
    assert r.success and "last asked at" in r.note and len(ssh_calls(site)) == 1


def test_a_path_on_no_node_is_not_asked_for_at_every_run(site):
    col = ND.NodeDisksCollector({"paths": ["/nowhere"], "alerts_enabled": False}, site.db)
    site.answers["cn01"] = site.answers["cn02"] = SimpleNamespace(
        returncode=0, stderr="", stdout=answer("/nowhere\t"))
    r = col.run()
    assert r.success and r.records_collected == 0 and "2 path reading(s) missing" in r.note
    r = col.run()
    assert "last asked at" in r.note and len(ssh_calls(site)) == 2


def test_one_node_answering_and_another_not_is_a_success(site):
    site.answers["cn01"] = SimpleNamespace(returncode=0, stderr="", stdout=answer("/localscratch\t"))
    r = ND.NodeDisksCollector({"alerts_enabled": False}, site.db).run()
    assert r.success and "1 of 2 nodes didn't answer" in r.note and "missing" in r.note


def test_ssh_to_the_nodes_hostname_and_store_its_name(site):
    site.st.sinfo = "gpu1 gpu1.cluster.example mix\n"
    r = ND.NodeDisksCollector({"alerts_enabled": False}, site.db).run()
    assert r.success and [c[c.index("--") + 1] for c in ssh_calls(site)] == ["gpu1.cluster.example"]
    assert sqlite3.connect(site.db).execute("SELECT node FROM node_disks").fetchall() == [("gpu1",)]
    sinfo, = [c for c in site.calls if c[0].endswith("sinfo")]
    assert sinfo[1:] == ["-a", "-N", "-h", "-o", "%N %n %t"]


def test_a_sweep_stops_asking_after_max_seconds(site):
    import time

    def slow(argv, **kw):
        time.sleep(1.5)
        return SimpleNamespace(returncode=0, stderr="", stdout=answer(
            df("/localscratch", 1, 1, "/")))
    site.answers["cn01"] = slow
    col = ND.NodeDisksCollector({"nodes": ["cn01", "cn04", "cn05"], "parallel": 1,
                                 "max_seconds": 1, "alerts_enabled": False}, site.db)
    t0 = time.monotonic()
    r = col.run()
    # cn01 was being asked when time ran out: its answer counts; the rest
    # weren't asked.
    assert time.monotonic() - t0 < 4
    assert r.success and r.records_collected == 1
    assert "2 node(s) not asked" in r.note
    assert [c[c.index("--") + 1] for c in ssh_calls(site)] == ["cn01"]


def test_a_node_answering_in_another_encoding_is_read(tmp_path, monkeypatch):
    # A real ssh stand-in: cn02 says something in Latin-1 and fails; cn01 runs
    # the command here.
    fake = tmp_path / "ssh"
    fake.write_text("#!/bin/sh\nwhile [ \"$1\" != -- ]; do shift; done; shift\n"
                    "host=$1; shift\n"
                    "if [ \"$host\" = cn02 ]; then printf 'Bienvenue \\347\\343\\n' >&2; exit 255; fi\n"
                    "exec sh -c \"$*\"\n")
    fake.chmod(0o755)
    monkeypatch.setattr(ND, "find_tool", lambda name: str(fake) if name == "ssh" else None)
    db = tmp_path / "s.db"
    ensure_database(db)
    r = ND.NodeDisksCollector({"nodes": ["cn01", "cn02"], "paths": ["/"],
                               "alerts_enabled": False}, db).run()
    assert r.success and r.records_collected == 1, r.error_message
    assert "cn02 (Bienvenue" in r.note


def test_no_ssh_fails_at_once(site, monkeypatch):
    monkeypatch.setattr(ND, "find_tool", lambda name: None if name == "ssh" else "/usr/bin/" + name)
    r = ND.NodeDisksCollector({"alerts_enabled": False}, site.db).run()
    assert not r.success and "ssh not found" in r.error_message and not site.calls


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(th, "send_alert", lambda **kw: out.append(kw))
    monkeypatch.setattr(th, "get_dispatcher", lambda: object())
    return out


def reading(node, pct, mount="/localscratch"):
    total = 2 * TB
    used = int(total * pct / 100)
    return {"type": "node_disk", "node": node, "path": "/localscratch", "mount": mount,
            "total_bytes": total, "used_bytes": used, "available_bytes": total - used,
            "used_percent": pct}


def test_a_missed_reading_does_not_raise_a_full_disk_again(tmp_path):
    from nomad.alerts import dispatcher as D
    db = tmp_path / "a.db"
    ensure_database(db)
    disp = D.init_dispatcher({})
    disp.db_path = str(db)
    item = dict(reading("cn05", 97), every_minutes=30)

    def seen(minutes_ago):
        conn = sqlite3.connect(db)
        conn.execute("UPDATE alert_state SET last_seen = ?, worst_seen = ?",
                     ((datetime.now() - timedelta(minutes=minutes_ago)).isoformat(),) * 2)
        conn.commit()
        th.ThresholdChecker({}).check("node_disks", [item], host="head")
        return sqlite3.connect(db).execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    try:
        assert seen(0) == 1
        assert seen(30) == 1
        assert seen(65) == 1                  # one reading missed: the same condition
        assert seen(80) == 2                  # unseen for longer than 75 minutes: new
    finally:
        D._dispatcher = None


def test_alerts_are_each_nodes_own(sent):
    th.ThresholdChecker({}).check("node_disks", [reading("cn01", 50), reading("cn02", 85, "/"),
                                                 reading("cn03", 97)], host="head")
    assert [(a["host"], a["subject"], a["severity"], a["source"]) for a in sent] == [
        ("cn02", "/localscratch", "warning", "node_disk"),
        ("cn03", "/localscratch", "critical", "node_disk")]
    assert "system disk" in sent[0]["message"] and "Node cn02: /localscratch at 85.0%" in sent[0]["message"]
    assert "system disk" not in sent[1]["message"]


def test_the_collector_is_off_until_enabled():
    from nomad.collectors.plan import NAMES, plan
    assert "node_disks" in NAMES
    p = {x.name: x for x in plan({})}["node_disks"]
    assert not p.enabled
    p = {x.name: x for x in plan({"collectors": {"node_disks": {"enabled": True}}})}["node_disks"]
    assert p.enabled


def test_the_table_comes_with_the_database(tmp_path):
    db = tmp_path / "n.db"
    ensure_database(db)
    cols = {r[1] for r in sqlite3.connect(db).execute("PRAGMA table_info(node_disks)")}
    assert {"node", "path", "mount", "used_percent", "available_bytes"} <= cols

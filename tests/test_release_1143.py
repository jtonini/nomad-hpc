"""nomad 1.14.3: who has the data on a compute node's local disk.

The node disk collector (1.14) said how full each node's /localscratch was,
but not whose data filled it: finding out meant logging in to the node.
Now, after the df readings, a few nodes chosen from those readings total
each path by owner on the node itself, and the largest owners are kept and
named in the disk's alert.
"""
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from nomad.alerts import thresholds as th
from nomad.collectors import node_disks as ND
from nomad.db.migrations import ensure_database

GB = 10 ** 9
TB = 10 ** 12


def df(path, used, avail, mount="/localscratch"):
    return f"{path}\t/dev/sdb1 {used + avail} {used} {avail} 2% {mount}"


def counted(path, owners, status="0", denied=0, errors=0, unreadable=()):
    lines = [f"C\t{path}"] + [f"O\t{u}\t{b}\t{n}" for u, b, n in owners]
    lines += [f"U\t{u}\t{n}" for u, n in unreadable]
    return lines + [f"S\t{status}\t{denied}\t{errors}"]


def answer(*lines):
    return "Welcome\n" + ND.OUTPUT_MARK + "\n" + "".join(line + "\n" for line in lines)


@pytest.fixture
def names(monkeypatch):
    known = {1001: "jdoe", 1002: "asmith", 1003: "bkim", 1004: "cdiaz"}

    def getpwuid(uid):
        if uid not in known:
            raise KeyError(uid)
        return SimpleNamespace(pw_name=known[uid])
    monkeypatch.setattr(ND.pwd, "getpwuid", getpwuid)
    return known


def test_a_path_is_counted_by_owner_on_the_node(tmp_path):
    real = tmp_path / "scratch"
    (real / "run1").mkdir(parents=True)
    (real / "run1" / "out.dat").write_bytes(os.urandom(300_000))
    for i in range(3):                         # one file, four names: counted once
        os.link(real / "run1" / "out.dat", real / f"copy{i}")
    link = tmp_path / "localscratch"
    link.symlink_to(real)                      # as on a node where it is a link
    cmd = ND.owners_command([str(link), "/nonexistent-path"], 10)
    # csh as a login shell: one line, and no history character.
    assert "\n" not in cmd and "!" not in cmd
    out = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True).stdout
    got = ND.parse_owners(out)
    mine = got[str(link)]
    assert mine["status"] == "0" and mine["denied"] == 0 and mine["unreadable"] == {}
    (uid, size, files), = mine["owners"]
    assert uid == os.getuid() and files == 3          # scratch, run1, out.dat once
    du = subprocess.run(["du", "-s", "-B1", str(real) + "/"], capture_output=True, text=True)
    assert size == int(du.stdout.split()[0])          # as du counts it
    assert got["/nonexistent-path"]["status"] == "nopath"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every folder")
def test_a_folder_the_account_cant_read_is_named_not_guessed(tmp_path):
    (tmp_path / "s" / "private").mkdir(parents=True)
    (tmp_path / "s" / "private" / "x").write_bytes(b"x" * 10_000)
    (tmp_path / "s" / "private").chmod(0o300)        # can enter, can't list
    try:
        out = subprocess.run(["sh", "-c", ND.owners_command([str(tmp_path / "s")], 10)],
                             capture_output=True, text=True).stdout
    finally:
        (tmp_path / "s" / "private").chmod(0o700)
    got = ND.parse_owners(out)[str(tmp_path / "s")]
    assert got["unreadable"] == {os.getuid(): 1}


def test_what_a_count_found(names):
    summary = ND.owners_summary(
        {"owners": [(1001, 5 * GB, 10), (1003, 900 * GB, 2), (4242, 1 * GB, 1),
                    (1002, 30 * GB, 7)], "status": "0", "denied": 0}, 2, {})
    assert summary["owners"] == 4 and summary["bytes"] == 936 * GB and summary["complete"]
    assert [(o["user"], o["bytes"]) for o in summary["top"]] == [("bkim", 900 * GB),
                                                                ("asmith", 30 * GB)]
    assert ND.owners_summary({"owners": [(4242, 1, 1)], "status": "0"}, 5, {})["top"][0]["user"] \
        == "4242"                                # no name: the number
    # Someone whose folders couldn't be read is kept, by name, with no size.
    s = ND.owners_summary({"owners": [(1001, 5 * GB, 10)], "unreadable": {1004: 2},
                           "status": "0"}, 1, {})
    assert (s["complete"], s["stopped"], s["unreadable_by"], s["owners"]) == (
        False, "unreadable folders", ["cdiaz"], 2)
    assert [(o["user"], o["bytes"], o["unreadable"]) for o in s["top"]] == [
        ("jdoe", 5 * GB, 0), ("cdiaz", 0, 2)]
    for got, why in (({"status": "124"}, "time limit"), ({"status": "1", "denied": 3},
                     "unreadable folders"), ({"status": "nopath"}, "no such folder"),
                     ({"status": "noaccess"}, "no access"), ({}, "no answer"),
                     ({"status": "1"}, "count failed")):
        s = ND.owners_summary(got, 5, {})
        assert not s["complete"] and s["stopped"] == why


def test_the_time_limit_is_reported_as_such():
    out = answer("C\t/x", "O\t1001\t512\t1", "S\t124\t0\t0")
    assert ND.parse_owners(out)["/x"]["status"] == "124"
    cmd = ND.owners_command(["/x"], 1)
    # The status on a line of its own (a line cut short by the time limit
    # must not swallow it); files that vanish mid-count are not errors; find
    # stuck on a failing disk is killed.
    assert "\\n__rc %s\\n" in cmd and "-ignore_readdir_race" in cmd and "timeout -k 5 1 " in cmd


def test_paths_with_a_history_character_or_twice_are_left_out():
    assert ND.paths_from({"paths": ["/a", "/b!c", "/a", "/d"]}) == ["/a", "/d"]


@pytest.fixture
def site(tmp_path, monkeypatch, names):
    db = tmp_path / "site.db"
    ensure_database(db)
    calls = []
    st = SimpleNamespace(sinfo="cn01 cn01 idle\ncn02 cn02 mix\ncn03 cn03 alloc\n",
                         used={"cn01": 50, "cn02": 85, "cn03": 10}, missing=set(), down=set(),
                         count_fails=set())

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        if argv[0].endswith("sinfo"):
            return SimpleNamespace(returncode=0, stdout=st.sinfo, stderr="")
        host, cmd = argv[argv.index("--") + 1], argv[-1]
        if host in st.down:
            return SimpleNamespace(returncode=255, stdout="", stderr="no route to host")
        used = st.used[host] * 20 * GB
        if "-xdev" in cmd:                                    # a count by owner
            if host in st.count_fails:
                raise subprocess.TimeoutExpired(argv, kw.get("timeout"))
            lines = counted("/localscratch", [(1001, used // 2, 100), (1002, used // 4, 10),
                                              (1003, used // 8, 5), (1004, 1 * GB, 1)])
            return SimpleNamespace(returncode=0, stderr="", stdout=answer(*lines))
        lines = ["/localscratch\t" if host in st.missing else
                 df("/localscratch", used, 2 * TB - used)]
        if "/data" in cmd:
            lines.append("/data\t")                           # no node has /data
        return SimpleNamespace(returncode=0, stderr="", stdout=answer(*lines))
    monkeypatch.setattr(ND.subprocess, "run", fake_run)
    monkeypatch.setattr(ND, "find_tool", lambda name: "/usr/bin/" + name)
    return SimpleNamespace(db=db, calls=calls, st=st)


def counted_nodes(site):
    return sorted(argv[argv.index("--") + 1] for argv, _kw in site.calls
                  if argv[0].endswith("/ssh") and "-xdev" in argv[-1])


def go_back(db, minutes):
    """Make every stored reading older."""
    conn = sqlite3.connect(db)
    for t in ("node_disks", "node_disks_sweeps", "node_disk_owner_counts", "node_disk_owners"):
        for rowid, ts in conn.execute(f"SELECT rowid, timestamp FROM {t}").fetchall():
            when = datetime.fromisoformat(ts) - timedelta(minutes=minutes)
            conn.execute(f"UPDATE {t} SET timestamp = ? WHERE rowid = ?",
                         (when.isoformat(timespec="seconds"), rowid))
    conn.commit()


def test_a_sweep_counts_the_owners_and_keeps_the_largest(site):
    r = ND.NodeDisksCollector({"alerts_enabled": False, "owners_top": 3}, site.db).run()
    assert r.success and r.records_collected == 3
    assert "3 path(s) counted by owner" in r.note
    conn = sqlite3.connect(site.db)
    assert conn.execute("SELECT node, owners, bytes, complete, stopped FROM node_disk_owner_counts "
                        "WHERE node = 'cn02'").fetchone() == ("cn02", 4, 1_488_500_000_000, 1, None)
    assert conn.execute("SELECT username, bytes, files, unreadable FROM node_disk_owners "
                        "WHERE node = 'cn02' ORDER BY bytes DESC").fetchall() == [
        ("jdoe", 850 * GB, 100, 0), ("asmith", 425 * GB, 10, 0), ("bkim", 212_500_000_000, 5, 0)]
    # The counts' timestamps are the readings'.
    assert conn.execute("SELECT COUNT(*) FROM node_disk_owner_counts c JOIN node_disks d "
                        "ON d.node = c.node AND d.path = c.path AND d.timestamp = c.timestamp"
                        ).fetchone() == (3,)
    # The df readings are asked quickly; a count is given the time it needs.
    timeouts = sorted(kw["timeout"] for argv, kw in site.calls if argv[0].endswith("/ssh"))
    assert timeouts == [20, 20, 20, 95, 95, 95]


def test_owners_are_counted_again_after_six_hours_or_at_every_sweep_when_full(site):
    col = ND.NodeDisksCollector({"alerts_enabled": False}, site.db)
    col.run()
    assert counted_nodes(site) == ["cn01", "cn02", "cn03"]
    site.calls.clear()
    go_back(site.db, 31)                     # the next sweep, half an hour later
    col.run()
    assert counted_nodes(site) == ["cn02"]   # 85% full: counted at every sweep
    site.calls.clear()
    go_back(site.db, 6 * 60 - 36)            # 5h55 after the first counts
    col.run()
    assert counted_nodes(site) == ["cn01", "cn02", "cn03"]


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(th, "send_alert", lambda **kw: out.append(kw))
    monkeypatch.setattr(th, "get_dispatcher", lambda: object())
    return out


def test_a_disk_that_fills_is_counted_in_the_sweep_that_sees_it(site, sent):
    col = ND.NodeDisksCollector({"owners_per_sweep": 1}, site.db)
    site.st.used = {"cn01": 50, "cn02": 50, "cn03": 50}
    col.run()
    go_back(site.db, 31)
    site.calls.clear()
    sent.clear()
    site.st.used["cn03"] = 96                # full since the last sweep
    col.run()
    assert counted_nodes(site) == ["cn03"]
    (a,) = sent
    assert a["host"] == "cn03" and "-- largest: jdoe" in a["message"]


def test_full_disks_take_turns(site):
    site.st.used = {"cn01": 95, "cn02": 90, "cn03": 85}
    col = ND.NodeDisksCollector({"alerts_enabled": False, "owners_per_sweep": 2}, site.db)
    col.run()
    assert counted_nodes(site) == ["cn01", "cn02"]
    site.calls.clear()
    go_back(site.db, 31)
    col.run()
    assert "cn03" in counted_nodes(site)     # never counted: before those counted just now


def test_nodes_without_the_path_or_an_answer_take_no_turn(site):
    site.st.sinfo += "cn04 cn04 idle\ncn05 cn05 idle\n"
    site.st.used.update(cn04=10, cn05=10)
    site.st.missing, site.st.down = {"cn04"}, {"cn05"}
    col = ND.NodeDisksCollector({"alerts_enabled": False, "owners_per_sweep": 1}, site.db)
    seen = []
    for _ in range(4):
        site.calls.clear()
        col.run()
        seen += counted_nodes(site)
        go_back(site.db, 6 * 60)
    assert "cn04" not in seen and "cn05" not in seen
    assert {"cn01", "cn02", "cn03"} <= set(seen)


def test_a_path_a_node_doesnt_have_is_not_counted(site):
    ND.NodeDisksCollector({"alerts_enabled": False, "paths": ["/localscratch", "/data"]},
                          site.db).run()
    counts = [argv[-1] for argv, _kw in site.calls
              if argv[0].endswith("/ssh") and "-xdev" in argv[-1]]
    assert len(counts) == 3
    assert all("/localscratch" in c and "/data" not in c for c in counts)


def test_a_count_that_doesnt_answer_keeps_the_readings(site):
    site.st.count_fails = {"cn01"}
    r = ND.NodeDisksCollector({"alerts_enabled": False}, site.db).run()
    assert r.success and r.records_collected == 3 and "1 not finished" in r.note
    conn = sqlite3.connect(site.db)
    assert conn.execute("SELECT COUNT(*) FROM node_disks").fetchone() == (3,)
    assert conn.execute("SELECT complete, stopped FROM node_disk_owner_counts WHERE node = 'cn01'"
                        ).fetchone() == (0, "no answer")


def test_owners_false_counts_nothing(site):
    r = ND.NodeDisksCollector({"alerts_enabled": False, "owners": False}, site.db).run()
    assert r.success and counted_nodes(site) == []
    assert sqlite3.connect(site.db).execute(
        "SELECT COUNT(*) FROM node_disk_owner_counts").fetchone() == (0,)


def full_item(**kw):
    used = int(1.9 * TB)
    item = {"type": "node_disk", "node": "cn02", "path": "/localscratch", "mount": "/localscratch",
            "total_bytes": 2 * TB, "used_bytes": used, "available_bytes": 2 * TB - used,
            "used_percent": 95.0,
            "owners": {"owners": 4, "complete": True, "unreadable_by": [], "top": [
                {"user": "jdoe", "uid": 1001, "bytes": 1.2 * TB, "files": 9},
                {"user": "asmith", "uid": 1002, "bytes": 300 * GB, "files": 9},
                {"user": "bkim", "uid": 1003, "bytes": 20 * GB, "files": 9},
                {"user": "cdiaz", "uid": 1004, "bytes": 1 * GB, "files": 9}]}}
    item.update(kw)
    return item


def test_the_alert_names_who_has_the_data(sent):
    th.ThresholdChecker({}).check("node_disks", [full_item()], host="head")
    (a,) = sent
    assert a["message"].endswith("-- largest: jdoe 1.2 TB, asmith 300.0 GB, bkim 20.0 GB")
    # The alert carries those three, not the whole count.
    carried = a["details"]["item"]
    assert "owners" not in carried and [o["user"] for o in carried["largest"]] == [
        "jdoe", "asmith", "bkim"]
    assert "cdiaz" not in str(a["details"])
    sent.clear()
    item = full_item()
    item["owners"].update(complete=False, unreadable_by=["cdiaz"])
    th.ThresholdChecker({}).check("node_disks", [item], host="head")
    assert sent[0]["message"].endswith("(folders of cdiaz could not be read)")
    sent.clear()
    item["owners"].update(unreadable_by=[])
    th.ThresholdChecker({}).check("node_disks", [item], host="head")
    assert sent[0]["message"].endswith("(count not finished)")
    sent.clear()
    item = full_item()
    del item["owners"]                       # not counted this time: as before
    th.ThresholdChecker({}).check("node_disks", [item], host="head")
    assert "largest" not in sent[0]["message"]


def test_owners_in_alerts_false_keeps_names_out(sent):
    th.ThresholdChecker({}).check("node_disks", [full_item(owners_in_alerts=False)], host="head")
    (a,) = sent
    for name in ("jdoe", "asmith", "bkim", "cdiaz"):
        assert name not in a["message"] and name not in str(a["details"])


def test_the_tables_come_with_the_database(tmp_path):
    db = tmp_path / "n.db"
    ensure_database(db)
    conn = sqlite3.connect(db)
    counts = {r[1] for r in conn.execute("PRAGMA table_info(node_disk_owner_counts)")}
    owners = {r[1] for r in conn.execute("PRAGMA table_info(node_disk_owners)")}
    assert {"node", "path", "owners", "bytes", "complete", "stopped"} <= counts
    assert {"node", "path", "username", "uid", "bytes", "files", "unreadable"} <= owners

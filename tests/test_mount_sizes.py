# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The size of the storage behind each mount: read by the mount probe on every
lab machine (statvfs, df's numbers), stored with the mount's state, and shown
for a lab's storage -- with the free space that exports of one pool share."""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nomad.cli import _bytes_shown, cli
from nomad.collectors import mount_probe
from nomad.collectors.workstation import WorkstationCollector, _byte_count
from nomad.config.access import ExportSize, export_sizes, shared_free
from nomad.db.migrations import ensure_database
from tests.conftest import _bootstrap_db_with_migrations

TB = 10 ** 12


def _statvfs(blocks, bfree, bavail, frsize=4096):
    return SimpleNamespace(f_blocks=blocks, f_bfree=bfree, f_bavail=bavail, f_frsize=frsize)


# --- the probe ---------------------------------------------------------------

def test_sizes_are_dfs():
    # Used counts the reserved blocks as df does; avail is what users can write.
    assert mount_probe._sizes(_statvfs(1000, 300, 250)) == (4096000, 2867200, 1024000)
    assert mount_probe._sizes(_statvfs(0, 0, 0)) is None          # a size-less pseudo fs
    assert mount_probe._sizes(_statvfs(10, 5, 5, frsize=0)) is None


def test_a_mount_that_answers_has_its_sizes(tmp_path):
    ok, ms, sizes = mount_probe._check_mount_responsive(str(tmp_path), 3.0)
    assert ok and ms >= 0
    st = os.statvfs(tmp_path)
    total, used, avail = sizes
    assert total == st.f_blocks * st.f_frsize
    assert used + avail <= total


def test_statvfs_failing_keeps_the_mount_responsive(tmp_path, monkeypatch):
    def boom(_):
        raise OSError("no")
    monkeypatch.setattr(mount_probe.os, "statvfs", boom)
    assert mount_probe._check_mount_responsive(str(tmp_path), 3.0)[0::2] == (True, None)


@pytest.fixture
def hang():
    """A stand-in for a call on a dead NFS server: it blocks until the test
    ends (a real one blocks until the server answers, or for ever)."""
    release = threading.Event()

    def blocked(*_a, **_k):
        release.wait(30)
        raise OSError("server gone")
    yield blocked
    release.set()


def test_statvfs_hanging_keeps_the_mount_responsive(tmp_path, monkeypatch, hang):
    monkeypatch.setattr(mount_probe.os, "statvfs", hang)
    started = time.monotonic()
    ok, ms, sizes = mount_probe._check_mount_responsive(str(tmp_path), 0.3)
    assert ok and sizes is None and ms < 300
    assert time.monotonic() - started < 2


def test_stat_hanging_is_unresponsive_without_sizes(tmp_path, monkeypatch, hang):
    monkeypatch.setattr(mount_probe.os, "stat", hang)
    started = time.monotonic()
    assert mount_probe._check_mount_responsive(str(tmp_path), 0.3) == (False, 300.0, None)
    assert time.monotonic() - started < 2


def test_stat_failing_is_unresponsive_without_sizes(tmp_path, monkeypatch):
    def denied(_):
        raise PermissionError("no")
    monkeypatch.setattr(mount_probe.os, "stat", denied)
    ok, ms, sizes = mount_probe._check_mount_responsive(str(tmp_path), 3.0)
    assert (ok, sizes) == (False, None) and ms < 3000


def test_dead_mounts_are_checked_at_once(tmp_path, monkeypatch, hang):
    # Four mounts of a dead server cost one timeout, not four.
    real_stat = os.stat
    monkeypatch.setattr(mount_probe.os, "stat",
                        lambda p, *a, **k: hang() if p.startswith("/dead") else real_stat(p))
    monkeypatch.setattr(mount_probe, "_parse_proc_mounts", lambda: iter(
        [("nas:/d%d" % i, "/dead%d" % i, "nfs4", "rw") for i in range(4)]
        + [("nas2:/ok", str(tmp_path), "nfs4", "rw")]))
    started = time.monotonic()
    rows = list(mount_probe.probe(stat_timeout_sec=0.5))
    assert time.monotonic() - started < 1.5
    assert [r["is_responsive"] for r in rows] == [0, 0, 0, 0, 1]
    assert rows[-1]["total_bytes"]


DRIVER = """
import importlib.util, os, sys, threading
spec = importlib.util.spec_from_file_location("mp", sys.argv[1])
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)
real_stat = os.stat
def stat(p, *a, **k):
    if p == "/dead":
        threading.Event().wait()          # never answers
    return real_stat(p, *a, **k)
mp.os.stat = stat
alive = sys.argv[2]
mp._parse_proc_mounts = lambda: iter([("nas:/x", "/dead", "nfs4", "rw"),
                                      ("nas:/y", alive, "nfs4", "rw")])
sys.argv = ["mount_probe", "--timeout", "0.5"]
mp._run()
"""


def test_a_hung_mount_does_not_keep_the_script_running(tmp_path):
    # The collector waits for the script's ssh session to end: a thread still
    # stuck on a dead server must not keep the process alive.
    started = time.monotonic()
    out = subprocess.run([sys.executable, "-c", DRIVER, mount_probe.__file__, str(tmp_path)],
                         capture_output=True, text=True, timeout=20)
    assert time.monotonic() - started < 10
    assert out.returncode == 0, out.stderr
    rows = [json.loads(line) for line in out.stdout.splitlines()]
    assert [(r["mountpoint"], r["is_responsive"]) for r in rows] == [
        ("/dead", 0), (str(tmp_path), 1)]


def test_a_check_that_cannot_start_skips_only_its_mount(tmp_path, monkeypatch):
    # Out of processes: no row for that mount (unknown, not "not responding").
    starts = []

    class Thread(threading.Thread):
        def start(self):
            starts.append(1)
            if len(starts) == 1:
                raise RuntimeError("can't start new thread")
            super().start()
    monkeypatch.setattr(mount_probe.threading, "Thread", Thread)
    monkeypatch.setattr(mount_probe, "_parse_proc_mounts", lambda: iter([
        ("nas:/a", str(tmp_path), "nfs4", "rw"), ("nas:/b", str(tmp_path), "nfs4", "rw")]))
    rows = list(mount_probe.probe(stat_timeout_sec=1.0))
    assert [r["source"] for r in rows] == ["nas:/b"]
    # One mount asked for directly is checked in place.
    starts.clear()
    ok, _, sizes = mount_probe._check_mount_responsive(str(tmp_path), 1.0)
    assert ok and sizes


def test_the_probe_is_python_3_6(tmp_path):
    # It runs on each workstation's own python3, which can be 3.6 (Rocky 8).
    import ast
    source = open(mount_probe.__file__).read()
    tree = ast.parse(source, feature_version=(3, 6))
    futures = [a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
               and n.module == "__future__" for a in n.names]
    assert "annotations" not in futures        # a SyntaxError before 3.7


def test_probe_rows_carry_the_sizes(monkeypatch, tmp_path):
    monkeypatch.setattr(mount_probe, "_parse_proc_mounts", lambda: iter([
        ("nas:/export/home", str(tmp_path), "nfs4", "rw")]))
    row, = mount_probe.probe()
    assert row["probe_version"] == "2"
    assert row["total_bytes"] and row["used_bytes"] is not None and row["avail_bytes"] is not None


def test_the_probe_runs_as_a_script_under_python3(tmp_path):
    # It is sent to each workstation's own python3 over ssh.
    out = subprocess.run(["python3", "-"], input=open(mount_probe.__file__).read(),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert '"avail_bytes"' in out.stdout or out.stdout == ""


# --- storing -----------------------------------------------------------------

def _snapshot(**sizes):
    row = {"mountpoint": "/home", "fstype": "nfs4", "source": "nas:/export/home",
           "is_mounted": 1, "is_responsive": 1, "response_ms": 1.5,
           "collected_at": 1, "probe_version": "2"}
    row.update(sizes)
    return row


def _store(db, *snapshots):
    WorkstationCollector({}, str(db)).store([
        {"hostname": "adam", "status": "online", "mount_snapshots": list(snapshots)}])


def test_migrations_add_the_size_columns(tmp_path):
    db = tmp_path / "site.db"
    ensure_database(db)
    cols = {r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(workstation_mount_state)")}
    assert {"total_bytes", "used_bytes", "avail_bytes"} <= cols


def test_a_database_that_lost_the_table_gets_it_back(tmp_path):
    # Migration 15 must not stop every `nomad collect` on such a database.
    db = tmp_path / "site.db"
    ensure_database(db)
    c = sqlite3.connect(db)
    c.execute("DROP TABLE workstation_mount_state")
    c.execute("DELETE FROM schema_migrations WHERE version >= 15")
    c.commit()
    c.close()
    ensure_database(db)
    cols = {r[1] for r in sqlite3.connect(db).execute(
        "PRAGMA table_info(workstation_mount_state)")}
    assert {"source", "is_responsive", "total_bytes", "used_bytes", "avail_bytes"} <= cols


def test_sizes_are_stored(tmp_path):
    db = tmp_path / "site.db"
    ensure_database(db)
    _store(db, _snapshot(total_bytes=40 * TB, used_bytes=3 * TB, avail_bytes=37 * TB),
           _snapshot(mountpoint="/old"))                  # a v1 probe: no sizes
    rows = sqlite3.connect(db).execute(
        "SELECT mountpoint, total_bytes, used_bytes, avail_bytes FROM workstation_mount_state "
        "ORDER BY mountpoint").fetchall()
    assert rows == [("/home", 40 * TB, 3 * TB, 37 * TB), ("/old", None, None, None)]


@pytest.mark.parametrize("value", ["40", -1, True, 1.5, 2 ** 63, None])
def test_what_is_not_a_size_is_stored_as_none(value):
    assert _byte_count(value) is None


def test_a_database_without_the_columns_still_gets_the_mounts(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE workstation_mount_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL,
        hostname TEXT NOT NULL, mountpoint TEXT NOT NULL, fstype TEXT, source TEXT,
        is_mounted INTEGER NOT NULL, is_responsive INTEGER NOT NULL, response_ms REAL,
        collected_at INTEGER, probe_version TEXT, collector_version TEXT)""")
    c.commit()
    c.close()
    _store(db, _snapshot(total_bytes=40 * TB, used_bytes=3 * TB, avail_bytes=37 * TB))
    assert sqlite3.connect(db).execute(
        "SELECT hostname, source FROM workstation_mount_state").fetchall() == [
        ("adam", "nas:/export/home")]


# --- reading them back -------------------------------------------------------

def _sized_db(path, rows):
    _bootstrap_db_with_migrations(str(path))
    c = sqlite3.connect(path)
    c.executemany(
        "INSERT INTO workstation_mount_state (timestamp, hostname, mountpoint, source, "
        "is_mounted, is_responsive, total_bytes, used_bytes, avail_bytes) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)", rows)
    c.commit()
    return c


HOME, SCRATCH, LOGP = ("nas:/mnt/everything/shared-home", "nas:/mnt/everything/scratch",
                       "nas2:/mnt/data/projects")


def test_latest_size_of_each_export_and_the_free_space_they_share(tmp_path):
    c = _sized_db(tmp_path / "c.db", [
        ("2026-10-05T22:00:00", "adam", "/home", HOME, 1, 40 * TB, 2 * TB, 38 * TB),
        ("2026-10-05T22:21:00", "adam", "/home", HOME, 1, 40 * TB, 3 * TB, 37 * TB),
        ("2026-10-05T22:21:00", "adam", "/scratch", SCRATCH, 1, 45 * TB, 8 * TB, 37 * TB),
        ("2026-10-05T22:21:00", "adam", "/projects", LOGP, 1, 9 * TB, 1 * TB, 37 * TB),
        ("2026-10-05T22:26:00", "boyi", "/home", HOME, 0, None, None, None),   # hung
    ])
    sizes = export_sizes(c, [HOME, SCRATCH, LOGP, "nas", "nowhere:/x"])
    assert set(sizes) == {HOME, SCRATCH, LOGP}
    home = sizes[HOME]
    assert (home.when[:16], home.host, home.used, home.avail) == (
        "2026-10-05T22:21", "adam", 3 * TB, 37 * TB)
    # Same server, same machine, same moment, same free space: one pool.
    assert home.shares_free_with == (SCRATCH,)
    assert sizes[SCRATCH].shares_free_with == (HOME,)
    # Another server with the same free space by chance is not counted as one.
    assert sizes[LOGP].shares_free_with == ()


def test_sharing_is_found_whichever_machine_was_read(tmp_path):
    # adam mounts only shared-home and is read first; boyi mounts both. The
    # latest shared-home reading comes from adam, yet both lines say it.
    c = _sized_db(tmp_path / "c.db", [
        ("2026-10-05T22:21:00", "adam", "/home", HOME, 1, 40 * TB, 3 * TB, 37 * TB),
        ("2026-10-05T22:21:00", "boyi", "/home", HOME, 1, 40 * TB, 3 * TB, 37 * TB),
        ("2026-10-05T22:21:00", "boyi", "/scratch", SCRATCH, 1, 45 * TB, 8 * TB, 37 * TB),
    ])
    sizes = export_sizes(c, [HOME, SCRATCH])
    assert sizes[HOME].host == "adam"                     # ties go by machine name
    assert sizes[HOME].shares_free_with == (SCRATCH,)
    assert sizes[SCRATCH].shares_free_with == (HOME,)


def test_exports_on_different_machines_are_seen_sharing(tmp_path):
    # One export is mounted on a single machine, the other on five others;
    # no machine reads both, and the run read the same free space for them.
    logp, flaps = "srv:/mnt/pool/b", "srv:/mnt/pool/a"
    free = 35265522958336
    rows = [("2026-10-06T08:21:35", "ws9", "/b", logp, 1, 58 * TB, 23 * TB, free)]
    rows += [("2026-10-06T08:21:35", h, "/a", flaps, 1, 43 * TB, 8 * TB, free)
             for h in ("ws1", "ws2", "ws3", "ws4", "ws5")]
    # Another server 43 GB apart in the same run stays its own pool.
    rows += [("2026-10-06T08:21:35", "adam", "/home", HOME, 1, 90 * TB, 55 * TB,
              free + 43 * 10 ** 9)]
    c = _sized_db(tmp_path / "c.db", rows)
    sizes = export_sizes(c, [logp, flaps, HOME])
    assert sizes[logp].shares_free_with == (flaps,)
    assert sizes[flaps].shares_free_with == (logp,)
    assert sizes[HOME].shares_free_with == ()
    group, = shared_free(c, sizes)
    assert (group.exports, group.used, group.avail) == ((flaps, logp), 31 * TB, free)


def test_two_exports_43_gb_apart_on_one_server_are_two_pools(tmp_path):
    a, b = "srv:/pool1/a", "srv:/pool2/b"
    c = _sized_db(tmp_path / "c.db", [
        ("2026-10-06T08:21:35", "ws9", "/a", a, 1, 58 * TB, 23 * TB, 35 * TB),
        ("2026-10-06T08:21:35", "adam", "/b", b, 1, 43 * TB, 8 * TB, 35 * TB + 43 * 10 ** 9),
    ])
    assert export_sizes(c, [a, b])[a].shares_free_with == ()


def test_sharing_tolerates_writes_between_the_two_readings(tmp_path):
    busy = 37 * TB - 2 ** 20                    # 1 MiB written in between
    other_pool = int(37 * TB * 0.98)            # 2% apart: another pool
    c = _sized_db(tmp_path / "c.db", [
        ("2026-10-05T22:21:00", "adam", "/home", HOME, 1, 40 * TB, 3 * TB, 37 * TB),
        ("2026-10-05T22:21:00", "adam", "/scratch", SCRATCH, 1, 45 * TB, 8 * TB, busy),
        ("2026-10-05T22:21:00", "adam", "/p", "nas:/mnt/other/p", 1, 9 * TB, 1 * TB, other_pool),
    ])
    sizes = export_sizes(c, [HOME, SCRATCH, "nas:/mnt/other/p"])
    assert sizes[HOME].shares_free_with == (SCRATCH,)
    assert sizes["nas:/mnt/other/p"].shares_free_with == ()


def test_sharing_chains(tmp_path):
    a, b, d = "nas:/a", "nas:/b", "nas:/d"
    c = _sized_db(tmp_path / "c.db", [
        ("2026-10-05T22:21:00", "adam", "/a", a, 1, 40 * TB, 1 * TB, 37 * TB),
        ("2026-10-05T22:21:00", "adam", "/b", b, 1, 40 * TB, 2 * TB, 37 * TB),
        ("2026-10-05T22:21:00", "boyi", "/b", b, 1, 40 * TB, 2 * TB, 37 * TB),
        ("2026-10-05T22:21:00", "boyi", "/d", d, 1, 40 * TB, 4 * TB, 37 * TB),
    ])
    sizes = export_sizes(c, [a, b, d])
    assert sizes[a].shares_free_with == (b, d)
    group, = shared_free(c, sizes)
    assert (group.exports, group.used, group.avail, group.when[:16]) == (
        (a, b, d), 7 * TB, 37 * TB, "2026-10-05T22:21")


def test_separate_pools_with_close_free_space_stay_apart(tmp_path):
    # Identical disks, lightly used: 48.00 and 47.97 TB free is 0.06% apart,
    # but 30 GB is more than writes between two reads a moment apart.
    a, b = "nas:/pool1/a", "nas:/pool2/b"
    c = _sized_db(tmp_path / "c.db", [
        ("2026-10-05T22:21:00", "adam", "/a", a, 1, 49 * TB, 1 * TB, 48 * TB),
        ("2026-10-05T22:21:00", "adam", "/b", b, 1, 49 * TB, 1 * TB, int(47.97 * TB)),
    ])
    assert export_sizes(c, [a, b])[a].shares_free_with == ()


def test_together_is_one_run_and_says_when(tmp_path):
    # scratch's last size is from 1 Oct (20 TB free then); shared-home's is
    # from today. Together is what the run of 1 Oct read for both, dated.
    c = _sized_db(tmp_path / "c.db", [
        ("2026-10-01T09:00:00", "adam", "/home", HOME, 1, 22 * TB, 2 * TB, 20 * TB),
        ("2026-10-01T09:00:00", "adam", "/scratch", SCRATCH, 1, 25 * TB, 5 * TB, 20 * TB),
        ("2026-10-05T22:21:00", "adam", "/home", HOME, 1, 40 * TB, 3 * TB, 37 * TB),
        ("2026-10-05T22:21:00", "adam", "/scratch", SCRATCH, 0, None, None, None),
    ])
    sizes = export_sizes(c, [HOME, SCRATCH])
    group, = shared_free(c, sizes)
    assert (group.used, group.avail, group.when[:16]) == (7 * TB, 20 * TB, "2026-10-01T09:00")


def test_together_needs_a_run_that_read_them_all(tmp_path):
    # Shared at 1 Oct; since, never read in one run within a day: no line.
    c = _sized_db(tmp_path / "c.db", [
        ("2026-09-20T09:00:00", "adam", "/home", HOME, 1, 22 * TB, 2 * TB, 20 * TB),
        ("2026-09-20T09:00:00", "adam", "/scratch", SCRATCH, 1, 25 * TB, 5 * TB, 20 * TB),
        ("2026-10-01T09:00:00", "adam", "/scratch", SCRATCH, 1, 25 * TB, 5 * TB, 20 * TB),
        ("2026-10-05T22:21:00", "adam", "/home", HOME, 1, 40 * TB, 3 * TB, 37 * TB),
    ])
    sizes = export_sizes(c, [HOME, SCRATCH])
    assert sizes[HOME].shares_free_with == ()            # no common run at either latest
    assert shared_free(c, sizes) == []


def test_one_filesystem_listed_twice_counts_once(tmp_path):
    # A directory of an export, listed as an export too, reads the same figures.
    sub = HOME + "/carol"
    c = _sized_db(tmp_path / "c.db", [
        ("2026-10-05T22:21:00", "adam", "/home", HOME, 1, 40 * TB, 3 * TB, 37 * TB),
        ("2026-10-05T22:21:00", "adam", "/carol", sub, 1, 40 * TB, 3 * TB, 37 * TB),
        ("2026-10-05T22:21:00", "adam", "/scratch", SCRATCH, 1, 45 * TB, 8 * TB, 37 * TB),
    ])
    group, = shared_free(c, export_sizes(c, [HOME, sub, SCRATCH]))
    assert (len(group.exports), group.used, group.avail) == (3, 11 * TB, 37 * TB)


def test_full_exports_are_not_taken_for_one_pool(tmp_path):
    c = _sized_db(tmp_path / "c.db", [
        ("2026-10-05T22:21:00", "adam", "/home", HOME, 1, 40 * TB, 40 * TB, 0),
        ("2026-10-05T22:21:00", "adam", "/scratch", SCRATCH, 1, 45 * TB, 45 * TB, 0),
    ])
    assert export_sizes(c, [HOME, SCRATCH])[HOME].shares_free_with == ()


def test_no_size_columns_no_sizes(tmp_path):
    c = sqlite3.connect(tmp_path / "old.db")
    c.execute("CREATE TABLE workstation_mount_state (timestamp TEXT, hostname TEXT, "
              "source TEXT, is_mounted INTEGER, is_responsive INTEGER)")
    assert export_sizes(c, [HOME]) == {}
    assert export_sizes(sqlite3.connect(tmp_path / "empty.db"), [HOME]) == {}


@pytest.mark.parametrize("used, avail, pct", [
    (3, 37, 8), (0, 10, 0), (1, 999, 1), (10, 0, 100), (0, 0, None)])
def test_percent_used_as_df_shows_it(used, avail, pct):
    assert ExportSize("t", "h", used + avail, used, avail).used_pct == pct


@pytest.mark.parametrize("n, shown", [
    (0, "0 B"), (999, "999 B"), (1500, "1.5 kB"), (3.1 * TB, "3.1 TB"),
    (40 * TB, "40.0 TB"), (123 * TB, "123 TB"), (2.5 * 10 ** 15, "2.5 PB"),
    (5 * 10 ** 18, "5000 PB"),
    # Rounded before the unit is chosen.
    (999_950, "1.0 MB"), (99.96 * TB, "100 TB"), (99.94 * TB, "99.9 TB"),
    (999.6 * TB, "1.0 PB")])
def test_sizes_in_decimal_units(n, shown):
    assert _bytes_shown(n) == shown


def test_cli_roles_shows_the_space(tmp_path):
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n\n'
                   '[console.labs.resources."carol$"]\n'
                   f'storage = ["{HOME}", "{SCRATCH}", "{LOGP}"]\n\n'
                   '[console.storage."nas"]\nname = "bigstore"\n'
                   'note = "community $HOME, all users"\n')
    dbp = tmp_path / "combined.db"
    c = _sized_db(dbp, [
        ("2026-10-05T22:21:00", "adam", "/home", HOME, 1, 40 * TB, int(3.1 * TB),
         int(36.9 * TB)),
        ("2026-10-05T22:21:00", "adam", "/scratch", SCRATCH, 1, 45 * TB, int(8.1 * TB),
         int(36.9 * TB)),
        ("2026-10-05T21:00:00", "adam", "/projects", LOGP, 1, 9 * TB, 1 * TB, 8 * TB),
        ("2026-10-05T22:21:00", "adam", "/projects", LOGP, 1, None, None, None),  # v1 probe since
    ])
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, cluster TEXT)")
    c.execute("INSERT INTO group_membership VALUES ('s1', 'carol$', 'spydur')")
    c.commit()
    r = CliRunner().invoke(cli, ["-c", str(cfg), "console", "roles", "carol", "--db", str(dbp)])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "  storage:   1 server named (bigstore)" in out
    # By server, named and with its note, so a shared server's use isn't
    # read as the lab's; its exports below, by path.
    assert ("    bigstore (nas), community $HOME, all users:\n"
            "      /mnt/everything/scratch: mounted and responding, 2026-10-05T22:21; "
            "18% used (8.1 TB), 36.9 TB free; free space shared with "
            "/mnt/everything/shared-home\n"
            # Sharing its free space, an export's total overlaps the other's: no "of".
            "      /mnt/everything/shared-home: mounted and responding, 2026-10-05T22:21; "
            "8% used (3.1 TB), 36.9 TB free; free space shared with /mnt/everything/scratch\n"
            # And their space together, the free space counted once.
            "      /mnt/everything/scratch + /mnt/everything/shared-home, together: "
            "24% used (11.2 TB), 36.9 TB free\n") in out
    # A server not named shows as the mounts name it. The last size read is
    # older than the last sign of life: it says when.
    assert ("    nas2:\n      /mnt/data/projects: mounted and responding, 2026-10-05T22:21; "
            "12% used (1.0 TB of 9.0 TB), 8.0 TB free at 2026-10-05T21:00") in out
    r = CliRunner().invoke(cli, ["-c", str(cfg), "console", "roles", "carol", "--db", str(dbp),
                                 "--mask"])
    masked = r.output
    for secret in ("everything", "nas", "bigstore", "community", "projects"):
        assert secret not in masked, secret
    assert "  storage:   1 server named\n" in masked
    assert ("    storage server #1:\n"
            "      export #1: mounted and responding, 2026-10-05T22:21; 18% used") in masked
    assert "free space shared with export #1" in masked
    assert "      export #1 + export #2, together: 24% used (11.2 TB), 36.9 TB free" in masked
    assert "    storage server #2:\n      export #1: mounted and responding" in masked


@pytest.mark.parametrize("storage, problem", [
    ({"nas:/x": {"name": "a"}}, "the part before the colon"),
    ({"nas": "bigstore"}, "should be a table"),
    ({"nas": {"name": 3}}, "name should be text"),
    ({"nas": {"label": "x"}}, "label: unknown"),
    ("nas", "one table per storage server"),
])
def test_wrong_storage_settings_are_said_and_left_out(storage, problem):
    from nomad.config.access import access_from
    a = access_from({"console": {"storage": storage}}, log=False)
    assert a.servers == {}
    assert any(problem in p for p in a.problems), a.problems


def test_storage_names_and_notes():
    from nomad.config.access import access_from
    a = access_from({"console": {"storage": {
        " 10.0.0.28 ": {"name": "sarahvaughan", "note": "community  $HOME,\nall users"},
        "10.0.0.43": {"note": "cold storage"},
        "empty": {}}}}, log=False)
    assert a.servers == {"10.0.0.28": ("sarahvaughan", "community $HOME, all users"),
                         "10.0.0.43": ("", "cold storage")}
    assert a.problems == ()


def test_sync_adds_the_size_columns_to_the_combined_database(tmp_path, monkeypatch):
    import nomad.cli as cli_mod
    home = tmp_path / "home"
    cache = home / ".local" / "share" / "nomad" / "sync_cache"
    cache.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # alpha still runs a nomad from before the sizes; beta has them.
    old = sqlite3.connect(cache / "alpha.db")
    _bootstrap_db_with_migrations(str(cache / "alpha.db"))
    for col in ("total_bytes", "used_bytes", "avail_bytes"):
        old.execute(f"ALTER TABLE workstation_mount_state DROP COLUMN {col}")
    old.execute("INSERT INTO workstation_mount_state (timestamp, hostname, mountpoint, source, "
                "is_mounted, is_responsive) VALUES ('2026-10-05T22:00:00', 'a1', '/h', "
                "'nas:/h', 1, 1)")
    old.commit()
    old.close()
    _sized_db(cache / "beta.db", [
        ("2026-10-05T22:21:00", "b1", "/home", HOME, 1, 40 * TB, 3 * TB, 37 * TB)]).close()
    cfg = tmp_path / "sync.toml"
    cfg.write_text('[[sites]]\nname = "alpha"\nhost = "alpha-head"\nssh_user = "x"\n\n'
                   '[[sites]]\nname = "beta"\nhost = "beta-head"\nssh_user = "x"\n')
    monkeypatch.setattr(cli_mod, "_pull_via_backup", lambda *a, **k: False)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "offline"))
    out = tmp_path / "combined.db"
    r = CliRunner().invoke(cli_mod.cli, ["sync", "-c", str(cfg), "-o", str(out)])
    assert r.exit_code == 0, r.output
    rows = sqlite3.connect(out).execute(
        "SELECT source_site, total_bytes FROM workstation_mount_state "
        "ORDER BY source_site").fetchall()
    assert rows == [("alpha", None), ("beta", 40 * TB)]
    indexes = {r[0] for r in sqlite3.connect(out).execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"idx_workstation_mount_state_source_timestamp",
            "idx_workstation_mount_state_hostname_timestamp"} <= indexes

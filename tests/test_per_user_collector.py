# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
PerUserCollector as it runs: from cron, a fresh collector every 5 minutes.

A fake host holds processes whose counters advance with a fake clock; each
reading builds a new collector (nothing carries over in memory, as under
cron) and runs it against a real SQLite database. What carries over is
per_user_state.

Before 1.7.15 the collector kept CPU baselines and rule windows in memory:
under cron every process read 0% CPU and no rule could fire.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from nomad.collectors.per_user import PerUserCollector, ProcessSnapshot, read_user_slices
from nomad.collectors.per_user import collector as collector_mod
from nomad.collectors.per_user.ancestry import ProcessInfo
from nomad.collectors.per_user.report import report

GB = 1024 ** 3
MB = 1000 ** 2
T0 = 1_790_000_000.0          # 2026-09-21, a fixed clock


@dataclass
class Proc:
    pid: int
    user: str
    uid: int
    command: str
    exe: str | None
    started_at: float
    cores: float                 # CPU it uses from now on
    rss: int
    cpu_seconds: float = 0.0
    io_mbs: float | None = None  # MB/s read and written each; None: unreadable
    io_read: int = 0
    io_write: int = 0
    ppid: int = 1
    since_boot: float | None = None
    slice_uid: int | None = None


class Host:
    """A login node: processes whose counters advance with the clock."""

    def __init__(self, tmp_path):
        self.now = T0
        self.procs: dict[int, Proc] = {}
        self.slices: dict[int, float] = {}
        self.cgroup = tmp_path / "cgroup"

    def start(self, pid, user="ann", uid=10001, command="python", *, cores=0.0, rss_gb=0.1,
              age=0.0, past_cores=None, exe=None, io_mbs=None, ppid=1):
        """A process started ``age`` seconds ago, which used ``past_cores``
        (default: ``cores``) on average until now."""
        p = Proc(pid, user, uid, command, exe or f"/home/{user}/bin/{command}",
                 self.now - age, cores, int(rss_gb * GB), io_mbs=io_mbs, ppid=ppid)
        p.cpu_seconds = (cores if past_cores is None else past_cores) * age
        if io_mbs is not None:
            p.io_read = p.io_write = int(io_mbs * MB * age)
        self.procs[pid] = p
        return p

    def stop(self, pid):
        self.procs.pop(pid)

    def advance(self, seconds=300.0):
        self.now += seconds
        for p in self.procs.values():
            p.cpu_seconds += p.cores * seconds
            if p.io_mbs is not None:
                p.io_read += int(p.io_mbs * MB * seconds)
                p.io_write += int(p.io_mbs * MB * seconds)

    def user_slice(self, uid, cpu_seconds):
        self.slices[uid] = cpu_seconds
        d = self.cgroup / "user.slice" / f"user-{uid}.slice"
        d.mkdir(parents=True, exist_ok=True)
        (d / "cpu.stat").write_text(
            f"usage_usec {int(cpu_seconds * 1e6)}\nuser_usec 0\nsystem_usec 0\n")

    def snapshots(self):
        return [ProcessSnapshot(
            info=ProcessInfo(pid=p.pid, ppid=p.ppid, uid=p.uid, username=p.user,
                             command=p.command, exe_path=p.exe),
            cpu_seconds=p.cpu_seconds, memory_rss_bytes=p.rss, memory_vms_bytes=p.rss * 2,
            num_threads=1, num_fds=None, started_at=p.started_at,
            cmdline=f"{p.command} --input data.csv",
            io_read_bytes=p.io_read if p.io_mbs is not None else None,
            io_write_bytes=p.io_write if p.io_mbs is not None else None,
            since_boot=p.since_boot, slice_uid=p.slice_uid,
        ) for p in self.procs.values()]


@pytest.fixture
def host(tmp_path, monkeypatch):
    h = Host(tmp_path)
    monkeypatch.setattr(collector_mod.time, "time", lambda: h.now)
    return h


def reading(host, db_path, config=None):
    """One cron run: a new collector, reading the fake host."""
    cfg = {"enabled": True, "role": "headnode", "cgroup_root": str(host.cgroup)}
    cfg.update(config or {})

    class OneRun(PerUserCollector):
        def iter_processes(self):
            return iter(host.snapshots())

    result = OneRun(cfg, db_path).run()
    assert result.success, result.error_message
    return result


def rows(db_path, sql, *args):
    with sqlite3.connect(db_path) as c:
        return c.execute(sql, args).fetchall()


def alerts(db_path):
    return {r[0]: r[1:] for r in rows(
        db_path, "SELECT rule_id, occurrences, sustained_for_seconds, last_seen, severity, "
                 "process_session_id, command FROM per_user_alert")}


# ---------------------------------------------------------------------------
# CPU from one run to the next
# ---------------------------------------------------------------------------

def test_cpu_is_the_average_over_the_interval_since_the_last_run(host, db_path):
    host.start(5001, cores=0.8, past_cores=0.02, age=86400)    # idle for a day, busy now
    reading(host, db_path)
    assert rows(db_path, "SELECT COUNT(*) FROM per_user_sample") == [(0,)]   # 2%: below the floor
    host.advance(300)
    reading(host, db_path)
    (cpu, window), = rows(db_path, "SELECT cpu_percent, cpu_window_seconds FROM per_user_sample")
    assert cpu == pytest.approx(80.0) and window == pytest.approx(300.0)
    fired = alerts(db_path)
    assert set(fired) == {"cpu_10pct_5min", "cpu_50pct_2min"}
    assert fired["cpu_50pct_2min"][1] == 300                    # held since the last run


def test_a_process_running_flat_out_for_days_is_flagged_from_its_start(host, db_path):
    host.start(5001, cores=1.0, age=2 * 86400)
    reading(host, db_path)
    (cpu, window), = rows(db_path, "SELECT cpu_percent, cpu_window_seconds FROM per_user_sample")
    assert cpu == pytest.approx(100.0) and window == pytest.approx(2 * 86400)
    # Its average since it started is stored, but fires nothing until the
    # next reading confirms it is busy now...
    assert alerts(db_path) == {}
    host.advance()
    reading(host, db_path)
    fired = alerts(db_path)
    assert {"cpu_10pct_5min", "cpu_50pct_2min"} <= set(fired)
    assert fired["cpu_50pct_2min"][1] == 2 * 86400 + 300        # ...dated from its start


def test_a_process_busy_long_ago_and_idle_since_is_not_flagged(host, db_path):
    # 12 hours flat out, then 60 hours idle: 16.7% since it started.
    host.start(5001, cores=0.0, past_cores=12 / 72, age=72 * 3600)
    reading(host, db_path)
    host.advance()
    reading(host, db_path)
    assert alerts(db_path) == {}


def test_a_process_started_since_the_last_reading_is_judged_on_its_life(host, db_path):
    reading(host, db_path)
    host.advance(100)
    host.start(5001, cores=1.0, age=0)
    host.advance(200)
    reading(host, db_path)
    assert alerts(db_path)["cpu_50pct_2min"][1] == 200


def test_a_process_seconds_old_is_not_judged_on_its_first_seconds(host, db_path):
    host.start(5001, cores=1.0, age=2.0)
    reading(host, db_path)
    assert rows(db_path, "SELECT COUNT(*) FROM per_user_sample") == [(0,)]
    assert alerts(db_path) == {}


def test_idle_processes_leave_no_rows_but_are_remembered(host, db_path):
    for pid in range(5001, 5051):
        host.start(pid, cores=0.001, age=3600)
    reading(host, db_path)
    assert rows(db_path, "SELECT COUNT(*) FROM per_user_sample") == [(0,)]
    kinds = dict(rows(db_path, "SELECT kind, COUNT(*) FROM per_user_state GROUP BY kind"))
    assert kinds == {"process": 50, "run": 1, "user": 1}


def test_state_keeps_only_live_processes(host, db_path):
    host.start(5001, age=60)
    host.start(5002, age=60)
    reading(host, db_path)
    host.stop(5001)
    host.advance()
    reading(host, db_path)
    assert rows(db_path, "SELECT COUNT(*) FROM per_user_state WHERE kind = 'process'") == [(1,)]


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def test_one_alert_row_per_process_and_rule_grows_while_it_goes_on(host, db_path):
    host.start(5001, cores=0.95, age=60)
    for _ in range(4):
        host.advance()
        reading(host, db_path)
    fired = alerts(db_path)
    occurrences, sustained, last_seen, severity, _, command = fired["cpu_50pct_2min"]
    assert occurrences == 3                           # the first reading only starts the count
    assert sustained == 60 + 4 * 300                  # held since the process started
    assert last_seen == collector_mod._utc_iso(host.now)
    assert severity == "actionable" and command == "python"
    (peak,), = rows(db_path, "SELECT peak_cpu_percent FROM per_user_alert "
                             "WHERE rule_id = 'cpu_50pct_2min'")
    assert peak == pytest.approx(95.0)


def test_a_memory_rule_needs_readings_that_span_its_duration(host, db_path):
    host.start(5001, rss_gb=5, age=3600)
    reading(host, db_path)
    host.advance()
    reading(host, db_path)
    assert "memory_4gb_10min" not in alerts(db_path)          # 300 s of 600
    host.advance()
    reading(host, db_path)
    fired = alerts(db_path)
    assert fired["memory_4gb_10min"][1:2] == (600,)
    assert fired["memory_4gb_10min"][3] == "informational"


def test_a_dip_below_starts_the_count_again(host, db_path):
    p = host.start(5001, rss_gb=5, age=3600)
    reading(host, db_path)
    host.advance()
    p.rss = int(1 * GB)
    reading(host, db_path)
    host.advance()
    p.rss = int(5 * GB)
    reading(host, db_path)
    host.advance()
    reading(host, db_path)
    assert "memory_4gb_10min" not in alerts(db_path)          # 300 s since it came back


def test_a_whitelisted_process_is_stored_marked_and_never_flagged(host, db_path):
    config = {"whitelist": {"parent_paths": ["/usr/local/sw/"]}}
    for pid in (5001, 5002, 5003):
        host.start(pid, user="backup", uid=10500, command="clusterbackup.py",
                   exe="/usr/local/sw/clusterbackup/clusterbackup.py", cores=0.95, age=3600)
    for _ in range(4):
        reading(host, db_path, config)
        host.advance()
    assert alerts(db_path) == {}                               # nor user_cpu at 285%
    (n, match), = rows(db_path, "SELECT COUNT(*), MIN(whitelist_match) FROM per_user_sample")
    assert n == 12 and match == "parent_path:/usr/local/sw/"


def test_system_accounts_are_never_flagged(host, db_path):
    host.start(900, user="root", uid=0, command="dnf", cores=1.0, age=3600)
    reading(host, db_path)
    assert alerts(db_path) == {}
    assert rows(db_path, "SELECT whitelist_match FROM per_user_sample") == [("min_uid:uid=0",)]


def test_a_users_processes_together_fire_the_user_rule(host, db_path):
    for pid in range(5001, 5005):
        host.start(pid, command="worker", cores=0.3, age=60)   # 30% each: 120%...
    host.start(5005, command="python", cores=1.2, age=60)      # ...plus 120%: 240%
    reading(host, db_path)
    host.advance()
    reading(host, db_path)
    assert "user_cpu_200pct_10min" not in alerts(db_path)     # held 300 s of 600
    host.advance()
    reading(host, db_path)
    fired = alerts(db_path)
    occurrences, sustained, _, severity, session, command = fired["user_cpu_200pct_10min"]
    assert sustained == 600 and severity == "actionable"
    assert session == "user-10001" and command == "python"   # the busiest
    host.advance()
    reading(host, db_path)
    assert alerts(db_path)["user_cpu_200pct_10min"][0] == 2   # same episode, one row


def test_a_run_by_hand_seconds_after_crons_changes_nothing(host, db_path):
    for pid in range(5001, 5004):
        host.start(pid, command="worker", cores=0.8, age=60)
    reading(host, db_path)
    host.advance(300)
    reading(host, db_path)
    host.advance(5)
    reading(host, db_path)                                     # nomad collect -C per_user
    host.advance(295)
    reading(host, db_path)
    got = rows(db_path, "SELECT sustained_for_seconds FROM per_user_alert "
                        "WHERE rule_id = 'user_cpu_200pct_10min'")
    assert got == [(600,)]                                    # one episode, from the start
    (sustained,), = rows(db_path, "SELECT MAX(sustained_for_seconds) FROM per_user_alert "
                                  "WHERE rule_id = 'cpu_50pct_2min'")
    assert sustained == 60 + 600


def test_a_swarm_of_short_processes_is_seen_through_the_user_slice(host, db_path):
    # Nothing long-lived is busy, but the user's slice used 2.5 cores: a
    # parallel make whose compilers start and end between readings.
    host.start(5001, command="make", cores=0.01, age=60)
    used = 1000.0
    for _ in range(3):
        host.user_slice(10001, used)
        reading(host, db_path)
        host.advance()
        used += 2.5 * 300
    assert "user_cpu_200pct_10min" in alerts(db_path)
    (cpu, source), = rows(db_path, "SELECT cpu_seconds, cpu_source FROM per_user_daily")
    assert cpu == pytest.approx(2 * 750) and source == "cgroup"


def test_rules_from_the_config_replace_the_defaults(host, db_path):
    config = {"rules": [{"rule_id": "cpu_90", "rule_type": "cpu", "threshold": 90,
                         "duration_seconds": 600, "severity": "informational"}]}
    host.start(5001, cores=0.95, age=60)
    for _ in range(3):
        host.advance()
        reading(host, db_path, config)
    assert set(alerts(db_path)) == {"cpu_90"}


def test_each_episode_has_its_own_row_peaks_and_span(host, db_path):
    p = host.start(5001, cores=0.9, age=60)
    for _ in range(4):                                         # busy 15 minutes
        reading(host, db_path)
        host.advance()
    p.cores = 0.0
    for _ in range(6):                                         # idle half an hour
        reading(host, db_path)
        host.advance()
    p.cores = 0.6
    for _ in range(2):                                         # busy again
        reading(host, db_path)
        host.advance()
    got = rows(db_path, "SELECT sustained_for_seconds, ROUND(peak_cpu_percent) FROM per_user_alert "
                        "WHERE rule_id = 'cpu_50pct_2min' ORDER BY id")
    assert got == [(60 + 1200, 90.0), (300, 60.0)]          # busy until the 5th reading
    with sqlite3.connect(db_path) as c:
        text = "\n".join(report(c, days=36500))
    assert "Flagged: 2" in text


def test_a_users_later_episode_does_not_inherit_an_earlier_peak(host, db_path):
    workers = [host.start(pid, command="worker", cores=1.0, age=60) for pid in range(5001, 5009)]
    for _ in range(3):                                         # 8 cores
        reading(host, db_path)
        host.advance()
    for w in workers:
        w.cores = 0.0
    for _ in range(3):
        reading(host, db_path)
        host.advance()
    for w in workers[:3]:
        w.cores = 1.0                                          # 3 cores
    for _ in range(3):
        reading(host, db_path)
        host.advance()
    got = rows(db_path, "SELECT ROUND(peak_cpu_percent) FROM per_user_alert "
                        "WHERE rule_id = 'user_cpu_200pct_10min' ORDER BY id")
    assert got == [(800.0,), (300.0,)]


def test_a_run_that_overlaps_another_leaves_its_totals_out(host, db_path):
    host.start(5001, cores=1.0, age=60)
    reading(host, db_path)
    host.advance()

    class OneRun(PerUserCollector):
        def iter_processes(self):
            return iter(host.snapshots())

    cfg = {"enabled": True, "cgroup_root": str(host.cgroup)}
    a, b = OneRun(cfg, db_path), OneRun(cfg, db_path)
    data_a, data_b = a.collect(), b.collect()                  # both read the same state
    a.store(data_a)
    b.store(data_b)
    (cpu,), = rows(db_path, "SELECT cpu_seconds FROM per_user_daily")
    assert cpu == pytest.approx(300.0)                         # not 600
    assert "left out" in b.note
    assert rows(db_path, "SELECT MAX(occurrences) FROM per_user_alert") == [(1,)]


def test_a_user_with_nothing_alive_still_counts_through_the_slice(host, db_path):
    host.start(5001, user="ann", cores=0.0, age=60)
    host.user_slice(10002, 100.0)                              # bob: a slice, no process
    reading(host, db_path)
    host.advance()
    host.user_slice(10002, 100.0 + 2.5 * 300)
    reading(host, db_path)
    host.advance()
    host.user_slice(10002, 100.0 + 5.0 * 300)
    reading(host, db_path)
    got = rows(db_path, "SELECT uid, cpu_seconds, cpu_source FROM per_user_daily")
    assert got == [(10002, pytest.approx(1500.0), "cgroup")]
    (pid, command), = rows(db_path, "SELECT pid, command FROM per_user_alert "
                                    "WHERE rule_id = 'user_cpu_200pct_10min'")
    assert (pid, command) == (0, "(user slice)")


def test_root_work_inside_a_users_slice_is_not_that_users(host, db_path):
    # An admin's sudo dnf runs in the admin's slice but as root.
    host.start(5001, user="adm", uid=10009, command="bash", cores=0.0, age=60).slice_uid = 10009
    host.start(900, user="root", uid=0, command="dnf", cores=2.0, age=60).slice_uid = 10009
    used = 0.0
    for _ in range(4):
        host.user_slice(10009, used)
        reading(host, db_path)
        host.advance()
        used += 2.0 * 300
    assert "user_cpu_200pct_10min" not in alerts(db_path)
    assert rows(db_path, "SELECT COUNT(*) FROM per_user_daily") == [(0,)]


def test_a_long_gap_is_not_added_to_one_day(host, db_path):
    host.start(5001, cores=1.0, age=60)
    reading(host, db_path)
    host.advance(3 * 86400)                                    # the collector was off
    reading(host, db_path)
    assert rows(db_path, "SELECT COUNT(*) FROM per_user_daily") == [(0,)]
    host.advance()
    reading(host, db_path)
    assert rows(db_path, "SELECT cpu_seconds FROM per_user_daily") == [(pytest.approx(300.0),)]


def test_a_clock_step_does_not_make_every_process_new(host, db_path):
    p = host.start(5001, cores=1.0, age=3600)
    p.since_boot = 5000.0
    reading(host, db_path)
    host.advance()
    p.started_at += 7.0                                        # boot time moved: wall start too
    reading(host, db_path)
    (cpu, window), = rows(db_path, "SELECT cpu_percent, cpu_window_seconds FROM per_user_sample "
                                   "ORDER BY id DESC LIMIT 1")
    assert window == pytest.approx(300.0)                      # read against its last reading


def test_nomads_own_cpu_in_its_accounts_slice_is_not_that_accounts(host, db_path):
    # Cron runs nomad in a session of its account (zeus): its CPU, and that
    # of the commands it runs, lands in zeus's slice.
    host.start(5001, user="zeus", uid=4321, command="bash", cores=0.0, age=60)
    used = 0.0
    cfg = {"enabled": True, "cgroup_root": str(host.cgroup), "whitelist": {"min_uid": 1000}}
    for i in range(3):
        host.user_slice(4321, used)
        me = (90000 + i, host.now - 1.0, 20.0, 4321)        # a new process each run:
                                                           # 20 CPU-seconds
        class Cron(PerUserCollector):
            def iter_processes(self):
                return iter(host.snapshots())

            def own_usage(self, me=me):
                return me

        assert Cron(cfg, db_path).run().success
        host.advance()
        used += 20.0
    assert rows(db_path, "SELECT COUNT(*) FROM per_user_daily") == [(0,)]


def test_only_ones_own_processes_visible_says_so(host, db_path, monkeypatch):
    monkeypatch.setattr(collector_mod.os, "geteuid", lambda: 4321)
    host.start(5001, user="zeus", uid=4321, age=60)
    assert "hidepid" in reading(host, db_path).note
    host.start(5002, user="ann", uid=10001, age=60)
    assert reading(host, db_path).note is None


def test_scripts_run_by_an_interpreter_can_be_whitelisted(host, db_path):
    from nomad.collectors.per_user.collector import script_of
    assert script_of(["python3", "-u", "/usr/local/sw/x/../x/backup.py", "--all"]) == \
        "/usr/local/sw/x/backup.py"
    assert script_of(["/usr/bin/perl", "tool.pl"]) == "tool.pl"
    assert script_of(["python3", "-c", "print(1)"]) is None
    assert script_of(["gmx_mpi", "mdrun"]) is None
    # An option that may take a value, a module, or stdin: never a script
    # (user code must not pass for an installed tool).
    for args in (["python3", "-X", "/usr/local/sw/x", "heavy.py"],
                 ["python3", "-", "/usr/local/sw/x"],
                 ["bash", "-s", "/usr/local/sw/x"],
                 ["bash", "--rcfile", "/usr/local/sw/x", "heavy.sh"],
                 ["python3", "-W", "ignore", "heavy.py"],
                 ["python3", "-m", "pkg.mod"],
                 ["node", "-r", "/usr/local/sw/register.js", "app.js"],
                 ["julia", "-L", "/usr/local/sw/setup.jl", "job.jl"]):
        assert script_of(args) is None, args
    assert script_of(["bash", "-e", "/usr/local/sw/run.sh"]) == "/usr/local/sw/run.sh"
    config = {"whitelist": {"parent_paths": ["/usr/local/sw/"],
                            "user_commands": [["bob", "sync_lab.py"]]}}
    a = host.start(5001, user="ann", command="python3", exe="/usr/bin/python3.11",
                   cores=1.0, age=3600)
    b = host.start(5002, user="bob", uid=10002, command="sync_lab.py", exe=None,
                   cores=1.0, age=3600)

    class Scripts(PerUserCollector):
        def iter_processes(self):
            snaps = host.snapshots()
            for snap in snaps:
                script = {5001: "/usr/local/sw/backup/backup.py",
                          5002: "/home/bob/bin/sync_lab.py"}[snap.info.pid]
                snap.info = type(snap.info)(**{**snap.info.__dict__, "script": script})
            return iter(snaps)

    cfg = {"enabled": True, "cgroup_root": str(host.cgroup), **config}
    for _ in range(2):
        assert Scripts(cfg, db_path).run().success
        host.advance()
    assert alerts(db_path) == {}
    got = dict(rows(db_path, "SELECT pid, MIN(whitelist_match) FROM per_user_sample GROUP BY 1"))
    assert got == {5001: "parent_path:/usr/local/sw/", 5002: "user_command:bob:sync_lab.py"}
    assert a and b


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def test_io_where_readable_and_none_where_not(host, db_path):
    host.start(5001, command="rsync", io_mbs=40, rss_gb=0.1, age=60)      # 80 MB/s in all
    host.start(5002, command="cp", cores=0.5, age=60)                     # unreadable
    reading(host, db_path)
    host.advance()
    reading(host, db_path)
    got = {r[0]: r[1:] for r in rows(
        db_path, "SELECT command, io_read_bps, io_write_bps FROM per_user_sample "
                 "WHERE timestamp = ?", collector_mod._utc_iso(host.now))}
    assert got["rsync"] == (pytest.approx(40 * MB), pytest.approx(40 * MB))
    assert got["cp"] == (None, None)
    host.advance()
    reading(host, db_path)
    assert "io_50mbs_10min" in alerts(db_path)


# ---------------------------------------------------------------------------
# Daily totals and retention
# ---------------------------------------------------------------------------

def test_daily_totals_add_up_each_users_intervals(host, db_path):
    host.start(5001, user="ann", uid=10001, cores=1.5, rss_gb=3, age=60)
    host.start(5002, user="ann", uid=10001, cores=0.5, rss_gb=1, age=60)
    host.start(5003, user="bob", uid=10002, cores=0.05, age=60)
    host.start(5004, user="cid", uid=10003, cores=0.0, age=60)       # an idle login
    reading(host, db_path)
    assert rows(db_path, "SELECT COUNT(*) FROM per_user_daily") == [(0,)]   # no interval yet
    for _ in range(3):
        host.advance()
        reading(host, db_path)
    got = {r[0]: r[1:] for r in rows(
        db_path, "SELECT username, cpu_seconds, busy_seconds, peak_cpu_percent, "
                 "peak_memory_bytes, cpu_source FROM per_user_daily")}
    assert set(got) == {"ann", "bob"}
    cpu, busy, peak, mem, source = got["ann"]
    assert cpu == pytest.approx(3 * 300 * 2.0) and busy == pytest.approx(900)
    assert peak == pytest.approx(200.0) and mem == 4 * GB and source == "processes"
    assert got["bob"][0] == pytest.approx(45.0) and got["bob"][1] == 0     # 5%: not busy


def test_a_process_started_since_the_last_run_counts_whole(host, db_path):
    reading(host, db_path)
    host.advance(200)
    host.start(5001, cores=1.0, age=0)
    host.advance(100)
    reading(host, db_path)
    (cpu,), = rows(db_path, "SELECT cpu_seconds FROM per_user_daily")
    assert cpu == pytest.approx(100.0)


def _old_sample(c, ts):
    c.execute("INSERT INTO per_user_sample (timestamp, hostname, role, username, uid, pid, "
              "process_session_id) VALUES (?, 'h', 'headnode', 'u', 10001, 1, 's')", (ts,))


def test_old_raw_samples_are_pruned_a_batch_per_run(host, db_path, monkeypatch):
    monkeypatch.setattr(collector_mod, "PRUNE_BATCH", 2)
    with sqlite3.connect(db_path) as c:
        for _ in range(5):
            _old_sample(c, "2026-05-12 10:00:00")
        _old_sample(c, collector_mod._utc_iso(host.now - 86400))
        c.execute("INSERT INTO per_user_alert (fired_at, hostname, role, username, uid, pid, "
                  "process_session_id, rule_id, rule_type, severity, dedup_key, last_seen) "
                  "VALUES ('2026-05-12 10:00:00', 'h', 'headnode', 'u', 1, 1, 's', 'r', 'cpu', "
                  "'actionable', 'k', '2026-05-12 10:00:00')")
    counts = []
    for _ in range(4):
        reading(host, db_path)
        host.advance()
        counts.append(rows(db_path, "SELECT COUNT(*) FROM per_user_sample")[0][0])
    assert counts == [4, 2, 1, 1]                     # yesterday's stays
    assert rows(db_path, "SELECT COUNT(*) FROM per_user_alert") == [(1,)]   # alerts are kept


def test_retention_zero_keeps_every_sample(host, db_path):
    with sqlite3.connect(db_path) as c:
        _old_sample(c, "2026-05-12 10:00:00")
    reading(host, db_path, {"sample_retention_days": 0})
    assert rows(db_path, "SELECT COUNT(*) FROM per_user_sample") == [(1,)]


# ---------------------------------------------------------------------------
# The rest
# ---------------------------------------------------------------------------

def test_records_count_samples_alerts_and_daily_rows(host, db_path):
    host.start(5001, cores=1.0, age=3600)
    assert reading(host, db_path).records_collected == 1      # a sample
    host.advance()
    r = reading(host, db_path)
    assert r.records_collected == 1 + 2 + 1                   # a sample, two CPU rules, a day


def test_collector_disabled_returns_early(host, db_path):
    host.start(5001, cores=1.0, age=3600)
    reading(host, db_path, {"enabled": False})
    assert rows(db_path, "SELECT COUNT(*) FROM per_user_sample") == [(0,)]
    assert rows(db_path, "SELECT COUNT(*) FROM per_user_state") == [(0,)]


def test_user_slices_from_cgroup_v2_and_v1(tmp_path):
    v2 = tmp_path / "v2" / "user.slice"
    for uid, usec in ((1000, 5_000_000), (10001, 250_000)):
        (v2 / f"user-{uid}.slice").mkdir(parents=True)
        (v2 / f"user-{uid}.slice" / "cpu.stat").write_text(f"usage_usec {usec}\nuser_usec 1\n")
    (v2 / "user-runtime-dir@1000.service").mkdir()
    assert read_user_slices(str(tmp_path / "v2")) == {1000: 5.0, 10001: 0.25}

    v1 = tmp_path / "v1" / "cpu,cpuacct" / "user.slice" / "user-10001.slice"
    v1.mkdir(parents=True)
    (v1 / "cpuacct.usage").write_text("3000000000\n")
    (tmp_path / "v1" / "memory" / "user.slice" / "user-10001.slice").mkdir(parents=True)
    assert read_user_slices(str(tmp_path / "v1")) == {10001: 3.0}
    assert read_user_slices(str(tmp_path / "none")) == {}


def test_reading_this_hosts_real_processes(tmp_path):
    pytest.importorskip("psutil")
    from nomad.db.migrations import ensure_database
    db = tmp_path / "real.db"
    ensure_database(db)
    c = PerUserCollector({"enabled": True}, db)
    snaps = list(c.iter_processes())
    assert snaps and all(s.cpu_seconds is not None for s in snaps if s.info.pid != 0)
    assert c.run().success
    assert rows(str(db), "SELECT COUNT(*) FROM per_user_state WHERE kind = 'process'")[0][0] > 0


# ---------------------------------------------------------------------------
# nomad per-user
# ---------------------------------------------------------------------------

def test_report_shows_flags_and_totals_and_masks_names(host, db_path):
    host.start(5001, user="ann", uid=10001, command="matlab", cores=1.0, rss_gb=20, age=3600)
    host.start(5002, user="bob", uid=10002, command="R", cores=0.2, age=3600)
    for _ in range(3):
        reading(host, db_path)
        host.advance()
    with sqlite3.connect(db_path) as c:
        plain = "\n".join(report(c, days=36500))
        masked = "\n".join(report(c, days=36500, mask=True))
    assert "ann" in plain and "matlab" in plain and "memory ≥ 16 GB" in plain
    assert "Most CPU here" in plain and "core-hours" in plain
    for name in ("ann", "bob", "matlab", "data.csv"):
        assert name not in masked
    assert "user1" in masked and "cmd1" in masked and "Flagged: 2" in masked


def test_report_on_a_hub_groups_by_site(host, db_path):
    host.start(5001, cores=1.0, age=3600)
    reading(host, db_path)
    with sqlite3.connect(db_path) as c:
        for t in ("per_user_alert", "per_user_daily", "per_user_sample"):
            c.execute(f"ALTER TABLE {t} ADD COLUMN source_site TEXT")
            c.execute(f"UPDATE {t} SET source_site = 'arachne'")
        lines = report(c, days=36500)
    assert any(line.startswith("arachne · ") for line in lines)


def test_report_without_per_user_tables(tmp_path):
    with sqlite3.connect(tmp_path / "x.db") as c:
        assert "never run here" in report(c)[0]

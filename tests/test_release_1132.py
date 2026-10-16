"""nomad 1.13.2: stored measurements are never written over with nothing, and
the lost ones come back from an older copy; workstation sessions and memory."""
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from nomad.collectors.job_metrics import JobMetrics, JobMetricsCollector
from nomad.db.migrations import ensure_database
from nomad.impact import Impact, extract, impact_from, render, report, restore
from nomad.workstation_sessions import split
from tests.test_impact import JOBS, LAUNCH, _db

SUBMIT = datetime(2026, 3, 10, 8, 0, 0)


def _record(job_id="77", cpu=None, rss=None, submit=SUBMIT):
    m = JobMetrics(job_id=job_id, job_name="run", user_name="u1", group_name=None,
                   partition="basic", state="COMPLETED", exit_code=0, submit_time=submit,
                   start_time=submit + timedelta(minutes=5),
                   end_time=submit + timedelta(hours=2), elapsed_seconds=6900,
                   timelimit_seconds=7200, req_cpus=4, req_mem_mb=8192, req_gpus=0,
                   avg_cpu_percent=cpu, max_rss_mb=rss, avg_rss_mb=rss and rss / 2,
                   max_vmsize_mb=None, max_disk_read_mb=None, max_disk_write_mb=None,
                   avg_disk_read_mb=None, avg_disk_write_mb=None)
    return {"type": "job_metrics", **m.to_dict()}


def _summary(db, job_id="77"):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT * FROM job_summary WHERE job_id = ?", (job_id,)).fetchone()
    c.close()
    return dict(row)


# -- the collector keeps what it stored -------------------------------------------------

def test_an_empty_reading_keeps_the_stored_measurement(tmp_path):
    """Slurm purges the steps' usage long before the jobs: an old job read
    again comes back with no CPU and no memory. That used to be stored over
    the measurement (1.13.0's backfill erased months of them)."""
    db = tmp_path / "site.db"
    ensure_database(db)
    col = JobMetricsCollector({}, str(db))
    col.store([_record(cpu=60.0, rss=4096)])
    before = _summary(db)
    assert before["avg_cpu_percent"] == 60.0 and before["peak_memory_gb"] == 4.0
    col.store([_record()])                                    # nothing read
    assert _summary(db) == before
    col.store([_record(cpu=80.0)])                            # CPU, no memory
    after = _summary(db)
    assert after["avg_cpu_percent"] == 80.0 and after["peak_memory_gb"] == 4.0
    assert (after["health_score"], after["feature_vector"]) == (
        before["health_score"], before["feature_vector"])     # scored from the full reading
    col.store([_record(cpu=None, rss=8192)])                  # memory, no CPU
    after = _summary(db)
    assert after["avg_cpu_percent"] == 80.0 and after["peak_memory_gb"] == 8.0


def test_an_empty_reading_of_a_new_job_is_still_stored(tmp_path):
    db = tmp_path / "site.db"
    ensure_database(db)
    JobMetricsCollector({}, str(db)).store([_record(job_id="78")])
    row = _summary(db, "78")
    assert row["avg_cpu_percent"] is None and not row["peak_memory_gb"]


# -- extract and restore ----------------------------------------------------------------

def _site_now(tmp_path):
    """The site as the backfill left it: jobs 1-3 and 7 unmeasured (3 and 7
    are later jobs under numbers given out again; the earlier 3 is kept as
    3@its submit time), job 4 measured the 1.13 way."""
    db = tmp_path / "site.db"
    ensure_database(db)
    c = sqlite3.connect(db)
    for jid, submit, cpu, peak, tasks in (
            ("1", "2026-03-10T08:00:00", None, 0.0, None),
            ("2", "2026-03-11T08:00:00", None, None, None),
            ("3", "2026-09-01T08:00:00", None, 0.0, None),
            ("3@2026-03-01T08:00:00", "2026-03-01T08:00:00", None, 0.0, None),
            ("7", "2026-09-02T08:00:00", None, 0.0, None),
            ("8", "2026-09-03T08:00:00", None, 0.0, None),
            ("9", "2026-09-04T08:00:00", 60.0, 2.0, 1),          # measured: a later job 9
            ("9@2026-03-02T08:00:00", "2026-03-02T08:00:00", None, 0.0, None),
            ("4", "2026-03-12T08:00:00", 90.0, 3.0, 2)):
        c.execute("INSERT INTO jobs (job_id, user_name, submit_time, end_time, state) "
                  "VALUES (?, 'u1', ?, ?, 'COMPLETED')", (jid, submit, submit))
        c.execute("INSERT INTO job_summary (job_id, avg_cpu_percent, peak_memory_gb, max_tasks, "
                  "feature_vector) VALUES (?, ?, ?, ?, '[0.0]')", (jid, cpu, peak, tasks))
    c.commit()
    c.close()
    return db


def _old_combined(tmp_path):
    """The hub's copy from before the backfill: a combined database."""
    db = tmp_path / "old-combined.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE jobs (job_id TEXT, source_site TEXT, submit_time TEXT, user_name TEXT,
            PRIMARY KEY (job_id, source_site));
        CREATE TABLE job_summary (job_id TEXT, source_site TEXT, peak_cpu_percent REAL,
            peak_memory_gb REAL, avg_cpu_percent REAL, avg_memory_gb REAL, avg_gpu_util REAL,
            health_score REAL, feature_vector TEXT, PRIMARY KEY (job_id, source_site));
    """)
    for jid, site, submit, cpu, peak in (
            ("1", "big", "2026-03-10T08:00:00", 41.0, 5.5),
            ("2", "big", "2026-03-11 08:00:00", 12.0, 0.0),          # CPU only; another format
            ("3", "big", "2026-03-01T08:00:00", 33.0, 2.0),          # the earlier job 3
            ("4", "big", "2026-03-12T08:00:00", 20.0, 1.0),          # measured again since
            ("5", "big", "2026-03-13T08:00:00", 50.0, 1.0),          # not on the site now
            ("6", "big", "2026-03-14T08:00:00", None, 0.0),          # never measured
            ("7", "big", "2026-01-02T08:00:00", 70.0, 1.0),          # number given out again
            ("8", "big", None, 80.0, 1.0),                           # no submit time to compare
            ("9", "big", "2026-03-02T08:00:00", 90.0, 1.0),          # kept aside, newer one measured
            ("1", "other", "2026-03-10T08:00:00", 99.0, 99.0)):      # another site's job 1
        c.execute("INSERT INTO jobs VALUES (?, ?, ?, 'u1')", (jid, site, submit))
        c.execute("INSERT INTO job_summary VALUES (?, ?, ?, ?, ?, ?, NULL, 0.7, ?)",
                  (jid, site, cpu, peak, cpu, peak and peak / 2, json.dumps([cpu or 0.0])))
    c.commit()
    c.close()
    return db


def test_extract_then_restore_brings_back_only_what_was_lost(tmp_path):
    site, old = _site_now(tmp_path), _old_combined(tmp_path)
    with pytest.raises(ValueError, match="combined"):
        extract(old, tmp_path / "x.db")                 # whose jobs?
    out = tmp_path / "big-measurements.db"
    assert extract(old, out, "big") == 8                # job 6 was never measured
    with pytest.raises(ValueError, match="exists"):
        extract(old, out, "big")
    names = {r[1] for r in sqlite3.connect(out).execute("PRAGMA table_info(measurements)")}
    assert "user_name" not in names and {"job_id", "submit_time", "feature_vector"} <= names

    dry = restore(str(site), out)
    assert {k: dry[k] for k in ("in_copy", "restored", "measured_now", "other_job",
                                "unsure", "not_here", "copy_site")} == {
        "in_copy": 8, "restored": 4, "measured_now": 1, "other_job": 1, "unsure": 1,
        "not_here": 1, "copy_site": "big"}
    assert _summary(site, "1")["avg_cpu_percent"] is None          # a dry run writes nothing

    c = sqlite3.connect(site)          # a value the copy doesn't have stays
    c.execute("UPDATE job_summary SET avg_gpu_util = 55.0 WHERE job_id = '1'")
    c.commit()
    c.close()
    done = restore(str(site), out, apply=True)
    assert done["restored"] == 4 and _summary(site, "1")["avg_gpu_util"] == 55.0
    assert _summary(site, "9@2026-03-02T08:00:00")["avg_cpu_percent"] == 90.0
    assert _summary(site, "9")["avg_cpu_percent"] == 60.0
    assert _summary(site, "8")["avg_cpu_percent"] is None
    one, two = _summary(site, "1"), _summary(site, "2")
    assert (one["avg_cpu_percent"], one["peak_memory_gb"], one["health_score"]) == (41.0, 5.5, 0.7)
    assert one["feature_vector"] == "[41.0]" and one["max_tasks"] is None     # the old way
    assert two["avg_cpu_percent"] == 12.0
    assert _summary(site, "3")["avg_cpu_percent"] is None           # another job's
    assert _summary(site, "3@2026-03-01T08:00:00")["avg_cpu_percent"] == 33.0
    assert _summary(site, "7")["avg_cpu_percent"] is None
    assert _summary(site, "4")["avg_cpu_percent"] == 90.0           # the 1.13 reading stays
    assert restore(str(site), out, apply=True)["restored"] == 0     # again: nothing to do


def test_restore_from_a_site_copy_directly_and_never_on_the_hub(tmp_path):
    site = _site_now(tmp_path)
    old = tmp_path / "old-site.db"
    c = sqlite3.connect(old)
    c.executescript("""
        CREATE TABLE jobs (job_id TEXT PRIMARY KEY, submit_time TEXT);
        CREATE TABLE job_summary (job_id TEXT PRIMARY KEY, avg_cpu_percent REAL,
            peak_memory_gb REAL);
        INSERT INTO jobs VALUES ('1', '2026-03-10T08:00:00');
        INSERT INTO job_summary VALUES ('1', 41.0, 5.5);
    """)
    c.commit()
    c.close()
    assert restore(str(site), old, apply=True)["restored"] == 1
    with pytest.raises(ValueError, match="hub"):
        restore(str(_old_combined(tmp_path)), old)          # a combined database


def test_the_commands(tmp_path, monkeypatch):
    from nomad.cli import cli
    site, old = _site_now(tmp_path), _old_combined(tmp_path)
    out = tmp_path / "m.db"
    r = CliRunner().invoke(cli, ["impact", "extract", "--from", str(old), "--site", "big",
                                 "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "8 measured jobs written" in r.output
    r = CliRunner().invoke(cli, ["impact", "restore", "--from", str(out), "--db", str(site)])
    assert r.exit_code == 0, r.output
    assert "4 to restore" in r.output and "A dry run" in r.output
    assert "big's jobs" in r.output and "1 without a submit time" in r.output
    r = CliRunner().invoke(cli, ["impact", "restore", "--from", str(out), "--db", str(site),
                                 "--apply"])
    assert r.exit_code == 0, r.output
    assert "4 restored" in r.output and "Copying" in r.output
    copy, = tmp_path.glob("site.before-restore-*.db")
    assert sqlite3.connect(copy).execute(           # the copy is from before
        "SELECT avg_cpu_percent FROM job_summary WHERE job_id = '1'").fetchone() == (None,)
    r = CliRunner().invoke(cli, ["impact", "restore", "--from", str(out), "--db", str(site),
                                 "--apply"])
    assert "0 restored" in r.output and "Copying" not in r.output    # nothing to do: no copy


# -- how much the figures rest on --------------------------------------------------------

def test_the_report_says_how_much_was_measured_and_how(tmp_path):
    db = _db(tmp_path, JOBS)
    imp = Impact(LAUNCH, date(2025, 9, 1))
    rep = report(sqlite3.connect(db), imp, today=date(2026, 11, 15))
    before, after = rep["periods"]["before"], rep["periods"]["after"]
    assert before["measured_pct"] == pytest.approx(66.7, abs=0.1)     # job 3 has nothing
    assert before["old_way_pct"] == 100.0 and after["old_way_pct"] == 0.0
    text = "\n".join(render(rep))
    assert "measured" in text.splitlines()[2]                          # a column
    assert "measured before 1.13" in text


# -- accounts that aren't people ----------------------------------------------------------

def test_exclude_users_is_read_and_checked():
    imp = impact_from({"impact": {"launch": "2026-10-19", "exclude_users": ["svc", " "]}})
    assert imp.exclude_users == frozenset({"svc"}) and imp.problems == ()
    imp = impact_from({"impact": {"launch": "2026-10-19", "exclude_users": "svc"}})
    assert imp.exclude_users == frozenset() and "exclude_users" in imp.problems[0]


def test_excluded_accounts_leave_the_cluster_figures(tmp_path):
    db = _db(tmp_path, JOBS)
    imp = Impact(LAUNCH, date(2025, 9, 1), exclude_users=frozenset({"u1"}))
    rep = report(sqlite3.connect(db), imp, today=date(2026, 11, 15))
    assert rep["periods"]["before"]["finished"] == 1 and rep["periods"]["after"]["finished"] == 1


# -- workstation sessions -----------------------------------------------------------------

def test_a_session_runs_while_the_slice_counters_grow():
    t = [f"2026-10-14T{h:02d}:00:00" for h in range(8)]
    rows = [("a", t[0], 100, 10, 5), ("a", t[1], 101, 20, 5),     # a new terminal: same slice
            ("a", t[2], 102, 30, 6),
            ("a", t[3], 103, 2, 1),                               # counters start again
            ("a", t[4], 103, 4, 1),
            ("b", t[5], 103, 50, 9),                              # another person, or machine
            ("c", t[6], 7, None, None), ("c", t[7], 8, None, None)]   # no counters: the epoch
    assert [len(s) for s in split(rows)] == [3, 2, 1, 1, 1]
    # Hours with no reading: the slice may have gone and come back higher.
    rows = [("a", "2026-10-14T09:55:00", 1, 10, 5), ("a", "2026-10-15T09:05:00", 2, 30, 9)]
    assert [len(s) for s in split(rows)] == [1, 1]


def _snapshots(tmp_path, rows, anon=True):
    db = tmp_path / "ws.db"
    ensure_database(db)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE IF NOT EXISTS workstation_state (timestamp TEXT, hostname TEXT, "
              "memory_total_mb INTEGER, cpu_count INTEGER)")       # the collector's own
    c.execute("INSERT INTO workstation_state (timestamp, hostname, memory_total_mb, cpu_count) "
              "VALUES (?, 'ws1', 65536, 16)", (datetime.now().isoformat(),))
    for ts, epoch, cpu, peak, own in rows:
        c.execute("INSERT INTO workstation_user_snapshot (timestamp, hostname, username, uid, "
                  "session_epoch, cpu_usage_usec, memory_peak_bytes, memory_anon_bytes) "
                  "VALUES (?, 'ws1', 'u1', 1001, ?, ?, ?, ?)",
                  (ts, epoch, cpu, peak, own if anon else None))
    c.commit()
    c.close()
    return db


def test_the_console_sees_one_session_where_logins_changed_the_epoch(tmp_path):
    from nomad.edu.progress import _load_user_sessions
    start = datetime.now() - timedelta(hours=30)
    GB = 2 ** 30
    rows = []
    for i in range(0, 25):                         # 24 h, a new terminal every 2 h
        rows.append(((start + timedelta(hours=i)).isoformat(), 1000 + i // 2,
                     int(i * 3600e6 * 8), 60 * GB, 6 * GB))
    db = _snapshots(tmp_path, rows)
    sessions = _load_user_sessions(str(db), "u1", days=7)
    assert len(sessions) == 1
    s = sessions[0]
    assert s["span_hours"] == pytest.approx(24) and s["samples"] == 25
    assert s["peak_memory_bytes"] == 60 * GB and s["anon_peak_bytes"] == 6 * GB
    assert s["cpu_usage_usec"] - s["cpu_usage_usec_first"] == pytest.approx(24 * 3600e6 * 8)
    from nomad.edu.insights import _user_compute_hours
    assert _user_compute_hours(str(db), "u1") == pytest.approx(24 * 8)    # none lost at logins


def test_impact_counts_pressure_from_the_programs_own_memory(tmp_path):
    from nomad import impact_sources
    GB = 2 ** 30
    day = datetime(2026, 11, 3, 8)
    rows = [((day + timedelta(minutes=5 * i)).isoformat(), 500 + i, i * 10_000_000,
             63 * GB, 3 * GB) for i in range(13)]                  # the cache filled it
    rows += [((day + timedelta(days=1, minutes=5 * i)).isoformat(), 900, i * 1_000_000,
              60 * GB, 58 * GB) for i in range(13)]                # its programs did
    db = _snapshots(tmp_path, rows)
    imp = Impact(LAUNCH, date(2025, 11, 1))
    rep = impact_sources.workstations(sqlite3.connect(db), imp, today=date(2026, 11, 15))[0]
    after = rep["periods"]["after"]
    assert (after["sessions"], after["pressured"], after["memory_read"]) == (2, 1, 2)
    (tmp_path / "old").mkdir()
    old = _snapshots(tmp_path / "old", rows, anon=False)    # readings before 1.13.2
    rep = impact_sources.workstations(sqlite3.connect(old), imp, today=date(2026, 11, 15))[0]
    assert rep["periods"]["after"]["pressured"] is None
    imp = Impact(LAUNCH, date(2025, 11, 1), exclude_users=frozenset({"u1"}))
    assert impact_sources.workstations(sqlite3.connect(db), imp, today=date(2026, 11, 15)) == []


# -- the probe ----------------------------------------------------------------------------

def test_the_probe_reads_the_programs_own_memory(tmp_path):
    from nomad.collectors.cgroup_probe import PROBE_VERSION, probe_users
    s = tmp_path / "user.slice" / "user-1001.slice"
    s.mkdir(parents=True)
    (s / "cpu.stat").write_text("usage_usec 100\nuser_usec 60\nsystem_usec 40\n")
    (s / "memory.current").write_text(str(40 * 2 ** 30))
    (s / "memory.peak").write_text(str(60 * 2 ** 30))
    (s / "memory.stat").write_text(f"anon {2 * 2 ** 30}\nfile {50 * 2 ** 30}\n"
                                   f"shmem {2 ** 30}\nkernel 1000\n")
    (s / "pids.current").write_text("3")
    snap, = probe_users(tmp_path, hostname="ws1", now=1)
    assert snap.memory_anon_bytes == 3 * 2 ** 30 and snap.memory_peak_bytes == 60 * 2 ** 30
    assert PROBE_VERSION == "2"
    (s / "memory.stat").unlink()
    snap, = probe_users(tmp_path, hostname="ws1", now=1)
    assert snap.memory_anon_bytes is None


def test_long_run_advice_asks_generous_memory_without_the_new_reading():
    """Readings from before 1.13.2: --mem from memory.peak (an upper bound),
    never the 1G a missing reading would give."""
    from nomad.edu.insights import _build_compute_verdict
    from tests.test_workstation_compute_verdict import TIERS, fp
    heavy = [fp(14, 60, peak_gb=40, epoch=1)]
    heavy[0].dimensions["memory_pressure"].raw["peak_bytes"] = 0
    heavy[0].dimensions["memory_pressure"].raw["cache_included_peak_bytes"] = 40 * 2 ** 30
    issue = _build_compute_verdict(heavy, TIERS)
    assert "--mem=80G" in json.dumps(issue.context)


# -- interactive servers: the Jupyter server's polling isn't computing ----------------------

def test_the_jupyter_servers_own_polling_does_not_make_a_person_busy():
    from nomad.collectors import interactive_daily as D
    conn = sqlite3.connect(":memory:")
    t0 = datetime(2026, 10, 14, 10, 0)

    def run(sessions, minutes):
        when = t0 + timedelta(minutes=minutes)
        D.update(conn, sessions, when, "web1", epoch=when.timestamp())

    def s(kind, pid, ticks):
        return {"user": "ann", "session_type": kind, "pid": pid, "start_time": "t",
                "mem_mb": 100, "cpu_ticks": ticks, "age_hours": 5.0}
    # 3% of a core, all the time: the server; an idle kernel beside it.
    run([s("Jupyter Server", 1, 0), s("Jupyter (Python)", 2, 0)], 0)
    run([s("Jupyter Server", 1, 900), s("Jupyter (Python)", 2, 0)], 5)
    # Then the kernel computes for five minutes.
    run([s("Jupyter Server", 1, 1800), s("Jupyter (Python)", 2, 30000)], 10)
    busy = dict(conn.execute(
        "SELECT session_type, busy_seconds FROM interactive_daily WHERE user = 'ann'"))
    assert busy["Jupyter Server"] == 600                  # its own row keeps it
    assert busy["(any)"] == 300                           # the person: the kernel's five minutes
    hourly = conn.execute("SELECT busy_seconds FROM interactive_person_hourly").fetchone()[0]
    assert hourly == 300


def test_the_report_computes_from_kernels_and_rstudio_for_the_history_too(tmp_path):
    from nomad import impact_sources
    db = tmp_path / "hub.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE interactive_daily (day TEXT, user TEXT, session_type TEXT, "
              "session_seconds REAL, busy_seconds REAL, cpu_measured_seconds REAL, "
              "source_site TEXT)")
    for user, kind, open_h, busy_h, meas_h in (
            # Before 1.13.2: "(any)" counted the server's polling as busy.
            ("u1", "(any)", 10, 10, 10), ("u1", "Jupyter Server", 10, 10, 10),
            ("u1", "Jupyter (Python)", 20, 1, 20),          # two kernels, open 10 h each
            ("u2", "(any)", 4, 4, 4), ("u2", "RStudio", 4, 1, 4),
            ("svc", "(any)", 24, 24, 24), ("svc", "RStudio", 24, 24, 24)):
        c.execute("INSERT INTO interactive_daily VALUES ('2026-10-14', ?, ?, ?, ?, ?, 'web')",
                  (user, kind, open_h * 3600, busy_h * 3600, meas_h * 3600))
    c.commit()
    imp = Impact(LAUNCH, date(2025, 11, 1), exclude_users=frozenset({"svc"}))
    rep = impact_sources.interactive(sqlite3.connect(db), imp, today=date(2026, 10, 16))[0]
    before = rep["periods"]["before"]
    assert before["people"] == 2 and before["open_hours"] == 14.0
    assert before["computing_pct"] == pytest.approx(100 * 2 / 24, abs=0.1)
    assert before["idle_hours"] == 22.0

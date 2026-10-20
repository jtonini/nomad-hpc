"""nomad 1.14.6: the readiness assessment and a person's own page (My
Activity, nomad edu me) no longer read whole tables on every visit, and the
readiness assessment matches a job to its own site's measurements."""
import random
import sqlite3
from datetime import datetime, timedelta

import pytest

from nomad.ml import estimator
from nomad.ml.estimator import _when, assess_readiness


def _old_when(value):
    """How a job's time was read until 1.14.6."""
    if not isinstance(value, str):
        return value
    for fmt in ['%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S']:
        try:
            return datetime.strptime(value[:26], fmt)
        except ValueError:
            continue
    return None


def test_times_are_read_as_before():
    rnd = random.Random(3)
    base = datetime(2026, 8, 13, 11, 1, 8, 123456)
    values = [None, "", "Unknown", "None", "2026-08-13", "2026-8-13T1:2:3", "2026-02-30T00:00:00",
              "2026-08-13T11:01:60", "2026-08-13T24:00:00", "2026-08-13T11:01:08Z",
              "2026-08-13T11:01:08+00:00", "2026-08-13 11:01:08.5", "2026-08-13T11:01:08.",
              "2026-08-13T11:01:08.1234567", "2026-08-13T11:01:08.123456789", " 2026-08-13T11:01:08",
              "2026-08-13T11:01:08 ", "２０２６-08-13T11:01:08", "2024-0\u0661-31T12:34:56.123456",
              "2024-01-31T12:34:5\u0666", base, 0]
    for _ in range(3000):
        dt = base - timedelta(seconds=rnd.randint(0, 10 ** 8), microseconds=rnd.randint(0, 999999))
        sep = rnd.choice("T ")
        spec = rnd.choice(["seconds", "milliseconds", "microseconds"])
        values.append(dt.isoformat(sep=sep, timespec=spec))
        values.append(dt.isoformat(timespec="seconds")[:rnd.randint(1, 19)])
    for v in values:
        assert _when(v) == _old_when(v), repr(v)


def _jobs_db(path, by_site):
    c = sqlite3.connect(path)
    site = ", source_site TEXT" if by_site else ""
    c.execute(f"CREATE TABLE jobs (job_id TEXT, state TEXT, submit_time TEXT, end_time TEXT, "
              f"req_cpus INTEGER, exit_code INTEGER, other TEXT{site})")
    c.execute(f"CREATE TABLE job_summary (job_id TEXT, avg_cpu_percent REAL, exit_code INTEGER, "
              f"notes TEXT{site})")
    return c


def test_a_job_is_matched_to_its_own_sites_measurements(tmp_path):
    db = tmp_path / "combined.db"
    c = _jobs_db(db, by_site=True)
    end = datetime.now().isoformat(timespec="seconds")
    c.executemany("INSERT INTO jobs VALUES (?, 'COMPLETED', ?, ?, 4, 0, 'x', ?)",
                  [("7", end, end, "site1"), ("7", end, end, "site2"), ("8", end, end, "site1")])
    # Only site2's job 7 was measured; site1's job 7 is another job.
    c.execute("INSERT INTO job_summary VALUES ('7', 55.0, 0, 'n', 'site2')")
    c.commit()
    cov = assess_readiness(str(db)).feature_coverage["avg_cpu_percent"]
    assert cov["n_present"] == 1                   # until 1.14.6: 2


def test_a_measurement_takes_the_place_of_the_jobs_own(tmp_path):
    db = tmp_path / "site.db"
    c = _jobs_db(db, by_site=False)
    end = datetime.now().isoformat(timespec="seconds")
    c.executemany("INSERT INTO jobs VALUES (?, 'COMPLETED', ?, ?, 4, ?, 'x')",
                  [("1", end, end, 1), ("2", end, end, 1), ("3", end, end, 1)])
    # Job 1's measurement says exit_code is unknown; job 2's is stored as a
    # number (a TEXT column keeps it as '2', as the jobs table does).
    c.execute("INSERT INTO job_summary VALUES ('1', 50.0, NULL, 'n')")
    c.execute("INSERT INTO job_summary VALUES (2, 60.0, 0, 'n')")
    c.commit()
    jobs = {j["job_id"]: j for j in estimator._load_jobs(str(db))}
    assert jobs["1"]["exit_code"] is None and jobs["1"]["avg_cpu_percent"] == 50.0
    assert jobs["2"]["exit_code"] == 0 and jobs["2"]["avg_cpu_percent"] == 60.0
    assert jobs["3"]["exit_code"] == 1 and "avg_cpu_percent" not in jobs["3"]
    # Only what an assessment reads is read.
    assert "other" not in jobs["1"] and "notes" not in jobs["1"]


def test_every_job_counts_even_without_the_columns_read(tmp_path):
    db = tmp_path / "odd.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (id TEXT, outcome TEXT)")
    c.executemany("INSERT INTO jobs VALUES (?, 'x')", [("1",), ("2",), ("3",)])
    c.execute("CREATE TABLE job_summary (id TEXT)")
    c.commit()
    assert assess_readiness(str(db)).total_jobs == 3


def test_readiness_without_tables(tmp_path):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).execute("CREATE TABLE other (x)")
    assert assess_readiness(str(db)).total_jobs == 0
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (job_id TEXT, state TEXT)")
    c.execute("INSERT INTO jobs VALUES ('1', 'FAILED')")
    c.commit()
    r = assess_readiness(str(db))
    assert (r.total_jobs, r.failure_jobs) == (1, 1)


def test_the_readiness_jobs_are_read_in_two_queries(tmp_path, monkeypatch):
    db = tmp_path / "site.db"
    c = _jobs_db(db, by_site=False)
    end = datetime.now().isoformat(timespec="seconds")
    c.executemany("INSERT INTO jobs VALUES (?, 'COMPLETED', ?, ?, 4, 0, 'x')",
                  [(str(i), end, end) for i in range(500)])
    c.executemany("INSERT INTO job_summary VALUES (?, 50.0, 0, 'n')", [(str(i),) for i in range(500)])
    c.commit()
    asked = []
    real = sqlite3.connect

    def traced(*a, **k):
        con = real(*a, **k)
        con.set_trace_callback(lambda sql: asked.append(sql) if sql.lstrip().upper().startswith("SELECT")
                               else None)
        return con
    monkeypatch.setattr(estimator.sqlite3, "connect", traced)
    assess_readiness(str(db))
    assert len([s for s in asked if "FROM jobs" in s or "FROM job_summary" in s]) == 2


# ── My Activity: the machines a person used ─────────────────────────────

def _sessions_db(path):
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE workstation_user_snapshot (timestamp TEXT, hostname TEXT, username TEXT,
            uid INTEGER, session_epoch INTEGER, cpu_usage_usec INTEGER,
            memory_peak_bytes INTEGER, memory_anon_bytes INTEGER, source_site TEXT);
        CREATE TABLE workstation_state (timestamp TEXT, hostname TEXT, memory_total_mb INTEGER,
            cpu_count INTEGER, source_site TEXT);
        CREATE INDEX ws_host_ts ON workstation_state (hostname, timestamp);
    """)
    return c


def test_a_session_gets_its_machines_latest_size(tmp_path, monkeypatch):
    from nomad.edu.progress import _load_user_sessions
    db = tmp_path / "hub.db"
    c = _sessions_db(db)
    now = datetime.now()
    # ws1 was upgraded yesterday: its latest record is the one that counts.
    c.executemany("INSERT INTO workstation_state VALUES (?, ?, ?, ?, 'labs')",
                  [((now - timedelta(days=3)).isoformat(), "ws1", 32768, 8),
                   ((now - timedelta(days=1)).isoformat(), "ws1", 65536, 16),
                   ((now - timedelta(days=2)).isoformat(), "ws2", 16384, 4)] +
                  [((now - timedelta(minutes=i)).isoformat(), f"other{i % 50}", 1024, 2)
                   for i in range(2000)])
    for host in ("ws1", "ws2"):
        for h in range(3):
            c.execute("INSERT INTO workstation_user_snapshot VALUES (?, ?, 'u1', 1001, 1, ?, ?, ?, 'labs')",
                      ((now - timedelta(hours=3 - h)).isoformat(), host, h * 10 ** 9, 2 ** 30, 2 ** 29))
    c.commit()
    c.close()
    asked = []
    real = sqlite3.connect

    def traced(*a, **k):
        con = real(*a, **k)
        con.set_trace_callback(asked.append)
        return con
    import nomad.edu.progress as P
    monkeypatch.setattr(P.sqlite3, "connect", traced)
    sessions = {s["hostname"]: s for s in _load_user_sessions(str(db), "u1", days=7)}
    assert (sessions["ws1"]["host_memory_total_mb"], sessions["ws1"]["host_cpu_count"]) == (65536, 16)
    assert (sessions["ws2"]["host_memory_total_mb"], sessions["ws2"]["host_cpu_count"]) == (16384, 4)
    # Only the person's machines were looked up, each by its latest record.
    lookups = [s for s in asked if "FROM workstation_state" in s]
    assert len(lookups) == 2 and all("LIMIT 1" in s for s in lookups)
    plan = real(db).execute(
        "EXPLAIN QUERY PLAN SELECT memory_total_mb, cpu_count FROM workstation_state "
        "WHERE hostname = ? ORDER BY timestamp DESC LIMIT 1", ("ws1",)).fetchall()
    assert "USING INDEX ws_host_ts" in str(plan) and "TEMP B-TREE" not in str(plan)


def test_a_machine_with_no_record_has_no_size(tmp_path):
    from nomad.edu.progress import _load_user_sessions
    db = tmp_path / "hub.db"
    c = _sessions_db(db)
    now = datetime.now()
    for h in range(3):
        c.execute("INSERT INTO workstation_user_snapshot VALUES (?, 'ws9', 'u1', 1001, 1, ?, ?, ?, 'labs')",
                  ((now - timedelta(hours=3 - h)).isoformat(), h * 10 ** 9, 2 ** 30, 2 ** 29))
    c.commit()
    s, = _load_user_sessions(str(db), "u1", days=7)
    assert s["host_memory_total_mb"] is None and s["host_cpu_count"] is None and s["long_runs"] is None


def test_the_hub_indexes_a_persons_sessions():
    import inspect
    from nomad import cli
    src = inspect.getsource(cli)
    assert '("workstation_user_snapshot", "username, timestamp")' in src


def test_that_index_serves_the_sessions_query(tmp_path):
    db = tmp_path / "hub.db"
    c = _sessions_db(db)
    c.execute("CREATE INDEX idx_workstation_user_snapshot_username_timestamp "
              "ON workstation_user_snapshot(username, timestamp)")
    plan = str(c.execute(
        "EXPLAIN QUERY PLAN SELECT hostname FROM workstation_user_snapshot "
        "WHERE timestamp >= ? AND uid >= 1000 AND username = ? ORDER BY hostname, uid, timestamp",
        ("2026-10-12", "u1")).fetchall())
    assert "USING INDEX idx_workstation_user_snapshot_username_timestamp" in plan

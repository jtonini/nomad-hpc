"""nomad impact beyond the clusters (1.13.1): interactive servers, lab
workstations, shared login hosts, and advice shown and taken."""
import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad import impact_advice, impact_sources
from nomad.impact import Impact

LAUNCH = date(2026, 10, 19)
TODAY = date(2026, 12, 31)
IMP = Impact(LAUNCH, date(2026, 4, 1))

# The Console's tables (its app/models/advice.py).
ADVICE_SCHEMA = """
CREATE TABLE pages_viewed (day TEXT NOT NULL, username TEXT NOT NULL, page TEXT NOT NULL,
    first_at TEXT NOT NULL, last_at TEXT NOT NULL, times INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (day, username, page));
CREATE TABLE advice_shown (day TEXT NOT NULL, username TEXT NOT NULL, page TEXT NOT NULL,
    site TEXT NOT NULL DEFAULT '', partition TEXT NOT NULL DEFAULT '', dimension TEXT NOT NULL,
    directive TEXT NOT NULL DEFAULT '', current_value REAL, suggested_value REAL,
    job_id TEXT NOT NULL DEFAULT '', first_at TEXT NOT NULL, last_at TEXT NOT NULL,
    times INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (day, username, page, site, partition, dimension, directive, job_id));
"""


def iso(t):
    return t.isoformat(timespec="seconds")


@pytest.fixture
def hub(tmp_path):
    db = tmp_path / "combined.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE interactive_daily (day TEXT, server_id TEXT, user TEXT, session_type TEXT,
            sessions INTEGER, session_seconds REAL, busy_seconds REAL, cpu_measured_seconds REAL,
            cpu_seconds REAL, mem_mb_seconds REAL, peak_mem_mb REAL, updated_at TEXT,
            rebuilt INTEGER DEFAULT 0, source_site TEXT);
        CREATE TABLE workstation_user_snapshot (id INTEGER PRIMARY KEY, timestamp TEXT,
            hostname TEXT, username TEXT, uid INTEGER, session_epoch INTEGER,
            cpu_usage_usec INTEGER, memory_peak_bytes INTEGER, source_site TEXT,
            memory_anon_bytes INTEGER);
        CREATE TABLE workstation_state (id INTEGER PRIMARY KEY, timestamp TEXT, hostname TEXT,
            memory_total_mb INTEGER, cpu_count INTEGER, source_site TEXT);
        CREATE TABLE per_user_alert (id INTEGER PRIMARY KEY, fired_at TEXT, last_seen TEXT,
            hostname TEXT, username TEXT, process_session_id TEXT, command TEXT, rule_type TEXT,
            threshold_value REAL, severity TEXT, sustained_for_seconds INTEGER,
            peak_cpu_percent REAL, peak_memory_bytes INTEGER, source_site TEXT);
        CREATE TABLE per_user_daily (day TEXT, hostname TEXT, username TEXT, cpu_seconds REAL,
            busy_seconds REAL, source_site TEXT);
        CREATE TABLE jobs (job_id TEXT, user_name TEXT, partition TEXT, state TEXT,
            end_time TEXT, req_mem_mb INTEGER, req_time_seconds INTEGER, req_cpus INTEGER,
            req_gpus INTEGER, source_site TEXT);
    """)
    # Interactive: September rebuilt (no CPU), November measured.
    for day, user, open_h, busy_h, meas_h, rebuilt in (
            ("2026-09-10", "s1", 10, 0, 0, 1), ("2026-09-11", "s2", 6, 0, 0, 1),
            ("2026-11-10", "s1", 8, 2, 8, 0), ("2026-11-11", "s3", 4, 2, 4, 0)):
        c.execute("INSERT INTO interactive_daily (day, server_id, user, session_type, sessions, "
                  "session_seconds, busy_seconds, cpu_measured_seconds, rebuilt, source_site) "
                  "VALUES (?, 'local', ?, '(any)', 1, ?, ?, ?, ?, 'teach')",
                  (day, user, open_h * 3600, open_h * 3600, meas_h * 3600, rebuilt))
        # Computing comes from the sessions themselves; the Jupyter server's
        # own polling, busy all the time (and in "(any)" before 1.13.2), isn't.
        for kind, busy in (("Jupyter (Python)", busy_h), ("Jupyter Server", meas_h)):
            c.execute("INSERT INTO interactive_daily (day, server_id, user, session_type, "
                      "sessions, session_seconds, busy_seconds, cpu_measured_seconds, rebuilt, "
                      "source_site) VALUES (?, 'local', ?, ?, 1, ?, ?, ?, ?, 'teach')",
                      (day, user, kind, open_h * 3600, busy * 3600, meas_h * 3600, rebuilt))
    c.execute("INSERT INTO interactive_daily (day, server_id, user, session_type, sessions, "
              "session_seconds, busy_seconds, cpu_measured_seconds, source_site) "
              "VALUES ('2026-11-10', 'local', 's1', 'jupyter', 3, 99999, 0, 0, 'teach')")
    # Workstations: a 16-core, 64 GB machine. w1 ran a 20 h run on 12 cores in
    # September (a long run); in November two 2 h light sessions.
    c.execute("INSERT INTO workstation_state (timestamp, hostname, memory_total_mb, cpu_count, "
              "source_site) VALUES ('2026-11-01T00:00:00', 'ws1', 65536, 16, 'labs')")

    def session(user, epoch, start, hours, cores, peak_gb, anon_gb=None, uid=1001):
        for i in range(int(hours * 12) + 1):
            t = start + timedelta(minutes=5 * i)
            c.execute("INSERT INTO workstation_user_snapshot (timestamp, hostname, username, uid, "
                      "session_epoch, cpu_usage_usec, memory_peak_bytes, source_site, "
                      "memory_anon_bytes) VALUES (?, 'ws1', ?, ?, ?, ?, ?, 'labs', ?)",
                      (iso(t), user, uid, epoch, int(cores * 300e6 * i), int(peak_gb * 2 ** 30),
                       None if anon_gb is None else int(anon_gb * 2 ** 30)))
    # Before 1.13.2: no reading of the programs' own memory.
    session("w1", 1, datetime(2026, 9, 15, 8), 20, 12, 10)
    session("w1", 2, datetime(2026, 11, 15, 8), 2, 1, 60, 60)    # held 60 of 64 GB
    session("w1", 3, datetime(2026, 11, 16, 8), 2, 1, 4, 4)
    c.execute("INSERT INTO workstation_user_snapshot (timestamp, hostname, username, uid, "
              "session_epoch, cpu_usage_usec, memory_peak_bytes, source_site) "
              "VALUES ('2026-11-16T09:00:00', 'ws1', 'root', 0, 9, 1, 1, 'labs')")
    # Login hosts: two flagged episodes in September, one in November.
    for i, (last, user, sev) in enumerate((("2026-09-05 14:00:00", "u1", "actionable"),
                                           ("2026-09-20 14:00:00", "u2", "info"),
                                           ("2026-11-03 14:00:00", "u1", "actionable"))):
        c.execute("INSERT INTO per_user_alert (fired_at, last_seen, hostname, username, "
                  "process_session_id, command, rule_type, threshold_value, severity, "
                  "sustained_for_seconds, peak_cpu_percent, peak_memory_bytes, source_site) "
                  "VALUES (?, ?, 'login1', ?, ?, 'python', 'cpu', 400, ?, 600, 800, 0, 'clusterA')",
                  (last, last, user, f"p{i}", sev))
    for day, user, cpu_h in (("2026-09-05", "u1", 30), ("2026-11-03", "u1", 3),
                             ("2026-11-04", "u3", 1)):
        c.execute("INSERT INTO per_user_daily VALUES (?, 'login1', ?, ?, 0, 'clusterA')",
                  (day, user, cpu_h * 3600))
    # Jobs for the advice: s1 asked 16G on clusterA/std before seeing the
    # advice (1G) on 1 Nov, 2G after; s2 kept asking 8G.
    for i, (user, end, mem) in enumerate(
            [("s1", f"2026-10-{d:02d}T12:00:00", 16384) for d in (10, 12, 14)]
            + [("s1", f"2026-11-{d:02d}T12:00:00", 2048) for d in (5, 7, 9, 11)]
            + [("s2", f"2026-10-{d:02d}T12:00:00", 8192) for d in (10, 12)]
            + [("s2", f"2026-11-{d:02d}T12:00:00", 8192) for d in (5, 7, 9)]
            + [("w1", "2026-11-20T12:00:00", 4096), ("w1", "2026-11-21T12:00:00", 4096)]):
        c.execute("INSERT INTO jobs VALUES (?, ?, 'std', 'COMPLETED', ?, ?, 3600, 4, 0, "
                  "'clusterA')", (str(i), user, end, mem))
    c.commit()
    c.close()

    adv = tmp_path / "activity.db"
    a = sqlite3.connect(adv)
    a.executescript(ADVICE_SCHEMA)
    for day, user, dim, directive, cur, sug, site in (
            ("2026-11-01", "s1", "memory", "mem", 16384, 1024, "clusterA"),
            ("2026-11-02", "s1", "memory", "mem", 16384, 1024, "clusterA"),   # again: counted once
            ("2026-11-01", "s2", "memory", "mem", 8192, 2048, "clusterA"),
            ("2026-12-28", "s3", "time", "time", 7200, 1800, "clusterA"),     # too early
            ("2026-10-20", "w1", "verdict:promote", "compute", None, None, "clusterA")):
        a.execute("INSERT INTO advice_shown (day, username, page, site, partition, dimension, "
                  "directive, current_value, suggested_value, first_at, last_at) "
                  "VALUES (?, ?, 'my_activity', ?, ?, ?, ?, ?, ?, ?, ?)",
                  (day, user, site, "" if dim.startswith("verdict") else "std", dim, directive,
                   cur, sug, day + "T09:00:00", day + "T09:00:00"))
        a.execute("INSERT OR IGNORE INTO pages_viewed VALUES (?, ?, 'my_activity', ?, ?, 1)",
                  (day, user, day + "T09:00:00", day + "T09:00:00"))
    a.commit()
    a.close()
    return db, adv


def test_interactive_hours_and_computing(hub):
    db, _ = hub
    rep = impact_sources.interactive(sqlite3.connect(db), IMP, today=TODAY)[0]
    assert rep["site"] == "teach" and rep["data_from"] == "2026-09-10"
    assert rep["computing_from"] == "2026-11-10"
    by = {m["month"]: m for m in rep["months"]}
    assert by["2026-09"]["open_hours"] == 16.0 and by["2026-09"]["computing_pct"] is None
    nov = by["2026-11"]
    assert (nov["people"], nov["open_hours"], nov["computing_pct"], nov["idle_hours"]) == \
        (2, 12.0, 33.3, 8.0)                    # the jupyter row (not "(any)") is left out
    assert rep["periods"]["after"]["hours_per_person"] == 6.0


def test_workstation_long_runs_and_memory(hub):
    db, _ = hub
    rep = impact_sources.workstations(sqlite3.connect(db), IMP, today=TODAY)[0]
    assert rep["site"] == "labs" and rep["machines"] == 1
    before, after = rep["periods"]["before"], rep["periods"]["after"]
    assert (before["sessions"], before["long_runs"], before["long_run_hours"]) == (1, 1, 20.0)
    assert before["core_hours"] == pytest.approx(240, abs=1)
    assert (after["sessions"], after["long_runs"], after["pressured"], after["people"]) == (2, 0, 1, 1)
    assert before["pressured"] is None and after["memory_read"] == 2    # read from 1.13.2


def test_login_host_episodes(hub):
    db, _ = hub
    rep = impact_sources.login_hosts(sqlite3.connect(db), IMP, today=TODAY)[0]
    before, after = rep["periods"]["before"], rep["periods"]["after"]
    assert (before["episodes"], before["actionable"], before["people_flagged"]) == (2, 1, 2)
    assert (after["episodes"], after["people"], after["core_hours"]) == (1, 2, 4.0)


def test_advice_shown_and_taken(hub):
    db, adv = hub
    rep = impact_advice.report(sqlite3.connect(db), IMP, today=TODAY, path=adv)
    assert rep["available"] and rep["recorded_since"] == "2026-10-20"
    mem = rep["cluster"]["memory"]
    # s1: 16G -> 2G against 1G: took it; s2: 8G -> 8G: no change.
    assert (mem["shown"], mem["people"], mem["took"], mem["no_change"]) == (2, 2, 1, 1)
    assert rep["cluster"]["time"]["too_early"] == 1
    # w1: a 20 h run before, none after, and cluster jobs after: moved.
    assert rep["where_to_run"] == {"shown": 1, "people": 1, "moved": 1, "fewer_long_runs": 0,
                                   "no_change": 0, "too_early": 0}
    text = "\n".join(impact_advice.render(rep))
    assert "comparison" in text and "s1" not in text and "w1" not in text


def test_without_the_consoles_record(hub, tmp_path):
    db, _ = hub
    rep = impact_advice.report(sqlite3.connect(db), IMP, today=TODAY, path=tmp_path / "none.db")
    assert not rep["available"] and "advice_db" in rep["reason"]


def test_the_report_has_every_part(hub, tmp_path):
    from nomad.cli import cli
    db, adv = hub
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f'[impact]\nlaunch = 2026-10-19\nbaseline_from = 2026-04-01\n'
                   f'advice_db = "{adv}"\n'
                   'events = [ { date = 2026-09-01, what = "JupyterHub culler on" } ]\n')
    r = CliRunner().invoke(cli, ["-c", str(cfg), "impact", "report", "--db", str(db)])
    assert r.exit_code == 0, r.output
    for title in ("NØMAÐ impact, clusterA", "Interactive servers", "Lab workstations",
                  "Shared login hosts", "Advice shown and taken", "JupyterHub culler on"):
        assert title in r.output, title
    for name in ("s1", "s2", "s3", "w1", "u1", "u2", "u3"):
        assert f" {name} " not in r.output and f" {name}\n" not in r.output
    r = CliRunner().invoke(cli, ["-c", str(cfg), "impact", "report", "--db", str(db),
                                 "--source", "interactive", "--json"])
    d = json.loads(r.output)
    assert set(d) == {"launch", "baseline_from", "events", "interactive"}
    r = CliRunner().invoke(cli, ["-c", str(cfg), "impact", "report", "--db", str(db),
                                 "--site", "teach"])
    assert r.exit_code == 0 and "Interactive servers" in r.output and "clusterA" not in r.output
    r = CliRunner().invoke(cli, ["-c", str(cfg), "impact", "status", "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "interactive servers" in r.output and "teach: from 2026-09-10" in r.output
    assert str(adv) in r.output


def test_status_and_report_on_a_database_without_jobs(tmp_path):
    from nomad.cli import cli
    db = tmp_path / "teach.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE jobs (job_id TEXT, user_name TEXT, end_time TEXT)")
    c.execute("CREATE TABLE interactive_daily (day TEXT, server_id TEXT, user TEXT, "
              "session_type TEXT, session_seconds REAL, busy_seconds REAL, "
              "cpu_measured_seconds REAL)")
    c.execute("INSERT INTO interactive_daily VALUES ('2026-11-10', 'local', 's1', '(any)', "
              "3600, 600, 3600)")
    c.commit()
    c.close()
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[impact]\nlaunch = 2026-10-19\n')
    r = CliRunner().invoke(cli, ["-c", str(cfg), "impact", "report", "--db", str(db),
                                 "--advice-db", str(tmp_path / "x.db")])
    assert r.exit_code == 0, r.output
    assert "Interactive servers" in r.output and "NØMAÐ impact, this site" not in r.output

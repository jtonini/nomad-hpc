"""[impact] and nomad impact: the launch date, the periods, what to backfill."""
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nomad.db.jobkeys import ensure_job_columns
from nomad.db.migrations import ensure_database
from nomad.impact import Impact, coverage, impact_from, render, report

LAUNCH = date(2026, 10, 19)


def test_the_section_as_written():
    imp = impact_from({"impact": {
        "launch": date(2026, 10, 19), "baseline_from": "2025-08-24",
        "events": [{"date": date(2027, 1, 11), "what": "nodes added"},
                   {"date": datetime(2026, 10, 6, 9), "what": "Console open to staff"}]}})
    assert imp.set and imp.launch == LAUNCH and imp.baseline_from == date(2025, 8, 24)
    assert [e.what for e in imp.events] == ["Console open to staff", "nodes added"]
    assert imp.problems == ()


def test_what_is_wrong_is_said_and_left_out():
    imp = impact_from({"impact": {"launch": "soon", "baseline_from": "2027-01-01",
                                  "events": [{"what": "no date"}], "lunch": 1}})
    assert not imp.set
    text = " ".join(imp.problems)
    assert "launch is not a date" in text and "entry 1 needs a date" in text
    assert "lunch: not a setting" in text
    imp = impact_from({"impact": {"launch": "2026-10-19", "baseline_from": "2026-11-01"}})
    assert imp.baseline_from == LAUNCH - timedelta(days=365)
    assert "not before launch" in imp.problems[0]
    assert impact_from({}).set is False and impact_from({}).problems == ()
    assert "launch is not set" in impact_from({"impact": {}}).problems[0]


def _db(tmp_path, jobs):
    """jobs: (id, user, end, cpu %, peak GB, measured the 1.13 way, eligible?)"""
    db = tmp_path / "site.db"
    ensure_database(Path(db))
    c = sqlite3.connect(db)
    ensure_job_columns(c)
    for jid, user, end, cpu, peak, new_way, eligible in jobs:
        e = datetime.fromisoformat(end)
        start, submit = e - timedelta(hours=1), e - timedelta(hours=2)
        c.execute("INSERT INTO jobs (job_id, user_name, partition, state, submit_time, start_time, "
                  "end_time, req_cpus, req_mem_mb, req_time_seconds, runtime_seconds, "
                  "wait_time_seconds, eligible_time) VALUES (?, ?, 'basic', 'COMPLETED', ?, ?, ?, "
                  "4, 8192, 7200, 3600, 3600, ?)",
                  (jid, user, submit.isoformat(), start.isoformat(), e.isoformat(),
                   (start - timedelta(minutes=10)).isoformat() if eligible else None))
        if cpu is not None:
            c.execute("INSERT INTO job_summary (job_id, avg_cpu_percent, peak_memory_gb, max_tasks) "
                      "VALUES (?, ?, ?, ?)", (jid, cpu, peak, 1 if new_way else None))
    c.commit()
    c.close()
    return db


JOBS = [
    ("1", "u1", "2025-09-10T10:00:00", 25.0, 2.0, False, False),
    ("2", "u2", "2025-09-12T10:00:00", 25.0, 2.0, False, False),
    ("3", "u1", "2026-03-10T10:00:00", None, None, False, False),
    ("4", "u3", "2026-10-25T10:00:00", 75.0, 6.0, True, True),
    ("5", "u1", "2026-11-02T10:00:00", 75.0, 6.0, True, True),
]


def test_coverage_says_what_each_period_has(tmp_path):
    db = _db(tmp_path, JOBS)
    imp = Impact(LAUNCH, date(2025, 9, 1))
    cov = coverage(sqlite3.connect(db), imp, today=date(2026, 11, 15))
    assert cov["first_job_end"] == "2025-09-10"
    assert (cov["before"]["jobs"], cov["before"]["measured"], cov["before"]["measured_113"]) == (3, 2, 0)
    assert (cov["after"]["jobs"], cov["after"]["measured_113"], cov["after"]["eligible"]) == (2, 2, 2)


def test_the_report_compares_like_with_like(tmp_path):
    db = _db(tmp_path, JOBS)
    imp = Impact(LAUNCH, date(2025, 9, 1), events=())
    rep = report(sqlite3.connect(db), imp, today=date(2026, 11, 15))
    by = {m["month"]: m for m in rep["months"]}
    assert by["2025-09"]["phase"] == "before" and by["2026-10"]["phase"] == "launch"
    assert by["2026-11"]["phase"] == "after"
    assert by["2025-09"]["new_people"] is None            # the data starts that month
    assert by["2026-10"]["new_people"] == 1               # u3
    assert by["2025-09"]["core_hours"]["used_pct"] == 25.0
    assert rep["periods"]["after"]["core_hours"]["used_pct"] == 75.0
    assert rep["periods"]["after"]["time_used_pct"] == 50.0
    assert rep["periods"]["after"]["wait_minutes"]["median"] == 10.0     # from eligible
    # The data reaches back to the same days a year earlier (no jobs then).
    assert rep["periods"]["year_earlier"]["from"] == "2025-10-19"
    assert rep["periods"]["year_earlier"]["finished"] == 0
    assert rep["periods"]["before"]["to"] == "2026-10-18"     # the last day before launch
    text = "\n".join(render(rep))
    assert "not why" in text and "2026-11  after" in text
    # Waits before launch count from submission (no eligible times yet): said.
    assert "aren't comparable yet: 0% of the jobs before launch" in text


def _cli(tmp_path, db, *args, toml=None):
    from nomad.cli import cli
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(toml if toml is not None else
                   '[impact]\nlaunch = 2026-10-19\nbaseline_from = 2025-09-01\n'
                   'events = [ { date = 2026-10-06, what = "Console open to staff" } ]\n')
    return CliRunner().invoke(cli, ["-c", str(cfg), "impact", *args, "--db", str(db)])


def test_status_says_what_to_run(tmp_path):
    db = _db(tmp_path, JOBS)
    r = _cli(tmp_path, db, "status")
    assert r.exit_code == 0, r.output
    assert "Launch 2026-10-19" in r.output and "Console open to staff" in r.output
    assert "nomad impact backfill" in r.output
    r = _cli(tmp_path, db, "status", toml="")
    assert "launch is not set" in r.output


def test_report_as_json(tmp_path):
    db = _db(tmp_path, JOBS)
    r = _cli(tmp_path, db, "report", "--json")
    assert r.exit_code == 0, r.output
    rep = json.loads(r.output)
    assert rep["launch"] == "2026-10-19" and rep["events"][0]["what"] == "Console open to staff"
    assert "u1" not in r.output and "u2" not in r.output     # counts only


def test_backfill_counts_then_reads_the_old_way_measured_again(tmp_path, monkeypatch):
    from nomad.collectors import job_metrics as JM
    db = _db(tmp_path, JOBS)
    r = _cli(tmp_path, db, "backfill")
    assert r.exit_code == 0, r.output
    assert "3 jobs here, 3 to read again" in r.output and "dry run" in r.output

    line = "|".join(["1", "run", "u1", "people", "basic", "COMPLETED", "0:0",
                     "2025-09-10T08:00:00", "2025-09-10T09:00:00", "2025-09-10T10:00:00",
                     "01:00:00", "02:00:00", "4", "8G", "cpu=4", "", "", "", "", "", "", "", "",
                     "cn01", "03:00:00", "", "sbatch -c 4 run.sh"])
    step = "|".join(["1.batch", "batch", "", "", "", "COMPLETED", "0:0", "", "", "", "01:00:00",
                     "", "4", "", "", "01:00:00", "3G", "", "", "", "", "", "", "cn01", "",
                     "1", ""])
    asked = []

    def fake(argv, **kw):
        asked.append(argv)
        a = argv[argv.index("--starttime") + 1]
        out = "\n".join([line, step]) if a.startswith("2025-09-08") else ""
        return SimpleNamespace(returncode=0, stdout=out + "\n", stderr="")
    monkeypatch.setattr(JM.subprocess, "run", fake)
    r = _cli(tmp_path, db, "backfill", "--apply", "--from", "2025-09-01", "--to", "2025-09-22")
    assert r.exit_code == 0, r.output
    assert len(asked) == 3                                 # three weeks
    row = sqlite3.connect(db).execute("SELECT avg_cpu_percent, max_tasks, cpu_request "
                                      "FROM job_summary WHERE job_id = '1'").fetchone()
    assert row == (75.0, 1, "cpus-per-task")               # TotalCPU 3 h of 4 cores × 1 h


def test_backfill_refuses_the_hubs_database(tmp_path):
    db = _db(tmp_path, JOBS)
    c = sqlite3.connect(db)
    c.execute("ALTER TABLE jobs ADD COLUMN source_site TEXT")
    c.commit()
    c.close()
    r = _cli(tmp_path, db, "backfill", "--apply")
    assert r.exit_code != 0 and "combined database" in r.output


def test_config_check_names_impact_problems(tmp_path):
    from nomad.cli import cli
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[impact]\nlaunch = "soon"\n')
    r = CliRunner().invoke(cli, ["-c", str(cfg), "config", "check"])
    assert "launch is not a date" in r.output


def test_a_launch_still_to_come_has_no_after(tmp_path):
    db = _db(tmp_path, JOBS[:2])
    imp = Impact(LAUNCH, date(2025, 9, 1))
    conn = sqlite3.connect(db)
    cov = coverage(conn, imp, today=date(2026, 10, 15))
    assert "after" not in cov and cov["before"]["to"] == "2026-10-15"
    rep = report(conn, imp, today=date(2026, 10, 15))
    assert set(rep["periods"]) == {"before"}
    assert "no after yet" in "\n".join(render(rep))


def test_on_the_hub_status_reads_the_combined_database(tmp_path):
    from nomad.cli import cli
    hub = _db(tmp_path, JOBS)
    c = sqlite3.connect(hub)
    c.execute("ALTER TABLE jobs ADD COLUMN source_site TEXT")
    c.execute("UPDATE jobs SET source_site = 'clusterA'")
    c.commit()
    c.close()
    cfg = tmp_path / "hub.toml"
    cfg.write_text(f'[hub]\noutput_db = "{hub}"\n'
                   'sites = [ { name = "clusterA", host = "a.example.edu" } ]\n'
                   '[impact]\nlaunch = 2026-10-19\nbaseline_from = 2025-09-01\n')
    r = CliRunner().invoke(cli, ["-c", str(cfg), "impact", "status"])
    assert r.exit_code == 0, r.output
    assert "clusterA: jobs from 2025-09-10" in r.output and "On clusterA:" in r.output

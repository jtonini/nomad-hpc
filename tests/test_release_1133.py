"""nomad 1.13.3: long runs found inside workstation sessions, which can be
weeks long now that a session follows the slice (1.13.2)."""
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from nomad.impact import Impact
from nomad.workstation_sessions import busy_runs

T0 = datetime(2026, 11, 2, 0, 0)


def readings(segments, start=T0, step_min=5):
    """(timestamp, cpu_usage_usec) every step_min minutes through segments
    of (hours, busy cores)."""
    out, t, cpu = [], start, 0.0
    for hours, cores in segments:
        for _ in range(int(round(hours * 60 / step_min))):
            out.append((t.isoformat(), int(cpu)))
            t += timedelta(minutes=step_min)
            cpu += cores * step_min * 60 * 1e6
    out.append((t.isoformat(), int(cpu)))
    return out


def test_a_two_day_job_inside_three_weeks_is_a_long_run():
    r = readings([(200, 0.1), (24, 16), (1 / 6, 0), (24, 16), (200, 0.1)])
    (hours, cores, ended), = busy_runs(r, 8, 12)          # a 10-minute pause inside
    assert hours == pytest.approx(48.2, abs=0.1) and cores == pytest.approx(15.9, abs=0.1)
    assert ended == (T0 + timedelta(hours=200 + 48 + 1 / 6)).isoformat(timespec="seconds")
    # The session's own average: about 2 of 16 cores, nowhere near half.
    span_h = (200 + 48 + 200)
    assert (16 * 48) / span_h < 8


def test_an_hour_long_pause_ends_a_run_and_a_short_burst_is_none():
    assert [round(h) for h, _c, _e in busy_runs(readings([(20, 10), (1, 0), (20, 10)]), 8, 12)] \
        == [20, 20]
    assert busy_runs(readings([(5, 16), (100, 0.1)]), 8, 12) == []
    assert busy_runs(readings([(30, 6)]), 8, 12) == []           # long but not heavy enough


def _snapshots(path, rows, host_cores=16):
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE workstation_user_snapshot (timestamp TEXT, hostname TEXT, username TEXT,
            uid INTEGER, session_epoch INTEGER, cpu_usage_usec INTEGER,
            memory_peak_bytes INTEGER, memory_anon_bytes INTEGER, source_site TEXT);
        CREATE TABLE workstation_state (timestamp TEXT, hostname TEXT, memory_total_mb INTEGER,
            cpu_count INTEGER, source_site TEXT);
    """)
    c.execute("INSERT INTO workstation_state VALUES (?, 'ws1', 65536, ?, 'labs')",
              (datetime.now().isoformat(), host_cores))
    for i, (ts, cpu) in enumerate(rows):
        c.execute("INSERT INTO workstation_user_snapshot VALUES (?, 'ws1', 'u1', 1001, ?, ?, ?, ?, "
                  "'labs')", (ts, 1000 + i // 24, cpu, 8 * 2 ** 30, 4 * 2 ** 30))
    c.commit()
    c.close()


def test_impact_counts_the_run_in_its_own_month(tmp_path):
    from nomad import impact_sources
    # One slice from mid-October to late November; a 2-day 16-core job in it
    # ending 2 November, after launch.
    start = datetime(2026, 10, 15)
    r = readings([(17 * 24 - 24, 0.1), (48, 16), (24 * 20, 0.1)], start=start)
    db = tmp_path / "hub.db"
    _snapshots(db, r)
    imp = Impact(date(2026, 11, 1), date(2025, 11, 1))
    rep = impact_sources.workstations(sqlite3.connect(db), imp, today=date(2026, 11, 30))[0]
    by = {m["month"]: m for m in rep["months"]}
    assert by["2026-11"]["sessions"] == 1 and by["2026-11"]["long_runs"] == 1
    assert by["2026-11"]["long_run_hours"] == pytest.approx(48, abs=0.5)
    assert by["2026-10"]["long_runs"] == 0


def test_the_console_advises_on_a_run_inside_a_long_session(tmp_path):
    from nomad.edu.insights import user_insights
    from nomad.edu.progress import _load_user_sessions
    from tests.test_workstation_compute_verdict import TIERS
    start = datetime.now() - timedelta(days=6, hours=12)
    # 6.5 days in one slice: 2 days at 12 of 16 cores, the rest light.
    r = readings([(48, 0.2), (48, 12), (60, 0.2)], start=start)
    db = tmp_path / "ws.db"
    _snapshots(db, r)
    s, = _load_user_sessions(str(db), "u1", days=7)
    assert s["span_hours"] == pytest.approx(156, abs=0.5)
    (hours, cores, _ended), = s["long_runs"]
    assert hours == pytest.approx(48, abs=0.5) and cores == pytest.approx(12, abs=0.1)
    verdict = user_insights(str(db), "u1", cluster_capacities=TIERS).issues[0]
    assert verdict.kind == "verdict" and verdict.context["reason"] == "compute"
    assert verdict.context["busy_cores"] == pytest.approx(12, abs=0.1)
    assert verdict.context["span_hours"] == pytest.approx(48, abs=0.5)
    assert "#SBATCH --time=3-00:00:00" in str(verdict.context)


def test_no_host_size_or_no_cpu_falls_back_on_the_session(tmp_path):
    from nomad.edu.progress import _load_user_sessions
    r = [(ts, None) for ts, _cpu in readings([(20, 10)], start=datetime.now() - timedelta(days=1))]
    db = tmp_path / "ws.db"
    _snapshots(db, r)
    s, = _load_user_sessions(str(db), "u1", days=7)
    assert s["long_runs"] is None


def test_a_heavy_job_beside_days_of_moderate_load_is_still_found():
    # 8 of 16 cores needed; days at 6.5 cores keep the stretch going (above
    # three quarters of 8) but pull its average below 8.
    for segments in ([(100, 0.1), (48, 16), (336, 6.5), (50, 0.1)],
                     [(100, 0.1), (336, 6.5), (48, 16), (50, 0.1)]):
        (hours, cores, _e), = busy_runs(readings(segments), 8, 12)
        assert hours == pytest.approx(48, abs=0.2) and cores == pytest.approx(16, abs=0.1)


def test_a_month_with_a_run_counts_its_person(tmp_path):
    from nomad import impact_sources
    # A slice from 1 October to 18 November; a 48 h job ending 10 October.
    start = datetime(2026, 10, 1)
    r = readings([(7 * 24, 0.1), (48, 16), (39 * 24, 0.1)], start=start)
    db = tmp_path / "hub.db"
    _snapshots(db, r)
    imp = Impact(date(2026, 11, 1), date(2025, 11, 1))
    rep = impact_sources.workstations(sqlite3.connect(db), imp, today=date(2026, 11, 30))[0]
    by = {m["month"]: m for m in rep["months"]}
    assert (by["2026-10"]["people"], by["2026-10"]["sessions"], by["2026-10"]["long_runs"]) \
        == (1, 0, 1)
    assert by["2026-11"]["sessions"] == 1 and by["2026-11"]["long_runs"] == 0


def test_a_run_that_ended_before_the_advice_the_same_day_is_before_it():
    from nomad.impact_advice import moved_to_cluster
    run = (48.0, 16.0, "2026-10-20T08:00:00")
    sessions = {"u1": [{"long_run": True, "runs": [run], "end": "2026-10-25"}]}
    a = {"user": "u1", "site": "c1", "at": "2026-10-20T15:00:00"}
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE jobs (user_name TEXT, end_time TEXT, source_site TEXT)")
    now = datetime(2026, 12, 30)
    assert moved_to_cluster(conn, a, sessions, now) == "fewer_long_runs"

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Insights and Dynamics on a combined database, one site at a time.

The fixture is shaped like a real hub database (``nomad sync``): every table
carries ``source_site`` except ``alerts``, which older syncs merged without
it; alerts use nomad's own columns (category/source, never resolved), not
the demo database's. The sites mirror what the engines got wrong on real
data in September 2026:

  big     one person runs 94% of the jobs; GPU jobs fail in one partition;
          a node drained for repair, another down for an hour; /scratch
          filling; a persisting condition re-alerted every six hours;
          people in several groups each (so groups can't be counted)
  small   31 jobs, 3 of them cancelled by their owner; /home and /scratch
          are one filesystem
  fsonly  filesystems only (a file server)
  ws      workstations only, two of them busy
  quiet   stopped reporting five hours ago
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from nomad.db import scope
from nomad.insights import InsightEngine
from nomad.insights import signals as sig
from nomad.insights.templates import narrate
from nomad.dynamics.engine import DynamicsEngine
from nomad.dynamics.attribution import job_attribution
from nomad.dynamics.capacity import compute_capacity
from nomad.dynamics.diversity import compute_diversity
from nomad.dynamics.niche import compute_niche_overlap
from nomad.dynamics.resilience import compute_resilience

TB = 1024 ** 4


def _schema(c):
    c.executescript("""
    CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT,
        user_name TEXT, group_name TEXT, partition TEXT, state TEXT,
        submit_time TEXT, start_time TEXT, end_time TEXT, req_cpus INTEGER,
        req_mem_mb INTEGER, req_gpus INTEGER, req_time_seconds INTEGER,
        runtime_seconds INTEGER, wait_time_seconds INTEGER, source_site TEXT);
    CREATE TABLE node_state (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
        node_name TEXT, state TEXT, cpus_total INTEGER, cpus_alloc INTEGER,
        memory_total_mb INTEGER, memory_alloc_mb INTEGER, cpu_alloc_percent REAL,
        memory_alloc_percent REAL, cluster TEXT, partitions TEXT, reason TEXT,
        gres TEXT, is_healthy INTEGER, source_site TEXT);
    CREATE TABLE filesystems (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
        path TEXT, total_bytes INTEGER, used_bytes INTEGER, available_bytes INTEGER,
        used_percent REAL, source_site TEXT);
    CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER,
        timestamp TEXT, severity TEXT, category TEXT, source TEXT, message TEXT,
        details TEXT, acknowledged INTEGER DEFAULT 0, resolved INTEGER DEFAULT 0,
        dedup_key TEXT);
    CREATE TABLE group_membership (id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT, group_name TEXT, gid INTEGER, cluster TEXT, source_site TEXT);
    CREATE TABLE workstation_state (id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT, hostname TEXT, load_avg_1m REAL, cpu_count INTEGER,
        memory_total_mb INTEGER, memory_used_mb INTEGER, disk_total_gb REAL,
        disk_used_gb REAL, disk_usage_pct REAL, zombie_count INTEGER, source_site TEXT);
    CREATE TABLE gpu_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
        gpu_index INTEGER, gpu_util_percent REAL, source_site TEXT);
    CREATE TABLE queue_state (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
        partition TEXT, pending_jobs INTEGER, running_jobs INTEGER,
        total_jobs INTEGER, source_site TEXT);
    CREATE TABLE iostat_device (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,
        device TEXT, util_percent REAL, source_site TEXT);
    CREATE TABLE sync_sites (name TEXT PRIMARY KEY, merged INTEGER);
    """)


def _job(c, site, user, state, end, partition="compute", gpus=0, group="people",
         cpus=4, mem=8000, runtime=3600, wait=600):
    submit = end - timedelta(seconds=runtime + wait)
    start = submit + timedelta(seconds=wait)
    c.execute(
        "INSERT INTO jobs (job_id, user_name, group_name, partition, state, submit_time,"
        " start_time, end_time, req_cpus, req_mem_mb, req_gpus, req_time_seconds,"
        " runtime_seconds, wait_time_seconds, source_site)"
        " VALUES (abs(random()) % 100000000, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user, group, partition, state, submit.isoformat(), start.isoformat(),
         end.isoformat(), cpus, mem, gpus, runtime * 2, runtime, wait, site))


def _big(c, now):
    # Last 7 days: 400 jobs; "heavy" runs 376 (94%). Their GPU jobs fail.
    for i in range(376):
        end = now - timedelta(hours=1 + (i % 160))
        if i < 150:
            state = "FAILED" if i < 45 else "COMPLETED"
            _job(c, "big", "heavy", state, end, partition="gpunodes", gpus=1)
        elif i < 160:
            _job(c, "big", "heavy", "CANCELLED by 1001", end)
        else:
            _job(c, "big", "heavy", "COMPLETED", end)
    for i in range(24):
        _job(c, "big", f"u{i % 4 + 1}", "COMPLETED" if i % 12 else "TIMEOUT",
             now - timedelta(hours=2 + i * 5))
    # The 7 days before: 300 jobs, a fifth failing.
    for i in range(300):
        _job(c, "big", "heavy", "FAILED" if i % 5 == 0 else "COMPLETED",
             now - timedelta(hours=170 + (i % 160)))

    # heavy belongs to three research groups: their jobs can't be placed.
    for user, groups in {"heavy": ["g1$", "g2$", "g3$"], "u1": ["g1$"],
                         "u2": ["g2$"], "u3": [], "u4": []}.items():
        for g in groups + ["people"]:
            c.execute("INSERT INTO group_membership (username, group_name, source_site)"
                      " VALUES (?, ?, 'big')", (user, g))

    # Nodes every 15 minutes for 3 days. spdr06 drained for repair two days
    # ago and still is; spdr07 went down for an hour and came back.
    for k in range(3 * 96 + 1):
        ts = now - timedelta(minutes=15 * (3 * 96 - k))
        for n in range(1, 11):
            name = f"spdr{n:02d}"
            state, reason, healthy = "MIXED", None, 1
            if name == "spdr06" and ts >= now - timedelta(days=2):
                state, reason, healthy = "IDLE+DRAIN", "vendor repair", 0
            if name == "spdr07" and now - timedelta(hours=40) <= ts < now - timedelta(hours=39):
                state, reason, healthy = "DOWN*", "Not responding", 0
            alloc = 0 if healthy == 0 else 13
            c.execute(
                "INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc,"
                " memory_total_mb, memory_alloc_mb, cluster, partitions, reason, gres,"
                " is_healthy, source_site) VALUES (?, ?, ?, 64, ?, 512000, ?, 'big',"
                " 'compute', ?, 'gpu:8', ?, 'big')",
                (ts.isoformat(), name, state, alloc, alloc * 8000, reason, healthy))

    # /home steady at 73%; /scratch filling: 83.7% -> 87% over 30 days,
    # about 10 TB a month (spydur's rate in September 2026).
    for d in range(31):
        for h in (0, 6, 12, 18):
            ts = now - timedelta(days=30 - d, hours=-h) if d < 30 else now - timedelta(minutes=10 * (4 - h // 6))
            if ts > now:
                continue
            scratch = int((0.837 + 0.033 * d / 30) * 313 * TB)
            home = int(0.73 * 15 * TB)
            for path, total, used in (("/scratch", 313 * TB, scratch), ("/home", 15 * TB, home)):
                c.execute("INSERT INTO filesystems (timestamp, path, total_bytes, used_bytes,"
                          " available_bytes, used_percent, source_site) VALUES (?,?,?,?,?,?, 'big')",
                          (ts.isoformat(), path, total, used, total - used, used / total * 100))

    # /scratch re-alerted every six hours for a week; three older alerts
    # stored before alerts recorded their site.
    for k in range(28):
        ts = now - timedelta(hours=6 * k + 1)
        c.execute("INSERT INTO alerts (timestamp, severity, category, source, message, details)"
                  " VALUES (?, 'warning', 'disk', 'big-head', 'Disk usage at 87% on /scratch', ?)",
                  (ts.isoformat(), json.dumps({"site": "big", "host": "big-head"})))
    for k in range(3):
        c.execute("INSERT INTO alerts (timestamp, severity, category, source, message, details)"
                  " VALUES (?, 'warning', 'disk', 'big-head', 'old alert', '{}')",
                  ((now - timedelta(days=2, hours=k)).isoformat(),))

    for h in range(7 * 24):
        ts = (now - timedelta(hours=h)).isoformat()
        for g in range(8):
            c.execute("INSERT INTO gpu_stats (timestamp, gpu_index, gpu_util_percent, source_site)"
                      " VALUES (?, ?, 28, 'big')", (ts, g))
        c.execute("INSERT INTO iostat_device (timestamp, device, util_percent, source_site)"
                  " VALUES (?, 'sda', 2, 'big')", (ts,))
        c.execute("INSERT INTO iostat_device (timestamp, device, util_percent, source_site)"
                  " VALUES (?, 'md127', 40, 'big')", (ts,))
    c.execute("INSERT INTO queue_state (timestamp, partition, pending_jobs, running_jobs,"
              " total_jobs, source_site) VALUES (?, 'compute', 2, 10, 12, 'big')",
              ((now - timedelta(minutes=5)).isoformat(),))


def _small(c, now):
    for i in range(26):
        _job(c, "small", "a1" if i % 5 else "a2", "COMPLETED", now - timedelta(hours=3 + i * 5))
    for i in range(3):
        _job(c, "small", "a1", "CANCELLED by 29405", now - timedelta(hours=4 + i))
    for i in range(2):
        _job(c, "small", "a2", "FAILED", now - timedelta(hours=6 + i), partition="gpunodes", gpus=1)
    for k in range(4):
        ts = (now - timedelta(minutes=5 * k)).isoformat()
        for n in ("node01", "node02", "node03"):
            c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total,"
                      " cpus_alloc, memory_total_mb, memory_alloc_mb, cluster, is_healthy,"
                      " source_site) VALUES (?, ?, 'IDLE', 128, 0, 512000, 0, 'small', 1, 'small')",
                      (ts, n))
        for path in ("/home", "/scratch"):   # one 145 TB volume, mounted twice
            c.execute("INSERT INTO filesystems (timestamp, path, total_bytes, used_bytes,"
                      " available_bytes, used_percent, source_site) VALUES (?,?,?,?,?,?, 'small')",
                      (ts, path, 145 * TB, int(0.75 * 145 * TB), int(0.25 * 145 * TB), 75.0))
    # A filesystem retired three months ago, last seen 96% full.
    c.execute("INSERT INTO filesystems (timestamp, path, total_bytes, used_bytes,"
              " available_bytes, used_percent, source_site) VALUES (?, '/old', ?, ?, ?, 96, 'small')",
              ((now - timedelta(days=90)).isoformat(), TB, int(0.96 * TB), int(0.04 * TB)))


def _fsonly(c, now):
    for k in range(4):
        ts = (now - timedelta(minutes=5 * k)).isoformat()
        c.execute("INSERT INTO filesystems (timestamp, path, total_bytes, used_bytes,"
                  " available_bytes, used_percent, source_site) VALUES (?, '/data', ?, ?, ?, 40, 'fsonly')",
                  (ts, 10 * TB, 4 * TB, 6 * TB))


def _ws(c, now):
    for k in range(4):
        ts = (now - timedelta(minutes=5 * k)).isoformat()
        for host, load in (("ws01", 12.0), ("ws02", 9.0), ("ws03", 0.5)):
            c.execute("INSERT INTO workstation_state (timestamp, hostname, load_avg_1m, cpu_count,"
                      " memory_total_mb, memory_used_mb, disk_total_gb, disk_used_gb,"
                      " disk_usage_pct, zombie_count, source_site)"
                      " VALUES (?, ?, ?, 4, 16000, 4000, 500, 100, 20, 0, 'ws')",
                      (ts, host, load))


def _quiet(c, now):
    ts = (now - timedelta(hours=5)).isoformat()
    c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc,"
              " cluster, is_healthy, source_site) VALUES (?, 'q01', 'IDLE', 32, 0, 'quiet', 1, 'quiet')",
              (ts,))
    c.execute("INSERT INTO filesystems (timestamp, path, total_bytes, used_bytes,"
              " available_bytes, used_percent, source_site) VALUES (?, '/home', ?, ?, ?, 50, 'quiet')",
              (ts, TB, TB // 2, TB // 2))


@pytest.fixture(scope="module")
def combined(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("hub") / "combined.db"
    c = sqlite3.connect(path)
    _schema(c)
    now = datetime.now()
    _big(c, now)
    _small(c, now)
    _fsonly(c, now)
    _ws(c, now)
    _quiet(c, now)
    c.executemany("INSERT INTO sync_sites (name, merged) VALUES (?, 1)",
                  [(s,) for s in ("big", "small", "fsonly", "ws", "quiet")])
    c.commit()
    c.close()
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _by_title(signals, title):
    return [s for s in signals if s.title == title]


# ── Reading one site ─────────────────────────────────────────────────────

def test_scope_reads_one_site_read_only(combined):
    conn = scope.connect(combined, "small")
    assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 31
    assert {r[0] for r in conn.execute("SELECT DISTINCT source_site FROM node_state")} == {"small"}
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM main.jobs")
    conn.close()
    assert scope.sites(combined) == ["big", "fsonly", "quiet", "small", "ws"]


def test_engines_leave_the_database_untouched_and_copy_nothing(combined, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("create_site_db copies the database; nothing should call it")
    monkeypatch.setattr(sig, "create_site_db", refuse)
    before = _digest(combined)
    for site in ("big", "small"):
        InsightEngine(combined, hours=168, site=site).to_dict()
        DynamicsEngine(combined, hours=168, site=site).to_dict()
    InsightEngine(combined, hours=168).to_dict()      # every site, labelled
    assert _digest(combined) == before


# ── Jobs ─────────────────────────────────────────────────────────────────

def test_cancelled_jobs_are_not_failures_and_small_counts_stay_calm(combined):
    got = sig.read_job_signals(combined, hours=168, site="small")
    rate = _by_title(got, "job_success_rate")[0]
    m = rate.metrics
    assert (m["judged"], m["cancelled"], m["problems"]) == (28, 3, 2)
    assert rate.severity == sig.Severity.INFO          # 28 jobs: too few to judge
    assert not _by_title(got, "job_rate_trend")
    assert not _by_title(got, "partition_failure_concentration")
    text = narrate(rate)
    assert "3 were cancelled" in text and "not counted as a failure" in text
    assert "Too few jobs" in text
    assert "baseline" not in text


def test_problems_concentrated_in_one_partition_and_one_person(combined):
    got = sig.read_job_signals(combined, hours=168, site="big")
    part = _by_title(got, "partition_failure_concentration")
    assert [p.metrics["partition"] for p in part] == ["gpunodes"]
    assert part[0].metrics["pct"] == pytest.approx(30.0)
    assert part[0].metrics["elsewhere_pct"] < 2
    trend = _by_title(got, "job_rate_trend")[0]
    assert trend.severity == sig.Severity.INFO          # fewer problems than before
    assert "Fewer jobs are failing" in narrate(trend)

    gpu = _by_title(sig.read_gpu_signals(combined, hours=168, site="big"),
                    "gpu_job_failure_rate")[0]
    text = narrate(gpu)
    assert "one person's" in text
    assert "research groups" not in text                 # no invented claims


# ── Storage ──────────────────────────────────────────────────────────────

def test_one_filesystem_at_two_paths_is_reported_once(combined):
    got = sig.read_disk_signals(combined, hours=12, site="small")
    assert len(got) == 1
    assert got[0].metrics["paths"] == ["/home", "/scratch"]
    assert "one filesystem" in narrate(got[0])


def test_growth_and_time_to_full_come_from_the_readings(combined):
    got = sig.read_disk_signals(combined, hours=12, site="big")
    scratch = [s for s in got if s.metrics["paths"] == ["/scratch"]][0]
    assert scratch.severity == sig.Severity.WARNING
    assert scratch.metrics["growth_gb_per_day"] > 0
    assert 60 < scratch.metrics["days_until_full"] < 365
    text = narrate(scratch)
    assert text.startswith("/scratch is 87% full")
    assert "Growing about" in text and "months at that rate" in text


# ── Alerts ───────────────────────────────────────────────────────────────

def test_alerts_are_read_from_nomads_own_columns_and_never_called_active(combined):
    got = sig.read_alert_signals(combined, hours=168, site="big")
    assert [s.title for s in got] == ["alerts_raised"]
    m = got[0].metrics
    assert (m["total"], m["conditions"], m["unplaced"]) == (28, 1, 3)
    text = narrate(got[0]).lower()
    assert "active" not in text and "flapping" not in text
    assert "older alerts don't record their site" in text


# ── Nodes ────────────────────────────────────────────────────────────────

def test_node_health_reads_the_latest_snapshot(combined):
    got = sig.read_node_health_signals(combined, site="big", config={})
    assert [(s.title, s.metrics["node"]) for s in got] == [("node_drain", "spdr06")]
    assert got[0].metrics["reason"] == "vendor repair"


# ── Coverage, staleness, health ──────────────────────────────────────────

def test_nothing_to_read_is_not_good_and_a_quiet_site_is_named(combined):
    eng = InsightEngine(combined, hours=168, site="quiet")
    status = {c["source"]: c["status"] for c in eng.coverage}
    assert status["nodes"] == "stale" and status["storage"] == "stale"
    assert status["jobs"] == "no_data"
    stale = _by_title(eng.signals, "data_stale")
    assert stale and "No new readings from quiet" in stale[0].detail
    assert eng.overall_health == "unknown"                # nothing current was measured
    d = eng.to_dict()
    assert d["measured"] is False and d["overall_health"] == "unknown"


def test_a_file_server_is_measured_for_storage_only(combined):
    eng = InsightEngine(combined, hours=168, site="fsonly")
    status = {c["source"]: (c["status"], c["detail"]) for c in eng.coverage}
    assert status["storage"][0] == "measured"
    assert status["jobs"] == ("no_data", "no jobs recorded here")
    assert status["nodes"] == ("no_data", "not collected here")
    assert eng.overall_health == "good"
    assert "Nothing to read: jobs" in eng.brief()


def test_busy_workstations_are_narrated_without_crashing(combined):
    eng = InsightEngine(combined, hours=12, site="ws")
    cpu = _by_title(eng.signals, "workstation_high_cpu")
    assert sorted(s.metrics["hostname"] for s in cpu) == ["ws01", "ws02"]
    texts = dict((s.title, t) for s, t in eng.narratives)
    assert "load of" in texts["workstation_high_cpu"]
    assert [i.title for i in eng.insights] == ["widespread_workstation_pressure"]
    assert "2 interactive machines" in eng.insights[0].narrative


def test_every_site_is_read_separately_and_labelled(combined):
    eng = InsightEngine(combined, hours=168)
    sites = {c["site"] for c in eng.coverage}
    assert sites == {"big", "small", "fsonly", "ws", "quiet"}
    texts = [t for _, t in eng.narratives]
    assert any(t.startswith("big: ") for t in texts)
    assert any(t.startswith("small: /home = /scratch") for t in texts)


def test_a_failing_reader_is_reported_not_silenced(combined, monkeypatch):
    def broken(*a, **k):
        raise sqlite3.OperationalError("no such column: host")
    monkeypatch.setattr(sig.SOURCES[5], "reader", broken)   # alerts
    eng = InsightEngine(combined, hours=168, site="big")
    alerts = [c for c in eng.coverage if c["source"] == "alerts"][0]
    assert alerts["status"] == "failed" and "no such column" in alerts["detail"]
    assert eng.overall_health in ("degraded", "impaired")


# ── Dynamics ─────────────────────────────────────────────────────────────

def test_groups_are_not_counted_when_people_belong_to_several(combined):
    eng = DynamicsEngine(combined, hours=168, site="big")
    d = eng.to_dict()
    for key in ("diversity", "niche", "externality"):
        assert d[key]["available"] is False
        assert "to more than one group" in d[key]["reason"]
    by_user = d["diversity_by_user"]
    assert by_user["available"] is True
    assert by_user["current"]["dominant_proportion"] == pytest.approx(0.94, abs=0.01)
    assert "One person" in by_user["fragility_detail"]

    got = sig.read_dynamics_signals(combined, hours=168, site="big")
    titles = [s.title for s in got]
    assert "diversity_fragility" in titles
    assert "niche_contention_risk" not in titles and "externality_detected" not in titles
    frag = _by_title(got, "diversity_fragility")[0]
    assert "One person ran 94%" in narrate(frag)


def test_forced_membership_still_available_and_says_what_it_does(combined):
    niche = compute_niche_overlap(combined, hours=168, site="big", attribution="membership")
    assert niche.available
    assert "counted once per group" in niche.reason


def test_each_job_counted_once_when_jobs_record_their_group(tmp_path):
    db = tmp_path / "g.db"
    c = sqlite3.connect(db)
    _schema(c)
    now = datetime.now()
    for i in range(60):
        _job(c, "x", f"p{i % 6}", "COMPLETED", now - timedelta(hours=1 + i),
             group=["chem", "bio", "phys"][i % 3])
    c.commit()
    c.close()
    div = compute_diversity(db, dimension="group", hours=168, site="x")
    assert div.available and div.attribution["method"] == "group_name"
    assert sum(div.current.category_counts.values()) == 60     # no fan-out
    assert set(div.current.category_counts) == {"chem", "bio", "phys"}


def test_unambiguous_membership_places_each_job_once(tmp_path):
    db = tmp_path / "m.db"
    c = sqlite3.connect(db)
    _schema(c)
    now = datetime.now()
    for i in range(40):
        _job(c, "x", f"p{i % 4}", "COMPLETED", now - timedelta(hours=1 + i))
    for user, group in (("p0", "a$"), ("p1", "a$"), ("p2", "b$"), ("p3", "b$")):
        for g in (group, "people"):
            c.execute("INSERT INTO group_membership (username, group_name, source_site)"
                      " VALUES (?, ?, 'x')", (user, g))
    c.commit()
    conn = scope.connect(db, "x")
    att = job_attribution(conn, (now - timedelta(days=7)).isoformat())
    conn.close()
    assert att.available and att.method == "membership"
    div = compute_diversity(db, dimension="group", hours=168, site="x")
    assert div.current.category_counts == {"a$": 20, "b$": 20}


def test_capacity_names_the_busiest_and_binds_only_near_the_limit(combined):
    cap = compute_capacity(combined, hours=168, site="big")
    dims = {d.dimension: d for d in cap.dimensions}
    # 9 nodes allocatable with 13 of 64 cores used (spdr06 drained, not counted)
    assert dims["cpu"].current_utilization == pytest.approx(13 / 64, abs=0.01)
    assert dims["io"].current_utilization == pytest.approx(0.40, abs=0.01)   # busiest device
    assert cap.binding_constraint is None
    assert cap.busiest.dimension == "io"
    assert cap.summary.startswith("Nothing is near its limit")
    assert not _by_title(sig.read_dynamics_signals(combined, hours=168, site="big"),
                         "capacity_binding_constraint")


def test_resilience_scores_failures_and_lists_drains(combined):
    res = compute_resilience(combined, hours=720, site="big")
    kinds = sorted(d.event_type for d in res.disturbances)
    assert kinds.count("node_failure") == 1              # spdr07, back after an hour
    assert kinds.count("node_drain") == 1                # spdr06, not scored
    assert res.drains == 1
    assert "not scored" in res.summary
    empty = compute_resilience(combined, hours=720, site="fsonly")
    assert empty.resilience_score is None


# ── What the review found (30 Sep) ───────────────────────────────────────

def test_a_retired_filesystem_is_not_reported_as_now(combined):
    got = sig.read_disk_signals(combined, hours=12, site="small")
    assert [s.metrics["paths"] for s in got] == [["/home", "/scratch"]]


def test_no_warning_from_fewer_than_fifty_jobs(tmp_path):
    db = tmp_path / "few.db"
    c = sqlite3.connect(db)
    _schema(c)
    now = datetime.now()
    for i in range(25):   # one partition, 12 of 25 failing, 6 timing out
        state = "FAILED" if i < 12 else "TIMEOUT" if i < 18 else "COMPLETED"
        _job(c, "x", "p1", state, now - timedelta(hours=1 + i), partition="gpu",
             gpus=1)
    for i in range(4):
        _job(c, "x", "p1", "OUT_OF_MEMORY", now - timedelta(hours=2 + i), gpus=1)
    c.commit(); c.close()
    got = sig.read_job_signals(db, hours=168, site="x") + \
        sig.read_gpu_signals(db, hours=168, site="x")
    assert not _by_title(got, "partition_failure_concentration")
    assert all(s.severity in (sig.Severity.INFO, sig.Severity.NOTICE) for s in got), \
        [(s.title, s.severity) for s in got]


def test_a_busy_queue_does_not_bind(tmp_path):
    db = tmp_path / "q.db"
    c = sqlite3.connect(db)
    _schema(c)
    now = datetime.now()
    for h in range(3):
        c.execute("INSERT INTO queue_state (timestamp, partition, pending_jobs, running_jobs,"
                  " total_jobs, source_site) VALUES (?, 'compute', 30, 10, 40, 'x')",
                  ((now - timedelta(hours=h)).isoformat(),))
        c.execute("INSERT INTO iostat_device (timestamp, device, util_percent, source_site)"
                  " VALUES (?, 'sda', 20, 'x')", ((now - timedelta(hours=h)).isoformat(),))
    c.commit(); c.close()
    cap = compute_capacity(db, hours=24, site="x")
    assert cap.binding_constraint is None and cap.busiest.dimension == "io"
    assert cap.overall_pressure == "low"


def test_sites_are_never_pooled_and_unknown_sites_refused(combined):
    with pytest.raises(ValueError, match="choose one"):
        DynamicsEngine(combined, hours=168)
    with pytest.raises(ValueError, match="No site 'nowhere'"):
        InsightEngine(combined, hours=168, site="nowhere")
    with pytest.raises(ValueError, match="No site 'nowhere'"):
        DynamicsEngine(combined, hours=168, site="nowhere")


def test_user_private_groups_are_not_groups(tmp_path):
    db = tmp_path / "upg.db"
    c = sqlite3.connect(db)
    _schema(c)
    now = datetime.now()
    for i in range(60):
        user = f"p{i % 10}"
        _job(c, "x", user, "COMPLETED", now - timedelta(hours=1 + i), group=user)
    c.commit(); c.close()
    div = compute_diversity(db, dimension="group", hours=168, site="x")
    assert not div.available


def test_resilience_reads_zoned_timestamps_and_open_spikes(tmp_path):
    db = tmp_path / "tz.db"
    c = sqlite3.connect(db)
    _schema(c)
    now = datetime.now().astimezone()
    for k in range(8):
        ts = (now - timedelta(hours=8 - k)).isoformat()      # "+00:00"-style offsets
        state = "DOWN" if k in (3, 4) else "IDLE"
        c.execute("INSERT INTO node_state (timestamp, node_name, state, is_healthy, source_site)"
                  " VALUES (?, 'n1', ?, ?, 'x')", (ts, state, 0 if state == "DOWN" else 1))
    base = datetime.now()
    for h in range(1, 7):          # a steady failure rate, then a spike in the last hour
        for i in range(20):
            failed = (h == 1 and i < 15) or (h > 1 and i < 1)
            _job(c, "x", "p1", "FAILED" if failed else "COMPLETED",
                 base - timedelta(hours=h - 1, minutes=10 + i))
    c.commit(); c.close()
    res = compute_resilience(db, hours=720, site="x")
    kinds = [d.event_type for d in res.disturbances]
    assert "node_failure" in kinds
    spikes = [d for d in res.disturbances if d.event_type == "job_failure_spike"]
    assert spikes and spikes[-1].recovered is None
    assert "still going" in spikes[-1].detail


def test_slack_and_email_do_not_say_all_clear_when_nothing_was_measured(combined):
    eng = InsightEngine(combined, hours=168, site="quiet")
    assert "unknown" in eng.to_slack().lower()
    assert "nominal" not in eng.to_slack().lower()
    subject, body = eng.to_email()
    assert "Unknown" in subject and "All Clear" not in subject
    assert "nothing current was measured" in body


def test_node_states_from_an_old_snapshot_say_so(combined):
    got = sig.read_node_health_signals(combined, site="quiet", config={})
    assert got == []                     # q01 was idle
    conn = sqlite3.connect(combined)
    conn.execute("UPDATE node_state SET state = 'DOWN', is_healthy = 0 WHERE source_site = 'quiet'")
    conn.commit(); conn.close()
    try:
        got = sig.read_node_health_signals(combined, site="quiet", config={})
        assert "at the last report" in got[0].detail and " was DOWN" in got[0].detail
    finally:
        conn = sqlite3.connect(combined)
        conn.execute("UPDATE node_state SET state = 'IDLE', is_healthy = 1 WHERE source_site = 'quiet'")
        conn.commit(); conn.close()


def test_slack_and_email_status_follow_health(combined, monkeypatch):
    def broken(*a, **k):
        raise sqlite3.OperationalError("no such column: req_gpus")
    monkeypatch.setattr(sig.SOURCES[2], "reader", broken)   # GPU jobs
    eng = InsightEngine(combined, hours=168, site="small")
    assert eng.overall_health == "degraded"
    slack = eng.to_slack()
    assert "Status: DEGRADED" in slack and "could not read" in slack
    subject, body = eng.to_email()
    assert "DEGRADED" in subject and "Could not read" in body


def test_a_one_member_group_does_not_make_its_member_ambiguous(tmp_path):
    db = tmp_path / "one.db"
    c = sqlite3.connect(db)
    _schema(c)
    now = datetime.now()
    for i in range(40):
        _job(c, "x", f"p{i % 4}", "COMPLETED", now - timedelta(hours=1 + i))
    rows = [("p0", "a$"), ("p1", "a$"), ("p2", "b$"), ("p3", "b$"),
            ("p0", "p0-private")]                 # p0 also has a group of their own
    for user, g in rows + [(u, "people") for u in ("p0", "p1", "p2", "p3")]:
        c.execute("INSERT INTO group_membership (username, group_name, source_site)"
                  " VALUES (?, ?, 'x')", (user, g))
    c.commit(); c.close()
    div = compute_diversity(db, dimension="group", hours=168, site="x")
    assert div.available
    assert div.current.category_counts == {"a$": 20, "b$": 20}


def test_a_test_alert_is_not_about_the_system(tmp_path):
    db = tmp_path / "t.db"
    c = sqlite3.connect(db)
    _schema(c)
    c.execute("ALTER TABLE alerts ADD COLUMN source_site TEXT")
    now = datetime.now()
    c.execute("INSERT INTO alerts (timestamp, severity, category, source, message, details,"
              " source_site) VALUES (?, 'warning', 'test', 'cli-test',"
              " 'NØMAÐ test alert (nomad test-alerts).', '{}', 'x')",
              ((now - timedelta(hours=1)).isoformat(),))
    c.execute("INSERT INTO alerts (timestamp, severity, category, source, message, details,"
              " source_site) VALUES (?, 'warning', 'disk', 'head', 'Disk usage at 81% on /home',"
              " '{}', 'x')", ((now - timedelta(days=3)).isoformat(),))
    c.commit(); c.close()
    got = sig.read_alert_signals(db, hours=168, site="x")
    assert got[0].metrics["total"] == 1
    assert got[0].severity == sig.Severity.NOTICE        # nothing in the last day
    assert "test alert" not in narrate(got[0])


def test_an_alert_whose_numbers_move_is_one_condition(tmp_path):
    db = tmp_path / "t.db"
    c = sqlite3.connect(db)
    _schema(c)
    c.execute("ALTER TABLE alerts ADD COLUMN source_site TEXT")
    now = datetime.now()
    for k, pct in enumerate(("86.0", "86.4", "87.0", "87.1")):
        c.execute("INSERT INTO alerts (timestamp, severity, category, source, message, details,"
                  " source_site) VALUES (?, 'warning', 'disk', 'head', ?, '{}', 'x')",
                  ((now - timedelta(hours=1 + 20 * k)).isoformat(),
                   f"Disk usage at {pct}% on /scratch"))
    c.execute("INSERT INTO alerts (timestamp, severity, category, source, message, details,"
              " source_site) VALUES (?, 'warning', 'disk', 'head', 'Disk usage at 81% on /home',"
              " '{}', 'x')", ((now - timedelta(hours=2)).isoformat(),))
    c.commit(); c.close()
    got = sig.read_alert_signals(db, hours=168, site="x")
    assert (got[0].metrics["total"], got[0].metrics["conditions"]) == (5, 2)


def test_a_source_quiet_for_over_a_week_has_stopped_and_does_not_lower_health(tmp_path):
    # A file server still reporting, and a workstation collector retired in spring.
    db = tmp_path / "t.db"
    c = sqlite3.connect(db)
    _schema(c)
    now = datetime.now()
    _fsonly(c, now)
    old = (now - timedelta(days=160)).isoformat()
    c.execute("INSERT INTO workstation_state (timestamp, hostname, load_avg_1m, cpu_count,"
              " memory_total_mb, memory_used_mb, disk_total_gb, disk_used_gb, disk_usage_pct,"
              " zombie_count, source_site) VALUES (?, 'ws9', 30, 4, 16000, 15500, 500, 100, 20,"
              " 0, 'fsonly')", (old,))
    c.commit(); c.close()
    eng = InsightEngine(db, hours=168, site="fsonly")
    ws = [x for x in eng.coverage if x["source"] == "workstations"][0]
    assert ws["status"] == "stopped" and ws["signals"] == 0
    assert ws["detail"] == f"last reading {(now - timedelta(days=160)):%Y-%m-%d}"
    assert not _by_title(eng.signals, "data_stale")
    assert not [s for s in eng.signals if s.title.startswith("workstation")]
    assert eng.overall_health == "good"
    assert "Stopped reporting: workstations (last reading" in eng.brief()


def test_sizes_are_in_the_dashboards_decimal_units(combined):
    from nomad.insights.templates import _fmt_bytes
    assert (_fmt_bytes(43.1e12), _fmt_bytes(500e9), _fmt_bytes(2e6)) == \
        ("43.1 TB", "500 GB", "2 MB")
    got = sig.read_disk_signals(combined, hours=12, site="big")
    scratch = [s for s in got if s.metrics["paths"] == ["/scratch"]][0]
    free = scratch.metrics["free_bytes"]
    assert f"({free / 1e12:.1f} TB free)" in narrate(scratch)


def test_attribution_reasons_say_which_period_they_count(combined):
    from nomad.dynamics.attribution import _period
    now = datetime.now()
    assert _period((now - timedelta(days=84)).isoformat()) == "in the last 12 weeks"
    assert _period((now - timedelta(days=7)).isoformat()) == "in the last 7 days"
    assert _period((now - timedelta(hours=24)).isoformat()) == "in the last 24 hours"
    assert _period((now - timedelta(hours=6)).isoformat()) == "in the last 6 hours"
    assert _period("not a time") == "in this window"
    d = DynamicsEngine(combined, hours=168, site="big").to_dict()
    # Diversity places jobs over its trend windows; niche over the window.
    assert "people who ran jobs in the last 12 weeks" in d["diversity"]["reason"]
    assert "people who ran jobs in the last 7 days" in d["niche"]["reason"]


def test_numbers_in_names_keep_alert_conditions_apart(tmp_path):
    db = tmp_path / "t.db"
    c = sqlite3.connect(db)
    _schema(c)
    c.execute("ALTER TABLE alerts ADD COLUMN source_site TEXT")
    now = datetime.now()
    msgs = ["GPU 0 memory at 91.0% (threshold: 90%)", "GPU 3 memory at 95.2% (threshold: 90%)",
            "GPU 0 memory at 92.5% (threshold: 90%)", "Disk /data1 at 81.0% (threshold: 80%)",
            "Disk /data2 at 81.0% (threshold: 80%)", "Node spdr06 load 12.40 (threshold: 8)",
            "Node spdr17 load 9.10 (threshold: 8)"]
    for k, m in enumerate(msgs):
        c.execute("INSERT INTO alerts (timestamp, severity, category, source, message, details,"
                  " source_site) VALUES (?, 'warning', 'x', 'head', ?, '{}', 'x')",
                  ((now - timedelta(hours=1 + k)).isoformat(), m))
    c.commit(); c.close()
    got = sig.read_alert_signals(db, hours=168, site="x")
    assert (got[0].metrics["total"], got[0].metrics["conditions"]) == (7, 6)


def test_a_storage_server_keeps_storage_current_when_filesystems_stopped(tmp_path):
    db = tmp_path / "t.db"
    c = sqlite3.connect(db)
    _schema(c)
    c.execute("CREATE TABLE storage_state (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT,"
              " hostname TEXT, total_bytes INTEGER, used_bytes INTEGER, free_bytes INTEGER,"
              " usage_pct REAL, source_site TEXT)")         # nomad's collector: usage_pct
    now = datetime.now()
    c.execute("INSERT INTO filesystems (timestamp, path, total_bytes, used_bytes,"
              " available_bytes, used_percent, source_site) VALUES (?, '/home', ?, ?, ?, 50, 'x')",
              ((now - timedelta(days=60)).isoformat(), TB, TB // 2, TB // 2))
    c.execute("INSERT INTO storage_state (timestamp, hostname, total_bytes, used_bytes,"
              " free_bytes, usage_pct, source_site) VALUES (?, 'nas1', ?, ?, ?, 97, 'x')",
              ((now - timedelta(minutes=5)).isoformat(), 10**13, 97 * 10**11, 3 * 10**11))
    c.commit(); c.close()
    eng = InsightEngine(db, hours=24, site="x")
    storage = [x for x in eng.coverage if x["source"] == "storage"][0]
    assert storage["status"] == "measured"
    nas = [s for s in eng.signals if s.metrics.get("server") == "nas1"]
    assert nas and nas[0].severity == sig.Severity.CRITICAL
    assert "(300.0 GB free)" in nas[0].detail
    assert eng.overall_health == "impaired"

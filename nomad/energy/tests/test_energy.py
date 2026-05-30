# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Tests for the NØMAD energy module.

Self-contained: builds a small SQLite fixture with the real schema (jobs,
job_accounting, gpu_stats) so the suite does not depend on the demo
generator. Assertions lock the behaviors that matter rather than brittle
float values: carbon lookup, the physical-vs-allocation valuation contract,
provenance tagging, and engine output shape.
"""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from nomad.energy import (
    EnergyEngine, compute_energy, resolve_intensity,
    MODE_PHYSICAL, MODE_ALLOCATION,
)
from nomad.energy.carbon import EGRID_SUBREGIONS, IEA_COUNTRIES


CONFIG = {"energy": {
    "carbon_source": "epa_egrid", "carbon_region": "SRVC",
    "cpu_tdp_watts": 160, "cores_per_socket": 32, "overhead_factor": 1.3,
    "cpu_assumed_util": 0.5, "cpu_idle_fraction": 0.3,
}}


@pytest.fixture
def db(tmp_path):
    """A minimal energy database with two jobs and DCGM GPU samples."""
    path = tmp_path / "energy_test.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE jobs (
            job_id TEXT, cluster TEXT, user_name TEXT, partition TEXT,
            node_list TEXT, state TEXT, req_cpus INTEGER, req_mem_mb INTEGER,
            req_gpus INTEGER, req_time_seconds INTEGER, runtime_seconds INTEGER,
            start_time DATETIME, end_time DATETIME,
            PRIMARY KEY (job_id, cluster));
        CREATE TABLE job_accounting (
            job_id TEXT, cluster TEXT, username TEXT, account TEXT,
            partition TEXT, state TEXT, elapsed_sec INTEGER, alloc_cpus INTEGER,
            mem_gb REAL, gpu_count INTEGER, cpu_hours REAL, gpu_hours REAL,
            PRIMARY KEY (job_id, cluster));
        CREATE TABLE gpu_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME,
            node_name TEXT, gpu_index INTEGER, gpu_name TEXT,
            gpu_util_percent REAL, real_util_pct REAL, power_draw_w REAL,
            workload_class TEXT, data_source TEXT);
    """)
    now = datetime(2026, 4, 24, 12, 0, 0)
    end1 = (now - timedelta(hours=1)).isoformat(sep=" ")
    end2 = (now - timedelta(hours=2)).isoformat(sep=" ")
    # job1: big time overestimate (req 2h, ran 0.5h), CPU-only.
    # job2: GPU job, modest overestimate.
    conn.executemany(
        """INSERT INTO jobs (job_id, cluster, user_name, partition, node_list,
              state, req_cpus, req_mem_mb, req_gpus, req_time_seconds,
              runtime_seconds, end_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            ("1", "test", "alice", "compute", "node01", "COMPLETED",
             8, 16000, 0, 7200, 1800, end1),
            ("2", "test", "bob", "gpu", "gpu01", "COMPLETED",
             4, 32000, 1, 3600, 3000, end2),
        ],
    )
    conn.executemany(
        """INSERT INTO job_accounting (job_id, cluster, username, account,
              partition, state, elapsed_sec, alloc_cpus, gpu_count)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            ("1", "test", "alice", "lab-a", "compute", "COMPLETED", 1800, 8, 0),
            ("2", "test", "bob", "lab-b", "gpu", "COMPLETED", 3000, 4, 1),
        ],
    )
    ts = (now - timedelta(hours=2)).isoformat(sep=" ")
    ts2 = (now - timedelta(hours=2) + timedelta(seconds=60)).isoformat(sep=" ")
    conn.executemany(
        """INSERT INTO gpu_stats (timestamp, node_name, gpu_index, gpu_name,
              gpu_util_percent, real_util_pct, power_draw_w, workload_class,
              data_source) VALUES (?,?,?,?,?,?,?,?,?)""",
        [
            (ts, "gpu01", 0, "NVIDIA A100-SXM4-40GB", 3.0, 0.0, 280.0, "idle", "dcgm"),
            (ts2, "gpu01", 0, "NVIDIA A100-SXM4-40GB", 70.0, 55.0, 350.0,
             "compute-active", "dcgm"),
        ],
    )
    conn.commit()
    conn.close()
    return str(path)


# ── carbon ────────────────────────────────────────────────────────────
def test_egrid_lookup():
    ci = resolve_intensity({"energy": {"carbon_source": "epa_egrid",
                                       "carbon_region": "SRVC"}})
    assert ci.g_per_kwh == EGRID_SUBREGIONS["SRVC"]
    assert ci.source == "epa_egrid" and ci.region == "SRVC"


def test_region_override_beats_config():
    ci = resolve_intensity(CONFIG, region_override="CAMX")
    assert ci.g_per_kwh == EGRID_SUBREGIONS["CAMX"]


def test_iea_and_manual_and_default():
    assert resolve_intensity({"energy": {"carbon_source": "iea",
                              "carbon_region": "NO"}}).g_per_kwh == IEA_COUNTRIES["NO"]
    man = resolve_intensity({"energy": {"carbon_source": "manual",
                             "carbon_intensity_g_per_kwh": 123}})
    assert man.g_per_kwh == 123 and man.source == "manual"
    dft = resolve_intensity({"energy": {"carbon_region": "NOPE"}})
    assert dft.source == "default"


def test_co2_conversion():
    ci = resolve_intensity(CONFIG)              # SRVC = 380
    assert ci.grams_co2(10.0) == pytest.approx(3800.0)


# ── valuation contract ──────────────────────────────────────────────────
def test_physical_under_allocation(db):
    phys = compute_energy(db, CONFIG, hours=24, cluster_name="test", mode=MODE_PHYSICAL)
    allo = compute_energy(db, CONFIG, hours=24, cluster_name="test", mode=MODE_ALLOCATION)
    # Same consumption regardless of how waste is valued.
    assert phys.consumed_wh == pytest.approx(allo.consumed_wh)
    # Allocation values unused capacity at full TDP -> always >= physical.
    assert allo.waste.total_wh > phys.waste.total_wh
    # Physical carbon waste cannot exceed consumption; allocation may.
    assert phys.waste.total_wh <= phys.consumed_wh


def test_time_overestimation_is_zero_in_physical(db):
    phys = compute_energy(db, CONFIG, hours=24, cluster_name="test", mode=MODE_PHYSICAL)
    allo = compute_energy(db, CONFIG, hours=24, cluster_name="test", mode=MODE_ALLOCATION)
    assert phys.waste.time_overestimation_wh == 0.0
    assert allo.waste.time_overestimation_wh > 0.0
    # The behavioral signal is preserved and identical across modes.
    assert phys.waste.over_request_seconds == allo.waste.over_request_seconds
    assert phys.waste.over_request_seconds > 0


def test_efficiency_bounds(db):
    for mode in (MODE_PHYSICAL, MODE_ALLOCATION):
        s = compute_energy(db, CONFIG, hours=24, cluster_name="test", mode=mode)
        assert 0.0 <= s.efficiency_pct <= 100.0


# ── provenance ────────────────────────────────────────────────────────
def test_provenance_tags(db):
    phys = compute_energy(db, CONFIG, hours=24, cluster_name="test", mode=MODE_PHYSICAL)
    prov = phys.waste.provenance
    assert prov["gpu_idle"].quality == "real"            # DCGM present in fixture
    assert prov["time_overestimation"].quality == "none"  # released, not energy
    assert prov["memory_overalloc"].quality == "partial"
    allo = compute_energy(db, CONFIG, hours=24, cluster_name="test", mode=MODE_ALLOCATION)
    assert allo.waste.provenance["time_overestimation"].quality == "real"


def test_dcgm_idle_detected(db):
    s = compute_energy(db, CONFIG, hours=24, cluster_name="test")
    assert s.dcgm_present is True
    assert s.gpu_consumed_wh > 0.0       # real power integrated
    assert s.waste.gpu_idle_wh > 0.0     # the idle sample is counted


# ── engine ────────────────────────────────────────────────────────────
def test_engine_summary_and_json(db):
    eng = EnergyEngine(db, hours=24, cluster_name="test", config=CONFIG)
    summary = eng.full_summary(explain=True)
    assert "NØMAD Energy" in summary and "Opportunity" in summary
    data = json.loads(eng.to_json())
    assert data["cluster"] == "test"
    assert data["consumed_kwh"] > 0
    assert "provenance" in data


def test_engine_breakdown_and_user(db):
    eng = EnergyEngine(db, hours=24, cluster_name="test", config=CONFIG)
    by_part = eng.breakdown("partition")
    assert set(by_part) == {"compute", "gpu"}
    by_group = eng.breakdown("group")
    assert set(by_group) == {"lab-a", "lab-b"}
    profile = eng.user_profile("alice")
    assert "alice" in profile
    # alice's big time overestimate should surface a wall-time recommendation.
    assert any("wall time" in r for r in eng.recommendations(eng.user_snapshot("alice")))


@pytest.fixture
def db_timeline(tmp_path):
    """A two-week timeline: loose requests before the midpoint, tight after.

    Self-contained (no demo dependency) -- exercises explicit-window and the
    before/after compare without asserting the full intervention realism.
    """
    path = tmp_path / "timeline.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE jobs (job_id TEXT, cluster TEXT, user_name TEXT, partition TEXT,
            node_list TEXT, state TEXT, req_cpus INTEGER, req_mem_mb INTEGER, req_gpus INTEGER,
            req_time_seconds INTEGER, runtime_seconds INTEGER, start_time DATETIME,
            end_time DATETIME, PRIMARY KEY (job_id, cluster));
        CREATE TABLE job_accounting (job_id TEXT, cluster TEXT, username TEXT, account TEXT,
            partition TEXT, state TEXT, elapsed_sec INTEGER, alloc_cpus INTEGER, mem_gb REAL,
            gpu_count INTEGER, cpu_hours REAL, gpu_hours REAL, PRIMARY KEY (job_id, cluster));
        CREATE TABLE gpu_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME,
            node_name TEXT, gpu_index INTEGER, gpu_name TEXT, gpu_util_percent REAL,
            real_util_pct REAL, power_draw_w REAL, workload_class TEXT, data_source TEXT);
    """)
    base = datetime(2026, 4, 1, 0, 0, 0)
    split = base + timedelta(days=7)
    jid = 0
    for day in range(14):
        loose = day < 7
        for _ in range(20):
            jid += 1
            start = base + timedelta(days=day, hours=jid % 12)
            runtime = 3600
            # before: huge over-request + many cores; after: tight + few cores
            req_time = runtime * (4 if loose else 1)
            cpus = 16 if loose else 4
            end = start + timedelta(seconds=runtime)
            conn.execute(
                """INSERT INTO jobs (job_id, cluster, user_name, partition, node_list,
                   state, req_cpus, req_mem_mb, req_gpus, req_time_seconds,
                   runtime_seconds, start_time, end_time)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (str(jid), "test", "alice", "compute", "node01", "COMPLETED",
                 cpus, cpus*4000, 0, req_time, runtime, start.isoformat(), end.isoformat()))
            conn.execute(
                """INSERT INTO job_accounting (job_id, cluster, username, account,
                   partition, state, elapsed_sec, alloc_cpus, gpu_count)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (str(jid), "test", "alice", "lab", "compute", "COMPLETED", runtime, cpus, 0))
    conn.commit(); conn.close()
    return str(path), base, split, base + timedelta(days=14)


def test_explicit_window_isolates_period(db_timeline):
    path, start, split, end = db_timeline
    pre = compute_energy(path, CONFIG, cluster_name="test", mode=MODE_PHYSICAL,
                         window=(start, split))
    post = compute_energy(path, CONFIG, cluster_name="test", mode=MODE_PHYSICAL,
                          window=(split, end))
    assert pre.n_jobs > 0 and post.n_jobs > 0
    # before half over-requests and over-allocates -> more recoverable energy
    assert pre.waste.total_wh > post.waste.total_wh
    assert pre.waste.over_request_seconds > post.waste.over_request_seconds


def test_compare_renders_reduction(db_timeline):
    path, start, split, end = db_timeline
    eng = EnergyEngine(path, cluster_name="test", config=CONFIG, mode=MODE_PHYSICAL)
    out = eng.compare()                       # midpoint split == our split by symmetry
    assert "Before / After" in out
    assert "Outcome" in out and "down" in out
    pre, post, sp = eng.compare_periods()
    assert post.waste.total_wh < pre.waste.total_wh


# ── forecast trend fit ────────────────────────────────────────────────
def test_trend_slope_sign():
    from nomad.energy.forecast import build_trend
    rising = build_trend("consumed", [10, 12, 14, 16, 18, 20], bucket_days=7)
    falling = build_trend("recoverable", [20, 17, 14, 11, 8, 5], bucket_days=7)
    assert rising.slope_per_day > 0
    assert falling.slope_per_day < 0
    assert rising.growth_pct(30) > 0 and falling.growth_pct(30) < 0


def test_r_squared_discriminates():
    from nomad.energy.forecast import build_trend
    clean = build_trend("consumed", [10, 12, 14, 16, 18, 20, 22, 24], bucket_days=7)
    noisy = build_trend("recoverable", [10, 2, 14, 3, 12, 1, 13, 4], bucket_days=7)
    assert clean.r_squared > 0.95 and clean.fit_quality() == "strong"
    assert noisy.r_squared < 0.4 and noisy.fit_quality() == "weak"


def test_forecast_engine_output(db_timeline):
    path, start, split, end = db_timeline
    eng = EnergyEngine(path, cluster_name="test", config=CONFIG, mode=MODE_PHYSICAL)
    out = eng.forecast_report("quarter")
    assert "Forecast" in out and "R\u00b2" in out
    c, r, label, hdays = eng.forecast()
    assert c is not None and 0.0 <= c.r_squared <= 1.0

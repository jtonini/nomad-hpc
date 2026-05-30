# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Tests for the NØMAD System Dynamics module.

Creates a temporary demo database with realistic synthetic data
and validates all dynamics computations.
"""
from __future__ import annotations

import json
import math
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest


# ── Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def demo_db(tmp_path):
    """Create a temporary database with synthetic HPC data."""
    db_path = tmp_path / "test_dynamics.db"
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()

    # ── Schema ────────────────────────────────────────────────────
    c.execute("""CREATE TABLE nodes (
        hostname TEXT PRIMARY KEY, cluster TEXT, partition TEXT, status TEXT,
        cpu_count INTEGER, gpu_count INTEGER, memory_mb INTEGER, last_seen DATETIME
    )""")

    c.execute("""CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY, user_name TEXT, partition TEXT, node_list TEXT,
        job_name TEXT, state TEXT, exit_code INTEGER, exit_signal INTEGER,
        failure_reason INTEGER, submit_time DATETIME, start_time DATETIME,
        end_time DATETIME, req_cpus INTEGER, req_mem_mb INTEGER, req_gpus INTEGER,
        req_time_seconds INTEGER, runtime_seconds INTEGER, wait_time_seconds INTEGER
    )""")

    c.execute("""CREATE TABLE group_membership (
        username TEXT, group_name TEXT, gid INTEGER, cluster TEXT,
        PRIMARY KEY (username, group_name, cluster)
    )""")

    c.execute("""CREATE TABLE node_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL,
        node_name TEXT NOT NULL, state TEXT, cpus_total INTEGER, cpus_alloc INTEGER,
        cpu_load REAL, memory_total_mb INTEGER, memory_alloc_mb INTEGER,
        memory_free_mb INTEGER, cpu_alloc_percent REAL,
        memory_alloc_percent REAL, cluster TEXT DEFAULT 'test',
        partitions TEXT, reason TEXT, features TEXT, gres TEXT,
        is_healthy INTEGER
    )""")

    c.execute("""CREATE TABLE gpu_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME,
        node_name TEXT, gpu_index INTEGER, gpu_name TEXT,
        gpu_util_percent REAL, memory_util_percent REAL, memory_used_mb INTEGER,
        memory_total_mb INTEGER, temperature_c REAL, power_draw_w REAL
    )""")

    c.execute("""CREATE TABLE queue_state (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL,
        partition TEXT NOT NULL,
        pending_jobs INTEGER DEFAULT 0, running_jobs INTEGER DEFAULT 0, total_jobs INTEGER DEFAULT 0
    )""")

    c.execute("""CREATE TABLE iostat_device (
        id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL,
        device TEXT NOT NULL, read_kb_per_sec REAL, write_kb_per_sec REAL,
        read_await_ms REAL, write_await_ms REAL, util_percent REAL
    )""")

    # ── Seed data ─────────────────────────────────────────────────
    now = datetime.now()

    # Nodes
    nodes = [
        ("node01", "test", "compute", "idle", 64, 0, 256000),
        ("node02", "test", "compute", "allocated", 64, 0, 256000),
        ("node03", "test", "gpu", "allocated", 32, 4, 128000),
        ("node04", "test", "gpu", "mixed", 32, 4, 128000),
        ("node05", "test", "highmem", "idle", 48, 0, 512000),
    ]
    for hostname, cluster, partition, status, cpus, gpus, mem in nodes:
        c.execute(
            "INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (hostname, cluster, partition, status, cpus, gpus, mem,
             now.isoformat()),
        )

    # Groups
    groups = [
        ("alice", "chem_sim", 1001, "test"),
        ("bob", "chem_sim", 1001, "test"),
        ("carol", "ml_team", 1002, "test"),
        ("dave", "ml_team", 1002, "test"),
        ("eve", "genomics", 1003, "test"),
        ("frank", "genomics", 1003, "test"),
        ("grace", "physics", 1004, "test"),
    ]
    for username, group_name, gid, cluster in groups:
        c.execute(
            "INSERT INTO group_membership VALUES (?, ?, ?, ?)",
            (username, group_name, gid, cluster),
        )

    # Jobs — distributed across groups with different profiles
    import random
    random.seed(42)

    job_profiles = {
        "alice": {"partition": "compute", "cpus": (16, 64), "mem": (32000, 128000), "gpus": 0, "runtime": (3600, 28800)},
        "bob": {"partition": "compute", "cpus": (8, 32), "mem": (16000, 64000), "gpus": 0, "runtime": (1800, 14400)},
        "carol": {"partition": "gpu", "cpus": (8, 16), "mem": (32000, 64000), "gpus": 4, "runtime": (7200, 86400)},
        "dave": {"partition": "gpu", "cpus": (4, 8), "mem": (16000, 32000), "gpus": 2, "runtime": (3600, 43200)},
        "eve": {"partition": "compute", "cpus": (32, 64), "mem": (64000, 256000), "gpus": 0, "runtime": (14400, 86400)},
        "frank": {"partition": "compute", "cpus": (16, 48), "mem": (32000, 128000), "gpus": 0, "runtime": (7200, 43200)},
        "grace": {"partition": "highmem", "cpus": (4, 16), "mem": (128000, 512000), "gpus": 0, "runtime": (1800, 7200)},
    }

    job_id = 1000
    for day_offset in range(7):
        for user, profile in job_profiles.items():
            n_jobs = random.randint(5, 15)
            for _ in range(n_jobs):
                job_id += 1
                submit = now - timedelta(
                    days=day_offset,
                    hours=random.randint(0, 23),
                    minutes=random.randint(0, 59),
                )
                cpus = random.randint(*profile["cpus"])
                mem = random.randint(*profile["mem"])
                runtime = random.randint(*profile["runtime"])
                wait = random.randint(10, 600)

                # Failure rate: higher for genomics during heavy periods
                fail_prob = 0.1
                if user in ("eve", "frank") and day_offset < 3:
                    fail_prob = 0.25
                state = "FAILED" if random.random() < fail_prob else "COMPLETED"

                c.execute(
                    """INSERT INTO jobs VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (str(job_id), user, profile["partition"],
                     f"node0{random.randint(1,5)}",
                     f"{user}_job_{job_id}", state,
                     0 if state == "COMPLETED" else 1,
                     0, 0 if state == "COMPLETED" else 1,
                     submit.isoformat(),
                     (submit + timedelta(seconds=wait)).isoformat(),
                     (submit + timedelta(seconds=wait + runtime)).isoformat(),
                     cpus, mem, profile["gpus"],
                     runtime + 3600, runtime, wait),
                )

    # Node state — with a simulated failure event
    for hour_offset in range(168):
        ts = now - timedelta(hours=hour_offset)
        for hostname in ["node01", "node02", "node03", "node04", "node05"]:
            # Simulate node03 failure at hour 48-52
            is_healthy = 1
            if hostname == "node03" and 48 <= hour_offset <= 52:
                is_healthy = 0

            cpu_alloc = random.uniform(30, 90) if is_healthy else 0
            mem_alloc = random.uniform(20, 80) if is_healthy else 0

            c.execute(
                """INSERT INTO node_state
                (timestamp, node_name, state, cpu_alloc_percent,
                 memory_alloc_percent, is_healthy)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (ts.isoformat(), hostname,
                 "idle" if not is_healthy else "allocated",
                 cpu_alloc, mem_alloc, is_healthy),
            )

    # GPU stats
    for hour_offset in range(168):
        ts = now - timedelta(hours=hour_offset)
        for hostname in ["node03", "node04"]:
            for gpu_id in range(4):
                util = random.uniform(40, 95)
                c.execute(
                    """INSERT INTO gpu_stats
                    (timestamp, node_name, gpu_index, gpu_name,
                     gpu_util_percent, memory_util_percent, memory_used_mb, memory_total_mb,
                     temperature_c, power_draw_w)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (ts.isoformat(), hostname, gpu_id, "RTX 6000 Ada",
                     util, util * 0.8, int(util * 490), 49152,
                     random.uniform(55, 80), random.uniform(100, 300)),
                )

    # Queue state
    for hour_offset in range(168):
        ts = now - timedelta(hours=hour_offset)
        running = random.randint(20, 60)
        pending = random.randint(5, 40)
        c.execute(
            "INSERT INTO queue_state (timestamp, partition, pending_jobs, running_jobs, total_jobs) VALUES (?, ?, ?, ?, ?)",
            (ts.isoformat(), "compute", pending, running, running + pending),
        )

    # I/O stats
    for hour_offset in range(168):
        ts = now - timedelta(hours=hour_offset)
        for hostname in ["node01", "node02", "node03"]:
            c.execute(
                """INSERT INTO iostat_device
                (timestamp, device, read_kb_per_sec, write_kb_per_sec,
                 read_await_ms, write_await_ms, util_percent)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (ts.isoformat(), "sda",
                 random.uniform(100, 5000), random.uniform(50, 2000),
                 random.uniform(1, 15), random.uniform(1, 25),
                 random.uniform(10, 85)),
            )

    conn.commit()
    conn.close()
    return db_path


# ── Diversity tests ───────────────────────────────────────────────────

class TestDiversity:
    def test_compute_diversity_returns_result(self, demo_db):
        from nomad.dynamics.diversity import compute_diversity
        result = compute_diversity(demo_db, dimension="group", hours=168)
        assert result is not None
        assert result.by_dimension == "group"
        assert result.current.richness > 0

    def test_shannon_entropy_range(self, demo_db):
        from nomad.dynamics.diversity import compute_diversity
        result = compute_diversity(demo_db)
        h = result.current.shannon_h
        assert h >= 0, "Shannon entropy must be non-negative"
        assert h <= math.log(result.current.richness) + 0.01, "H' must be <= ln(S)"

    def test_simpson_index_range(self, demo_db):
        from nomad.dynamics.diversity import compute_diversity
        result = compute_diversity(demo_db)
        d = result.current.simpson_d
        assert 0.0 <= d <= 1.0, f"Simpson D must be in [0,1], got {d}"

    def test_evenness_range(self, demo_db):
        from nomad.dynamics.diversity import compute_diversity
        result = compute_diversity(demo_db)
        j = result.current.evenness_j
        assert 0.0 <= j <= 1.0, f"Evenness J must be in [0,1], got {j}"

    def test_diversity_by_partition(self, demo_db):
        from nomad.dynamics.diversity import compute_diversity
        result = compute_diversity(demo_db, dimension="partition")
        assert result.by_dimension == "partition"
        assert result.current.richness > 0

    def test_diversity_by_user(self, demo_db):
        from nomad.dynamics.diversity import compute_diversity
        result = compute_diversity(demo_db, dimension="user")
        assert result.by_dimension == "user"

    def test_trend_computed(self, demo_db):
        from nomad.dynamics.diversity import compute_diversity
        result = compute_diversity(demo_db, n_windows=6)
        assert len(result.trend) > 0
        assert result.trend_direction in ("increasing", "decreasing", "stable")

    def test_category_counts_sum(self, demo_db):
        from nomad.dynamics.diversity import compute_diversity
        result = compute_diversity(demo_db)
        total = sum(result.current.category_counts.values())
        assert total > 0, "Should have counted some jobs"

    def test_dominant_category_exists(self, demo_db):
        from nomad.dynamics.diversity import compute_diversity
        result = compute_diversity(demo_db)
        assert result.current.dominant_category != ""
        assert result.current.dominant_proportion > 0

    def test_pure_diversity_computation(self):
        """Test the core computation with known values."""
        from nomad.dynamics.diversity import _compute_diversity
        # Equal distribution: H' should be ln(3) ≈ 1.099
        h, d, j = _compute_diversity({"a": 100, "b": 100, "c": 100})
        assert abs(h - math.log(3)) < 0.001
        assert abs(j - 1.0) < 0.001
        assert abs(d - (1 - 3 * (1/3)**2)) < 0.001

    def test_single_category(self):
        from nomad.dynamics.diversity import _compute_diversity
        h, d, j = _compute_diversity({"monopoly": 1000})
        assert h == 0.0
        assert d == 0.0

    def test_empty_counts(self):
        from nomad.dynamics.diversity import _compute_diversity
        h, d, j = _compute_diversity({})
        assert h == 0.0 and d == 0.0 and j == 0.0


# ── Niche overlap tests ──────────────────────────────────────────────

class TestNiche:
    def test_compute_niche_returns_result(self, demo_db):
        from nomad.dynamics.niche import compute_niche_overlap
        result = compute_niche_overlap(demo_db, hours=168)
        assert result is not None
        assert len(result.profiles) > 0

    def test_overlap_symmetry(self, demo_db):
        from nomad.dynamics.niche import compute_niche_overlap
        result = compute_niche_overlap(demo_db)
        for (a, b), val in result.overlap_matrix.items():
            if (b, a) in result.overlap_matrix:
                assert abs(val - result.overlap_matrix[(b, a)]) < 0.001, \
                    f"Overlap must be symmetric: O({a},{b}) != O({b},{a})"

    def test_overlap_range(self, demo_db):
        from nomad.dynamics.niche import compute_niche_overlap
        result = compute_niche_overlap(demo_db)
        for (a, b), val in result.overlap_matrix.items():
            assert 0.0 <= val <= 1.0 + 0.001, \
                f"Overlap must be in [0,1], got {val} for ({a},{b})"

    def test_pianka_known_values(self):
        from nomad.dynamics.niche import _pianka_overlap
        # Identical proportions → overlap = 1.0
        p = {"a": 0.5, "b": 0.3, "c": 0.2}
        assert abs(_pianka_overlap(p, p) - 1.0) < 0.001

        # Orthogonal proportions → overlap = 0.0
        p1 = {"a": 1.0, "b": 0.0}
        p2 = {"a": 0.0, "b": 1.0}
        assert abs(_pianka_overlap(p1, p2)) < 0.001

    def test_profiles_have_proportions(self, demo_db):
        from nomad.dynamics.niche import compute_niche_overlap
        result = compute_niche_overlap(demo_db)
        for p in result.profiles:
            assert len(p.proportions) > 0
            assert p.job_count > 0


# ── Carrying capacity tests ──────────────────────────────────────────

class TestCapacity:
    def test_compute_capacity_returns_result(self, demo_db):
        from nomad.dynamics.capacity import compute_capacity
        result = compute_capacity(demo_db, hours=168)
        assert result is not None

    def test_binding_constraint_identified(self, demo_db):
        from nomad.dynamics.capacity import compute_capacity
        result = compute_capacity(demo_db)
        if result.dimensions:
            assert result.binding_constraint is not None
            assert result.binding_constraint.is_binding

    def test_utilization_range(self, demo_db):
        from nomad.dynamics.capacity import compute_capacity
        result = compute_capacity(demo_db)
        for d in result.dimensions:
            assert 0.0 <= d.current_utilization <= 1.0 + 0.01, \
                f"{d.label} utilization out of range: {d.current_utilization}"

    def test_pressure_levels(self, demo_db):
        from nomad.dynamics.capacity import compute_capacity
        result = compute_capacity(demo_db)
        assert result.overall_pressure in ("low", "moderate", "high", "critical")

    def test_saturation_projection(self):
        from nomad.dynamics.capacity import _project_saturation
        # 50% utilization growing at 1%/hr → 50 hours
        hours = _project_saturation(0.5, 0.01)
        assert hours is not None
        assert abs(hours - 50.0) < 0.1

        # Decreasing → None
        assert _project_saturation(0.5, -0.01) is None

        # Very slow growth → None (> 720h)
        assert _project_saturation(0.5, 0.0001) is None


# ── Resilience tests ─────────────────────────────────────────────────

class TestResilience:
    def test_compute_resilience_returns_result(self, demo_db):
        from nomad.dynamics.resilience import compute_resilience
        result = compute_resilience(demo_db, hours=720)
        assert result is not None

    def test_score_range(self, demo_db):
        from nomad.dynamics.resilience import compute_resilience
        result = compute_resilience(demo_db)
        assert 0.0 <= result.resilience_score <= 100.0

    def test_detects_node_failure(self, demo_db):
        from nomad.dynamics.resilience import compute_resilience
        result = compute_resilience(demo_db)
        node_failures = [d for d in result.disturbances if d.event_type == "node_failure"]
        assert len(node_failures) > 0, "Should detect the simulated node03 failure"

    def test_trend_values(self, demo_db):
        from nomad.dynamics.resilience import compute_resilience
        result = compute_resilience(demo_db)
        assert result.resilience_trend in ("improving", "degrading", "stable")


# ── Externality tests ────────────────────────────────────────────────

class TestExternality:
    def test_compute_externalities_returns_result(self, demo_db):
        from nomad.dynamics.externality import compute_externalities
        result = compute_externalities(demo_db, hours=168)
        assert result is not None

    def test_edge_impact_range(self, demo_db):
        from nomad.dynamics.externality import compute_externalities
        result = compute_externalities(demo_db)
        for edge in result.edges:
            assert 0.0 <= edge.impact_score <= 1.0

    def test_net_scores_balance(self, demo_db):
        from nomad.dynamics.externality import compute_externalities
        result = compute_externalities(demo_db)
        if result.group_profiles:
            # Net scores should roughly sum to zero (what's imposed = what's received)
            total_imposed = sum(p.imposed_score for p in result.group_profiles)
            total_received = sum(p.received_score for p in result.group_profiles)
            assert abs(total_imposed - total_received) < 0.1

    def test_temporal_correlation_known(self):
        from nomad.dynamics.externality import _temporal_correlation
        # Perfect positive correlation
        source = [("h0", 1), ("h1", 2), ("h2", 3), ("h3", 4), ("h4", 5)]
        target = [("h0", 10), ("h1", 20), ("h2", 30), ("h3", 40), ("h4", 50)]
        corr = _temporal_correlation(source, target)
        assert abs(corr - 1.0) < 0.001

        # No correlation (constant target)
        target_flat = [("h0", 5), ("h1", 5), ("h2", 5), ("h3", 5), ("h4", 5)]
        corr_flat = _temporal_correlation(source, target_flat)
        assert abs(corr_flat) < 0.001


# ── Engine tests ─────────────────────────────────────────────────────

class TestEngine:
    def test_engine_creates(self, demo_db):
        from nomad.dynamics.engine import DynamicsEngine
        engine = DynamicsEngine(demo_db)
        assert engine is not None

    def test_full_summary_produces_output(self, demo_db):
        from nomad.dynamics.engine import DynamicsEngine
        engine = DynamicsEngine(demo_db, cluster_name="test-cluster")
        output = engine.full_summary()
        assert "NOMAD System Dynamics Report" in output
        assert "test-cluster" in output
        assert "Diversity" in output
        assert "Capacity" in output

    def test_json_output_valid(self, demo_db):
        from nomad.dynamics.engine import DynamicsEngine
        engine = DynamicsEngine(demo_db)
        j = engine.to_json()
        data = json.loads(j)
        assert "diversity" in data
        assert "niche" in data
        assert "capacity" in data
        assert "resilience" in data
        assert "externality" in data

    def test_individual_reports(self, demo_db):
        from nomad.dynamics.engine import DynamicsEngine
        engine = DynamicsEngine(demo_db)
        assert "Diversity" in engine.diversity_report()
        assert "Niche" in engine.niche_report()
        assert "Capacity" in engine.capacity_report()
        assert "Resilience" in engine.resilience_report()
        assert "Externalities" in engine.externality_report()

    def test_lazy_computation(self, demo_db):
        from nomad.dynamics.engine import DynamicsEngine
        engine = DynamicsEngine(demo_db)
        # Nothing computed yet
        assert engine._diversity is None
        assert engine._niche is None
        # Access triggers computation
        _ = engine.diversity
        assert engine._diversity is not None
        # Others still lazy
        assert engine._niche is None


# ── Formatter tests ──────────────────────────────────────────────────

class TestFormatters:
    def test_diversity_cli_format(self, demo_db):
        from nomad.dynamics.diversity import compute_diversity
        from nomad.dynamics.formatters import format_diversity_cli
        result = compute_diversity(demo_db)
        output = format_diversity_cli(result)
        assert "Shannon" in output
        assert "Simpson" in output

    def test_diversity_json_format(self, demo_db):
        from nomad.dynamics.diversity import compute_diversity
        from nomad.dynamics.formatters import format_diversity_json
        result = compute_diversity(demo_db)
        data = format_diversity_json(result)
        assert "shannon_h" in data["current"]
        assert isinstance(data["current"]["category_counts"], dict)

    def test_niche_cli_format(self, demo_db):
        from nomad.dynamics.niche import compute_niche_overlap
        from nomad.dynamics.formatters import format_niche_cli
        result = compute_niche_overlap(demo_db)
        output = format_niche_cli(result)
        assert "Niche" in output
        assert "Resource profiles" in output

    def test_capacity_cli_format(self, demo_db):
        from nomad.dynamics.capacity import compute_capacity
        from nomad.dynamics.formatters import format_capacity_cli
        result = compute_capacity(demo_db)
        output = format_capacity_cli(result)
        assert "Capacity" in output

    def test_resilience_cli_format(self, demo_db):
        from nomad.dynamics.resilience import compute_resilience
        from nomad.dynamics.formatters import format_resilience_cli
        result = compute_resilience(demo_db)
        output = format_resilience_cli(result)
        assert "Resilience" in output

    def test_externality_cli_format(self, demo_db):
        from nomad.dynamics.externality import compute_externalities
        from nomad.dynamics.formatters import format_externality_cli
        result = compute_externalities(demo_db)
        output = format_externality_cli(result)
        assert "Externalities" in output

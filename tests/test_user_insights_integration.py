# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Integration tests for user_insights() against a synthetic SQLite DB.

These are the first tests to exercise the full DB path through
user_insights(): job loading, session loading, verdict building, and
the Option-1 ordering (verdict leads when present). Unit-level coverage
of the individual pieces lives in test_edu_insights.py,
test_workstation_scoring.py, and test_workstation_verdict.py.

Cluster capacities are passed via the override parameter rather than
seeding node_state, keeping each test focused on the session→verdict
path without depending on node_state schema.
"""
from __future__ import annotations

import sqlite3
import pytest

from nomad.edu.insights import user_insights


# Site-agnostic synthetic cluster tiers (mirrors realistic spydur layout).
CLUSTER_TIERS = [
    {"cluster": "primary", "memory_mb": 384_000, "memory_gb": 375.0,
     "node_count": 16, "partitions": "basic"},
    {"cluster": "primary", "memory_mb": 768_000, "memory_gb": 750.0,
     "node_count": 8, "partitions": "medium"},
    {"cluster": "primary", "memory_mb": 1_536_000, "memory_gb": 1500.0,
     "node_count": 2, "partitions": "large"},
]


def _build_db(path, *, with_jobs=False, with_sessions=False):
    """Create a synthetic NØMAD DB with optional job and session data."""
    con = sqlite3.connect(path)
    c = con.cursor()

    # ── Workstation tables ──
    c.execute("""
        CREATE TABLE workstation_state (
            id INTEGER PRIMARY KEY,
            timestamp DATETIME,
            hostname TEXT,
            memory_total_mb INTEGER,
            cpu_count INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE workstation_user_snapshot (
            id INTEGER PRIMARY KEY,
            timestamp DATETIME,
            hostname TEXT,
            username TEXT,
            uid INTEGER,
            session_epoch INTEGER,
            memory_peak_bytes INTEGER,
            cpu_usage_usec INTEGER
        )
    """)

    # ── Job tables ──
    c.execute("""
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            user_name TEXT,
            partition TEXT,
            node_list TEXT,
            job_name TEXT,
            state TEXT,
            exit_code INTEGER,
            exit_signal INTEGER,
            failure_reason TEXT,
            submit_time TEXT,
            start_time TEXT,
            end_time TEXT,
            req_cpus INTEGER,
            req_mem_mb INTEGER,
            req_gpus INTEGER,
            req_time_seconds INTEGER,
            runtime_seconds INTEGER,
            wait_time_seconds INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE job_summary (
            job_id TEXT PRIMARY KEY,
            peak_cpu_percent REAL,
            peak_memory_gb REAL,
            avg_cpu_percent REAL,
            avg_memory_gb REAL,
            avg_io_wait_percent REAL,
            total_nfs_read_gb REAL,
            total_nfs_write_gb REAL,
            total_local_read_gb REAL,
            total_local_write_gb REAL,
            nfs_ratio REAL,
            used_gpu INTEGER,
            avg_gpu_util REAL,
            health_score REAL
        )
    """)

    if with_sessions:
        # One host: 62 GB, 32 cores (small Parish-tier workstation).
        c.execute(
            "INSERT INTO workstation_state (timestamp, hostname, memory_total_mb, cpu_count) "
            "VALUES (datetime('now'), 'wks1', 63642, 32)"
        )
        # Two qualifying sessions: ~60 GB peak (97% of host), ~20h span each.
        # span_hours derived from first/last snapshot timestamps sharing a session_epoch.
        peak = int(60 * 1024 * 1024 * 1024)
        # Each session: a start snapshot and an end snapshot sharing one
        # session_epoch. span_hours is derived from their timestamp gap, so
        # SQLite modifiers must be SEPARATE args ('-3 days','+20 hours'),
        # not a single combined string (which SQLite silently mis-parses).
        sessions = [
            (1001, "-3 days", "+20 hours"),  # ~20h span
            (1002, "-2 days", "+18 hours"),  # ~18h span
        ]
        for epoch, day_off, hour_off in sessions:
            # first snapshot (session start)
            c.execute(
                "INSERT INTO workstation_user_snapshot "
                "(timestamp, hostname, username, uid, session_epoch, memory_peak_bytes, cpu_usage_usec) "
                "VALUES (datetime('now', ?), 'wks1', 'alice', 1001, ?, ?, 1000000)",
                (day_off, epoch, peak),
            )
            # last snapshot — same epoch, start + hour_off → defines span
            c.execute(
                "INSERT INTO workstation_user_snapshot "
                "(timestamp, hostname, username, uid, session_epoch, memory_peak_bytes, cpu_usage_usec) "
                "VALUES (datetime('now', ?, ?), 'wks1', 'alice', 1001, ?, ?, 2000000)",
                (day_off, hour_off, epoch, peak),
            )

    if with_jobs:
        # A wasteful job: 32 cores requested, ~3% used → CPU dimension issue.
        for jid in ("j1", "j2", "j3"):
            c.execute(
                "INSERT INTO jobs (job_id, user_name, state, end_time, "
                "req_cpus, req_mem_mb, req_gpus, req_time_seconds, runtime_seconds, wait_time_seconds) "
                "VALUES (?, 'alice', 'COMPLETED', datetime('now','-1 day'), 32, 8000, 0, 36000, 3600, 60)",
                (jid,),
            )
            c.execute(
                "INSERT INTO job_summary (job_id, peak_cpu_percent, avg_cpu_percent, "
                "peak_memory_gb, avg_memory_gb, avg_io_wait_percent, nfs_ratio, used_gpu, health_score) "
                "VALUES (?, 4.0, 3.0, 2.0, 1.5, 1.0, 0.1, 0, 50.0)",
                (jid,),
            )

    con.commit()
    con.close()


def test_verdict_leads_when_both_jobs_and_sessions(tmp_path):
    """Jobs (wasteful CPU) + sessions (memory+duration) → verdict is issues[0]."""
    db = str(tmp_path / "both.db")
    _build_db(db, with_jobs=True, with_sessions=True)
    ui = user_insights(db, "alice", cluster_capacities=CLUSTER_TIERS)
    assert ui.job_count == 3
    assert ui.session_count == 2
    assert len(ui.issues) >= 2
    assert ui.issues[0].kind == "verdict"
    assert ui.issues[0].context["verdict"] == "promote"
    # at least one dimension issue follows
    assert any(i.kind == "dimension" for i in ui.issues[1:])


def test_workstation_only_user_gets_verdict(tmp_path):
    """Sessions but no jobs → verdict present, job_count 0."""
    db = str(tmp_path / "wks_only.db")
    _build_db(db, with_jobs=False, with_sessions=True)
    ui = user_insights(db, "alice", cluster_capacities=CLUSTER_TIERS)
    assert ui.job_count == 0
    assert ui.session_count == 2
    assert len(ui.issues) == 1
    assert ui.issues[0].kind == "verdict"


def test_cluster_only_user_no_verdict(tmp_path):
    """Jobs but no sessions → no verdict, dimension issues only."""
    db = str(tmp_path / "cluster_only.db")
    _build_db(db, with_jobs=True, with_sessions=False)
    ui = user_insights(db, "alice", cluster_capacities=CLUSTER_TIERS)
    assert ui.job_count == 3
    assert ui.session_count == 0
    assert all(i.kind == "dimension" for i in ui.issues)


def test_empty_user_returns_clean(tmp_path):
    """Neither jobs nor sessions → empty issues, no crash."""
    db = str(tmp_path / "empty.db")
    _build_db(db, with_jobs=False, with_sessions=False)
    ui = user_insights(db, "nobody", cluster_capacities=CLUSTER_TIERS)
    assert ui.job_count == 0
    assert ui.session_count == 0
    assert ui.issues == []


def test_cluster_capacities_override_is_used(tmp_path):
    """Passing cluster_capacities should drive verdict target selection
    without touching node_state."""
    db = str(tmp_path / "override.db")
    _build_db(db, with_jobs=False, with_sessions=True)
    # Tiny cluster — only a 256 GB tier. 60 GB peak → 256/60 = 4.3x → promote
    # to 256 GB tier specifically (proves our list, not a DB lookup, was used).
    small_cluster = [
        {"cluster": "tiny", "memory_mb": 256_000, "memory_gb": 250.0,
         "node_count": 2, "partitions": "all"},
    ]
    ui = user_insights(db, "alice", cluster_capacities=small_cluster)
    assert ui.issues[0].kind == "verdict"
    assert ui.issues[0].context["verdict"] == "promote"
    assert ui.issues[0].context["target_memory_gb"] == 250.0


# ── Per-(cluster, partition) dimension scoping ───────────────────────

def _build_scoped_db(path):
    """Synthetic DB where one user runs wasteful jobs across two distinct
    (cluster, partition) groups, to exercise per-(cluster,partition)
    dimension aggregation. Includes the cluster column the production
    schema has (the older _build_db fixture predates it)."""
    con = sqlite3.connect(path)
    c = con.cursor()
    c.execute("""
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            cluster TEXT,
            user_name TEXT,
            partition TEXT,
            node_list TEXT,
            job_name TEXT,
            state TEXT,
            exit_code INTEGER,
            exit_signal INTEGER,
            failure_reason TEXT,
            submit_time TEXT,
            start_time TEXT,
            end_time TEXT,
            req_cpus INTEGER,
            req_mem_mb INTEGER,
            req_gpus INTEGER,
            req_time_seconds INTEGER,
            runtime_seconds INTEGER,
            wait_time_seconds INTEGER
        )
    """)
    c.execute("""
        CREATE TABLE job_summary (
            job_id TEXT PRIMARY KEY,
            cluster TEXT,
            peak_cpu_percent REAL,
            peak_memory_gb REAL,
            avg_cpu_percent REAL,
            avg_memory_gb REAL,
            avg_io_wait_percent REAL,
            total_nfs_read_gb REAL,
            total_nfs_write_gb REAL,
            total_local_read_gb REAL,
            total_local_write_gb REAL,
            nfs_ratio REAL,
            used_gpu INTEGER,
            avg_gpu_util REAL,
            health_score REAL
        )
    """)
    # Workstation tables must exist (user_insights loads sessions unconditionally).
    c.execute("CREATE TABLE workstation_state (id INTEGER PRIMARY KEY, timestamp DATETIME, "
              "hostname TEXT, memory_total_mb INTEGER, cpu_count INTEGER)")
    c.execute("CREATE TABLE workstation_user_snapshot (id INTEGER PRIMARY KEY, timestamp DATETIME, "
              "hostname TEXT, username TEXT, uid INTEGER, session_epoch INTEGER, "
              "memory_peak_bytes INTEGER, cpu_usage_usec INTEGER)")

    def add_job(jid, cluster, partition, avg_cpu):
        c.execute(
            "INSERT INTO jobs (job_id, cluster, user_name, partition, state, end_time, "
            "req_cpus, req_mem_mb, req_gpus, req_time_seconds, runtime_seconds, wait_time_seconds) "
            "VALUES (?, ?, 'bob', ?, 'COMPLETED', datetime('now','-1 day'), "
            "32, 8000, 0, 36000, 3600, 60)",
            (jid, cluster, partition),
        )
        c.execute(
            "INSERT INTO job_summary (job_id, cluster, peak_cpu_percent, avg_cpu_percent, "
            "peak_memory_gb, avg_memory_gb, avg_io_wait_percent, nfs_ratio, used_gpu, health_score) "
            "VALUES (?, ?, ?, ?, 2.0, 1.5, 1.0, 0.1, 0, 40.0)",
            (jid, cluster, avg_cpu + 2, avg_cpu),
        )

    # Group 1: clusterA/compute — 5 jobs, catastrophic CPU (3% of 32 cores).
    for i in range(5):
        add_job(f"a{i}", "clusterA", "compute", 3.0)
    # Group 2: clusterB/gpu — 4 jobs, milder CPU waste (28%).
    for i in range(4):
        add_job(f"b{i}", "clusterB", "gpu", 28.0)

    con.commit()
    con.close()


def test_dimension_issues_scoped_per_cluster_partition(tmp_path):
    """A user spanning two (cluster, partition) groups gets SEPARATE scoped
    CPU issues, not one blended issue — and severity is not diluted across
    groups."""
    db = str(tmp_path / "scoped.db")
    _build_scoped_db(db)
    ui = user_insights(db, "bob", cluster_capacities=CLUSTER_TIERS)

    cpu_issues = [i for i in ui.issues if i.kind == "dimension" and i.dimension_key == "cpu"]
    # Two distinct CPU issues, one per (cluster, partition) — not one blended.
    assert len(cpu_issues) == 2, f"expected 2 scoped CPU issues, got {len(cpu_issues)}"

    by_scope = {(i.cluster, i.partition): i for i in cpu_issues}
    assert ("clusterA", "compute") in by_scope
    assert ("clusterB", "gpu") in by_scope

    # The clusterA/compute group is the disaster: critical, all 5 jobs, not
    # diluted by the milder clusterB jobs.
    a = by_scope[("clusterA", "compute")]
    assert a.severity == "critical"
    assert a.affected_jobs == 5
    assert a.total_applicable == 5

    # The clusterB/gpu group is scoped separately with its own job count.
    b = by_scope[("clusterB", "gpu")]
    assert b.affected_jobs == 4
    assert b.total_applicable == 4

    # Anti-regression: no dimension issue falls back to unscoped for a
    # multi-cluster user.
    for i in ui.issues:
        if i.kind == "dimension":
            assert i.cluster is not None and i.partition is not None


def test_single_cluster_user_scoped_to_one_group(tmp_path):
    """A single-(cluster, partition) user still produces correctly-scoped
    issues — one group, stamped, not None."""
    db = str(tmp_path / "single.db")
    con = sqlite3.connect(db)
    _build_scoped_db(db)  # reuse schema; then narrow to one group
    con = sqlite3.connect(db)
    con.execute("DELETE FROM jobs WHERE cluster = 'clusterB'")
    con.execute("DELETE FROM job_summary WHERE cluster = 'clusterB'")
    con.commit()
    con.close()
    ui = user_insights(db, "bob", cluster_capacities=CLUSTER_TIERS)
    cpu_issues = [i for i in ui.issues if i.kind == "dimension" and i.dimension_key == "cpu"]
    assert len(cpu_issues) == 1
    assert cpu_issues[0].cluster == "clusterA"
    assert cpu_issues[0].partition == "compute"
    assert cpu_issues[0].severity == "critical"

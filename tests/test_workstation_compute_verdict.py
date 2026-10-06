# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Long, CPU-heavy workstation sessions get advice to use the cluster.

The workstation verdict looked at memory only: a three-day, 14-core
simulation at a fraction of the workstation's memory got no advice at all.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from nomad.edu.insights import _build_cluster_promotion_verdict, user_insights
from nomad.edu.progress import _load_user_sessions
from nomad.edu.scoring import score_session, session_busy_cores

GB = 1024 ** 3
TIERS = [
    {"cluster": "primary", "memory_mb": 384_000, "memory_gb": 375.0,
     "node_count": 16, "partitions": "basic"},
    {"cluster": "primary", "memory_mb": 1_536_000, "memory_gb": 1500.0,
     "node_count": 2, "partitions": "large"},
]


def fp(busy_cores, span_hours, *, peak_gb=8, host_cores=16, host_gb=64, epoch=1,
       counters=True):
    session = {"username": "jdoe", "hostname": "labws1", "session_epoch": epoch,
               "peak_memory_bytes": int(peak_gb * GB), "span_hours": span_hours,
               "samples": int(span_hours * 12)}
    if counters:
        session["cpu_usage_usec_first"] = 5_000_000
        session["cpu_usage_usec"] = 5_000_000 + int(busy_cores * span_hours * 3600 * 1e6)
    return score_session(session, {"memory_total_mb": host_gb * 1024, "cpu_count": host_cores})


def test_busy_cores_are_cpu_time_over_wall_time():
    assert fp(14, 60).busy_cores == pytest.approx(14)
    assert session_busy_cores({"cpu_usage_usec": 10, "span_hours": 2}) is None


def test_one_long_heavy_run_is_advised_onto_the_cluster():
    issue = _build_cluster_promotion_verdict([fp(14, 60)], TIERS)
    assert issue is not None and issue.kind == "verdict"
    assert issue.dimension == "Cluster Recommended For Long Runs"
    assert issue.severity == "medium"
    ctx = issue.context
    assert ctx["verdict"] == "promote" and ctx["reason"] == "compute"
    assert ctx["target_partition"] == "basic"
    assert ctx["sbatch_snippet"].splitlines() == [
        "#SBATCH --ntasks=14", "#SBATCH --mem=16G", "#SBATCH --time=3-18:00:00",
        "#SBATCH --partition=basic"]
    assert "60 hours (one session" in issue.rationale
    assert "about 14 of labws1's 16 cores" in issue.rationale


def test_two_half_day_runs_add_up():
    issue = _build_cluster_promotion_verdict([fp(10, 13, epoch=1), fp(9, 13, epoch=2)], TIERS)
    assert issue is not None and "26 hours (2 sessions" in issue.rationale


@pytest.mark.parametrize("sessions", [
    [fp(2, 60)],                       # a couple of cores of 16: the workstation copes
    [fp(12, 20)],                      # heavy, but less than a day in all
    [fp(12, 11, epoch=1), fp(12, 11, epoch=2), fp(12, 11, epoch=3)],   # none is long
    [fp(14, 60, counters=False)],      # no CPU readings: nothing to say
], ids=["light", "short-total", "short-sessions", "no-counters"])
def test_what_does_not_count(sessions):
    assert _build_cluster_promotion_verdict(sessions, TIERS) is None


def test_no_cluster_no_advice():
    assert _build_cluster_promotion_verdict([fp(14, 60)], []) is None


def test_memory_pressure_still_leads():
    # Long and near the RAM ceiling: the memory verdict, as before.
    sessions = [fp(14, 20, peak_gb=60, epoch=1), fp(14, 20, peak_gb=60, epoch=2)]
    issue = _build_cluster_promotion_verdict(sessions, TIERS)
    assert issue.dimension == "Cluster Promotion Recommended"


def test_from_the_database(tmp_path):
    db = tmp_path / "nomad.db"
    now = datetime.now()
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE workstation_user_snapshot (timestamp TEXT, hostname TEXT, "
                  "username TEXT, uid INTEGER, session_epoch INTEGER, "
                  "memory_peak_bytes INTEGER, cpu_usage_usec INTEGER)")
        c.execute("CREATE TABLE workstation_state (timestamp TEXT, hostname TEXT, "
                  "memory_total_mb INTEGER, cpu_count INTEGER)")
        c.execute("INSERT INTO workstation_state VALUES (?, 'labws1', 65536, 16)",
                  (now.isoformat(),))
        start = now - timedelta(hours=60)
        for i in range(0, 60 * 12 + 1):           # every 5 minutes for 60 h
            when = start + timedelta(minutes=5 * i)
            c.execute("INSERT INTO workstation_user_snapshot VALUES (?,?,?,?,?,?,?)",
                      (when.isoformat(), "labws1", "jdoe", 1001, 42, 8 * GB,
                       10_000_000 + int(12 * 300 * 1e6) * i))   # 12 cores busy
    rows = _load_user_sessions(str(db), "jdoe")
    assert rows[0]["cpu_usage_usec_first"] == 10_000_000
    insights = user_insights(str(db), "jdoe", cluster_capacities=TIERS)
    verdict = insights.issues[0]
    assert verdict.kind == "verdict" and verdict.context["reason"] == "compute"
    assert verdict.context["busy_cores"] == pytest.approx(12, abs=0.1)

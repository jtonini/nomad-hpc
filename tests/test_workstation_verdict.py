# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Tests for the cluster-promotion verdict builder in nomad.edu.insights.

These tests exercise the A/B/C verdict dispatch:
  A — promote (cluster has >= 2x headroom)
  B — workload_fits_tool (cluster lacks meaningful headroom)
  C — exhausted_everywhere (peak exceeds even largest cluster tier)

Session fingerprints are built via score_session (the production path),
not constructed by hand. That way changes to score_memory_pressure or
score_duration_fit will surface here too.
"""
from __future__ import annotations

import sqlite3
import pytest

from nomad.edu.insights import (
    VERDICT_HEADROOM_MIN,
    VERDICT_MEMORY_PRESSURE_MAX,
    _build_cluster_promotion_verdict,
    _load_cluster_capacities,
    _select_verdict_kind,
)
from nomad.edu.scoring import score_session


# ── Helpers ──────────────────────────────────────────────────────────

def make_session(*, peak_gb, span_hours, hostname="host_a",
                 username="user1", session_epoch=1_700_000_000):
    """A session row shaped like an aggregated workstation_user_snapshot."""
    return {
        "username": username,
        "hostname": hostname,
        "session_epoch": session_epoch,
        "peak_memory_bytes": int(peak_gb * 1024 * 1024 * 1024),
        "span_hours": span_hours,
        "samples": max(1, int(span_hours * 60)),
    }


def fingerprint(peak_gb, span_hours, host_gb, epoch=1_700_000_000):
    """Build a SessionFingerprint via the real scoring path."""
    session = make_session(peak_gb=peak_gb, span_hours=span_hours,
                           session_epoch=epoch)
    host_state = {"memory_total_mb": int(host_gb * 1024)}
    return score_session(session, host_state)


# Synthetic cluster capacity lists — site-agnostic.
# These mimic real spydur tiers (375, 750, 1500) without naming spydur.
CLUSTER_TIERS_REALISTIC = [
    {"cluster": "primary", "memory_mb": 384_000, "memory_gb": 375.0,
     "node_count": 16, "partitions": "basic"},
    {"cluster": "primary", "memory_mb": 768_000, "memory_gb": 750.0,
     "node_count": 8,  "partitions": "medium"},
    {"cluster": "primary", "memory_mb": 1_536_000, "memory_gb": 1500.0,
     "node_count": 2,  "partitions": "large"},
]

# Small-tier-only cluster for Verdict B scenarios
CLUSTER_TIERS_SMALL = [
    {"cluster": "primary", "memory_mb": 256_000, "memory_gb": 250.0,
     "node_count": 4, "partitions": "basic"},
]


# ── _select_verdict_kind ─────────────────────────────────────────────

def test_select_promote_when_cluster_has_headroom():
    """Small peak, large cluster → promote, smallest-fit tier picked."""
    kind, target = _select_verdict_kind(60.0, 62.0, CLUSTER_TIERS_REALISTIC)
    assert kind == "promote"
    assert target is not None
    assert target["memory_gb"] == 375.0  # smallest tier with >= 2x


def test_select_promote_picks_smallest_fit_tier():
    """100 GB peak should pick 375 GB tier (3.75x), not 750 or 1500."""
    kind, target = _select_verdict_kind(100.0, 250.0, CLUSTER_TIERS_REALISTIC)
    assert kind == "promote"
    assert target["memory_gb"] == 375.0


def test_select_workload_fits_tool_when_cluster_undersized():
    """1100 GB peak, 1132 GB host: 1500 GB tier exists but only 1.36x → B."""
    kind, target = _select_verdict_kind(1100.0, 1132.0, CLUSTER_TIERS_REALISTIC)
    assert kind == "workload_fits_tool"
    assert target is None


def test_select_exhausted_when_peak_exceeds_largest_cluster():
    """2000 GB peak, 1500 GB largest cluster → exhausted_everywhere."""
    kind, target = _select_verdict_kind(2000.0, 2000.0, CLUSTER_TIERS_REALISTIC)
    assert kind == "exhausted_everywhere"
    assert target is None


def test_select_workload_fits_tool_when_no_clusters_known():
    """Empty cluster list → workload_fits_tool (cannot promote anywhere)."""
    kind, target = _select_verdict_kind(60.0, 62.0, [])
    assert kind == "workload_fits_tool"
    assert target is None


def test_select_threshold_edge_just_meets_headroom():
    """Peak = 187.5 GB, tier = 375 GB: ratio = exactly 2x → promote."""
    kind, target = _select_verdict_kind(187.5, 250.0, CLUSTER_TIERS_REALISTIC)
    assert kind == "promote"
    assert target["memory_gb"] == 375.0


def test_select_threshold_edge_just_below_headroom():
    """Peak = 188 GB, tier = 375 GB: ratio = 1.99x → falls through to next tier."""
    kind, target = _select_verdict_kind(188.0, 250.0, CLUSTER_TIERS_REALISTIC)
    assert kind == "promote"
    assert target["memory_gb"] == 750.0


# ── _build_cluster_promotion_verdict ─────────────────────────────────

def test_verdict_a_fires_on_two_qualifying_sessions():
    """Two sessions with high memory pressure AND long duration → Verdict A."""
    fingerprints = [
        fingerprint(peak_gb=60, span_hours=20, host_gb=62, epoch=1),
        fingerprint(peak_gb=58, span_hours=18, host_gb=62, epoch=2),
    ]
    issue = _build_cluster_promotion_verdict(fingerprints, CLUSTER_TIERS_REALISTIC)
    assert issue is not None
    assert issue.kind == "verdict"
    assert issue.context["verdict"] == "promote"
    assert issue.context["session_count"] == 2
    assert issue.context["target_memory_gb"] == 375.0
    assert "sbatch_snippet" in issue.context
    assert "--mem=" in issue.context["sbatch_snippet"]


def test_verdict_b_fires_when_cluster_lacks_headroom():
    """High pressure on a host larger than any cluster tier → Verdict B."""
    fingerprints = [
        fingerprint(peak_gb=1100, span_hours=30, host_gb=1132, epoch=1),
        fingerprint(peak_gb=1080, span_hours=24, host_gb=1132, epoch=2),
    ]
    issue = _build_cluster_promotion_verdict(fingerprints, CLUSTER_TIERS_REALISTIC)
    assert issue is not None
    assert issue.context["verdict"] == "workload_fits_tool"
    assert "right tool" in issue.rationale.lower()


def test_verdict_does_not_fire_with_only_one_qualifying_session():
    """Single session pattern is not enough — min 2 sessions required."""
    fingerprints = [
        fingerprint(peak_gb=60, span_hours=20, host_gb=62, epoch=1),
        fingerprint(peak_gb=5, span_hours=2, host_gb=62, epoch=2),  # benign
    ]
    issue = _build_cluster_promotion_verdict(fingerprints, CLUSTER_TIERS_REALISTIC)
    assert issue is None


def test_verdict_does_not_fire_when_memory_high_but_duration_short():
    """Memory pressure alone (no long sessions) → no verdict."""
    fingerprints = [
        fingerprint(peak_gb=60, span_hours=1, host_gb=62, epoch=1),
        fingerprint(peak_gb=58, span_hours=1, host_gb=62, epoch=2),
    ]
    issue = _build_cluster_promotion_verdict(fingerprints, CLUSTER_TIERS_REALISTIC)
    assert issue is None


def test_verdict_does_not_fire_when_duration_long_but_memory_comfortable():
    """Long sessions with low memory pressure → no verdict."""
    fingerprints = [
        fingerprint(peak_gb=5, span_hours=24, host_gb=62, epoch=1),
        fingerprint(peak_gb=6, span_hours=20, host_gb=62, epoch=2),
    ]
    issue = _build_cluster_promotion_verdict(fingerprints, CLUSTER_TIERS_REALISTIC)
    assert issue is None


def test_verdict_does_not_fire_on_empty_fingerprints():
    """No sessions at all → no verdict, no crash."""
    issue = _build_cluster_promotion_verdict([], CLUSTER_TIERS_REALISTIC)
    assert issue is None


def test_verdict_sbatch_snippet_buffers_duration_and_caps_at_seven_days():
    """sbatch --time should be 1.5x observed duration, capped at 7 days."""
    fingerprints = [
        fingerprint(peak_gb=60, span_hours=200, host_gb=62, epoch=1),  # 200h would be 300h * 1.5
        fingerprint(peak_gb=58, span_hours=180, host_gb=62, epoch=2),
    ]
    issue = _build_cluster_promotion_verdict(fingerprints, CLUSTER_TIERS_REALISTIC)
    assert issue is not None
    snippet = issue.context["sbatch_snippet"]
    # 200h * 1.5 = 300h = 12.5 days, but capped at 7 days = 168h
    assert "--time=7-00:00:00" in snippet


# ── _load_cluster_capacities ─────────────────────────────────────────

def test_load_cluster_capacities_from_synthetic_db(tmp_path):
    """In-memory SQLite with synthetic node_state rows should produce
    the expected sorted-ascending tier list."""
    db = tmp_path / "test.db"
    con = sqlite3.connect(db)
    con.execute("""
        CREATE TABLE node_state (
            id INTEGER PRIMARY KEY,
            timestamp DATETIME,
            node_name TEXT,
            cluster TEXT,
            memory_total_mb INTEGER,
            partitions TEXT
        )
    """)
    # Insert a few rows: 2 clusters, 3 distinct memory tiers
    con.executemany(
        "INSERT INTO node_state (timestamp, node_name, cluster, memory_total_mb, partitions) VALUES (datetime('now'), ?, ?, ?, ?)",
        [
            ("nodeA1", "alpha", 256_000, "basic"),
            ("nodeA2", "alpha", 256_000, "basic"),
            ("nodeB1", "alpha", 512_000, "medium"),
            ("nodeC1", "beta",  768_000, "large"),
        ]
    )
    con.commit()
    con.close()

    caps = _load_cluster_capacities(str(db))
    assert len(caps) == 3
    assert caps[0]["memory_mb"] == 256_000  # smallest first
    assert caps[0]["node_count"] == 2
    assert caps[-1]["memory_mb"] == 768_000  # largest last


def test_load_cluster_capacities_missing_db_returns_empty():
    """Nonexistent DB path should return empty list, not crash."""
    caps = _load_cluster_capacities("/nonexistent/path/no.db")
    assert caps == []

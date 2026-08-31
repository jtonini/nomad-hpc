# SPDX-License-Identifier: AGPL-3.0-or-later
"""Chronic-pressure verdict path: many short high-RAM sessions promote even
though no single session is long (the acute AND-rule would miss them)."""
from nomad.edu.insights import (
    _build_cluster_promotion_verdict,
    VERDICT_CHRONIC_MIN_SESSIONS,
)
# Reuse the proven session/fingerprint helpers from the verdict test module.
from tests.test_workstation_verdict import fingerprint

CLUSTER_TIERS = [
    {"cluster": "hpc1", "memory_mb": 128_000, "memory_gb": 128.0,
     "node_count": 8, "partitions": "compute"},
    {"cluster": "hpc1", "memory_mb": 256_000, "memory_gb": 256.0,
     "node_count": 4, "partitions": "highmem"},
]


def test_chronic_fires_with_many_short_pressured_sessions():
    # 12 sessions, all ~95% RAM but each only ~2h (short). No single session
    # is both pressured AND long — the chronic path must catch the pattern.
    fps = [fingerprint(peak_gb=59, span_hours=2, host_gb=62, epoch=i)
           for i in range(12)]
    issue = _build_cluster_promotion_verdict(fps, CLUSTER_TIERS)
    assert issue is not None
    assert issue.context["verdict"] == "promote"
    assert issue.context["reason"] == "chronic"
    assert issue.context["session_count"] == 12


def test_chronic_does_not_fire_below_the_bar():
    n = VERDICT_CHRONIC_MIN_SESSIONS - 1
    fps = [fingerprint(peak_gb=59, span_hours=2, host_gb=62, epoch=i)
           for i in range(n)]
    issue = _build_cluster_promotion_verdict(fps, CLUSTER_TIERS)
    assert issue is None


def test_acute_still_takes_precedence():
    # Long+pressured sessions fire the acute path (only 2 needed), reason=acute.
    fps = [
        fingerprint(peak_gb=59, span_hours=24, host_gb=62, epoch=1),
        fingerprint(peak_gb=58, span_hours=20, host_gb=62, epoch=2),
    ]
    issue = _build_cluster_promotion_verdict(fps, CLUSTER_TIERS)
    assert issue is not None
    assert issue.context["reason"] == "acute"

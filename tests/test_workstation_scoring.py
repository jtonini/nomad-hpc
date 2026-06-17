# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Tests for workstation session scoring in nomad.edu.scoring."""

from __future__ import annotations

import re
import pytest

from nomad.edu.scoring import (
    SessionFingerprint,
    score_duration_fit,
    score_memory_pressure,
    score_session,
    select_cluster_target,
    SPYDUR_MEMORY_TIERS_MB,
)


# ── Fixtures ─────────────────────────────────────────────────────────

@pytest.fixture
def host_boyi():
    """boyi: 256 GB host (real Parish lab workstation)."""
    return {"hostname": "boyi", "memory_total_mb": 256_904, "cpu_count": 64}


@pytest.fixture
def host_thais():
    """thais: 64 GB host (smaller Parish lab box, the headline example)."""
    return {"hostname": "thais", "memory_total_mb": 63_642, "cpu_count": 32}


def make_session(*, peak_gb: float, span_hours: float, hostname="boyi",
                 username="user1", session_epoch=1_700_000_000) -> dict:
    """Build a session dict shaped like a workstation_user_snapshot row
    aggregated by session_epoch."""
    return {
        "username": username,
        "hostname": hostname,
        "session_epoch": session_epoch,
        "peak_memory_bytes": int(peak_gb * 1024 * 1024 * 1024),
        "span_hours": span_hours,
        "samples": max(1, int(span_hours * 60)),  # 60s collector cadence
    }


# ── Memory pressure ──────────────────────────────────────────────────

def test_memory_pressure_kbui_critical(host_boyi):
    """The headline case: kbui on boyi at 98.7% of host RAM."""
    session = make_session(peak_gb=253.0, span_hours=34.3)
    score = score_memory_pressure(session, host_boyi)
    assert score.applicable
    assert score.score < 5         # critically pressured
    assert score.level == "Needs Work"
    # Cgroup peak can equal or slightly exceed host RAM total
    # because peak_memory_bytes includes cache. Verify pressure
    # is reported in the saturation zone (>=95%) rather than
    # asserting an exact percentage.
    m = re.search(r"\((\d+)%\)", score.detail)
    assert m is not None, f"no percentage in detail: {score.detail!r}"
    assert int(m.group(1)) >= 95


def test_memory_pressure_comfortable(host_boyi):
    """5% of a 256 GB host = comfortable."""
    session = make_session(peak_gb=12.0, span_hours=1.0)
    score = score_memory_pressure(session, host_boyi)
    assert score.applicable
    assert score.score >= 85
    assert score.level == "Excellent"


def test_memory_pressure_moderate(host_boyi):
    """~50% pressure → middle of the road."""
    session = make_session(peak_gb=128.0, span_hours=2.0)
    score = score_memory_pressure(session, host_boyi)
    assert 45 <= score.score <= 55


def test_memory_pressure_missing_host_capacity():
    """Host capacity unknown → not applicable, not divide-by-zero."""
    session = make_session(peak_gb=8.0, span_hours=1.0)
    score = score_memory_pressure(session, {"memory_total_mb": 0})
    assert not score.applicable
    assert score.score == 50  # sentinel


def test_memory_pressure_missing_peak(host_boyi):
    """No peak data → not applicable."""
    session = {"hostname": "boyi", "username": "x", "session_epoch": 1,
               "peak_memory_bytes": 0, "span_hours": 1.0}
    score = score_memory_pressure(session, host_boyi)
    assert not score.applicable


# ── Duration fit ─────────────────────────────────────────────────────

def test_duration_fit_short(host_boyi):
    """30 minute session → perfect score."""
    session = make_session(peak_gb=4.0, span_hours=0.5)
    score = score_duration_fit(session, host_boyi)
    assert score.score == 100
    assert score.level == "Excellent"


def test_duration_fit_4h(host_boyi):
    """4h falls in 2-6h taper, score around 80."""
    session = make_session(peak_gb=4.0, span_hours=4.0)
    score = score_duration_fit(session, host_boyi)
    assert 75 <= score.score <= 85


def test_duration_fit_12h(host_boyi):
    """12h falls in 6-18h taper, score around 45."""
    session = make_session(peak_gb=4.0, span_hours=12.0)
    score = score_duration_fit(session, host_boyi)
    assert 40 <= score.score <= 50


def test_duration_fit_kbui_floor(host_boyi):
    """34h hits the >=18h floor."""
    session = make_session(peak_gb=4.0, span_hours=34.3)
    score = score_duration_fit(session, host_boyi)
    assert score.score == 10
    assert score.level == "Needs Work"


def test_duration_fit_missing(host_boyi):
    """No span data → not applicable."""
    session = {"hostname": "boyi", "username": "x", "session_epoch": 1,
               "peak_memory_bytes": 1024, "span_hours": None}
    score = score_duration_fit(session, host_boyi)
    assert not score.applicable


# ── Composite scoring ────────────────────────────────────────────────

def test_score_session_kbui_both_critical(host_boyi):
    """The headline case fingerprint: both dimensions critical."""
    session = make_session(peak_gb=253.0, span_hours=34.3, username="kbui")
    fp = score_session(session, host_boyi)
    assert isinstance(fp, SessionFingerprint)
    assert fp.username == "kbui"
    assert fp.hostname == "boyi"
    assert set(fp.dimensions) == {"memory_pressure", "duration_fit"}
    assert all(d.score < 20 for d in fp.dimensions.values())
    assert fp.overall < 20
    assert len(fp.needs_work) == 2


def test_score_session_memory_only(host_thais):
    """High memory, short duration: only memory dimension trips."""
    session = make_session(peak_gb=60.0, span_hours=0.5, hostname="thais")
    fp = score_session(session, host_thais)
    assert fp.dimensions["memory_pressure"].score < 20
    assert fp.dimensions["duration_fit"].score == 100


def test_score_session_duration_only(host_boyi):
    """Long duration, low memory: only duration dimension trips."""
    session = make_session(peak_gb=8.0, span_hours=20.0)
    fp = score_session(session, host_boyi)
    assert fp.dimensions["memory_pressure"].score >= 85
    assert fp.dimensions["duration_fit"].score == 10


def test_score_session_benign(host_boyi):
    """Short, low-memory session: everything good."""
    session = make_session(peak_gb=4.0, span_hours=1.0)
    fp = score_session(session, host_boyi)
    assert fp.overall >= 85
    assert fp.needs_work == []


# ── Raw payload (verdict-builder reads these without parsing strings) ─

def test_memory_pressure_raw_populated_when_applicable(host_thais):
    """raw must carry peak_bytes, host_total_bytes, pressure_ratio so the verdict builder can read structured values rather than parsing detail strings."""
    session = make_session(peak_gb=60, span_hours=5, hostname=host_thais["hostname"])
    dim = score_memory_pressure(session, host_thais)
    assert dim.applicable
    assert dim.raw is not None
    assert dim.raw["peak_bytes"] == int(60 * 1024 * 1024 * 1024)
    assert dim.raw["host_total_bytes"] == host_thais["memory_total_mb"] * 1024 * 1024
    # 60 GB peak on 62.15 GB host_thais → pressure_ratio ~ 0.97
    assert 0.95 <= dim.raw["pressure_ratio"] <= 0.99


def test_memory_pressure_raw_populated_when_not_applicable():
    """Even when the dimension cannot be scored (missing/zero data), raw must still be a dict — never None — so downstream code can read dim.raw[key] guarded only by dim.applicable."""
    bad_host = {"hostname": "h", "memory_total_mb": 0, "cpu_count": 1}
    session = make_session(peak_gb=0, span_hours=1)
    dim = score_memory_pressure(session, bad_host)
    assert not dim.applicable
    assert dim.raw is not None
    assert dim.raw["pressure_ratio"] == 0.0


def test_duration_fit_raw_carries_seconds_and_hours():
    """raw must carry both span_hours (canonical input) and span_seconds (derived) so consumers can pick whichever unit they need."""
    session = make_session(peak_gb=10, span_hours=34.0)
    dim = score_duration_fit(session, {"memory_total_mb": 256_000})
    assert dim.applicable
    assert dim.raw is not None
    assert dim.raw["span_hours"] == 34.0
    assert dim.raw["span_seconds"] == 34.0 * 3600


# ── Cluster target selection ─────────────────────────────────────────

def test_select_target_kbui():
    """boyi (256 GB) with kbui's 253 GB peak: 384 GB tier is too close
    (1.5× host), bump to 768 GB tier (3× host)."""
    result = select_cluster_target(peak_mb=253 * 1024, host_mb=256_904)
    assert result is not None
    target_mb, ratio = result
    assert target_mb == 768_000
    assert 2.9 <= ratio <= 3.1


def test_select_target_thais():
    """thais (64 GB) with a 60 GB peak: 384 GB tier already gives 6×."""
    result = select_cluster_target(peak_mb=60 * 1024, host_mb=63_642)
    assert result is not None
    target_mb, ratio = result
    assert target_mb == 384_000
    assert 5.5 <= ratio <= 6.5


def test_select_target_extreme():
    """A 1 TB peak: even the 1.5 TB tier is required."""
    result = select_cluster_target(peak_mb=1_000_000, host_mb=63_642)
    assert result is not None
    target_mb, _ = result
    assert target_mb == 1_536_000


def test_select_target_unreachable():
    """A peak that exceeds even the largest tier: None."""
    result = select_cluster_target(peak_mb=2_000_000, host_mb=63_642)
    assert result is None


def test_select_target_invalid_inputs():
    """Zero or negative inputs return None rather than crashing."""
    assert select_cluster_target(peak_mb=0, host_mb=64_000) is None
    assert select_cluster_target(peak_mb=10_000, host_mb=0) is None


def test_select_target_tiers_are_sorted():
    """Defensive: out-of-order tiers shouldn't break the algorithm."""
    result = select_cluster_target(
        peak_mb=253 * 1024, host_mb=256_904,
        tiers=(1_536_000, 384_000, 768_000),  # deliberately scrambled
    )
    assert result == (768_000, 768_000 / 256_904)

# SPDX-License-Identifier: AGPL-3.0-or-later
"""Queue-aware promotion verdict: queue wait modulates promote-vs-hedge,
weighted by whether the workstation is genuinely memory-bound. Never
fabricates a speedup; degrades honestly when data is absent."""
from nomad.edu.insights import (
    _time_to_results,
    QUEUE_ZONE_CLEAN_S, QUEUE_ZONE_NOTE_S, QUEUE_ZONE_CAVEAT_S,
)

H = 3600


def _thrash(is_thrash, iowait=0.34):
    return {
        "thrashing": is_thrash,
        "iowait_fraction": iowait if is_thrash else 0.0,
        "swap_used_mb": 14000 if is_thrash else 512,
        "iowait_pct": 34 if is_thrash else 2,
    }


# ── Zones (no thrashing) ─────────────────────────────────────────────

def test_clean_zone_promotes():
    r = _time_to_results(0.3 * H, _thrash(False), 100)
    assert r["zone"] == "clean"
    assert r["recommend"] == "promote"


def test_note_zone_promotes():
    r = _time_to_results(3 * H, _thrash(False), 100)
    assert r["zone"] == "note"
    assert r["recommend"] == "promote"


def test_caveat_zone_promotes():
    r = _time_to_results(10 * H, _thrash(False), 100)
    assert r["zone"] == "caveat"
    assert r["recommend"] == "promote"


def test_hedge_zone_no_thrash_is_tradeoff():
    # Long queue, workstation not memory-bound -> honest tradeoff, hedged.
    r = _time_to_results(960 * H, _thrash(False), 100)
    assert r["zone"] == "hedge"
    assert r["recommend"] == "promote_hedged"
    assert r["confidence"] == "tradeoff"


# ── Thrashing changes confidence, and the guardrail ─────────────────

def test_short_queue_thrashing_is_high_confidence():
    r = _time_to_results(0.3 * H, _thrash(True), 100)
    assert r["confidence"] == "high"


def test_hedge_thrashing_wins_when_recovery_beats_wait():
    # 40h queue, thrashing with iowait 0.34: recovered ~55h > 40h -> promote.
    r = _time_to_results(40 * H, _thrash(True), 100)
    assert r["zone"] == "hedge"
    assert r["recommend"] == "promote"
    assert r["confidence"] == "high"
    assert r["recovered_hours"] > 40


def test_hedge_thrashing_still_hedges_when_queue_too_long():
    # THE GUARDRAIL: even memory-bound, a 40-day queue swamps the recovered
    # time (~55h << 960h) -> hedge, never a false "cluster is faster" claim.
    r = _time_to_results(960 * H, _thrash(True), 100)
    assert r["zone"] == "hedge"
    assert r["recommend"] == "promote_hedged"
    assert r["confidence"] == "tradeoff"


# ── Honest degradation on missing data ───────────────────────────────

def test_no_wait_history_no_modulation():
    # Workstation-only institution / new cluster: no wait data -> plain promote.
    r = _time_to_results(None, _thrash(True), 100)
    assert r["zone"] == "unknown"
    assert r["recommend"] == "promote"


def test_no_host_state_makes_no_thrash_claim():
    # Can't assess memory (no host state) -> no thrashing claim, moderate promote.
    r = _time_to_results(10 * H, None, 100)
    assert r["thrashing"] is False
    assert r["recommend"] == "promote"


def test_hedge_thrashing_but_no_compute_hours_hedges():
    # Thrashing but no measured compute time -> can't prove recovery beats the
    # wait, so hedge rather than fabricate. Honest.
    r = _time_to_results(960 * H, _thrash(True), None)
    assert r["recommend"] == "promote_hedged"


# ── Zone boundaries ──────────────────────────────────────────────────

def test_zone_boundaries():
    assert _time_to_results(QUEUE_ZONE_CLEAN_S - 1, _thrash(False), 100)["zone"] == "clean"
    assert _time_to_results(QUEUE_ZONE_CLEAN_S, _thrash(False), 100)["zone"] == "note"
    assert _time_to_results(QUEUE_ZONE_NOTE_S, _thrash(False), 100)["zone"] == "caveat"
    assert _time_to_results(QUEUE_ZONE_CAVEAT_S, _thrash(False), 100)["zone"] == "hedge"

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Tests for nomad.collectors.per_user.rules

Synthetic process tracks calibrated against the four canonical cases
from the Idea 18 validation:

  - ia3nk's gmx_mpi: 95% CPU sustained 161 minutes
    -> should fire cpu_10pct_5min and cpu_50pct_2min
  - perickso's R: bursts to 670%, 75GB RSS, 109 minutes
    -> should fire memory_16gb_2min (CPU rules also fire on the bursts)
  - abezerra's antigravity language server: 25-34% CPU, 1-2GB RSS, 6.4h
    -> cpu_10pct_5min fires; memory_4gb_10min does NOT (under 4GB)
    -> if the same case had 5GB it WOULD fire memory_4gb_10min
        as 'informational' (soft landing)
  - sumo-bandplot: 99% CPU for 2 minutes
    -> cpu_50pct_2min fires; cpu_10pct_5min does NOT (too short)
    -> [the 80%/1min rule that would catch this isn't in v1; pacct adds it]
"""
from __future__ import annotations

import pytest

from nomad.collectors.per_user.rules import (
    DEFAULT_RULES,
    ProcessTrack,
    Rule,
    RuleEngine,
    Sample,
)


GB = 1024 ** 3


def make_track(pid: int = 1234, username: str = "testuser") -> ProcessTrack:
    return ProcessTrack(
        process_session_id=f"sha1-{pid}",
        pid=pid,
        username=username,
        uid=10001,
        command="testcmd",
    )


def feed(track: ProcessTrack, engine: RuleEngine, samples: list[Sample]) -> None:
    """Feed samples one at a time, the way the collector would."""
    for s in samples:
        track.add_sample(s, engine.max_window_seconds)


# ---------------------------------------------------------------------------
# ia3nk: sustained high CPU should fire both CPU rules
# ---------------------------------------------------------------------------

def test_sustained_high_cpu_fires_both_cpu_rules():
    engine = RuleEngine()
    track = make_track()
    # 6 minutes of 95% CPU at 60s sampling -> 7 samples (t=0..360)
    samples = [Sample(timestamp=float(i * 60), cpu_percent=95.0, memory_rss_bytes=100_000_000)
               for i in range(7)]
    feed(track, engine, samples)
    firings = engine.evaluate(track, now=360.0)
    fired_ids = {f.rule.rule_id for f in firings}
    assert "cpu_10pct_5min" in fired_ids
    assert "cpu_50pct_2min" in fired_ids


# ---------------------------------------------------------------------------
# perickso: 75GB RSS for 109 minutes fires the high-memory rule
# ---------------------------------------------------------------------------

def test_high_memory_fires_actionable_rule():
    engine = RuleEngine()
    track = make_track()
    # 3 minutes of 75GB at 60s sampling
    samples = [Sample(timestamp=float(i * 60), cpu_percent=5.0, memory_rss_bytes=75 * GB)
               for i in range(4)]
    feed(track, engine, samples)
    firings = engine.evaluate(track, now=180.0)
    by_id = {f.rule.rule_id: f for f in firings}
    assert "memory_16gb_2min" in by_id
    assert by_id["memory_16gb_2min"].rule.severity == "actionable"


# ---------------------------------------------------------------------------
# abezerra (under 4GB): cpu_10pct fires, no memory rule fires
# ---------------------------------------------------------------------------

def test_ide_language_server_under_memory_threshold_fires_only_cpu():
    engine = RuleEngine()
    track = make_track(username="abezerra")
    # 6 minutes at 25% CPU and 1.5GB RSS
    samples = [Sample(timestamp=float(i * 60), cpu_percent=25.0, memory_rss_bytes=int(1.5 * GB))
               for i in range(7)]
    feed(track, engine, samples)
    firings = engine.evaluate(track, now=360.0)
    fired_ids = {f.rule.rule_id for f in firings}
    assert "cpu_10pct_5min" in fired_ids
    assert "cpu_50pct_2min" not in fired_ids       # only 25%
    assert "memory_4gb_10min" not in fired_ids     # only 1.5GB
    assert "memory_16gb_2min" not in fired_ids


# ---------------------------------------------------------------------------
# abezerra (5GB hypothetical): memory_4gb_10min fires as 'informational'
# ---------------------------------------------------------------------------

def test_ide_language_server_above_4gb_fires_informational_only():
    engine = RuleEngine()
    track = make_track(username="abezerra")
    # 11 minutes at 5% CPU and 5GB RSS — quiet but holding memory
    samples = [Sample(timestamp=float(i * 60), cpu_percent=5.0, memory_rss_bytes=5 * GB)
               for i in range(12)]
    feed(track, engine, samples)
    firings = engine.evaluate(track, now=660.0)
    by_id = {f.rule.rule_id: f for f in firings}
    assert "memory_4gb_10min" in by_id
    assert by_id["memory_4gb_10min"].rule.severity == "informational"
    assert "memory_16gb_2min" not in by_id
    assert "cpu_10pct_5min" not in by_id


# ---------------------------------------------------------------------------
# sumo-bandplot: 2 minutes at 99% — only cpu_50pct_2min fires in v1
# ---------------------------------------------------------------------------

def test_short_burst_fires_only_two_minute_cpu_rule():
    engine = RuleEngine()
    track = make_track()
    # 2 minutes at 99% — exactly the 50%/2min window
    samples = [Sample(timestamp=float(i * 60), cpu_percent=99.0, memory_rss_bytes=200_000_000)
               for i in range(3)]                          # t=0, 60, 120
    feed(track, engine, samples)
    firings = engine.evaluate(track, now=120.0)
    fired_ids = {f.rule.rule_id for f in firings}
    assert "cpu_50pct_2min" in fired_ids
    assert "cpu_10pct_5min" not in fired_ids               # only 2 minutes of data


# ---------------------------------------------------------------------------
# Sustain windows: a brief dip below threshold should reset the window
# ---------------------------------------------------------------------------

def test_dip_below_threshold_prevents_firing():
    engine = RuleEngine()
    track = make_track()
    # 6 minutes mostly at 60% CPU, with one sample at 5%
    samples = [Sample(timestamp=float(i * 60), cpu_percent=60.0, memory_rss_bytes=100_000_000)
               for i in range(7)]
    samples[3] = Sample(timestamp=180.0, cpu_percent=5.0, memory_rss_bytes=100_000_000)
    feed(track, engine, samples)
    firings = engine.evaluate(track, now=360.0)
    fired_ids = {f.rule.rule_id for f in firings}
    # cpu_10pct_5min: needs ALL samples in last 5 min >= 10%. The dip is
    # within the last 5 min, so this should NOT fire.
    assert "cpu_10pct_5min" not in fired_ids
    # cpu_50pct_2min: last 2 minutes are all at 60%, so this DOES fire.
    assert "cpu_50pct_2min" in fired_ids


# ---------------------------------------------------------------------------
# Cooldown: re-evaluating without new samples doesn't double-fire
# ---------------------------------------------------------------------------

def test_cooldown_suppresses_immediate_refire():
    engine = RuleEngine(cooldown_seconds=3600)
    track = make_track()
    samples = [Sample(timestamp=float(i * 60), cpu_percent=95.0, memory_rss_bytes=100_000_000)
               for i in range(7)]
    feed(track, engine, samples)
    firings_1 = engine.evaluate(track, now=360.0)
    firings_2 = engine.evaluate(track, now=420.0)          # 1 minute later
    assert len(firings_1) > 0
    assert len(firings_2) == 0


def test_cooldown_expires_and_allows_refire():
    engine = RuleEngine(cooldown_seconds=600)              # 10 min cooldown
    track = make_track()
    samples = [Sample(timestamp=float(i * 60), cpu_percent=95.0, memory_rss_bytes=100_000_000)
               for i in range(7)]
    feed(track, engine, samples)
    firings_1 = engine.evaluate(track, now=360.0)
    # Continue sampling for another 15 minutes at 95%
    for i in range(7, 22):
        track.add_sample(
            Sample(timestamp=float(i * 60), cpu_percent=95.0, memory_rss_bytes=100_000_000),
            engine.max_window_seconds,
        )
    firings_2 = engine.evaluate(track, now=1320.0)         # 22 min total
    assert len(firings_1) > 0
    # Cooldown of 10 min should have expired by t=1320
    fired_ids_2 = {f.rule.rule_id for f in firings_2}
    assert "cpu_10pct_5min" in fired_ids_2


# ---------------------------------------------------------------------------
# Insufficient observation window: if we just started seeing the process,
# we shouldn't fire even if all observed samples cross the threshold.
# ---------------------------------------------------------------------------

def test_short_observation_doesnt_fire():
    engine = RuleEngine()
    track = make_track()
    # Only 1 sample
    track.add_sample(
        Sample(timestamp=0.0, cpu_percent=99.0, memory_rss_bytes=20 * GB),
        engine.max_window_seconds,
    )
    firings = engine.evaluate(track, now=0.0)
    assert firings == []


# ---------------------------------------------------------------------------
# pacct rules are accepted but ignored in v1
# ---------------------------------------------------------------------------

def test_pacct_rules_are_not_evaluated_in_v1():
    pacct_rule = Rule(
        rule_id="cpu_80pct_1min",
        rule_type="cpu",
        threshold_value=80.0,
        threshold_unit="percent",
        duration_seconds=60,
        source="pacct",
    )
    engine = RuleEngine(rules=(*DEFAULT_RULES, pacct_rule))
    track = make_track()
    samples = [Sample(timestamp=float(i * 30), cpu_percent=99.0, memory_rss_bytes=100_000_000)
               for i in range(3)]
    feed(track, engine, samples)
    firings = engine.evaluate(track, now=60.0)
    fired_ids = {f.rule.rule_id for f in firings}
    assert "cpu_80pct_1min" not in fired_ids


# ---------------------------------------------------------------------------
# Window eviction: track shouldn't grow unbounded
# ---------------------------------------------------------------------------

def test_window_eviction_bounds_sample_count():
    engine = RuleEngine()                                  # max window = 600s
    track = make_track()
    # Feed 30 minutes (1800s) of samples at 60s intervals = 30 samples
    for i in range(30):
        track.add_sample(
            Sample(timestamp=float(i * 60), cpu_percent=5.0, memory_rss_bytes=100_000_000),
            engine.max_window_seconds,
        )
    # We should retain ~10 samples (last 600s)
    assert len(track.samples) <= 12        # allow small slack for boundary
    assert len(track.samples) >= 9

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAD per-user collector — rule engine.

Pure-logic module. No I/O, no psutil, no DB. Takes sample observations in,
emits alert decisions out. Fully testable with synthetic input.

Concepts
--------
A *Rule* is a configured detection threshold:
    cpu_percent >= 10 sustained for 5 minutes
    memory_rss >= 16 GB sustained for 2 minutes

A *ProcessTrack* is the per-PID rolling window of samples needed to evaluate
sustain windows. The collector owns one of these per live process.

A *RuleFiring* is what comes out when a rule's sustain window is satisfied.
The collector turns firings into alert rows (with dedup against prior
firings on the same process_session_id + rule_id).

Design notes
------------
- Rules declare their `source` ('psutil' or 'pacct'). v1 wires only psutil
  rules; pacct rules are accepted by the engine but no samples will satisfy
  them until the pacct backend lands.
- Sustain logic uses a deque of (timestamp, value) pairs. We evaluate by
  asking: "of the samples within the last `duration_seconds`, do all of
  them satisfy the threshold?". This is robust to clock skew and dropped
  ticks (which the validation showed are rare but not impossible).
- A rule fires at most once per process_session per cooldown window (default
  1 hour). Re-firings within the cooldown bump `occurrences` rather than
  inserting new alerts. This matches the existing `alerts` table pattern.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal
from collections.abc import Iterable


RuleType = Literal["cpu", "memory"]
RuleSource = Literal["psutil", "pacct"]
Severity = Literal["actionable", "informational"]


# ---------------------------------------------------------------------------
# Configuration: rule definitions
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Rule:
    """A single detection rule. Immutable; held in collector config."""
    rule_id: str                          # stable identifier, used in dedup keys
    rule_type: RuleType
    threshold_value: float                # 10.0 (percent) or 4.0 (gb)
    threshold_unit: Literal["percent", "gb"]
    duration_seconds: int
    severity: Severity = "actionable"
    source: RuleSource = "psutil"
    edu_template_id: str | None = None    # optional handoff to edu engine
    # Fraction of samples in the sustain window that must satisfy the
    # threshold (0.0..1.0). 1.0 = strict "all samples". Defaults below tolerate
    # ~30% dips on CPU rules (real-world workloads are bursty) and require
    # strict sustain on memory rules (RSS doesn't dip the same way).
    sustain_fraction: float = 0.7

    def threshold_bytes(self) -> int | None:
        """For memory rules, return the threshold expressed in bytes."""
        if self.threshold_unit == "gb":
            return int(self.threshold_value * (1024 ** 3))
        return None


# Default rule set per the validation findings. The 80%/1min rule from the
# handoff is *deliberately omitted* from v1: 60s sampling has too much aliasing
# to reliably detect a 1-minute event. It returns when pacct lands.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(
        rule_id="cpu_10pct_5min",
        rule_type="cpu",
        threshold_value=10.0,
        threshold_unit="percent",
        duration_seconds=300,
        severity="actionable",
        edu_template_id="head_node_cpu_sustained",
        sustain_fraction=0.7,              # tolerate dips on bursty CPU workloads
    ),
    Rule(
        rule_id="cpu_50pct_2min",
        rule_type="cpu",
        threshold_value=50.0,
        threshold_unit="percent",
        duration_seconds=120,
        severity="actionable",
        edu_template_id="head_node_cpu_high",
        sustain_fraction=0.7,              # tolerate dips on bursty CPU workloads
    ),
    Rule(
        rule_id="memory_4gb_10min",
        rule_type="memory",
        threshold_value=4.0,
        threshold_unit="gb",
        duration_seconds=600,
        severity="informational",          # softer — IDE/language-server case
        edu_template_id="head_node_memory_moderate",
        sustain_fraction=1.0,              # RSS doesn't dip; require strict sustain
    ),
    Rule(
        rule_id="memory_16gb_2min",
        rule_type="memory",
        threshold_value=16.0,
        threshold_unit="gb",
        duration_seconds=120,
        severity="actionable",
        edu_template_id="head_node_memory_high",
        sustain_fraction=1.0,              # RSS doesn't dip; require strict sustain
    ),
)


# ---------------------------------------------------------------------------
# Per-process state
# ---------------------------------------------------------------------------

@dataclass
class Sample:
    """One observation of a process. Constructed by the collector."""
    timestamp: float                      # unix epoch seconds
    cpu_percent: float
    memory_rss_bytes: int


@dataclass
class ProcessTrack:
    """Rolling state for one process. Owned by the collector, evaluated here."""
    process_session_id: str
    pid: int
    username: str
    uid: int
    command: str

    # Bounded window: keep enough samples to evaluate the longest rule.
    # Sized externally based on configured rules.
    samples: deque[Sample] = field(default_factory=deque)

    # Per-rule firing state (rule_id -> last_fired_unix). For cooldown.
    last_fired: dict[str, float] = field(default_factory=dict)

    # Peak observed (since track creation) — surfaced in alert payload.
    peak_cpu_percent: float = 0.0
    peak_memory_bytes: int = 0

    def add_sample(self, s: Sample, max_window_seconds: int) -> None:
        self.samples.append(s)
        self.peak_cpu_percent = max(self.peak_cpu_percent, s.cpu_percent)
        self.peak_memory_bytes = max(self.peak_memory_bytes, s.memory_rss_bytes)
        # Evict samples older than the longest rule window
        cutoff = s.timestamp - max_window_seconds
        while self.samples and self.samples[0].timestamp < cutoff:
            self.samples.popleft()


# ---------------------------------------------------------------------------
# Firings
# ---------------------------------------------------------------------------

@dataclass
class RuleFiring:
    """Result of evaluating a rule against a track. The collector persists this."""
    rule: Rule
    track: ProcessTrack
    fired_at: float                       # unix epoch
    sustained_for_seconds: int            # how long the condition held
    peak_cpu_percent: float
    peak_memory_bytes: int


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class RuleEngine:
    """Evaluates rules against process tracks.

    Stateless across calls except via ProcessTrack.last_fired (cooldown).
    The collector calls evaluate() once per tick per active track.
    """

    DEFAULT_COOLDOWN_SECONDS = 3600       # 1 hour: re-firings within this bump occurrences

    def __init__(
        self,
        rules: Iterable[Rule] = DEFAULT_RULES,
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self.rules: tuple[Rule, ...] = tuple(rules)
        self.cooldown_seconds = cooldown_seconds
        # Pre-compute the longest window — drives sample retention in tracks
        self._max_window = max((r.duration_seconds for r in self.rules), default=0)

    @property
    def max_window_seconds(self) -> int:
        return self._max_window

    def evaluate(self, track: ProcessTrack, now: float | None = None) -> list[RuleFiring]:
        """Evaluate all rules against this track. Returns 0+ firings."""
        if now is None:
            now = time.time()
        firings: list[RuleFiring] = []
        for rule in self.rules:
            if rule.source != "psutil":
                # pacct-sourced rules are evaluated elsewhere (Component 1.5)
                continue
            firing = self._evaluate_rule(rule, track, now)
            if firing is not None:
                firings.append(firing)
                track.last_fired[rule.rule_id] = now
        return firings

    def _evaluate_rule(
        self, rule: Rule, track: ProcessTrack, now: float,
    ) -> RuleFiring | None:
        # Cooldown: don't fire again within the cooldown window
        last = track.last_fired.get(rule.rule_id)
        if last is not None and (now - last) < self.cooldown_seconds:
            return None

        window_start = now - rule.duration_seconds
        # All samples in window must satisfy threshold
        in_window = [s for s in track.samples if s.timestamp >= window_start]
        if not in_window:
            return None

        # We need samples covering at least `duration_seconds` of wall time.
        # If the oldest in-window sample is younger than the window, we haven't
        # observed the condition long enough yet.
        oldest_ts = in_window[0].timestamp
        if (now - oldest_ts) < rule.duration_seconds:
            return None

        if rule.rule_type == "cpu":
            satisfying = sum(
                1 for s in in_window if s.cpu_percent >= rule.threshold_value
            )
        elif rule.rule_type == "memory":
            threshold_bytes = rule.threshold_bytes()
            assert threshold_bytes is not None
            satisfying = sum(
                1 for s in in_window if s.memory_rss_bytes >= threshold_bytes
            )
        else:
            return None
        if (satisfying / len(in_window)) < rule.sustain_fraction:
            return None

        return RuleFiring(
            rule=rule,
            track=track,
            fired_at=now,
            sustained_for_seconds=int(now - oldest_ts),
            peak_cpu_percent=track.peak_cpu_percent,
            peak_memory_bytes=track.peak_memory_bytes,
        )

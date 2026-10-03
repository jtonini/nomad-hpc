# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ per-user collector — rules: when a process, or a user's processes
together, count as heavy use of the host.

Pure logic: no I/O, no psutil, no database.

The collector runs from cron (``nomad collect --once`` every few minutes), a
fresh Python process each time, so nothing survives in memory from one
reading to the next. The engine that used to keep a rolling window of
samples in memory started empty every run and never saw a condition held
for its duration: under cron it could not fire.

So each reading says, for each rule, whether the condition held *over the
interval since the previous reading* -- CPU and I/O as averages over that
interval, from cumulative counters; memory as the value at the reading --
and the collector keeps, per process (or user) and rule, since when it has
held (``since``) in its state table. :func:`step` advances that, and
:func:`fires` says when it has held long enough.

A rule's ``duration_seconds`` is therefore "at least": with readings five
minutes apart, a 2-minute CPU rule fires on the first five-minute average
above its threshold.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# rule_type -> unit of threshold_value
RULE_TYPES: dict[str, str] = {
    "cpu": "percent",          # one process, % of one core, averaged over the interval
    "memory": "gb",            # one process, resident memory at the reading
    "io": "mb_s",              # one process, bytes read + written per second (needs root
                               # for other users' processes)
    "user_cpu": "percent",     # all of a user's processes together
    "user_memory": "gb",
}
PER_USER_TYPES = frozenset({"user_cpu", "user_memory"})
# Conditions measured as an average over the interval (they held across the
# whole of it); the others are values at the reading.
WINDOWED_TYPES = frozenset({"cpu", "io", "user_cpu"})
SEVERITIES = ("actionable", "informational")

GB = 1024 ** 3
MB = 1000 ** 2


@dataclass(frozen=True)
class Rule:
    """A single detection rule. Immutable; held in collector config."""
    rule_id: str                          # stable identifier, used in dedup keys
    rule_type: str                        # a key of RULE_TYPES
    threshold_value: float                # 10.0 (percent), 4.0 (gb), 50 (mb_s)
    threshold_unit: str
    duration_seconds: int
    severity: str = "actionable"
    edu_template_id: str | None = None    # optional handoff to edu engine

    @property
    def per_user(self) -> bool:
        return self.rule_type in PER_USER_TYPES

    @property
    def windowed(self) -> bool:
        return self.rule_type in WINDOWED_TYPES

    def threshold_bytes(self) -> int | None:
        """For memory rules, the threshold in bytes."""
        if self.threshold_unit == "gb":
            return int(self.threshold_value * GB)
        return None

    def value(self, reading: Reading) -> float | None:
        """The reading's value in the rule's unit (None: not measured)."""
        if self.rule_type in ("cpu", "user_cpu"):
            return reading.cpu_percent
        if self.rule_type in ("memory", "user_memory"):
            return None if reading.memory_bytes is None else reading.memory_bytes / GB
        if self.rule_type == "io":
            return None if reading.io_bytes_per_s is None else reading.io_bytes_per_s / MB
        return None


# Thresholds from the spydur/arachne validation (May 2026). Durations are
# minimums: readings come every few minutes. cpu_10pct_5min is
# informational: under cron it means 30 CPU-seconds in one 5-minute
# interval, which a pip install or a short compile reaches.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule("cpu_10pct_5min", "cpu", 10.0, "percent", 300,
         "informational", "head_node_cpu_sustained"),
    Rule("cpu_50pct_2min", "cpu", 50.0, "percent", 120,
         "actionable", "head_node_cpu_high"),
    Rule("memory_4gb_10min", "memory", 4.0, "gb", 600,
         "informational", "head_node_memory_moderate"),   # IDE / language-server case
    Rule("memory_16gb_2min", "memory", 16.0, "gb", 120,
         "actionable", "head_node_memory_high"),
    # A user's processes together: many small ones (make -j, a pool of
    # workers, a shell loop) that no single-process rule sees.
    Rule("user_cpu_200pct_10min", "user_cpu", 200.0, "percent", 600,
         "actionable", "head_node_user_cpu"),
    Rule("user_memory_32gb_10min", "user_memory", 32.0, "gb", 600,
         "actionable", "head_node_user_memory"),
    # Data moved through the host by one process (copies to NFS, transfers).
    # Measured only where the collector can read the process's I/O.
    Rule("io_50mbs_10min", "io", 50.0, "mb_s", 600,
         "informational", "head_node_io"),
)


@dataclass
class Reading:
    """What one reading measured for a process, or for a user's processes."""
    now: float                            # unix seconds of the reading
    window_start: float | None            # start of the interval the averages cover
    cpu_percent: float | None = None      # average over [window_start, now]
    memory_bytes: int | None = None       # at the reading
    io_bytes_per_s: float | None = None   # average over [window_start, now]
    # First sight of a process that was already running at the previous
    # reading: its averages are since it started, which may hide that it
    # went idle long ago. They start the count but fire nothing until a
    # reading over a real interval confirms them.
    provisional: bool = False


def step(rule: Rule, reading: Reading, since: float | None) -> float | None:
    """Since when the rule's condition has held, after this reading.

    None when it does not hold now. An average over the interval that is
    above the threshold means it held since the interval began; a value at
    the reading, only since now. A reading that did not measure the value
    (a run by hand seconds after cron's, too short for an average) is no
    news either way: ``since`` stays as it was.
    """
    v = rule.value(reading)
    if v is None:
        return since
    if v < rule.threshold_value:
        return None
    if since is not None:
        return since
    if rule.windowed and reading.window_start is not None:
        return reading.window_start
    return reading.now


def tolerance(rule: Rule) -> float:
    """Slack for cron jitter: a 5-minute rule over a 299-second interval."""
    return min(30.0, 0.1 * rule.duration_seconds)


def fires(rule: Rule, since: float | None, now: float) -> bool:
    return since is not None and (now - since) >= rule.duration_seconds - tolerance(rule)


def advance(rule: Rule, reading: Reading, since: float | None) -> tuple[float | None, bool]:
    """:func:`step`, and whether the rule fires on this reading: only on one
    that measured its value, and for an average, over a real interval."""
    since = step(rule, reading, since)
    measured = rule.value(reading) is not None
    confirmed = not (rule.windowed and reading.provisional)
    return since, measured and confirmed and fires(rule, since, reading.now)


def parse_rules(items) -> tuple[Rule, ...]:
    """Rules from ``[[collectors.per_user.rules]]`` tables.

    Each needs rule_id, rule_type, threshold (or threshold_value) and
    duration_seconds; severity defaults to actionable. A rule that does not
    parse is warned about and left out.
    """
    out = []
    for i, d in enumerate(items or ()):
        try:
            rule_type = str(d["rule_type"])
            if rule_type not in RULE_TYPES:
                raise ValueError(f"rule_type {rule_type!r} is not one of {', '.join(RULE_TYPES)}")
            severity = str(d.get("severity", "actionable"))
            if severity not in SEVERITIES:
                raise ValueError(f"severity {severity!r} is not one of {', '.join(SEVERITIES)}")
            threshold = float(d["threshold"] if "threshold" in d else d["threshold_value"])
            duration = int(d["duration_seconds"])
            if threshold <= 0 or duration < 0:
                raise ValueError("threshold must be positive and duration_seconds not negative")
            out.append(Rule(
                rule_id=str(d["rule_id"]), rule_type=rule_type,
                threshold_value=threshold, threshold_unit=RULE_TYPES[rule_type],
                duration_seconds=duration, severity=severity,
                edu_template_id=d.get("edu_template_id"),
            ))
        except (KeyError, TypeError, ValueError) as e:
            logger.warning("per_user: rule %d left out (%s): %r", i + 1, e, d)
    ids = [r.rule_id for r in out]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        logger.warning("per_user: rule ids used twice, later ones left out: %s",
                       ", ".join(sorted(dupes)))
        seen, kept = set(), []
        for r in out:
            if r.rule_id not in seen:
                seen.add(r.rule_id)
                kept.append(r)
        out = kept
    return tuple(out)

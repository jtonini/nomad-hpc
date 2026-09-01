# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAD Edu Insights — User-Facing Recommendation Aggregator

Aggregates per-job recommendations (already computed in scoring.py) into a
structured summary across a user's recent jobs. Powers `nomad edu me` for
self-service users who want to know "how am I doing on this cluster?"

Aggregation strategy is dimension-aware:

    Discrete (cores, GPUs)     → mode of suggested_value
    Continuous (mem, time)     → quantile of actual_usage × buffer factor

This lets continuous quantities aggregate sensibly. Mode-of-string fails on
continuous values because each job produces a unique recommendation; the
distribution is what carries the signal.

Per threshold baseline 2026-05-01:
    Default threshold = 40 across all dimensions
    Configurable via [thresholds] section in nomad.toml
    A dimension surfaces only if >50% of recent jobs scored below threshold
"""

from __future__ import annotations

import logging
import sqlite3
import statistics
from collections import Counter
from itertools import groupby
from dataclasses import dataclass, field
from typing import Any, Optional

from nomad.edu.progress import _count_user_jobs, _load_user_jobs, _load_user_sessions, _score_jobs, _score_sessions
from nomad.edu.scoring import (
    JobFingerprint,
    SessionFingerprint,
    Suggestion,
    format_duration_human,
    format_memory,
    format_time_slurm,
    round_memory_up,
    round_time_up,
)

logger = logging.getLogger(__name__)


# ── Defaults ─────────────────────────────────────────────────────────

DEFAULT_THRESHOLDS: dict[str, float] = {
    "cpu":             40.0,
    "memory":          40.0,
    "time":            40.0,
    "io":              40.0,
    "gpu":             40.0,
    "memory_pressure": 40.0,   # workstation: how saturated host RAM was
    "duration_fit":    40.0,   # workstation: how long the session ran
}

DEFAULT_SYSTEMIC_RATIO = 0.5
SEVERITY_CRITICAL_AVG = 15.0
SEVERITY_HIGH_AVG     = 30.0
SEVERITY_CRITICAL_RATIO = 0.8

# Buffer factors for continuous-quantity recommendations.
# Configurable in future via TOML; constants for now.
MEMORY_BUFFER_FACTOR = 2.0
TIME_BUFFER_FACTOR = 1.5
USAGE_QUANTILE = 0.95  # use p95 of usage (covers most of the user's jobs)


# Map short dim keys (used in fingerprints) to display names
KEY_TO_DISPLAY = {
    "cpu":             "CPU Efficiency",
    "memory":          "Memory Efficiency",
    "time":            "Time Estimation",
    "io":              "Filesystem I/O",
    "gpu":             "GPU Utilization",
    "memory_pressure": "Workstation Memory Pressure",
    "duration_fit":    "Workstation Session Duration",
}

# Human-readable labels for directives (NØMAD is for non-specialists too):
# directive -> (SLURM flag shown to the user, singular unit, plural unit).
DIRECTIVE_LABEL = {
    "ntasks": ("--ntasks", "core", "cores"),
    "gres":   ("--gres=gpu", "GPU", "GPUs"),
    "mem":    ("--mem", "", ""),      # value already carries a unit (e.g. 32G)
    "time":   ("--time", "", ""),     # value is HH:MM:SS
}

# Aggregation strategy per directive type.
#   "mode"          -> pick the most common suggested_value (integers)
#   "quantile"      -> p95 of actual_usage × buffer factor (continuous)
DIRECTIVE_STRATEGY = {
    "ntasks": "mode",
    "gres":   "mode",
    "mem":    "quantile",
    "time":   "quantile",
}

# Verdict thresholds — stricter than per-dimension threshold of 40, because
# verdicts fire only when a dimension is genuinely bad (not borderline).
VERDICT_MEMORY_PRESSURE_MAX = 20.0   # score <= 20 means peak >= 80% of host RAM
VERDICT_DURATION_FIT_MAX    = 30.0   # score <= 30 means span >= 12h
VERDICT_MIN_SESSIONS        = 2      # pattern, not a one-off
VERDICT_CHRONIC_MIN_SESSIONS = 10   # many pressured sessions (any duration) = chronic
VERDICT_HEADROOM_MIN        = 2.0    # cluster tier must be >= 2x peak to fire Verdict A



# ── Data classes ─────────────────────────────────────────────────────

@dataclass
class UsageStats:
    """Distribution of actual usage across affected jobs."""
    median: float
    p25: float
    p75: float
    min: float
    max: float
    unit: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "median": self.median,
            "p25": self.p25,
            "p75": self.p75,
            "min": self.min,
            "max": self.max,
            "unit": self.unit,
        }


@dataclass
class Issue:
    """One systemic recommendation across a user's recent jobs."""
    dimension: str
    dimension_key: str
    affected_jobs: int
    total_applicable: int
    avg_score: float
    severity: str           # "critical" / "high" / "medium"
    trajectory: str         # "improving" / "stable" / "worsening"

    # Recommendation: structured numeric + display strings
    directive: str = ""               # "mem", "time", "ntasks", "gres"
    suggested_value: float = 0.0      # final recommendation in canonical units
    suggested_display: str = ""       # "4G", "18:00:00", "1"
    directive_label: str = ""         # human phrase: "--ntasks from 8 to 1 core"
    current_value_typical: float = 0.0  # modal/median of current requests
    current_display: str = ""         # "200 GB"
    usage_stats: UsageStats | None = None
    strategy: str = ""                # "mode" / "p95_with_buffer"
    rationale: str = ""               # why the problem matters (educational framing)
    suggestion_rationale: str = ""    # how the suggested value was chosen
    kind: str = "dimension"           # "dimension" (per-dim aggregate) or "verdict" (cross-cutting)
    context: dict[str, Any] | None = None  # structured payload for verdict-kind issues (target cluster, sbatch snippet, ...)
    cluster: str | None = None        # scope: which cluster this dimension issue came from (None = unscoped/verdict)
    partition: str | None = None      # scope: which partition within that cluster

    @property
    def affected_ratio(self) -> float:
        return (self.affected_jobs / self.total_applicable
                if self.total_applicable else 0.0)


@dataclass
class UserInsights:
    """Aggregate insight summary for a user across recent jobs and sessions."""
    username: str
    job_count: int              # jobs the engine analyzed (finished states)
    window_days: int
    session_count: int = 0
    session_window_days: int = 7
    total_job_count: int = 0    # all of the user's jobs (any state) — the
                                # count shown to users, matching the dashboard
    issues: list[Issue] = field(default_factory=list)
    overall_trajectory: str = "stable"
    overall_score: float = 0.0

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)


# ── Configuration ────────────────────────────────────────────────────

def _load_thresholds(config: dict[str, Any] | None = None) -> dict[str, float]:
    """Load thresholds from config, falling back to defaults."""
    thresholds = dict(DEFAULT_THRESHOLDS)
    if config and isinstance(config.get("thresholds"), dict):
        for key, value in config["thresholds"].items():
            if key in thresholds:
                try:
                    thresholds[key] = float(value)
                except (TypeError, ValueError):
                    logger.warning(
                        f"Invalid threshold for {key}: {value!r}; using default"
                    )
    return thresholds


def _load_cluster_capacities(db_path: str) -> list[dict[str, Any]]:
    """
    Load distinct cluster memory tiers from node_state.

    Returns a list of {cluster, memory_mb, memory_gb, node_count, partitions}
    dicts, sorted ascending by memory_mb. Used by the verdict builder to
    pick a promotion target. Site-agnostic: reads whatever clusters the
    DB has been syncing.

    Empty list means no recent node_state data — verdict builder will
    treat this as "cannot promote anywhere" rather than crashing.
    """
    capacities: list[dict[str, Any]] = []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                """
                SELECT cluster,
                       memory_total_mb,
                       COUNT(DISTINCT node_name) AS node_count,
                       GROUP_CONCAT(DISTINCT partitions) AS partitions
                FROM node_state
                WHERE timestamp > datetime('now', '-1 day')
                  AND memory_total_mb > 0
                GROUP BY cluster, memory_total_mb
                ORDER BY memory_total_mb ASC
                """
            ).fetchall()
            for cluster, mem_mb, node_count, partitions in rows:
                capacities.append({
                    "cluster": cluster,
                    "memory_mb": int(mem_mb),
                    "memory_gb": round(mem_mb / 1024.0, 1),
                    "node_count": int(node_count),
                    "partitions": partitions or "",
                })
        finally:
            con.close()
    except sqlite3.Error as e:
        logger.warning(f"_load_cluster_capacities: DB error: {e}")
    return capacities


def _load_partition_wait(db_path: str) -> dict[tuple[str, str], float]:
    """
    Historical queue wait per (cluster, partition), in SECONDS, from
    jobs.wait_time_seconds. Used by the queue-aware promotion verdict to weigh
    "how long would I wait if I moved to the cluster?".

    Uses the MEDIAN (robust to outliers) over recent completed jobs.

    Returns {} when there is no jobs table, no wait data, or any error — a
    common case for smaller institutions that register only workstations and
    no HPC cluster. An empty result means "no queue signal": the verdict then
    falls back to the memory-only recommendation and makes NO queue claims.
    Never crashes, never fabricates.
    """
    waits: dict[tuple[str, str], list[float]] = {}
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                """
                SELECT cluster, partition, wait_time_seconds
                FROM jobs
                WHERE wait_time_seconds IS NOT NULL
                  AND wait_time_seconds >= 0
                  AND cluster IS NOT NULL
                  AND partition IS NOT NULL
                """
            ).fetchall()
            for cluster, partition, wait in rows:
                waits.setdefault((cluster, partition), []).append(float(wait))
        finally:
            con.close()
    except sqlite3.Error as e:
        logger.warning(f"_load_partition_wait: DB error: {e}")
        return {}
    return {
        key: statistics.median(vals)
        for key, vals in waits.items()
        if vals
    }


# Thresholds for detecting workstation memory-thrashing (tunable constants,
# portable across deployments — describe a pattern, not a site).
THRASH_SWAP_MB_MIN    = 2048.0   # active swap well above idle baseline
THRASH_IOWAIT_PCT_MIN = 15.0     # CPU stalled on I/O (paging) this fraction


def _assess_thrashing(db_path: str, hostname: str) -> dict[str, Any] | None:
    """
    Assess whether a workstation host is memory-THRASHING — i.e. slow *because*
    of memory pressure (paging to disk), not merely at the RAM ceiling.

    This is the evidence that distinguishes "the cluster is genuinely faster
    even with a queue" (thrashing: the workstation wastes time swapping) from
    "at the ceiling but fine" (no thrashing: an honest tradeoff).

    Reads the latest workstation_state row for the host: swap_used_mb and
    cpu_iowait_pct. Thrashing = active swap AND elevated iowait.

    Returns None when there is no host state at all (can't assess — the
    verdict then makes NO thrashing claim and uses tradeoff wording). Never
    fabricates: absence of evidence is reported as absence, not as "fine".
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                """
                SELECT swap_used_mb, cpu_iowait_pct
                FROM workstation_state
                WHERE hostname = ?
                ORDER BY timestamp DESC
                LIMIT 1
                """,
                (hostname,),
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        logger.warning(f"_assess_thrashing: DB error: {e}")
        return None

    if row is None:
        return None  # no host state — cannot assess, make no claim

    swap_mb = float(row[0] or 0.0)
    iowait_pct = float(row[1] or 0.0)
    thrashing = (swap_mb >= THRASH_SWAP_MB_MIN
                 and iowait_pct >= THRASH_IOWAIT_PCT_MIN)
    return {
        "thrashing": thrashing,
        "swap_used_mb": swap_mb,
        "iowait_pct": iowait_pct,
        # Fraction of CPU time lost to I/O stall — used to estimate recovered
        # time on the cluster. Only meaningful when thrashing.
        "iowait_fraction": iowait_pct / 100.0 if thrashing else 0.0,
    }


# Queue-wait decision zones (SECONDS). Fixed at HEALTHY-SYSTEM ideals — NOT
# recalibrated to any deployment's reality. A partition whose real wait sits
# far above these is a capacity-constraint signal the verdict surfaces
# honestly, rather than normalizing a long queue as "fine".
QUEUE_ZONE_CLEAN_S  = 1 * 3600     # < 1h  — promote cleanly
QUEUE_ZONE_NOTE_S   = 4 * 3600     # 1-4h  — promote, note the wait
QUEUE_ZONE_CAVEAT_S = 24 * 3600    # 4-24h — promote, prominent caveat
# > 24h — HEDGE: promote only if thrashing recovery clearly beats the wait.


def _time_to_results(
    queue_wait_s: float | None,
    thrash: dict[str, Any] | None,
    compute_hours: float | None = None,
) -> dict[str, Any]:
    """
    Decide how queue wait modulates a promotion recommendation, weighted by
    thrashing evidence. Returns a structured verdict-modulation dict — NEVER a
    fabricated speedup number.

    Inputs (all optional; the function degrades honestly on missing data):
      queue_wait_s  — median queue wait for the target partition, or None if
                      no wait history (then no modulation: 'unknown' zone).
      thrash        — result of _assess_thrashing, or None if host state
                      couldn't be assessed (then no thrashing claim).
      compute_hours — measured actual compute time, if available. Used ONLY to
                      strengthen a confident message; never required, never the
                      sole basis (its production reliability is unverified).

    Returns {zone, thrashing, wait_hours, recommend, confidence, ...} where:
      zone       — 'unknown'|'clean'|'note'|'caveat'|'hedge'
      recommend  — 'promote' | 'promote_hedged'
      confidence — 'high' (thrashing proven, cluster clearly wins)
                   'moderate' (short/no queue)
                   'tradeoff' (long queue, no thrashing — honest tradeoff)
    """
    thrashing = bool(thrash and thrash.get("thrashing"))
    iowait_frac = float(thrash.get("iowait_fraction", 0.0)) if thrash else 0.0

    # No wait history -> no queue modulation. Memory-only verdict stands.
    if queue_wait_s is None:
        return {
            "zone": "unknown", "thrashing": thrashing,
            "wait_hours": None, "recommend": "promote",
            "confidence": "moderate",
        }

    wait_h = queue_wait_s / 3600.0

    # Zone from the fixed healthy-system thresholds.
    if queue_wait_s < QUEUE_ZONE_CLEAN_S:
        zone = "clean"
    elif queue_wait_s < QUEUE_ZONE_NOTE_S:
        zone = "note"
    elif queue_wait_s < QUEUE_ZONE_CAVEAT_S:
        zone = "caveat"
    else:
        zone = "hedge"

    # Short queues: promote (the memory case wins easily).
    if zone in ("clean", "note", "caveat"):
        return {
            "zone": zone, "thrashing": thrashing, "wait_hours": wait_h,
            "recommend": "promote",
            "confidence": "high" if thrashing else "moderate",
        }

    # HEDGE zone (>24h): promotion only clearly wins if the workstation is
    # thrashing badly enough that recovered compute time exceeds the wait.
    # We do NOT fabricate a runtime; we compare only when compute_hours is
    # available AND thrashing is proven. Otherwise: honest tradeoff.
    if thrashing and compute_hours and iowait_frac > 0:
        ws_effective_h = compute_hours / max(1e-6, (1.0 - iowait_frac))
        recovered_h = ws_effective_h - compute_hours  # time lost to paging
        # Cluster wins if the paging penalty (per the SAME work, and it
        # recurs every run) outweighs the one-time queue wait.
        if recovered_h >= wait_h:
            return {
                "zone": "hedge", "thrashing": True, "wait_hours": wait_h,
                "recommend": "promote",
                "confidence": "high",
                "recovered_hours": round(recovered_h, 1),
            }

    # Long queue, and we cannot show the cluster clearly wins -> hedge.
    return {
        "zone": "hedge", "thrashing": thrashing, "wait_hours": wait_h,
        "recommend": "promote_hedged",
        "confidence": "tradeoff",
    }


def _user_compute_hours(db_path: str, username: str) -> float | None:
    """
    Total ACTUAL compute time (hours) for a user's recent workstation sessions,
    from the cpu_usage_usec delta (MAX-MIN) per session_epoch. This is measured
    compute time, distinct from wall-clock session span.

    Returns None when there is no usable cpu_usage data (so the time-to-results
    comparison degrades to evidence-only wording rather than fabricating a
    number). Production reliability of cpu_usage under load is unverified, so a
    None here is expected and handled honestly downstream.
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = con.execute(
                """
                SELECT MAX(cpu_usage_usec) - MIN(cpu_usage_usec)
                FROM workstation_user_snapshot
                WHERE username = ?
                GROUP BY session_epoch
                """,
                (username,),
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        logger.warning(f"_user_compute_hours: DB error: {e}")
        return None
    deltas = [float(r[0]) for r in rows if r[0] is not None and r[0] > 0]
    if not deltas:
        return None
    total_usec = sum(deltas)
    return total_usec / 1_000_000 / 3600.0


# ── Aggregation strategies ───────────────────────────────────────────

def _quantile(values: list[float], q: float) -> float:
    """Compute the q-th quantile of values (0 <= q <= 1). Linear interpolation."""
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    sorted_vals = sorted(values)
    pos = q * (len(sorted_vals) - 1)
    lower_idx = int(pos)
    frac = pos - lower_idx
    if lower_idx + 1 >= len(sorted_vals):
        return sorted_vals[-1]
    return sorted_vals[lower_idx] + frac * (
        sorted_vals[lower_idx + 1] - sorted_vals[lower_idx]
    )


def _aggregate_mode(suggestions: list[Suggestion]) -> tuple[float, str, str]:
    """
    Mode-based aggregation for discrete quantities (cores, GPUs).

    Returns (suggested_value, strategy_label, rationale).
    """
    values = [s.suggested_value for s in suggestions]
    counter = Counter(values)
    modal_value, modal_count = counter.most_common(1)[0]
    # Plain-language: describe WHY this value without a raw fraction that
    # collides with the card's "N of M jobs flagged" count.
    if modal_count == len(suggestions):
        rationale = "every flagged job requested the same, so this fits them all"
    else:
        rationale = "the most common request across your flagged jobs"
    return modal_value, "mode", rationale


def _aggregate_quantile(
    suggestions: list[Suggestion],
    buffer_factor: float,
    rounder,
) -> tuple[float, str, str]:
    """
    Quantile-based aggregation for continuous quantities (memory, time).

    Take p95 of actual_usage across jobs, multiply by buffer, round up to a
    SLURM-friendly value. Returns (suggested_value, strategy_label, rationale).
    """
    usages = [s.actual_usage for s in suggestions if s.actual_usage > 0]
    if not usages:
        # Fallback: use median of suggested_value as the engine had nothing else
        return _aggregate_mode(suggestions)
    p95 = _quantile(usages, USAGE_QUANTILE)
    target = p95 * buffer_factor
    suggested_value = rounder(target)
    pct_label = int(USAGE_QUANTILE * 100)
    rationale = (f"covers {pct_label}% of your jobs with "
                 f"{buffer_factor:g}x safety buffer")
    return suggested_value, f"p{pct_label}_with_{buffer_factor:g}x_buffer", rationale


def _build_usage_stats(suggestions: list[Suggestion]) -> UsageStats | None:
    """Compute the distribution of actual_usage across suggestions."""
    usages = [s.actual_usage for s in suggestions if s.actual_usage > 0]
    if not usages:
        return None
    sorted_u = sorted(usages)
    return UsageStats(
        median=statistics.median(sorted_u),
        p25=_quantile(sorted_u, 0.25),
        p75=_quantile(sorted_u, 0.75),
        min=sorted_u[0],
        max=sorted_u[-1],
        unit=suggestions[0].unit,
    )


def _typical_current_value(suggestions: list[Suggestion]) -> float:
    """
    Pick the typical current_value (what the user usually requests). Mode for
    discrete, median for continuous. Both use suggestion list directly.
    """
    values = [s.current_value for s in suggestions if s.current_value > 0]
    if not values:
        return 0.0
    counter = Counter(values)
    modal_value, modal_count = counter.most_common(1)[0]
    if modal_count >= len(values) * 0.5:
        return modal_value  # clearly typical
    return statistics.median(sorted(values))


def _format_value_for_directive(directive: str, value: float) -> str:
    """Render a numeric value as a human-readable string for the directive."""
    if directive == "mem":
        return format_memory(value)
    if directive == "time":
        return format_time_slurm(value)
    if directive in ("ntasks", "gres"):
        return str(int(value))
    return str(value)


# ── Aggregation: per-dimension Issue construction ────────────────────

def _compute_dimension_trajectory(
    fingerprints: list[JobFingerprint],
    dim_key: str,
) -> str:
    """First-half-vs-second-half comparison on this dimension's score."""
    if dim_key not in KEY_TO_DISPLAY:
        return "stable"
    scores = []
    for fp in fingerprints:
        d = fp.dimensions.get(dim_key)
        if d is not None and d.applicable:
            scores.append(d.score)
    if len(scores) < 4:
        return "stable"
    midpoint = len(scores) // 2
    first_half = sum(scores[:midpoint]) / midpoint
    second_half = sum(scores[midpoint:]) / (len(scores) - midpoint)
    delta = second_half - first_half
    if delta > 5:
        return "improving"
    if delta < -5:
        return "worsening"
    return "stable"


def _classify_severity(avg_score: float, affected_ratio: float) -> str:
    """Critical only if both avg score is severe AND prevalence is high."""
    if (avg_score <= SEVERITY_CRITICAL_AVG
            and affected_ratio >= SEVERITY_CRITICAL_RATIO):
        return "critical"
    if avg_score <= SEVERITY_HIGH_AVG:
        return "high"
    return "medium"


def _aggregate_dimension(
    dim_key: str,
    fingerprints: "list[JobFingerprint] | list[SessionFingerprint]",
    threshold: float,
) -> Issue | None:
    """Build an Issue for one dimension, or None if not systemic."""
    dim_name = KEY_TO_DISPLAY.get(dim_key, dim_key)
    affected_scores: list[float] = []
    affected_suggestions: list[Suggestion] = []
    total_applicable = 0

    for fp in fingerprints:
        d = fp.dimensions.get(dim_key)
        if d is None or not d.applicable:
            continue
        total_applicable += 1
        if d.score < threshold:
            affected_scores.append(d.score)
            if d.suggestion is not None:
                affected_suggestions.append(d.suggestion)

    if total_applicable == 0:
        return None

    affected_ratio = len(affected_scores) / total_applicable
    if affected_ratio < DEFAULT_SYSTEMIC_RATIO:
        return None

    avg_score = sum(affected_scores) / len(affected_scores)
    severity = _classify_severity(avg_score, affected_ratio)
    trajectory = _compute_dimension_trajectory(fingerprints, dim_key)

    # Rationale = why it matters (DIMENSION_FRAMING) + what to do
    # (DIMENSION_REMEDY, for dimensions with no single SLURM directive).
    # Portable across deployments; no site-specific paths.
    rationale = DIMENSION_FRAMING.get(dim_key, "")
    remedy = DIMENSION_REMEDY.get(dim_key, "")
    if remedy:
        rationale = f"{rationale} {remedy}".strip() if rationale else remedy

    issue = Issue(
        dimension=dim_name,
        dimension_key=dim_key,
        affected_jobs=len(affected_scores),
        total_applicable=total_applicable,
        avg_score=avg_score,
        severity=severity,
        trajectory=trajectory,
        rationale=rationale,
    )

    # If we have no structured suggestions to aggregate, return the issue
    # without a recommendation — the score is bad but we can't synthesize
    # a typical SLURM directive from no data (e.g. I/O dimension).
    if not affected_suggestions:
        return issue

    # All affected suggestions should share a directive (each scorer uses one).
    # Sanity check; if mixed, take the modal directive.
    directives = Counter(s.directive for s in affected_suggestions)
    primary_directive = directives.most_common(1)[0][0]
    same_directive = [s for s in affected_suggestions
                      if s.directive == primary_directive]

    issue.directive = primary_directive

    # Build usage distribution
    issue.usage_stats = _build_usage_stats(same_directive)

    # Pick typical current request value
    typical_current = _typical_current_value(same_directive)
    issue.current_value_typical = typical_current
    issue.current_display = _format_value_for_directive(
        primary_directive, typical_current
    )

    # Aggregate using the strategy for this directive
    strategy = DIRECTIVE_STRATEGY.get(primary_directive, "mode")
    if strategy == "mode":
        value, strategy_label, rationale = _aggregate_mode(same_directive)
    elif strategy == "quantile":
        if primary_directive == "mem":
            buffer = MEMORY_BUFFER_FACTOR
            rounder = round_memory_up
        elif primary_directive == "time":
            buffer = TIME_BUFFER_FACTOR
            rounder = round_time_up
        else:
            buffer = 1.5
            rounder = lambda x: int(x)
        value, strategy_label, rationale = _aggregate_quantile(
            same_directive, buffer, rounder,
        )
    else:
        value, strategy_label, rationale = _aggregate_mode(same_directive)

    issue.suggested_value = value
    issue.suggested_display = _format_value_for_directive(
        primary_directive, value
    )
    issue.strategy = strategy_label
    issue.suggestion_rationale = rationale

    # Human phrase for the card (NØMAD serves non-specialists): name the flag
    # and the unit, not a bare number. e.g. "--ntasks from 8 to 1 core".
    flag, unit_s, unit_p = DIRECTIVE_LABEL.get(primary_directive, ("", "", ""))
    def _u(display, canonical):
        # Append a unit word for count-style directives (ntasks/gres).
        if unit_s and unit_p:
            try:
                n = int(float(canonical))
            except (TypeError, ValueError):
                n = None
            word = unit_s if n == 1 else unit_p
            return f"{display} {word}"
        return display
    cur = _u(issue.current_display, issue.current_value_typical)
    sug = _u(issue.suggested_display, issue.suggested_value)
    if flag:
        issue.directive_label = f"{flag} from {cur} to {sug}"

    return issue


# ── Top-level overall trajectory ─────────────────────────────────────

def _classify_overall_trajectory(fingerprints: list[JobFingerprint]) -> str:
    """First-half vs second-half on overall score."""
    if len(fingerprints) < 4:
        return "stable"
    overall = [fp.overall for fp in fingerprints]
    midpoint = len(overall) // 2
    first_half = sum(overall[:midpoint]) / midpoint
    second_half = sum(overall[midpoint:]) / (len(overall) - midpoint)
    delta = second_half - first_half
    if delta > 5:
        return "improving"
    if delta < -5:
        return "declining"
    return "stable"


# ── Public API ───────────────────────────────────────────────────────


def _select_verdict_kind(
    peak_gb: float,
    host_gb: float,
    cluster_capacities: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
    """
    Decide which verdict fires based on cluster headroom.

    Returns (kind, target_or_None) where kind is one of:
        "promote"             — cluster has >= 2x headroom for peak_gb
        "workload_fits_tool"  — cluster lacks meaningful headroom; workstation
                                is the right tool. Positive framing.
        "exhausted_everywhere" — peak exceeds even largest cluster tier
                                 AND workstation is already top-of-fleet.
                                 Suggest external resources.
    """
    if not cluster_capacities:
        return ("workload_fits_tool", None)

    peak_mb = peak_gb * 1024
    threshold_mb = peak_mb * VERDICT_HEADROOM_MIN

    fitting = [c for c in cluster_capacities if c["memory_mb"] >= threshold_mb]
    if fitting:
        # smallest-fit selection — first element since list is ascending
        return ("promote", fitting[0])

    largest_cluster = cluster_capacities[-1]
    largest_cluster_gb = largest_cluster["memory_gb"]

    # Exhausted-everywhere: peak exceeds even the largest cluster node.
    if peak_gb >= largest_cluster_gb:
        return ("exhausted_everywhere", None)

    # Otherwise: cluster exists but lacks 2x headroom — workstation is right tool.
    return ("workload_fits_tool", None)


def _format_wait(hours: float) -> str:
    """Human-readable queue wait: '45 minutes', '5 hours', '2.5 days'."""
    if hours < 1:
        mins = max(1, int(round(hours * 60)))
        return f"{mins} minutes"
    if hours < 48:
        return f"{hours:.0f} hours" if hours >= 2 else "1 hour"
    return f"{hours / 24:.0f} days"


def _verdict_promote_message(
    peak_gb: float, host_gb: float, span_hours: float,
    target: dict[str, Any], session_count: int, reason: str = "acute",
    queue_mod: dict[str, Any] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Verdict A: cluster has meaningful headroom; recommend promotion."""
    headroom = target["memory_gb"] / peak_gb
    sbatch_mem_gb = int(peak_gb * 2)  # 2x peak as starting recommendation
    sbatch_time_h = min(span_hours * 1.5, 7 * 24)  # buffer, cap at 7 days
    sbatch_days = int(sbatch_time_h // 24)
    sbatch_hh = int(sbatch_time_h - sbatch_days * 24)
    time_str = f"{sbatch_days}-{sbatch_hh:02d}:00:00" if sbatch_days else f"{sbatch_hh:02d}:00:00"

    # Pick smallest representative partition name
    partitions = (target.get("partitions") or "").split(",")
    partition = next((p for p in partitions if p and p not in ("all",)), partitions[0] if partitions else "")

    if reason == "chronic":
        # Chronic: many short sessions repeatedly at the ceiling. Duration is
        # not the story — the recurring pressure is. Don't lean on span length.
        detail = (
            f"Across {session_count} recent sessions your work repeatedly ran "
            f"near this workstation's memory ceiling — peaking at "
            f"{peak_gb:.0f} GB on a {host_gb:.0f} GB host "
            f"({(peak_gb/host_gb)*100:.0f}% saturation). Each session is short, "
            f"but the pattern is consistent: your workflow has outgrown the box. "
            f"{target['cluster']}'s {target['memory_gb']:.0f} GB tier offers "
            f"{headroom:.1f}x headroom, so the same work would run without "
            f"crowding your workstation's RAM."
        )
    else:
        detail = (
            f"Across {session_count} recent sessions, your workload peaked at "
            f"{peak_gb:.0f} GB on a {host_gb:.0f} GB host ({(peak_gb/host_gb)*100:.0f}% saturation) "
            f"with spans averaging {span_hours:.1f}h. "
            f"{target['cluster']}'s {target['memory_gb']:.0f} GB tier offers "
            f"{headroom:.1f}x headroom and would let your work complete without "
            f"competing for your workstation's RAM."
        )
    # Queue-aware wording (plain language — never name-drops "thrashing").
    # Appends a sentence reflecting the target partition's queue and whether
    # the workstation is genuinely memory-bound. Skipped when no queue signal.
    if queue_mod and queue_mod.get("zone") not in (None, "unknown"):
        zone = queue_mod["zone"]
        wh = queue_mod.get("wait_hours")
        wait_str = _format_wait(wh) if wh is not None else "an unknown time"
        if queue_mod.get("recommend") == "promote_hedged":
            if queue_mod.get("thrashing"):
                # Memory-bound, but the queue is so long even the recovered
                # time can't beat it. Honest: acknowledge the bottleneck AND
                # that the queue defeats the move for now.
                detail += (
                    f" One caveat: although your sessions are running out of "
                    f"memory and losing time moving data to and from disk, "
                    f"this partition's queue is currently so long (typically "
                    f"~{wait_str}) that even that recovered time wouldn't beat "
                    f"the wait. For quick turnaround the workstation stays "
                    f"pragmatic right now — but a queue this long, on top of "
                    f"your memory pressure, is worth raising with your research "
                    f"computing team."
                )
            else:
                detail += (
                    f" One caveat: this partition's queue is currently very "
                    f"long (typically ~{wait_str}). Your sessions aren't "
                    f"memory-starved enough for the cluster to clearly beat "
                    f"that wait, so the workstation stays the pragmatic choice "
                    f"for quick turnaround right now — but a queue this long is "
                    f"worth raising with your research computing team."
                )
        elif zone == "hedge" and queue_mod.get("confidence") == "high":
            detail += (
                f" Even though this partition's queue is long (~{wait_str}), "
                f"your sessions are running out of memory and spending much of "
                f"their time moving data to and from disk instead of computing. "
                f"The cluster's headroom recovers that lost time, so you'd "
                f"likely finish sooner there despite the wait."
            )
        elif zone == "caveat":
            detail += (
                f" Note: this partition's queue typically runs ~{wait_str}, so "
                f"factor that into when you'll get results."
            )
        else:  # clean / note
            detail += f" The queue here is short (typically ~{wait_str})."

    sbatch_snippet = (
        f"#SBATCH --mem={sbatch_mem_gb}G\n"
        f"#SBATCH --time={time_str}\n"
        f"#SBATCH --partition={partition}"
    ) if partition else (
        f"#SBATCH --mem={sbatch_mem_gb}G\n"
        f"#SBATCH --time={time_str}"
    )
    context = {
        "verdict": "promote",
        "peak_gb": peak_gb,
        "host_gb": host_gb,
        "span_hours": span_hours,
        "session_count": session_count,
        "target_cluster": target["cluster"],
        "target_memory_gb": target["memory_gb"],
        "target_partition": partition,
        "headroom_ratio": round(headroom, 2),
        "sbatch_snippet": sbatch_snippet,
        "reason": reason,
        "queue": queue_mod,  # structured queue-modulation (or None)
    }
    return ("Cluster Promotion Recommended", detail, context)


def _verdict_workload_fits_tool_message(
    peak_gb: float, host_gb: float, span_hours: float,
    cluster_capacities: list[dict[str, Any]], session_count: int,
) -> tuple[str, str, dict[str, Any]]:
    """Verdict B: cluster lacks meaningful headroom; workstation is right tool."""
    largest_gb = cluster_capacities[-1]["memory_gb"] if cluster_capacities else 0
    detail = (
        f"Across {session_count} recent sessions, your workload's memory "
        f"footprint ({peak_gb:.0f} GB peak) is well-matched to your "
        f"workstation's capacity ({host_gb:.0f} GB)."
    )
    if largest_gb > 0:
        detail += (
            f" The cluster's largest tier ({largest_gb:.0f} GB) offers only "
            f"{largest_gb/peak_gb:.1f}x headroom, which doesn't justify migration "
            f"overhead."
        )
    detail += (
        " The workstation is the right tool here. Consider best-neighbor "
        "practices: nice levels, cgroup memory limits, and off-hours "
        "scheduling if others share the host."
    )
    context = {
        "verdict": "workload_fits_tool",
        "peak_gb": peak_gb,
        "host_gb": host_gb,
        "span_hours": span_hours,
        "session_count": session_count,
        "largest_cluster_gb": largest_gb,
    }
    return ("Workstation Is The Right Tool", detail, context)


def _verdict_exhausted_message(
    peak_gb: float, host_gb: float, span_hours: float, session_count: int,
) -> tuple[str, str, dict[str, Any]]:
    """Verdict C: peak exceeds even largest cluster; suggest external resources."""
    detail = (
        f"Your workload ({peak_gb:.0f} GB peak across {session_count} sessions) "
        f"exceeds the memory capacity of every cluster node available locally. "
        f"For sustained needs at this scale, consider national-scale resources "
        f"(ACCESS allocations, Bridges-2) or memory-optimized cloud instances."
    )
    context = {
        "verdict": "exhausted_everywhere",
        "peak_gb": peak_gb,
        "host_gb": host_gb,
        "span_hours": span_hours,
        "session_count": session_count,
    }
    return ("Workload Exceeds Local Capacity", detail, context)


def _build_cluster_promotion_verdict(
    fingerprints: list[SessionFingerprint],
    cluster_capacities: list[dict[str, Any]],
    db_path: str | None = None,
) -> Issue | None:
    """
    Build a cluster-promotion verdict if the user shows a pattern of
    sessions with co-occurring memory pressure AND long duration.

    Returns None when the verdict doesn't fire. Otherwise returns an
    Issue with kind="verdict" carrying structured context for Console
    and CLI to render.
    """
    if not fingerprints:
        return None

    # Find sessions where BOTH dimensions are below the verdict thresholds.
    qualifying: list[tuple[SessionFingerprint, float, float, float]] = []
    for fp in fingerprints:
        mp = fp.dimensions.get("memory_pressure")
        df = fp.dimensions.get("duration_fit")
        if mp is None or df is None:
            continue
        if not mp.applicable or not df.applicable:
            continue
        if mp.score > VERDICT_MEMORY_PRESSURE_MAX:
            continue
        if df.score > VERDICT_DURATION_FIT_MAX:
            continue
        peak_b = (mp.raw or {}).get("peak_bytes", 0)
        host_b = (mp.raw or {}).get("host_total_bytes", 0)
        span_h = (df.raw or {}).get("span_hours", 0.0)
        if peak_b <= 0 or host_b <= 0:
            continue
        peak_gb = peak_b / 1024 / 1024 / 1024
        host_gb = host_b / 1024 / 1024 / 1024
        qualifying.append((fp, peak_gb, host_gb, span_h))

    # Acute path: >= VERDICT_MIN_SESSIONS sessions each pressured AND long.
    # If that doesn't fire, fall back to the CHRONIC path: many pressured
    # sessions regardless of duration. A user whose workflow repeatedly sits
    # near the RAM ceiling has outgrown the workstation even when no single
    # session is long (each is "fine" alone; the pattern is not).
    reason = "acute"
    if len(qualifying) < VERDICT_MIN_SESSIONS:
        chronic: list[tuple[SessionFingerprint, float, float, float]] = []
        for fp in fingerprints:
            mp = fp.dimensions.get("memory_pressure")
            df = fp.dimensions.get("duration_fit")
            if mp is None or not mp.applicable:
                continue
            if mp.score > VERDICT_MEMORY_PRESSURE_MAX:
                continue  # not pressured — duration is NOT required here
            peak_b = (mp.raw or {}).get("peak_bytes", 0)
            host_b = (mp.raw or {}).get("host_total_bytes", 0)
            span_h = (df.raw or {}).get("span_hours", 0.0) if df is not None else 0.0
            if peak_b <= 0 or host_b <= 0:
                continue
            chronic.append((fp, peak_b / 1024**3, host_b / 1024**3, span_h))
        if len(chronic) < VERDICT_CHRONIC_MIN_SESSIONS:
            return None
        qualifying = chronic
        reason = "chronic"

    # Take the peak case as the headline (max peak_gb across qualifying)
    headline = max(qualifying, key=lambda q: q[1])
    _, peak_gb, host_gb, span_hours = headline
    session_count = len(qualifying)

    kind, target = _select_verdict_kind(peak_gb, host_gb, cluster_capacities)

    # Queue-aware modulation: how long is the target partition's queue, and is
    # the workstation actually memory-bound (worth the wait)? Computed only
    # when we have a DB to read; degrades to plain promote otherwise (e.g.
    # workstation-only institutions with no cluster job history).
    queue_mod = None
    if db_path is not None and kind == "promote" and target is not None:
        headline_fp = headline[0]
        _hostname = getattr(headline_fp, "hostname", None)
        _username = getattr(headline_fp, "username", None)
        _parts = (target.get("partitions") or "").split(",")
        _tpart = next((p for p in _parts if p and p != "all"),
                      _parts[0] if _parts else "")
        _waits = _load_partition_wait(db_path)
        _wait_s = _waits.get((target.get("cluster"), _tpart))
        _thrash = _assess_thrashing(db_path, _hostname) if _hostname else None
        _comp_h = _user_compute_hours(db_path, _username) if _username else None
        queue_mod = _time_to_results(_wait_s, _thrash, _comp_h)

    if kind == "promote":
        title, detail, context = _verdict_promote_message(
            peak_gb, host_gb, span_hours, target, session_count, reason,
            queue_mod,
        )
        severity = "high"
    elif kind == "exhausted_everywhere":
        title, detail, context = _verdict_exhausted_message(
            peak_gb, host_gb, span_hours, session_count
        )
        severity = "medium"
    else:
        title, detail, context = _verdict_workload_fits_tool_message(
            peak_gb, host_gb, span_hours, cluster_capacities, session_count
        )
        severity = "medium"

    return Issue(
        dimension=title,
        dimension_key="cluster_promotion",
        affected_jobs=session_count,
        total_applicable=len(fingerprints),
        avg_score=0.0,  # not a per-dim score
        severity=severity,
        trajectory="stable",
        rationale=detail,
        kind="verdict",
        context=context,
    )

def user_insights(
    db_path: str,
    username: str,
    days: int = 90,
    config: dict[str, Any] | None = None,
    session_days: int = 7,
    cluster_capacities: list[dict[str, Any]] | None = None,
) -> UserInsights:
    """
    Compute systemic insights for a user across recent jobs AND workstation
    sessions.

    Returns UserInsights with issues ordered: cluster-promotion verdict
    first (when it fires), then per-dimension job issues sorted by severity.

    Workstation sessions are loaded unconditionally. A user with cluster
    jobs but no workstation sessions simply gets no verdict; a user with
    sessions but no cluster jobs still gets their verdict (the early
    return only short-circuits when BOTH are empty).

    cluster_capacities, if provided, overrides the live node_state lookup
    (used by tests and by callers who cache capacities, e.g. the Console).
    """
    thresholds = _load_thresholds(config)
    rows = _load_user_jobs(db_path, username, days=days)
    fingerprints = _score_jobs(rows)

    # Workstation sessions — loaded regardless of whether the user has jobs.
    session_rows = _load_user_sessions(db_path, username, days=session_days)
    session_fingerprints = _score_sessions(session_rows)

    total_jobs = _count_user_jobs(db_path, username, days=days)
    insights = UserInsights(
        username=username,
        job_count=len(fingerprints),
        total_job_count=total_jobs,
        window_days=days,
        session_count=len(session_fingerprints),
        session_window_days=session_days,
    )

    # Build the cross-cutting cluster-promotion verdict from sessions.
    if cluster_capacities is None:
        cluster_capacities = _load_cluster_capacities(db_path)
    verdict = _build_cluster_promotion_verdict(
        session_fingerprints, cluster_capacities, db_path=db_path
    )

    # Short-circuit only when there's nothing at all to report.
    if not fingerprints and verdict is None:
        return insights

    issues: list[Issue] = []

    if fingerprints:
        insights.overall_score = (
            sum(fp.overall for fp in fingerprints) / len(fingerprints)
        )
        insights.overall_trajectory = _classify_overall_trajectory(fingerprints)

        # Group job fingerprints by (cluster, partition) so each dimension
        # issue is scoped to where it actually occurs. Blending clusters
        # would hide which one is the problem (e.g. a user wasteful on
        # hpc1/compute but fine on hpc2/gpu would show one muddied issue).
        def _group_key(fp: JobFingerprint) -> tuple[str, str]:
            return (fp.cluster or "", fp.partition or "")
        for (grp_cluster, grp_partition), group_iter in groupby(
            sorted(fingerprints, key=_group_key), key=_group_key
        ):
            group = list(group_iter)
            for dim_key in KEY_TO_DISPLAY:
                # Workstation dimensions live in session fingerprints, not
                # job fingerprints — skip them; signal surfaces via verdict.
                if dim_key in ("memory_pressure", "duration_fit"):
                    continue
                issue = _aggregate_dimension(
                    dim_key, group,
                    threshold=thresholds[dim_key],
                )
                if issue is not None:
                    issue.cluster = grp_cluster or None
                    issue.partition = grp_partition or None
                    issues.append(issue)

        severity_rank = {"critical": 0, "high": 1, "medium": 2}
        # Location first (cluster, partition), then severity within — reads
        # as "what's wrong on hpc1/compute, then hpc2/gpu".
        issues.sort(key=lambda i: (
            i.cluster or "", i.partition or "",
            severity_rank[i.severity], i.avg_score,
        ))

    # Option 1: verdict always leads when present.
    if verdict is not None:
        insights.issues = [verdict] + issues
    else:
        insights.issues = issues

    return insights


# ── Formatting ───────────────────────────────────────────────────────

# Per-dimension educational framing — one line each, shown in default output
DIMENSION_FRAMING = {
    "cpu": ("You allocated cores but didn't use them; jobs that could "
            "have used them waited."),
    "memory": ("Memory you don't use is unavailable to other jobs on the node."),
    "time": ("Over-requesting walltime delays your jobs in the queue and "
             "blocks backfill scheduling for everyone."),
    "io": ("Heavy NFS use slows your job and saturates shared storage "
           "for other users."),
    "gpu": ("GPUs are scarce; reserving them without using them blocks "
            "other GPU-needy jobs."),
    "memory_pressure": ("Your interactive sessions are pushing your "
            "workstation's RAM close to capacity, which can slow other "
            "users on the same host and risk OOM kills."),
    "duration_fit": ("Long-running interactive sessions on shared "
            "workstations hold resources for others and lack the "
            "scheduling fairness of cluster jobs."),
}

# Actionable remedies for dimensions that have no single SLURM directive to
# suggest (cpu/time/memory/gpu already emit concrete #SBATCH changes). Kept
# portable across deployments — describes the pattern, not a site path.
DIMENSION_REMEDY = {
    "io": ("Stage inputs to node-local scratch at the start of the job and "
           "write results there, copying back at the end — this keeps heavy "
           "I/O off the shared filesystem and usually runs faster too."),
}


def _format_usage_line(stats: UsageStats, directive: str) -> str:
    """Render usage distribution as a human-readable line."""
    if directive == "mem":
        return (f"use {format_memory(stats.median)} median "
                f"(range {format_memory(stats.min)} – "
                f"{format_memory(stats.max)})")
    if directive == "time":
        return (f"run for {format_duration_human(stats.median)} median "
                f"(range {format_duration_human(stats.min)} – "
                f"{format_duration_human(stats.max)})")
    return (f"use {stats.median:.1f} median "
            f"(range {stats.min:.1f}–{stats.max:.1f} {stats.unit})")


def format_user_insights(insights: UserInsights, detailed: bool = False) -> str:
    """Render UserInsights as text suitable for terminal output."""
    lines: list[str] = []

    if insights.job_count == 0 and not insights.issues:
        return (f"No recent jobs found for {insights.username} "
                f"in the last {insights.window_days} days.\n"
                f"If you've run jobs recently, ensure the cluster's "
                f"job_metrics collector is current (>= v1.5.6).")

    lines.append(f"  Your NØMAD Profile — {insights.username}")
    lines.append(f"  {'─' * 56}")
    if insights.total_job_count > 0:
        lines.append(f"  {insights.total_job_count} jobs in the last {insights.window_days} days")
        lines.append(f"  Overall score: {insights.overall_score:.1f} / 100  "
                     f"({insights.overall_trajectory})")
    else:
        lines.append("  Based on your recent workstation sessions")
    lines.append("")

    if not insights.issues:
        lines.append("  No systemic issues detected.")
        lines.append("  Either you're doing great, or your jobs vary too much")
        lines.append("  to flag any single dimension.")
        return "\n".join(lines)

    lines.append("  Top issues across your recent jobs:")
    lines.append(f"  {'─' * 56}")

    for issue in insights.issues:
        # Verdicts (cluster-promotion) carry full guidance in the rationale —
        # including the queue-aware caveat. Render directly, not through the
        # per-dimension job template.
        if issue.kind == "verdict":
            import textwrap
            lines.append("")
            lines.append(f"  [RECOMMENDATION] {issue.dimension}")
            if issue.rationale:
                for para in textwrap.wrap(issue.rationale, width=72):
                    lines.append(f"    {para}")
            ctx = issue.context or {}
            snippet = ctx.get("sbatch_snippet")
            if snippet:
                lines.append("")
                lines.append("    Suggested batch script header:")
                for sl in snippet.split("\n"):
                    lines.append(f"      {sl}")
            continue
        traj_arrow = {
            "improving": "↑ improving",
            "stable":    "→ not improving",
            "worsening": "↓ getting worse",
        }.get(issue.trajectory, issue.trajectory)
        sev_marker = {
            "critical": "[CRITICAL]",
            "high":     "[HIGH]    ",
            "medium":   "[MEDIUM]  ",
        }.get(issue.severity, "[?]       ")

        scope = ""
        if issue.cluster and issue.partition:
            scope = f" ({issue.cluster}/{issue.partition})"
        lines.append("")
        lines.append(f"  {sev_marker} {issue.dimension}{scope} — {traj_arrow}")
        lines.append(
            f"    {issue.affected_jobs}/{issue.total_applicable} jobs "
            f"scored below threshold (avg score: {issue.avg_score:.1f})"
        )

        # Show usage context if we have it
        if issue.usage_stats and issue.current_display:
            lines.append("")
            lines.append(f"    Your jobs:    "
                         f"request {issue.current_display} (typical)")
            lines.append(f"                  "
                         f"{_format_usage_line(issue.usage_stats, issue.directive)}")
            if issue.current_value_typical > 0 and issue.usage_stats.median > 0:
                util = (issue.usage_stats.median /
                        issue.current_value_typical * 100)
                lines.append(f"                  that's {util:.1f}% utilization")

        # Educational framing
        framing = DIMENSION_FRAMING.get(issue.dimension_key)
        if framing:
            lines.append("")
            lines.append(f"    {framing}")

        # Recommendation
        if issue.suggested_display:
            lines.append("")
            lines.append(f"    Try:          "
                         f"#SBATCH --{issue.directive}={issue.suggested_display}")
            if issue.directive_label:
                lines.append(f"                  change {issue.directive_label}")
            if issue.suggestion_rationale:
                lines.append(f"                  ({issue.suggestion_rationale})")

    if not detailed:
        lines.append("")
        lines.append(
            "  Run with --detailed for per-dimension trajectory and details."
        )
        lines.append(
            "  Run `nomad edu explain <job_id>` for a single-job analysis."
        )

    return "\n".join(lines)

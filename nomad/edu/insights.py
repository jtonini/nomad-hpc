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

from nomad.edu.progress import _load_user_jobs, _load_user_sessions, _score_jobs, _score_sessions
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
    "io":              "I/O Awareness",
    "gpu":             "GPU Utilization",
    "memory_pressure": "Workstation Memory Pressure",
    "duration_fit":    "Workstation Session Duration",
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
    current_value_typical: float = 0.0  # modal/median of current requests
    current_display: str = ""         # "200 GB"
    usage_stats: UsageStats | None = None
    strategy: str = ""                # "mode" / "p95_with_buffer"
    rationale: str = ""               # explanation of how the value was chosen
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
    job_count: int
    window_days: int
    session_count: int = 0
    session_window_days: int = 7
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
    rationale = (f"most common across affected jobs "
                 f"({modal_count} of {len(suggestions)})")
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

    issue = Issue(
        dimension=dim_name,
        dimension_key=dim_key,
        affected_jobs=len(affected_scores),
        total_applicable=total_applicable,
        avg_score=avg_score,
        severity=severity,
        trajectory=trajectory,
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
    issue.rationale = rationale

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


def _verdict_promote_message(
    peak_gb: float, host_gb: float, span_hours: float,
    target: dict[str, Any], session_count: int,
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

    detail = (
        f"Across {session_count} recent sessions, your workload peaked at "
        f"{peak_gb:.0f} GB on a {host_gb:.0f} GB host ({(peak_gb/host_gb)*100:.0f}% saturation) "
        f"with spans averaging {span_hours:.1f}h. "
        f"{target['cluster']}'s {target['memory_gb']:.0f} GB tier offers "
        f"{headroom:.1f}x headroom and would let your work complete without "
        f"competing for your workstation's RAM."
    )
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

    if len(qualifying) < VERDICT_MIN_SESSIONS:
        return None

    # Take the peak case as the headline (max peak_gb across qualifying)
    headline = max(qualifying, key=lambda q: q[1])
    _, peak_gb, host_gb, span_hours = headline
    session_count = len(qualifying)

    kind, target = _select_verdict_kind(peak_gb, host_gb, cluster_capacities)

    if kind == "promote":
        title, detail, context = _verdict_promote_message(
            peak_gb, host_gb, span_hours, target, session_count
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

    insights = UserInsights(
        username=username,
        job_count=len(fingerprints),
        window_days=days,
        session_count=len(session_fingerprints),
        session_window_days=session_days,
    )

    # Build the cross-cutting cluster-promotion verdict from sessions.
    if cluster_capacities is None:
        cluster_capacities = _load_cluster_capacities(db_path)
    verdict = _build_cluster_promotion_verdict(
        session_fingerprints, cluster_capacities
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

    if insights.job_count == 0:
        return (f"No recent jobs found for {insights.username} "
                f"in the last {insights.window_days} days.\n"
                f"If you've run jobs recently, ensure the cluster's "
                f"job_metrics collector is current (>= v1.5.6).")

    lines.append(f"  Your NØMAD Profile — {insights.username}")
    lines.append(f"  {'─' * 56}")
    lines.append(f"  {insights.job_count} jobs in the last {insights.window_days} days")
    lines.append(f"  Overall score: {insights.overall_score:.1f} / 100  "
                 f"({insights.overall_trajectory})")
    lines.append("")

    if not insights.issues:
        lines.append("  No systemic issues detected.")
        lines.append("  Either you're doing great, or your jobs vary too much")
        lines.append("  to flag any single dimension.")
        return "\n".join(lines)

    lines.append("  Top issues across your recent jobs:")
    lines.append(f"  {'─' * 56}")

    for issue in insights.issues:
        traj_arrow = {
            "improving": "↑ improving",
            "stable":    "→ stable",
            "worsening": "↓ worsening",
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
            if issue.rationale:
                lines.append(f"                  {issue.rationale}")

    if not detailed:
        lines.append("")
        lines.append(
            "  Run with --detailed for per-dimension trajectory and details."
        )
        lines.append(
            "  Run `nomad edu explain <job_id>` for a single-job analysis."
        )

    return "\n".join(lines)

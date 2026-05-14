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
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

from nomad.edu.progress import _load_user_jobs, _score_jobs
from nomad.edu.scoring import (
    JobFingerprint,
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
    "cpu":    40.0,
    "memory": 40.0,
    "time":   40.0,
    "io":     40.0,
    "gpu":    40.0,
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
    "cpu":    "CPU Efficiency",
    "memory": "Memory Efficiency",
    "time":   "Time Estimation",
    "io":     "I/O Awareness",
    "gpu":    "GPU Utilization",
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

    @property
    def affected_ratio(self) -> float:
        return (self.affected_jobs / self.total_applicable
                if self.total_applicable else 0.0)


@dataclass
class UserInsights:
    """Aggregate insight summary for a user across recent jobs."""
    username: str
    job_count: int
    window_days: int
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
    fingerprints: list[JobFingerprint],
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

def user_insights(
    db_path: str,
    username: str,
    days: int = 90,
    config: dict[str, Any] | None = None,
) -> UserInsights:
    """
    Compute systemic insights for a user across recent jobs.
    Returns UserInsights with issues sorted by severity (critical first).
    """
    thresholds = _load_thresholds(config)
    rows = _load_user_jobs(db_path, username, days=days)
    fingerprints = _score_jobs(rows)

    insights = UserInsights(
        username=username,
        job_count=len(fingerprints),
        window_days=days,
    )

    if not fingerprints:
        return insights

    insights.overall_score = sum(fp.overall for fp in fingerprints) / len(fingerprints)
    insights.overall_trajectory = _classify_overall_trajectory(fingerprints)

    issues: list[Issue] = []
    for dim_key in KEY_TO_DISPLAY:
        issue = _aggregate_dimension(
            dim_key, fingerprints,
            threshold=thresholds[dim_key],
        )
        if issue is not None:
            issues.append(issue)

    severity_rank = {"critical": 0, "high": 1, "medium": 2}
    issues.sort(key=lambda i: (severity_rank[i.severity], i.avg_score))
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

        lines.append("")
        lines.append(f"  {sev_marker} {issue.dimension} — {traj_arrow}")
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

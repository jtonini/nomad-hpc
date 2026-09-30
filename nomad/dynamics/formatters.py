# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Output formatters for NØMAÐ System Dynamics.

Provides CLI text and JSON formatting for all dynamics subcommands.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .diversity import DiversityResult
from .niche import NicheResult
from .capacity import CapacityResult
from .resilience import ResilienceResult
from .externality import ExternalityResult


# ── Shared utilities ──────────────────────────────────────────────────

_SEVERITY_ICONS = {
    "low": "  ",
    "moderate": "! ",
    "high": "!!",
    "critical": "!!",
}

_PRESSURE_ICONS = {
    "low": "  ",
    "moderate": "! ",
    "high": "!!",
    "critical": "!!",
}


def _bar(value: float, width: int = 30) -> str:
    """Render a simple text progress bar."""
    filled = int(value * width)
    return f"[{'=' * filled}{' ' * (width - filled)}] {value:.0%}"


def _section_header(title: str) -> str:
    return f"\n{'=' * 60}\n  {title}\n{'=' * 60}"


# ── Diversity formatter ───────────────────────────────────────────────

_BY = {"user": "person", "group": "group", "partition": "partition"}


def _not_computed(lines: list[str], reason: str) -> str:
    lines.append(f"\n  Not computed: {reason}")
    return "\n".join(lines)


def format_diversity_cli(result: DiversityResult) -> str:
    """Format diversity analysis for CLI output."""
    lines = [_section_header(
        f"Workload Diversity (by {_BY.get(result.by_dimension, result.by_dimension)})")]
    if not getattr(result, "available", True):
        return _not_computed(lines, result.reason)

    c = result.current
    lines.append(f"\n  Shannon entropy (H'):  {c.shannon_h:.3f}")
    lines.append(f"  Simpson's index (D):   {c.simpson_d:.3f}")
    lines.append(f"  Evenness (J):          {c.evenness_j:.3f}")
    lines.append(f"  Richness (S):          {c.richness}")
    lines.append(f"  Dominant:              {c.dominant_category} ({c.dominant_proportion:.0%})")

    # Category breakdown
    lines.append("\n  Distribution:")
    total = sum(c.category_counts.values())
    for cat, count in sorted(c.category_counts.items(), key=lambda x: -x[1]):
        prop = count / total if total else 0
        bar = _bar(prop, 20)
        lines.append(f"    {cat:<20s} {count:>5d} jobs  {bar}")

    # Trend
    if result.trend:
        arrow = {"increasing": "^", "decreasing": "v", "stable": "-"}.get(result.trend_direction, "?")
        lines.append(f"\n  Trend: {result.trend_direction.replace('_', ' ')} ({arrow}) "
                     f"(slope: {result.trend_slope:+.4f}/window)")

        lines.append("\n  H' over time:")
        for snap in result.trend[-6:]:  # show last 6 windows
            date = snap.window_end.strftime("%Y-%m-%d")
            lines.append(f"    {date}  H'={snap.shannon_h:.3f}  "
                         f"D={snap.simpson_d:.3f}  S={snap.richness}")

    # Fragility warning
    if result.fragility_warning:
        lines.append(f"\n  !! WARNING: {result.fragility_detail}")

    return "\n".join(lines)


def format_diversity_json(result: DiversityResult) -> dict:
    """Convert diversity result to JSON-serializable dict."""
    return {
        "dimension": result.by_dimension,
        "available": getattr(result, "available", True),
        "reason": getattr(result, "reason", ""),
        "attribution": getattr(result, "attribution", None),
        "current": {
            "shannon_h": result.current.shannon_h,
            "simpson_d": result.current.simpson_d,
            "evenness_j": result.current.evenness_j,
            "richness": result.current.richness,
            "dominant_category": result.current.dominant_category,
            "dominant_proportion": result.current.dominant_proportion,
            "category_counts": result.current.category_counts,
        },
        "trend_direction": result.trend_direction,
        "trend_slope": result.trend_slope,
        "trend_windows": getattr(result, "trend_windows", 0),
        "fragility_warning": result.fragility_warning,
        "fragility_detail": result.fragility_detail,
        "trend": [
            {
                "window_end": s.window_end.isoformat(),
                "shannon_h": s.shannon_h,
                "simpson_d": s.simpson_d,
                "richness": s.richness,
            }
            for s in result.trend
        ],
    }


# ── Niche overlap formatter ──────────────────────────────────────────

def format_niche_cli(result: NicheResult) -> str:
    """Format niche overlap analysis for CLI output."""
    lines = [_section_header("Niche Overlap Analysis")]

    if not getattr(result, "available", True):
        return _not_computed(lines, result.reason)
    if not result.profiles:
        lines.append("\n  Insufficient data for niche analysis.")
        return "\n".join(lines)

    # Group profiles
    lines.append(f"\n  Resource profiles ({len(result.profiles)} groups):")
    lines.append(f"    {'Group':<20s} {'Jobs':>6s} {'CPU':>6s} {'Mem':>8s} {'GPU':>5s} {'Runtime':>8s}")
    lines.append(f"    {'-' * 53}")
    for p in result.profiles:
        lines.append(
            f"    {p.name:<20s} {p.job_count:>6d} "
            f"{p.raw_values.get('avg_cpus', 0):>6.1f} "
            f"{p.raw_values.get('avg_mem_mb', 0) / 1024:>7.1f}G "
            f"{p.raw_values.get('avg_gpus', 0):>5.1f} "
            f"{p.raw_values.get('avg_runtime_sec', 0) / 3600:>7.1f}h"
        )

    # Overlap matrix (top triangle)
    names = [p.name for p in result.profiles]
    if len(names) <= 10:
        lines.append("\n  Overlap matrix (Pianka's O):")
        # Header row
        max_name_len = min(max(len(n) for n in names), 12)
        header = f"    {'':>{max_name_len}s}"
        for n in names:
            header += f" {n[:6]:>6s}"
        lines.append(header)

        for i, ni in enumerate(names):
            row = f"    {ni[:max_name_len]:>{max_name_len}s}"
            for j, nj in enumerate(names):
                if i == j:
                    row += "     -"
                elif (ni, nj) in result.overlap_matrix:
                    o = result.overlap_matrix[(ni, nj)]
                    marker = "*" if o >= 0.6 else " "
                    row += f" {o:>5.2f}{marker}"
                else:
                    row += "     -"
            lines.append(row)

    # High-overlap pairs
    if result.high_overlap_pairs:
        lines.append("\n  High-overlap pairs (contention risk):")
        for pair in result.high_overlap_pairs:
            risk_icon = _SEVERITY_ICONS.get(pair.contention_risk, "  ")
            dims = ", ".join(pair.shared_dimensions) if pair.shared_dimensions else "general"
            lines.append(
                f"  {risk_icon} {pair.group_a} <-> {pair.group_b}: "
                f"O={pair.overlap:.2f} [{pair.contention_risk}] "
                f"(shared: {dims})"
            )
    else:
        lines.append("\n  No high-overlap pairs detected (threshold: 0.6).")

    return "\n".join(lines)


def format_niche_json(result: NicheResult) -> dict:
    """Convert niche result to JSON-serializable dict."""
    return {
        "available": getattr(result, "available", True),
        "reason": getattr(result, "reason", ""),
        "attribution": getattr(result, "attribution", None),
        "profiles": [
            {
                "name": p.name,
                "job_count": p.job_count,
                "raw_values": p.raw_values,
                "proportions": p.proportions,
            }
            for p in result.profiles
        ],
        "overlap_matrix": {
            f"{a}|{b}": v for (a, b), v in result.overlap_matrix.items()
        },
        "high_overlap_pairs": [
            {
                "group_a": p.group_a,
                "group_b": p.group_b,
                "overlap": p.overlap,
                "contention_risk": p.contention_risk,
                "shared_dimensions": p.shared_dimensions,
            }
            for p in result.high_overlap_pairs
        ],
        "contention_risk_count": result.contention_risk_count,
    }


# ── Capacity formatter ───────────────────────────────────────────────

def format_capacity_cli(result: CapacityResult) -> str:
    """Format carrying capacity analysis for CLI output."""
    lines = [_section_header("Carrying Capacity")]

    if not result.dimensions:
        lines.append(f"\n  {result.summary}")
        return "\n".join(lines)

    lines.append(f"\n  Overall pressure: {result.overall_pressure.upper()}")
    lines.append(f"  {result.summary}")

    lines.append("\n  Resource dimensions:")
    for d in sorted(result.dimensions, key=lambda x: -x.current_utilization):
        binding = " <-- BINDING" if d.is_binding else ""
        bar = _bar(d.current_utilization, 25)
        sat = (
            f"  (~{d.hours_to_saturation:.0f}h to saturation)"
            if d.hours_to_saturation
            else ""
        )
        lines.append(f"    {d.label:<18s} {bar}{sat}{binding}")

        # Trend info
        if d.trend_slope != 0:
            direction = "rising" if d.trend_slope > 0 else "falling"
            lines.append(
                f"    {'':>18s} Trend: {direction} "
                f"({d.trend_slope:+.4f}/hr)"
            )

    return "\n".join(lines)


def format_capacity_json(result: CapacityResult) -> dict:
    """Convert capacity result to JSON-serializable dict."""
    busiest = getattr(result, "busiest", None)
    return {
        "overall_pressure": result.overall_pressure,
        "summary": result.summary,
        "binding_constraint": result.binding_constraint.dimension if result.binding_constraint else None,
        "busiest": busiest.dimension if busiest else None,
        "dimensions": [
            {
                "dimension": d.dimension,
                "label": d.label,
                "current_utilization": d.current_utilization,
                "capacity": d.capacity,
                "used": d.used,
                "unit": d.unit,
                "trend_slope": d.trend_slope,
                "hours_to_saturation": d.hours_to_saturation,
                "is_binding": d.is_binding,
            }
            for d in result.dimensions
        ],
    }


# ── Resilience formatter ─────────────────────────────────────────────

def format_resilience_cli(result: ResilienceResult) -> str:
    """Format resilience analysis for CLI output."""
    lines = [_section_header("System Resilience")]

    score = result.resilience_score
    lines.append(f"\n  Resilience score: {score:.0f}/100" if score is not None
                 else "\n  Resilience score: -- (nothing to read)")
    lines.append(f"  Trend: {result.resilience_trend.replace('_', ' ')}")
    lines.append(f"  {result.summary}")

    if result.mean_recovery_hours is not None:
        lines.append("\n  Recovery statistics:")
        lines.append(f"    Mean recovery time:   {result.mean_recovery_hours:.1f} hours")
        lines.append(f"    Median recovery time: {result.median_recovery_hours:.1f} hours")

    if result.disturbances:
        lines.append(f"\n  Disturbance events ({len(result.disturbances)} total):")
        for d in result.disturbances[-10:]:  # show last 10
            rec = f"{d.recovery_hours:.1f}h" if d.recovery_hours else "ongoing"
            lines.append(
                f"    [{d.severity:>8s}] {d.onset.strftime('%Y-%m-%d %H:%M')} "
                f"  {d.event_type:<22s}  recovery: {rec}"
            )
            if d.detail:
                lines.append(f"             {d.detail}")

    return "\n".join(lines)


def format_resilience_json(result: ResilienceResult) -> dict:
    """Convert resilience result to JSON-serializable dict."""
    return {
        "resilience_score": result.resilience_score,
        "resilience_trend": result.resilience_trend,
        "counted_events": getattr(result, "counted_events", 0),
        "drains": getattr(result, "drains", 0),
        "mean_recovery_hours": result.mean_recovery_hours,
        "median_recovery_hours": result.median_recovery_hours,
        "summary": result.summary,
        "disturbances": [
            {
                "event_type": d.event_type,
                "onset": d.onset.isoformat(),
                "recovered": d.recovered.isoformat() if d.recovered else None,
                "recovery_hours": d.recovery_hours,
                "severity": d.severity,
                "detail": d.detail,
            }
            for d in result.disturbances
        ],
    }


# ── Externality formatter ────────────────────────────────────────────

def format_externality_cli(result: ExternalityResult) -> str:
    """Format externality analysis for CLI output."""
    lines = [_section_header("Inter-Group Externalities")]

    if not getattr(result, "available", True):
        return _not_computed(lines, result.reason)
    lines.append(f"\n  {result.summary}")

    if result.group_profiles:
        lines.append("\n  Group profiles (correlations, not proof of cause):")
        lines.append(f"    {'Group':<20s} {'Resource':>8s} {'Failure':>9s} {'Net':>8s}  Side")
        lines.append(f"    {'-' * 58}")
        for p in result.group_profiles:
            role = "resource" if p.net_score > 0 else "failure" if p.net_score < 0 else "neutral"
            lines.append(
                f"    {p.group_name:<20s} "
                f"{p.imposed_score:>8.2f} "
                f"{p.received_score:>9.2f} "
                f"{p.net_score:>+8.2f}  "
                f"{role}"
            )

    if result.edges:
        lines.append(f"\n  Correlations ({len(result.edges)}):")
        for e in result.edges[:10]:  # top 10
            lines.append(
                f"    {e.source_group} --> {e.target_group}: "
                f"r={e.impact_score:.2f} ({e.mechanism})"
            )
            if e.detail:
                lines.append(f"      {e.detail}")

    return "\n".join(lines)


def format_externality_json(result: ExternalityResult) -> dict:
    """Convert externality result to JSON-serializable dict."""
    return {
        "available": getattr(result, "available", True),
        "reason": getattr(result, "reason", ""),
        "attribution": getattr(result, "attribution", None),
        "summary": result.summary,
        "top_imposers": result.top_imposers,
        "top_receivers": result.top_receivers,
        "group_profiles": [
            {
                "group_name": p.group_name,
                "imposed_score": p.imposed_score,
                "received_score": p.received_score,
                "net_score": p.net_score,
            }
            for p in result.group_profiles
        ],
        "edges": [
            {
                "source_group": e.source_group,
                "target_group": e.target_group,
                "impact_score": e.impact_score,
                "mechanism": e.mechanism,
                "detail": e.detail,
            }
            for e in result.edges
        ],
    }


# ── Full summary formatter ───────────────────────────────────────────

def format_full_summary_cli(
    diversity: DiversityResult,
    niche: NicheResult,
    capacity: CapacityResult,
    resilience: ResilienceResult,
    externality: ExternalityResult,
    cluster_name: str = "cluster",
) -> str:
    """Format the full dynamics summary combining all metrics."""
    lines = []
    lines.append(f"\n{'#' * 60}")
    lines.append(f"  NØMAÐ System Dynamics Report — {cluster_name}")
    lines.append(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"{'#' * 60}")

    # Executive summary
    lines.append("\n  Executive Summary")
    lines.append(f"  {'-' * 40}")

    # Diversity headline
    if getattr(diversity, "available", True):
        h = diversity.current.shannon_h
        trend = diversity.trend_direction
        by = _BY.get(diversity.by_dimension, diversity.by_dimension)
        lines.append(f"  Diversity by {by}: H'={h:.3f} ({trend})")
        if diversity.fragility_warning:
            lines.append(f"    !! {diversity.fragility_detail}")
    else:
        lines.append(f"  Diversity by group: not computed")

    # Capacity headline
    if capacity.binding_constraint:
        bc = capacity.binding_constraint
        lines.append(
            f"  Capacity: {bc.label} is binding at "
            f"{bc.current_utilization:.0%} [{capacity.overall_pressure}]"
        )
    elif capacity.dimensions:
        lines.append(f"  Capacity: {capacity.summary}")

    # Resilience headline
    if resilience.resilience_score is not None:
        lines.append(
            f"  Resilience: {resilience.resilience_score:.0f}/100 "
            f"({resilience.resilience_trend})"
        )
    else:
        lines.append("  Resilience: nothing to read")

    # Externality headline
    if not getattr(externality, "available", True):
        lines.append("  Externalities: not computed")
    elif externality.top_imposers:
        lines.append(
            f"  Externalities: {len(externality.edges)} inter-group correlation(s); "
            f"strongest: {', '.join(externality.top_imposers)}"
        )
    else:
        lines.append("  Externalities: no significant inter-group correlations")

    # Niche headline
    n_high = len(niche.high_overlap_pairs)
    if not getattr(niche, "available", True):
        lines.append("  Niche overlap: not computed")
    elif n_high > 0:
        lines.append(
            f"  Niche overlap: {n_high} high-overlap pair(s) "
            f"— contention risk detected"
        )
    else:
        lines.append("  Niche overlap: no high-overlap pairs")

    # Detailed sections
    lines.append(format_diversity_cli(diversity))
    lines.append(format_niche_cli(niche))
    lines.append(format_capacity_cli(capacity))
    lines.append(format_resilience_cli(resilience))
    lines.append(format_externality_cli(externality))

    return "\n".join(lines)


def format_full_summary_json(
    diversity: DiversityResult,
    niche: NicheResult,
    capacity: CapacityResult,
    resilience: ResilienceResult,
    externality: ExternalityResult,
    cluster_name: str = "cluster",
) -> str:
    """Full dynamics report as JSON string."""
    data = {
        "cluster": cluster_name,
        "generated_at": datetime.now().isoformat(),
        "diversity": format_diversity_json(diversity),
        "niche": format_niche_json(niche),
        "capacity": format_capacity_json(capacity),
        "resilience": format_resilience_json(resilience),
        "externality": format_externality_json(externality),
    }
    return json.dumps(data, indent=2, default=str)

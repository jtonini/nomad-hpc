# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Inter-user externality scoring for research computing.

Quantifies the impact of each user/group's resource-intensive behavior
on other users' job outcomes. Answers: "whose jobs are hurting other
people's jobs?"

Based on Pigou's externality framework: an externality is a cost
imposed on third parties not involved in the transaction. In HPC,
a user consuming I/O bandwidth affects the failure rate of other
users' I/O-intensive jobs.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from nomad.db import scope


@dataclass
class ExternalityEdge:
    """A measured inter-group impact relationship."""
    source_group: str  # the group imposing the cost
    target_group: str  # the group bearing the cost
    impact_score: float  # 0-1, strength of relationship
    mechanism: str  # "resource_contention", "io_saturation", etc.
    detail: str = ""
    source_metric: str = ""
    target_metric: str = ""


@dataclass
class GroupExternality:
    """Externality profile for a single group."""
    group_name: str
    imposed_score: float  # total externality this group imposes on others
    received_score: float  # total externality this group receives from others
    net_score: float  # imposed - received (positive = net imposer)
    outgoing_edges: list[ExternalityEdge] = field(default_factory=list)
    incoming_edges: list[ExternalityEdge] = field(default_factory=list)


@dataclass
class ExternalityResult:
    """Complete externality analysis."""
    group_profiles: list[GroupExternality]
    edges: list[ExternalityEdge]
    top_imposers: list[str] = field(default_factory=list)
    top_receivers: list[str] = field(default_factory=list)
    summary: str = ""
    available: bool = True       # False when jobs can't be placed in groups
    reason: str = ""
    attribution: dict | None = None


# Pearson's r over a handful of hours is noise: with 4 points, r >= 0.3
# happens by chance most of the time. Pairs need this many shared hours.
MIN_SHARED_WINDOWS = 12


def _temporal_correlation(
    source_activity: list[tuple[str, float]],
    target_failures: list[tuple[str, float]],
    min_windows: int = 4,
) -> float:
    """Compute correlation between source activity and target failures.

    Both inputs are lists of (time_window, value) pairs.
    Returns Pearson correlation coefficient.
    """
    # Build aligned series
    source_map = dict(source_activity)
    target_map = dict(target_failures)
    common_windows = sorted(set(source_map.keys()) & set(target_map.keys()))

    if len(common_windows) < min_windows:
        return 0.0

    sx = [source_map[w] for w in common_windows]
    sy = [target_map[w] for w in common_windows]

    n = len(sx)
    mx = sum(sx) / n
    my = sum(sy) / n

    cov = sum((x - mx) * (y - my) for x, y in zip(sx, sy))
    var_x = sum((x - mx) ** 2 for x in sx)
    var_y = sum((y - my) ** 2 for y in sy)

    if var_x == 0 or var_y == 0:
        return 0.0

    return cov / (var_x ** 0.5 * var_y ** 0.5)


def compute_externalities(
    db_path: Path | str,
    hours: int = 168,
    min_jobs: int = 10,
    correlation_threshold: float = 0.3,
    site: str | None = None,
    attribution: str = "auto",
) -> ExternalityResult:
    """Compute inter-group externality scores.

    For each pair of groups, measures whether high resource usage by
    one group correlates temporally with increased failure rates in
    another group.

    Parameters
    ----------
    db_path : path to NØMAÐ database
    hours : how far back to analyze
    min_jobs : minimum jobs per group to include
    correlation_threshold : minimum |correlation| to report an edge
    site : on a combined database, the site to read
    attribution : "auto" places each job in one group or declines (see
        nomad.dynamics.attribution); "membership" forces the old join
    """
    from nomad.dynamics.attribution import job_attribution

    db_path = Path(db_path)
    conn = scope.connect(db_path, site)

    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

    att = job_attribution(conn, cutoff, mode=attribution)
    if not att.available:
        conn.close()
        return ExternalityResult(group_profiles=[], edges=[], summary=att.reason,
                                 available=False, reason=att.reason,
                                 attribution=att.as_dict())

    # ── Get group-level time series ───────────────────────────────────
    # Resource intensity per group per time window
    rows = conn.execute(f"""
        SELECT
            {att.group_expr} AS grp,
            strftime('%Y-%m-%d %H', j.submit_time) AS window,
            COUNT(*) AS total_jobs,
            SUM(CASE WHEN UPPER(j.state) IN ('FAILED', 'NODE_FAIL', 'BOOT_FAIL',
                     'TIMEOUT', 'OUT_OF_MEMORY') THEN 1 ELSE 0 END) AS failed_jobs,
            AVG(j.req_cpus) AS avg_cpus,
            AVG(j.req_mem_mb) AS avg_mem,
            SUM(j.req_cpus * j.runtime_seconds) / 3600.0 AS cpu_hours
        FROM jobs j
        {att.join}
        WHERE j.submit_time >= ? AND j.end_time IS NOT NULL
          AND UPPER(j.state) NOT LIKE 'CANCELLED%'
          AND UPPER(j.state) NOT IN ('RUNNING', 'PENDING', 'PREEMPTED')
        GROUP BY grp, window
        HAVING total_jobs >= 2
        ORDER BY grp, window
    """, (cutoff,)).fetchall()

    conn.close()

    if not rows:
        return ExternalityResult(
            group_profiles=[], edges=[],
            summary="Insufficient data for externality analysis.",
            reason=att.reason, attribution=att.as_dict(),
        )

    # Organize by group
    group_data: dict[str, dict[str, dict[str, float]]] = {}
    for r in rows:
        grp = r["grp"]
        window = r["window"]
        if grp not in group_data:
            group_data[grp] = {}
        group_data[grp][window] = {
            "total_jobs": r["total_jobs"],
            "failed_jobs": r["failed_jobs"],
            "failure_rate": r["failed_jobs"] / r["total_jobs"] if r["total_jobs"] > 0 else 0,
            "cpu_hours": r["cpu_hours"] or 0,
            "avg_cpus": r["avg_cpus"] or 0,
            "avg_mem": r["avg_mem"] or 0,
        }

    # Filter groups with enough data
    groups = [
        g for g, windows in group_data.items()
        if sum(w.get("total_jobs", 0) for w in windows.values()) >= min_jobs
    ]

    if len(groups) < 2:
        return ExternalityResult(
            group_profiles=[], edges=[],
            summary="Fewer than two active groups — externality analysis requires "
                    "at least two groups with sufficient job history.",
            reason=att.reason, attribution=att.as_dict(),
        )

    # ── Compute pairwise correlations ─────────────────────────────────
    edges: list[ExternalityEdge] = []

    for src in groups:
        for tgt in groups:
            if src == tgt:
                continue

            # Source: CPU-hours consumed per window
            src_activity = [
                (w, d["cpu_hours"])
                for w, d in group_data[src].items()
            ]

            # Target: failure rate per window
            tgt_failures = [
                (w, d["failure_rate"])
                for w, d in group_data[tgt].items()
            ]

            corr = _temporal_correlation(src_activity, tgt_failures,
                                         min_windows=MIN_SHARED_WINDOWS)

            if abs(corr) >= correlation_threshold and corr > 0:
                # Positive correlation: source activity → target failures
                edges.append(ExternalityEdge(
                    source_group=src,
                    target_group=tgt,
                    impact_score=corr,
                    mechanism="resource_contention",
                    detail=(
                        f"When '{src}' increases resource usage, "
                        f"'{tgt}' failure rate increases (r={corr:.2f})."
                    ),
                    source_metric="cpu_hours",
                    target_metric="failure_rate",
                ))

    # Sort by impact
    edges.sort(key=lambda e: e.impact_score, reverse=True)

    # ── Build group profiles ──────────────────────────────────────────
    group_profiles: list[GroupExternality] = []

    for g in groups:
        outgoing = [e for e in edges if e.source_group == g]
        incoming = [e for e in edges if e.target_group == g]

        imposed = sum(e.impact_score for e in outgoing)
        received = sum(e.impact_score for e in incoming)

        group_profiles.append(GroupExternality(
            group_name=g,
            imposed_score=imposed,
            received_score=received,
            net_score=imposed - received,
            outgoing_edges=outgoing,
            incoming_edges=incoming,
        ))

    group_profiles.sort(key=lambda p: p.net_score, reverse=True)

    # Top imposers and receivers
    top_imposers = [p.group_name for p in group_profiles if p.net_score > 0][:3]
    top_receivers = [
        p.group_name for p in sorted(group_profiles, key=lambda p: p.net_score)
        if p.net_score < 0
    ][:3]

    # Summary
    parts = [f"{len(edges)} pair(s) of groups where one group's resource use "
             f"rises and falls with the other's failure rate (a correlation, "
             f"not proof of cause)."]
    if top_imposers:
        parts.append(
            f"Strongest on the resource side: {', '.join(top_imposers)}."
        )
    if top_receivers:
        parts.append(
            f"Strongest on the failure side: {', '.join(top_receivers)}."
        )
    if not edges:
        parts = ["No significant inter-group externalities detected at current threshold."]

    return ExternalityResult(
        group_profiles=group_profiles,
        edges=edges,
        top_imposers=top_imposers,
        top_receivers=top_receivers,
        summary=" ".join(parts),
        reason=att.reason,
        attribution=att.as_dict(),
    )

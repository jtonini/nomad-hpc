# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Workload diversity indices for research computing environments.

Implements Shannon entropy (H'), Simpson's diversity index (D), and
Pielou's evenness (J) over job accounting data. Supports computation
by group, partition, or job type, with temporal trend analysis.

Mathematical reference:
    Shannon (1948): H' = -Σ p_i ln(p_i)
    Simpson (1949): D  = 1 - Σ p_i²
    Pielou (1966):  J  = H' / ln(S)
"""
from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from nomad.db import scope


@dataclass
class DiversitySnapshot:
    """Diversity metrics for a single time window."""
    window_start: datetime
    window_end: datetime
    shannon_h: float
    simpson_d: float
    evenness_j: float
    richness: int  # number of distinct categories
    category_counts: dict[str, int] = field(default_factory=dict)
    dominant_category: str = ""
    dominant_proportion: float = 0.0


@dataclass
class DiversityResult:
    """Complete diversity analysis over a time range."""
    by_dimension: str  # "group", "partition", "job_type"
    current: DiversitySnapshot
    trend: list[DiversitySnapshot] = field(default_factory=list)
    trend_direction: str = "stable"  # "increasing", "decreasing", "stable"
    trend_slope: float = 0.0
    fragility_warning: bool = False
    fragility_detail: str = ""
    trend_windows: int = 0       # windows with enough jobs to count in the trend
    available: bool = True       # False when jobs can't be placed in groups
    reason: str = ""             # why not, or how jobs were placed
    attribution: dict | None = None


def _compute_diversity(counts: dict[str, int]) -> tuple[float, float, float]:
    """Compute Shannon H', Simpson D, and evenness J from category counts.

    Returns (shannon_h, simpson_d, evenness_j).
    """
    total = sum(counts.values())
    if total == 0 or len(counts) == 0:
        return 0.0, 0.0, 0.0

    proportions = [c / total for c in counts.values() if c > 0]
    s = len(proportions)

    if s <= 1:
        return 0.0, 0.0, 1.0  # single category = no diversity, perfect evenness

    # Shannon entropy
    h = -sum(p * math.log(p) for p in proportions)

    # Simpson's index
    d = 1.0 - sum(p * p for p in proportions)

    # Pielou's evenness
    j = h / math.log(s) if s > 1 else 1.0

    return h, d, j


def _get_category_column(dimension: str) -> str:
    """Map dimension name to the SQL column to group by."""
    mapping = {
        "group": "COALESCE(gm.group_name, 'ungrouped')",
        "partition": "j.partition",
        "user": "j.user_name",
    }
    return mapping.get(dimension, "j.partition")


def _needs_group_join(dimension: str) -> bool:
    """Whether the query needs a JOIN to group_membership."""
    return dimension == "group"


# A trend window with fewer jobs than this is left out of the trend: the
# index of a handful of jobs moves with every job.
MIN_WINDOW_JOBS = 20

_WHO = {"user": ("person", "people"), "group": ("group", "groups"),
        "partition": ("partition", "partitions")}


def compute_diversity(
    db_path: Path | str,
    dimension: str = "group",
    hours: int = 168,
    window_hours: int = 168,
    n_windows: int = 12,
    site: str | None = None,
    attribution: str = "auto",
) -> DiversityResult:
    """Compute diversity indices over job accounting data.

    Parameters
    ----------
    db_path : path to NØMAÐ database
    dimension : what to measure diversity over ("group", "partition", "user")
    hours : how far back to look for the current snapshot
    window_hours : size of each trend window in hours
    n_windows : number of historical windows for trend analysis
    site : on a combined database, the site to read
    attribution : for dimension="group": "auto" places each job in one group
        or declines (see nomad.dynamics.attribution); "membership" forces
        the join to group_membership, counting a job once per group
    """
    from nomad.dynamics.attribution import job_attribution

    db_path = Path(db_path)
    conn = scope.connect(db_path, site)

    now = datetime.now()
    cutoff = now - timedelta(hours=hours)

    # ── Current snapshot ──────────────────────────────────────────────
    cat_col = _get_category_column(dimension)
    join_group = _needs_group_join(dimension)
    join_clause = ""
    att = None
    if join_group:
        span = max(hours, window_hours * n_windows)
        att = job_attribution(conn, (now - timedelta(hours=span)).isoformat(),
                              mode=attribution)
        if not att.available:
            conn.close()
            empty = DiversitySnapshot(window_start=cutoff, window_end=now,
                                      shannon_h=0.0, simpson_d=0.0,
                                      evenness_j=0.0, richness=0)
            return DiversityResult(by_dimension=dimension, current=empty,
                                   available=False, reason=att.reason,
                                   attribution=att.as_dict())
        cat_col, join_clause = att.group_expr, att.join

    if join_group:
        query = f"""
            SELECT {cat_col} AS category, COUNT(*) AS cnt
            FROM jobs j
            {join_clause}
            WHERE j.submit_time >= ?
            GROUP BY category
            ORDER BY cnt DESC
        """
    else:
        query = f"""
            SELECT {cat_col} AS category, COUNT(*) AS cnt
            FROM jobs j
            WHERE j.submit_time >= ?
            GROUP BY category
            ORDER BY cnt DESC
        """

    rows = conn.execute(query, (cutoff.isoformat(),)).fetchall()
    counts = {r["category"]: r["cnt"] for r in rows if r["category"]}

    h, d, j_val = _compute_diversity(counts)

    dominant = max(counts, key=counts.get) if counts else ""
    total = sum(counts.values())
    dom_prop = counts.get(dominant, 0) / total if total > 0 else 0.0

    current = DiversitySnapshot(
        window_start=cutoff,
        window_end=now,
        shannon_h=h,
        simpson_d=d,
        evenness_j=j_val,
        richness=len(counts),
        category_counts=counts,
        dominant_category=dominant,
        dominant_proportion=dom_prop,
    )

    # ── Temporal trend ────────────────────────────────────────────────
    trend: list[DiversitySnapshot] = []
    for i in range(n_windows, 0, -1):
        w_end = now - timedelta(hours=window_hours * (i - 1))
        w_start = w_end - timedelta(hours=window_hours)

        if join_group:
            tq = f"""
                SELECT {cat_col} AS category, COUNT(*) AS cnt
                FROM jobs j
                {join_clause}
                WHERE j.submit_time >= ? AND j.submit_time < ?
                GROUP BY category
            """
        else:
            tq = f"""
                SELECT {cat_col} AS category, COUNT(*) AS cnt
                FROM jobs j
                WHERE j.submit_time >= ? AND j.submit_time < ?
                GROUP BY category
            """

        rows = conn.execute(tq, (w_start.isoformat(), w_end.isoformat())).fetchall()
        w_counts = {r["category"]: r["cnt"] for r in rows if r["category"]}
        w_h, w_d, w_j = _compute_diversity(w_counts)

        w_dom = max(w_counts, key=w_counts.get) if w_counts else ""
        w_total = sum(w_counts.values())
        w_dom_prop = w_counts.get(w_dom, 0) / w_total if w_total > 0 else 0.0

        trend.append(DiversitySnapshot(
            window_start=w_start,
            window_end=w_end,
            shannon_h=w_h,
            simpson_d=w_d,
            evenness_j=w_j,
            richness=len(w_counts),
            category_counts=w_counts,
            dominant_category=w_dom,
            dominant_proportion=w_dom_prop,
        ))

    conn.close()

    # ── Trend analysis ────────────────────────────────────────────────
    # Fewer than three windows with enough jobs make no trend: say so
    # rather than call it "stable".
    trend_direction = "too_few_weeks"
    trend_slope = 0.0
    h_vals = [t.shannon_h for t in trend
              if t.richness > 0 and sum(t.category_counts.values()) >= MIN_WINDOW_JOBS]
    if len(trend) >= 3:
        if len(h_vals) >= 3:
            trend_direction = "stable"
            # Simple linear regression on H' over time
            n = len(h_vals)
            x_mean = (n - 1) / 2
            y_mean = sum(h_vals) / n
            num = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(h_vals))
            den = sum((i - x_mean) ** 2 for i in range(n))
            if den > 0:
                trend_slope = num / den
                if trend_slope > 0.01:
                    trend_direction = "increasing"
                elif trend_slope < -0.01:
                    trend_direction = "decreasing"

    # ── Fragility check ───────────────────────────────────────────────
    fragility_warning = False
    fragility_detail = ""
    one, many = _WHO.get(dimension, ("category", "categories"))
    total_now = sum(counts.values())
    if current.dominant_proportion > 0.6 and current.richness >= 2:
        fragility_warning = True
        lead = ("One person" if dimension == "user"
                else f"The {one} '{current.dominant_category}'")
        fragility_detail = (
            f"{lead} accounts for {current.dominant_proportion:.0%} of the "
            f"{total_now:,} jobs in this window: the workload rests on one {one}."
        )
    elif trend_direction == "decreasing" and abs(trend_slope) > 0.02:
        fragility_warning = True
        fragility_detail = (
            f"Jobs are concentrating in fewer {many}: diversity (H') is "
            f"falling by {abs(trend_slope):.3f} per window."
        )

    return DiversityResult(
        by_dimension=dimension,
        current=current,
        trend=trend,
        trend_direction=trend_direction,
        trend_slope=trend_slope,
        fragility_warning=fragility_warning,
        fragility_detail=fragility_detail,
        trend_windows=len(h_vals),
        reason=att.reason if att else "",
        attribution=att.as_dict() if att else None,
    )

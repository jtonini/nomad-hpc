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


def compute_diversity(
    db_path: Path | str,
    dimension: str = "group",
    hours: int = 168,
    window_hours: int = 168,
    n_windows: int = 12,
) -> DiversityResult:
    """Compute diversity indices over job accounting data.

    Parameters
    ----------
    db_path : path to NØMAÐ database
    dimension : what to measure diversity over ("group", "partition", "user")
    hours : how far back to look for the current snapshot
    window_hours : size of each trend window in hours
    n_windows : number of historical windows for trend analysis
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    now = datetime.now()
    cutoff = now - timedelta(hours=hours)

    # ── Current snapshot ──────────────────────────────────────────────
    cat_col = _get_category_column(dimension)
    join_group = _needs_group_join(dimension)

    if join_group:
        query = f"""
            SELECT {cat_col} AS category, COUNT(*) AS cnt
            FROM jobs j
            LEFT JOIN group_membership gm ON j.user_name = gm.username
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

    # Filter umbrella groups (>80% of all users)
    if dimension == "group":
        try:
            total_users = conn.execute(
                "SELECT COUNT(DISTINCT username) FROM group_membership"
            ).fetchone()[0]
            if total_users > 0:
                umbrella = set()
                for r2 in conn.execute(
                    "SELECT group_name, COUNT(DISTINCT username) as cnt"
                    " FROM group_membership GROUP BY group_name"
                ).fetchall():
                    if r2["cnt"] / total_users > 0.8:
                        umbrella.add(r2["group_name"])
                if umbrella:
                    rows = [r for r in rows if r["category"] not in umbrella]
        except Exception:
            pass
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
                LEFT JOIN group_membership gm ON j.user_name = gm.username
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
    trend_direction = "stable"
    trend_slope = 0.0
    if len(trend) >= 3:
        h_vals = [t.shannon_h for t in trend if t.richness > 0]
        if len(h_vals) >= 3:
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
    if current.dominant_proportion > 0.6:
        fragility_warning = True
        fragility_detail = (
            f"'{current.dominant_category}' accounts for "
            f"{current.dominant_proportion:.0%} of all jobs. "
            f"Workload is concentrated — loss of this group would "
            f"dramatically reduce cluster utilization."
        )
    elif trend_direction == "decreasing" and abs(trend_slope) > 0.02:
        fragility_warning = True
        fragility_detail = (
            f"Diversity (H') is trending downward at "
            f"{trend_slope:.3f}/window. Investigate whether user "
            f"communities are being lost or consolidated."
        )

    return DiversityResult(
        by_dimension=dimension,
        current=current,
        trend=trend,
        trend_direction=trend_direction,
        trend_slope=trend_slope,
        fragility_warning=fragility_warning,
        fragility_detail=fragility_detail,
    )

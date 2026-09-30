# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Multi-dimensional carrying capacity analysis.

Models the cluster as a multi-resource system where each dimension
(CPU, memory, GPU, I/O, scheduler queue) has a carrying capacity.
Identifies the binding constraint and projects time to saturation.

The binding constraint is the resource dimension closest to full
utilization — the ecological equivalent of Liebig's law of the
minimum, where growth is limited by the scarcest resource.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from nomad.db import scope


@dataclass
class DimensionUtilization:
    """Utilization for a single resource dimension."""
    dimension: str
    label: str
    current_utilization: float  # 0.0 - 1.0
    capacity: float  # absolute capacity
    used: float  # absolute usage
    unit: str
    trend_slope: float = 0.0  # change per hour
    hours_to_saturation: float | None = None  # projected
    is_binding: bool = False
    history: list[tuple[datetime, float]] = field(default_factory=list)


@dataclass
class CapacityResult:
    """Complete carrying capacity analysis."""
    dimensions: list[DimensionUtilization]
    binding_constraint: DimensionUtilization | None = None  # only when >= BINDING_AT
    overall_pressure: str = "low"  # "low", "moderate", "high", "critical"
    summary: str = ""
    busiest: DimensionUtilization | None = None


def _compute_trend_slope(values: list[tuple[datetime, float]]) -> float:
    """Simple linear regression on (time, utilization) pairs.

    Returns slope in utilization-units per hour.
    """
    if len(values) < 3:
        return 0.0

    # Convert to hours since first observation
    t0 = values[0][0]
    xs = [(v[0] - t0).total_seconds() / 3600 for v in values]
    ys = [v[1] for v in values]
    n = len(xs)

    x_mean = sum(xs) / n
    y_mean = sum(ys) / n

    num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
    den = sum((x - x_mean) ** 2 for x in xs)

    if den == 0:
        return 0.0

    return num / den


def _project_saturation(current: float, slope: float) -> float | None:
    """Project hours until utilization reaches 1.0.

    Returns None if slope is non-positive or saturation is > 720h (30 days).
    """
    if slope <= 0 or current >= 1.0:
        return None
    remaining = 1.0 - current
    hours = remaining / slope
    if hours > 720:
        return None
    return hours


# The busiest resource is called the binding constraint -- Liebig's scarcest
# factor -- only when it is actually near its limit. At 28% nothing binds.
BINDING_AT = 0.75


def _hourly(rows) -> list[tuple[datetime, float]]:
    """(hour start, mean of the values in it) from (timestamp, value) rows."""
    buckets: dict[str, list[float]] = {}
    for ts, value in rows:
        if ts is None or value is None:
            continue
        buckets.setdefault(str(ts)[:13], []).append(float(value))
    out = []
    for hour in sorted(buckets):
        try:
            t = datetime.fromisoformat(hour.replace(" ", "T") + ":00")
        except ValueError:
            continue
        vals = buckets[hour]
        out.append((t, sum(vals) / len(vals)))
    return out


def _dimension(dimension, label, history, capacity, unit) -> DimensionUtilization | None:
    if not history:
        return None
    current = history[-1][1]
    slope = _compute_trend_slope(history)
    return DimensionUtilization(
        dimension=dimension, label=label,
        current_utilization=current,
        capacity=capacity, used=current * capacity if capacity else 0.0,
        unit=unit,
        trend_slope=slope,
        hours_to_saturation=_project_saturation(current, slope),
        history=history,
    )


def compute_capacity(
    db_path: Path | str,
    hours: int = 168,
    n_samples: int = 24,
    site: str | None = None,
) -> CapacityResult:
    """Compute multi-dimensional carrying capacity utilization.

    CPU and memory are what Slurm has allocated on the nodes that can take
    jobs (allocated cores / cores in each snapshot); GPU is measured
    utilization; I/O is the busiest device's utilization (an average over
    every disk hides the one that is saturated); the queue is pending jobs
    per running job, with 3 counted as full. Each is averaged per hour and
    "current" is the latest hour.

    Parameters
    ----------
    db_path : path to NØMAÐ database
    hours : how far back to analyze
    n_samples : number of time samples for trend computation
    site : on a combined database, the site to read
    """
    db_path = Path(db_path)
    conn = scope.connect(db_path, site)

    now = datetime.now()
    cutoff = (now - timedelta(hours=hours)).isoformat()
    dimensions: list[DimensionUtilization] = []

    try:
        ns = scope.table_columns(conn, "node_state")
        healthy = "is_healthy = 1" if "is_healthy" in ns else "1"
        if ns:
            latest = conn.execute(
                f"SELECT * FROM node_state WHERE timestamp = "
                f"(SELECT MAX(timestamp) FROM node_state) AND {healthy}").fetchall()
        else:
            latest = []

        # ── CPU and memory: allocated share of what the nodes have ───────
        for dim, label, alloc, total, pct, unit, scale in (
            ("cpu", "CPU cores allocated", "cpus_alloc", "cpus_total",
             "cpu_alloc_percent", "cores", 1.0),
            ("memory", "Memory allocated", "memory_alloc_mb", "memory_total_mb",
             "memory_alloc_percent", "GB", 1 / 1024),
        ):
            if {alloc, total} <= ns:
                rows = conn.execute(f"""
                    SELECT timestamp,
                           CAST(SUM({alloc}) AS REAL) / NULLIF(SUM({total}), 0) AS frac
                    FROM node_state WHERE timestamp >= ? AND {healthy}
                    GROUP BY timestamp
                """, (cutoff,)).fetchall()
                history = _hourly((r["timestamp"], r["frac"]) for r in rows)
                capacity = sum((r[total] or 0) for r in latest) * scale
            elif pct in ns:
                rows = conn.execute(f"""
                    SELECT timestamp, {pct} / 100.0 AS frac
                    FROM node_state WHERE timestamp >= ? AND {healthy}
                """, (cutoff,)).fetchall()
                history = _hourly((r["timestamp"], r["frac"]) for r in rows)
                capacity = 0.0
            else:
                continue
            d = _dimension(dim, label, history, capacity, unit)
            if d:
                dimensions.append(d)

        # ── GPU: measured utilization ────────────────────────────────────
        if scope.table_columns(conn, "gpu_stats") >= {"gpu_util_percent", "timestamp"}:
            rows = conn.execute("""
                SELECT timestamp, gpu_util_percent / 100.0 AS frac
                FROM gpu_stats WHERE timestamp >= ?
            """, (cutoff,)).fetchall()
            history = _hourly((r["timestamp"], r["frac"]) for r in rows)
            reporting = conn.execute(
                "SELECT COUNT(*) FROM gpu_stats WHERE timestamp = "
                "(SELECT MAX(timestamp) FROM gpu_stats)").fetchone()[0]
            d = _dimension("gpu", "GPU busy (measured)", history,
                           float(reporting or 0), "GPUs reporting")
            if d:
                dimensions.append(d)

        # ── Queue pressure (pending per running) ─────────────────────────
        if scope.table_columns(conn, "queue_state"):
            rows = conn.execute("""
                SELECT timestamp,
                       CAST(SUM(pending_jobs) AS REAL) / MAX(SUM(running_jobs), 1) AS pressure,
                       SUM(pending_jobs) AS pending, SUM(running_jobs) AS running
                FROM queue_state WHERE timestamp >= ?
                GROUP BY timestamp ORDER BY timestamp
            """, (cutoff,)).fetchall()
            history = _hourly((r["timestamp"], min((r["pressure"] or 0) / 3.0, 1.0))
                              for r in rows)
            d = _dimension("queue", "Queue (pending per running)", history,
                           float((rows[-1]["pending"] or 0) + (rows[-1]["running"] or 0))
                           if rows else 0.0, "jobs")
            if d:
                d.used = float(rows[-1]["pending"] or 0)
                dimensions.append(d)

        # ── I/O: the busiest device at each reading ──────────────────────
        if scope.table_columns(conn, "iostat_device") >= {"util_percent", "timestamp"}:
            rows = conn.execute("""
                SELECT timestamp, MAX(util_percent) / 100.0 AS frac
                FROM iostat_device WHERE timestamp >= ?
                GROUP BY timestamp
            """, (cutoff,)).fetchall()
            history = _hourly((r["timestamp"], min(r["frac"], 1.0) if r["frac"] is not None else None)
                              for r in rows)
            d = _dimension("io", "Disk I/O (busiest device)", history, 100, "%")
            if d:
                dimensions.append(d)
    finally:
        conn.close()

    if not dimensions:
        return CapacityResult(
            dimensions=[],
            summary="Insufficient data to compute carrying capacity.",
        )

    # The queue is shown but can't bind: pending jobs include held,
    # dependent and throttled ones, so "3 waiting per running" is not a
    # resource at its limit.
    resources = [d for d in dimensions if d.dimension != "queue"]
    if not resources:
        return CapacityResult(
            dimensions=dimensions,
            summary="Only the queue was measured; no resource to compare.",
        )
    busiest = max(resources, key=lambda d: d.current_utilization)
    max_util = busiest.current_utilization
    binding = busiest if max_util >= BINDING_AT else None
    if binding:
        binding.is_binding = True

    if max_util >= 0.9:
        pressure = "critical"
    elif max_util >= 0.75:
        pressure = "high"
    elif max_util >= 0.5:
        pressure = "moderate"
    else:
        pressure = "low"

    if binding:
        summary = (f"Binding constraint: {binding.label} at "
                   f"{binding.current_utilization:.0%}.")
        if binding.hours_to_saturation is not None:
            summary += (f" At the current rise it would be full in about "
                        f"{binding.hours_to_saturation:.0f} hours.")
    else:
        summary = (f"Nothing is near its limit: the busiest is {busiest.label} "
                   f"at {busiest.current_utilization:.0%}.")

    return CapacityResult(
        dimensions=dimensions,
        binding_constraint=binding,
        overall_pressure=pressure,
        summary=summary,
        busiest=busiest,
    )

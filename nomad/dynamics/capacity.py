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
    binding_constraint: DimensionUtilization | None = None
    overall_pressure: str = "low"  # "low", "moderate", "high", "critical"
    summary: str = ""


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


def compute_capacity(
    db_path: Path | str,
    hours: int = 168,
    n_samples: int = 24,
) -> CapacityResult:
    """Compute multi-dimensional carrying capacity utilization.

    Parameters
    ----------
    db_path : path to NØMAÐ database
    hours : how far back to analyze
    n_samples : number of time samples for trend computation
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    now = datetime.now()
    cutoff = now - timedelta(hours=hours)

    dimensions: list[DimensionUtilization] = []

    # ── CPU utilization ───────────────────────────────────────────────
    # From node_state: cpu_alloc_percent averaged across nodes
    cpu_rows = conn.execute("""
        SELECT timestamp, AVG(cpu_alloc_percent) AS avg_cpu
        FROM node_state
        WHERE timestamp >= ? AND is_healthy = 1
        GROUP BY strftime('%Y-%m-%d %H', timestamp)
        ORDER BY timestamp
    """, (cutoff.isoformat(),)).fetchall()

    if cpu_rows:
        cpu_history = [
            (datetime.fromisoformat(r["timestamp"]), r["avg_cpu"] / 100.0)
            for r in cpu_rows if r["avg_cpu"] is not None
        ]
        cpu_current = cpu_history[-1][1] if cpu_history else 0.0
        cpu_slope = _compute_trend_slope(cpu_history)

        # Get total CPU capacity from nodes table
        cap_row = conn.execute(
            "SELECT SUM(cpu_count) AS total_cpu FROM nodes WHERE status != 'down'"
        ).fetchone()
        total_cpu = float(cap_row["total_cpu"]) if cap_row and cap_row["total_cpu"] else 1.0

        dimensions.append(DimensionUtilization(
            dimension="cpu", label="CPU Cores",
            current_utilization=cpu_current,
            capacity=total_cpu, used=cpu_current * total_cpu,
            unit="cores",
            trend_slope=cpu_slope,
            hours_to_saturation=_project_saturation(cpu_current, cpu_slope),
            history=cpu_history,
        ))

    # ── Memory utilization ────────────────────────────────────────────
    mem_rows = conn.execute("""
        SELECT timestamp, AVG(memory_alloc_percent) AS avg_mem
        FROM node_state
        WHERE timestamp >= ? AND is_healthy = 1
        GROUP BY strftime('%Y-%m-%d %H', timestamp)
        ORDER BY timestamp
    """, (cutoff.isoformat(),)).fetchall()

    if mem_rows:
        mem_history = [
            (datetime.fromisoformat(r["timestamp"]), r["avg_mem"] / 100.0)
            for r in mem_rows if r["avg_mem"] is not None
        ]
        mem_current = mem_history[-1][1] if mem_history else 0.0
        mem_slope = _compute_trend_slope(mem_history)

        cap_row = conn.execute(
            "SELECT SUM(memory_mb) AS total_mem FROM nodes WHERE status != 'down'"
        ).fetchone()
        total_mem_gb = float(cap_row["total_mem"]) / 1024 if cap_row and cap_row["total_mem"] else 1.0

        dimensions.append(DimensionUtilization(
            dimension="memory", label="Memory",
            current_utilization=mem_current,
            capacity=total_mem_gb, used=mem_current * total_mem_gb,
            unit="GB",
            trend_slope=mem_slope,
            hours_to_saturation=_project_saturation(mem_current, mem_slope),
            history=mem_history,
        ))

    # ── GPU utilization ───────────────────────────────────────────────
    gpu_rows = conn.execute("""
        SELECT timestamp, AVG(gpu_util_percent) AS avg_gpu_util
        FROM gpu_stats
        WHERE timestamp >= ?
        GROUP BY strftime('%Y-%m-%d %H', timestamp)
        ORDER BY timestamp
    """, (cutoff.isoformat(),)).fetchall()

    if gpu_rows:
        gpu_history = [
            (datetime.fromisoformat(r["timestamp"]), r["avg_gpu_util"] / 100.0)
            for r in gpu_rows if r["avg_gpu_util"] is not None
        ]
        gpu_current = gpu_history[-1][1] if gpu_history else 0.0
        gpu_slope = _compute_trend_slope(gpu_history)

        cap_row = conn.execute(
            "SELECT SUM(gpu_count) AS total_gpu FROM nodes WHERE gpu_count > 0 AND status != 'down'"
        ).fetchone()
        total_gpu = float(cap_row["total_gpu"]) if cap_row and cap_row["total_gpu"] else 1.0

        dimensions.append(DimensionUtilization(
            dimension="gpu", label="GPU",
            current_utilization=gpu_current,
            capacity=total_gpu, used=gpu_current * total_gpu,
            unit="GPUs",
            trend_slope=gpu_slope,
            hours_to_saturation=_project_saturation(gpu_current, gpu_slope),
            history=gpu_history,
        ))

    # ── Queue pressure (pending/running ratio) ────────────────────────
    queue_rows = conn.execute("""
        SELECT timestamp,
               CAST(SUM(pending_jobs) AS REAL) / MAX(SUM(running_jobs), 1) AS pressure
        FROM queue_state
        WHERE timestamp >= ?
        GROUP BY timestamp
        ORDER BY timestamp
    """, (cutoff.isoformat(),)).fetchall()

    if queue_rows:
        queue_history = [
            (datetime.fromisoformat(r["timestamp"]),
             min(r["pressure"] / 3.0, 1.0))  # normalize: 3.0 pending/running = 100%
            for r in queue_rows if r["pressure"] is not None
        ]
        queue_current = queue_history[-1][1] if queue_history else 0.0
        queue_slope = _compute_trend_slope(queue_history)

        # Latest raw values for display
        last_q = conn.execute(
            "SELECT SUM(pending_jobs) AS pending, SUM(running_jobs) AS running FROM queue_state GROUP BY timestamp ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        pending = float(last_q["pending"]) if last_q else 0
        running = float(last_q["running"]) if last_q else 0

        dimensions.append(DimensionUtilization(
            dimension="queue", label="Scheduler Queue",
            current_utilization=queue_current,
            capacity=running + pending, used=pending,
            unit="jobs pending",
            trend_slope=queue_slope,
            hours_to_saturation=_project_saturation(queue_current, queue_slope),
            history=queue_history,
        ))

    # ── I/O (from iostat if available) ────────────────────────────────
    io_rows = conn.execute("""
        SELECT timestamp, AVG(util_percent) AS avg_io
        FROM iostat_device
        WHERE timestamp >= ?
        GROUP BY strftime('%Y-%m-%d %H', timestamp)
        ORDER BY timestamp
    """, (cutoff.isoformat(),)).fetchall()

    if io_rows:
        io_history = [
            (datetime.fromisoformat(r["timestamp"]), r["avg_io"] / 100.0)
            for r in io_rows if r["avg_io"] is not None
        ]
        io_current = io_history[-1][1] if io_history else 0.0
        io_slope = _compute_trend_slope(io_history)

        dimensions.append(DimensionUtilization(
            dimension="io", label="I/O",
            current_utilization=io_current,
            capacity=100, used=io_current * 100,
            unit="%",
            trend_slope=io_slope,
            hours_to_saturation=_project_saturation(io_current, io_slope),
            history=io_history,
        ))

    conn.close()

    if not dimensions:
        return CapacityResult(
            dimensions=[],
            summary="Insufficient data to compute carrying capacity.",
        )

    # ── Identify binding constraint ───────────────────────────────────
    binding = max(dimensions, key=lambda d: d.current_utilization)
    binding.is_binding = True

    # Overall pressure assessment
    max_util = binding.current_utilization
    if max_util >= 0.9:
        pressure = "critical"
    elif max_util >= 0.75:
        pressure = "high"
    elif max_util >= 0.5:
        pressure = "moderate"
    else:
        pressure = "low"

    # Summary narrative
    sat_dims = [d for d in dimensions if d.hours_to_saturation is not None]
    sat_note = ""
    if sat_dims:
        soonest = min(sat_dims, key=lambda d: d.hours_to_saturation)
        sat_note = (
            f" {soonest.label} is projected to reach saturation "
            f"in {soonest.hours_to_saturation:.0f} hours at current growth rate."
        )

    summary = (
        f"Binding constraint: {binding.label} at "
        f"{binding.current_utilization:.0%} utilization.{sat_note}"
    )

    return CapacityResult(
        dimensions=dimensions,
        binding_constraint=binding,
        overall_pressure=pressure,
        summary=summary,
    )

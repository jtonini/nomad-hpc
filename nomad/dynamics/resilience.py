# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Cluster resilience analysis.

Detects disturbance events (node failures, performance drops,
semester transitions) from historical data and computes recovery
time — how long the system takes to return to baseline.

Based on Holling's resilience framework: resilience is the capacity
of a system to absorb disturbance and reorganize while retaining
essentially the same function. Measured as mean time to return to
baseline operational state.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


@dataclass
class Disturbance:
    """A detected disturbance event."""
    event_type: str  # "node_failure", "performance_drop", "job_spike_failure"
    onset: datetime
    recovered: datetime | None = None
    recovery_hours: float | None = None
    severity: str = "minor"  # "minor", "moderate", "major"
    detail: str = ""
    metric_at_onset: float = 0.0
    metric_at_recovery: float = 0.0
    baseline_metric: float = 0.0


@dataclass
class ResilienceResult:
    """Complete resilience analysis."""
    disturbances: list[Disturbance]
    mean_recovery_hours: float | None = None
    median_recovery_hours: float | None = None
    resilience_trend: str = "stable"  # "improving", "degrading", "stable"
    resilience_score: float = 0.0  # 0-100, higher = more resilient
    summary: str = ""


def _detect_node_failures(
    conn: sqlite3.Connection,
    cutoff: str,
) -> list[Disturbance]:
    """Detect periods where nodes went down/drained from node_state."""
    disturbances = []

    # Look for nodes transitioning to unhealthy
    rows = conn.execute("""
        SELECT node_name, timestamp, is_healthy
        FROM node_state
        WHERE timestamp >= ?
        ORDER BY node_name, timestamp
    """, (cutoff,)).fetchall()

    if not rows:
        return disturbances

    # Track state transitions per node
    current_state: dict[str, tuple[bool, datetime]] = {}
    failures: list[tuple[str, datetime]] = []
    recoveries: list[tuple[str, datetime]] = []

    for r in rows:
        host = r["node_name"]
        ts = datetime.fromisoformat(r["timestamp"])
        healthy = bool(r["is_healthy"])

        if host in current_state:
            was_healthy, _ = current_state[host]
            if was_healthy and not healthy:
                failures.append((host, ts))
            elif not was_healthy and healthy:
                recoveries.append((host, ts))

        current_state[host] = (healthy, ts)

    # Match failures to recoveries
    for host, fail_ts in failures:
        # Find next recovery for this host
        recovery = None
        for rh, rts in recoveries:
            if rh == host and rts > fail_ts:
                recovery = rts
                break

        rec_hours = None
        if recovery:
            rec_hours = (recovery - fail_ts).total_seconds() / 3600

        disturbances.append(Disturbance(
            event_type="node_failure",
            onset=fail_ts,
            recovered=recovery,
            recovery_hours=rec_hours,
            severity="moderate" if rec_hours and rec_hours > 4 else "minor",
            detail=f"Node '{host}' went unhealthy",
        ))

    return disturbances


def _detect_job_failure_spikes(
    conn: sqlite3.Connection,
    cutoff: str,
    window_hours: int = 6,
    threshold_multiplier: float = 2.0,
) -> list[Disturbance]:
    """Detect periods of abnormally high job failure rates."""
    disturbances = []

    # Compute failure rate per window
    rows = conn.execute("""
        SELECT
            strftime('%Y-%m-%d %H', end_time) AS window,
            COUNT(*) AS total,
            SUM(CASE WHEN state = 'FAILED' THEN 1 ELSE 0 END) AS failed
        FROM jobs
        WHERE end_time >= ?
        GROUP BY window
        ORDER BY window
    """, (cutoff,)).fetchall()

    if len(rows) < 4:
        return disturbances

    # Compute baseline failure rate (overall)
    total_jobs = sum(r["total"] for r in rows)
    total_failures = sum(r["failed"] for r in rows)
    baseline_rate = total_failures / total_jobs if total_jobs > 0 else 0

    if baseline_rate == 0:
        return disturbances

    # Find windows where failure rate exceeds threshold
    in_spike = False
    spike_start = None

    for r in rows:
        rate = r["failed"] / r["total"] if r["total"] > 0 else 0
        ts = datetime.strptime(r["window"], "%Y-%m-%d %H")

        if rate > baseline_rate * threshold_multiplier and not in_spike:
            in_spike = True
            spike_start = ts
        elif rate <= baseline_rate * threshold_multiplier and in_spike:
            in_spike = False
            rec_hours = (ts - spike_start).total_seconds() / 3600

            severity = "major" if rec_hours > 12 else "moderate" if rec_hours > 4 else "minor"

            disturbances.append(Disturbance(
                event_type="job_failure_spike",
                onset=spike_start,
                recovered=ts,
                recovery_hours=rec_hours,
                severity=severity,
                detail=(
                    f"Job failure rate spiked above "
                    f"{baseline_rate * threshold_multiplier:.0%} "
                    f"(baseline: {baseline_rate:.0%})"
                ),
                baseline_metric=baseline_rate,
            ))

    return disturbances


def compute_resilience(
    db_path: Path | str,
    hours: int = 720,  # default: 30 days
) -> ResilienceResult:
    """Compute cluster resilience from historical disturbance data.

    Parameters
    ----------
    db_path : path to NØMAÐ database
    hours : how far back to look for disturbances (default 30 days)
    """
    db_path = Path(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

    # ── Detect disturbances ───────────────────────────────────────────
    disturbances: list[Disturbance] = []
    disturbances.extend(_detect_node_failures(conn, cutoff))
    disturbances.extend(_detect_job_failure_spikes(conn, cutoff))

    conn.close()

    # Sort by onset time
    disturbances.sort(key=lambda d: d.onset)

    if not disturbances:
        return ResilienceResult(
            disturbances=[],
            resilience_score=100.0,
            summary="No disturbance events detected in the analysis window. "
                    "Insufficient data to assess resilience dynamics.",
        )

    # ── Recovery time statistics ──────────────────────────────────────
    recovered = [d for d in disturbances if d.recovery_hours is not None]

    mean_rec = None
    median_rec = None
    if recovered:
        rec_hours = sorted(d.recovery_hours for d in recovered)
        mean_rec = sum(rec_hours) / len(rec_hours)
        mid = len(rec_hours) // 2
        median_rec = rec_hours[mid] if len(rec_hours) % 2 else (
            (rec_hours[mid - 1] + rec_hours[mid]) / 2
        )

    # ── Resilience trend ──────────────────────────────────────────────
    # Compare recovery times of earlier vs. later disturbances
    trend = "stable"
    if len(recovered) >= 4:
        half = len(recovered) // 2
        early_mean = sum(d.recovery_hours for d in recovered[:half]) / half
        late_mean = sum(d.recovery_hours for d in recovered[half:]) / (len(recovered) - half)

        if late_mean < early_mean * 0.75:
            trend = "improving"
        elif late_mean > early_mean * 1.25:
            trend = "degrading"

    # ── Resilience score (0-100) ──────────────────────────────────────
    # Based on: fewer disturbances, faster recovery, improving trend
    n_disturbances = len(disturbances)
    unrecovered = sum(1 for d in disturbances if d.recovery_hours is None)

    # Start at 100, deduct points
    score = 100.0
    score -= min(n_disturbances * 5, 30)  # up to -30 for frequency
    score -= min(unrecovered * 15, 30)  # up to -30 for unrecovered
    if mean_rec:
        score -= min(mean_rec * 2, 20)  # up to -20 for slow recovery
    if trend == "degrading":
        score -= 10
    elif trend == "improving":
        score += 5

    score = max(0.0, min(100.0, score))

    # ── Summary ───────────────────────────────────────────────────────
    parts = [
        f"{n_disturbances} disturbance events detected "
        f"in the past {hours // 24} days.",
    ]
    if mean_rec:
        parts.append(f"Mean recovery time: {mean_rec:.1f} hours.")
    if unrecovered:
        parts.append(f"{unrecovered} events have not yet recovered.")
    if trend != "stable":
        direction = "improving (faster recovery)" if trend == "improving" else "degrading (slower recovery)"
        parts.append(f"Resilience is {direction} over time.")

    return ResilienceResult(
        disturbances=disturbances,
        mean_recovery_hours=mean_rec,
        median_recovery_hours=median_rec,
        resilience_trend=trend,
        resilience_score=score,
        summary=" ".join(parts),
    )

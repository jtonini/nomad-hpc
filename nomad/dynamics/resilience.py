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

from nomad.db import scope


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
    resilience_score: float | None = 0.0  # 0-100, higher = more resilient; None = nothing to read
    summary: str = ""
    counted_events: int = 0        # failures and spikes (drains not counted)
    drains: int = 0


# Node conditions, read from Slurm's state as whole tokens ("POWERED_DOWN"
# is a cloud node at rest, not a failure; "IDLE*" is not responding).
_DOWN_TOKENS = {"DOWN", "NOT_RESPONDING", "NO_RESPOND", "FAIL", "FAILING", "FAILG",
                "ERROR", "UNKNOWN", "UNK", "INVALID", "INVALID_REG", "INVAL"}
_DRAIN_TOKENS = {"DRAIN", "DRAINED", "DRAINING", "DRNG"}
_AT_REST = {"POWERED_DOWN", "POWERING_DOWN", "POWER_DOWN", "POWERED_OFF"}


def node_condition(state, is_healthy=None) -> str:
    """'up', 'drained' or 'down' for one node_state sample."""
    s = str(state or "").strip().upper()
    tokens = {t.strip("~#%$@^!-*") for t in s.split("+") if t}
    if "*" in s:
        tokens.add("NOT_RESPONDING")
    if tokens & _DOWN_TOKENS:
        return "down"
    if tokens & _DRAIN_TOKENS:
        return "drained"
    if tokens & _AT_REST:
        return "up"
    if is_healthy is not None and not is_healthy:
        return "down"
    return "up"


def _detect_node_failures(
    conn: sqlite3.Connection,
    cutoff: str,
) -> list[Disturbance]:
    """Periods where a node went down or stopped responding, and drains.

    A drain is kept apart ("node_drain"): most are an administrator taking
    a node out on purpose -- vendor work, maintenance -- and counting them
    as failures made planned work look like fragility.

    The samples are walked row by row rather than loaded at once: thirty
    days of 5-minute samples on a hundred nodes are nearly a million rows.
    """
    disturbances = []

    cols = {r[1] for r in conn.execute("PRAGMA table_info(node_state)")}
    reason = "reason" if "reason" in cols else "NULL"
    healthy = "is_healthy" if "is_healthy" in cols else "NULL"
    rows = conn.execute(f"""
        SELECT node_name, timestamp, state, {healthy} AS is_healthy, {reason} AS reason
        FROM node_state
        WHERE timestamp >= ?
        ORDER BY node_name, timestamp
    """, (cutoff,))

    current: dict[str, str] = {}
    open_events: dict[str, tuple[str, datetime, str]] = {}

    def close(host: str, ts: datetime | None):
        kind, onset, why = open_events.pop(host)
        rec_hours = (ts - onset).total_seconds() / 3600 if ts else None
        event = "node_failure" if kind == "down" else "node_drain"
        what = "went down or stopped responding" if kind == "down" else "was drained"
        disturbances.append(Disturbance(
            event_type=event,
            onset=onset,
            recovered=ts,
            recovery_hours=rec_hours,
            severity="moderate" if rec_hours is None or rec_hours > 4 else "minor",
            detail=f"Node '{host}' {what}" + (f": {why}" if why else ""),
        ))

    for r in rows:
        host = r["node_name"]
        ts = scope.parse_time(r["timestamp"])
        if ts is None:
            continue
        cond = node_condition(r["state"], r["is_healthy"])
        prev = current.get(host)
        if prev is not None and cond != prev:
            if host in open_events:
                close(host, ts)
            if cond in ("down", "drained"):
                open_events[host] = (cond, ts, r["reason"] or "")
        current[host] = cond

    for host in list(open_events):
        close(host, None)

    return disturbances


MIN_SPIKE_JOBS = 10
MIN_SPIKE_FAILURES = 5


def _detect_job_failure_spikes(
    conn: sqlite3.Connection,
    cutoff: str,
    window_hours: int = 6,
    threshold_multiplier: float = 2.0,
) -> list[Disturbance]:
    """Hours when jobs failed at more than twice the usual rate.

    Only hours with enough jobs count (one failed job out of one is a 100%
    "spike"). A spike lasts over consecutive such hours; an hour with too few
    jobs ends it, rather than stretching one Friday spike to Monday. A spike
    still running at the end of the window is reported as ongoing.
    """
    disturbances = []

    rows = conn.execute("""
        SELECT
            strftime('%Y-%m-%d %H', end_time) AS window,
            COUNT(*) AS total,
            SUM(CASE WHEN UPPER(state) IN ('FAILED', 'NODE_FAIL', 'BOOT_FAIL')
                     THEN 1 ELSE 0 END) AS failed
        FROM jobs
        WHERE end_time >= ?
          AND UPPER(COALESCE(state, '')) != 'UNKNOWN'   -- outcome not known
        GROUP BY window
        HAVING total >= ?
        ORDER BY window
    """, (cutoff, MIN_SPIKE_JOBS)).fetchall()

    if len(rows) < 4:
        return disturbances

    total_jobs = sum(r["total"] for r in rows)
    total_failures = sum(r["failed"] for r in rows)
    baseline_rate = total_failures / total_jobs if total_jobs > 0 else 0
    if baseline_rate == 0:
        return disturbances
    limit = baseline_rate * threshold_multiplier

    def add(start: datetime, last: datetime | None, ongoing: bool):
        end = None if ongoing else last + timedelta(hours=1)
        hours = None if ongoing else (end - start).total_seconds() / 3600
        disturbances.append(Disturbance(
            event_type="job_failure_spike",
            onset=start,
            recovered=end,
            recovery_hours=hours,
            severity=("moderate" if hours is None else
                      "major" if hours > 12 else "moderate" if hours > 4 else "minor"),
            detail=(f"Job failure rate above {limit:.0%} "
                    f"(usual: {baseline_rate:.0%})"
                    + (", still going at the end of the window" if ongoing else "")),
            baseline_metric=baseline_rate,
        ))

    start = last = None
    for r in rows:
        try:
            ts = datetime.strptime(r["window"], "%Y-%m-%d %H")
        except (TypeError, ValueError):
            continue
        spiking = r["failed"] / r["total"] > limit and r["failed"] >= MIN_SPIKE_FAILURES
        if start is not None and (not spiking or ts - last > timedelta(hours=1)):
            add(start, last, ongoing=False)
            start = last = None
        if spiking:
            if start is None:
                start = ts
            last = ts
    if start is not None:
        recent = datetime.now() - last <= timedelta(hours=2)
        add(start, last, ongoing=recent)

    return disturbances


def compute_resilience(
    db_path: Path | str,
    hours: int = 720,  # default: 30 days
    site: str | None = None,
) -> ResilienceResult:
    """Compute cluster resilience from historical disturbance data.

    Disturbances are nodes going down or not responding, and hours when
    jobs failed at more than twice the usual rate. Drains are listed but not
    scored: most are deliberate.

    Parameters
    ----------
    db_path : path to NØMAÐ database
    hours : how far back to look for disturbances (default 30 days)
    site : on a combined database, the site to read
    """
    db_path = Path(db_path)
    conn = scope.connect(db_path, site)

    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

    # ── Detect disturbances ───────────────────────────────────────────
    disturbances: list[Disturbance] = []
    measured = False
    try:
        if scope.table_columns(conn, "node_state"):
            measured = measured or conn.execute(
                "SELECT 1 FROM node_state WHERE timestamp >= ? LIMIT 1",
                (cutoff,)).fetchone() is not None
            disturbances.extend(_detect_node_failures(conn, cutoff))
        if scope.table_columns(conn, "jobs"):
            measured = measured or conn.execute(
                "SELECT 1 FROM jobs WHERE end_time >= ? LIMIT 1",
                (cutoff,)).fetchone() is not None
            disturbances.extend(_detect_job_failure_spikes(conn, cutoff))
    finally:
        conn.close()

    # Sort by onset time
    disturbances.sort(key=lambda d: d.onset)
    days = hours // 24

    if not measured:
        return ResilienceResult(
            disturbances=[],
            resilience_score=None,
            summary="No node states or jobs to read in this window.",
        )

    counted = [d for d in disturbances if d.event_type != "node_drain"]
    drains = [d for d in disturbances if d.event_type == "node_drain"]
    drain_note = ""
    if drains:
        drain_note = (f" {len(drains)} drain{'s' if len(drains) != 1 else ''} "
                      f"listed but not scored (usually deliberate).")

    if not counted:
        return ResilienceResult(
            disturbances=disturbances,
            resilience_score=100.0,
            summary=(f"No node failures or failure spikes in the past {days} days."
                     + drain_note),
            drains=len(drains),
        )

    # ── Recovery time statistics ──────────────────────────────────────
    recovered = [d for d in counted if d.recovery_hours is not None]

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
    # Compare recovery times of earlier vs. later disturbances; with fewer
    # than four recovered events there is no trend to speak of.
    trend = "too_few_events"
    if len(recovered) >= 4:
        trend = "stable"
        half = len(recovered) // 2
        early_mean = sum(d.recovery_hours for d in recovered[:half]) / half
        late_mean = sum(d.recovery_hours for d in recovered[half:]) / (len(recovered) - half)

        if late_mean < early_mean * 0.75:
            trend = "improving"
        elif late_mean > early_mean * 1.25:
            trend = "degrading"

    # ── Resilience score (0-100) ──────────────────────────────────────
    # Based on: fewer disturbances, faster recovery, improving trend
    n_disturbances = len(counted)
    unrecovered = sum(1 for d in counted if d.recovery_hours is None)

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
    nodes = sum(1 for d in counted if d.event_type == "node_failure")
    spikes = n_disturbances - nodes
    what = []
    if nodes:
        what.append(f"{nodes} node failure{'s' if nodes != 1 else ''}")
    if spikes:
        what.append(f"{spikes} job failure spike{'s' if spikes != 1 else ''}")
    parts = [f"{' and '.join(what).capitalize()} in the past {days} days."]
    if median_rec is not None:
        parts.append(f"Median recovery {median_rec:.1f} hours (mean {mean_rec:.1f}).")
    if unrecovered:
        parts.append(f"{unrecovered} not yet recovered.")
    if trend in ("improving", "degrading"):
        direction = "improving (faster recovery)" if trend == "improving" else "degrading (slower recovery)"
        parts.append(f"Resilience is {direction} over time.")

    return ResilienceResult(
        disturbances=disturbances,
        mean_recovery_hours=mean_rec,
        median_recovery_hours=median_rec,
        resilience_trend=trend,
        resilience_score=score,
        summary=" ".join(parts) + drain_note,
        counted_events=n_disturbances,
        drains=len(drains),
    )

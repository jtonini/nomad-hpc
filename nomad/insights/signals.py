# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Signal readers for the NØMAÐ Insight Engine.

Each reader queries a specific data source (jobs, disk, GPU, network,
alerts, nodes, workstations, dynamics) and returns typed Signal objects
that the engine can interpret and narrate.

Every reader takes ``site``: on a combined database (``nomad sync``) it reads
only that site's rows, through read-only views (nomad.db.scope) -- nothing is
copied. A reader that finds its table missing returns no signals; any other
database error is raised, so the engine can say the reader failed instead of
reporting "nothing found".
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import logging

from nomad.db import scope

logger = logging.getLogger(__name__)


class SignalType(Enum):
    """Categories of operational signals."""
    DISK = "disk"
    GPU = "gpu"
    NETWORK = "network"
    JOBS = "jobs"
    MEMORY = "memory"
    QUEUE = "queue"
    TESSERA = "tessera"
    DERIVATIVE = "derivative"
    ALERT = "alert"
    CLOUD = "cloud"
    INTERACTIVE = "interactive"
    DYNAMICS = "dynamics"
    PER_USER = "per_user"
    NODES = "nodes"
    DATA = "data"



class Severity(Enum):
    """Signal severity levels."""
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    CRITICAL = "critical"


SEVERITY_ORDER = [Severity.INFO, Severity.NOTICE, Severity.WARNING, Severity.CRITICAL]


@dataclass
class Signal:
    """A single operational signal extracted from NØMAÐ data."""
    signal_type: SignalType
    severity: Severity
    title: str
    detail: str
    metrics: dict[str, Any] = field(default_factory=dict)
    affected_entities: list[str] = field(default_factory=list)
    timestamp: datetime | None = None
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Unique key for deduplication and correlation."""
        return f"{self.signal_type.value}:{self.title}"


# ── Shared helpers ───────────────────────────────────────────────────────

# Below this many jobs a rate is noise: 2 failures in 31 jobs is 6.5%, and a
# "trend" between two such windows says nothing. Counts are still reported.
MIN_JOBS = 50

# A snapshot source (node states, filesystems, queue) older than this is no
# longer "now"; the engine says so instead of showing the old reading as
# current.
STALE_AFTER_HOURS = 2
# A measured value in an alert message: a decimal, or a number with its unit
# ("86.0%", "87%", "120ms", "85°C", "in 3 days"). Digits inside names
# ("GPU 3", "/data2", "spdr17") are not values and keep conditions apart.
_MEASURED = re.compile(
    r"(?<![\w/.\-])\d+(?:\.\d+)?(?=\s*(?:%|ms\b|°C|days?\b|hours?\b|h\b))"
    r"|(?<![\w/.\-])\d+\.\d+")
# Quiet for longer than this, a source has stopped reporting -- a collector
# retired months ago, not an outage to warn about every day. It is listed
# with its last reading but no longer lowers health.
STOPPED_AFTER_DAYS = 7


def _get_conn(db_path: Path, site: str | None = None) -> sqlite3.Connection:
    """Open a read-only connection, limited to one site's rows when given."""
    return scope.connect(db_path, site)


def _no_table(e: Exception) -> bool:
    """The error means the table (or view) isn't there: no data, not a fault."""
    return isinstance(e, sqlite3.OperationalError) and "no such table" in str(e)


def _period(hours: int) -> str:
    if hours % 24 == 0 and hours >= 48:
        return f"{hours // 24} days"
    return f"{hours} hours"


# How a job ended. Slurm writes "CANCELLED by 29405" for a job its owner
# cancelled; a cancelled job is not a failure, and counting it as one put
# arachne "below baseline" with 2 failures in 31 jobs.
_OUTCOME = {
    "COMPLETED": "completed",
    "FAILED": "failed", "BOOT_FAIL": "failed",
    "TIMEOUT": "limit", "OUT_OF_MEMORY": "limit", "OOM": "limit",
    "DEADLINE": "limit",
    "NODE_FAIL": "node_fail",
    "CANCELLED": "cancelled", "REVOKED": "cancelled",
    "PREEMPTED": "preempted",
}


def job_outcome(state) -> str:
    """completed / failed / limit / node_fail / cancelled / preempted / other."""
    s = str(state or "").strip().upper()
    if not s:
        return "other"
    return _OUTCOME.get(s.split()[0].rstrip("+"), "other")


def _state_bucket(state) -> str:
    """The raw state word ("TIMEOUT", "OUT_OF_MEMORY", ...), for breakdowns."""
    s = str(state or "").strip().upper()
    return s.split()[0].rstrip("+") if s else ""


@dataclass
class _Outcomes:
    completed: int = 0
    failed: int = 0
    timeout: int = 0
    oom: int = 0
    node_fail: int = 0
    other_limit: int = 0
    cancelled: int = 0
    preempted: int = 0

    def add(self, state, n: int = 1) -> None:
        kind = job_outcome(state)
        word = _state_bucket(state)
        if kind == "completed":
            self.completed += n
        elif kind == "failed":
            self.failed += n
        elif kind == "limit":
            if word == "TIMEOUT":
                self.timeout += n
            elif word in ("OUT_OF_MEMORY", "OOM"):
                self.oom += n
            else:
                self.other_limit += n
        elif kind == "node_fail":
            self.node_fail += n
        elif kind == "cancelled":
            self.cancelled += n
        elif kind == "preempted":
            self.preempted += n

    @property
    def problems(self) -> int:
        """Failed, hit a limit, or lost to a node failure."""
        return self.failed + self.timeout + self.oom + self.other_limit + self.node_fail

    @property
    def judged(self) -> int:
        """Jobs that ran to an end of their own: not cancelled or preempted."""
        return self.completed + self.problems

    @property
    def problem_rate(self) -> float | None:
        return self.problems / self.judged * 100 if self.judged else None


def _rate_severity(rate: float, judged: int) -> Severity:
    if judged < MIN_JOBS:
        return Severity.INFO
    if rate > 20:
        return Severity.CRITICAL
    if rate > 10:
        return Severity.WARNING
    if rate > 5:
        return Severity.NOTICE
    return Severity.INFO


# ── Job signals ──────────────────────────────────────────────────────────

def read_job_signals(db_path: Path, hours: int = 24, config: dict | None = None,
                     site: str | None = None) -> list[Signal]:
    """How jobs ended in the window, where the problems concentrate, and the change."""
    signals: list[Signal] = []
    now = datetime.now()
    cutoff = (now - timedelta(hours=hours)).isoformat()
    prev_cutoff = (now - timedelta(hours=hours * 2)).isoformat()
    conn = _get_conn(db_path, site)
    try:
        rows = conn.execute("""
            SELECT state, partition, COUNT(*) AS n
            FROM jobs WHERE end_time >= ?
            GROUP BY state, partition
        """, (cutoff,)).fetchall()
        prev_rows = conn.execute("""
            SELECT state, COUNT(*) AS n
            FROM jobs WHERE end_time >= ? AND end_time < ?
            GROUP BY state
        """, (prev_cutoff, cutoff)).fetchall()

        total = _Outcomes()
        by_partition: dict[str, _Outcomes] = {}
        for r in rows:
            total.add(r["state"], r["n"])
            by_partition.setdefault(r["partition"] or "(none)", _Outcomes()).add(r["state"], r["n"])
        ended = sum(r["n"] for r in rows)
        if not ended:
            return signals

        rate = total.problem_rate
        judged = total.judged
        signals.append(Signal(
            signal_type=SignalType.JOBS,
            severity=_rate_severity(rate or 0, judged),
            title="job_success_rate",
            detail=(f"{judged} jobs ended in the last {_period(hours)}"
                    + (f", {rate:.1f}% failed or hit a limit" if rate is not None else "")),
            metrics={
                "total": ended, "judged": judged, "hours": hours,
                "completed": total.completed,
                "problems": total.problems,
                "problem_rate": rate,
                "success_rate": (total.completed / judged * 100) if judged else None,
                "failed": total.failed, "timed_out": total.timeout,
                "oom": total.oom, "node_fail": total.node_fail,
                "other_limit": total.other_limit,
                "cancelled": total.cancelled, "preempted": total.preempted,
                "min_jobs": MIN_JOBS,
            },
        ))

        # Where the problems concentrate: a partition whose rate is well above
        # the rest of the site, with enough problems to mean something.
        for part, o in sorted(by_partition.items(), key=lambda kv: -kv[1].problems):
            if o.problems < 10 or o.judged < MIN_JOBS:
                continue
            rest_problems = total.problems - o.problems
            rest_judged = judged - o.judged
            part_rate = o.problems / o.judged * 100
            rest_rate = rest_problems / rest_judged * 100 if rest_judged else None
            if rest_rate is not None and part_rate < max(2 * rest_rate, 5):
                continue
            if rest_rate is None and part_rate < 10:
                continue
            signals.append(Signal(
                signal_type=SignalType.JOBS,
                severity=Severity.WARNING if part_rate > 20 else Severity.NOTICE,
                title="partition_failure_concentration",
                detail=(f"Partition '{part}': {o.problems} of {o.judged} jobs failed "
                        f"or hit a limit ({part_rate:.1f}%)"),
                metrics={"partition": part, "failures": o.problems, "jobs": o.judged,
                         "pct": part_rate, "elsewhere_pct": rest_rate,
                         "failed": o.failed, "timed_out": o.timeout, "oom": o.oom,
                         "node_fail": o.node_fail},
                affected_entities=[part],
                tags={"partition": part},
            ))

        # Out of memory: who, so an operator can help them
        if total.oom:
            oom_users = conn.execute("""
                SELECT user_name AS username, COUNT(*) AS cnt
                FROM jobs
                WHERE end_time >= ? AND UPPER(state) IN ('OUT_OF_MEMORY', 'OOM')
                GROUP BY user_name ORDER BY cnt DESC LIMIT 3
            """, (cutoff,)).fetchall()
            users = [f"{u['username']} ({u['cnt']})" for u in oom_users]
            share = total.oom / judged * 100 if judged else 0
            signals.append(Signal(
                signal_type=SignalType.MEMORY,
                severity=(Severity.WARNING if total.oom > 3 and share >= 2
                          and judged >= MIN_JOBS else Severity.NOTICE),
                title="oom_failures",
                detail=f"{total.oom} jobs ran out of memory in the last {_period(hours)}",
                metrics={"oom_count": total.oom, "top_users": users, "hours": hours,
                         "share_pct": share},
                affected_entities=[u["username"] for u in oom_users],
            ))

        # Ran out of time: worth a word when it is more than a stray job
        if total.timeout >= 5 and judged and total.timeout / judged >= 0.02:
            share = total.timeout / judged * 100
            signals.append(Signal(
                signal_type=SignalType.JOBS,
                severity=(Severity.WARNING if share >= 10 and judged >= MIN_JOBS
                          else Severity.NOTICE),
                title="timeout_failures",
                detail=f"{total.timeout} jobs ran out of time in the last {_period(hours)}",
                metrics={"timeout_count": total.timeout, "hours": hours,
                         "share_pct": share, "judged": judged},
            ))

        # Change against the window before, when both hold enough jobs
        prev = _Outcomes()
        for r in prev_rows:
            prev.add(r["state"], r["n"])
        if prev.judged >= MIN_JOBS and judged >= MIN_JOBS:
            prev_rate = prev.problem_rate
            delta = rate - prev_rate
            if abs(delta) > 3:
                worse = delta > 0
                signals.append(Signal(
                    signal_type=SignalType.JOBS,
                    severity=(Severity.INFO if not worse
                              else Severity.WARNING if rate > 10 else Severity.NOTICE),
                    title="job_rate_trend",
                    detail=(f"Failed or hit a limit: {prev_rate:.1f}% in the previous "
                            f"{_period(hours)}, {rate:.1f}% now"),
                    metrics={
                        "previous_problem_rate": prev_rate, "current_problem_rate": rate,
                        "delta": delta, "previous_jobs": prev.judged,
                        "current_jobs": judged, "hours": hours,
                        # older names, as success rates
                        "previous_rate": 100 - prev_rate, "current_rate": 100 - rate,
                    },
                ))

    except sqlite3.OperationalError as e:
        if not _no_table(e):
            raise
    finally:
        conn.close()

    return signals


# ── Disk / storage signals ───────────────────────────────────────────────

STORAGE_TREND_DAYS = 30
STORAGE_MIN_TREND_DAYS = 7


def _daily_growth(conn, path: str, since: datetime) -> tuple[float | None, int]:
    """Bytes per day by least squares over daily averages, and the day count."""
    rows = conn.execute("""
        SELECT substr(timestamp, 1, 10) AS day, AVG(used_bytes) AS used
        FROM filesystems
        WHERE path = ? AND timestamp >= ? AND used_bytes IS NOT NULL
        GROUP BY day ORDER BY day
    """, (path, since.isoformat())).fetchall()
    if len(rows) < STORAGE_MIN_TREND_DAYS:
        return None, len(rows)
    xs, ys = [], []
    for r in rows:
        try:
            xs.append(datetime.fromisoformat(r["day"]).toordinal())
            ys.append(float(r["used"]))
        except (TypeError, ValueError):
            continue
    if len(xs) < STORAGE_MIN_TREND_DAYS:
        return None, len(xs)
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if not den:
        return None, len(xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den, len(xs)


def read_disk_signals(db_path: Path, hours: int = 6, config: dict | None = None,
                      site: str | None = None) -> list[Signal]:
    """How full each monitored filesystem is, and how fast it is growing."""
    signals: list[Signal] = []
    now = datetime.now()
    conn = _get_conn(db_path, site)
    try:
        cols = scope.table_columns(conn, "filesystems")
        fs_rows = []
        newest_fs = scope.newest(conn, "filesystems") if cols else None
        if cols and newest_fs:
            # Only paths still reported: a filesystem retired months ago
            # keeps its last reading forever, and a 96% from then is not now.
            since = (newest_fs - timedelta(days=1)).isoformat()
            site_col = "f.source_site" if "source_site" in cols else "NULL"
            fs_rows = conn.execute(f"""
                SELECT f.path, f.used_percent, f.total_bytes, f.used_bytes,
                       f.available_bytes, f.timestamp, {site_col} AS source_site
                FROM filesystems f
                JOIN (SELECT path, MAX(timestamp) AS ts FROM filesystems
                      WHERE timestamp >= ? GROUP BY path) latest
                  ON f.path = latest.path AND f.timestamp = latest.ts
            """, (since,)).fetchall()

        # One filesystem mounted at two paths reports the same size and use;
        # say it once. (arachne's /home and /scratch are one 145 TB volume.)
        groups: dict[tuple, list] = {}
        for r in fs_rows:
            key = ((r["source_site"] or ""), r["total_bytes"], r["used_bytes"]) \
                if r["total_bytes"] else (r["path"],)
            groups.setdefault(key, []).append(r)

        for rows in groups.values():
            rows.sort(key=lambda r: r["path"])
            r = rows[0]
            usage = r["used_percent"]
            if usage is None and r["total_bytes"]:
                usage = (r["used_bytes"] or 0) / r["total_bytes"] * 100
            if usage is None:
                continue
            site_label = r["source_site"] or site or ""
            paths = [x["path"] for x in rows]
            # Read one site at a time, the paths need no site in front.
            prefix = f"{r['source_site']}:" if (r["source_site"] and not site) else ""
            label = prefix + " = ".join(paths)
            taken = scope.parse_time(r["timestamp"])
            age_h = (now - taken).total_seconds() / 3600 if taken else None

            growth, days = _daily_growth(
                conn, r["path"], now - timedelta(days=STORAGE_TREND_DAYS))
            free = r["available_bytes"]
            if free is None and r["total_bytes"] is not None and r["used_bytes"] is not None:
                free = r["total_bytes"] - r["used_bytes"]
            days_to_full = (free / growth) if (growth and growth > 0 and free) else None

            if usage >= 90:
                sev = Severity.CRITICAL
            elif usage >= 80:
                sev = Severity.WARNING
            elif usage >= 70:
                sev = Severity.NOTICE
            elif days_to_full is not None and days_to_full < 30:
                sev = Severity.NOTICE
            else:
                continue
            if days_to_full is not None and days_to_full < 3:
                sev = Severity.CRITICAL
            elif days_to_full is not None and days_to_full < 14 \
                    and SEVERITY_ORDER.index(sev) < SEVERITY_ORDER.index(Severity.WARNING):
                sev = Severity.WARNING

            signals.append(Signal(
                signal_type=SignalType.DISK,
                severity=sev,
                title="filesystem_usage",
                detail=f"{label} at {usage:.0f}%",
                metrics={
                    "server": label, "paths": paths, "site": site_label,
                    "usage_pct": usage,
                    "free_bytes": free, "total_bytes": r["total_bytes"],
                    "growth_bytes_per_day": growth,
                    "free_gb": (free or 0) / 1073741824.0,
                    "avail_gb": (free or 0) / 1073741824.0,
                    "total_gb": (r["total_bytes"] or 0) / 1073741824.0,
                    "growth_gb_per_day": (growth / 1073741824.0) if growth is not None else None,
                    "trend_days": days,
                    "days_until_full": days_to_full,
                    "as_of": r["timestamp"],
                    "age_hours": age_h,
                    "stale": bool(age_h is not None and age_h > STALE_AFTER_HOURS),
                },
                affected_entities=[label],
                tags={"server": site_label, "path": paths[0]},
            ))

        # storage_state (ZFS/NAS appliances)
        scols = scope.table_columns(conn, "storage_state")
        if scols:
            # nomad's storage collector writes usage_pct; the demo database
            # has usage_percent. Asking for the wrong one failed the reader.
            ucol = "usage_pct" if "usage_pct" in scols else "usage_percent"
            cutoff = (now - timedelta(hours=hours)).isoformat()
            rows = conn.execute(f"""
                SELECT s1.hostname, s1.{ucol} AS usage_percent, s1.total_bytes,
                       s1.used_bytes, s1.free_bytes, s1.timestamp
                FROM storage_state s1
                JOIN (SELECT hostname, MAX(timestamp) AS ts FROM storage_state
                      GROUP BY hostname) s2
                  ON s1.hostname = s2.hostname AND s1.timestamp = s2.ts
            """).fetchall()
            for r in rows:
                usage = r["usage_percent"]
                free_gb = (r["free_bytes"] or 0) / 1073741824.0
                total_gb = (r["total_bytes"] or 0) / 1073741824.0
                free_b, total_b = r["free_bytes"] or 0, r["total_bytes"] or 0
                if usage is not None and usage >= 70:
                    sev = (Severity.CRITICAL if usage >= 90 else
                           Severity.WARNING if usage >= 80 else Severity.NOTICE)
                    signals.append(Signal(
                        signal_type=SignalType.DISK,
                        severity=sev,
                        title="filesystem_usage",
                        detail=f"{r['hostname']} at {usage:.0f}% ({free_b / 1e9:.1f} GB free)",
                        metrics={"server": r["hostname"], "usage_pct": usage,
                                 "free_bytes": free_b, "total_bytes": total_b,
                                 "avail_gb": free_gb, "free_gb": free_gb,
                                 "total_gb": total_gb, "as_of": r["timestamp"]},
                        affected_entities=[r["hostname"]],
                        tags={"server": r["hostname"]},
                    ))

                # Filling fast enough to be full within two days?
                history = conn.execute(f"""
                    SELECT timestamp, {ucol} AS usage_pct FROM storage_state
                    WHERE hostname = ? AND timestamp >= ?
                    ORDER BY timestamp
                """, (r["hostname"], cutoff)).fetchall()
                if len(history) < 2:
                    continue
                t0 = scope.parse_time(history[0]["timestamp"])
                t1 = scope.parse_time(history[-1]["timestamp"])
                if not t0 or not t1:
                    continue
                dt_hours = (t1 - t0).total_seconds() / 3600
                if dt_hours <= 0 or history[-1]["usage_pct"] is None \
                        or history[0]["usage_pct"] is None:
                    continue
                rate = (history[-1]["usage_pct"] - history[0]["usage_pct"]) / dt_hours
                if rate <= 0.5:
                    continue
                hours_to_full = (100 - history[-1]["usage_pct"]) / rate
                if hours_to_full >= 48:
                    continue
                fill_gb = rate / 100 * total_b / 1e9           # decimal GB, as labelled
                signals.append(Signal(
                    signal_type=SignalType.DISK,
                    severity=Severity.CRITICAL if hours_to_full < 12 else Severity.WARNING,
                    title="disk_fill_projection",
                    detail=(f"{r['hostname']} filling at {fill_gb:.1f} GB/hr, "
                            f"projected full in {hours_to_full:.0f}h"),
                    metrics={"server": r["hostname"], "hostname": r["hostname"],
                             "fill_rate_gb_hr": fill_gb, "hours_to_full": hours_to_full},
                    affected_entities=[r["hostname"]],
                    tags={"server": r["hostname"]},
                ))

    except sqlite3.OperationalError as e:
        if not _no_table(e):
            raise
    finally:
        conn.close()

    return signals


# ── GPU signals ──────────────────────────────────────────────────────────

def read_gpu_signals(db_path: Path, hours: int = 24, config: dict | None = None,
                     site: str | None = None) -> list[Signal]:
    """How GPU jobs ended, and whether the problems are one person's."""
    signals: list[Signal] = []
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = _get_conn(db_path, site)
    try:
        rows = conn.execute("""
            SELECT state, user_name, COUNT(*) AS n
            FROM jobs
            WHERE end_time >= ? AND req_gpus > 0
            GROUP BY state, user_name
        """, (cutoff,)).fetchall()
        if not rows:
            return signals
        total = _Outcomes()
        problems_by_person: dict[str, int] = {}
        for r in rows:
            total.add(r["state"], r["n"])
            if job_outcome(r["state"]) in ("failed", "limit", "node_fail"):
                problems_by_person[r["user_name"]] = \
                    problems_by_person.get(r["user_name"], 0) + r["n"]

        rate = total.problem_rate
        if total.judged >= MIN_JOBS and rate is not None and rate > 20:
            people = len(problems_by_person)
            top = max(problems_by_person.values()) if problems_by_person else 0
            signals.append(Signal(
                signal_type=SignalType.GPU,
                severity=Severity.WARNING,
                title="gpu_job_failure_rate",
                detail=(f"{rate:.0f}% of GPU jobs failed or hit a limit "
                        f"({total.problems}/{total.judged})"),
                metrics={"total_gpu_jobs": total.judged, "failed": total.problems,
                         "fail_rate": rate, "people": people,
                         "top_person_share": (top / total.problems * 100)
                         if total.problems else 0,
                         "hours": hours},
            ))

        if total.oom:
            signals.append(Signal(
                signal_type=SignalType.GPU,
                severity=(Severity.WARNING if total.oom > 2 and total.judged >= MIN_JOBS
                          else Severity.NOTICE),
                title="gpu_oom",
                detail=f"{total.oom} GPU jobs ran out of memory",
                metrics={"gpu_oom_count": total.oom, "total_gpu_jobs": total.judged},
            ))

    except sqlite3.OperationalError as e:
        if not _no_table(e):
            raise
    finally:
        conn.close()

    return signals


# ── Queue signals ────────────────────────────────────────────────────────

def read_queue_signals(db_path: Path, hours: int = 6, config: dict | None = None,
                       site: str | None = None) -> list[Signal]:
    """Backlog in the latest queue snapshot, and how long jobs waited to start."""
    signals: list[Signal] = []
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = _get_conn(db_path, site)
    try:
        if scope.table_columns(conn, "queue_state"):
            latest = conn.execute("""
                SELECT partition, pending_jobs, running_jobs, total_jobs, timestamp
                FROM queue_state
                WHERE timestamp = (SELECT MAX(timestamp) FROM queue_state)
                  AND timestamp >= ?
            """, (cutoff,)).fetchall()
            for r in latest:
                partition = r["partition"]
                pending = r["pending_jobs"] or 0
                running = r["running_jobs"] or 0
                if running == 0 and pending >= 5:
                    ratio = float(pending)
                elif running > 0 and pending > 0:
                    ratio = pending / running
                else:
                    continue
                if ratio <= 2:
                    continue
                signals.append(Signal(
                    signal_type=SignalType.QUEUE,
                    severity=(Severity.WARNING if ratio > 5 or running == 0
                              else Severity.NOTICE),
                    title="queue_pressure",
                    detail=(f"Partition '{partition}': {pending} pending vs "
                            f"{running} running"),
                    metrics={"partition": partition, "pending": pending,
                             "running": running, "ratio": ratio,
                             "as_of": r["timestamp"]},
                    affected_entities=[partition],
                    tags={"partition": partition},
                ))

        # Wait = start - submit, from the job's own timestamps (a stored
        # wait_time_seconds is not trusted), as a median: a few held or
        # array jobs make a mean meaningless.
        waits: dict[str, list[float]] = {}
        for r in conn.execute("""
            SELECT partition,
                   (julianday(start_time) - julianday(submit_time)) * 86400 AS wait
            FROM jobs
            WHERE end_time >= ? AND start_time IS NOT NULL AND submit_time IS NOT NULL
        """, (cutoff,)):
            if r["wait"] is not None and r["wait"] >= 0:
                waits.setdefault(r["partition"] or "(none)", []).append(r["wait"])
        for partition, ws in waits.items():
            if len(ws) < 20:
                continue
            median = statistics.median(ws)
            if median <= 3600:
                continue
            signals.append(Signal(
                signal_type=SignalType.QUEUE,
                severity=(Severity.WARNING if median > 6 * 3600 and len(ws) >= MIN_JOBS
                          else Severity.NOTICE),
                title="high_wait_time",
                detail=(f"Partition '{partition}': median wait {median/3600:.1f}h "
                        f"over {len(ws)} jobs"),
                metrics={"partition": partition, "avg_wait_sec": median,
                         "median_wait_sec": median, "max_wait_sec": max(ws),
                         "jobs": len(ws)},
                affected_entities=[partition],
                tags={"partition": partition},
            ))

    except sqlite3.OperationalError as e:
        if not _no_table(e):
            raise
    finally:
        conn.close()

    return signals


# ── Network signals ──────────────────────────────────────────────────────

def read_network_signals(db_path: Path, hours: int = 6, config: dict | None = None,
                         site: str | None = None) -> list[Signal]:
    """Check for network anomalies."""
    signals: list[Signal] = []
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = _get_conn(db_path, site)
    try:
        rows = conn.execute("""
            SELECT source_host || '->' || dest_host as path, AVG(ping_avg_ms) as avg_latency,
                   MAX(ping_avg_ms) as max_latency,
                   AVG(ping_loss_pct) as avg_loss,
                   MAX(ping_loss_pct) as max_loss
            FROM network_perf
            WHERE timestamp >= ?
            GROUP BY source_host, dest_host
        """, (cutoff,)).fetchall()

        for r in rows:
            if (r["avg_latency"] or 0) > 5:
                signals.append(Signal(
                    signal_type=SignalType.NETWORK,
                    severity=Severity.WARNING if r["avg_latency"] > 20 else Severity.NOTICE,
                    title="high_network_latency",
                    detail=f"Path '{r['path']}': avg latency {r['avg_latency']:.1f}ms (peak {r['max_latency']:.1f}ms)",
                    metrics={"path": r["path"], "avg_latency": r["avg_latency"], "max_latency": r["max_latency"]},
                    affected_entities=[r["path"]],
                ))

            if (r["avg_loss"] or 0) > 0.1:
                signals.append(Signal(
                    signal_type=SignalType.NETWORK,
                    severity=Severity.CRITICAL if r["avg_loss"] > 1 else Severity.WARNING,
                    title="packet_loss",
                    detail=f"Path '{r['path']}': {r['avg_loss']:.2f}% average packet loss",
                    metrics={"path": r["path"], "avg_loss": r["avg_loss"], "max_loss": r["max_loss"]},
                    affected_entities=[r["path"]],
                ))

    except sqlite3.OperationalError as e:
        if not _no_table(e):
            raise
    finally:
        conn.close()

    return signals


# ── Alert signals ────────────────────────────────────────────────────────

def _alert_site(details) -> str | None:
    try:
        d = json.loads(details) if isinstance(details, str) else (details or {})
        return d.get("site") if isinstance(d, dict) else None
    except (TypeError, ValueError):
        return None


def read_alert_signals(db_path: Path, hours: int = 24, config: dict | None = None,
                       site: str | None = None) -> list[Signal]:
    """Alerts raised in the window, grouped by what they were about.

    nomad stores alerts but never marks them resolved, so an alert is not
    "active": it was raised. A condition that persists is raised again once
    a day (1.7.16; every cooldown before), which is not flapping either --
    so neither is claimed.
    The stored columns differ: nomad writes category/source(=host), the demo
    database source(=metric)/host; both are read.
    """
    signals: list[Signal] = []
    now = datetime.now()
    cutoff = (now - timedelta(hours=hours)).isoformat()
    conn = _get_conn(db_path, site)
    try:
        cols = scope.table_columns(conn, "alerts")
        if not cols:
            return signals
        if "host" in cols:
            what, host = "source", "host"
        else:
            what, host = ("category" if "category" in cols else "source"), "source"
        details = "details" if "details" in cols else "NULL"
        dedup = "dedup_key" if "dedup_key" in cols else "NULL"
        rows = conn.execute(f"""
            SELECT severity, {what} AS what, {host} AS host, message,
                   {details} AS details, timestamp, {dedup} AS dedup
            FROM alerts WHERE timestamp >= ?
            ORDER BY timestamp DESC
        """, (cutoff,)).fetchall()

        # Alerts merged without source_site carry their site in details
        # (nomad 1.7.9+); older ones can't be placed.
        unplaced = 0
        if site and "source_site" not in cols:
            kept = []
            for r in rows:
                s = _alert_site(r["details"])
                if s == site:
                    kept.append(r)
                elif s is None:
                    unplaced += 1
            rows = kept
        # `nomad test-alerts` stores its test (category "test"): proof that
        # mail works, not something about the system, so it doesn't count.
        rows = [r for r in rows if (r["what"] or "").lower() != "test"]
        if not rows:
            return signals

        # One condition however its numbers move: "/scratch at 86.0%" and
        # "at 87.0%" are the same alert raised on different days.
        conditions: dict[tuple, dict] = {}
        for r in rows:
            # 1.7.16 names the condition (source|host|subject[|metric]); a
            # forecast's "in 45 hours" and "in 1.9 days" are one condition.
            if r["dedup"] and "|" in str(r["dedup"]):
                key = ("key", str(r["dedup"]), "")
            else:
                shape = _MEASURED.sub("#", r["message"] or "")[:80]
                key = (r["what"] or "", r["host"] or "", shape)
            c = conditions.setdefault(key, {
                "message": r["message"] or "", "count": 0, "last": r["timestamp"],
                "severity": (r["severity"] or "").lower(), "host": r["host"],
            })
            c["count"] += 1

        recent_cut = (now - timedelta(hours=24)).isoformat()
        recent = [r for r in rows if (r["timestamp"] or "") >= recent_cut]
        sevs = {(r["severity"] or "").lower() for r in recent}
        sev = (Severity.CRITICAL if "critical" in sevs else
               Severity.WARNING if "warning" in sevs else Severity.NOTICE)
        ordered = sorted(conditions.values(), key=lambda c: (c["last"] or ""), reverse=True)
        signals.append(Signal(
            signal_type=SignalType.ALERT,
            severity=sev,
            title="alerts_raised",
            detail=(f"{len(rows)} alerts raised in the last {_period(hours)} "
                    f"about {len(conditions)} condition(s)"),
            metrics={
                "total": len(rows), "conditions": len(conditions),
                "last_24h": len(recent), "hours": hours,
                "critical": sum(1 for r in rows if (r["severity"] or "").lower() == "critical"),
                "warning": sum(1 for r in rows if (r["severity"] or "").lower() == "warning"),
                "unplaced": unplaced,
                "top": [{"message": c["message"], "count": c["count"], "last": c["last"],
                         "severity": c["severity"]} for c in ordered[:5]],
                "messages": [c["message"] for c in ordered[:10]],
            },
        ))

    except sqlite3.OperationalError as e:
        if not _no_table(e):
            raise
    finally:
        conn.close()

    return signals


# ── Cloud signals ────────────────────────────────────────────────────────

def read_cloud_signals(db_path: Path, hours: int = 24, config: dict | None = None,
                       site: str | None = None) -> list[Signal]:
    """Analyze cloud instance metrics for cost and performance signals."""
    signals: list[Signal] = []
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = _get_conn(db_path, site)
    try:
        cost_rows = conn.execute("""
            SELECT SUM(value) as total_cost
            FROM cloud_metrics
            WHERE metric_name = 'cost_usd_per_day' AND timestamp >= ?
        """, (cutoff,)).fetchone()

        if cost_rows and cost_rows["total_cost"]:
            total = cost_rows["total_cost"]
            signals.append(Signal(
                signal_type=SignalType.CLOUD,
                severity=Severity.INFO,
                title="cloud_cost_summary",
                detail=f"Cloud spending: ${total:.2f} in the last {hours}h",
                metrics={"total_cost_usd": total, "hours": hours},
            ))

        underused = conn.execute("""
            SELECT node_name, AVG(value) as avg_cpu
            FROM cloud_metrics
            WHERE metric_name = 'cpu_utilization' AND timestamp >= ?
            GROUP BY node_name
            HAVING avg_cpu < 15
        """, (cutoff,)).fetchall()

        for u in underused:
            signals.append(Signal(
                signal_type=SignalType.CLOUD,
                severity=Severity.NOTICE,
                title="underutilized_cloud_instance",
                detail=f"Instance '{u['node_name']}' averaging {u['avg_cpu']:.1f}% CPU — consider downsizing",
                metrics={"instance": u["node_name"], "avg_cpu": u["avg_cpu"]},
                affected_entities=[u["node_name"]],
            ))

    except sqlite3.OperationalError as e:
        if not _no_table(e):
            raise
    finally:
        conn.close()

    return signals


# ── Node health signals ──────────────────────────────────────────────────

def _state_tokens(state) -> set[str]:
    s = str(state or "").strip().upper()
    tokens = {t.strip("~#%$@^!-*") for t in s.split("+") if t}
    if "*" in s:
        tokens.add("NOT_RESPONDING")
    return tokens


def read_node_health_signals(
    db_path: Path,
    hours: int = 24,
    config: dict | None = None,
    site: str | None = None,
) -> list[Signal]:
    """
    Nodes that are down, not responding or drained in the latest snapshot.

    Independent of the alerts table: queries node_state directly so the
    digest catches node issues even when alert dispatch is unconfigured.
    Reads the site's latest snapshot (not each node's last row ever, which
    kept retired nodes "down" forever). States are read as whole tokens
    ("POWERED_DOWN" is not DOWN; "IDLE*" is not responding).

    Configuration (nomad.toml):
        [signals.node_state]
        critical_states = [...]   # state tokens -> critical signals
        warning_states  = [...]   # state tokens -> warning signals
        ignore_states   = [...]   # never surface these states
        summary_threshold = 3     # multi-node rollup fires above this count
                                  # (0 disables the rollup signal)
    """
    cfg = (config or {}).get('signals', {}).get('node_state', {})
    critical = {x.upper() for x in cfg.get('critical_states',
                ['DOWN', 'NOT_RESPONDING', 'FAIL', 'FAILING', 'OFFLINE', 'UNREACH'])}
    warning = {x.upper() for x in cfg.get('warning_states',
                ['DRAIN', 'DRNG', 'DRAINING', 'DRAINED', 'CLOSED', 'UNAVAIL'])}
    ignore = {x.upper() for x in cfg.get('ignore_states', [])}
    summary_threshold = cfg.get('summary_threshold', 3)

    from nomad.collectors.node_state import node_is_available

    signals: list[Signal] = []
    conn = _get_conn(db_path, site)
    try:
        cols = scope.table_columns(conn, "node_state")
        if not cols:
            return signals
        extra = ", ".join(c if c in cols else f"NULL AS {c}"
                          for c in ("reason", "cluster", "partitions", "is_healthy"))
        rows = conn.execute(f"""
            SELECT node_name, state, timestamp, {extra}
            FROM node_state
            WHERE timestamp = (SELECT MAX(timestamp) FROM node_state)
            ORDER BY node_name
        """).fetchall()

        taken = scope.parse_time(rows[0]["timestamp"]) if rows else None
        old = taken is not None and \
            (datetime.now() - taken).total_seconds() / 3600 > STALE_AFTER_HOURS
        crit_count = 0
        for r in rows:
            state = (r["state"] or "").upper()
            tokens = _state_tokens(state)
            unhealthy = (not node_is_available(state)) or r["is_healthy"] == 0
            if not unhealthy or (tokens & ignore):
                continue
            if tokens & critical:
                sev, title = Severity.CRITICAL, "node_down"
                crit_count += 1
            elif tokens & warning:
                sev, title = Severity.WARNING, "node_drain"
            else:
                sev, title = Severity.WARNING, "node_unhealthy"

            node = r["node_name"]
            cluster = r["cluster"] or site or "default"
            reason = r["reason"] or "no reason given"
            part = r["partitions"] or ""
            detail = (f"Node {node} was {state} on {cluster} at the last report "
                      f"({taken:%Y-%m-%d %H:%M})" if old
                      else f"Node {node} is {state} on {cluster}")
            if part:
                detail += f" ({part} partition)"
            detail += f"; reason: {reason}"

            signals.append(Signal(
                signal_type=SignalType.NODES,
                severity=sev,
                title=title,
                detail=detail,
                metrics={
                    "node": node, "state": state, "reason": reason,
                    "cluster": cluster, "partitions": part,
                    "last_seen": r["timestamp"],
                },
                affected_entities=[node],
            ))

        if summary_threshold > 0 and len(signals) > summary_threshold:
            signals.insert(0, Signal(
                signal_type=SignalType.NODES,
                severity=Severity.CRITICAL if crit_count else Severity.WARNING,
                title="multiple_nodes_unhealthy",
                detail=(f"{len(signals)} nodes "
                        + ("were unavailable at the last report" if old
                           else "currently unavailable")
                        + f" ({crit_count} down or not responding)."),
                metrics={
                    "total_unhealthy": len(signals),
                    "critical_count": crit_count,
                    "warning_count": len(signals) - crit_count,
                    "nodes_in_snapshot": len(rows),
                },
            ))
    except sqlite3.OperationalError as e:
        if not _no_table(e):
            raise
    finally:
        conn.close()

    return signals


# ── Workstation signals ──────────────────────────────────────────────────

def read_workstation_signals(db_path: Path, hours: int = 6, config: dict | None = None,
                             site: str | None = None) -> list[Signal]:
    """Busy, full or zombie-ridden workstations, from each one's latest reading."""
    signals: list[Signal] = []
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    conn = _get_conn(db_path, site)
    try:
        cols = scope.table_columns(conn, "workstation_state")
        if not cols:
            return signals
        wanted = ("load_avg_1m", "cpu_count", "memory_total_mb", "memory_used_mb",
                  "disk_total_gb", "disk_used_gb", "disk_usage_pct",
                  "zombie_count", "source_site")
        select = ", ".join(f"w.{c}" if c in cols else f"NULL AS {c}" for c in wanted)
        rows = conn.execute(f"""
            SELECT w.hostname, w.timestamp, {select}
            FROM workstation_state w
            JOIN (SELECT hostname, MAX(timestamp) AS ts FROM workstation_state
                  WHERE timestamp >= ? GROUP BY hostname) latest
              ON w.hostname = latest.hostname AND w.timestamp = latest.ts
        """, (cutoff,)).fetchall()

        for r in rows:
            host = r['hostname']
            label = f"{r['source_site']}:{host}" if (r['source_site'] and not site) else host

            if r['memory_total_mb'] and r['memory_used_mb'] is not None:
                mem_pct = r['memory_used_mb'] / r['memory_total_mb'] * 100
                if mem_pct > 85:
                    signals.append(Signal(
                        signal_type=SignalType.MEMORY,
                        severity=Severity.WARNING if mem_pct > 95 else Severity.NOTICE,
                        title='workstation_high_memory',
                        detail=(f"{label}: memory at {mem_pct:.0f}% "
                                f"({r['memory_used_mb']/1024:.1f}/{r['memory_total_mb']/1024:.1f} GB)"),
                        metrics={'hostname': label, 'mem_pct': mem_pct,
                                 'avg_mem': mem_pct, 'as_of': r['timestamp']},
                        affected_entities=[label],
                    ))

            if r['load_avg_1m'] is not None and r['cpu_count']:
                ratio = r['load_avg_1m'] / r['cpu_count']
                if ratio > 0.8:
                    signals.append(Signal(
                        signal_type=SignalType.INTERACTIVE,
                        severity=Severity.WARNING if ratio > 1.5 else Severity.NOTICE,
                        title='workstation_high_cpu',
                        detail=(f"{label}: load {r['load_avg_1m']:.1f} on "
                                f"{r['cpu_count']} cores ({ratio:.1f}x)"),
                        metrics={'hostname': label, 'load': r['load_avg_1m'],
                                 'cpus': r['cpu_count'], 'load_ratio': ratio,
                                 'as_of': r['timestamp']},
                        affected_entities=[label],
                    ))

            disk_pct = r['disk_usage_pct']
            if disk_pct is None and r['disk_total_gb']:
                disk_pct = (r['disk_used_gb'] or 0) / r['disk_total_gb'] * 100
            if disk_pct is not None and disk_pct > 80:
                free_gb = ((r['disk_total_gb'] or 0) - (r['disk_used_gb'] or 0)) \
                    if r['disk_total_gb'] else None
                signals.append(Signal(
                    signal_type=SignalType.DISK,
                    severity=(Severity.CRITICAL if disk_pct > 95 else
                              Severity.WARNING if disk_pct > 90 else Severity.NOTICE),
                    title='workstation_disk_usage',
                    detail=(f"{label}: disk at {disk_pct:.0f}%"
                            + (f" ({free_gb:.0f} GB free)" if free_gb is not None else "")),
                    metrics={'hostname': label, 'disk_pct': disk_pct, 'free_gb': free_gb},
                    affected_entities=[label],
                ))

            zombies = r['zombie_count'] or 0
            if zombies > 5:
                signals.append(Signal(
                    signal_type=SignalType.INTERACTIVE,
                    severity=Severity.NOTICE,
                    title='workstation_zombies',
                    detail=f"{label}: {zombies} zombie processes",
                    metrics={'hostname': label, 'zombies': zombies},
                    affected_entities=[label],
                ))

    except sqlite3.OperationalError as e:
        if not _no_table(e):
            raise
    finally:
        conn.close()

    return signals


# ── Dynamics signals ─────────────────────────────────────────────────────

def read_dynamics_signals(db_path: Path, hours: int = 168, config: dict | None = None,
                          site: str | None = None) -> list[Signal]:
    """What the dynamics analyses find that an operator should hear about.

    Group-based findings (niche overlap, externalities, diversity by group)
    appear only when each job can be attributed to one group; see
    nomad.dynamics.attribution.
    """
    signals: list[Signal] = []
    label = site or ""
    tag = {"cluster": label} if label else {}

    from nomad.dynamics.diversity import compute_diversity
    div = compute_diversity(db_path, dimension="user", hours=hours, site=site)
    jobs = sum(div.current.category_counts.values())
    if jobs >= MIN_JOBS and div.current.richness >= 2 \
            and div.current.dominant_proportion > 0.6:
        signals.append(Signal(
            signal_type=SignalType.DYNAMICS,
            severity=Severity.NOTICE,
            title="diversity_fragility",
            detail=(f"One person ran {div.current.dominant_proportion:.0%} of the "
                    f"{jobs} jobs submitted in the last {_period(hours)}"),
            metrics={
                **tag,
                "dimension": "user",
                "dominant": div.current.dominant_category,
                "dominant_proportion": div.current.dominant_proportion,
                "shannon_h": div.current.shannon_h,
                "jobs": jobs, "people": div.current.richness, "hours": hours,
            },
        ))

    from nomad.dynamics.capacity import compute_capacity
    cap = compute_capacity(db_path, hours=hours, site=site)
    bc = cap.binding_constraint
    if bc is not None:
        signals.append(Signal(
            signal_type=SignalType.DYNAMICS,
            severity=(Severity.CRITICAL if bc.current_utilization >= 0.9
                      else Severity.WARNING),
            title="capacity_binding_constraint",
            detail=f"{bc.label} at {bc.current_utilization:.0%}",
            metrics={
                **tag,
                "dimension": bc.dimension, "label": bc.label,
                "utilization": bc.current_utilization,
                "pressure": cap.overall_pressure,
                "hours_to_saturation": bc.hours_to_saturation,
            },
        ))
        if bc.hours_to_saturation and bc.hours_to_saturation < 48:
            signals.append(Signal(
                signal_type=SignalType.DYNAMICS,
                severity=Severity.CRITICAL,
                title="capacity_saturation_imminent",
                detail=(f"{bc.label} projected to reach saturation in "
                        f"{bc.hours_to_saturation:.0f} hours"),
                metrics={**tag, "dimension": bc.label,
                         "hours_to_saturation": bc.hours_to_saturation},
            ))

    from nomad.dynamics.niche import compute_niche_overlap
    niche = compute_niche_overlap(db_path, hours=hours, site=site)
    if niche.available and niche.high_overlap_pairs:
        high = [p for p in niche.high_overlap_pairs if p.contention_risk == "high"]
        if high:
            top = high[0]
            signals.append(Signal(
                signal_type=SignalType.DYNAMICS,
                severity=Severity.WARNING if len(high) >= 3 else Severity.NOTICE,
                title="niche_contention_risk",
                detail=f"{len(high)} high-overlap group pair(s)",
                metrics={**tag, "high_overlap_count": len(high),
                         "top_pair_a": top.group_a, "top_pair_b": top.group_b,
                         "top_overlap": top.overlap},
            ))

    from nomad.dynamics.resilience import compute_resilience
    res = compute_resilience(db_path, hours=max(hours, 720), site=site)
    if res.counted_events >= 3 and res.resilience_score < 50:
        signals.append(Signal(
            signal_type=SignalType.DYNAMICS,
            severity=Severity.WARNING,
            title="resilience_low",
            detail=f"Resilience score {res.resilience_score:.0f}/100",
            metrics={**tag, "score": res.resilience_score,
                     "trend": res.resilience_trend,
                     "mean_recovery_hours": res.mean_recovery_hours,
                     "summary": res.summary},
        ))
    if res.resilience_trend == "degrading":
        signals.append(Signal(
            signal_type=SignalType.DYNAMICS,
            severity=Severity.NOTICE,
            title="resilience_degrading",
            detail="Recovery times are getting longer",
            metrics={**tag, "score": res.resilience_score, "trend": "degrading"},
        ))

    from nomad.dynamics.externality import compute_externalities
    ext = compute_externalities(db_path, hours=hours, site=site)
    if ext.available and ext.top_imposers:
        signals.append(Signal(
            signal_type=SignalType.DYNAMICS,
            severity=Severity.NOTICE,
            title="externality_detected",
            detail=f"{len(ext.edges)} inter-group correlation(s)",
            metrics={**tag, "edge_count": len(ext.edges),
                     "top_imposers": ext.top_imposers[:3],
                     "top_receivers": ext.top_receivers[:3]},
        ))

    return signals


# ── Master reader ────────────────────────────────────────────────────────

@dataclass
class Source:
    """One reader and the table that tells whether it had anything to read."""
    key: str
    label: str
    reader: Any
    table: str
    time_column: str = "timestamp"
    snapshot: bool = True      # a periodic reading, so it can go stale
    hours_cap: int | None = None


SOURCES: list[Source] = [
    Source("jobs", "Jobs", read_job_signals, "jobs", "end_time", snapshot=False),
    Source("storage", "Storage", read_disk_signals, "filesystems", hours_cap=12),
    Source("gpu", "GPU jobs", read_gpu_signals, "jobs", "end_time", snapshot=False),
    Source("queue", "Queue", read_queue_signals, "queue_state", hours_cap=12),
    Source("network", "Network", read_network_signals, "network_perf", hours_cap=12),
    Source("alerts", "Alerts", read_alert_signals, "alerts", snapshot=False),
    Source("nodes", "Nodes", read_node_health_signals, "node_state"),
    Source("cloud", "Cloud", read_cloud_signals, "cloud_metrics"),
    Source("workstations", "Workstations", read_workstation_signals,
           "workstation_state", hours_cap=12),
    Source("dynamics", "Dynamics", read_dynamics_signals, "jobs", "end_time",
           snapshot=False),
]


def _newest_for_site(conn, table: str, column: str, site: str | None):
    """Newest reading for the site. A table without source_site isn't
    limited by the site view; alerts then carry their site in details."""
    cols = scope.table_columns(conn, table)
    if not site or not cols or scope.SITE_COLUMN in cols:
        return scope.newest(conn, table, column)
    if table == "alerts" and "details" in cols:
        try:
            row = conn.execute(
                f"SELECT {column} FROM alerts "
                f"WHERE json_valid(details) AND json_extract(details, '$.site') = ? "
                f"ORDER BY {column} DESC LIMIT 1", (site,)).fetchone()
        except sqlite3.Error:
            return None
        return scope.parse_time(row[0]) if row else None
    return None


def _coverage_for(conn, src: Source, hours: int, now: datetime,
                  site: str | None = None) -> dict:
    """measured / stale / stopped / no_data for one source, from its newest reading."""
    newest = _newest_for_site(conn, src.table, src.time_column, site)
    if src.key == "storage":
        # Either table is storage: a file server's storage_state still
        # reporting keeps storage current when filesystems stopped.
        other = _newest_for_site(conn, "storage_state", "timestamp", site)
        newest = max((t for t in (newest, other) if t is not None), default=None)
    entry = {"source": src.key, "label": src.label,
             "newest": newest.isoformat(timespec="seconds") if newest else None}
    if newest is None:
        entry.update(status="no_data",
                     detail=("no jobs recorded here" if src.table == "jobs"
                             else "not collected here"))
        return entry
    age_h = (now - newest).total_seconds() / 3600
    window = src.hours_cap and min(hours, src.hours_cap) or hours
    if src.snapshot and age_h > STOPPED_AFTER_DAYS * 24:
        entry.update(status="stopped",
                     detail=f"last reading {newest:%Y-%m-%d}")
    elif src.snapshot and age_h > STALE_AFTER_HOURS:
        entry.update(status="stale",
                     detail=f"last reading {newest:%Y-%m-%d %H:%M}, {age_h:.0f}h ago")
    elif not src.snapshot and age_h > window:
        entry.update(status="no_data",
                     detail=f"nothing in the window; last {newest:%Y-%m-%d %H:%M}")
    else:
        entry.update(status="measured", detail="")
    return entry


def read_all_signals_with_coverage(
    db_path: Path,
    hours: int = 24,
    config: dict | None = None,
    site: str | None = None,
) -> tuple[list[Signal], list[dict]]:
    """Run every reader; say for each whether it measured, had no data, or failed.

    A reader whose data is stale still runs (its signals carry their own
    time), and a ``data_stale`` signal names what went quiet.
    """
    if config is None:
        from nomad.config import load_config
        try:
            config = load_config()
        except Exception as e:
            logger.debug(f"Could not load config; using defaults: {e}")
            config = {}

    now = datetime.now()
    signals: list[Signal] = []
    coverage: list[dict] = []
    try:
        conn = _get_conn(db_path, site)
    except sqlite3.Error as e:
        for src in SOURCES:
            coverage.append({"source": src.key, "label": src.label, "newest": None,
                             "status": "failed", "detail": f"database: {e}",
                             "signals": 0})
        return signals, coverage
    try:
        entries = {src.key: _coverage_for(conn, src, hours, now, site) for src in SOURCES}
    finally:
        conn.close()

    for src in SOURCES:
        entry = entries[src.key]
        h = min(hours, src.hours_cap) if src.hours_cap else hours
        if (entry["status"] == "no_data" and entry["newest"] is None) \
                or entry["status"] == "stopped":
            entry["signals"] = 0
            coverage.append(entry)
            continue
        try:
            got = src.reader(db_path, hours=h, config=config, site=site)
        except Exception as e:
            logger.warning(f"Signal reader {src.reader.__name__} failed: {e}")
            entry.update(status="failed", detail=f"{type(e).__name__}: {e}")
            got = []
        entry["signals"] = len(got)
        signals.extend(got)
        coverage.append(entry)

    stale = [e for e in coverage if e["status"] == "stale"]
    if stale:
        where = f" from {site}" if site else ""
        signals.append(Signal(
            signal_type=SignalType.DATA,
            severity=Severity.WARNING,
            title="data_stale",
            detail="No new readings" + where + ": " + "; ".join(
                f"{e['label'].lower()} ({e['detail']})" for e in stale),
            metrics={"site": site, "sources": [
                {"label": e["label"], "newest": e["newest"]} for e in stale]},
        ))
    return signals, coverage


def read_all_signals(
    db_path: Path,
    hours: int = 24,
    config: dict | None = None,
    site: str | None = None,
) -> list[Signal]:
    """
    Run all signal readers and return combined results.

    If config is None, loads from the standard locations via
    nomad.config.load_config(). Each reader receives the full config
    dict and is responsible for finding its own subsection.
    """
    signals, _ = read_all_signals_with_coverage(db_path, hours, config, site)
    return signals


def create_site_db(db_path: Path, site: str) -> Path:
    """Create a temporary database with only one site's data.

    Kept for callers outside nomad. It copies every table, which on a real
    combined database is gigabytes; nomad itself now reads one site through
    ``site=`` (nomad.db.scope) instead.
    """
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)

    dst = sqlite3.connect(str(tmp_path))
    dst.execute("ATTACH DATABASE ? AS src", (str(db_path),))

    tables = [r[0] for r in dst.execute(
        "SELECT name FROM src.sqlite_master"
        " WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()]

    for tbl in tables:
        try:
            cols = [r[1] for r in dst.execute(
                f"PRAGMA src.table_info({tbl})"
            ).fetchall()]
            if "source_site" in cols:
                dst.execute(
                    f"CREATE TABLE {tbl} AS"
                    f" SELECT * FROM src.{tbl}"
                    f" WHERE source_site = ?",
                    (site,))
            else:
                dst.execute(
                    f"CREATE TABLE {tbl} AS"
                    f" SELECT * FROM src.{tbl}")
        except Exception:
            pass

    dst.commit()
    dst.execute("DETACH DATABASE src")
    dst.close()
    return tmp_path


# ---------------------------------------------------------------------------
# Per-user signals (Idea 18 Component 1)
# ---------------------------------------------------------------------------

from collections import Counter as _PerUserCounter   # avoid colliding if Counter is used elsewhere


@dataclass
class PerUserSignal:
    """Signal derived from per_user_alert rows.

    severity comes through unchanged from the rule definition:
      - 'actionable'    head-node misuse warranting follow-up
      - 'informational' softer cases (IDE-driven memory, etc.)
    """
    signal_type: SignalType = SignalType.PER_USER
    severity: str = "informational"
    hostname: str = ""
    username: str = ""
    rule_id: str = ""
    rule_type: str = ""
    occurrences: int = 1
    last_seen: str = ""
    command: str | None = None
    peak_cpu_percent: float | None = None
    peak_memory_bytes: int | None = None
    sustained_for_seconds: int = 0
    context: dict = field(default_factory=dict)


def read_per_user_signals(
    db_path: str,
    lookback_hours: int = 168,
    hostname: str | None = None,
) -> list:
    """Pull recent per_user_alert rows; aggregate by (host, user, rule, cmd).

    Recent by ``last_seen``: an alert row lives as long as its condition goes
    on (a process at 100% for two weeks is one row, fired two weeks ago).
    per_user writes its times in UTC.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    where = ["last_seen >= ?"]
    params: list = [cutoff]
    if hostname:
        where.append("hostname = ?")
        params.append(hostname)
    where_clause = " AND ".join(where)

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT hostname, username, rule_id, rule_type, severity,
                       SUM(occurrences)            AS occurrences,
                       MAX(last_seen)              AS last_seen,
                       MAX(peak_cpu_percent)       AS peak_cpu_percent,
                       MAX(peak_memory_bytes)      AS peak_memory_bytes,
                       MAX(sustained_for_seconds)  AS sustained_for_seconds,
                       command
                FROM per_user_alert
                WHERE {where_clause}
                GROUP BY hostname, username, rule_id, command
                ORDER BY last_seen DESC
                """,
                params,
            ).fetchall()
    except sqlite3.OperationalError:
        # Table doesn't exist (migration v8 not applied). Soft-fail.
        return []

    signals = []
    for r in rows:
        signals.append(PerUserSignal(
            signal_type=SignalType.PER_USER,
            severity=r["severity"],
            hostname=r["hostname"],
            username=r["username"],
            rule_id=r["rule_id"],
            rule_type=r["rule_type"],
            occurrences=int(r["occurrences"] or 1),
            last_seen=r["last_seen"] or "",
            command=r["command"],
            peak_cpu_percent=r["peak_cpu_percent"],
            peak_memory_bytes=r["peak_memory_bytes"],
            sustained_for_seconds=int(r["sustained_for_seconds"] or 0),
        ))
    return signals


def aggregate_cluster_culture_signal(signals, hostname: str):
    """Summarise per-user activity for one host."""
    host_signals = [s for s in signals if s.hostname == hostname]
    if not host_signals:
        return None

    users = _PerUserCounter(s.username for s in host_signals)
    severities = _PerUserCounter(s.severity for s in host_signals)
    rules = _PerUserCounter(s.rule_id for s in host_signals)

    return PerUserSignal(
        signal_type=SignalType.PER_USER,
        severity="informational",
        hostname=hostname,
        username="",
        rule_id="",
        rule_type="",
        occurrences=len(host_signals),
        last_seen=max(s.last_seen for s in host_signals),
        context={
            "kind": "cluster_culture",
            "distinct_users": len(users),
            "user_counts": dict(users),
            "severity_breakdown": dict(severities),
            "top_rules": dict(rules.most_common(3)),
        },
    )

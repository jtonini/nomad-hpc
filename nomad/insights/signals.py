# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Signal readers for the NØMAD Insight Engine.

Each reader queries a specific data source (jobs, disk, GPU, network,
TESSERA, derivatives, alerts) and returns typed Signal objects that
the engine can interpret and narrate.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import logging
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



class Severity(Enum):
    """Signal severity levels."""
    INFO = "info"
    NOTICE = "notice"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Signal:
    """A single operational signal extracted from NØMAD data."""
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


def _get_conn(db_path: Path) -> sqlite3.Connection:
    """Open a read-only connection."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


# ── Job signals ──────────────────────────────────────────────────────────


def create_site_db(db_path: Path, site: str) -> Path:
    """Create a temporary database with only one site's data.
    
    Copies all tables from the source database. Tables with a
    source_site column are filtered to the specified site.
    Tables without source_site are copied in full.
    
    Returns the path to the temporary database.
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


def read_job_signals(db_path: Path, hours: int = 24, config: dict | None = None) -> list[Signal]:
    """Analyze recent job outcomes for failure patterns."""
    signals: list[Signal] = []
    conn = _get_conn(db_path)
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

    try:
        # Overall success rate
        row = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN state = 'COMPLETED' THEN 1 ELSE 0 END) as completed,
                   SUM(CASE WHEN state = 'FAILED' THEN 1 ELSE 0 END) as failed,
                   SUM(CASE WHEN state = 'TIMEOUT' THEN 1 ELSE 0 END) as timed_out,
                   SUM(CASE WHEN state = 'OUT_OF_MEMORY' THEN 1 ELSE 0 END) as oom
            FROM jobs WHERE end_time >= ?
        """, (cutoff,)).fetchone()

        if row and row["total"] > 0:
            total = row["total"]
            failed = row["failed"] or 0
            timed_out = row["timed_out"] or 0
            oom = row["oom"] or 0
            success_rate = ((row["completed"] or 0) / total) * 100

            if success_rate < 80:
                sev = Severity.CRITICAL
            elif success_rate < 90:
                sev = Severity.WARNING
            elif success_rate < 95:
                sev = Severity.NOTICE
            else:
                sev = Severity.INFO

            signals.append(Signal(
                signal_type=SignalType.JOBS,
                severity=sev,
                title="job_success_rate",
                detail=f"{total} jobs in the last {hours}h, {success_rate:.1f}% success rate",
                metrics={
                    "total": total, "success_rate": success_rate,
                    "failed": failed, "timed_out": timed_out, "oom": oom,
                    "hours": hours,
                },
            ))

            # Failure concentration by partition
            partitions = conn.execute("""
                SELECT partition, COUNT(*) as cnt
                FROM jobs
                WHERE end_time >= ? AND state IN ('FAILED', 'TIMEOUT', 'OUT_OF_MEMORY')
                GROUP BY partition ORDER BY cnt DESC LIMIT 3
            """, (cutoff,)).fetchall()

            for p in partitions:
                pct = (p["cnt"] / total) * 100
                if pct > 5:
                    signals.append(Signal(
                        signal_type=SignalType.JOBS,
                        severity=Severity.WARNING if pct > 10 else Severity.NOTICE,
                        title="partition_failure_concentration",
                        detail=f"Partition '{p['partition']}': {p['cnt']} failures ({pct:.1f}% of all jobs)",
                        metrics={"partition": p["partition"], "failures": p["cnt"], "pct": pct},
                        affected_entities=[p["partition"]],
                        tags={"partition": p["partition"]},
                    ))

            # OOM-specific signal
            if oom > 0:
                oom_users = conn.execute("""
                    SELECT user_name as username, COUNT(*) as cnt
                    FROM jobs
                    WHERE end_time >= ? AND state = 'OUT_OF_MEMORY'
                    GROUP BY user_name ORDER BY cnt DESC LIMIT 3
                """, (cutoff,)).fetchall()
                users = [f"{u['username']} ({u['cnt']})" for u in oom_users]
                signals.append(Signal(
                    signal_type=SignalType.MEMORY,
                    severity=Severity.WARNING if oom > 3 else Severity.NOTICE,
                    title="oom_failures",
                    detail=f"{oom} out-of-memory failures in the last {hours}h",
                    metrics={"oom_count": oom, "top_users": users},
                    affected_entities=[u["username"] for u in oom_users],
                ))

            # Timeout signal
            if timed_out > 2:
                signals.append(Signal(
                    signal_type=SignalType.JOBS,
                    severity=Severity.WARNING if timed_out > 5 else Severity.NOTICE,
                    title="timeout_failures",
                    detail=f"{timed_out} jobs timed out in the last {hours}h",
                    metrics={"timeout_count": timed_out, "hours": hours},
                ))

        # Compare to previous period
        prev_cutoff = (datetime.now() - timedelta(hours=hours * 2)).isoformat()
        prev = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN state = 'COMPLETED' THEN 1 ELSE 0 END) as completed
            FROM jobs WHERE end_time >= ? AND end_time < ?
        """, (prev_cutoff, cutoff)).fetchone()

        if prev and prev["total"] > 10 and row and row["total"] > 10:
            prev_rate = ((prev["completed"] or 0) / prev["total"]) * 100
            curr_rate = success_rate
            delta = curr_rate - prev_rate
            if abs(delta) > 3:
                direction = "improving" if delta > 0 else "degrading"
                signals.append(Signal(
                    signal_type=SignalType.JOBS,
                    severity=Severity.INFO if delta > 0 else Severity.WARNING,
                    title="job_rate_trend",
                    detail=f"Success rate {direction}: {prev_rate:.1f}% -> {curr_rate:.1f}% ({delta:+.1f}pp)",
                    metrics={"previous_rate": prev_rate, "current_rate": curr_rate, "delta": delta},
                ))

    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()

    return signals


# ── Disk / storage signals ───────────────────────────────────────────────

def read_disk_signals(db_path: Path, hours: int = 6, config: dict | None = None) -> list[Signal]:
    """Check storage filesystem trends."""
    signals: list[Signal] = []
    conn = _get_conn(db_path)
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

    # Try filesystems table first (from DiskCollector)
    try:
        fs_rows = conn.execute("""
            SELECT f.path, f.used_percent,
                   (f.total_bytes/1073741824.0) as total_gb,
                   (f.used_bytes/1073741824.0) as used_gb,
                   (f.available_bytes/1073741824.0) as free_gb,
                   f.days_until_full,
                   f.first_derivative,
                   f.timestamp,
                   f.source_site
            FROM filesystems f
            INNER JOIN (
                SELECT path,
                       COALESCE(source_site, 'local') as ss,
                       MAX(timestamp) as max_ts
                FROM filesystems
                GROUP BY path, COALESCE(source_site, 'local')
            ) latest ON f.path = latest.path
                AND f.timestamp = latest.max_ts
                AND COALESCE(f.source_site, 'local') = latest.ss
        """).fetchall()
    except Exception:
        try:
            fs_rows = conn.execute("""
                SELECT f.path, f.used_percent,
                    (f.total_bytes/1073741824.0) as total_gb,
                    (f.used_bytes/1073741824.0) as used_gb,
                    (f.available_bytes/1073741824.0) as free_gb,
                    f.days_until_full,
                    f.first_derivative,
                    f.timestamp,
                    NULL as source_site
                FROM filesystems f
                INNER JOIN (
                    SELECT path, MAX(timestamp) as max_ts
                    FROM filesystems GROUP BY path
                ) latest ON f.path = latest.path
                    AND f.timestamp = latest.max_ts
            """).fetchall()
        except Exception:
            fs_rows = []

    for r in fs_rows:
        usage = r['used_percent']
        site = r['source_site'] or 'local'
        label = f"{site}:{r['path']}"
        if usage >= 90:
            sev = Severity.CRITICAL
        elif usage >= 80:
            sev = Severity.WARNING
        elif usage >= 70:
            sev = Severity.NOTICE
        else:
            continue

        detail = (f"{label} at {usage:.0f}% "
                  f"({r['free_gb']:.1f} GB free)")

        # Add fill rate info if available
        if r['days_until_full'] and r['days_until_full'] > 0:
            detail += (f". Projected full in "
                       f"{r['days_until_full']:.0f} days.")
        if r['first_derivative'] and r['first_derivative'] > 0:
            gb_per_day = (r['first_derivative']
                          * 86400 / 1073741824.0)
            if gb_per_day > 0.1:
                detail += (f" Fill rate: "
                           f"{gb_per_day:.1f} GB/day.")

        signals.append(Signal(
            signal_type=SignalType.DISK,
            severity=sev,
            title='filesystem_usage',
            detail=detail,
            metrics={
                'server': label,
                'usage_pct': usage,
                'avail_gb': r['free_gb'],
                'free_gb': r['free_gb'],
                'total_gb': r['total_gb'],
            },
            affected_entities=[label],
            tags={'server': site, 'path': r['path']},
        ))

    try:
        # Also check storage_state (ZFS/NAS)
        rows = conn.execute("""
            SELECT s1.hostname, s1.usage_percent,
                   (s1.total_bytes/1073741824.0) as total_gb,
                   (s1.used_bytes/1073741824.0) as used_gb,
                   (s1.free_bytes/1073741824.0) as free_gb,
                   s1.timestamp
            FROM storage_state s1
            INNER JOIN (
                SELECT hostname, MAX(timestamp) as max_ts
                FROM storage_state GROUP BY hostname
            ) s2 ON s1.hostname = s2.hostname
                 
                 AND s1.timestamp = s2.max_ts
        """).fetchall()

        for r in rows:
            usage = r["usage_percent"]
            if usage >= 90:
                sev = Severity.CRITICAL
            elif usage >= 80:
                sev = Severity.WARNING
            elif usage >= 70:
                sev = Severity.NOTICE
            else:
                continue  # Only signal notable usage

            signals.append(Signal(
                signal_type=SignalType.DISK,
                severity=sev,
                title="filesystem_usage",
                detail=f"{r['hostname']} at {usage:.0f}% ({r['free_gb']:.1f} GB free)",
                metrics={
                    "server": r["hostname"], 
                    "usage_pct": usage, "avail_gb": r["free_gb"], "free_gb": r["free_gb"],
                    "total_gb": r["total_gb"],
                },
                affected_entities=[r["hostname"]],
                tags={"server": r["hostname"]},
            ))

        # Fill rate estimation (needs 2+ data points)
        for r in rows:
            history = conn.execute("""
                SELECT timestamp, usage_percent as usage_pct FROM storage_state
                WHERE server_name = ? AND filesystem = ?
                  AND timestamp >= ?
                ORDER BY timestamp
            """, (r["hostname"], cutoff)).fetchall()

            if len(history) >= 2:
                first = history[0]
                last = history[-1]
                try:
                    t0 = datetime.fromisoformat(first["timestamp"])
                    t1 = datetime.fromisoformat(last["timestamp"])
                    dt_hours = (t1 - t0).total_seconds() / 3600
                    if dt_hours > 0:
                        rate_pct_per_hour = (last["usage_pct"] - first["usage_pct"]) / dt_hours
                        if rate_pct_per_hour > 0.5:  # Growing meaningfully
                            remaining_pct = 100 - last["usage_pct"]
                            hours_to_full = remaining_pct / rate_pct_per_hour if rate_pct_per_hour > 0 else float('inf')
                            if hours_to_full < 48:
                                total_gb = r["total_gb"] or 1
                                fill_rate_gb = (rate_pct_per_hour / 100) * total_gb
                                signals.append(Signal(
                                    signal_type=SignalType.DISK,
                                    severity=Severity.CRITICAL if hours_to_full < 12 else Severity.WARNING,
                                    title="disk_fill_projection",
                                    detail=f"{r['server_name']}:{r.get('storage_type', '')} filling at {fill_rate_gb:.1f} GB/hr, projected full in {hours_to_full:.0f}h",
                                    metrics={
                                        "server": r["hostname"], "hostname": r["hostname"],
                                        "fill_rate_gb_hr": fill_rate_gb,
                                        "hours_to_full": hours_to_full,
                                    },
                                    affected_entities=[r["hostname"]],
                                    tags={"server": r["hostname"]},
                                ))
                except (ValueError, TypeError):
                    pass

    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()

    return signals


# ── GPU signals ──────────────────────────────────────────────────────────

def read_gpu_signals(db_path: Path, hours: int = 24, config: dict | None = None) -> list[Signal]:
    """Analyze GPU utilization and memory patterns."""
    signals: list[Signal] = []
    conn = _get_conn(db_path)
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

    try:
        # GPU utilization from jobs requesting GPUs
        gpu_jobs = conn.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN state = 'FAILED' THEN 1 ELSE 0 END) as failed,
                   SUM(CASE WHEN state = 'OUT_OF_MEMORY' THEN 1 ELSE 0 END) as oom,
                   AVG(CAST(req_gpus AS REAL)) as avg_gpus
            FROM jobs
            WHERE end_time >= ? AND req_gpus > 0
        """, (cutoff,)).fetchone()

        if gpu_jobs and gpu_jobs["total"] > 0:
            total = gpu_jobs["total"]
            failed = gpu_jobs["failed"] or 0
            oom = gpu_jobs["oom"] or 0
            fail_rate = (failed / total) * 100

            if fail_rate > 20:
                signals.append(Signal(
                    signal_type=SignalType.GPU,
                    severity=Severity.WARNING,
                    title="gpu_job_failure_rate",
                    detail=f"{fail_rate:.0f}% of GPU jobs failed in the last {hours}h ({failed}/{total})",
                    metrics={"total_gpu_jobs": total, "failed": failed, "fail_rate": fail_rate},
                ))

            if oom > 0:
                signals.append(Signal(
                    signal_type=SignalType.GPU,
                    severity=Severity.WARNING if oom > 2 else Severity.NOTICE,
                    title="gpu_oom",
                    detail=f"{oom} GPU jobs ran out of memory — possible VRAM limitation",
                    metrics={"gpu_oom_count": oom, "total_gpu_jobs": total},
                ))

    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()

    return signals


# ── Queue signals ────────────────────────────────────────────────────────

def read_queue_signals(db_path: Path, hours: int = 6, config: dict | None = None) -> list[Signal]:
    """Analyze job queue pressure and wait times."""
    signals: list[Signal] = []
    conn = _get_conn(db_path)
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

    try:
        # Queue depth from queue_state table
        latest = conn.execute("""
            SELECT partition, pending_jobs, running_jobs, total_jobs, timestamp
            FROM queue_state
            WHERE timestamp >= ?
            ORDER BY timestamp DESC
        """, (cutoff,)).fetchall()

        # Deduplicate to latest per partition
        seen: dict[str, sqlite3.Row] = {}
        for r in latest:
            if r["partition"] not in seen:
                seen[r["partition"]] = r

        for partition, r in seen.items():
            pending = r["pending_jobs"] or 0
            running = r["running_jobs"] or 0
            if pending > 0 and running > 0:
                ratio = pending / running
                if ratio > 2:
                    signals.append(Signal(
                        signal_type=SignalType.QUEUE,
                        severity=Severity.WARNING if ratio > 5 else Severity.NOTICE,
                        title="queue_pressure",
                        detail=f"Partition '{partition}': {pending} pending vs {running} running (ratio {ratio:.1f}x)",
                        metrics={"partition": partition, "pending": pending, "running": running, "ratio": ratio},
                        affected_entities=[partition],
                        tags={"partition": partition},
                    ))

        # Average wait time from jobs
        wait_rows = conn.execute("""
            SELECT partition, AVG(wait_time_seconds) as avg_wait, MAX(wait_time_seconds) as max_wait
            FROM jobs
            WHERE end_time >= ? AND wait_time_seconds IS NOT NULL
            GROUP BY partition
        """, (cutoff,)).fetchall()

        for w in wait_rows:
            avg_wait = w["avg_wait"] or 0
            max_wait = w["max_wait"] or 0
            if avg_wait > 3600:  # > 1 hour average wait
                signals.append(Signal(
                    signal_type=SignalType.QUEUE,
                    severity=Severity.WARNING if avg_wait > 7200 else Severity.NOTICE,
                    title="high_wait_time",
                    detail=f"Partition '{w['partition']}': average wait {avg_wait/3600:.1f}h (max {max_wait/3600:.1f}h)",
                    metrics={
                        "partition": w["partition"],
                        "avg_wait_sec": avg_wait, "max_wait_sec": max_wait,
                    },
                    affected_entities=[w["partition"]],
                    tags={"partition": w["partition"]},
                ))

    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()

    return signals


# ── Network signals ──────────────────────────────────────────────────────

def read_network_signals(db_path: Path, hours: int = 6, config: dict | None = None) -> list[Signal]:
    """Check for network anomalies."""
    signals: list[Signal] = []
    conn = _get_conn(db_path)
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

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

    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()

    return signals


# ── Alert signals ────────────────────────────────────────────────────────

def read_alert_signals(db_path: Path, hours: int = 24, config: dict | None = None) -> list[Signal]:
    """Read active/recent alerts from the alerts table."""
    signals: list[Signal] = []
    conn = _get_conn(db_path)
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

    try:
        rows = conn.execute("""
            SELECT severity, source as metric, host, message,
                   details, timestamp, resolved
            FROM alerts
            WHERE timestamp >= ?
            ORDER BY timestamp DESC
        """, (cutoff,)).fetchall()

        unresolved = [r for r in rows if not r["resolved"]]
        resolved = [r for r in rows if r["resolved"]]

        if unresolved:
            # Group by severity
            crit = [r for r in unresolved if r["severity"] == "critical"]
            warn = [r for r in unresolved if r["severity"] == "warning"]

            # Build detail with actual alert messages
            alert_msgs = []
            for a in unresolved:
                alert_msgs.append(a["message"])

            detail = f"{len(unresolved)} active alert(s)"
            if alert_msgs:
                detail += ": " + "; ".join(
                    alert_msgs[:5])  # Limit to 5
                if len(alert_msgs) > 5:
                    detail += f" (+{len(alert_msgs)-5} more)"

            signals.append(Signal(
                signal_type=SignalType.ALERT,
                severity=Severity.CRITICAL if crit else Severity.WARNING,
                title="active_alerts",
                detail=detail,
                metrics={
                    "total_active": len(unresolved),
                    "critical": len(crit),
                    "warning": len(warn),
                    "resolved_recently": len(resolved),
                    "messages": alert_msgs[:10],
                },
            ))

        # Flapping detection: alerts that resolved and re-triggered
        metrics_seen: dict[str, int] = {}
        for r in rows:
            m = r["metric"] or "unknown"
            metrics_seen[m] = metrics_seen.get(m, 0) + 1

        flap_threshold = max(3, (hours // 24) * 3)
        for metric, count in metrics_seen.items():
            if count >= flap_threshold:
                signals.append(Signal(
                    signal_type=SignalType.ALERT,
                    severity=Severity.WARNING,
                    title="flapping_alert",
                    detail=f"Alert on '{metric}' triggered {count} times in {hours}h — possible flapping",
                    metrics={"metric": metric, "trigger_count": count},
                    affected_entities=[metric],
                ))

    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()

    return signals


# ── Cloud signals ────────────────────────────────────────────────────────

def read_cloud_signals(db_path: Path, hours: int = 24, config: dict | None = None) -> list[Signal]:
    """Analyze cloud instance metrics for cost and performance signals."""
    signals: list[Signal] = []
    conn = _get_conn(db_path)
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()

    try:
        # Cost trend
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

        # Underutilized instances
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

    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()

    return signals

def read_node_health_signals(
    db_path: Path,
    hours: int = 24,
    config: dict | None = None,
) -> list[Signal]:
    """
    Surface currently unhealthy nodes from the latest node_state samples.

    Independent of the alerts table: queries node_state directly so the
    digest catches node issues even when alert dispatch is unconfigured.

    Configuration (nomad.toml):
        [signals.node_state]
        critical_states = [...]   # substrings -> critical signals
        warning_states  = [...]   # substrings -> warning signals
        ignore_states   = [...]   # never surface these states
        summary_threshold = 3     # multi-node rollup fires above this count
                                  # (0 disables the rollup signal)
    """
    cfg = (config or {}).get('signals', {}).get('node_state', {})
    critical = [x.upper() for x in cfg.get('critical_states',
                ['DOWN', 'NOT_RESPONDING', 'FAIL', 'OFFLINE', 'UNREACH'])]
    warning = [x.upper() for x in cfg.get('warning_states',
                ['DRAIN', 'DRNG', 'DRAINING', 'CLOSED', 'UNAVAIL'])]
    ignore = [x.upper() for x in cfg.get('ignore_states', [])]
    summary_threshold = cfg.get('summary_threshold', 3)

    signals: list[Signal] = []
    conn = _get_conn(db_path)

    try:
        rows = conn.execute("""
            SELECT node_name, state, reason, cluster, partitions, timestamp
            FROM node_state
            WHERE (node_name, timestamp) IN (
                SELECT node_name, MAX(timestamp)
                FROM node_state
                GROUP BY node_name
            )
              AND is_healthy = 0
            ORDER BY node_name
        """).fetchall()

        if not rows:
            return signals

        crit_count = 0
        for r in rows:
            state = (r["state"] or "").upper()
            if any(tok in state for tok in ignore):
                continue

            if any(tok in state for tok in critical):
                sev = Severity.CRITICAL
                title = "node_down"
                crit_count += 1
            elif any(tok in state for tok in warning):
                sev = Severity.WARNING
                title = "node_drain"
            else:
                sev = Severity.WARNING
                title = "node_unhealthy"

            node = r["node_name"]
            cluster = r["cluster"] or "default"
            reason = r["reason"] or "no reason given"
            part = r["partitions"] or ""

            detail = f"Node {node} is {state} on {cluster}"
            if part:
                detail += f" ({part} partition)"
            detail += f"; reason: {reason}"

            signals.append(Signal(
                signal_type=SignalType.ALERT,
                severity=sev,
                title=title,
                detail=detail,
                metrics={
                    "node": node,
                    "state": state,
                    "reason": reason,
                    "cluster": cluster,
                    "partitions": part,
                    "last_seen": r["timestamp"],
                },
            ))

        # Multi-node rollup. Fires when too many nodes are unhealthy at once.
        # Threshold is site-tunable: small clusters set 1-2, large clusters 5-10.
        if summary_threshold > 0 and len(signals) > summary_threshold:
            signals.insert(0, Signal(
                signal_type=SignalType.ALERT,
                severity=Severity.CRITICAL if crit_count else Severity.WARNING,
                title="multiple_nodes_unhealthy",
                detail=(f"{len(signals)} nodes currently unhealthy "
                        f"({crit_count} critical). Cluster capacity reduced."),
                metrics={
                    "total_unhealthy": len(signals),
                    "critical_count": crit_count,
                    "warning_count": len(signals) - crit_count,
                },
            ))
    finally:
        conn.close()

    return signals

# ── Workstation signals ──────────────────────────────────────────────────

def read_workstation_signals(db_path: Path, hours: int = 6, config: dict | None = None) -> list[Signal]:
    """Check workstation health from workstation_state table."""
    signals: list[Signal] = []
    conn = _get_conn(db_path)

    try:
        # Get latest reading per workstation
        rows = conn.execute("""
            SELECT w.hostname, w.department, w.status,
                   w.load_avg_1m, w.cpu_count,
                   w.memory_total_mb, w.memory_used_mb,
                   w.disk_total_gb, w.disk_used_gb,
                   w.disk_usage_pct, w.users_logged_in,
                   w.process_count, w.zombie_count,
                   w.uptime_seconds, w.source_site
            FROM workstation_state w
            INNER JOIN (
                SELECT hostname,
                       COALESCE(source_site, 'local') as ss,
                       MAX(timestamp) as max_ts
                FROM workstation_state
                GROUP BY hostname, COALESCE(source_site, 'local')
            ) latest ON w.hostname = latest.hostname
                AND w.timestamp = latest.max_ts
                AND COALESCE(w.source_site, 'local') = latest.ss
        """).fetchall()

        for r in rows:
            host = r['hostname']
            site = r['source_site'] or 'local'
            label = f"{site}:{host}" if site != 'local' else host

            # High memory usage
            mem_total = r['memory_total_mb'] or 1
            mem_used = r['memory_used_mb'] or 0
            mem_pct = (mem_used / mem_total) * 100
            if mem_pct > 85:
                sev = Severity.WARNING if mem_pct > 95 else Severity.NOTICE
                signals.append(Signal(
                    signal_type=SignalType.MEMORY,
                    severity=sev,
                    title='workstation_high_memory',
                    detail=(
                        f"{label}: memory at {mem_pct:.0f}% "
                        f"({mem_used/1024:.1f}/{mem_total/1024:.1f} GB)"),
                    metrics={'hostname': host, 'mem_pct': mem_pct},
                    affected_entities=[label],
                ))

            # High CPU load
            load = r['load_avg_1m'] or 0
            cpus = r['cpu_count'] or 1
            load_ratio = load / cpus
            if load_ratio > 0.8:
                sev = Severity.WARNING if load_ratio > 1.5 else Severity.NOTICE
                signals.append(Signal(
                    signal_type=SignalType.MEMORY,
                    severity=sev,
                    title='workstation_high_cpu',
                    detail=(
                        f"{label}: load {load:.1f} on "
                        f"{cpus} cores ({load_ratio:.1f}x)"),
                    metrics={'hostname': host, 'load': load, 'cpus': cpus},
                    affected_entities=[label],
                ))

            # High disk usage
            disk_pct = r['disk_usage_pct'] or 0
            if disk_pct > 80:
                sev = Severity.CRITICAL if disk_pct > 95 else (
                    Severity.WARNING if disk_pct > 90 else Severity.NOTICE)
                free_gb = r['disk_free_gb'] or 0
                signals.append(Signal(
                    signal_type=SignalType.DISK,
                    severity=sev,
                    title='workstation_disk_usage',
                    detail=(
                        f"{label}: disk at {disk_pct:.0f}% "
                        f"({free_gb:.0f} GB free)"),
                    metrics={'hostname': host, 'disk_pct': disk_pct},
                    affected_entities=[label],
                ))

            # Zombie processes
            zombies = r['zombie_count'] or 0
            if zombies > 5:
                signals.append(Signal(
                    signal_type=SignalType.MEMORY,
                    severity=Severity.NOTICE,
                    title='workstation_zombies',
                    detail=(
                        f"{label}: {zombies} zombie processes"),
                    metrics={'hostname': host, 'zombies': zombies},
                    affected_entities=[label],
                ))

    except sqlite3.OperationalError:
        pass
    finally:
        conn.close()

    return signals


# ── Master reader ────────────────────────────────────────────────────────


def _read_dynamics_for_site(
        db_path: Path, hours: int, site_label: str = ""
) -> list[Signal]:
    """Run dynamics for a single site/cluster."""
    signals: list[Signal] = []
    prefix = f"{site_label}: " if site_label else ""
    cluster_tag = {"cluster": site_label} if site_label else {}

    try:
        from nomad.dynamics.diversity import compute_diversity
        div = compute_diversity(db_path, dimension="group", hours=hours)
        # Guard: skip diversity signals when data is too sparse to be meaningful
        # H' = 0 means single category (no diversity to assess)
        # 'ungrouped' as dominant means SLURM accounts not yet populated
        total_jobs = sum(div.current.counts.values()) if hasattr(div.current, 'counts') and div.current.counts else 0
        skip_diversity = (
            div.current.shannon_h == 0
            or total_jobs < 20
            or div.current.dominant_category in ('ungrouped', 'unknown', None)
        )

        # Fragility warning
        if div.fragility_warning and not skip_diversity:
            signals.append(Signal(
                signal_type=SignalType.DYNAMICS,
                severity=Severity.NOTICE,
                title="diversity_fragility",
                detail=prefix + div.fragility_detail,
                metrics={
                    **cluster_tag,
                    "shannon_h": div.current.shannon_h,
                    "dominant": div.current.dominant_category,
                    "dominant_proportion": div.current.dominant_proportion,
                    "trend": div.trend_direction,
                },
            ))

        # Diversity declining (only if there's actual diversity to lose)
        if (div.trend_direction == "decreasing"
                and abs(div.trend_slope) > 0.01
                and not skip_diversity
                and div.current.shannon_h > 0.1):
            signals.append(Signal(
                signal_type=SignalType.DYNAMICS,
                severity=Severity.NOTICE,
                title="diversity_declining",
                detail=(
                    f"{prefix}Workload diversity is declining "
                    f"(slope: {div.trend_slope:.4f}/window). "
                    f"Current H'={div.current.shannon_h:.3f}."
                ),
                metrics={
                    **cluster_tag,
                    "shannon_h": div.current.shannon_h,
                    "slope": div.trend_slope,
                },
            ))
    except Exception:
        pass

    try:
        from nomad.dynamics.capacity import compute_capacity
        cap = compute_capacity(db_path, hours=hours)

        if cap.binding_constraint and cap.binding_constraint.current_utilization >= 0.05:
            bc = cap.binding_constraint
            sev = Severity.CRITICAL if bc.current_utilization >= 0.9 else \
                  Severity.WARNING if bc.current_utilization >= 0.75 else \
                  Severity.NOTICE

            signals.append(Signal(
                signal_type=SignalType.DYNAMICS,
                severity=sev,
                title="capacity_binding_constraint",
                detail=(
                    f"{prefix}{bc.label} is the binding constraint at "
                    f"{bc.current_utilization:.0%} utilization."
                ),
                metrics={
                    "dimension": bc.dimension,
                    "label": bc.label,
                    "utilization": bc.current_utilization,
                    "pressure": cap.overall_pressure,
                    "hours_to_saturation": bc.hours_to_saturation,
                },
            ))

            # Saturation warning
            if bc.hours_to_saturation and bc.hours_to_saturation < 48:
                signals.append(Signal(
                    signal_type=SignalType.DYNAMICS,
                    severity=Severity.CRITICAL,
                    title="capacity_saturation_imminent",
                    detail=(
                        f"{prefix}{bc.label} projected to reach saturation "
                        f"in {bc.hours_to_saturation:.0f} hours "
                        f"at current growth rate."
                    ),
                    metrics={
                        "dimension": bc.dimension,
                        "hours_to_saturation": bc.hours_to_saturation,
                    },
                ))
    except Exception:
        pass

    try:
        from nomad.dynamics.niche import compute_niche_overlap
        niche = compute_niche_overlap(db_path, hours=hours)

        if niche.high_overlap_pairs:
            high_count = len([p for p in niche.high_overlap_pairs if p.contention_risk == "high"])
            if high_count > 0:
                top = niche.high_overlap_pairs[0]
                signals.append(Signal(
                    signal_type=SignalType.DYNAMICS,
                    severity=Severity.WARNING if high_count >= 3 else Severity.NOTICE,
                    title="niche_contention_risk",
                    detail=(
                        f"{prefix}{high_count} high-overlap group pair(s) detected. "
                        f"Highest: {top.group_a} <-> {top.group_b} "
                        f"(O={top.overlap:.2f})."
                    ),
                    metrics={
                        **cluster_tag,
                    "high_overlap_count": high_count,
                        "top_pair_a": top.group_a,
                        "top_pair_b": top.group_b,
                        "top_overlap": top.overlap,
                    },
                ))
    except Exception:
        pass

    try:
        from nomad.dynamics.resilience import compute_resilience
        res = compute_resilience(db_path, hours=max(hours, 720))

        if res.resilience_score < 50:
            signals.append(Signal(
                signal_type=SignalType.DYNAMICS,
                severity=Severity.WARNING,
                title="resilience_low",
                detail=(
                    f"Cluster resilience score is {res.resilience_score:.0f}/100. "
                    f"{res.summary}"
                ),
                metrics={
                    "score": res.resilience_score,
                    "trend": res.resilience_trend,
                    "mean_recovery_hours": res.mean_recovery_hours,
                },
            ))

        if res.resilience_trend == "degrading":
            signals.append(Signal(
                signal_type=SignalType.DYNAMICS,
                severity=Severity.NOTICE,
                title="resilience_degrading",
                detail=(
                    f"Cluster resilience is degrading — recovery times "
                    f"are increasing over time. Score: {res.resilience_score:.0f}/100."
                ),
                metrics={
                    "score": res.resilience_score,
                    "trend": "degrading",
                },
            ))
    except Exception:
        pass

    try:
        from nomad.dynamics.externality import compute_externalities
        ext = compute_externalities(db_path, hours=hours)

        if ext.top_imposers:
            signals.append(Signal(
                signal_type=SignalType.DYNAMICS,
                severity=Severity.NOTICE,
                title="externality_detected",
                detail=(
                    f"{len(ext.edges)} inter-group impact relationship(s). "
                    f"Top imposer(s): {', '.join(ext.top_imposers[:2])}."
                ),
                metrics={
                    "edge_count": len(ext.edges),
                    "top_imposers": ext.top_imposers[:3],
                    "top_receivers": ext.top_receivers[:3],
                },
            ))
    except Exception:
        pass

    return signals

# ── Master reader ────────────────────────────────────────────────────────
def read_dynamics_signals(db_path: Path, hours: int = 168, config: dict | None = None) -> list[Signal]:
    """Read system dynamics metrics per-cluster."""
    # Detect multi-site (combined) database
    conn = _get_conn(db_path)
    sites = []
    try:
        sites = [r[0] for r in conn.execute(
            "SELECT DISTINCT source_site FROM jobs"
            " WHERE source_site IS NOT NULL"
        ).fetchall()]
    except Exception:
        pass
    conn.close()

    if len(sites) > 1:
        # Combined DB: run per-cluster using temp DBs
        import tempfile
        all_signals = []
        for site in sites:
            try:
                tmp_path = create_site_db(db_path, site)
                sigs = _read_dynamics_for_site(
                    tmp_path, hours, site)
                all_signals.extend(sigs)
                tmp_path.unlink(missing_ok=True)
            except Exception:
                try:
                    tmp_path.unlink(missing_ok=True)
                except Exception:
                    pass
        return all_signals
    else:
        # Single-site DB: run directly
        return _read_dynamics_for_site(
            db_path, hours)


def read_all_signals(
    db_path: Path,
    hours: int = 24,
    config: dict | None = None,
) -> list[Signal]:
    """
    Run all signal readers and return combined results.

    If config is None, loads from the standard locations via
    nomad.config.load_config(). Each reader receives the full config
    dict and is responsible for finding its own subsection.
    """
    if config is None:
        from nomad.config import load_config
        try:
            config = load_config()
        except Exception as e:
            logger.debug(f"Could not load config; using defaults: {e}")
            config = {}

    all_signals: list[Signal] = []

    readers = [
        (read_job_signals,         {"hours": hours}),
        (read_disk_signals,        {"hours": min(hours, 12)}),
        (read_gpu_signals,         {"hours": hours}),
        (read_queue_signals,       {"hours": min(hours, 12)}),
        (read_network_signals,     {"hours": min(hours, 12)}),
        (read_alert_signals,       {"hours": hours}),
        (read_node_health_signals, {"hours": hours}),
        (read_cloud_signals,       {"hours": hours}),
        (read_workstation_signals, {"hours": min(hours, 12)}),
        (read_dynamics_signals,    {"hours": hours}),
    ]

    for reader, kwargs in readers:
        try:
            sigs = reader(db_path, config=config, **kwargs)
            all_signals.extend(sigs)
        except Exception as e:
            logger.debug(f"Signal reader {reader.__name__} failed: {e}")

    return all_signals

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
    """Pull recent per_user_alert rows; aggregate by (host, user, rule, cmd)."""
    cutoff = (datetime.now() - timedelta(hours=lookback_hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    where = ["fired_at >= ?"]
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

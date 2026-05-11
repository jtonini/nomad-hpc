# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAD per-user collector (Idea 18 Component 1).

Per-tick lifecycle:
  1. iter_processes() → list of (pid, ProcessInfo, instantaneous_metrics)
  2. For each process:
       - skip whitelisted (record sample with whitelist_match set, no rule eval)
       - observe() into TrackStore
       - rule_engine.evaluate(track)
       - on firing: write to per_user_alert (insert or update occurrences)
  3. fd_walk for compute role (Component 2; gated)
  4. write per_user_sample rows
  5. gc() the track store

The collector deliberately does the rule evaluation BEFORE persisting samples
so an alert and its triggering sample land in the DB in a coherent order.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

try:
    import psutil
except ImportError:                       # pragma: no cover
    psutil = None                          # tests can stub iter_processes directly

from .ancestry import (
    AncestryResult,
    ProcessInfo,
    WhitelistConfig,
    WhitelistMatch,
    match_whitelist,
    walk_ancestry,
)
from .privileged import (
    DEFAULT_BUCKETS,
    PermissionDenied,
    can_walk_fds_of_other_users,
    walk_fds,
)
from .rules import (
    DEFAULT_RULES,
    Rule,
    RuleEngine,
    RuleFiring,
    Sample,
)
from .state import FiringDedup, TrackStore, make_session_id

logger = logging.getLogger(__name__)

COLLECTOR_VERSION = "1.0.0-component1"
CMDLINE_TRUNCATE = 512


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PerUserConfig:
    """Configuration loaded from [collectors.per_user] in nomad.toml.

    Defaults match the validation findings; cluster-specific overrides come
    through the existing nomad config loading machinery.
    """
    enabled: bool = True
    role: str = "headnode"                 # headnode | monitoring | compute
    sample_interval_seconds: int = 60
    ancestry_depth: int = 8

    rules: tuple[Rule, ...] = DEFAULT_RULES
    whitelist: WhitelistConfig = field(
        default_factory=lambda: WhitelistConfig(
            # /opt/ DROPPED from defaults — see test_ia3nk_gmx_mpi_is_not_whitelisted_by_default.
            # Add per-cluster in nomad.toml if needed.
            parent_paths=("/usr/local/sw/", "/var/spool/cron/"),
            users=("slurm", "munge"),
            min_uid=1000,
        )
    )

    # fd walking: enable per role. compute=True powers Component 2.
    fd_walk_enabled: bool = False
    fd_walk_sample_subset: float = 1.0     # fraction of processes to walk; 1.0 = all


# ---------------------------------------------------------------------------
# What iter_processes returns
# ---------------------------------------------------------------------------

@dataclass
class ProcessSnapshot:
    """One psutil sample for one process. Constructed by the collector;
    consumed internally."""
    info: ProcessInfo
    cpu_percent: float
    memory_rss_bytes: int
    memory_vms_bytes: int
    num_threads: int
    num_fds: int                          # count only; not the walked-result
    started_at: float                      # unix epoch
    cmdline: str                           # truncated


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class PerUserCollector:
    """The main collector.

    Lifecycle is driven by the existing scheduler in nomad/collectors/base.py;
    this class implements the standard collect()/store() interface, plus
    holds the persistent in-memory state across ticks.
    """

    name = "per_user"
    description = "Per-user CPU/memory tracking with whitelist-aware alerting"
    default_interval = 60

    def __init__(
        self,
        config: PerUserConfig,
        db_path: str,
        hostname: str | None = None,
    ) -> None:
        self.config = config
        self.db_path = db_path
        self.hostname = hostname or _detect_hostname()
        self.engine = RuleEngine(rules=config.rules)
        self.tracks = TrackStore(
            hostname=self.hostname,
            max_window_seconds=self.engine.max_window_seconds,
        )
        self.dedup = FiringDedup()

        # Capability detection happens once at startup
        self._can_walk_fds = (
            self.config.fd_walk_enabled and can_walk_fds_of_other_users()
        )
        if self.config.fd_walk_enabled and not self._can_walk_fds:
            logger.warning(
                "per_user: fd_walk_enabled=True but lacking privilege; falling back "
                "to no fd walking. Run as root via systemd to enable Component 2."
            )

        # Internal pid->cpu_percent_baseline cache for psutil. psutil's
        # cpu_percent returns 0 on first call per process; we keep state
        # across ticks so subsequent calls return real values.
        self._psutil_seen: set[int] = set()

    # -----------------------------------------------------------------------
    # Public BaseCollector interface
    # -----------------------------------------------------------------------

    def collect(self) -> dict[str, Any]:
        """One tick. Returns a dict with sample/alert/fd-row counts for the
        scheduler's logging. All persistence happens here, not in store()."""
        if not self.config.enabled:
            return {"enabled": False}

        t0 = time.time()
        snapshots = list(self.iter_processes())
        live_session_ids: set[str] = set()
        sample_rows: list[dict[str, Any]] = []
        alert_rows: list[dict[str, Any]] = []
        fd_rows: list[dict[str, Any]] = []

        for snap in snapshots:
            session_id = make_session_id(self.hostname, snap.info.pid, snap.started_at)
            live_session_ids.add(session_id)

            ancestry = walk_ancestry(
                pid=snap.info.pid,
                lookup=self._lookup_for_ancestry(snapshots),
                max_depth=self.config.ancestry_depth,
            )
            wmatch = match_whitelist(snap.info, ancestry, self.config.whitelist)

            # Build sample row
            sample_rows.append(_build_sample_row(
                hostname=self.hostname,
                role=self.config.role,
                snap=snap,
                session_id=session_id,
                ancestry=ancestry,
                whitelist_match=wmatch,
            ))

            if wmatch is not None:
                # Whitelisted: record sample, don't run rules
                continue

            # Observe + evaluate
            sample = Sample(
                timestamp=t0,
                cpu_percent=snap.cpu_percent,
                memory_rss_bytes=snap.memory_rss_bytes,
            )
            track = self.tracks.observe(
                pid=snap.info.pid,
                start_time=snap.started_at,
                username=snap.info.username,
                uid=snap.info.uid,
                command=snap.info.command,
                sample=sample,
            )
            firings = self.engine.evaluate(track, now=t0)
            for f in firings:
                alert_rows.append(_build_alert_row(
                    hostname=self.hostname,
                    role=self.config.role,
                    snap=snap,
                    firing=f,
                    ancestry=ancestry,
                ))

            # fd walk (compute role only, gated by capability)
            if self._can_walk_fds and self.config.role == "compute":
                fd_rows.extend(self._fd_walk_one(snap, session_id, t0))

        # Persist
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            self._persist_samples(conn, sample_rows)
            self._persist_alerts(conn, alert_rows)
            if fd_rows:
                self._persist_fd_samples(conn, fd_rows)

        # GC
        evicted = self.tracks.gc(now=t0, live_session_ids=live_session_ids)

        elapsed = time.time() - t0
        return {
            "samples": len(sample_rows),
            "alerts_fired": len(alert_rows),
            "fd_rows": len(fd_rows),
            "tracks_active": len(self.tracks),
            "tracks_evicted": evicted,
            "elapsed_seconds": elapsed,
        }

    def store(self, data: Any) -> None:                # pragma: no cover
        """No-op: collect() persists directly. The BaseCollector contract
        expects store() to exist; we satisfy it without doing extra work."""
        return

    # -----------------------------------------------------------------------
    # Iteration over psutil — overridable for testing
    # -----------------------------------------------------------------------

    def iter_processes(self) -> Iterable[ProcessSnapshot]:
        """Yield one ProcessSnapshot per visible process.

        Subclasses / tests can override this to inject synthetic data without
        touching psutil.
        """
        if psutil is None:
            return
        attrs = ["pid", "ppid", "uids", "username", "name", "exe",
                 "memory_info", "num_threads", "num_fds", "create_time", "cmdline"]
        for proc in psutil.process_iter(attrs=attrs, ad_value=None):
            try:
                info = proc.info
                pid = info["pid"]

                # cpu_percent: first call returns 0; subsequent calls return real values
                # We accept the 0 on first sight — the rule engine needs >1 sample anyway.
                if pid in self._psutil_seen:
                    cpu = proc.cpu_percent(interval=None)
                else:
                    proc.cpu_percent(interval=None)        # prime
                    self._psutil_seen.add(pid)
                    cpu = 0.0

                mem = info.get("memory_info")
                if mem is None:
                    continue

                uids = info.get("uids")
                uid = uids.real if uids is not None else -1

                cmdline_list = info.get("cmdline") or []
                cmdline = " ".join(cmdline_list)[:CMDLINE_TRUNCATE]

                yield ProcessSnapshot(
                    info=ProcessInfo(
                        pid=pid,
                        ppid=info.get("ppid"),
                        uid=uid,
                        username=info.get("username") or "",
                        command=info.get("name") or "",
                        exe_path=info.get("exe"),
                    ),
                    cpu_percent=cpu,
                    memory_rss_bytes=mem.rss,
                    memory_vms_bytes=mem.vms,
                    num_threads=info.get("num_threads") or 0,
                    num_fds=info.get("num_fds") or 0,
                    started_at=info.get("create_time") or 0.0,
                    cmdline=cmdline,
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as e:                       # pragma: no cover
                logger.debug("iter_processes: skipping %s: %s", proc, e)
                continue

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _lookup_for_ancestry(self, snapshots: list[ProcessSnapshot]):
        """Build a pid -> ProcessInfo lookup from the current snapshot batch."""
        by_pid = {s.info.pid: s.info for s in snapshots}
        return by_pid.get

    def _fd_walk_one(
        self, snap: ProcessSnapshot, session_id: str, t0: float,
    ) -> list[dict[str, Any]]:
        """Walk fds for one process, build per-bucket rows. Soft-fails on
        permission errors (logged once per cycle by the collector)."""
        try:
            walk = walk_fds(snap.info.pid)
        except PermissionDenied:
            return []
        except Exception as e:                            # pragma: no cover
            logger.debug("fd walk failed for pid=%s: %s", snap.info.pid, e)
            return []

        ts = _utc_iso(t0)
        rows = []
        for bucket, count in walk.bucket_counts.items():
            rows.append({
                "timestamp": ts,
                "hostname": self.hostname,
                "username": snap.info.username,
                "uid": snap.info.uid,
                "pid": snap.info.pid,
                "process_session_id": session_id,
                "fs_bucket": bucket,
                "fd_count": count,
                "representative_path": walk.representative_paths.get(bucket),
            })
        return rows

    # -----------------------------------------------------------------------
    # Persistence
    # -----------------------------------------------------------------------

    def _persist_samples(self, conn: sqlite3.Connection, rows: list[dict]) -> None:
        if not rows:
            return
        conn.executemany(
            """
            INSERT INTO per_user_sample (
                timestamp, hostname, role, username, uid, pid, process_session_id,
                command, cmdline, exe_path,
                cpu_percent, memory_rss_bytes, memory_vms_bytes,
                num_threads, num_fds, started_at, elapsed_seconds,
                ancestry_chain, whitelist_match,
                collector_version, source
            ) VALUES (
                :timestamp, :hostname, :role, :username, :uid, :pid, :process_session_id,
                :command, :cmdline, :exe_path,
                :cpu_percent, :memory_rss_bytes, :memory_vms_bytes,
                :num_threads, :num_fds, :started_at, :elapsed_seconds,
                :ancestry_chain, :whitelist_match,
                :collector_version, :source
            )
            """,
            rows,
        )

    def _persist_alerts(self, conn: sqlite3.Connection, rows: list[dict]) -> None:
        """Insert new alerts; bump occurrences on duplicates.

        We use INSERT ... ON CONFLICT(dedup_key) DO UPDATE so the SQL handles
        dedup atomically. The in-memory FiringDedup is just a hint to avoid
        the round-trip on hot keys.
        """
        if not rows:
            return
        conn.executemany(
            """
            INSERT INTO per_user_alert (
                fired_at, hostname, role, username, uid, pid, process_session_id,
                rule_id, rule_type, severity, threshold_value, threshold_unit,
                sustained_for_seconds, command, cmdline, ancestry_chain,
                peak_cpu_percent, peak_memory_bytes, dedup_key, occurrences,
                last_seen, edu_template_id
            ) VALUES (
                :fired_at, :hostname, :role, :username, :uid, :pid, :process_session_id,
                :rule_id, :rule_type, :severity, :threshold_value, :threshold_unit,
                :sustained_for_seconds, :command, :cmdline, :ancestry_chain,
                :peak_cpu_percent, :peak_memory_bytes, :dedup_key, 1,
                :last_seen, :edu_template_id
            )
            ON CONFLICT(dedup_key) DO UPDATE SET
                occurrences = occurrences + 1,
                last_seen = excluded.last_seen,
                peak_cpu_percent = MAX(peak_cpu_percent, excluded.peak_cpu_percent),
                peak_memory_bytes = MAX(peak_memory_bytes, excluded.peak_memory_bytes)
            """,
            rows,
        )
        for r in rows:
            self.dedup.record(r["dedup_key"], r["fired_at_ts"]) if "fired_at_ts" in r else None

    def _persist_fd_samples(self, conn: sqlite3.Connection, rows: list[dict]) -> None:
        conn.executemany(
            """
            INSERT INTO per_user_fd_sample (
                timestamp, hostname, username, uid, pid, process_session_id,
                fs_bucket, fd_count, representative_path
            ) VALUES (
                :timestamp, :hostname, :username, :uid, :pid, :process_session_id,
                :fs_bucket, :fd_count, :representative_path
            )
            """,
            rows,
        )


# ---------------------------------------------------------------------------
# Row builders (free functions, easy to test)
# ---------------------------------------------------------------------------

def _build_sample_row(
    *,
    hostname: str,
    role: str,
    snap: ProcessSnapshot,
    session_id: str,
    ancestry: AncestryResult,
    whitelist_match: WhitelistMatch | None,
) -> dict[str, Any]:
    now_unix = time.time()
    return {
        "timestamp": _utc_iso(now_unix),
        "hostname": hostname,
        "role": role,
        "username": snap.info.username,
        "uid": snap.info.uid,
        "pid": snap.info.pid,
        "process_session_id": session_id,
        "command": snap.info.command,
        "cmdline": snap.cmdline,
        "exe_path": snap.info.exe_path,
        "cpu_percent": snap.cpu_percent,
        "memory_rss_bytes": snap.memory_rss_bytes,
        "memory_vms_bytes": snap.memory_vms_bytes,
        "num_threads": snap.num_threads,
        "num_fds": snap.num_fds,
        "started_at": _utc_iso(snap.started_at) if snap.started_at else None,
        "elapsed_seconds": (now_unix - snap.started_at) if snap.started_at else None,
        "ancestry_chain": json.dumps(ancestry.chain),
        "whitelist_match": (
            f"{whitelist_match.reason}:{whitelist_match.detail}"
            if whitelist_match else None
        ),
        "collector_version": COLLECTOR_VERSION,
        "source": "psutil",
    }


def _build_alert_row(
    *,
    hostname: str,
    role: str,
    snap: ProcessSnapshot,
    firing: RuleFiring,
    ancestry: AncestryResult,
) -> dict[str, Any]:
    dedup_key = FiringDedup.make_key(hostname, firing.track.process_session_id, firing.rule.rule_id)
    fired_iso = _utc_iso(firing.fired_at)
    return {
        "fired_at": fired_iso,
        "fired_at_ts": firing.fired_at,
        "hostname": hostname,
        "role": role,
        "username": firing.track.username,
        "uid": firing.track.uid,
        "pid": firing.track.pid,
        "process_session_id": firing.track.process_session_id,
        "rule_id": firing.rule.rule_id,
        "rule_type": firing.rule.rule_type,
        "severity": firing.rule.severity,
        "threshold_value": firing.rule.threshold_value,
        "threshold_unit": firing.rule.threshold_unit,
        "sustained_for_seconds": firing.sustained_for_seconds,
        "command": firing.track.command,
        "cmdline": snap.cmdline,
        "ancestry_chain": json.dumps(ancestry.chain),
        "peak_cpu_percent": firing.peak_cpu_percent,
        "peak_memory_bytes": firing.peak_memory_bytes,
        "dedup_key": dedup_key,
        "last_seen": fired_iso,
        "edu_template_id": firing.rule.edu_template_id,
    }


# ---------------------------------------------------------------------------
# Tiny utilities
# ---------------------------------------------------------------------------

def _utc_iso(unix_ts: float) -> str:
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _detect_hostname() -> str:
    import socket
    return socket.gethostname()

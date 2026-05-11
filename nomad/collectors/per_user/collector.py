# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAD per-user collector (Idea 18 Component 1).

Inherits from BaseCollector. Lifecycle:
  - The framework calls run(), which calls collect() then store(data).
  - collect() returns an envelope (single dict in a list) containing samples,
    alerts, and fd_rows. The DB is not touched here.
  - store(data) writes the envelope's contents in one transaction.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    import psutil
except ImportError:                       # pragma: no cover
    psutil = None

from ..base import BaseCollector, registry
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


@dataclass(frozen=True)
class PerUserConfig:
    """Internal config. Built from the toml dict via from_dict()."""
    enabled: bool = True
    role: str = "headnode"
    sample_interval_seconds: int = 60
    ancestry_depth: int = 8
    rules: tuple = DEFAULT_RULES
    whitelist: WhitelistConfig = field(
        default_factory=lambda: WhitelistConfig(
            parent_paths=("/usr/local/sw/", "/var/spool/cron/"),
            users=("slurm", "munge"),
            min_uid=1000,
        )
    )
    fd_walk_enabled: bool = False
    fd_walk_sample_subset: float = 1.0

    @classmethod
    def from_dict(cls, d):
        wl_dict = d.get("whitelist", {}) or {}
        whitelist = WhitelistConfig(
            parent_paths=tuple(wl_dict.get("parent_paths",
                ("/usr/local/sw/", "/var/spool/cron/"))),
            users=tuple(wl_dict.get("users", ("slurm", "munge"))),
            user_commands=tuple(
                tuple(uc) for uc in wl_dict.get("user_commands", [])
            ),
            min_uid=wl_dict.get("min_uid", 1000),
        )
        return cls(
            enabled=d.get("enabled", True),
            role=d.get("role", "headnode"),
            sample_interval_seconds=d.get("sample_interval_seconds", 60),
            ancestry_depth=d.get("ancestry_depth", 8),
            rules=DEFAULT_RULES,
            whitelist=whitelist,
            fd_walk_enabled=d.get("fd_walk_enabled", False),
            fd_walk_sample_subset=d.get("fd_walk_sample_subset", 1.0),
        )


@dataclass
class ProcessSnapshot:
    info: ProcessInfo
    cpu_percent: float
    memory_rss_bytes: int
    memory_vms_bytes: int
    num_threads: int
    num_fds: int
    started_at: float
    cmdline: str


def _envelope(samples, alerts, fd_rows, evicted=0):
    return [{
        "_kind": "per_user_envelope",
        "samples": samples,
        "alerts": alerts,
        "fd_rows": fd_rows,
        "evicted_tracks": evicted,
    }]


@registry.register
class PerUserCollector(BaseCollector):
    """Per-user process tracking on head nodes (Idea 18 Component 1)."""

    name = "per_user"
    description = "Per-user CPU/memory tracking with whitelist-aware alerting"
    default_interval = 60

    def __init__(self, config, db_path):
        super().__init__(config, db_path)
        self.per_user_config = PerUserConfig.from_dict(config)
        self.engine = RuleEngine(rules=self.per_user_config.rules)
        self._hostname = _detect_hostname()
        self.tracks = TrackStore(
            hostname=self._hostname,
            max_window_seconds=self.engine.max_window_seconds,
        )
        self.dedup = FiringDedup()
        self._can_walk_fds = (
            self.per_user_config.fd_walk_enabled and can_walk_fds_of_other_users()
        )
        if self.per_user_config.fd_walk_enabled and not self._can_walk_fds:
            logger.warning(
                "per_user: fd_walk_enabled=True but lacking privilege; "
                "falling back to no fd walking."
            )
        self._psutil_seen = set()
        logger.info(
            "PerUserCollector: role=%s rules=%d",
            self.per_user_config.role,
            len(self.per_user_config.rules),
        )

    def collect(self):
        if not self.per_user_config.enabled:
            return []
        t0 = time.time()
        snapshots = list(self.iter_processes())
        live_session_ids = set()
        sample_rows = []
        alert_rows = []
        fd_rows = []
        by_pid = {s.info.pid: s.info for s in snapshots}

        for snap in snapshots:
            session_id = make_session_id(self._hostname, snap.info.pid, snap.started_at)
            live_session_ids.add(session_id)
            ancestry = walk_ancestry(
                pid=snap.info.pid,
                lookup=by_pid.get,
                max_depth=self.per_user_config.ancestry_depth,
            )
            wmatch = match_whitelist(snap.info, ancestry, self.per_user_config.whitelist)
            sample_rows.append(_build_sample_row(
                hostname=self._hostname,
                role=self.per_user_config.role,
                snap=snap,
                session_id=session_id,
                ancestry=ancestry,
                whitelist_match=wmatch,
            ))
            if wmatch is not None:
                continue
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
                    hostname=self._hostname,
                    role=self.per_user_config.role,
                    snap=snap,
                    firing=f,
                    ancestry=ancestry,
                ))
            if self._can_walk_fds and self.per_user_config.role == "compute":
                fd_rows.extend(self._fd_walk_one(snap, session_id, t0))

        evicted = self.tracks.gc(now=t0, live_session_ids=live_session_ids)
        return _envelope(sample_rows, alert_rows, fd_rows, evicted=evicted)

    def store(self, data):
        if not data:
            return
        env = data[0]
        if env.get("_kind") != "per_user_envelope":
            logger.warning("per_user.store: unexpected data shape, ignoring")
            return
        samples = env.get("samples") or []
        alerts = env.get("alerts") or []
        fd_rows = env.get("fd_rows") or []
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            self._persist_samples(conn, samples)
            self._persist_alerts(conn, alerts)
            if fd_rows:
                self._persist_fd_samples(conn, fd_rows)

    def iter_processes(self):
        if psutil is None:
            return
        attrs = ["pid", "ppid", "uids", "username", "name", "exe",
                 "memory_info", "num_threads", "num_fds", "create_time", "cmdline"]
        for proc in psutil.process_iter(attrs=attrs, ad_value=None):
            try:
                info = proc.info
                pid = info["pid"]
                if pid in self._psutil_seen:
                    cpu = proc.cpu_percent(interval=None)
                else:
                    proc.cpu_percent(interval=None)
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

    def _fd_walk_one(self, snap, session_id, t0):
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
                "hostname": self._hostname,
                "username": snap.info.username,
                "uid": snap.info.uid,
                "pid": snap.info.pid,
                "process_session_id": session_id,
                "fs_bucket": bucket,
                "fd_count": count,
                "representative_path": walk.representative_paths.get(bucket),
            })
        return rows

    def _persist_samples(self, conn, rows):
        if not rows:
            return
        conn.executemany("""
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
        """, rows)

    def _persist_alerts(self, conn, rows):
        if not rows:
            return
        conn.executemany("""
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
        """, rows)

    def _persist_fd_samples(self, conn, rows):
        conn.executemany("""
            INSERT INTO per_user_fd_sample (
                timestamp, hostname, username, uid, pid, process_session_id,
                fs_bucket, fd_count, representative_path
            ) VALUES (
                :timestamp, :hostname, :username, :uid, :pid, :process_session_id,
                :fs_bucket, :fd_count, :representative_path
            )
        """, rows)


def _build_sample_row(*, hostname, role, snap, session_id, ancestry, whitelist_match):
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


def _build_alert_row(*, hostname, role, snap, firing, ancestry):
    dedup_key = FiringDedup.make_key(hostname, firing.track.process_session_id, firing.rule.rule_id)
    fired_iso = _utc_iso(firing.fired_at)
    return {
        "fired_at": fired_iso,
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


def _utc_iso(unix_ts):
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _detect_hostname():
    import socket
    return socket.gethostname()

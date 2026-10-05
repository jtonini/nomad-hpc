# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ Disk Collector

Monitors filesystem usage, quotas, and fill rates.
Integrates with derivative analysis for early warning.
"""

import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .base import BaseCollector, CollectionError, registry

logger = logging.getLogger(__name__)


@dataclass
class FilesystemInfo:
    """Information about a filesystem."""

    path: str
    total_bytes: int
    used_bytes: int
    available_bytes: int
    used_percent: float
    mount_device: str = ""
    filesystem_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            'path': self.path,
            'total_bytes': self.total_bytes,
            'used_bytes': self.used_bytes,
            'available_bytes': self.available_bytes,
            'used_percent': self.used_percent,
            'mount_device': self.mount_device,
            'filesystem_type': self.filesystem_type,
        }


@dataclass
class QuotaInfo:
    """Information about a user/group quota."""

    entity_type: str  # 'user' or 'group'
    entity_name: str
    filesystem_path: str
    used_bytes: int
    limit_bytes: int | None
    used_percent: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            'entity_type': self.entity_type,
            'entity_name': self.entity_name,
            'filesystem_path': self.filesystem_path,
            'used_bytes': self.used_bytes,
            'limit_bytes': self.limit_bytes,
            'used_percent': self.used_percent,
        }


# Fill forecast: the readings of the last few hours, fitted with a straight
# line. Two 5-minute readings (what the old derivative check used) make a
# rate out of noise; a few hours of them make one out of what is happening.
FORECAST_WINDOW_HOURS = 6.0
FORECAST_MIN_READINGS = 4
FORECAST_MIN_SPAN_SECONDS = 3600
# Growth over the window smaller than this share of the filesystem is not a
# trend worth projecting.
FORECAST_MIN_GROWTH = 0.001


def fill_forecast(readings, total_bytes, available_bytes):
    """(rate in bytes per second, hours until full) from [(unix_s, bytes)], the
    bytes rising as the filesystem fills (used, or free space negated).

    Least squares over the readings. The hours are None unless the line
    rises, by at least FORECAST_MIN_GROWTH of the filesystem over the
    readings' span, with free space left; the rate is None without enough
    readings (FORECAST_MIN_READINGS over FORECAST_MIN_SPAN_SECONDS).
    """
    pts = sorted(readings)
    if len(pts) < FORECAST_MIN_READINGS:
        return None, None
    span = pts[-1][0] - pts[0][0]
    if span < FORECAST_MIN_SPAN_SECONDS:
        return None, None
    n = len(pts)
    mt = sum(t for t, _ in pts) / n
    mu = sum(u for _, u in pts) / n
    sxx = sum((t - mt) ** 2 for t, _ in pts)
    if sxx <= 0:
        return None, None
    rate = sum((t - mt) * (u - mu) for t, u in pts) / sxx
    if rate <= 0 or rate * span < FORECAST_MIN_GROWTH * (total_bytes or 0):
        return rate, None
    if not available_bytes or available_bytes <= 0:
        return rate, None                 # already full: the threshold alert says so
    return rate, available_bytes / rate / 3600


def _close(a, b) -> bool:
    try:
        return abs(a - b) <= 0.1 * max(abs(a), abs(b))
    except TypeError:
        return False


def _same_filesystem(total, used, now_total, now_used) -> bool:
    """Another filesystem differs in both size and usage (an unmounted
    /home: df reads the root filesystem beneath). A dataset on a shared pool
    changes size as the pool fills, its usage staying put; a filesystem
    emptied by a cleanup keeps its size."""
    return _close(total, now_total) or _close(used, now_used)


def _parse_time(text):
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return None


@registry.register
class DiskCollector(BaseCollector):
    """
    Collector for filesystem usage and quotas.
    
    Configuration options:
        filesystems: List of paths to monitor ["/", "/home", "/scratch"]
        quota_enabled: Whether to collect quota information (default: False)
        quota_backend: "quota", "lfs", or "custom" (default: "quota")
        quota_command: Custom command for quota (if backend is "custom")
        use_shutil: Use Python shutil instead of df command (default: False)
    
    Collected data:
        - Filesystem total/used/available bytes and percent
        - Per-user and per-group quotas (if enabled)
    """

    name = "disk"
    description = "Filesystem usage and quota monitoring"
    default_interval = 300  # 5 minutes

    def __init__(self, config: dict[str, Any], db_path: Path | str):
        super().__init__(config, db_path)

        self.filesystems = config.get('filesystems', ['/'])
        self.quota_enabled = config.get('quota_enabled', False)
        self.quota_backend = config.get('quota_backend', 'quota')
        self.quota_command = config.get('quota_command', None)
        self.use_shutil = config.get('use_shutil', False)

        logger.info(f"DiskCollector monitoring: {self.filesystems}")

    def collect(self) -> list[dict[str, Any]]:
        """Collect filesystem usage and quota data."""
        data = []

        # Collect filesystem usage
        for fs_path in self.filesystems:
            try:
                fs_info = self._collect_filesystem(fs_path)
                if fs_info:
                    data.append({
                        'type': 'filesystem',
                        **fs_info.to_dict()
                    })
            except Exception as e:
                logger.warning(f"Failed to collect filesystem {fs_path}: {e}")

        # Collect quotas if enabled
        if self.quota_enabled:
            try:
                quotas = self._collect_quotas()
                for quota in quotas:
                    data.append({
                        'type': 'quota',
                        **quota.to_dict()
                    })
            except Exception as e:
                logger.warning(f"Failed to collect quotas: {e}")

        if not data:
            raise CollectionError("No filesystem data collected")

        return data

    def _collect_filesystem(self, path: str) -> FilesystemInfo | None:
        """Collect usage info for a single filesystem."""

        # Check if path exists
        if not Path(path).exists():
            logger.warning(f"Path does not exist: {path}")
            return None

        if self.use_shutil:
            return self._collect_filesystem_shutil(path)
        else:
            return self._collect_filesystem_df(path)

    def _collect_filesystem_shutil(self, path: str) -> FilesystemInfo:
        """Collect filesystem info using Python shutil."""
        usage = shutil.disk_usage(path)

        return FilesystemInfo(
            path=path,
            total_bytes=usage.total,
            used_bytes=usage.used,
            available_bytes=usage.free,
            used_percent=(usage.used / usage.total * 100) if usage.total > 0 else 0,
        )

    def _collect_filesystem_df(self, path: str) -> FilesystemInfo:
        """Collect filesystem info using df command."""
        try:
            # Run df command
            result = subprocess.run(
                ['df', '-B1', '--output=source,fstype,size,used,avail,pcent,target', path],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                raise CollectionError(f"df command failed: {result.stderr}")

            # Parse output (skip header)
            lines = result.stdout.strip().split('\n')
            if len(lines) < 2:
                raise CollectionError(f"Unexpected df output for {path}")

            # Parse the data line
            parts = lines[1].split()
            if len(parts) >= 6:
                device = parts[0]
                fstype = parts[1]
                total = int(parts[2])
                used = int(parts[3])
                avail = int(parts[4])
                percent_str = parts[5].rstrip('%')

                return FilesystemInfo(
                    path=path,
                    total_bytes=total,
                    used_bytes=used,
                    available_bytes=avail,
                    used_percent=float(percent_str),
                    mount_device=device,
                    filesystem_type=fstype,
                )

        except subprocess.TimeoutExpired:
            raise CollectionError(f"df command timed out for {path}")
        except (ValueError, IndexError) as e:
            raise CollectionError(f"Failed to parse df output: {e}")

        # Fallback to shutil
        logger.debug(f"Falling back to shutil for {path}")
        return self._collect_filesystem_shutil(path)

    def _collect_quotas(self) -> list[QuotaInfo]:
        """Collect quota information."""
        if self.quota_backend == 'quota':
            return self._collect_quotas_standard()
        elif self.quota_backend == 'lfs':
            return self._collect_quotas_lustre()
        elif self.quota_backend == 'custom':
            return self._collect_quotas_custom()
        else:
            logger.warning(f"Unknown quota backend: {self.quota_backend}")
            return []

    def _collect_quotas_standard(self) -> list[QuotaInfo]:
        """Collect quotas using standard quota command."""
        quotas = []

        try:
            # Get group quotas
            result = subprocess.run(
                ['quota', '-g', '-w', '-p'],
                capture_output=True,
                text=True,
                timeout=60,
            )

            if result.returncode == 0:
                quotas.extend(self._parse_quota_output(result.stdout, 'group'))

        except subprocess.TimeoutExpired:
            logger.warning("quota command timed out")
        except FileNotFoundError:
            logger.warning("quota command not found")
        except Exception as e:
            logger.warning(f"Failed to run quota command: {e}")

        return quotas

    def _parse_quota_output(self, output: str, entity_type: str) -> list[QuotaInfo]:
        """Parse standard quota command output."""
        quotas = []

        # Quota output format varies, this handles common format:
        # Filesystem  blocks   quota   limit   grace   files   quota   limit   grace

        lines = output.strip().split('\n')
        current_fs = None

        for line in lines:
            # Skip headers
            if 'Filesystem' in line or 'Disk quotas' in line or not line.strip():
                continue

            # Check for filesystem line
            if line.startswith('/') or line.startswith('Disk'):
                parts = line.split()
                if parts:
                    current_fs = parts[0]
                continue

            # Try to parse quota data
            parts = line.split()
            if len(parts) >= 3 and current_fs:
                try:
                    # blocks are in KB
                    used_kb = int(parts[0].rstrip('*'))
                    limit_kb = int(parts[2]) if parts[2] != '0' else None

                    used_bytes = used_kb * 1024
                    limit_bytes = limit_kb * 1024 if limit_kb else None
                    used_percent = (used_bytes / limit_bytes * 100) if limit_bytes else None

                    quotas.append(QuotaInfo(
                        entity_type=entity_type,
                        entity_name="current",  # Would need additional parsing
                        filesystem_path=current_fs,
                        used_bytes=used_bytes,
                        limit_bytes=limit_bytes,
                        used_percent=used_percent,
                    ))
                except (ValueError, IndexError):
                    continue

        return quotas

    def _collect_quotas_lustre(self) -> list[QuotaInfo]:
        """Collect quotas from Lustre filesystem using lfs quota."""
        quotas = []

        for fs_path in self.filesystems:
            try:
                result = subprocess.run(
                    ['lfs', 'quota', '-g', '$(id -gn)', fs_path],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    shell=True,
                )

                if result.returncode == 0:
                    # Parse lfs quota output
                    # Format: Disk quotas for group NAME (gid NNN):
                    #      Filesystem  kbytes   quota   limit   grace   files   quota   limit   grace
                    # ...
                    pass  # TODO: Implement Lustre quota parsing

            except Exception as e:
                logger.warning(f"Failed to get Lustre quota for {fs_path}: {e}")

        return quotas

    def _collect_quotas_custom(self) -> list[QuotaInfo]:
        """Collect quotas using custom command."""
        if not self.quota_command:
            logger.warning("Custom quota backend specified but no command provided")
            return []

        try:
            result = subprocess.run(
                self.quota_command,
                capture_output=True,
                text=True,
                timeout=60,
                shell=True,
            )

            if result.returncode == 0:
                # Expect JSON output from custom command
                import json
                data = json.loads(result.stdout)
                return [
                    QuotaInfo(**item) for item in data
                    if all(k in item for k in ['entity_type', 'entity_name', 'filesystem_path', 'used_bytes'])
                ]

        except Exception as e:
            logger.warning(f"Custom quota command failed: {e}")

        return []

    def store(self, data: list[dict[str, Any]]) -> None:
        """Store collected data in the database.

        Each filesystem reading also gets its fill forecast (from this and
        the readings of the last ``forecast_window_hours``), stored in the
        row's ``fill_rate_bytes_per_day`` and ``days_until_full`` and added
        to the record, where the alert check reads it.
        """
        now = datetime.now()
        timestamp = now.isoformat()
        window = float(self.config.get('forecast_window_hours', FORECAST_WINDOW_HOURS))
        since = (now - timedelta(hours=window)).isoformat()

        with self.get_db_connection() as conn:
            for record in data:
                record_type = record.get('type')

                if record_type == 'filesystem':
                    total = record['total_bytes']
                    readings = []
                    for ts, t_total, t_used, t_avail in conn.execute(
                            "SELECT timestamp, total_bytes, used_bytes, available_bytes "
                            "FROM filesystems WHERE path = ? AND timestamp >= ? "
                            "ORDER BY timestamp", (record['path'], since)):
                        t = _parse_time(ts)
                        # A reading of another filesystem (the path unmounted:
                        # df read the one beneath) is not this one's history.
                        if t is None or not _same_filesystem(
                                t_total, t_used, total, record['used_bytes']):
                            continue
                        readings.append((t.timestamp(), -t_avail))
                    # The decline of free space, not the growth of "used": on
                    # a pool shared by several datasets the free space shrinks
                    # while this one's own usage stays flat.
                    readings.append((now.timestamp(), -record['available_bytes']))
                    rate, hours = fill_forecast(readings, total, record['available_bytes'])
                    record['fill_rate_bytes_per_day'] = None if rate is None else rate * 86400
                    record['hours_until_full'] = hours
                    record['forecast_window_hours'] = (readings[-1][0] - readings[0][0]) / 3600
                    conn.execute(
                        """
                        INSERT INTO filesystems 
                        (path, total_bytes, used_bytes, available_bytes, used_percent, timestamp,
                         fill_rate_bytes_per_day, days_until_full)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record['path'],
                            record['total_bytes'],
                            record['used_bytes'],
                            record['available_bytes'],
                            record['used_percent'],
                            timestamp,
                            record['fill_rate_bytes_per_day'],
                            None if hours is None else hours / 24,
                        )
                    )

                elif record_type == 'quota':
                    conn.execute(
                        """
                        INSERT INTO quotas
                        (filesystem_path, entity_type, entity_name, used_bytes, limit_bytes, used_percent, timestamp)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record['filesystem_path'],
                            record['entity_type'],
                            record['entity_name'],
                            record['used_bytes'],
                            record.get('limit_bytes'),
                            record.get('used_percent'),
                            timestamp,
                        )
                    )

            conn.commit()
            logger.debug(f"Stored {len(data)} disk records")

    def get_latest(self, path: str) -> dict[str, Any] | None:
        """Get the latest filesystem data for a path."""
        with self.get_db_connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM filesystems 
                WHERE path = ? 
                ORDER BY timestamp DESC 
                LIMIT 1
                """,
                (path,)
            ).fetchone()

            if row:
                return dict(row)
        return None

    def get_history(
        self,
        path: str,
        hours: int = 24,
    ) -> list[dict[str, Any]]:
        """Get filesystem history for derivative analysis."""
        with self.get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM filesystems 
                WHERE path = ? 
                  AND timestamp > datetime('now', ?)
                ORDER BY timestamp ASC
                """,
                (path, f'-{hours} hours')
            ).fetchall()

            return [dict(row) for row in rows]

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ IOStat Collector

Collects system-level I/O metrics from iostat.
Captures device utilization, wait times, and throughput.
"""

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .base import BaseCollector, CollectionError, registry, MissingToolError

logger = logging.getLogger(__name__)


@dataclass
class CPUStats:
    """CPU-level I/O statistics."""
    user_percent: float
    system_percent: float
    iowait_percent: float
    idle_percent: float

    def to_dict(self) -> dict[str, Any]:
        return {
            'user_percent': self.user_percent,
            'system_percent': self.system_percent,
            'iowait_percent': self.iowait_percent,
            'idle_percent': self.idle_percent,
        }


@dataclass
class DeviceStats:
    """Per-device I/O statistics."""
    device: str

    # Read metrics
    reads_per_sec: float
    read_kb_per_sec: float
    read_await_ms: float

    # Write metrics
    writes_per_sec: float
    write_kb_per_sec: float
    write_await_ms: float

    # Utilization
    util_percent: float
    queue_length: float

    def to_dict(self) -> dict[str, Any]:
        return {
            'device': self.device,
            'reads_per_sec': self.reads_per_sec,
            'read_kb_per_sec': self.read_kb_per_sec,
            'read_await_ms': self.read_await_ms,
            'writes_per_sec': self.writes_per_sec,
            'write_kb_per_sec': self.write_kb_per_sec,
            'write_await_ms': self.write_await_ms,
            'util_percent': self.util_percent,
            'queue_length': self.queue_length,
        }


@registry.register
class IOStatCollector(BaseCollector):
    """
    Collector for system-level I/O statistics from iostat.
    
    Configuration options:
        devices: List of devices to monitor (default: auto-detect, excluding loops)
        include_cpu: Include CPU iowait stats (default: True)
    
    Collected data:
        - CPU iowait percentage
        - Per-device read/write throughput
        - Per-device latency (await)
        - Device utilization percentage
    """

    name = "iostat"
    description = "System-level I/O statistics"
    default_interval = 60

    def __init__(self, config: dict[str, Any], db_path: str):
        super().__init__(config, db_path)

        self.devices = config.get('devices', None)  # None = auto-detect
        self.include_cpu = config.get('include_cpu', True)
        self.exclude_loops = config.get('exclude_loops', True)

        logger.info(f"IOStatCollector: devices={self.devices or 'auto'}")

    def collect(self) -> list[dict[str, Any]]:
        """Collect I/O statistics from iostat."""

        try:
            # Run iostat with extended stats, single snapshot
            # -x: extended stats, -y: skip first report (since boot), 1 1: 1 sec interval, 1 report
            result = subprocess.run(
                ['iostat', '-x', '-y', '1', '1'],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                raise CollectionError(f"iostat failed: {result.stderr}")

            return self._parse_iostat_output(result.stdout)

        except FileNotFoundError:
            raise MissingToolError("iostat not found - install sysstat package")
        except subprocess.TimeoutExpired:
            raise CollectionError("iostat timed out")

    def _parse_iostat_output(self, output: str) -> list[dict[str, Any]]:
        """Parse iostat output into structured data."""
        records = []
        timestamp = datetime.now()

        lines = output.strip().split('\n')

        cpu_stats = None
        device_stats = []

        parsing_cpu = False
        parsing_devices = False

        for line in lines:
            line = line.strip()

            # Detect CPU section
            if line.startswith('avg-cpu:'):
                parsing_cpu = True
                parsing_devices = False
                continue

            # Detect Device section
            if line.startswith('Device'):
                parsing_cpu = False
                parsing_devices = True
                continue

            # Parse CPU line
            if parsing_cpu and line:
                cpu_stats = self._parse_cpu_line(line)
                parsing_cpu = False
                continue

            # Parse device lines
            if parsing_devices and line:
                device = self._parse_device_line(line)
                if device:
                    # Filter devices
                    if self.exclude_loops and device.device.startswith('loop'):
                        continue
                    if self.devices and device.device not in self.devices:
                        continue
                    device_stats.append(device)

        # Create records
        if cpu_stats and self.include_cpu:
            records.append({
                'type': 'iostat_cpu',
                'timestamp': timestamp.isoformat(),
                **cpu_stats.to_dict()
            })

        for device in device_stats:
            records.append({
                'type': 'iostat_device',
                'timestamp': timestamp.isoformat(),
                **device.to_dict()
            })

        return records

    def _parse_cpu_line(self, line: str) -> CPUStats | None:
        """Parse CPU statistics line."""
        try:
            # Format: %user %nice %system %iowait %steal %idle
            parts = line.split()
            if len(parts) >= 6:
                return CPUStats(
                    user_percent=float(parts[0]),
                    system_percent=float(parts[2]),
                    iowait_percent=float(parts[3]),
                    idle_percent=float(parts[5]),
                )
        except (ValueError, IndexError) as e:
            logger.debug(f"Failed to parse CPU line: {e}")
        return None

    def _parse_device_line(self, line: str) -> DeviceStats | None:
        """Parse device statistics line."""
        try:
            parts = line.split()
            if len(parts) >= 21:
                # Extended iostat format (varies by version, this handles common format)
                # Device r/s rkB/s rrqm/s %rrqm r_await rareq-sz w/s wkB/s wrqm/s %wrqm w_await wareq-sz d/s dkB/s drqm/s %drqm d_await dareq-sz f/s f_await aqu-sz %util
                return DeviceStats(
                    device=parts[0],
                    reads_per_sec=float(parts[1]),
                    read_kb_per_sec=float(parts[2]),
                    read_await_ms=float(parts[5]),
                    writes_per_sec=float(parts[7]),
                    write_kb_per_sec=float(parts[8]),
                    write_await_ms=float(parts[11]),
                    queue_length=float(parts[20]),
                    util_percent=float(parts[21]),
                )
            elif len(parts) >= 14:
                # Simpler format fallback
                return DeviceStats(
                    device=parts[0],
                    reads_per_sec=float(parts[1]),
                    read_kb_per_sec=float(parts[2]),
                    read_await_ms=float(parts[5]) if len(parts) > 5 else 0,
                    writes_per_sec=float(parts[7]) if len(parts) > 7 else 0,
                    write_kb_per_sec=float(parts[8]) if len(parts) > 8 else 0,
                    write_await_ms=float(parts[11]) if len(parts) > 11 else 0,
                    queue_length=float(parts[-2]) if len(parts) > 2 else 0,
                    util_percent=float(parts[-1]),
                )
        except (ValueError, IndexError) as e:
            logger.debug(f"Failed to parse device line '{line}': {e}")
        return None

    def store(self, data: list[dict[str, Any]]) -> None:
        """Store I/O statistics in database."""

        with self.get_db_connection() as conn:
            # Ensure tables exist
            conn.execute("""
                CREATE TABLE IF NOT EXISTS iostat_cpu (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    user_percent REAL,
                    system_percent REAL,
                    iowait_percent REAL,
                    idle_percent REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_iostat_cpu_ts 
                ON iostat_cpu(timestamp)
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS iostat_device (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    device TEXT NOT NULL,
                    reads_per_sec REAL,
                    read_kb_per_sec REAL,
                    read_await_ms REAL,
                    writes_per_sec REAL,
                    write_kb_per_sec REAL,
                    write_await_ms REAL,
                    util_percent REAL,
                    queue_length REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_iostat_device_ts 
                ON iostat_device(timestamp, device)
            """)

            for record in data:
                if record.get('type') == 'iostat_cpu':
                    conn.execute(
                        """
                        INSERT INTO iostat_cpu 
                        (timestamp, user_percent, system_percent, iowait_percent, idle_percent)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            record['timestamp'],
                            record['user_percent'],
                            record['system_percent'],
                            record['iowait_percent'],
                            record['idle_percent'],
                        )
                    )
                elif record.get('type') == 'iostat_device':
                    conn.execute(
                        """
                        INSERT INTO iostat_device
                        (timestamp, device, reads_per_sec, read_kb_per_sec, read_await_ms,
                         writes_per_sec, write_kb_per_sec, write_await_ms, util_percent, queue_length)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record['timestamp'],
                            record['device'],
                            record['reads_per_sec'],
                            record['read_kb_per_sec'],
                            record['read_await_ms'],
                            record['writes_per_sec'],
                            record['write_kb_per_sec'],
                            record['write_await_ms'],
                            record['util_percent'],
                            record['queue_length'],
                        )
                    )

            conn.commit()
            logger.debug(f"Stored {len(data)} iostat records")

    def get_recent_iowait(self, minutes: int = 60) -> list[tuple[datetime, float]]:
        """Get recent iowait percentages."""
        with self.get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT timestamp, iowait_percent
                FROM iostat_cpu
                WHERE timestamp > datetime('now', ?)
                ORDER BY timestamp
                """,
                (f'-{minutes} minutes',)
            ).fetchall()

            return [(datetime.fromisoformat(row[0]), row[1]) for row in rows]

    def get_device_stats(self, device: str, minutes: int = 60) -> list[dict]:
        """Get recent stats for a specific device."""
        with self.get_db_connection() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM iostat_device
                WHERE device = ? AND timestamp > datetime('now', ?)
                ORDER BY timestamp
                """,
                (device, f'-{minutes} minutes')
            ).fetchall()

            return [dict(row) for row in rows]

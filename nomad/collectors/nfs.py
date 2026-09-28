# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ NFS I/O Collector

Collects NFS-specific I/O statistics from nfsiostat.
Critical for detecting NFS bottlenecks in HPC environments.
Gracefully skips if no NFS mounts or nfsiostat not available.
"""

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .base import BaseCollector, registry

logger = logging.getLogger(__name__)


@dataclass
class NFSStats:
    """NFS mount statistics."""
    mount_point: str
    server: str

    # Operations per second
    ops_per_sec: float
    read_ops_per_sec: float
    write_ops_per_sec: float

    # Throughput (KB/s)
    read_kb_per_sec: float
    write_kb_per_sec: float

    # Latency (ms)
    avg_rtt_ms: float       # Round-trip time
    avg_exe_ms: float       # Execution time (includes queue)

    # Retransmissions
    retrans_percent: float

    def to_dict(self) -> dict[str, Any]:
        return {
            'mount_point': self.mount_point,
            'server': self.server,
            'ops_per_sec': self.ops_per_sec,
            'read_ops_per_sec': self.read_ops_per_sec,
            'write_ops_per_sec': self.write_ops_per_sec,
            'read_kb_per_sec': self.read_kb_per_sec,
            'write_kb_per_sec': self.write_kb_per_sec,
            'avg_rtt_ms': self.avg_rtt_ms,
            'avg_exe_ms': self.avg_exe_ms,
            'retrans_percent': self.retrans_percent,
        }


@registry.register
class NFSCollector(BaseCollector):
    """
    Collector for NFS I/O statistics from nfsiostat.
    
    Gracefully skips if:
    - nfsiostat not available
    - No NFS mounts present
    
    Collected data:
        - Operations per second (total, read, write)
        - Throughput (read/write KB/s)
        - Latency (RTT, execution time)
        - Retransmissions
    """

    name = "nfs"
    description = "NFS I/O statistics"
    default_interval = 60

    def __init__(self, config: dict[str, Any], db_path: str):
        super().__init__(config, db_path)

        self._nfs_available = None  # Lazy check
        logger.info("NFSCollector initialized")

    def _check_nfs_available(self) -> bool:
        """Check if nfsiostat is available and NFS mounts exist."""
        if self._nfs_available is not None:
            return self._nfs_available

        # Check for nfsiostat command
        try:
            result = subprocess.run(
                ['which', 'nfsiostat'],
                capture_output=True,
                timeout=5,
            )
            if result.returncode != 0:
                self._nfs_available = False
                logger.info("nfsiostat not found - NFS collector will be skipped")
                return False
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self._nfs_available = False
            return False

        # Check for NFS mounts
        try:
            with open('/proc/mounts') as f:
                mounts = f.read()
                has_nfs = any(t in mounts for t in ['nfs ', 'nfs4 '])
                if not has_nfs:
                    self._nfs_available = False
                    logger.info("No NFS mounts detected - NFS collector will be skipped")
                    return False
        except Exception:
            pass

        self._nfs_available = True
        return True

    def collect(self) -> list[dict[str, Any]]:
        """Collect NFS statistics from nfsiostat."""

        if not self._check_nfs_available():
            return []  # Gracefully return empty

        try:
            # Run nfsiostat with 1 second interval, single report
            result = subprocess.run(
                ['nfsiostat', '1', '1'],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                logger.debug(f"nfsiostat failed: {result.stderr}")
                return []

            return self._parse_nfsiostat_output(result.stdout)

        except subprocess.TimeoutExpired:
            logger.warning("nfsiostat timed out")
            return []
        except Exception as e:
            logger.debug(f"NFS collection failed: {e}")
            return []

    def _parse_nfsiostat_output(self, output: str) -> list[dict[str, Any]]:
        """Parse nfsiostat output."""
        records = []
        timestamp = datetime.now()

        lines = output.strip().split('\n')

        current_mount = None
        current_server = None

        for i, line in enumerate(lines):
            line = line.strip()

            # Detect mount line: "server:/export mounted on /mount/point"
            if ' mounted on ' in line:
                parts = line.split(' mounted on ')
                if len(parts) == 2:
                    current_server = parts[0].strip()
                    current_mount = parts[1].strip().rstrip(':')
                continue

            # Skip headers and empty lines
            if not line or line.startswith('op/s') or 'nfsiostat' in line.lower():
                continue

            # Parse data line for current mount
            if current_mount and line[0].isdigit():
                stats = self._parse_data_line(line, current_mount, current_server)
                if stats:
                    records.append({
                        'type': 'nfs',
                        'timestamp': timestamp.isoformat(),
                        **stats.to_dict()
                    })
                    current_mount = None
                    current_server = None

        return records

    def _parse_data_line(self, line: str, mount: str, server: str) -> NFSStats | None:
        """Parse a nfsiostat data line."""
        try:
            parts = line.split()
            if len(parts) < 8:
                return None

            # Format varies but typically:
            # op/s rpc_bklog read_ops/s read_kB/s write_ops/s write_kB/s ...
            # Later columns may include RTT, exe time, etc.

            return NFSStats(
                mount_point=mount,
                server=server or 'unknown',
                ops_per_sec=float(parts[0]),
                read_ops_per_sec=float(parts[2]) if len(parts) > 2 else 0,
                write_ops_per_sec=float(parts[4]) if len(parts) > 4 else 0,
                read_kb_per_sec=float(parts[3]) if len(parts) > 3 else 0,
                write_kb_per_sec=float(parts[5]) if len(parts) > 5 else 0,
                avg_rtt_ms=float(parts[6]) if len(parts) > 6 else 0,
                avg_exe_ms=float(parts[7]) if len(parts) > 7 else 0,
                retrans_percent=float(parts[8]) if len(parts) > 8 else 0,
            )
        except (ValueError, IndexError) as e:
            logger.debug(f"Failed to parse NFS data line: {e}")
            return None

    def store(self, data: list[dict[str, Any]]) -> None:
        """Store NFS statistics in database."""

        if not data:
            return

        with self.get_db_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS nfs_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    mount_point TEXT NOT NULL,
                    server TEXT,
                    ops_per_sec REAL,
                    read_ops_per_sec REAL,
                    write_ops_per_sec REAL,
                    read_kb_per_sec REAL,
                    write_kb_per_sec REAL,
                    avg_rtt_ms REAL,
                    avg_exe_ms REAL,
                    retrans_percent REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_nfs_stats_ts 
                ON nfs_stats(timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_nfs_stats_mount 
                ON nfs_stats(mount_point, timestamp)
            """)

            for record in data:
                if record.get('type') == 'nfs':
                    conn.execute(
                        """
                        INSERT INTO nfs_stats 
                        (timestamp, mount_point, server, ops_per_sec,
                         read_ops_per_sec, write_ops_per_sec,
                         read_kb_per_sec, write_kb_per_sec,
                         avg_rtt_ms, avg_exe_ms, retrans_percent)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record['timestamp'],
                            record['mount_point'],
                            record['server'],
                            record['ops_per_sec'],
                            record['read_ops_per_sec'],
                            record['write_ops_per_sec'],
                            record['read_kb_per_sec'],
                            record['write_kb_per_sec'],
                            record['avg_rtt_ms'],
                            record['avg_exe_ms'],
                            record['retrans_percent'],
                        )
                    )

            conn.commit()
            logger.debug(f"Stored {len(data)} NFS records")

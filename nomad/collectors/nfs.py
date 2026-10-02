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

from .base import BaseCollector, registry, find_tool

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
    avg_rtt_ms: float | None   # Round-trip time (None: no reads or writes)
    avg_exe_ms: float | None   # Execution time (includes queue)

    # Retransmissions
    retrans_percent: float | None

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


def _numbers(line: str) -> list[float]:
    """Numbers on an nfsiostat value line; "(0.0%)" counts as one."""
    out = []
    for tok in line.split():
        tok = tok.strip('()%')
        try:
            out.append(float(tok))
        except ValueError:
            continue
    return out


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
        # Seconds the interval report covers (the run takes this long).
        self._sample_seconds = int(config.get('sample_seconds', 5))
        logger.info("NFSCollector initialized")

    def _check_nfs_available(self) -> bool:
        """Check if nfsiostat is available and NFS mounts exist."""
        if self._nfs_available is not None:
            return self._nfs_available

        # Check for nfsiostat (in /usr/sbin, outside cron's PATH)
        self._nfsiostat = find_tool('nfsiostat')
        if not self._nfsiostat:
            self._nfs_available = False
            self._nfs_reason = "nfsiostat not installed (nfs-utils)"
            logger.info("nfsiostat not found - NFS collector will be skipped")
            return False

        # Check for NFS mounts
        try:
            with open('/proc/mounts') as f:
                mounts = f.read()
                has_nfs = any(t in mounts for t in ['nfs ', 'nfs4 '])
                if not has_nfs:
                    self._nfs_available = False
                    self._nfs_reason = "no NFS mounts on this host"
                    logger.info("No NFS mounts detected - NFS collector will be skipped")
                    return False
        except Exception:
            pass

        self._nfs_available = True
        return True

    def collect(self) -> list[dict[str, Any]]:
        """Collect NFS statistics from nfsiostat."""

        if not self._check_nfs_available():
            self.note = getattr(self, "_nfs_reason", None) or "NFS not available here"
            return []

        try:
            # Run nfsiostat with 1 second interval, single report
            # `nfsiostat N 2`: the first report averages everything since the
            # share was mounted (months, on a server); the second covers the
            # last N seconds, which is what "now" means. (`1 1` printed only
            # the since-mount averages.)
            result = subprocess.run(
                [self._nfsiostat, str(self._sample_seconds), '2'],
                capture_output=True,
                text=True,
                timeout=self._sample_seconds + 30,
            )

            if result.returncode != 0:
                self.note = f"nfsiostat failed: {' '.join(result.stderr.split())[:100]}"
                return []

            records = self._parse_nfsiostat_output(result.stdout)
            if not records:
                self.note = "nfsiostat printed nothing nomad could read"
            return records

        except subprocess.TimeoutExpired:
            self.note = "nfsiostat timed out"
            return []
        except Exception as e:
            self.note = f"NFS collection failed: {e}"
            return []

    def _parse_nfsiostat_output(self, output: str) -> list[dict[str, Any]]:
        """The last report per mount from nfsiostat (nfs-utils 1.3 and 2.x).

        Each mount prints::

            srv:/export/home mounted on /home:

                       ops/s       rpc bklog
                      19.857           0.000

            read:   ops/s  kB/s  kB/op  retrans  avg RTT (ms)  avg exe (ms)  [avg queue (ms)  errors]
                    2.394  128.582  53.712  0 (0.0%)  1.128  1.163  [0.020  0 (0.0%)]
            write:  (the same columns)

        With two reports, a mount appears twice; the later one wins.
        """
        timestamp = datetime.now().isoformat()
        latest: dict[str, dict] = {}
        mount = server = None
        section = None
        for raw in output.splitlines():
            line = raw.strip()
            if ' mounted on ' in line:
                server, mount = (p.strip() for p in line.split(' mounted on ', 1))
                mount = mount.rstrip(':')
                latest[mount] = {'mount_point': mount, 'server': server}
                section = None
                continue
            if mount is None or not line:
                continue
            if line.startswith('ops/s'):
                section = 'ops'
                continue
            if line.startswith('read:') or line.startswith('write:'):
                section = line.split(':', 1)[0]
                continue
            if section and line[0].isdigit():
                values = _numbers(line)
                rec = latest[mount]
                if section == 'ops' and values:
                    rec['ops'] = values[0]
                elif section in ('read', 'write') and len(values) >= 7:
                    # ops/s kB/s kB/op retrans (retrans%) RTT exe ...
                    rec[section] = {'ops': values[0], 'kb': values[1],
                                    'retrans_pct': values[4], 'rtt': values[5],
                                    'exe': values[6]}
                section = None

        records = []
        for rec in latest.values():
            r, w = rec.get('read'), rec.get('write')
            if 'ops' not in rec and not r and not w:
                continue
            r = r or {'ops': 0.0, 'kb': 0.0, 'retrans_pct': 0.0, 'rtt': 0.0, 'exe': 0.0}
            w = w or {'ops': 0.0, 'kb': 0.0, 'retrans_pct': 0.0, 'rtt': 0.0, 'exe': 0.0}
            rw = r['ops'] + w['ops']
            # Latency only means something when there were reads or writes.
            weighted = {k: (r[k] * r['ops'] + w[k] * w['ops']) / rw if rw else None
                        for k in ('rtt', 'exe', 'retrans_pct')}

            stats = NFSStats(
                mount_point=rec['mount_point'],
                server=rec['server'],
                ops_per_sec=rec.get('ops', rw),
                read_ops_per_sec=r['ops'],
                write_ops_per_sec=w['ops'],
                read_kb_per_sec=r['kb'],
                write_kb_per_sec=w['kb'],
                avg_rtt_ms=weighted['rtt'],
                avg_exe_ms=weighted['exe'],
                retrans_percent=weighted['retrans_pct'],
            )
            records.append({'type': 'nfs', 'timestamp': timestamp, **stats.to_dict()})
        return records

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

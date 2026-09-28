# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ VMStat Collector

Collects memory pressure, swap activity, and context switches from vmstat.
Key indicators for system stress and memory-bound jobs.
"""

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .base import BaseCollector, CollectionError, registry

logger = logging.getLogger(__name__)


@dataclass
class VMStats:
    """System memory and process statistics."""
    # Processes
    procs_runnable: int      # r: processes waiting for run time
    procs_blocked: int       # b: processes in uninterruptible sleep (I/O)

    # Memory (KB)
    swap_used_kb: int        # swpd: virtual memory used
    free_kb: int             # free: idle memory
    buffer_kb: int           # buff: memory used as buffers
    cache_kb: int            # cache: memory used as cache

    # Swap activity (KB/s)
    swap_in_kb: int          # si: memory swapped in from disk
    swap_out_kb: int         # so: memory swapped out to disk

    # I/O (blocks/s)
    blocks_in: int           # bi: blocks received from device
    blocks_out: int          # bo: blocks sent to device

    # System
    interrupts: int          # in: interrupts per second
    context_switches: int    # cs: context switches per second

    # CPU percentages
    cpu_user: int            # us: user time
    cpu_system: int          # sy: system time
    cpu_idle: int            # id: idle time
    cpu_iowait: int          # wa: waiting for I/O
    cpu_steal: int           # st: stolen from VM

    def to_dict(self) -> dict[str, Any]:
        return {
            'procs_runnable': self.procs_runnable,
            'procs_blocked': self.procs_blocked,
            'swap_used_kb': self.swap_used_kb,
            'free_kb': self.free_kb,
            'buffer_kb': self.buffer_kb,
            'cache_kb': self.cache_kb,
            'swap_in_kb': self.swap_in_kb,
            'swap_out_kb': self.swap_out_kb,
            'blocks_in': self.blocks_in,
            'blocks_out': self.blocks_out,
            'interrupts': self.interrupts,
            'context_switches': self.context_switches,
            'cpu_user': self.cpu_user,
            'cpu_system': self.cpu_system,
            'cpu_idle': self.cpu_idle,
            'cpu_iowait': self.cpu_iowait,
            'cpu_steal': self.cpu_steal,
        }

    @property
    def memory_pressure(self) -> float:
        """
        Memory pressure indicator (0-1).
        High when: swap active, blocked procs, low free memory.
        """
        total_mem = self.free_kb + self.buffer_kb + self.cache_kb + self.swap_used_kb
        if total_mem == 0:
            return 0.0

        # Factors contributing to memory pressure
        swap_ratio = min(self.swap_used_kb / max(total_mem, 1), 1.0)
        swap_activity = min((self.swap_in_kb + self.swap_out_kb) / 1000, 1.0)
        blocked_ratio = min(self.procs_blocked / 10, 1.0)

        # Weighted combination
        pressure = (0.4 * swap_ratio + 0.4 * swap_activity + 0.2 * blocked_ratio)
        return min(pressure, 1.0)


@registry.register
class VMStatCollector(BaseCollector):
    """
    Collector for memory and process statistics from vmstat.
    
    Collected data:
        - Swap usage and activity
        - Memory (free, buffer, cache)
        - Process states (runnable, blocked)
        - Context switches and interrupts
        - CPU breakdown
    """

    name = "vmstat"
    description = "Memory pressure and swap statistics"
    default_interval = 60

    def __init__(self, config: dict[str, Any], db_path: str):
        super().__init__(config, db_path)
        logger.info("VMStatCollector initialized")

    def collect(self) -> list[dict[str, Any]]:
        """Collect statistics from vmstat."""

        try:
            # Run vmstat with 1 second interval, 2 reports (skip first which is since boot)
            result = subprocess.run(
                ['vmstat', '1', '2'],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                raise CollectionError(f"vmstat failed: {result.stderr}")

            return self._parse_vmstat_output(result.stdout)

        except FileNotFoundError:
            raise CollectionError("vmstat not found")
        except subprocess.TimeoutExpired:
            raise CollectionError("vmstat timed out")

    def _parse_vmstat_output(self, output: str) -> list[dict[str, Any]]:
        """Parse vmstat output."""
        records = []
        timestamp = datetime.now()

        lines = output.strip().split('\n')

        # Find the last data line (skip header and first report)
        data_line = None
        for line in reversed(lines):
            parts = line.split()
            if len(parts) >= 17 and parts[0].isdigit():
                data_line = parts
                break

        if not data_line:
            return records

        try:
            # Parse vmstat columns
            # procs: r b | memory: swpd free buff cache | swap: si so | io: bi bo | system: in cs | cpu: us sy id wa st
            stats = VMStats(
                procs_runnable=int(data_line[0]),
                procs_blocked=int(data_line[1]),
                swap_used_kb=int(data_line[2]),
                free_kb=int(data_line[3]),
                buffer_kb=int(data_line[4]),
                cache_kb=int(data_line[5]),
                swap_in_kb=int(data_line[6]),
                swap_out_kb=int(data_line[7]),
                blocks_in=int(data_line[8]),
                blocks_out=int(data_line[9]),
                interrupts=int(data_line[10]),
                context_switches=int(data_line[11]),
                cpu_user=int(data_line[12]),
                cpu_system=int(data_line[13]),
                cpu_idle=int(data_line[14]),
                cpu_iowait=int(data_line[15]),
                cpu_steal=int(data_line[16]) if len(data_line) > 16 else 0,
            )

            records.append({
                'type': 'vmstat',
                'timestamp': timestamp.isoformat(),
                'memory_pressure': stats.memory_pressure,
                **stats.to_dict()
            })

        except (ValueError, IndexError) as e:
            logger.warning(f"Failed to parse vmstat output: {e}")

        return records

    def store(self, data: list[dict[str, Any]]) -> None:
        """Store vmstat data in database."""

        with self.get_db_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS vmstat (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    procs_runnable INTEGER,
                    procs_blocked INTEGER,
                    swap_used_kb INTEGER,
                    free_kb INTEGER,
                    buffer_kb INTEGER,
                    cache_kb INTEGER,
                    swap_in_kb INTEGER,
                    swap_out_kb INTEGER,
                    blocks_in INTEGER,
                    blocks_out INTEGER,
                    interrupts INTEGER,
                    context_switches INTEGER,
                    cpu_user INTEGER,
                    cpu_system INTEGER,
                    cpu_idle INTEGER,
                    cpu_iowait INTEGER,
                    cpu_steal INTEGER,
                    memory_pressure REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_vmstat_ts 
                ON vmstat(timestamp)
            """)

            for record in data:
                if record.get('type') == 'vmstat':
                    conn.execute(
                        """
                        INSERT INTO vmstat 
                        (timestamp, procs_runnable, procs_blocked, swap_used_kb,
                         free_kb, buffer_kb, cache_kb, swap_in_kb, swap_out_kb,
                         blocks_in, blocks_out, interrupts, context_switches,
                         cpu_user, cpu_system, cpu_idle, cpu_iowait, cpu_steal,
                         memory_pressure)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record['timestamp'],
                            record['procs_runnable'],
                            record['procs_blocked'],
                            record['swap_used_kb'],
                            record['free_kb'],
                            record['buffer_kb'],
                            record['cache_kb'],
                            record['swap_in_kb'],
                            record['swap_out_kb'],
                            record['blocks_in'],
                            record['blocks_out'],
                            record['interrupts'],
                            record['context_switches'],
                            record['cpu_user'],
                            record['cpu_system'],
                            record['cpu_idle'],
                            record['cpu_iowait'],
                            record['cpu_steal'],
                            record['memory_pressure'],
                        )
                    )

            conn.commit()
            logger.debug(f"Stored {len(data)} vmstat records")

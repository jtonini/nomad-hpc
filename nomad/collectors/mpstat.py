# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ MPStat Collector

Collects per-core CPU utilization from mpstat.
Detects core imbalance, NUMA effects, and affinity issues.
"""

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np

from .base import BaseCollector, CollectionError, registry

logger = logging.getLogger(__name__)


@dataclass
class CoreStats:
    """Statistics for a single CPU core."""
    core_id: int
    user_percent: float
    nice_percent: float
    system_percent: float
    iowait_percent: float
    irq_percent: float
    soft_percent: float
    steal_percent: float
    idle_percent: float

    @property
    def busy_percent(self) -> float:
        """Total busy percentage (100 - idle)."""
        return 100.0 - self.idle_percent

    def to_dict(self) -> dict[str, Any]:
        return {
            'core_id': self.core_id,
            'user_percent': self.user_percent,
            'nice_percent': self.nice_percent,
            'system_percent': self.system_percent,
            'iowait_percent': self.iowait_percent,
            'irq_percent': self.irq_percent,
            'soft_percent': self.soft_percent,
            'steal_percent': self.steal_percent,
            'idle_percent': self.idle_percent,
            'busy_percent': self.busy_percent,
        }


@dataclass
class CPUSummary:
    """Summary statistics across all cores."""
    timestamp: datetime
    num_cores: int

    # Aggregate metrics
    avg_busy_percent: float
    max_busy_percent: float
    min_busy_percent: float
    std_busy_percent: float  # Standard deviation - indicates imbalance

    # Iowait aggregates
    avg_iowait_percent: float
    max_iowait_percent: float

    # Imbalance indicators
    busy_spread: float  # max - min busy
    imbalance_ratio: float  # std / avg (coefficient of variation)
    cores_idle: int  # cores with < 5% busy
    cores_saturated: int  # cores with > 95% busy

    def to_dict(self) -> dict[str, Any]:
        return {
            'num_cores': self.num_cores,
            'avg_busy_percent': self.avg_busy_percent,
            'max_busy_percent': self.max_busy_percent,
            'min_busy_percent': self.min_busy_percent,
            'std_busy_percent': self.std_busy_percent,
            'avg_iowait_percent': self.avg_iowait_percent,
            'max_iowait_percent': self.max_iowait_percent,
            'busy_spread': self.busy_spread,
            'imbalance_ratio': self.imbalance_ratio,
            'cores_idle': self.cores_idle,
            'cores_saturated': self.cores_saturated,
        }


@registry.register
class MPStatCollector(BaseCollector):
    """
    Collector for per-core CPU statistics from mpstat.
    
    Configuration options:
        store_per_core: Store individual core stats (default: True)
        store_summary: Store summary statistics (default: True)
    
    Collected data:
        - Per-core utilization (user, system, iowait, idle)
        - Summary statistics (avg, std, spread)
        - Imbalance indicators
    """

    name = "mpstat"
    description = "Per-core CPU statistics"
    default_interval = 60

    def __init__(self, config: dict[str, Any], db_path: str):
        super().__init__(config, db_path)

        self.store_per_core = config.get('store_per_core', True)
        self.store_summary = config.get('store_summary', True)

        logger.info(f"MPStatCollector: per_core={self.store_per_core}, summary={self.store_summary}")

    def collect(self) -> list[dict[str, Any]]:
        """Collect CPU statistics from mpstat."""

        try:
            # Run mpstat with per-CPU stats, single snapshot
            # -P ALL: all processors, 1 1: 1 second interval, 1 report
            result = subprocess.run(
                ['mpstat', '-P', 'ALL', '1', '1'],
                capture_output=True,
                text=True,
                timeout=10,
            )

            if result.returncode != 0:
                raise CollectionError(f"mpstat failed: {result.stderr}")

            return self._parse_mpstat_output(result.stdout)

        except FileNotFoundError:
            raise CollectionError("mpstat not found - install sysstat package")
        except subprocess.TimeoutExpired:
            raise CollectionError("mpstat timed out")

    def _parse_mpstat_output(self, output: str) -> list[dict[str, Any]]:
        """Parse mpstat output into structured data."""
        records = []
        timestamp = datetime.now()

        lines = output.strip().split('\n')
        core_stats = []

        for line in lines:
            line = line.strip()

            # Skip header lines
            if not line or 'Linux' in line or 'CPU' in line and '%usr' in line:
                continue

            # Skip 'all' aggregate line (we compute our own)
            if line.startswith('all') or ' all ' in line:
                continue

            # Parse per-core line
            core = self._parse_core_line(line)
            if core is not None:
                core_stats.append(core)

                if self.store_per_core:
                    records.append({
                        'type': 'mpstat_core',
                        'timestamp': timestamp.isoformat(),
                        **core.to_dict()
                    })

        # Compute and store summary
        if core_stats and self.store_summary:
            summary = self._compute_summary(timestamp, core_stats)
            records.append({
                'type': 'mpstat_summary',
                'timestamp': timestamp.isoformat(),
                **summary.to_dict()
            })

        return records

    def _parse_core_line(self, line: str) -> CoreStats | None:
        """Parse a single core statistics line."""
        try:
            parts = line.split()

            # Find the core ID (could be at different positions)
            # Format varies: "HH:MM:SS  0  1.00 ..." or "0  1.00 ..."
            core_idx = None
            for i, part in enumerate(parts):
                if part.isdigit() and i < 3:
                    core_idx = i
                    break

            if core_idx is None:
                return None

            core_id = int(parts[core_idx])

            # Values follow core ID
            # Order: %usr %nice %sys %iowait %irq %soft %steal %guest %gnice %idle
            # But may vary, typically: usr nice sys iowait irq soft steal idle
            values = [float(v) for v in parts[core_idx + 1:] if self._is_float(v)]

            if len(values) >= 8:
                return CoreStats(
                    core_id=core_id,
                    user_percent=values[0],
                    nice_percent=values[1],
                    system_percent=values[2],
                    iowait_percent=values[3],
                    irq_percent=values[4],
                    soft_percent=values[5],
                    steal_percent=values[6],
                    idle_percent=values[-1],  # idle is always last
                )
            elif len(values) >= 4:
                # Simplified format
                return CoreStats(
                    core_id=core_id,
                    user_percent=values[0],
                    nice_percent=0,
                    system_percent=values[1] if len(values) > 1 else 0,
                    iowait_percent=values[2] if len(values) > 2 else 0,
                    irq_percent=0,
                    soft_percent=0,
                    steal_percent=0,
                    idle_percent=values[-1],
                )

        except (ValueError, IndexError) as e:
            logger.debug(f"Failed to parse core line '{line}': {e}")
        return None

    def _is_float(self, value: str) -> bool:
        """Check if string is a valid float."""
        try:
            float(value)
            return True
        except ValueError:
            return False

    def _compute_summary(self, timestamp: datetime, cores: list[CoreStats]) -> CPUSummary:
        """Compute summary statistics across all cores."""
        busy_values = np.array([c.busy_percent for c in cores])
        iowait_values = np.array([c.iowait_percent for c in cores])

        avg_busy = float(np.mean(busy_values))
        std_busy = float(np.std(busy_values))

        return CPUSummary(
            timestamp=timestamp,
            num_cores=len(cores),
            avg_busy_percent=avg_busy,
            max_busy_percent=float(np.max(busy_values)),
            min_busy_percent=float(np.min(busy_values)),
            std_busy_percent=std_busy,
            avg_iowait_percent=float(np.mean(iowait_values)),
            max_iowait_percent=float(np.max(iowait_values)),
            busy_spread=float(np.max(busy_values) - np.min(busy_values)),
            imbalance_ratio=std_busy / avg_busy if avg_busy > 0 else 0,
            cores_idle=int(np.sum(busy_values < 5)),
            cores_saturated=int(np.sum(busy_values > 95)),
        )

    def store(self, data: list[dict[str, Any]]) -> None:
        """Store CPU statistics in database."""

        with self.get_db_connection() as conn:
            # Ensure tables exist
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mpstat_core (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    core_id INTEGER NOT NULL,
                    user_percent REAL,
                    nice_percent REAL,
                    system_percent REAL,
                    iowait_percent REAL,
                    irq_percent REAL,
                    soft_percent REAL,
                    steal_percent REAL,
                    idle_percent REAL,
                    busy_percent REAL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_mpstat_core_ts 
                ON mpstat_core(timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_mpstat_core_id 
                ON mpstat_core(timestamp, core_id)
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS mpstat_summary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    num_cores INTEGER,
                    avg_busy_percent REAL,
                    max_busy_percent REAL,
                    min_busy_percent REAL,
                    std_busy_percent REAL,
                    avg_iowait_percent REAL,
                    max_iowait_percent REAL,
                    busy_spread REAL,
                    imbalance_ratio REAL,
                    cores_idle INTEGER,
                    cores_saturated INTEGER
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_mpstat_summary_ts 
                ON mpstat_summary(timestamp)
            """)

            for record in data:
                if record.get('type') == 'mpstat_core':
                    conn.execute(
                        """
                        INSERT INTO mpstat_core 
                        (timestamp, core_id, user_percent, nice_percent, system_percent,
                         iowait_percent, irq_percent, soft_percent, steal_percent,
                         idle_percent, busy_percent)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record['timestamp'],
                            record['core_id'],
                            record['user_percent'],
                            record['nice_percent'],
                            record['system_percent'],
                            record['iowait_percent'],
                            record['irq_percent'],
                            record['soft_percent'],
                            record['steal_percent'],
                            record['idle_percent'],
                            record['busy_percent'],
                        )
                    )
                elif record.get('type') == 'mpstat_summary':
                    conn.execute(
                        """
                        INSERT INTO mpstat_summary
                        (timestamp, num_cores, avg_busy_percent, max_busy_percent,
                         min_busy_percent, std_busy_percent, avg_iowait_percent,
                         max_iowait_percent, busy_spread, imbalance_ratio,
                         cores_idle, cores_saturated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record['timestamp'],
                            record['num_cores'],
                            record['avg_busy_percent'],
                            record['max_busy_percent'],
                            record['min_busy_percent'],
                            record['std_busy_percent'],
                            record['avg_iowait_percent'],
                            record['max_iowait_percent'],
                            record['busy_spread'],
                            record['imbalance_ratio'],
                            record['cores_idle'],
                            record['cores_saturated'],
                        )
                    )

            conn.commit()
            logger.debug(f"Stored {len(data)} mpstat records")

    def get_recent_summary(self, minutes: int = 60) -> list[dict]:
        """Get recent summary statistics."""
        with self.get_db_connection() as conn:
            conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
            rows = conn.execute(
                """
                SELECT *
                FROM mpstat_summary
                WHERE timestamp > datetime('now', ?)
                ORDER BY timestamp
                """,
                (f'-{minutes} minutes',)
            ).fetchall()
            return rows

    def get_core_history(self, core_id: int, minutes: int = 60) -> list[dict]:
        """Get history for a specific core."""
        with self.get_db_connection() as conn:
            conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
            rows = conn.execute(
                """
                SELECT *
                FROM mpstat_core
                WHERE core_id = ? AND timestamp > datetime('now', ?)
                ORDER BY timestamp
                """,
                (core_id, f'-{minutes} minutes')
            ).fetchall()
            return rows

    def get_imbalanced_periods(self, threshold: float = 0.5, minutes: int = 60) -> list[dict]:
        """Find periods of high core imbalance."""
        with self.get_db_connection() as conn:
            conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
            rows = conn.execute(
                """
                SELECT *
                FROM mpstat_summary
                WHERE imbalance_ratio > ? 
                  AND timestamp > datetime('now', ?)
                ORDER BY imbalance_ratio DESC
                """,
                (threshold, f'-{minutes} minutes')
            ).fetchall()
            return rows

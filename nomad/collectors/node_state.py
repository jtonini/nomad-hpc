# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAD Node State Collector

Collects SLURM node state, allocation, and health from scontrol.
Detects drained nodes, allocation patterns, and node issues.
"""

import logging
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .base import BaseCollector, CollectionError, registry

from nomad.alerts import send_alert

logger = logging.getLogger(__name__)


@dataclass
class NodeState:
    """SLURM node state and allocation."""
    node_name: str
    state: str              # IDLE, MIXED, ALLOCATED, DRAIN, DOWN, etc.

    # CPU
    cpus_total: int
    cpus_alloc: int
    cpu_load: float

    # Memory (MB)
    memory_total_mb: int
    memory_alloc_mb: int
    memory_free_mb: int

    # State info
    partitions: str
    reason: str | None      # Drain/down reason

    # Features
    features: str | None
    gres: str | None        # GPU/other resources

    def to_dict(self) -> dict[str, Any]:
        return {
            'node_name': self.node_name,
            'state': self.state,
            'cpus_total': self.cpus_total,
            'cpus_alloc': self.cpus_alloc,
            'cpu_load': self.cpu_load,
            'memory_total_mb': self.memory_total_mb,
            'memory_alloc_mb': self.memory_alloc_mb,
            'memory_free_mb': self.memory_free_mb,
            'partitions': self.partitions,
            'reason': self.reason,
            'features': self.features,
            'gres': self.gres,
        }

    @property
    def cpu_alloc_percent(self) -> float:
        if self.cpus_total == 0:
            return 0.0
        return (self.cpus_alloc / self.cpus_total) * 100

    @property
    def memory_alloc_percent(self) -> float:
        if self.memory_total_mb == 0:
            return 0.0
        return (self.memory_alloc_mb / self.memory_total_mb) * 100

    @property
    def is_healthy(self) -> bool:
        """Node is healthy if not drained, down, or in error state."""
        unhealthy_states = ['DOWN', 'DRAIN', 'DRAINING', 'ERROR', 'FAIL', 'FAILING']
        return not any(s in self.state.upper() for s in unhealthy_states)


@registry.register
class NodeStateCollector(BaseCollector):
    """
    Collector for SLURM node state from scontrol.
    
    Collected data:
        - Node state (IDLE, MIXED, ALLOCATED, DRAIN, DOWN)
        - CPU and memory allocation
        - CPU load
        - Drain/down reasons
        - GRES (GPUs, etc.)
    """

    name = "node_state"
    description = "SLURM node state and allocation"
    default_interval = 60

    def __init__(self, config: dict[str, Any], db_path: str):
        super().__init__(config, db_path)
        self.nodes = config.get('nodes', None)  # None = all nodes
        self.cluster_name = config.get('cluster_name', 'default')
        self._alert_config = config.get('alerts', {}).get('node_state', {})
        logger.info(f"NodeStateCollector: nodes={self.nodes or 'all'}")

    # Default state policies. Used when [alerts.node_state] is absent from
    # nomad.toml. Substring match (case-insensitive) covers Slurm composite
    # states like "DOWN+NOT_RESPONDING" and "MIXED+DRAIN", plus common
    # vocabulary from PBS ("down", "offline") and LSF ("unavail", "unreach").
    DEFAULT_CRITICAL_STATES = ('DOWN', 'NOT_RESPONDING', 'FAIL', 'OFFLINE', 'UNREACH')
    DEFAULT_WARNING_STATES = ('DRAIN', 'DRNG', 'DRAINING', 'CLOSED', 'UNAVAIL')

    def collect(self) -> list[dict[str, Any]]:
        """Collect node state from scontrol."""
        try:
            cmd = ['scontrol', 'show', 'node']
            if self.nodes:
                cmd.append(','.join(self.nodes))
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                raise CollectionError(f"scontrol failed: {result.stderr}")
            records = self._parse_scontrol_output(result.stdout)
            if self._alert_config.get('enabled', True):
                self._dispatch_state_alerts(records)
            return records
        except FileNotFoundError:
            raise CollectionError("scontrol not found - SLURM not installed?")
        except subprocess.TimeoutExpired:
            raise CollectionError("scontrol timed out")

    def _classify_state(self, state: str) -> str | None:
        """
        Return 'critical' | 'warning' | None for a state string.

        None means the state is explicitly ignored (e.g. POWERED_DOWN at
        sites that consider it operational, not an incident).

        Configuration:
            [alerts.node_state]
            critical_states = ["DOWN", "NOT_RESPONDING", ...]
            warning_states  = ["DRAIN", "DRNG", ...]
            ignore_states   = ["POWERED_DOWN", ...]
        """
        s = (state or '').upper()
        cfg = self._alert_config

        ignore = [x.upper() for x in cfg.get('ignore_states', [])]
        critical = [x.upper() for x in cfg.get('critical_states', self.DEFAULT_CRITICAL_STATES)]
        warning = [x.upper() for x in cfg.get('warning_states', self.DEFAULT_WARNING_STATES)]

        if any(tok in s for tok in ignore):
            return None
        if any(tok in s for tok in critical):
            return 'critical'
        if any(tok in s for tok in warning):
            return 'warning'
        return 'warning'  # is_healthy=0 but state unrecognized -> warn conservatively

    def _dispatch_state_alerts(self, records: list[dict[str, Any]]) -> None:
        """
        Dispatch alerts for unhealthy nodes via the AlertDispatcher.

        Deduplication is handled by the dispatcher's cooldown (same
        source:host:severity within cooldown_minutes is suppressed),
        so a node staying DOWN for hours sends one alert, not many.
        """
        for rec in records:
            if rec.get('is_healthy', 1):
                continue

            severity = self._classify_state(rec.get('state', ''))
            if severity is None:
                continue  # explicitly ignored

            node_name = rec.get('node_name', 'unknown')
            reason = rec.get('reason') or 'no reason given'

            try:
                send_alert(
                    severity=severity,
                    source='node_state',
                    message=f"Node {node_name} is {rec.get('state')} ({reason})",
                    host=node_name,
                    details={
                        'state': rec.get('state'),
                        'reason': reason,
                        'cluster': rec.get('cluster', 'default'),
                        'partitions': rec.get('partitions'),
                    },
                )
            except Exception as e:
                logger.warning(
                    f"Failed to dispatch node-state alert for {node_name}: {e}"
                )

    def _parse_scontrol_output(self, output: str) -> list[dict[str, Any]]:
        """Parse scontrol show node output."""
        records = []
        timestamp = datetime.now()

        # Split by node blocks (each starts with NodeName=)
        node_blocks = re.split(r'\n(?=NodeName=)', output.strip())

        for block in node_blocks:
            if not block.strip():
                continue

            node = self._parse_node_block(block)
            if node:
                records.append({
                    'type': 'node_state',
                    'cluster': self.cluster_name,
                    'timestamp': timestamp.isoformat(),
                    'cpu_alloc_percent': node.cpu_alloc_percent,
                    'memory_alloc_percent': node.memory_alloc_percent,
                    'is_healthy': node.is_healthy,
                    **node.to_dict()
                })

        return records

    def _parse_node_block(self, block: str) -> NodeState | None:
        """Parse a single node's scontrol output."""

        def extract(pattern: str, default: str = '') -> str:
            match = re.search(pattern, block)
            return match.group(1) if match else default

        def extract_int(pattern: str, default: int = 0) -> int:
            match = re.search(pattern, block)
            if match:
                try:
                    # Handle values like "7937M" -> 7937
                    val = match.group(1).rstrip('MKG')
                    return int(val)
                except ValueError:
                    return default
            return default

        def extract_float(pattern: str, default: float = 0.0) -> float:
            match = re.search(pattern, block)
            if match:
                try:
                    return float(match.group(1))
                except ValueError:
                    return default
            return default

        try:
            node_name = extract(r'NodeName=(\S+)')
            if not node_name:
                return None

            # Parse State (can have modifiers like MIXED+DRAIN)
            state = extract(r'State=(\S+)', 'UNKNOWN')

            # CPU info
            cpus_total = extract_int(r'CPUTot=(\d+)')
            cpus_alloc = extract_int(r'CPUAlloc=(\d+)')
            cpu_load = extract_float(r'CPULoad=(\d+\.?\d*)')

            # Memory info
            memory_total = extract_int(r'RealMemory=(\d+)')
            memory_alloc = extract_int(r'AllocMem=(\d+)')
            memory_free = extract_int(r'FreeMem=(\d+)')

            # Other info
            partitions = extract(r'Partitions=(\S+)', '')
            reason = extract(r'Reason=([^\n]+)', None)
            if reason == 'N/A' or reason == '':
                reason = None

            features = extract(r'ActiveFeatures=(\S+)', None)
            if features == '(null)':
                features = None

            gres = extract(r'Gres=(\S+)', None)
            if gres == '(null)':
                gres = None

            return NodeState(
                node_name=node_name,
                state=state,
                cpus_total=cpus_total,
                cpus_alloc=cpus_alloc,
                cpu_load=cpu_load,
                memory_total_mb=memory_total,
                memory_alloc_mb=memory_alloc,
                memory_free_mb=memory_free,
                partitions=partitions,
                reason=reason,
                features=features,
                gres=gres,
            )

        except Exception as e:
            logger.warning(f"Failed to parse node block: {e}")
            return None

    def store(self, data: list[dict[str, Any]]) -> None:
        """Store node state data in database."""

        with self.get_db_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS node_state (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    node_name TEXT NOT NULL,
                    state TEXT,
                    cpus_total INTEGER,
                    cpus_alloc INTEGER,
                    cpu_load REAL,
                    memory_total_mb INTEGER,
                    memory_alloc_mb INTEGER,
                    memory_free_mb INTEGER,
                    cpu_alloc_percent REAL,
                    memory_alloc_percent REAL,
                    cluster TEXT DEFAULT 'default',
                    partitions TEXT,
                    reason TEXT,
                    features TEXT,
                    gres TEXT,
                    is_healthy INTEGER
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_node_state_ts 
                ON node_state(timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_node_state_name 
                ON node_state(node_name, timestamp)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_node_state_cluster
                ON node_state(cluster, timestamp)
            """)

            for record in data:
                if record.get('type') == 'node_state':
                    conn.execute(
                        """
                        INSERT INTO node_state 
                        (timestamp, node_name, cluster, state, cpus_total, cpus_alloc, cpu_load,
                         memory_total_mb, memory_alloc_mb, memory_free_mb,
                         cpu_alloc_percent, memory_alloc_percent,
                         partitions, reason, features, gres, is_healthy)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record['timestamp'],
                            record['node_name'],
                            record.get('cluster', 'default'),
                            record['state'],
                            record['cpus_total'],
                            record['cpus_alloc'],
                            record['cpu_load'],
                            record['memory_total_mb'],
                            record['memory_alloc_mb'],
                            record['memory_free_mb'],
                            record['cpu_alloc_percent'],
                            record['memory_alloc_percent'],
                            record['partitions'],
                            record['reason'],
                            record['features'],
                            record['gres'],
                            1 if record['is_healthy'] else 0,
                        )
                    )

            conn.commit()
            logger.debug(f"Stored {len(data)} node state records")

    def get_unhealthy_nodes(self) -> list[dict]:
        """Get currently unhealthy nodes."""
        with self.get_db_connection() as conn:
            conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
            rows = conn.execute(
                """
                SELECT node_name, state, reason, timestamp
                FROM node_state
                WHERE is_healthy = 0
                  AND timestamp = (SELECT MAX(timestamp) FROM node_state)
                """
            ).fetchall()
            return rows

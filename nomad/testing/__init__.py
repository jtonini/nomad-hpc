# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Mock Cluster for Testing

Provides a simulated HPC environment for unit testing collectors,
the patching framework, and edu module without requiring a real cluster.

Usage:
    from nomad.testing import MockCluster

    with MockCluster() as cluster:
        # cluster.db_path contains a test database
        # cluster.config contains a valid config dict
        result = my_collector.collect()
        assert len(result) > 0
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch


@dataclass
class MockNode:
    """A simulated compute node."""
    name: str
    partition: str
    state: str = "idle"
    cpu_total: int = 32
    cpu_alloc: int = 0
    mem_total_mb: int = 128000
    mem_alloc_mb: int = 0
    gres: str = ""
    reason: str = ""

    def scontrol_output(self) -> str:
        """Generate scontrol show node output."""
        return f"""NodeName={self.name} Arch=x86_64 CoresPerSocket=16
   CPUAlloc={self.cpu_alloc} CPUTot={self.cpu_total} CPULoad={self.cpu_alloc * 0.8:.2f}
   AvailableFeatures=(null)
   ActiveFeatures=(null)
   Gres={self.gres}
   NodeAddr={self.name} NodeHostName={self.name}
   OS=Linux RealMemory={self.mem_total_mb} AllocMem={self.mem_alloc_mb}
   Sockets=2 Boards=1 State={self.state} ThreadsPerCore=1 TmpDisk=0
   Partitions={self.partition}
   Reason={self.reason}
"""


@dataclass
class MockJob:
    """A simulated SLURM job."""
    job_id: int
    user: str
    partition: str
    node: str
    state: str = "COMPLETED"
    exit_code: int = 0
    req_cpus: int = 4
    req_mem_mb: int = 8192
    req_gpus: int = 0
    req_time_seconds: int = 3600
    runtime_seconds: int = 1800
    submit_time: datetime | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None

    # Performance metrics
    avg_cpu_percent: float = 75.0
    peak_cpu_percent: float = 95.0
    avg_memory_gb: float = 4.0
    peak_memory_gb: float = 6.0
    avg_io_wait_percent: float = 5.0
    total_nfs_read_gb: float = 0.5
    total_nfs_write_gb: float = 0.2
    total_local_read_gb: float = 2.0
    total_local_write_gb: float = 1.0
    nfs_ratio: float = 0.15
    used_gpu: bool = False
    health_score: float = 0.85

    def __post_init__(self):
        if self.end_time is None:
            self.end_time = datetime.now() - timedelta(hours=random.randint(1, 48))
        if self.start_time is None:
            self.start_time = self.end_time - timedelta(seconds=self.runtime_seconds)
        if self.submit_time is None:
            self.submit_time = self.start_time - timedelta(seconds=random.randint(60, 600))


@dataclass
class MockCluster:
    """
    A simulated HPC cluster for testing.
    
    Creates:
    - Temporary SQLite database with schema
    - Mock nodes and jobs
    - Config dictionary
    - Subprocess mocks for SLURM commands
    """
    name: str = "test-cluster"
    partitions: dict[str, list[str]] = field(default_factory=lambda: {
        "compute": ["node01", "node02", "node03", "node04"],
        "gpu": ["gpu01", "gpu02"],
        "debug": ["debug01"],
    })
    users: list[str] = field(default_factory=lambda: [
        "alice", "bob", "charlie", "diana", "eve"
    ])
    groups: dict[str, list[str]] = field(default_factory=lambda: {
        "cs101": ["alice", "bob", "charlie"],
        "bio301": ["diana", "eve"],
        "research": ["alice", "diana"],
    })

    # Internal state
    _temp_dir: tempfile.TemporaryDirectory | None = field(default=None, repr=False)
    _db_path: Path | None = field(default=None, repr=False)
    _nodes: list[MockNode] = field(default_factory=list, repr=False)
    _jobs: list[MockJob] = field(default_factory=list, repr=False)

    def __enter__(self) -> MockCluster:
        self._temp_dir = tempfile.TemporaryDirectory()
        self._db_path = Path(self._temp_dir.name) / "test_nomad.db"
        self._setup_database()
        self._generate_nodes()
        self._generate_jobs()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._temp_dir:
            self._temp_dir.cleanup()

    @property
    def db_path(self) -> str:
        return str(self._db_path)

    @property
    def config(self) -> dict[str, Any]:
        """Return a config dict suitable for NØMAÐ components."""
        return {
            "general": {
                "data_dir": self._temp_dir.name,
                "log_level": "debug",
            },
            "cluster_name": self.name,
            "collectors": {
                "enabled": ["node_state", "job_metrics", "groups"],
                "interval": 60,
            },
        }

    def _setup_database(self):
        """Create database with NØMAÐ schema."""
        conn = sqlite3.connect(self._db_path)
        c = conn.cursor()

        # Jobs table
        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY, user_name TEXT, partition TEXT, node_list TEXT,
            job_name TEXT, state TEXT, exit_code INTEGER, exit_signal INTEGER,
            failure_reason INTEGER, submit_time DATETIME, start_time DATETIME,
            end_time DATETIME, req_cpus INTEGER, req_mem_mb INTEGER, req_gpus INTEGER,
            req_time_seconds INTEGER, runtime_seconds INTEGER, wait_time_seconds INTEGER)""")

        # Job summary table
        c.execute("""CREATE TABLE IF NOT EXISTS job_summary (
            job_id TEXT PRIMARY KEY, peak_cpu_percent REAL, peak_memory_gb REAL,
            avg_cpu_percent REAL, avg_memory_gb REAL, avg_io_wait_percent REAL,
            total_nfs_read_gb REAL, total_nfs_write_gb REAL,
            total_local_read_gb REAL, total_local_write_gb REAL,
            nfs_ratio REAL, used_gpu INTEGER, health_score REAL)""")

        # Node state table
        c.execute("""CREATE TABLE IF NOT EXISTS node_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            node_name TEXT, cluster TEXT DEFAULT 'default',
            partitions TEXT, state TEXT, cpu_total INTEGER, cpu_alloc INTEGER,
            cpu_load REAL, mem_total_mb INTEGER, mem_alloc_mb INTEGER,
            gres TEXT, reason TEXT)""")

        # Group membership table
        c.execute("""CREATE TABLE IF NOT EXISTS group_membership (
            username TEXT, group_name TEXT, gid INTEGER, cluster TEXT,
            PRIMARY KEY (username, group_name, cluster))""")

        conn.commit()
        conn.close()

    def _generate_nodes(self):
        """Generate mock nodes."""
        self._nodes = []
        for partition, node_names in self.partitions.items():
            for node_name in node_names:
                gres = "gpu:2" if "gpu" in partition else ""
                node = MockNode(
                    name=node_name,
                    partition=partition,
                    state=random.choice(["idle", "allocated", "mixed"]),
                    cpu_alloc=random.randint(0, 32),
                    mem_alloc_mb=random.randint(0, 64000),
                    gres=gres,
                )
                self._nodes.append(node)

    def _generate_jobs(self, count: int = 100):
        """Generate mock jobs and insert into database."""
        self._jobs = []
        conn = sqlite3.connect(self._db_path)
        c = conn.cursor()

        for i in range(count):
            partition = random.choice(list(self.partitions.keys()))
            node = random.choice(self.partitions[partition])
            user = random.choice(self.users)

            # Vary job quality to test edu scoring
            efficiency = random.random()
            job = MockJob(
                job_id=1000 + i,
                user=user,
                partition=partition,
                node=node,
                state=random.choice(["COMPLETED", "COMPLETED", "COMPLETED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY"]),
                req_cpus=random.choice([1, 2, 4, 8, 16, 32]),
                req_mem_mb=random.choice([2048, 4096, 8192, 16384, 32768, 65536]),
                req_gpus=1 if partition == "gpu" else 0,
                req_time_seconds=random.choice([3600, 7200, 14400, 28800, 86400]),
                runtime_seconds=int(random.uniform(0.1, 0.9) * random.choice([3600, 7200, 14400])),
                avg_cpu_percent=efficiency * 100,
                peak_cpu_percent=min(100, efficiency * 100 + 20),
                avg_memory_gb=random.uniform(1, 8),
                peak_memory_gb=random.uniform(2, 12),
                nfs_ratio=random.uniform(0, 1),
                health_score=efficiency,
            )
            self._jobs.append(job)

            # Insert into database
            c.execute("""INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                str(job.job_id), job.user, job.partition, job.node,
                f"job_{job.job_id}", job.state, job.exit_code, 0, 0,
                job.submit_time.isoformat(), job.start_time.isoformat(), job.end_time.isoformat(),
                job.req_cpus, job.req_mem_mb, job.req_gpus,
                job.req_time_seconds, job.runtime_seconds,
                int((job.start_time - job.submit_time).total_seconds()),
            ))

            c.execute("""INSERT INTO job_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                str(job.job_id), job.peak_cpu_percent, job.peak_memory_gb,
                job.avg_cpu_percent, job.avg_memory_gb, job.avg_io_wait_percent,
                job.total_nfs_read_gb, job.total_nfs_write_gb,
                job.total_local_read_gb, job.total_local_write_gb,
                job.nfs_ratio, 1 if job.used_gpu else 0, job.health_score,
            ))

        # Insert group memberships
        for group_name, members in self.groups.items():
            for username in members:
                c.execute("""INSERT OR REPLACE INTO group_membership VALUES (?, ?, ?, ?)""",
                          (username, group_name, hash(group_name) % 10000, self.name))

        conn.commit()
        conn.close()

    def get_scontrol_output(self) -> str:
        """Generate complete scontrol show nodes output."""
        return "\n".join(node.scontrol_output() for node in self._nodes)

    @contextmanager
    def mock_subprocess(self) -> Generator[None, None, None]:
        """Context manager that mocks subprocess calls to return cluster data."""
        def mock_run(cmd, *args, **kwargs):
            result = MagicMock()
            result.returncode = 0

            cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd

            if "scontrol show node" in cmd_str:
                result.stdout = self.get_scontrol_output()
            elif "squeue" in cmd_str:
                result.stdout = ""  # Empty queue
            elif "sacct" in cmd_str:
                result.stdout = ""  # Use database instead
            else:
                result.stdout = ""

            return result

        with patch("subprocess.run", side_effect=mock_run):
            yield

    def get_job(self, job_id: int) -> MockJob | None:
        """Get a specific job by ID."""
        return next((j for j in self._jobs if j.job_id == job_id), None)

    def get_user_jobs(self, user: str) -> list[MockJob]:
        """Get all jobs for a user."""
        return [j for j in self._jobs if j.user == user]

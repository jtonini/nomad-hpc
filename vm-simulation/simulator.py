#!/usr/bin/env python3
"""
NØMAÐ Job Simulator

Generates realistic HPC job data based on cluster configuration.
Outputs to SQLite database compatible with NØMAÐ dashboard.

Usage:
    python simulator.py --config clusters/small.toml --jobs 1000 --output test.db
    python simulator.py --config clusters/large.toml --jobs 50000 --days 30
"""

import argparse
import sqlite3
import random
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any
import json

# Try to import toml
try:
    import tomllib
except ImportError:
    try:
        import toml as tomllib
    except ImportError:
        print("ERROR: Please install toml: pip install toml")
        sys.exit(1)


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class Node:
    """Represents a cluster node."""
    name: str
    node_type: str  # cpu, highmem, gpu, gpu_large
    cores: int
    memory_gb: int
    cluster: str = "default"
    gpus: int = 0
    gpu_type: str = ""
    local_disk_gb: int = 500
    partitions: list = field(default_factory=list)
    is_flaky: bool = False
    is_intermittent: bool = False
    flaky_rate: float = 0.0


@dataclass
class Partition:
    """Represents a SLURM partition."""
    name: str
    nodes: list
    max_time_seconds: int
    priority: int = 0
    is_default: bool = False


@dataclass
class User:
    """Represents a cluster user with behavioral profile."""
    name: str
    profile: str  # careful, normal, careless, student, etc.
    failure_rate: float
    nfs_heavy_prob: float
    time_overrequest: float
    gpu_preference: bool = False
    job_types: list = field(default_factory=list)


@dataclass
class Job:
    """Represents a simulated job."""
    job_id: str
    user_name: str
    partition: str
    node_list: str
    job_name: str
    state: str
    exit_code: int | None
    exit_signal: int | None
    failure_reason: int
    submit_time: datetime
    start_time: datetime
    end_time: datetime
    req_cpus: int
    req_mem_mb: int
    req_gpus: int
    req_time_seconds: int
    runtime_seconds: int
    wait_time_seconds: int
    # I/O metrics
    nfs_write_gb: float
    local_write_gb: float
    nfs_read_gb: float
    local_read_gb: float
    io_wait_pct: float
    # Computed
    health_score: float
    nfs_ratio: float


# ============================================================================
# Cluster Configuration Loader
# ============================================================================

class ClusterConfig:
    """Loads and manages cluster configuration from TOML."""
    
    def __init__(self, config_path: str):
        self.config_path = Path(config_path)
        self.config = self._load_config()
        
        self.name = self.config.get('cluster', {}).get('name', 'unknown')
        self.description = self.config.get('cluster', {}).get('description', '')
        
        self.nodes = self._build_nodes()
        self.partitions = self._build_partitions()
        self.users = self._build_users()
        
        print(f"Loaded cluster: {self.name}")
        print(f"  Nodes: {len(self.nodes)}")
        print(f"  Partitions: {len(self.partitions)}")
        print(f"  Users: {len(self.users)}")
    
    def _load_config(self) -> dict:
        """Load TOML configuration file."""
        with open(self.config_path, 'rb') as f:
            if hasattr(tomllib, 'load'):
                return tomllib.load(f)
            else:
                # Fallback for older toml library
                f.seek(0)
                return tomllib.loads(f.read().decode('utf-8'))
    
    def _build_nodes(self) -> list[Node]:
        """Build list of nodes from config."""
        nodes = []
        sim_config = self.config.get('simulation', {})
        flaky_nodes = set(sim_config.get('flaky_nodes', []))
        intermittent_nodes = set(sim_config.get('intermittent_nodes', []))
        flaky_rate = sim_config.get('flaky_failure_rate', 0.15)
        intermittent_rate = sim_config.get('intermittent_failure_rate', 0.08)
        
        for node_type, node_config in self.config.get('nodes', {}).items():
            prefix = node_config.get('prefix', node_type)
            count = node_config.get('count', 1)
            cores = node_config.get('cores', 64)
            memory_gb = node_config.get('memory_gb', 256)
            gpus = node_config.get('gpus', 0)
            gpu_type = node_config.get('gpu_type', '')
            local_disk = node_config.get('local_disk_gb', 500)
            partitions = node_config.get('partitions', [])
            cluster = node_config.get("cluster", "default")
            
            # Generate node names
            digits = len(str(count))
            for i in range(1, count + 1):
                name = f"{prefix}{i:0{digits}d}"
                
                is_flaky = name in flaky_nodes
                is_intermittent = name in intermittent_nodes
                extra_rate = flaky_rate if is_flaky else (intermittent_rate if is_intermittent else 0)
                
                nodes.append(Node(
                    name=name,
                    node_type=node_type,
                    cluster=cluster,
                    cores=cores,
                    memory_gb=memory_gb,
                    gpus=gpus,
                    gpu_type=gpu_type,
                    local_disk_gb=local_disk,
                    partitions=partitions,
                    is_flaky=is_flaky,
                    is_intermittent=is_intermittent,
                    flaky_rate=extra_rate,
                ))
        
        return nodes
    
    def _build_partitions(self) -> list[Partition]:
        """Build list of partitions from config."""
        partitions = []
        
        for part_name, part_config in self.config.get('partitions', {}).items():
            max_time_str = part_config.get('max_time', '7-00:00:00')
            max_time_sec = self._parse_time(max_time_str)
            
            partitions.append(Partition(
                name=part_name,
                nodes=part_config.get('nodes', []),
                max_time_seconds=max_time_sec,
                priority=part_config.get('priority', 0),
                is_default=part_config.get('default', False),
            ))
        
        return partitions
    
    def _build_users(self) -> list[User]:
        """Build list of users with profiles."""
        users = []
        sim_config = self.config.get('simulation', {})
        user_names = sim_config.get('users', {}).get('names', ['user1', 'user2', 'user3'])
        profiles = sim_config.get('user_profiles', {})
        
        # Default profiles if none specified
        if not profiles:
            profiles = {
                'normal': {
                    'failure_rate': 0.12,
                    'nfs_heavy_prob': 0.3,
                    'time_overrequest': 1.5,
                }
            }
        
        profile_names = list(profiles.keys())
        
        for name in user_names:
            # Assign profile based on user name patterns
            if 'student' in name.lower():
                profile_name = 'student' if 'student' in profiles else random.choice(profile_names)
            elif name in ['admin', 'test', 'benchmark']:
                profile_name = 'expert' if 'expert' in profiles else 'careful' if 'careful' in profiles else profile_names[0]
            else:
                profile_name = random.choice(profile_names)
            
            profile = profiles.get(profile_name, profiles[profile_names[0]])
            
            users.append(User(
                name=name,
                profile=profile_name,
                failure_rate=profile.get('failure_rate', 0.12),
                nfs_heavy_prob=profile.get('nfs_heavy_prob', 0.3),
                time_overrequest=profile.get('time_overrequest', 1.5),
                gpu_preference=profile.get('gpu_preference', False),
                job_types=profile.get('job_types', []),
            ))
        
        return users
    
    def _parse_time(self, time_str: str) -> int:
        """Parse SLURM time format to seconds."""
        try:
            days = 0
            if '-' in time_str:
                day_part, time_part = time_str.split('-', 1)
                days = int(day_part)
            else:
                time_part = time_str
            
            parts = time_part.split(':')
            if len(parts) == 3:
                hours, minutes, seconds = map(int, parts)
            elif len(parts) == 2:
                hours, minutes = map(int, parts)
                seconds = 0
            else:
                return int(parts[0])
            
            return days * 86400 + hours * 3600 + minutes * 60 + seconds
        except:
            return 86400  # Default to 1 day
    
    def get_nodes_for_partition(self, partition_name: str) -> list[Node]:
        """Get all nodes that belong to a partition."""
        return [n for n in self.nodes if partition_name in n.partitions]
    
    def get_partition_by_name(self, name: str) -> Partition | None:
        """Get partition by name."""
        for p in self.partitions:
            if p.name == name:
                return p
        return None


# ============================================================================
# Job Simulator
# ============================================================================

class JobSimulator:
    """Generates realistic HPC job data."""
    
    # Failure reason codes
    FAILURE_SUCCESS = 0
    FAILURE_TIMEOUT = 1
    FAILURE_CANCELLED = 2
    FAILURE_FAILED = 3
    FAILURE_OOM = 4
    FAILURE_SEGFAULT = 5
    FAILURE_NODE_FAIL = 6
    FAILURE_DEPENDENCY = 7
    
    def __init__(self, cluster: ClusterConfig, seed: int = None):
        self.cluster = cluster
        if seed:
            random.seed(seed)
        
        self.job_counter = 1000
    
    def generate_jobs(
        self,
        n_jobs: int,
        days: int = 7,
        start_date: datetime = None,
    ) -> list[Job]:
        """Generate n_jobs over the specified time period."""
        
        if start_date is None:
            start_date = datetime.now() - timedelta(days=days)
        
        end_date = start_date + timedelta(days=days)
        total_seconds = int((end_date - start_date).total_seconds())
        
        jobs = []
        for i in range(n_jobs):
            job = self._generate_single_job(start_date, total_seconds)
            jobs.append(job)
            
            if (i + 1) % 1000 == 0:
                print(f"  Generated {i + 1}/{n_jobs} jobs...")
        
        # Sort by submit time
        jobs.sort(key=lambda j: j.submit_time)
        
        return jobs
    
    def _generate_single_job(self, start_date: datetime, total_seconds: int) -> Job:
        """Generate a single job with realistic attributes."""
        
        # Pick user
        user = random.choice(self.cluster.users)
        
        # Pick partition based on user preferences
        partition = self._pick_partition(user)
        partition_obj = self.cluster.get_partition_by_name(partition)
        max_time = partition_obj.max_time_seconds if partition_obj else 86400
        
        # Pick node(s)
        available_nodes = self.cluster.get_nodes_for_partition(partition)
        if not available_nodes:
            available_nodes = self.cluster.nodes
        node = random.choice(available_nodes)
        
        # Generate resource requests
        req_cpus = self._pick_cpus(node, partition)
        req_mem_mb = self._pick_memory(node, req_cpus)
        req_gpus = self._pick_gpus(node, user)
        
        # Generate actual runtime (before we know if it fails)
        base_runtime = random.randint(60, min(max_time, 86400))
        runtime_seconds = int(base_runtime * random.uniform(0.1, 1.2))
        
        # Requested time (users overestimate)
        req_time_seconds = min(
            int(runtime_seconds * user.time_overrequest * random.uniform(0.8, 1.5)),
            max_time
        )
        
        # Generate I/O patterns
        nfs_write, local_write, nfs_read, local_read, io_wait = self._generate_io(user, node)
        
        # Determine if job fails and why
        state, exit_code, exit_signal, failure_reason = self._determine_outcome(
            user, node, runtime_seconds, req_time_seconds, nfs_write, req_mem_mb
        )
        
        # Adjust runtime for failures
        if failure_reason == self.FAILURE_TIMEOUT:
            runtime_seconds = req_time_seconds  # Hit the limit
        elif failure_reason in (self.FAILURE_OOM, self.FAILURE_SEGFAULT):
            runtime_seconds = int(runtime_seconds * random.uniform(0.1, 0.8))
        elif failure_reason == self.FAILURE_FAILED:
            runtime_seconds = int(runtime_seconds * random.uniform(0.05, 0.9))
        elif failure_reason == self.FAILURE_CANCELLED:
            runtime_seconds = int(runtime_seconds * random.uniform(0.1, 0.5))
        elif failure_reason == self.FAILURE_NODE_FAIL:
            runtime_seconds = int(runtime_seconds * random.uniform(0.2, 0.7))
        
        # Generate timestamps
        submit_offset = random.randint(0, total_seconds)
        submit_time = start_date + timedelta(seconds=submit_offset)
        wait_time_seconds = int(random.expovariate(1/300))  # Exponential with mean 5 min
        wait_time_seconds = min(wait_time_seconds, 7200)  # Cap at 2 hours
        start_time = submit_time + timedelta(seconds=wait_time_seconds)
        end_time = start_time + timedelta(seconds=runtime_seconds)
        
        # Compute health score
        nfs_ratio = nfs_write / max(nfs_write + local_write, 0.001)
        health_score = self._compute_health_score(
            failure_reason, nfs_ratio, io_wait, runtime_seconds, req_time_seconds
        )
        
        # Generate job ID
        self.job_counter += 1
        job_id = str(self.job_counter)
        
        # Generate job name
        job_name = self._generate_job_name(user, partition, req_gpus > 0)
        
        return Job(
            job_id=job_id,
            user_name=user.name,
            partition=partition,
            node_list=node.name,
            job_name=job_name,
            state=state,
            exit_code=exit_code,
            exit_signal=exit_signal,
            failure_reason=failure_reason,
            submit_time=submit_time,
            start_time=start_time,
            end_time=end_time,
            req_cpus=req_cpus,
            req_mem_mb=req_mem_mb,
            req_gpus=req_gpus,
            req_time_seconds=req_time_seconds,
            runtime_seconds=runtime_seconds,
            wait_time_seconds=wait_time_seconds,
            nfs_write_gb=nfs_write,
            local_write_gb=local_write,
            nfs_read_gb=nfs_read,
            local_read_gb=local_read,
            io_wait_pct=io_wait,
            health_score=health_score,
            nfs_ratio=nfs_ratio,
        )
    
    def _pick_partition(self, user: User) -> str:
        """Pick a partition based on user preferences."""
        if user.job_types:
            # User has preferred partition types
            available = [p.name for p in self.cluster.partitions if p.name in user.job_types]
            if available:
                return random.choice(available)
        
        if user.gpu_preference:
            gpu_partitions = [p.name for p in self.cluster.partitions if 'gpu' in p.name.lower()]
            if gpu_partitions:
                return random.choice(gpu_partitions)
        
        # Default: weighted by whether partition is default
        weights = [10 if p.is_default else 1 for p in self.cluster.partitions]
        return random.choices([p.name for p in self.cluster.partitions], weights=weights)[0]
    
    def _pick_cpus(self, node: Node, partition: str) -> int:
        """Pick number of CPUs to request."""
        max_cpus = node.cores
        
        # Common patterns
        if 'short' in partition or 'debug' in partition:
            return random.choice([1, 2, 4])
        
        choices = [1, 2, 4, 8, 16, 32, 64]
        choices = [c for c in choices if c <= max_cpus]
        weights = [8, 6, 5, 4, 3, 2, 1][:len(choices)]
        
        return random.choices(choices, weights=weights)[0]
    
    def _pick_memory(self, node: Node, cpus: int) -> int:
        """Pick memory to request (in MB)."""
        max_mem_mb = node.memory_gb * 1024
        mem_per_cpu = max_mem_mb // node.cores
        
        # Request proportional to CPUs, with some variation
        base_mem = mem_per_cpu * cpus
        return int(base_mem * random.uniform(0.5, 1.5))
    
    def _pick_gpus(self, node: Node, user: User) -> int:
        """Pick number of GPUs to request."""
        if node.gpus == 0:
            return 0
        
        if not user.gpu_preference and random.random() > 0.7:
            return 0  # Not all GPU node jobs use GPUs
        
        return random.choice([1, 2, 4, min(4, node.gpus)])
    
    def _generate_io(self, user: User, node: Node) -> tuple:
        """Generate I/O metrics based on user behavior."""
        # NFS-heavy users write more to network storage
        if random.random() < user.nfs_heavy_prob:
            # NFS-heavy job
            nfs_write = random.uniform(10, 200)
            local_write = random.uniform(0, 20)
            io_wait = random.uniform(10, 60)
        else:
            # Local-heavy job (good behavior)
            nfs_write = random.uniform(0, 30)
            local_write = random.uniform(10, 100)
            io_wait = random.uniform(0, 20)
        
        # Read patterns
        nfs_read = random.uniform(0, nfs_write * 2)
        local_read = random.uniform(0, local_write * 2)
        
        return nfs_write, local_write, nfs_read, local_read, io_wait
    
    def _determine_outcome(
        self,
        user: User,
        node: Node,
        runtime: int,
        req_time: int,
        nfs_write: float,
        req_mem_mb: int,
    ) -> tuple[str, int | None, int | None, int]:
        """Determine job outcome based on various factors."""
        
        # Base failure rate from user profile
        fail_prob = user.failure_rate
        
        # Increase failure rate for flaky nodes
        if node.is_flaky or node.is_intermittent:
            fail_prob += node.flaky_rate
        
        # Increase failure rate for NFS-heavy jobs
        if nfs_write > 100:
            fail_prob += 0.10
        elif nfs_write > 50:
            fail_prob += 0.05
        
        # Determine if job succeeds
        if random.random() > fail_prob:
            return 'COMPLETED', 0, None, self.FAILURE_SUCCESS
        
        # Job fails - determine why
        # Weights for different failure types
        failure_weights = {
            self.FAILURE_TIMEOUT: 25,
            self.FAILURE_CANCELLED: 10,
            self.FAILURE_FAILED: 30,
            self.FAILURE_OOM: 15,
            self.FAILURE_SEGFAULT: 10,
            self.FAILURE_NODE_FAIL: 8 if node.is_flaky else 2,
            self.FAILURE_DEPENDENCY: 2,
        }
        
        # Adjust weights based on conditions
        if runtime > req_time * 0.9:
            failure_weights[self.FAILURE_TIMEOUT] *= 3
        
        if req_mem_mb > node.memory_gb * 800:  # Close to node memory
            failure_weights[self.FAILURE_OOM] *= 2
        
        if nfs_write > 100:
            failure_weights[self.FAILURE_TIMEOUT] *= 1.5  # I/O delays cause timeouts
        
        # Pick failure type
        failure_types = list(failure_weights.keys())
        weights = list(failure_weights.values())
        failure_reason = random.choices(failure_types, weights=weights)[0]
        
        # Determine state, exit_code, signal
        if failure_reason == self.FAILURE_TIMEOUT:
            return 'TIMEOUT', 0, None, failure_reason
        elif failure_reason == self.FAILURE_CANCELLED:
            return 'CANCELLED', None, 15, failure_reason  # SIGTERM
        elif failure_reason == self.FAILURE_FAILED:
            return 'FAILED', random.choice([1, 2, 127, 255]), None, failure_reason
        elif failure_reason == self.FAILURE_OOM:
            return 'OUT_OF_MEMORY', 137, 9, failure_reason  # SIGKILL
        elif failure_reason == self.FAILURE_SEGFAULT:
            return 'FAILED', 139, 11, failure_reason  # SIGSEGV
        elif failure_reason == self.FAILURE_NODE_FAIL:
            return 'NODE_FAIL', None, None, failure_reason
        elif failure_reason == self.FAILURE_DEPENDENCY:
            return 'FAILED', 1, None, failure_reason
        
        return 'FAILED', 1, None, self.FAILURE_FAILED
    
    def _compute_health_score(
        self,
        failure_reason: int,
        nfs_ratio: float,
        io_wait: float,
        runtime: int,
        req_time: int,
    ) -> float:
        """Compute job health score (0-1)."""
        if failure_reason != self.FAILURE_SUCCESS:
            # Failed jobs get low scores
            return random.uniform(0.0, 0.4)
        
        score = 1.0
        
        # Penalize high NFS ratio
        if nfs_ratio > 0.7:
            score -= 0.3
        elif nfs_ratio > 0.5:
            score -= 0.15
        
        # Penalize high I/O wait
        if io_wait > 40:
            score -= 0.2
        elif io_wait > 20:
            score -= 0.1
        
        # Penalize poor time estimation
        time_efficiency = runtime / max(req_time, 1)
        if time_efficiency < 0.3:
            score -= 0.15  # Vastly overestimated time
        elif time_efficiency > 0.95:
            score -= 0.1  # Cutting it close
        
        return max(0.0, min(1.0, score + random.uniform(-0.1, 0.1)))
    
    def _generate_job_name(self, user: User, partition: str, uses_gpu: bool) -> str:
        """Generate a realistic job name."""
        prefixes = ['job', 'run', 'sim', 'calc', 'proc', 'batch', 'task']
        
        if 'student' in user.name:
            prefixes.extend(['homework', 'project', 'test', 'lab'])
        
        if uses_gpu:
            prefixes.extend(['train', 'model', 'ml', 'nn', 'gpu_run'])
        
        if 'highmem' in partition:
            prefixes.extend(['analyze', 'genome', 'assembly'])
        
        prefix = random.choice(prefixes)
        suffix = random.randint(1, 999)
        
        return f"{prefix}_{suffix}"


# ============================================================================
# Database Writer
# ============================================================================

class DatabaseWriter:
    """Writes simulated jobs to SQLite database."""
    
    SCHEMA = """
    -- Jobs table (simplified for simulation)
    CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        user_name TEXT NOT NULL,
        group_name TEXT,
        partition TEXT,
        node_list TEXT,
        job_name TEXT,
        submit_time DATETIME,
        start_time DATETIME,
        end_time DATETIME,
        state TEXT,
        exit_code INTEGER,
        exit_signal INTEGER,
        failure_reason INTEGER DEFAULT 0,
        req_cpus INTEGER,
        req_mem_mb INTEGER,
        req_gpus INTEGER,
        req_time_seconds INTEGER,
        runtime_seconds INTEGER,
        wait_time_seconds INTEGER
    );
    
    CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_name);
    CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
    CREATE INDEX IF NOT EXISTS idx_jobs_failure ON jobs(failure_reason);
    CREATE INDEX IF NOT EXISTS idx_jobs_partition ON jobs(partition);
    
    -- Job summary table (for dashboard compatibility)
    CREATE TABLE IF NOT EXISTS job_summary (
        job_id TEXT PRIMARY KEY,
        peak_cpu_percent REAL,
        peak_memory_gb REAL,
        avg_cpu_percent REAL,
        avg_memory_gb REAL,
        peak_vram_gb REAL,
        avg_io_wait_percent REAL,
        total_nfs_read_gb REAL,
        total_nfs_write_gb REAL,
        total_local_read_gb REAL,
        total_local_write_gb REAL,
        nfs_ratio REAL,
        used_gpu BOOLEAN,
        had_swap BOOLEAN,
        health_score REAL,
        cluster_id INTEGER,
        is_anomaly BOOLEAN DEFAULT FALSE,
        FOREIGN KEY (job_id) REFERENCES jobs(job_id)
    );
    
    -- Nodes table
    CREATE TABLE IF NOT EXISTS nodes (
        hostname TEXT PRIMARY KEY,
        cluster TEXT,
        partition TEXT,
        status TEXT NOT NULL,
        cpu_count INTEGER,
        gpu_count INTEGER,
        memory_mb INTEGER,
        last_seen DATETIME
    );
    
    -- Schema version
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        description TEXT
    );
    
    INSERT OR IGNORE INTO schema_version (version, description) 
    VALUES (1, 'Simulation schema');
    """
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """Initialize database with schema."""
        conn = sqlite3.connect(self.db_path)
        conn.executescript(self.SCHEMA)
        conn.commit()
        conn.close()
    
    def write_jobs(self, jobs: list[Job]):
        """Write jobs to database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for job in jobs:
            # Insert into jobs table
            cursor.execute("""
                INSERT OR REPLACE INTO jobs
                (job_id, user_name, partition, node_list, job_name, state,
                 exit_code, exit_signal, failure_reason,
                 submit_time, start_time, end_time,
                 req_cpus, req_mem_mb, req_gpus, req_time_seconds,
                 runtime_seconds, wait_time_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job.job_id, job.user_name, job.partition, job.node_list,
                job.job_name, job.state, job.exit_code, job.exit_signal,
                job.failure_reason,
                job.submit_time.isoformat(), job.start_time.isoformat(),
                job.end_time.isoformat(),
                job.req_cpus, job.req_mem_mb, job.req_gpus, job.req_time_seconds,
                job.runtime_seconds, job.wait_time_seconds,
            ))
            
            # Insert into job_summary table
            cursor.execute("""
                INSERT OR REPLACE INTO job_summary
                (job_id, peak_cpu_percent, peak_memory_gb, avg_cpu_percent,
                 avg_memory_gb, avg_io_wait_percent,
                 total_nfs_read_gb, total_nfs_write_gb,
                 total_local_read_gb, total_local_write_gb,
                 nfs_ratio, used_gpu, health_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job.job_id,
                random.uniform(20, 95),  # peak_cpu_percent
                job.req_mem_mb / 1024 * random.uniform(0.3, 0.9),  # peak_memory_gb
                random.uniform(15, 80),  # avg_cpu_percent
                job.req_mem_mb / 1024 * random.uniform(0.2, 0.7),  # avg_memory_gb
                job.io_wait_pct,
                job.nfs_read_gb,
                job.nfs_write_gb,
                job.local_read_gb,
                job.local_write_gb,
                job.nfs_ratio,
                job.req_gpus > 0,
                job.health_score,
            ))
        
        conn.commit()
        conn.close()
        print(f"Wrote {len(jobs)} jobs to {self.db_path}")
    
    def write_nodes(self, nodes: list[Node]):
        """Write nodes to database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        for node in nodes:
            cursor.execute("""
                INSERT OR REPLACE INTO nodes
                (hostname, cluster, partition, status, cpu_count, gpu_count, memory_mb, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                node.name,
                node.cluster,
                ','.join(node.partitions),
                'UP' if not node.is_flaky else 'DRAIN',
                node.cores,
                node.gpus,
                node.memory_gb * 1024,
                datetime.now().isoformat(),
            ))
        
        conn.commit()
        conn.close()
        print(f"Wrote {len(nodes)} nodes to {self.db_path}")


# ============================================================================
# Statistics Reporter
# ============================================================================

def print_statistics(jobs: list[Job], cluster: ClusterConfig):
    """Print summary statistics about generated jobs."""
    
    total = len(jobs)
    success = sum(1 for j in jobs if j.failure_reason == 0)
    
    # Count by failure reason
    failure_counts = {}
    failure_names = {
        0: 'SUCCESS', 1: 'TIMEOUT', 2: 'CANCELLED', 3: 'FAILED',
        4: 'OOM', 5: 'SEGFAULT', 6: 'NODE_FAIL', 7: 'DEPENDENCY'
    }
    for j in jobs:
        fr = j.failure_reason
        failure_counts[fr] = failure_counts.get(fr, 0) + 1
    
    print("\n" + "=" * 60)
    print("SIMULATION SUMMARY")
    print("=" * 60)
    print(f"Cluster: {cluster.name}")
    print(f"Nodes: {len(cluster.nodes)}")
    print(f"Jobs: {total}")
    print(f"Success Rate: {success/total*100:.1f}%")
    print()
    print("Failure Distribution:")
    for fr, count in sorted(failure_counts.items()):
        name = failure_names.get(fr, f'UNKNOWN({fr})')
        pct = count / total * 100
        bar = '█' * int(pct / 2)
        print(f"  {name:12} {count:6} ({pct:5.1f}%) {bar}")
    
    # Jobs by partition
    print("\nJobs by Partition:")
    partition_counts = {}
    for j in jobs:
        partition_counts[j.partition] = partition_counts.get(j.partition, 0) + 1
    for part, count in sorted(partition_counts.items(), key=lambda x: -x[1]):
        print(f"  {part:12} {count:6} ({count/total*100:.1f}%)")
    
    # I/O patterns
    nfs_heavy = sum(1 for j in jobs if j.nfs_ratio > 0.5)
    print(f"\nNFS-heavy jobs: {nfs_heavy} ({nfs_heavy/total*100:.1f}%)")
    
    # Time range
    first = min(j.submit_time for j in jobs)
    last = max(j.end_time for j in jobs)
    print(f"\nTime range: {first.strftime('%Y-%m-%d')} to {last.strftime('%Y-%m-%d')}")
    print("=" * 60)


# ============================================================================
# Main
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='NØMAÐ Job Simulator - Generate realistic HPC job data',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --config clusters/small.toml --jobs 1000
  %(prog)s --config clusters/large.toml --jobs 50000 --days 30
  %(prog)s --config clusters/medium.toml --jobs 5000 --output my_test.db
        """
    )
    
    parser.add_argument(
        '--config', '-c',
        required=True,
        help='Path to cluster configuration TOML file'
    )
    parser.add_argument(
        '--jobs', '-n',
        type=int,
        default=1000,
        help='Number of jobs to generate (default: 1000)'
    )
    parser.add_argument(
        '--days', '-d',
        type=int,
        default=7,
        help='Number of days to simulate (default: 7)'
    )
    parser.add_argument(
        '--output', '-o',
        default='nomad.db',
        help='Output database path (default: nomad.db)'
    )
    parser.add_argument(
        '--seed', '-s',
        type=int,
        default=None,
        help='Random seed for reproducibility'
    )
    
    args = parser.parse_args()
    
    # Load cluster config
    print(f"Loading cluster configuration from {args.config}...")
    cluster = ClusterConfig(args.config)
    
    # Create simulator
    print(f"\nGenerating {args.jobs} jobs over {args.days} days...")
    simulator = JobSimulator(cluster, seed=args.seed)
    jobs = simulator.generate_jobs(args.jobs, days=args.days)
    
    # Write to database
    print(f"\nWriting to {args.output}...")
    writer = DatabaseWriter(args.output)
    writer.write_nodes(cluster.nodes)
    writer.write_jobs(jobs)
    
    # Print statistics
    print_statistics(jobs, cluster)
    
    print(f"\nDone! Database saved to: {args.output}")
    print(f"View with: python -m nomad.cli dashboard --database {args.output}")


if __name__ == '__main__':
    main()

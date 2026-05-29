# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAD Demo Mode

Generates synthetic HPC job data for testing and demonstration.
Allows reviewers and users to test NØMAD without a real HPC cluster.

Usage:
    nomad demo              # Generate data and launch dashboard
    nomad demo --jobs 500   # Generate 500 jobs
    nomad demo --no-launch  # Generate only, don't launch dashboard
"""

import random
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# ============================================================================
# Embedded Cluster Configuration (no external files needed)
# ============================================================================

DEMO_CLUSTER = {
    "name": "demo-cluster",
    "description": "NØMAD demo cluster with 10 nodes",
    "nodes": [
        {"name": "node01", "cores": 32, "memory_gb": 128, "gpus": 0, "partition": "compute"},
        {"name": "node02", "cores": 32, "memory_gb": 128, "gpus": 0, "partition": "compute"},
        {"name": "node03", "cores": 32, "memory_gb": 128, "gpus": 0, "partition": "compute"},
        {"name": "node04", "cores": 32, "memory_gb": 128, "gpus": 0, "partition": "compute"},
        {"name": "node05", "cores": 32, "memory_gb": 128, "gpus": 0, "partition": "compute"},
        {"name": "node06", "cores": 32, "memory_gb": 128, "gpus": 0, "partition": "compute"},
        {"name": "node07", "cores": 64, "memory_gb": 512, "gpus": 0, "partition": "highmem"},
        {"name": "node08", "cores": 64, "memory_gb": 512, "gpus": 0, "partition": "highmem"},
        {"name": "gpu01", "cores": 32, "memory_gb": 256, "gpus": 4, "partition": "gpu"},
        {"name": "gpu02", "cores": 32, "memory_gb": 256, "gpus": 4, "partition": "gpu"},
    ],
    "users": ["alice", "bob", "charlie", "diana", "eve", "frank"],
    "cloud_instances": [
        {"name": "ml-train-01",   "instance_id": "i-0a1b2c3d4e5f6a7b8", "instance_type": "p3.2xlarge",   "az": "us-east-1a", "gpus": 1},
        {"name": "ml-train-02",   "instance_id": "i-1b2c3d4e5f6a7b8c9", "instance_type": "p3.2xlarge",   "az": "us-east-1b", "gpus": 1},
        {"name": "data-proc-01",  "instance_id": "i-2c3d4e5f6a7b8c9d0", "instance_type": "c5.4xlarge",   "az": "us-east-1a", "gpus": 0},
        {"name": "data-proc-02",  "instance_id": "i-3d4e5f6a7b8c9d0e1", "instance_type": "c5.4xlarge",   "az": "us-east-1a", "gpus": 0},
        {"name": "burst-compute", "instance_id": "i-4e5f6a7b8c9d0e1f2", "instance_type": "c5.9xlarge",   "az": "us-east-1b", "gpus": 0},
        {"name": "gpu-inference",  "instance_id": "i-5f6a7b8c9d0e1f2a3", "instance_type": "g4dn.xlarge", "az": "us-east-1a", "gpus": 1},
    ],
    "job_names": [
        "analysis", "simulation", "training", "inference", "preprocessing",
        "postprocess", "benchmark", "test_run", "production", "debug",
        "md_sim", "dft_calc", "genome_align", "image_proc", "data_clean",
    ],
}


@dataclass
class Job:
    """Simulated job."""
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
    nfs_write_gb: float
    local_write_gb: float
    io_wait_pct: float
    health_score: float
    nfs_ratio: float


class DemoGenerator:
    """Generates realistic synthetic HPC job data."""

    def __init__(self, seed: int | None = None):
        if seed is not None:
            random.seed(seed)
        self.job_counter = 1000

    def generate_jobs(self, n_jobs: int, days: int = 7) -> list[Job]:
        """Generate n_jobs over the specified number of days."""
        jobs = []
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)

        for _ in range(n_jobs):
            job = self._generate_job(start_time, end_time)
            jobs.append(job)

        jobs.sort(key=lambda j: j.submit_time)
        return jobs

    def _generate_job(self, start_range: datetime, end_range: datetime) -> Job:
        """Generate a single realistic job."""
        self.job_counter += 1
        job_id = str(self.job_counter)

        user = random.choice(DEMO_CLUSTER["users"])
        job_name = random.choice(DEMO_CLUSTER["job_names"])

        # User behavior profile
        user_skill = hash(user) % 3
        base_failure_rate = [0.05, 0.12, 0.25][user_skill]
        nfs_heavy_prob = [0.1, 0.3, 0.6][user_skill]

        # Pick partition
        if "gpu" in job_name or "training" in job_name or "inference" in job_name:
            partition = "gpu"
            node = random.choice([n for n in DEMO_CLUSTER["nodes"] if n["partition"] == "gpu"])
            req_gpus = random.choice([1, 2, 4])
        elif "highmem" in job_name or "genome" in job_name:
            partition = "highmem"
            node = random.choice([n for n in DEMO_CLUSTER["nodes"] if n["partition"] == "highmem"])
            req_gpus = 0
        else:
            partition = "compute"
            node = random.choice([n for n in DEMO_CLUSTER["nodes"] if n["partition"] == "compute"])
            req_gpus = 0

        req_cpus = random.choice([1, 2, 4, 8, 16, 32])
        req_mem_mb = req_cpus * random.randint(2000, 8000)
        req_time_seconds = random.choice([3600, 7200, 14400, 28800, 86400, 172800, 604800])

        submit_time = start_range + timedelta(
            seconds=random.uniform(0, (end_range - start_range).total_seconds())
        )
        wait_time_seconds = int(random.expovariate(1/300))
        start_time = submit_time + timedelta(seconds=wait_time_seconds)

        # Flaky nodes
        if "03" in node["name"] or "gpu01" in node["name"]:
            base_failure_rate += 0.1

        failure_roll = random.random()
        if failure_roll < base_failure_rate:
            failure_type = random.choices(
                [1, 2, 3, 4, 5, 6],
                weights=[0.25, 0.15, 0.25, 0.20, 0.10, 0.05],
            )[0]

            if failure_type == 1:  # TIMEOUT
                runtime_seconds = req_time_seconds
                state, exit_code, exit_signal = "TIMEOUT", None, 9
            elif failure_type == 2:  # CANCELLED
                runtime_seconds = int(req_time_seconds * random.uniform(0.1, 0.8))
                state, exit_code, exit_signal = "CANCELLED", None, 15
            elif failure_type == 4:  # OOM
                runtime_seconds = int(req_time_seconds * random.uniform(0.2, 0.9))
                state, exit_code, exit_signal = "OUT_OF_MEMORY", None, 9
            elif failure_type == 5:  # SEGFAULT
                runtime_seconds = int(req_time_seconds * random.uniform(0.01, 0.5))
                state, exit_code, exit_signal = "FAILED", 139, 11
            elif failure_type == 6:  # NODE_FAIL
                runtime_seconds = int(req_time_seconds * random.uniform(0.1, 0.9))
                state, exit_code, exit_signal = "NODE_FAIL", None, None
            else:  # FAILED
                runtime_seconds = int(req_time_seconds * random.uniform(0.1, 0.9))
                state, exit_code, exit_signal = "FAILED", random.choice([1, 2, 127, 255]), None

            failure_reason = failure_type
        else:
            runtime_seconds = int(req_time_seconds * random.uniform(0.3, 0.95))
            state, exit_code, exit_signal = "COMPLETED", 0, None
            failure_reason = 0

        end_time = start_time + timedelta(seconds=runtime_seconds)

        # I/O patterns
        is_nfs_heavy = random.random() < nfs_heavy_prob
        total_write_gb = runtime_seconds / 3600 * random.uniform(0.1, 5.0)
        nfs_ratio = random.uniform(0.5, 0.95) if is_nfs_heavy else random.uniform(0.01, 0.3)
        nfs_write_gb = total_write_gb * nfs_ratio
        local_write_gb = total_write_gb * (1 - nfs_ratio)
        io_wait_pct = nfs_ratio * random.uniform(5, 30) if is_nfs_heavy else random.uniform(0, 5)

        health_score = random.uniform(0.7, 1.0) - (nfs_ratio * 0.2) if failure_reason == 0 else random.uniform(0.1, 0.5)

        return Job(
            job_id=job_id, user_name=user, partition=partition, node_list=node["name"],
            job_name=f"{job_name}_{job_id}", state=state, exit_code=exit_code,
            exit_signal=exit_signal, failure_reason=failure_reason,
            submit_time=submit_time, start_time=start_time, end_time=end_time,
            req_cpus=req_cpus, req_mem_mb=req_mem_mb, req_gpus=req_gpus,
            req_time_seconds=req_time_seconds, runtime_seconds=runtime_seconds,
            wait_time_seconds=wait_time_seconds, nfs_write_gb=nfs_write_gb,
            local_write_gb=local_write_gb, io_wait_pct=io_wait_pct,
            health_score=health_score, nfs_ratio=nfs_ratio,
        )


class DemoDatabase:
    """Creates and populates a demo database."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    def _init_schema(self):
        """Create database schema."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute("""CREATE TABLE IF NOT EXISTS nodes (
            hostname TEXT PRIMARY KEY, cluster TEXT, partition TEXT, status TEXT,
            cpu_count INTEGER, gpu_count INTEGER, memory_mb INTEGER, last_seen DATETIME)""")

        c.execute("""CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT NOT NULL, cluster TEXT NOT NULL, user_name TEXT, partition TEXT, node_list TEXT,
            job_name TEXT, state TEXT, exit_code INTEGER, exit_signal INTEGER,
            failure_reason INTEGER, submit_time DATETIME, start_time DATETIME,
            end_time DATETIME, req_cpus INTEGER, req_mem_mb INTEGER, req_gpus INTEGER,
            req_time_seconds INTEGER, runtime_seconds INTEGER, wait_time_seconds INTEGER,
            PRIMARY KEY (job_id, cluster))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_end_time ON jobs(end_time)")

        c.execute("""CREATE TABLE IF NOT EXISTS job_summary (
            job_id TEXT NOT NULL, cluster TEXT NOT NULL, peak_cpu_percent REAL, peak_memory_gb REAL,
            avg_cpu_percent REAL, avg_memory_gb REAL, avg_io_wait_percent REAL,
            total_nfs_read_gb REAL, total_nfs_write_gb REAL,
            total_local_read_gb REAL, total_local_write_gb REAL,
            nfs_ratio REAL, used_gpu INTEGER, health_score REAL,
            PRIMARY KEY (job_id, cluster))""")

        c.execute("""CREATE TABLE IF NOT EXISTS node_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL,
            node_name TEXT NOT NULL, state TEXT, cpus_total INTEGER, cpus_alloc INTEGER,
            cpu_load REAL, memory_total_mb INTEGER, memory_alloc_mb INTEGER,
            memory_free_mb INTEGER, cpu_alloc_percent REAL, memory_alloc_percent REAL,
            cluster TEXT DEFAULT 'demo-cluster', partitions TEXT, reason TEXT, features TEXT, gres TEXT, is_healthy INTEGER)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_node_state_ts ON node_state(timestamp)")

        # Proficiency scores for edu tracking
        c.execute("""CREATE TABLE IF NOT EXISTS proficiency_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            job_id TEXT NOT NULL,
            user_name TEXT NOT NULL,
            cluster TEXT DEFAULT 'default',
            cpu_score REAL, cpu_level TEXT,
            memory_score REAL, memory_level TEXT,
            time_score REAL, time_level TEXT,
            io_score REAL, io_level TEXT,
            gpu_score REAL, gpu_level TEXT, gpu_applicable INTEGER,
            overall_score REAL, overall_level TEXT,
            needs_work TEXT, strengths TEXT,
            UNIQUE(job_id))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_prof_user ON proficiency_scores(user_name)")

        # Group membership for edu module
        c.execute("""CREATE TABLE IF NOT EXISTS group_membership (
            username TEXT, group_name TEXT, gid INTEGER, cluster TEXT,
            PRIMARY KEY (username, group_name, cluster))""")

        # Populate with demo users in demo groups
        demo_groups = [
            ("alice", "cs101", 2001, "demo"),
            ("alice", "research", 3001, "demo"),
            ("bob", "cs101", 2001, "demo"),
            ("charlie", "cs101", 2001, "demo"),
            ("charlie", "physics-lab", 3002, "demo"),
            ("diana", "cs101", 2001, "demo"),
            ("diana", "bio301", 2002, "demo"),
            ("eve", "bio301", 2002, "demo"),
            ("eve", "research", 3001, "demo"),
        ]
        for username, group_name, gid, cluster in demo_groups:
            c.execute("""INSERT OR REPLACE INTO group_membership 
                (username, group_name, gid, cluster) VALUES (?, ?, ?, ?)""",
                (username, group_name, gid, cluster))


        # Job accounting for Resources tab
        c.execute("""CREATE TABLE IF NOT EXISTS job_accounting (
            job_id TEXT NOT NULL, cluster TEXT NOT NULL, username TEXT, account TEXT,
            partition TEXT, state TEXT, elapsed_sec INTEGER, alloc_cpus INTEGER,
            mem_gb REAL, gpu_count INTEGER DEFAULT 0, cpu_hours REAL DEFAULT 0,
            gpu_hours REAL DEFAULT 0, submit_time TEXT,
            collected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (job_id, cluster))""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jacct_user ON job_accounting(username)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jacct_submit ON job_accounting(submit_time)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jacct_cluster ON job_accounting(cluster)")

        # Interactive servers for RStudio/Jupyter tab
        c.execute("""CREATE TABLE IF NOT EXISTS interactive_servers (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT,
            method TEXT NOT NULL, ssh_host TEXT, ssh_user TEXT,
            enabled BOOLEAN DEFAULT TRUE, last_collection DATETIME)""")

        # Interactive sessions
        c.execute("""CREATE TABLE IF NOT EXISTS interactive_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL,
            server_id TEXT NOT NULL, user TEXT NOT NULL, session_type TEXT NOT NULL,
            pid INTEGER, cpu_percent REAL, mem_percent REAL, mem_mb REAL,
            mem_virtual_mb REAL, start_time DATETIME, age_hours REAL, is_idle BOOLEAN)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_int_sess_ts ON interactive_sessions(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_int_sess_user ON interactive_sessions(user)")

        # Interactive summary
        c.execute("""CREATE TABLE IF NOT EXISTS interactive_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL,
            server_id TEXT NOT NULL, total_sessions INTEGER, idle_sessions INTEGER,
            total_memory_mb REAL, unique_users INTEGER, rstudio_sessions INTEGER,
            jupyter_python_sessions INTEGER, jupyter_r_sessions INTEGER,
            stale_sessions INTEGER, memory_hog_sessions INTEGER)""")

        # GPU stats
        c.execute("""CREATE TABLE IF NOT EXISTS gpu_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL,
            node_name TEXT, gpu_index INTEGER, gpu_name TEXT, gpu_util_percent REAL,
            memory_util_percent REAL, memory_used_mb INTEGER, memory_total_mb INTEGER,
            temperature_c INTEGER, power_draw_w REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_gpu_stats_ts ON gpu_stats(timestamp)")
        c.execute("""CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            severity TEXT,
            source TEXT,
            host TEXT,
            message TEXT,
            details TEXT,
            resolved INTEGER DEFAULT 0,
            resolved_at TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_severity ON alerts(severity)")
        c.execute("""CREATE TABLE IF NOT EXISTS cloud_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            node_name TEXT NOT NULL,
            cluster TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            value REAL NOT NULL,
            unit TEXT NOT NULL,
            source TEXT NOT NULL,
            instance_type TEXT,
            availability_zone TEXT,
            tags TEXT,
            cost_usd REAL)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cloud_ts ON cloud_metrics(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cloud_node ON cloud_metrics(node_name, timestamp)")
        conn.commit()
        conn.close()

    def write_nodes(self):
        """Write demo cluster nodes."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        now = datetime.now().isoformat()

        for node in DEMO_CLUSTER["nodes"]:
            c.execute("""INSERT OR REPLACE INTO nodes
                (hostname, cluster, partition, status, cpu_count, gpu_count, memory_mb, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (node["name"], "demo", node["partition"], "UP", node["cores"],
                 node["gpus"], node["memory_gb"] * 1024, now))

            c.execute("""INSERT INTO node_state
                (timestamp, node_name, state, cpus_total, cpus_alloc, cpu_load,
                 memory_total_mb, memory_alloc_mb, memory_free_mb,
                 cpu_alloc_percent, memory_alloc_percent, cluster, partitions, gres, is_healthy)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, node["name"], "idle", node["cores"], random.randint(0, node["cores"]),
                 random.uniform(0.1, 2.0), node["memory_gb"] * 1024,
                 random.randint(0, node["memory_gb"] * 512),
                 random.randint(node["memory_gb"] * 256, node["memory_gb"] * 1024),
                 random.uniform(10, 80), random.uniform(20, 70), "demo", node["partition"],
                 f"gpu:{node['gpus']}" if node["gpus"] > 0 else "", 1))

        conn.commit()
        conn.close()

    def write_jobs(self, jobs: list[Job]):
        """Write jobs to database."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        for job in jobs:
            cluster_name = DEMO_CLUSTER["name"]
            c.execute("""INSERT OR REPLACE INTO jobs
                (job_id, cluster, user_name, partition, node_list, job_name, state,
                 exit_code, exit_signal, failure_reason, submit_time, start_time,
                 end_time, req_cpus, req_mem_mb, req_gpus, req_time_seconds,
                 runtime_seconds, wait_time_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job.job_id, cluster_name, job.user_name, job.partition, job.node_list,
                 job.job_name, job.state, job.exit_code, job.exit_signal,
                 job.failure_reason, job.submit_time.isoformat(),
                 job.start_time.isoformat(), job.end_time.isoformat(),
                 job.req_cpus, job.req_mem_mb, job.req_gpus, job.req_time_seconds,
                 job.runtime_seconds, job.wait_time_seconds))

            c.execute("""INSERT OR REPLACE INTO job_summary
                (job_id, cluster, peak_cpu_percent, peak_memory_gb, avg_cpu_percent,
                 avg_memory_gb, avg_io_wait_percent, total_nfs_read_gb,
                 total_nfs_write_gb, total_local_read_gb, total_local_write_gb,
                 nfs_ratio, used_gpu, health_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job.job_id, cluster_name, random.uniform(20, 95),
                 job.req_mem_mb / 1024 * random.uniform(0.3, 0.9),
                 random.uniform(15, 80),
                 job.req_mem_mb / 1024 * random.uniform(0.2, 0.7),
                 job.io_wait_pct, job.nfs_write_gb * random.uniform(0.1, 0.5),
                 job.nfs_write_gb, job.local_write_gb * random.uniform(0.1, 0.5),
                 job.local_write_gb, job.nfs_ratio, 1 if job.req_gpus > 0 else 0,
                 job.health_score))
        conn.commit()
        conn.close()

    def write_job_accounting(self, jobs: list[Job]):
        """Write job accounting data for Resources tab."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        cluster_name = DEMO_CLUSTER["name"]
        for job in jobs:
            cpu_hours = (job.runtime_seconds / 3600) * job.req_cpus
            gpu_hours = (job.runtime_seconds / 3600) * job.req_gpus if job.req_gpus > 0 else 0
            c.execute("""INSERT OR REPLACE INTO job_accounting
                (job_id, cluster, username, account, partition, state, elapsed_sec,
                 alloc_cpus, mem_gb, gpu_count, cpu_hours, gpu_hours, submit_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job.job_id, cluster_name, job.user_name, "default", job.partition,
                 job.state, job.runtime_seconds, job.req_cpus, job.req_mem_mb / 1024,
                 job.req_gpus, cpu_hours, gpu_hours, job.submit_time.isoformat()))
        conn.commit()
        conn.close()

    def write_interactive_sessions(self):
        """Write demo interactive sessions for RStudio/Jupyter tab."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        now = datetime.now()

        # Create demo servers
        servers = [
            ("rstudio-server", "RStudio Server", "Demo RStudio instance", "local"),
            ("jupyter-hub", "JupyterHub", "Demo JupyterHub instance", "local"),
        ]
        for sid, name, desc, method in servers:
            c.execute("""INSERT OR REPLACE INTO interactive_servers
                (id, name, description, method, enabled, last_collection)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (sid, name, desc, method, True, now.isoformat()))

        # Create demo sessions
        users = DEMO_CLUSTER["users"]
        session_types = ["RStudio", "Jupyter (Python)", "Jupyter (R)"]
        for i, user in enumerate(users[:4]):
            server_id = "rstudio-server" if i % 2 == 0 else "jupyter-hub"
            session_type = session_types[i % 3]
            start_time = now - timedelta(hours=random.uniform(1, 48))
            age_hours = (now - start_time).total_seconds() / 3600
            is_idle = random.random() > 0.6
            c.execute("""INSERT INTO interactive_sessions
                (timestamp, server_id, user, session_type, pid, cpu_percent,
                 mem_percent, mem_mb, mem_virtual_mb, start_time, age_hours, is_idle)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now.isoformat(), server_id, user, session_type, 10000 + i,
                 random.uniform(0, 25), random.uniform(5, 40),
                 random.uniform(500, 8000), random.uniform(1000, 16000),
                 start_time.isoformat(), age_hours, is_idle))

        # Write summary
        rstudio_count = sum(1 for u in users[:4] if users.index(u) % 2 == 0)
        jupyter_py = sum(1 for i, u in enumerate(users[:4]) if i % 3 == 1)
        jupyter_r = sum(1 for i, u in enumerate(users[:4]) if i % 3 == 2)
        idle_count = sum(1 for _ in range(4) if random.random() > 0.6)
        total_mem = sum(random.uniform(500, 8000) for _ in range(4))
        c.execute("""INSERT INTO interactive_summary
            (timestamp, server_id, total_sessions, idle_sessions, total_memory_mb,
             unique_users, rstudio_sessions, jupyter_python_sessions, jupyter_r_sessions,
             stale_sessions, memory_hog_sessions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (now.isoformat(), "demo", 4, idle_count, total_mem,
             4, 2, 1, 1, 0, 0))
        conn.commit()
        conn.close()

    def write_gpu_stats(self, interval_minutes: int = 15):
        """Write a realistic GPU time series for GPU + energy monitoring.

        Generates DCGM-style samples across the job window (one every
        `interval_minutes`) for each GPU, alternating between busy sessions
        and idle gaps. Power is physically tied to utilization
        (P = idle_floor + (TDP - idle_floor) * util), so idle GPUs draw a
        realistic ~15% TDP floor rather than nothing -- which is what makes
        `nomad energy` show real, non-zero GPU idle waste.
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        now = datetime.now()

        # Ensure new columns exist (migration-safe)
        for col, col_type in [
            ("real_util_pct", "REAL"), ("workload_class", "TEXT"),
            ("data_source", "TEXT"), ("node_name", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE gpu_stats ADD COLUMN {col} {col_type}")
            except Exception:
                pass

        # GPU TDP by model substring (watts) for the power model.
        gpu_tdp_table = {"H100": 700, "A100": 400, "A40": 300,
                         "RTX 6000 ADA": 300, "RTX 6000": 300, "V100": 300, "L40": 300}

        def _tdp(name):
            n = (name or "").upper()
            for key, watts in gpu_tdp_table.items():
                if key in n:
                    return watts
            return 350

        # Busy workload profiles: (class, smi_range, real_ratio, temp_range).
        # real_ratio models the DCGM "real utilization" gap below nvidia-smi.
        busy_profiles = [
            ("tensor-heavy compute", (70, 98), 0.90, (65, 82)),
            ("tensor compute",       (50, 80), 0.75, (58, 75)),
            ("FP64 / HPC compute",   (60, 90), 0.85, (60, 78)),
            ("compute-active",       (40, 75), 0.65, (50, 70)),
            ("memory-bound",         (30, 60), 0.50, (48, 65)),
        ]

        # Window from the jobs already written; fall back to the last 7 days.
        row = c.execute("SELECT MIN(start_time), MAX(end_time) FROM jobs").fetchone()
        try:
            win_end = datetime.fromisoformat(str(row[1])) if row and row[1] else now
            win_start = datetime.fromisoformat(str(row[0])) if row and row[0] else now - timedelta(days=7)
        except (ValueError, TypeError):
            win_end, win_start = now, now - timedelta(days=7)

        gpu_nodes = [n for n in DEMO_CLUSTER["nodes"] if n["gpus"] > 0]
        step = timedelta(minutes=interval_minutes)
        gpu_name = "NVIDIA A100-SXM4-40GB"
        tdp = _tdp(gpu_name)
        idle_floor = round(0.15 * tdp)

        samples = []
        for node in gpu_nodes:
            for gpu_idx in range(node["gpus"]):
                t = win_start
                busy = random.random() < 0.6
                block_end = t + timedelta(
                    hours=random.uniform(1, 5) if busy else random.uniform(0.5, 6))
                profile = random.choice(busy_profiles)
                while t < win_end:
                    if t >= block_end:                      # flip busy <-> idle
                        busy = not busy
                        block_end = t + timedelta(
                            hours=random.uniform(1, 5) if busy else random.uniform(0.5, 6))
                        if busy:
                            profile = random.choice(busy_profiles)
                    if busy:
                        cls, smi_range, real_ratio, temp_range = profile
                        smi = round(random.uniform(*smi_range), 1)
                    else:
                        cls, real_ratio, temp_range = "idle", 0.2, (35, 45)
                        smi = round(random.uniform(0, 8), 1)
                    real = max(0.0, min(100.0, round(smi * real_ratio + random.gauss(0, 2), 1)))
                    power = idle_floor + (tdp - idle_floor) * (smi / 100.0) + random.gauss(0, tdp * 0.02)
                    power = round(max(idle_floor * 0.8, min(tdp * 1.02, power)), 1)
                    mem = random.randint(8000, 38000) if smi > 10 else random.randint(512, 2000)
                    samples.append(
                        (t.isoformat(), node["name"], gpu_idx, gpu_name, smi,
                         round(mem / 40960 * 100, 1), mem, 40960,
                         random.randint(*temp_range), power, real, cls, "dcgm"))
                    t += step

        c.executemany("""INSERT INTO gpu_stats
            (timestamp, node_name, gpu_index, gpu_name, gpu_util_percent,
             memory_util_percent, memory_used_mb, memory_total_mb,
             temperature_c, power_draw_w,
             real_util_pct, workload_class, data_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", samples)

        # Also write gpu_health records (one per GPU at window end)
        try:
            c.execute("""CREATE TABLE IF NOT EXISTS gpu_health (
                timestamp DATETIME NOT NULL, node TEXT NOT NULL, gpu_id INTEGER NOT NULL,
                pcie_replay_count INTEGER DEFAULT 0, pcie_replay_rate_per_sec REAL DEFAULT 0,
                ecc_correctable_total INTEGER DEFAULT 0, ecc_uncorrectable_total INTEGER DEFAULT 0,
                rows_remapped_correctable INTEGER DEFAULT 0, rows_remapped_uncorrectable INTEGER DEFAULT 0,
                rows_remapped_pending INTEGER DEFAULT 0, row_remap_failure INTEGER DEFAULT 0,
                health_status TEXT DEFAULT 'OK',
                PRIMARY KEY (timestamp, node, gpu_id)
            )""")
            for node in gpu_nodes:
                for gpu_idx in range(node["gpus"]):
                    health = random.choices(["OK", "WARN", "HOT"], weights=[85, 10, 5])[0]
                    pcie_rate = round(random.uniform(0.01, 0.5), 3) if health == "WARN" else 0.0
                    c.execute("""INSERT OR REPLACE INTO gpu_health
                        (timestamp, node, gpu_id, pcie_replay_rate_per_sec, health_status)
                        VALUES (?, ?, ?, ?, ?)""",
                        (now.isoformat(), node["name"], gpu_idx, pcie_rate, health))
        except Exception:
            pass

        conn.commit()
        conn.close()

    def write_alerts(self):
        """Write synthetic alert data for demo."""
        import json
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        now = datetime.now()
        nodes = [n["name"] for n in DEMO_CLUSTER["nodes"]]
        alert_templates = [
            ("warning", "disk", "Disk usage at {pct}% on {path}", {"path": "/home", "threshold": 80}),
            ("critical", "disk", "Disk usage critical at {pct}% on {path}", {"path": "/scratch", "threshold": 90}),
            ("warning", "memory", "High memory pressure on {node}", {}),
            ("critical", "job", "Job failure rate elevated: {rate}% in last hour", {"partition": "compute"}),
            ("info", "slurm", "Node {node} returned to service", {}),
            ("warning", "gpu", "GPU temperature {temp}C on {node}", {}),
            ("critical", "memory", "OOM killer invoked on {node}", {}),
            ("info", "disk", "Scrub completed on storage pool", {}),
        ]
        for i in range(40):
            ts = (now - timedelta(hours=random.uniform(0, 168))).isoformat()
            severity, source, msg_tmpl, details = random.choice(alert_templates)
            node = random.choice(nodes)
            msg = msg_tmpl.format(
                pct=random.randint(80, 98),
                path=random.choice(["/home", "/scratch", "/data"]),
                node=node,
                rate=random.randint(10, 40),
                temp=random.randint(80, 95),
            )
            resolved = 1 if random.random() > 0.3 else 0
            resolved_at = (now - timedelta(hours=random.uniform(0, 24))).isoformat() if resolved else None
            c.execute("""INSERT INTO alerts
                (timestamp, severity, source, host, message, details, resolved, resolved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, severity, source, node, msg, json.dumps(details), resolved, resolved_at))
        conn.commit()
        conn.close()

    def write_cloud_metrics(self):
        """Write synthetic cloud metrics for demo.

        Generates realistic AWS CloudWatch-style metrics for a small
        fleet of cloud instances, showing the cross-environment
        monitoring story: on-prem HPC + cloud in the same dashboard.
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        now = datetime.now()
        instances = DEMO_CLUSTER.get("cloud_instances", [])
        if not instances:
            conn.close()
            return

        # Generate 7 days of metrics at 5-minute intervals
        interval_minutes = 5
        samples_per_day = (24 * 60) // interval_minutes
        total_samples = samples_per_day * 7

        # Workload profiles per instance (create realistic patterns)
        profiles = {
            "ml-train-01":   {"cpu_base": 75, "cpu_var": 20, "mem_base": 70, "mem_var": 15, "pattern": "training"},
            "ml-train-02":   {"cpu_base": 80, "cpu_var": 15, "mem_base": 75, "mem_var": 10, "pattern": "training"},
            "data-proc-01":  {"cpu_base": 45, "cpu_var": 30, "mem_base": 35, "mem_var": 20, "pattern": "bursty"},
            "data-proc-02":  {"cpu_base": 40, "cpu_var": 35, "mem_base": 30, "mem_var": 25, "pattern": "bursty"},
            "burst-compute": {"cpu_base": 20, "cpu_var": 60, "mem_base": 25, "mem_var": 40, "pattern": "spiky"},
            "gpu-inference":  {"cpu_base": 30, "cpu_var": 10, "mem_base": 50, "mem_var": 10, "pattern": "steady"},
        }

        # Cost rates per instance type (approximate hourly USD)
        cost_rates = {
            "p3.2xlarge":  3.06,
            "c5.4xlarge":  0.68,
            "c5.9xlarge":  1.53,
            "g4dn.xlarge": 0.526,
        }

        records = []
        for inst in instances:
            name = inst["name"]
            profile = profiles.get(name, {"cpu_base": 50, "cpu_var": 25, "mem_base": 50, "mem_var": 25, "pattern": "steady"})
            hourly_cost = cost_rates.get(inst["instance_type"], 1.0)

            for i in range(total_samples):
                ts = now - timedelta(minutes=(total_samples - i) * interval_minutes)
                hour = ts.hour

                # Time-of-day modulation (research workloads peak daytime)
                if profile["pattern"] == "training":
                    # ML training runs long, slight dip overnight
                    time_mod = 0.85 if 2 <= hour <= 6 else 1.0
                elif profile["pattern"] == "bursty":
                    # Data processing peaks during work hours
                    time_mod = 1.2 if 9 <= hour <= 17 else 0.4
                elif profile["pattern"] == "spiky":
                    # Burst compute — random spikes
                    time_mod = 2.5 if random.random() > 0.85 else 0.3
                else:
                    # Steady inference
                    time_mod = 0.9 + 0.2 * (random.random())

                cpu = max(0, min(100, profile["cpu_base"] * time_mod + random.gauss(0, profile["cpu_var"] * 0.3)))
                mem = max(0, min(100, profile["mem_base"] * time_mod + random.gauss(0, profile["mem_var"] * 0.3)))
                net_in = max(0, random.gauss(50_000_000, 20_000_000) * time_mod)
                net_out = max(0, random.gauss(30_000_000, 15_000_000) * time_mod)

                ts_str = ts.isoformat()
                tags = str({"Environment": "research", "ManagedBy": "nomad"})

                base = (ts_str, inst["name"], "research-aws", "aws",
                        inst["instance_type"], inst["az"], tags)

                records.append(base + ("cpu_util", round(cpu, 1), "percent", None))
                records.append(base + ("mem_util", round(mem, 1), "percent", None))
                records.append(base + ("net_recv_bytes", round(net_in), "bytes", None))
                records.append(base + ("net_send_bytes", round(net_out), "bytes", None))

                # GPU metrics for GPU instances
                if inst.get("gpus", 0) > 0:
                    if profile["pattern"] == "training":
                        gpu_util = max(0, min(100, 85 * time_mod + random.gauss(0, 8)))
                        gpu_mem = max(0, min(100, 75 * time_mod + random.gauss(0, 6)))
                    else:
                        gpu_util = max(0, min(100, 40 + random.gauss(0, 15)))
                        gpu_mem = max(0, min(100, 35 + random.gauss(0, 10)))
                    records.append(base + ("gpu_util", round(gpu_util, 1), "percent", None))
                    records.append(base + ("gpu_mem_util", round(gpu_mem, 1), "percent", None))

            # Daily cost entries (one per day)
            for day_offset in range(7):
                day_ts = now - timedelta(days=day_offset)
                # Cost varies with utilization
                avg_cpu = profile["cpu_base"] / 100.0
                daily_cost = hourly_cost * 24 * (0.5 + 0.5 * avg_cpu)
                daily_cost *= random.uniform(0.9, 1.1)  # small variance
                records.append((
                    day_ts.replace(hour=0, minute=0, second=0).isoformat(),
                    f"EC2/{inst['name']}", "research-aws", "aws",
                    inst["instance_type"], inst["az"], "",
                    "daily_cost_usd", round(daily_cost, 2), "usd", round(daily_cost, 2),
                ))

        c.executemany("""INSERT INTO cloud_metrics
            (timestamp, node_name, cluster, source,
             instance_type, availability_zone, tags,
             metric_name, value, unit, cost_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", records)

        conn.commit()
        conn.close()

        total_instances = len(instances)
        total_metrics = len(records)
        gpu_instances = sum(1 for i in instances if i.get("gpus", 0) > 0)
        print(f"  Cloud instances: {total_instances} ({gpu_instances} GPU)")
        print(f"  Cloud metrics: {total_metrics:,}")

    def write_network_perf(self):
        """Write demo network performance data."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS network_perf (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                source_host TEXT NOT NULL,
                dest_host TEXT NOT NULL,
                path_type TEXT,
                status TEXT,
                ping_min_ms REAL,
                ping_avg_ms REAL,
                ping_max_ms REAL,
                ping_mdev_ms REAL,
                ping_loss_pct REAL,
                throughput_mbps REAL,
                bytes_transferred INTEGER,
                tcp_retrans INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_netperf_timestamp ON network_perf(timestamp)")

        # Generate 1 week of network data (every 30 min = 336 samples per path)
        network_paths = [
            ('head-node', 'nas-01', 'switch'),
            ('head-node', 'nas-01-direct', 'direct'),
        ]

        base_time = datetime.now() - timedelta(days=7)

        for source, dest, path_type in network_paths:
            for i in range(336):  # 7 days * 48 samples/day
                sample_time = base_time + timedelta(minutes=i * 30)
                hour = sample_time.hour
                weekday = sample_time.weekday()

                # Base throughput depends on path type
                if path_type == 'direct':
                    base_throughput = 940  # ~1 Gbps direct wire
                    throughput_var = 20
                else:
                    base_throughput = 800  # Switch path
                    throughput_var = 100
                    # Add business hours congestion (9am-5pm weekdays)
                    if weekday < 5 and 9 <= hour < 17:
                        base_throughput -= random.randint(100, 300)
                        throughput_var = 150

                throughput = max(100, base_throughput + random.randint(-throughput_var, throughput_var))

                # Latency correlates inversely with throughput
                if path_type == 'direct':
                    latency_avg = random.uniform(0.1, 0.5)
                    jitter = random.uniform(0.01, 0.1)
                else:
                    latency_avg = random.uniform(0.5, 2.0)
                    jitter = random.uniform(0.1, 0.5)
                    if weekday < 5 and 9 <= hour < 17:
                        latency_avg += random.uniform(0.5, 2.0)
                        jitter += random.uniform(0.2, 0.5)

                # TCP retransmits - more on congested switch
                if path_type == 'direct':
                    retrans = random.randint(0, 2)
                else:
                    retrans = random.randint(0, 5)
                    if weekday < 5 and 9 <= hour < 17:
                        retrans += random.randint(0, 10)

                # Packet loss - rare
                loss = 0.0 if random.random() > 0.05 else random.uniform(0.1, 1.0)

                status = 'healthy' if throughput > 500 and loss < 1 else 'degraded'

                c.execute("""
                    INSERT INTO network_perf (
                        timestamp, source_host, dest_host, path_type, status,
                        ping_min_ms, ping_avg_ms, ping_max_ms, ping_mdev_ms, ping_loss_pct,
                        throughput_mbps, bytes_transferred, tcp_retrans
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sample_time.isoformat(),
                    source, dest, path_type, status,
                    latency_avg * 0.8, latency_avg, latency_avg * 1.3, jitter, loss,
                    throughput, int(throughput * 1024 * 1024 / 8 * 10), retrans
                ))

        conn.commit()
        conn.close()
        print(f"    Network samples: {336 * len(network_paths)} (2 paths x 7 days)")

    def write_workstation_state(self):
        """Write demo workstation monitoring data.

        Generates data matching the production schema used by
        WorkstationCollector.store() — 29 columns. This schema is also
        what gets synced from jonimitchell.db into combined.db on mingus.
        """
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS workstation_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                hostname TEXT NOT NULL,
                department TEXT,
                status TEXT,
                os_version TEXT,
                cpu_model TEXT,
                uptime_seconds INTEGER,
                load_avg_1m REAL,
                load_avg_5m REAL,
                load_avg_15m REAL,
                cpu_count INTEGER,
                cpu_user_pct REAL,
                cpu_system_pct REAL,
                cpu_idle_pct REAL,
                cpu_iowait_pct REAL,
                memory_total_mb INTEGER,
                memory_used_mb INTEGER,
                memory_free_mb INTEGER,
                memory_cached_mb INTEGER,
                swap_total_mb INTEGER,
                swap_used_mb INTEGER,
                disk_total_gb REAL,
                disk_used_gb REAL,
                disk_free_gb REAL,
                disk_usage_pct REAL,
                users_logged_in INTEGER,
                process_count INTEGER,
                zombie_count INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ws_hostname ON workstation_state(hostname)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ws_timestamp ON workstation_state(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_ws_dept ON workstation_state(department)")

        # Demo workstations by department. Each has stable characteristics
        # (cpu_count, cpu_model, ram_gb, disk_gb) so time-series data looks
        # realistic rather than randomly re-rolled every sample.
        workstations = [
            # Chemistry department
            {"hostname": "chem-ws01", "department": "Chemistry", "os": "Ubuntu 22.04",
             "cpu_count": 8,  "cpu_model": "Intel Xeon E5-2620 v4", "ram_gb": 32, "disk_gb": 500},
            {"hostname": "chem-ws02", "department": "Chemistry", "os": "Ubuntu 22.04",
             "cpu_count": 8,  "cpu_model": "Intel Xeon E5-2620 v4", "ram_gb": 32, "disk_gb": 500},
            {"hostname": "chem-ws03", "department": "Chemistry", "os": "Ubuntu 22.04",
             "cpu_count": 16, "cpu_model": "AMD EPYC 7302P",        "ram_gb": 64, "disk_gb": 1000},
            {"hostname": "chem-ws04", "department": "Chemistry", "os": "Rocky 9.2",
             "cpu_count": 16, "cpu_model": "AMD EPYC 7302P",        "ram_gb": 64, "disk_gb": 1000},
            # Biology department
            {"hostname": "bio-ws01",  "department": "Biology",   "os": "Ubuntu 22.04",
             "cpu_count": 4,  "cpu_model": "Intel Core i7-10700",   "ram_gb": 16, "disk_gb": 500},
            {"hostname": "bio-ws02",  "department": "Biology",   "os": "Ubuntu 22.04",
             "cpu_count": 4,  "cpu_model": "Intel Core i7-10700",   "ram_gb": 16, "disk_gb": 500},
            {"hostname": "bio-ws03",  "department": "Biology",   "os": "Rocky 9.2",
             "cpu_count": 8,  "cpu_model": "Intel Xeon W-2145",     "ram_gb": 32, "disk_gb": 1000},
            # Physics department
            {"hostname": "phys-ws01", "department": "Physics",   "os": "Ubuntu 22.04",
             "cpu_count": 8,  "cpu_model": "Intel Xeon W-2145",     "ram_gb": 32, "disk_gb": 500},
            {"hostname": "phys-ws02", "department": "Physics",   "os": "Rocky 9.2",
             "cpu_count": 16, "cpu_model": "AMD EPYC 7302P",        "ram_gb": 64, "disk_gb": 1000},
            {"hostname": "phys-ws03", "department": "Physics",   "os": "Ubuntu 22.04",
             "cpu_count": 8,  "cpu_model": "Intel Xeon W-2145",     "ram_gb": 32, "disk_gb": 500},
            # Math/CS department
            {"hostname": "mathcs-ws01","department": "Math/CS",  "os": "Ubuntu 22.04",
             "cpu_count": 4,  "cpu_model": "Intel Core i5-10500",   "ram_gb": 16, "disk_gb": 500},
            {"hostname": "mathcs-ws02","department": "Math/CS",  "os": "Ubuntu 22.04",
             "cpu_count": 4,  "cpu_model": "Intel Core i5-10500",   "ram_gb": 16, "disk_gb": 500},
            {"hostname": "mathcs-ws03","department": "Math/CS",  "os": "Rocky 9.2",
             "cpu_count": 8,  "cpu_model": "Intel Xeon W-2145",     "ram_gb": 32, "disk_gb": 500},
            {"hostname": "mathcs-ws04","department": "Math/CS",  "os": "Ubuntu 22.04",
             "cpu_count": 8,  "cpu_model": "Intel Xeon W-2145",     "ram_gb": 32, "disk_gb": 500},
        ]

        base_time = datetime.now() - timedelta(days=7)

        for ws in workstations:
            ram_mb = ws["ram_gb"] * 1024
            disk_gb_total = ws["disk_gb"]
            cpu_count = ws["cpu_count"]

            # Each workstation gets samples every 5 minutes for 7 days
            for i in range(2016):  # 7 days * 288 samples/day (every 5 min)
                sample_time = base_time + timedelta(minutes=i * 5)
                hour = sample_time.hour
                weekday = sample_time.weekday()

                # Status distribution: mostly online, some degraded, rare offline
                if random.random() < 0.02:
                    status = "offline"
                    # Offline: everything zero
                    cpu_user = cpu_system = cpu_iowait = 0.0
                    cpu_idle = 100.0
                    mem_used_pct = 0.0
                    disk_used_pct = 0.0
                    load1 = load5 = load15 = 0.0
                    users = 0
                    uptime = 0
                    procs = 0
                    zombies = 0
                elif random.random() < 0.05:
                    status = "degraded"
                    cpu_user   = random.uniform(60, 80)
                    cpu_system = random.uniform(15, 25)
                    cpu_iowait = random.uniform(2, 8)
                    cpu_idle   = max(0.0, 100 - cpu_user - cpu_system - cpu_iowait)
                    mem_used_pct  = random.uniform(85, 98)
                    disk_used_pct = random.uniform(60, 90)
                    load1  = random.uniform(cpu_count * 1.5, cpu_count * 3)
                    load5  = load1 * random.uniform(0.8, 1.0)
                    load15 = load5 * random.uniform(0.8, 1.0)
                    users  = random.randint(1, 3)
                    uptime = random.randint(3600, 86400 * 30)
                    procs  = random.randint(200, 400)
                    zombies = random.randint(0, 5)
                else:
                    status = "online"
                    # Higher usage during work hours on weekdays
                    if weekday < 5 and 9 <= hour < 17:
                        cpu_user   = random.uniform(15, 50)
                        cpu_system = random.uniform(3, 10)
                        cpu_iowait = random.uniform(0.5, 3)
                        mem_used_pct = random.uniform(30, 75)
                        users  = random.randint(0, 2)
                        load1  = random.uniform(0.5, cpu_count * 0.7)
                    else:
                        cpu_user   = random.uniform(0.5, 10)
                        cpu_system = random.uniform(0.3, 3)
                        cpu_iowait = random.uniform(0.1, 1)
                        mem_used_pct = random.uniform(20, 40)
                        users  = random.randint(0, 1) if random.random() > 0.7 else 0
                        load1  = random.uniform(0.05, 1)
                    cpu_idle = max(0.0, 100 - cpu_user - cpu_system - cpu_iowait)
                    disk_used_pct = random.uniform(30, 70)
                    load5  = load1 * random.uniform(0.8, 1.1)
                    load15 = load5 * random.uniform(0.8, 1.0)
                    uptime = random.randint(86400, 86400 * 90)
                    procs  = random.randint(150, 300)
                    zombies = 0 if random.random() > 0.05 else random.randint(1, 3)

                # Derive absolute memory values from the percentage
                mem_used_mb   = int(ram_mb * mem_used_pct / 100)
                mem_cached_mb = int(ram_mb * random.uniform(0.10, 0.25))
                mem_free_mb   = max(0, ram_mb - mem_used_mb - mem_cached_mb)

                # Swap: usually barely used
                swap_total_mb = ws["ram_gb"] * 512   # convention: swap = RAM/2 in MB
                swap_used_mb  = int(swap_total_mb * random.uniform(0, 0.1))

                # Derive absolute disk values from the percentage
                disk_used_gb  = round(disk_gb_total * disk_used_pct / 100, 1)
                disk_free_gb  = round(disk_gb_total - disk_used_gb, 1)

                c.execute("""
                    INSERT INTO workstation_state (
                        timestamp, hostname, department, status,
                        os_version, cpu_model,
                        uptime_seconds, load_avg_1m, load_avg_5m, load_avg_15m,
                        cpu_count, cpu_user_pct, cpu_system_pct, cpu_idle_pct, cpu_iowait_pct,
                        memory_total_mb, memory_used_mb, memory_free_mb, memory_cached_mb,
                        swap_total_mb, swap_used_mb,
                        disk_total_gb, disk_used_gb, disk_free_gb, disk_usage_pct,
                        users_logged_in, process_count, zombie_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sample_time.isoformat(),
                    ws["hostname"], ws["department"], status,
                    ws["os"], ws["cpu_model"],
                    uptime,
                    round(load1, 2), round(load5, 2), round(load15, 2),
                    cpu_count,
                    round(cpu_user, 2), round(cpu_system, 2),
                    round(cpu_idle, 2), round(cpu_iowait, 2),
                    ram_mb, mem_used_mb, mem_free_mb, mem_cached_mb,
                    swap_total_mb, swap_used_mb,
                    float(disk_gb_total), disk_used_gb, disk_free_gb, round(disk_used_pct, 2),
                    users, procs, zombies,
                ))

        conn.commit()
        conn.close()
        print(f"    Workstation samples: {2016 * len(workstations)} (14 hosts x 7 days)")


    def write_storage_state(self):
        """Write demo storage/NAS monitoring data."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS storage_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                hostname TEXT NOT NULL,
                storage_type TEXT,
                status TEXT DEFAULT 'online',
                total_bytes INTEGER,
                used_bytes INTEGER,
                free_bytes INTEGER,
                usage_percent REAL,
                iops_read INTEGER,
                iops_write INTEGER,
                throughput_read_mbps REAL,
                throughput_write_mbps REAL,
                latency_read_ms REAL,
                latency_write_ms REAL,
                nfs_clients_connected INTEGER,
                active_operations INTEGER,
                pools_json TEXT,
                arc_stats_json TEXT,
                nfs_exports_json TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_storage_hostname ON storage_state(hostname)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_storage_timestamp ON storage_state(timestamp)")

        # Demo storage servers
        storage_servers = [
            {
                "hostname": "nas-01",
                "storage_type": "ZFS",
                "total_tb": 100,
                "pools": ["tank", "scratch"],
                "exports": ["/home", "/scratch", "/data/shared"]
            },
            {
                "hostname": "nas-02",
                "storage_type": "ZFS",
                "total_tb": 50,
                "pools": ["archive"],
                "exports": ["/archive", "/backup"]
            },
            {
                "hostname": "fast-scratch",
                "storage_type": "NVMe",
                "total_tb": 20,
                "pools": ["nvme-pool"],
                "exports": ["/fast-scratch"]
            },
        ]

        base_time = datetime.now() - timedelta(days=7)

        for storage in storage_servers:
            total_bytes = storage["total_tb"] * 1024 * 1024 * 1024 * 1024
            base_used_pct = random.uniform(0.4, 0.7)

            # Samples every 5 minutes for 7 days
            for i in range(2016):
                sample_time = base_time + timedelta(minutes=i * 5)
                hour = sample_time.hour
                weekday = sample_time.weekday()

                # Status
                if random.random() < 0.01:
                    status = "degraded"
                else:
                    status = "online"

                # Usage grows slowly over time
                growth = i / 2016 * 0.02  # 2% growth over week
                used_pct = min(0.95, base_used_pct + growth + random.uniform(-0.01, 0.01))
                used_bytes = int(total_bytes * used_pct)
                free_bytes = total_bytes - used_bytes

                # I/O patterns - higher during work hours
                if weekday < 5 and 9 <= hour < 17:
                    iops_read = random.randint(500, 5000)
                    iops_write = random.randint(200, 2000)
                    throughput_read = random.uniform(100, 800)
                    throughput_write = random.uniform(50, 400)
                    nfs_clients = random.randint(20, 80)
                    active_ops = random.randint(10, 100)
                else:
                    iops_read = random.randint(50, 500)
                    iops_write = random.randint(20, 200)
                    throughput_read = random.uniform(10, 100)
                    throughput_write = random.uniform(5, 50)
                    nfs_clients = random.randint(5, 20)
                    active_ops = random.randint(1, 20)

                # NVMe is faster
                if storage["storage_type"] == "NVMe":
                    latency_read = random.uniform(0.1, 0.5)
                    latency_write = random.uniform(0.2, 0.8)
                    iops_read *= 3
                    iops_write *= 3
                else:
                    latency_read = random.uniform(0.5, 3)
                    latency_write = random.uniform(1, 5)

                # JSON fields
                import json
                pools_json = json.dumps([{"name": p, "health": "ONLINE"} for p in storage["pools"]])
                arc_json = json.dumps({"hit_rate": random.uniform(0.85, 0.98), "size_gb": random.uniform(16, 64)})
                exports_json = json.dumps(storage["exports"])

                c.execute("""
                    INSERT INTO storage_state (
                        timestamp, hostname, storage_type, status,
                        total_bytes, used_bytes, free_bytes, usage_percent,
                        iops_read, iops_write, throughput_read_mbps, throughput_write_mbps,
                        latency_read_ms, latency_write_ms, nfs_clients_connected,
                        active_operations, pools_json, arc_stats_json, nfs_exports_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sample_time.isoformat(),
                    storage["hostname"], storage["storage_type"], status,
                    total_bytes, used_bytes, free_bytes, round(used_pct * 100, 1),
                    iops_read, iops_write, round(throughput_read, 1), round(throughput_write, 1),
                    round(latency_read, 2), round(latency_write, 2), nfs_clients,
                    active_ops, pools_json, arc_json, exports_json
                ))

        conn.commit()
        conn.close()
        print(f"    Storage servers: {len(storage_servers)}, {2016 * len(storage_servers)} samples")


    def write_queue_state(self):
        """Write demo SLURM queue state data."""
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS queue_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                partition TEXT NOT NULL,
                pending_jobs INTEGER DEFAULT 0,
                running_jobs INTEGER DEFAULT 0,
                total_jobs INTEGER DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_queue_ts ON queue_state(timestamp)")

        base_time = datetime.now() - timedelta(days=7)
        partitions = {
            "compute": {"base_running": 30, "base_pending": 8},
            "highmem": {"base_running": 5, "base_pending": 2},
            "gpu": {"base_running": 12, "base_pending": 6},
        }

        for i in range(336):  # 7 days * 48 samples/day (every 30 min)
            sample_time = base_time + timedelta(minutes=i * 30)
            hour = sample_time.hour
            weekday = sample_time.weekday()

            for part_name, cfg in partitions.items():
                # Higher activity during business hours
                if weekday < 5 and 9 <= hour < 17:
                    running = cfg["base_running"] + random.randint(-3, 8)
                    pending = cfg["base_pending"] + random.randint(0, 10)
                else:
                    running = max(1, cfg["base_running"] // 2 + random.randint(-2, 3))
                    pending = max(0, random.randint(0, 3))

                total = running + pending
                c.execute("""
                    INSERT INTO queue_state (timestamp, partition, pending_jobs, running_jobs, total_jobs)
                    VALUES (?, ?, ?, ?, ?)
                """, (sample_time.isoformat(), part_name, pending, running, total))

        conn.commit()
        conn.close()
        print(f"    Queue state: {336 * len(partitions)} samples (3 partitions x 7 days)")

    def write_iostat(self):
        """Write demo iostat CPU and device data."""
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS iostat_cpu (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                user_percent REAL,
                system_percent REAL,
                iowait_percent REAL,
                idle_percent REAL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS iostat_device (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                device TEXT NOT NULL,
                read_kb_per_sec REAL,
                write_kb_per_sec REAL,
                read_await_ms REAL,
                write_await_ms REAL,
                util_percent REAL
            )
        """)

        base_time = datetime.now() - timedelta(days=7)
        devices = ["sda", "sdb", "nvme0n1"]

        for i in range(2016):  # 7 days * 288 samples/day (every 5 min)
            sample_time = base_time + timedelta(minutes=i * 5)
            hour = sample_time.hour
            weekday = sample_time.weekday()

            # CPU stats vary by time
            if weekday < 5 and 9 <= hour < 17:
                user_pct = random.uniform(15, 55)
                sys_pct = random.uniform(3, 12)
                iowait = random.uniform(1, 8)
            else:
                user_pct = random.uniform(2, 15)
                sys_pct = random.uniform(1, 5)
                iowait = random.uniform(0.1, 3)

            idle_pct = max(0, 100 - user_pct - sys_pct - iowait)

            c.execute("""
                INSERT INTO iostat_cpu (timestamp, user_percent, system_percent, iowait_percent, idle_percent)
                VALUES (?, ?, ?, ?, ?)
            """, (sample_time.isoformat(), round(user_pct, 1), round(sys_pct, 1),
                  round(iowait, 1), round(idle_pct, 1)))

            # Device stats
            for dev in devices:
                if dev == "nvme0n1":
                    read_kb = random.uniform(500, 50000)
                    write_kb = random.uniform(1000, 80000)
                    read_await = random.uniform(0.05, 0.5)
                    write_await = random.uniform(0.05, 0.8)
                    util = random.uniform(5, 40)
                else:
                    read_kb = random.uniform(100, 20000)
                    write_kb = random.uniform(200, 30000)
                    read_await = random.uniform(0.5, 5)
                    write_await = random.uniform(1, 10)
                    util = random.uniform(2, 60)

                if weekday < 5 and 9 <= hour < 17:
                    write_kb *= 1.5
                    util = min(99, util * 1.3)

                c.execute("""
                    INSERT INTO iostat_device (timestamp, device, read_kb_per_sec, write_kb_per_sec,
                                               read_await_ms, write_await_ms, util_percent)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (sample_time.isoformat(), dev, round(read_kb, 1), round(write_kb, 1),
                      round(read_await, 2), round(write_await, 2), round(util, 1)))

        conn.commit()
        conn.close()
        print(f"    I/O stats: {2016} CPU samples, {2016 * len(devices)} device samples")

    def write_mpstat(self):
        """Write demo mpstat summary data."""
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS mpstat_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                num_cores INTEGER,
                avg_busy_percent REAL,
                max_busy_percent REAL,
                min_busy_percent REAL,
                std_busy_percent REAL,
                busy_spread REAL,
                imbalance_ratio REAL,
                cores_idle INTEGER,
                cores_saturated INTEGER
            )
        """)

        base_time = datetime.now() - timedelta(days=7)
        num_cores = 32

        for i in range(2016):
            sample_time = base_time + timedelta(minutes=i * 5)
            hour = sample_time.hour
            weekday = sample_time.weekday()

            if weekday < 5 and 9 <= hour < 17:
                avg_busy = random.uniform(25, 65)
                std_busy = random.uniform(10, 25)
            else:
                avg_busy = random.uniform(5, 20)
                std_busy = random.uniform(3, 12)

            max_busy = min(100, avg_busy + random.uniform(15, 35))
            min_busy = max(0, avg_busy - random.uniform(10, avg_busy * 0.8))
            spread = max_busy - min_busy
            imbalance = std_busy / max(avg_busy, 0.1)

            cores_idle = int(num_cores * max(0, (1 - avg_busy / 100)) * random.uniform(0.3, 0.7))
            cores_saturated = int(num_cores * (avg_busy / 100) * random.uniform(0, 0.2))

            c.execute("""
                INSERT INTO mpstat_summary (timestamp, num_cores, avg_busy_percent, max_busy_percent,
                                            min_busy_percent, std_busy_percent, busy_spread,
                                            imbalance_ratio, cores_idle, cores_saturated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (sample_time.isoformat(), num_cores, round(avg_busy, 1), round(max_busy, 1),
                  round(min_busy, 1), round(std_busy, 1), round(spread, 1),
                  round(imbalance, 2), cores_idle, cores_saturated))

        conn.commit()
        conn.close()
        print(f"    CPU core stats: {2016} samples ({num_cores} cores)")

    def write_vmstat(self):
        """Write demo vmstat memory data."""
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute("""
            CREATE TABLE IF NOT EXISTS vmstat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                free_kb INTEGER,
                buffer_kb INTEGER,
                cache_kb INTEGER,
                swap_used_kb INTEGER,
                swap_in_kb INTEGER,
                swap_out_kb INTEGER,
                procs_blocked INTEGER,
                memory_pressure REAL
            )
        """)

        base_time = datetime.now() - timedelta(days=7)
        total_mem_kb = 128 * 1024 * 1024  # 128 GB

        for i in range(2016):
            sample_time = base_time + timedelta(minutes=i * 5)
            hour = sample_time.hour
            weekday = sample_time.weekday()

            if weekday < 5 and 9 <= hour < 17:
                used_pct = random.uniform(40, 80)
            else:
                used_pct = random.uniform(15, 40)

            cache_kb = int(total_mem_kb * random.uniform(0.1, 0.25))
            buffer_kb = int(total_mem_kb * random.uniform(0.01, 0.05))
            used_kb = int(total_mem_kb * used_pct / 100)
            free_kb = total_mem_kb - used_kb - cache_kb - buffer_kb
            free_kb = max(1024, free_kb)

            # Swap - occasional light usage
            if used_pct > 70 and random.random() > 0.7:
                swap_used = random.randint(1024, 512 * 1024)  # 1MB - 512MB
                swap_in = random.randint(0, 100)
                swap_out = random.randint(0, 200)
            else:
                swap_used = 0
                swap_in = 0
                swap_out = 0

            procs_blocked = random.randint(0, 2) if used_pct > 60 else 0
            pressure = min(1.0, (used_pct / 100) * random.uniform(0.8, 1.2))

            c.execute("""
                INSERT INTO vmstat (timestamp, free_kb, buffer_kb, cache_kb,
                                    swap_used_kb, swap_in_kb, swap_out_kb,
                                    procs_blocked, memory_pressure)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (sample_time.isoformat(), free_kb, buffer_kb, cache_kb,
                  swap_used, swap_in, swap_out, procs_blocked, round(pressure, 2)))

        conn.commit()
        conn.close()
        print(f"    Memory stats: {2016} samples")


def get_demo_db_path() -> Path:
    """Get path for demo database (in search path for find_database)."""
    return Path.home() / "nomad_demo.db"

def run_demo(
    n_jobs: int = 1000,
    days: int = 7,
    seed: int | None = None,
    launch_dashboard: bool = True,
    port: int = 5000,
) -> str:
    """
    Run NØMAD demo mode.

    Generates synthetic data and optionally launches the dashboard.
    """
    db_path = get_demo_db_path()

    import os; os.system("clear" if os.name != "nt" else "cls")
    print()
    print("  ┌─────────────────────────────────────────┐")
    print("  │                                         │")
    print("  │             N Ø M A D                   │")
    print("  │                                         │")
    print("  │   NØde Monitoring And Diagnostics       │")
    print("  │   ─────────────────────────────────     │")
    print("  │   Demo Mode                             │")
    print("  │                                         │")
    print("  └─────────────────────────────────────────┘")
    print()
    print(f"  Generating {n_jobs} jobs over {days} days...")

    generator = DemoGenerator(seed=seed)
    jobs = generator.generate_jobs(n_jobs, days=days)

    db = DemoDatabase(str(db_path))
    db.write_nodes()
    db.write_jobs(jobs)
    db.write_job_accounting(jobs)
    db.write_interactive_sessions()
    db.write_gpu_stats()
    db.write_alerts()
    db.write_network_perf()
    db.write_workstation_state()
    db.write_storage_state()
    db.write_queue_state()
    db.write_iostat()
    db.write_mpstat()
    db.write_vmstat()
    db.write_cloud_metrics()

    success = sum(1 for j in jobs if j.failure_reason == 0)
    print("\nGenerated:")
    print(f"  On-prem nodes: {len(DEMO_CLUSTER['nodes'])}")
    print(f"  Cloud instances: {len(DEMO_CLUSTER.get('cloud_instances', []))}")
    print(f"  Jobs:  {n_jobs}")
    print(f"  Success rate: {success/n_jobs*100:.1f}%")
    print(f"\nDatabase: {db_path}")
    # Inject stress scenarios for Insight Engine
    try:
        from nomad.insights.inject_stress import inject_stress_scenarios
        inject_stress_scenarios(str(db_path))
    except Exception:
        pass

    if launch_dashboard:
        from nomad.viz.server import serve_dashboard
        serve_dashboard(host="localhost", port=port, db_path=str(db_path))

    return str(db_path)

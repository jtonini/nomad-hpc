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
from typing import Optional


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
    exit_code: Optional[int]
    exit_signal: Optional[int]
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

    def __init__(self, seed: Optional[int] = None):
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
            job_id TEXT PRIMARY KEY, user_name TEXT, partition TEXT, node_list TEXT,
            job_name TEXT, state TEXT, exit_code INTEGER, exit_signal INTEGER,
            failure_reason INTEGER, submit_time DATETIME, start_time DATETIME,
            end_time DATETIME, req_cpus INTEGER, req_mem_mb INTEGER, req_gpus INTEGER,
            req_time_seconds INTEGER, runtime_seconds INTEGER, wait_time_seconds INTEGER)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_end_time ON jobs(end_time)")

        c.execute("""CREATE TABLE IF NOT EXISTS job_summary (
            job_id TEXT PRIMARY KEY, peak_cpu_percent REAL, peak_memory_gb REAL,
            avg_cpu_percent REAL, avg_memory_gb REAL, avg_io_wait_percent REAL,
            total_nfs_read_gb REAL, total_nfs_write_gb REAL,
            total_local_read_gb REAL, total_local_write_gb REAL,
            nfs_ratio REAL, used_gpu INTEGER, health_score REAL)""")

        c.execute("""CREATE TABLE IF NOT EXISTS node_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL,
            node_name TEXT NOT NULL, state TEXT, cpus_total INTEGER, cpus_alloc INTEGER,
            cpu_load REAL, memory_total_mb INTEGER, memory_alloc_mb INTEGER,
            memory_free_mb INTEGER, cpu_alloc_percent REAL, memory_alloc_percent REAL,
            cluster TEXT DEFAULT 'demo', partitions TEXT, reason TEXT, features TEXT, gres TEXT, is_healthy INTEGER)""")
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
            c.execute("""INSERT OR REPLACE INTO jobs
                (job_id, user_name, partition, node_list, job_name, state,
                 exit_code, exit_signal, failure_reason, submit_time, start_time,
                 end_time, req_cpus, req_mem_mb, req_gpus, req_time_seconds,
                 runtime_seconds, wait_time_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job.job_id, job.user_name, job.partition, job.node_list,
                 job.job_name, job.state, job.exit_code, job.exit_signal,
                 job.failure_reason, job.submit_time.isoformat(),
                 job.start_time.isoformat(), job.end_time.isoformat(),
                 job.req_cpus, job.req_mem_mb, job.req_gpus, job.req_time_seconds,
                 job.runtime_seconds, job.wait_time_seconds))

            c.execute("""INSERT OR REPLACE INTO job_summary
                (job_id, peak_cpu_percent, peak_memory_gb, avg_cpu_percent,
                 avg_memory_gb, avg_io_wait_percent, total_nfs_read_gb,
                 total_nfs_write_gb, total_local_read_gb, total_local_write_gb,
                 nfs_ratio, used_gpu, health_score)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job.job_id, random.uniform(20, 95),
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

    def write_gpu_stats(self):
        """Write GPU stats for GPU monitoring."""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        now = datetime.now()
        
        # Get GPU nodes
        gpu_nodes = [n for n in DEMO_CLUSTER["nodes"] if n["gpus"] > 0]
        for node in gpu_nodes:
            for gpu_idx in range(node["gpus"]):
                c.execute("""INSERT INTO gpu_stats
                    (timestamp, node_name, gpu_index, gpu_name, gpu_util_percent,
                     memory_util_percent, memory_used_mb, memory_total_mb,
                     temperature_c, power_draw_w)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (now.isoformat(), node["name"], gpu_idx, "NVIDIA A100",
                     random.uniform(10, 95), random.uniform(20, 80),
                     random.randint(8000, 32000), 40960,
                     random.randint(35, 75), random.uniform(100, 300)))
        conn.commit()
        conn.close()


def get_demo_db_path() -> Path:
    """Get path for demo database (in search path for find_database)."""
    return Path.home() / "nomad_demo.db"


def run_demo(
    n_jobs: int = 1000,
    days: int = 7,
    seed: Optional[int] = None,
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

    success = sum(1 for j in jobs if j.failure_reason == 0)
    print(f"\nGenerated:")
    print(f"  Nodes: {len(DEMO_CLUSTER['nodes'])}")
    print(f"  Jobs:  {n_jobs}")
    print(f"  Success rate: {success/n_jobs*100:.1f}%")
    print(f"\nDatabase: {db_path}")

    if launch_dashboard:
        from nomad.viz.server import serve_dashboard
        serve_dashboard(host="localhost", port=port, db_path=str(db_path))

    return str(db_path)

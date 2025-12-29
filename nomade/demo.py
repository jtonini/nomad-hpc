"""
NØMADE Demo Mode

Generates synthetic HPC job data for testing and demonstration.
Allows reviewers and users to test NØMADE without a real HPC cluster.

Usage:
    nomade demo              # Generate data and launch dashboard
    nomade demo --jobs 500   # Generate 500 jobs
    nomade demo --no-launch  # Generate only, don't launch dashboard
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
    "description": "NØMADE demo cluster with 10 nodes",
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
            partitions TEXT, reason TEXT, features TEXT, gres TEXT, is_healthy INTEGER)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_node_state_ts ON node_state(timestamp)")

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
                 cpu_alloc_percent, memory_alloc_percent, partitions, gres, is_healthy)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, node["name"], "idle", node["cores"], random.randint(0, node["cores"]),
                 random.uniform(0.1, 2.0), node["memory_gb"] * 1024,
                 random.randint(0, node["memory_gb"] * 512),
                 random.randint(node["memory_gb"] * 256, node["memory_gb"] * 1024),
                 random.uniform(10, 80), random.uniform(20, 70), node["partition"],
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


def get_demo_db_path() -> Path:
    """Get path for demo database (in search path for find_database)."""
    return Path.home() / "nomade_demo.db"


def run_demo(
    n_jobs: int = 1000,
    days: int = 7,
    seed: Optional[int] = None,
    launch_dashboard: bool = True,
    port: int = 5000,
) -> str:
    """
    Run NØMADE demo mode.

    Generates synthetic data and optionally launches the dashboard.
    """
    db_path = get_demo_db_path()

    print("NØMADE Demo Mode")
    print("=" * 40)
    print(f"Generating {n_jobs} jobs over {days} days...")

    generator = DemoGenerator(seed=seed)
    jobs = generator.generate_jobs(n_jobs, days=days)

    db = DemoDatabase(str(db_path))
    db.write_nodes()
    db.write_jobs(jobs)

    success = sum(1 for j in jobs if j.failure_reason == 0)
    print(f"\nGenerated:")
    print(f"  Nodes: {len(DEMO_CLUSTER['nodes'])}")
    print(f"  Jobs:  {n_jobs}")
    print(f"  Success rate: {success/n_jobs*100:.1f}%")
    print(f"\nDatabase: {db_path}")

    if launch_dashboard:
        print(f"\nLaunching dashboard on http://localhost:{port}")
        print("Press Ctrl+C to stop\n")
        from nomade.viz.server import serve_dashboard
        serve_dashboard(host="localhost", port=port, db_path=str(db_path))

    return str(db_path)

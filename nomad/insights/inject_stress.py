#!/usr/bin/env python3
"""
Patch demo.py to inject stress scenarios into the demo database.

These scenarios create co-occurring signals that trigger the Level 2
correlator in the Insight Engine:

1. Disk filling + job failures (disk_filling_and_failures)
2. GPU OOM + GPU partition failures (gpu_jobs_out_of_memory)
3. Network latency + job failures (network_issues_and_failures)
4. Queue pressure + high wait times (partition_bottleneck)
5. Cloud cost + underutilized instances (cloud_cost_optimization)
6. Workstation overload + alerts (widespread_workstation_pressure)
"""

import sqlite3
from datetime import datetime, timedelta
import random


# The stress jobs' numbers, far above the generated ones (1001 on).
STRESS_JOBS = 7700000


def _times(end, wait_s, run_s):
    """(submitted, started, ended) as text, for a job that ended at end."""
    start = end - timedelta(seconds=run_s)
    return ((start - timedelta(seconds=wait_s)).isoformat(), start.isoformat(), end.isoformat())


def inject_stress_scenarios(db_path: str):
    """Inject stress data into an existing demo database."""
    conn = sqlite3.connect(db_path)
    try:
        _inject(conn)
        conn.commit()
    finally:
        # Closed whatever happens: a failure left the database locked for
        # the rest of the process (`nomad demo --console` writes to it next).
        conn.close()


def _inject(conn):
    c = conn.cursor()
    now = datetime.now()
    random.seed(42)

    print("  Injecting stress scenarios for Insight Engine correlations...")

    # ── 1. Disk filling scenario ─────────────────────────────────────
    # Storage filling up over the last 12 hours
    for h in range(12):
        ts = (now - timedelta(hours=h)).isoformat()
        usage = 82 + h * 1.5  # 82% -> 100% over 12 hours, going backwards means recent is highest
        usage_recent = 100 - h * 1.5  # Invert: most recent = highest
        actual_usage = min(82 + (12 - h) * 1.5, 98)
        total = 2000 * 1073741824  # 2TB
        used = int(total * actual_usage / 100)
        free = total - used
        c.execute("""INSERT INTO storage_state
            (timestamp, hostname, storage_type, status, total_bytes, used_bytes, free_bytes, usage_percent)
            VALUES (?,?,?,?,?,?,?,?)""",
            (ts, "scratch-nfs", "scratch", "online", total, used, free, actual_usage))

    # ── 2. GPU OOM + partition failures ──────────────────────────────
    # GPU jobs that fail with OOM in the gpu partition
    # (Numbers of their own, replaced when the demo is made again: a second
    # run's duplicates used to undo all of this. Times that add up: submitted,
    # waited, started, ran, ended.)
    for i in range(15):
        end = now - timedelta(hours=random.uniform(0, 48))
        c.execute("""INSERT OR REPLACE INTO jobs
            (job_id, cluster, user_name, partition, state, submit_time, start_time,
             end_time, runtime_seconds, wait_time_seconds, req_cpus, req_gpus, req_mem_mb)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(STRESS_JOBS + i), "demo-cluster", f"mluser{i%3}", "gpu", "OUT_OF_MEMORY",
             *_times(end, 300, 600), 600, 300, 8, random.choice([1, 2]), 32000))

    # ── 3. Network latency spike ─────────────────────────────────────
    for h in range(12):
        ts = (now - timedelta(hours=h)).isoformat()
        latency = 15.0 + random.uniform(0, 10)  # Well above 5ms threshold
        loss = 0.3 + random.uniform(0, 0.5)     # Above 0.1% threshold
        c.execute("""INSERT INTO network_perf
            (timestamp, source_host, dest_host, path_type, status,
             ping_min_ms, ping_avg_ms, ping_max_ms, ping_mdev_ms, ping_loss_pct)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (ts, "head", "scratch-nfs", "storage", "degraded",
             latency * 0.7, latency, latency * 1.5, 2.0, loss))

    # ── 4. Queue pressure ────────────────────────────────────────────
    for h in range(6):
        ts = (now - timedelta(hours=h)).isoformat()
        c.execute("""INSERT INTO queue_state
            (timestamp, partition, pending_jobs, running_jobs, total_jobs)
            VALUES (?,?,?,?,?)""",
            (ts, "gpu", 85, 12, 97))  # 7x ratio, well above 2x threshold

    # High wait times on gpu partition jobs
    for i in range(20):
        end = now - timedelta(hours=random.uniform(0, 12))
        wait = int(random.uniform(7200, 14400))  # 2-4 hours wait, above 1h threshold
        c.execute("""INSERT OR REPLACE INTO jobs
            (job_id, cluster, user_name, partition, state, submit_time, start_time,
             end_time, runtime_seconds, wait_time_seconds, req_cpus, req_gpus, req_mem_mb)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (str(STRESS_JOBS + 100 + i), "demo-cluster", f"gpuuser{i%5}", "gpu", "COMPLETED",
             *_times(end, wait, 3600), 3600, wait, 8, 1, 16000))

    # ── 5. Cloud: underutilized expensive instances ──────────────────
    for h in range(24):
        ts = (now - timedelta(hours=h)).isoformat()
        # Expensive GPU instance doing almost nothing
        c.execute("""INSERT INTO cloud_metrics
            (timestamp, node_name, cluster, metric_name, value, unit, source,
             instance_type, availability_zone)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (ts, "ml-idle-gpu-01", "research-aws", "cpu_utilization", 
             random.uniform(3, 8), "percent", "cloudwatch", "p3.8xlarge", "us-east-1a"))
        c.execute("""INSERT INTO cloud_metrics
            (timestamp, node_name, cluster, metric_name, value, unit, source,
             instance_type, availability_zone)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (ts, "ml-idle-gpu-01", "research-aws", "cost_usd_per_day",
             12.24, "usd", "cloudwatch", "p3.8xlarge", "us-east-1a"))

    # ── 6. Workstation overload ──────────────────────────────────────
    for h in range(6):
        ts = (now - timedelta(hours=h)).isoformat()
        # Two workstations maxed out
        # (The workstation table's own columns: until 1.17.4 these named
        # others, so the insert failed and the scenarios above were lost.)
        for host, cpu, mem, disk, users, loads in (
                ("login-node-01", 92 + random.uniform(0, 5), 94 + random.uniform(0, 4), 45, 8,
                 (32.5, 28.1, 24.3)),
                ("login-node-02", 88 + random.uniform(0, 8), 91 + random.uniform(0, 6), 52, 6,
                 (28.7, 25.2, 22.8))):
            c.execute("""INSERT INTO workstation_state
                (timestamp, hostname, department, status, cpu_count, cpu_user_pct, cpu_idle_pct,
                 memory_total_mb, memory_used_mb, memory_free_mb, disk_usage_pct, users_logged_in,
                 load_avg_1m, load_avg_5m, load_avg_15m)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (ts, host, "research", "online", 32, cpu, 100 - cpu, 131072,
                 int(131072 * mem / 100), int(131072 * (100 - mem) / 100), disk, users, *loads))

    print("  Stress scenarios injected:")
    print("    - Disk: scratch-nfs filling 82%->98% over 12h")
    print("    - GPU: 15 OOM failures in gpu partition")
    print("    - Network: head->scratch-nfs latency 15-25ms + packet loss")
    print("    - Queue: gpu partition 85 pending / 12 running (7x ratio)")
    print("    - Cloud: ml-idle-gpu-01 (p3.8xlarge) at 3-8% CPU, $12/day")
    print("    - Workstations: login-node-01/02 at 90%+ CPU and memory")


if __name__ == "__main__":
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else str(__import__('pathlib').Path.home() / "nomad_demo.db")
    inject_stress_scenarios(db)
    print(f"\nDone. Test with: nomad insights brief --db {db} --cluster demo-cluster --hours 168")

# NØMADE Architecture Summary

## Data Collection Overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                         NØMADE Data Collection v0.2.0                        │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  SYSTEM COLLECTORS (every 60s):                                              │
│  ┌──────────────┬─────────────────────────────────────────────────────────┐  │
│  │ disk         │ Filesystem usage (total, used, free, projections)       │  │
│  │ iostat       │ Device I/O: %iowait, utilization, latency               │  │
│  │ mpstat       │ Per-core CPU: utilization, imbalance detection          │  │
│  │ vmstat       │ Memory pressure, swap activity, blocked processes       │  │
│  │ nfs          │ NFS I/O: ops/sec, throughput, RTT, retransmissions      │  │
│  │ gpu          │ NVIDIA GPU: utilization, memory, temperature, power     │  │
│  └──────────────┴─────────────────────────────────────────────────────────┘  │
│                                                                              │
│  SLURM COLLECTORS (every 60s):                                               │
│  ┌──────────────┬─────────────────────────────────────────────────────────┐  │
│  │ slurm        │ Queue state: pending, running, partition stats          │  │
│  │ job_metrics  │ sacct data: CPU/mem efficiency, health scores           │  │
│  │ node_state   │ Node allocation, drain reasons, CPU load, memory        │  │
│  └──────────────┴─────────────────────────────────────────────────────────┘  │
│                                                                              │
│  JOB MONITOR (every 30s):                                                    │
│  ┌──────────────┬─────────────────────────────────────────────────────────┐  │
│  │ job_monitor  │ Per-job I/O: NFS vs local writes from /proc/[pid]/io    │  │
│  └──────────────┴─────────────────────────────────────────────────────────┘  │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

## Feature Vector (19 dimensions)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                    Feature Vector for Similarity Analysis                   │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  FROM SACCT (job outcome):              FROM IOSTAT (system I/O):           │
│  ┌────────────────────────────────┐     ┌────────────────────────────────┐  │
│  │  1. health_score        [0-1]  │     │ 11. avg_iowait_percent   [0-1] │  │
│  │  2. cpu_efficiency      [0-1]  │     │ 12. peak_iowait_percent  [0-1] │  │
│  │  3. memory_efficiency   [0-1]  │     │ 13. avg_device_util      [0-1] │  │
│  │  4. used_gpu            [0,1]  │     └────────────────────────────────┘  │
│  │  5. had_swap            [0,1]  │                                         │
│  └────────────────────────────────┘     FROM MPSTAT (CPU cores):            │
│                                         ┌────────────────────────────────┐  │
│  FROM JOB_MONITOR (I/O behavior):       │ 14. avg_core_busy        [0-1] │  │
│  ┌────────────────────────────────┐     │ 15. core_imbalance_ratio [0-1] │  │
│  │  6. total_write_gb      [0-1]  │     │ 16. max_core_busy        [0-1] │  │
│  │  7. write_rate_mbps     [0-1]  │     └────────────────────────────────┘  │
│  │  8. nfs_ratio           [0-1]  │                                         │
│  │  9. runtime_minutes     [0-1]  │     FROM VMSTAT (memory pressure):      │
│  │ 10. write_intensity     [0-1]  │     ┌────────────────────────────────┐  │
│  └────────────────────────────────┘     │ 17. avg_memory_pressure  [0-1] │  │
│                                         │ 18. peak_swap_activity   [0-1] │  │
│                                         │ 19. avg_procs_blocked    [0-1] │  │
│                                         └────────────────────────────────┘  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Collector Details

| # | Collector | Source | Key Data | Graceful Skip |
|---|-----------|--------|----------|---------------|
| 1 | `disk` | `shutil.disk_usage` | total/used/free, fill rate projections | No |
| 2 | `slurm` | `squeue`, `sinfo` | pending/running jobs, partition stats | No |
| 3 | `job_metrics` | `sacct` | CPU/mem efficiency, exit codes, health | No |
| 4 | `iostat` | `iostat -x` | %iowait, device util, r/w latency | No |
| 5 | `mpstat` | `mpstat -P ALL` | per-core CPU, imbalance ratio | No |
| 6 | `vmstat` | `vmstat` | swap, memory pressure, blocked procs | No |
| 7 | `node_state` | `scontrol show node` | allocation, drain reasons, load | No |
| 8 | `gpu` | `nvidia-smi` | util, memory, temp, power | Yes (if no GPU) |
| 9 | `nfs` | `nfsiostat` | ops/sec, throughput, RTT | Yes (if no NFS) |
| 10 | `job_monitor` | `/proc/[pid]/io` | per-job NFS vs local I/O | No |

## Database Tables

### System Metrics
- `filesystems` - disk usage snapshots
- `iostat_cpu` - system %iowait
- `iostat_device` - per-device I/O stats
- `mpstat_core` - per-core CPU stats
- `mpstat_summary` - CPU imbalance metrics
- `vmstat` - memory pressure, swap
- `node_state` - SLURM node allocation
- `gpu_stats` - NVIDIA GPU metrics
- `nfs_stats` - NFS I/O metrics

### Job Data
- `jobs` - job metadata from sacct
- `job_metrics` - time-series job stats
- `job_io_samples` - per-job I/O snapshots
- `job_summary` - health scores, feature vectors

### Analysis
- `job_similarity` - pairwise similarity edges
- `clusters` - job cluster profiles
- `alerts` - alert history
- `collection_log` - collector run history

## CLI Commands

```bash
# Core commands
nomade status              # Full system overview
nomade syscheck            # Verify requirements
nomade collect --once      # Single collection cycle
nomade collect -i 60       # Continuous (every 60s)
nomade monitor -i 30       # Job I/O monitor (every 30s)

# Analysis
nomade disk /home          # Filesystem trends
nomade jobs --user X       # Job history
nomade similarity          # Similarity analysis
nomade alerts              # View alerts

# Bash helpers (source scripts/nomade.sh)
nstatus    nwatch    ndisk    njobs    nsimilarity
nalerts    ncollect  nmonitor nsyscheck nlog
```

## Quick Start

```bash
# 1. Initialize database
sqlite3 /var/lib/nomade/nomade.db < nomade/db/schema.sql

# 2. Verify system
nomade syscheck

# 3. Test collection
nomade collect --once

# 4. Start continuous collection
nohup nomade collect -i 60 > /tmp/nomade-collect.log 2>&1 &
nohup nomade monitor -i 30 > /tmp/nomade-monitor.log 2>&1 &

# 5. Check status
nomade status
```

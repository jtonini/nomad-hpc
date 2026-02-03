# NØMADE

**NØde MAnagement DEvice** — A lightweight HPC monitoring, visualization, and predictive analytics tool.

> *"Travels light, adapts to its environment, and doesn't need permanent infrastructure."*

[![PyPI](https://img.shields.io/pypi/v/nomade-hpc.svg)](https://pypi.org/project/nomade-hpc/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)

---

## Overview

NØMADE is a lightweight, self-contained monitoring and prediction system for HPC clusters. Unlike heavyweight monitoring solutions that require complex infrastructure, NØMADE is designed to be deployed quickly, run with minimal resources, and provide actionable insights through real-time alerts, interactive session monitoring, and predictive analytics.

### Key Features

- **Multi-Cluster Dashboard**: Monitor multiple HPC clusters and workstations from a single interface, with nodes grouped by partition and real-time utilization badges
- **Interactive Session Monitoring**: Track RStudio and Jupyter sessions across clusters — identify idle sessions, memory hogs, and stale notebooks
- **Real-time Monitoring**: Track disk usage, SLURM queues, node health, license servers, and job metrics
- **Derivative Analysis**: Detect accelerating trends before they become critical (not just threshold alerts)
- **Predictive Analytics**: ML-based job health prediction using cosine similarity networks
- **3D Visualization**: Interactive Fruchterman-Reingold force-directed network visualization with safe/danger zones
- **Actionable Recommendations**: Data-driven defaults and user-specific suggestions
- **Lightweight**: SQLite database, minimal dependencies, no external services required

### Philosophy

NØMADE is inspired by nomadic principles:
- **Travels light**: Minimal dependencies, single SQLite database, no complex infrastructure
- **Adapts to its environment**: Configurable collectors, flexible alert rules, cluster-agnostic
- **Leaves no trace**: Clean uninstall, no system modifications required (except optional SLURM hooks)

---

## Quick Start

**Try it now (no HPC required):**
```bash
pip install nomade-hpc
nomade demo
```

This generates synthetic data and launches the dashboard at http://localhost:5000, complete with multi-cluster views, partition grouping, and interactive session monitoring.

**For production HPC deployment:**
```bash
pip install nomade-hpc
nomade init
nomade collect    # Start data collection
nomade dashboard  # Launch web interface
```

**Or install from source:**
```bash
git clone https://github.com/jtonini/nomade.git
cd nomade
pip install -e .
nomade demo  # Test with synthetic data
```

---

## Dashboard

NØMADE's web dashboard provides a comprehensive overview of your HPC infrastructure through multiple views:

### Cluster Tabs

Each cluster appears as a top-level tab. Within each cluster, nodes are grouped by partition with:

- **Partition headers** showing name, description, node count, and down-node alerts
- **Utilization badges** — compact CPU, MEM, and GPU usage indicators per partition
- **Job summary** — total jobs, succeeded, and failed counts at a glance
- **Node cards** — color-coded circles reflecting the worst of job success rate and resource pressure (CPU/MEM >75% yellow, >90% red)

Click any node to open a detailed sidebar with job statistics, resource utilization bars, failure breakdown, and top users.

### Interactive Sessions Tab

Monitor RStudio and Jupyter sessions in real time:

- **Summary cards**: Total sessions, idle count, memory usage, unique users
- **Sessions by type**: RStudio, Jupyter (Python/R), Jupyter Server
- **Top users by memory**: Identify resource hogs across all session types
- **Alert panel**: Flags users with excessive idle sessions, stale notebooks, and high memory consumption

### Network View

3D force-directed visualization of job similarity networks:

- **Fruchterman-Reingold layout**: Connected jobs cluster together based on cosine similarity
- **PCA view**: Emergent patterns in the job feature space
- **Axis selection**: Map any feature dimensions to X/Y/Z axes
- **Color coding**: Jobs colored by health score from green (healthy) to red (failing)

### Additional Panels

- **ML Risk Panel**: High-risk job predictions with confidence scores
- **Failed Jobs Modal**: Click any failure category to drill into affected jobs
- **Clustering Quality**: Assortativity, neighborhood purity, SES.MNTD metrics

---

## Interactive Session Monitoring

NØMADE can monitor RStudio Server and JupyterHub sessions, helping administrators identify and manage idle resources.

### CLI Report

```bash
# Full report (Python 3.7+)
nomade report-interactive

# Alerts only
nomade report-interactive --quiet

# JSON output for scripting
nomade report-interactive --json

# Custom thresholds
nomade report-interactive --idle-hours 4 --memory-threshold 2048
```

### Standalone Script (Python 3.6+)

For older systems (e.g., CentOS 7 with Python 3.6):

```bash
./bin/nomade-interactive-report
./bin/nomade-interactive-report --quiet
./bin/nomade-interactive-report --json
```

### Sample Output

```
======================================================================
              Interactive Sessions Report
======================================================================
  Total Sessions: 112
  Idle Sessions:  107 (95.5%)
  Total Memory:   11.09 GB
  Unique Users:   28

  SESSIONS BY TYPE:
  Type                    Total     Idle  Memory (MB)
  -------------------- -------- -------- ------------
  Jupyter (Python)           92       89         7432
  RStudio                    18       16         3201
  Jupyter (R)                 1        1          312

  TOP USERS BY MEMORY:
  User         Sessions  RStudio  Jupyter   Mem (MB)   Idle
  ------------ -------- -------- -------- ---------- ------
  msimpso3           11        0       11       2156     11
  ad3tb              11        0       11       1240     11
  rp5un              10        0       10        841     10

  [!] Users with >5 idle sessions:
      msimpso3: 11 idle, 2156 MB
      ad3tb: 11 idle, 1240 MB
```

### JupyterHub Idle Culler

NØMADE pairs well with the JupyterHub idle culler to automatically clean up stale sessions:

```python
# /etc/jupyterhub/jupyterhub_config.py
c.JupyterHub.services = [
    {
        'name': 'idle-culler',
        'command': [
            'python3', '-m', 'jupyterhub_idle_culler',
            '--timeout=86400',      # 24 hours
            '--cull-every=3600',    # Check hourly
            '--concurrency=5',
        ],
        'admin': True,  # For JupyterHub < 2.0
    }
]
```

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              NØMADE                                     │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│  ┌─────────────────────────────────────────────────────────────────┐    │
│  │                 WEB DASHBOARD (Flask)                           │    │
│  │   Cluster Tabs · Partition Groups · Interactive · Network 3D   │    │
│  └─────────────────────────────┬───────────────────────────────────┘    │
│                                │                                        │
│  ┌─────────────────────────────┴───────────────────────────────────┐    │
│  │                      ALERT ENGINE                               │    │
│  │       Rules · Derivatives · Deduplication · Cooldowns           │    │
│  │          Email · Slack · Webhook · Dashboard                    │    │
│  └─────────────────────────────┬───────────────────────────────────┘    │
│                                │                                        │
│         ┌──────────────────────┴──────────────────────┐                 │
│         ▼                                             ▼                 │
│  ┌─────────────────────┐                ┌─────────────────────────┐     │
│  │  MONITORING ENGINE  │                │   PREDICTION ENGINE     │     │
│  │  Threshold-based    │                │   Cosine similarity     │     │
│  │  Immediate alerts   │                │   17-dim feature space  │     │
│  └─────────┬───────────┘                └─────────────┬───────────┘     │
│            │                                          │                 │
│            └──────────────────┬───────────────────────┘                 │
│                               │                                         │
│  ┌────────────────────────────┴────────────────────────────────────┐    │
│  │                         DATA LAYER                              │    │
│  │            SQLite · Time-series · Job History · I/O Samples     │    │
│  └────────────────────────────┬────────────────────────────────────┘    │
│                               │                                         │
│  ┌────────────────────────────┴─────────────────────────────────────┐   │
│  │                        COLLECTORS                                │   │
│  │  disk · slurm · job_metrics · iostat · mpstat · vmstat           │   │
│  │  node_state · gpu · nfs · interactive                            │   │
│  └──────────────────────────────────────────────────────────────────┘   │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Two Engines, One System

1. **Monitoring Engine**: Real-time threshold and derivative-based alerts
   - Catches immediate issues (disk full, node down, stuck jobs, idle sessions)
   - Uses first and second derivatives for early warning
   - "Your disk fill rate is *accelerating* — full in 3 days, not 10"

2. **Prediction Engine**: Pattern-based ML analytics
   - Catches patterns before they become issues
   - Uses job cosine similarity networks and health prediction
   - "Jobs with your I/O pattern have 72% failure rate"

---

## Data Collection

### Collectors

| Collector | Source | Data Collected | Graceful Skip |
|-----------|--------|----------------|---------------|
| `disk` | `shutil.disk_usage` | Filesystem total/used/free, projections | No |
| `slurm` | `squeue`, `sinfo` | Queue depth, partition stats, wait times | No |
| `job_metrics` | `sacct` | Job history, CPU/mem efficiency, health scores | No |
| `iostat` | `iostat -x` | %iowait, device utilization, latency | No |
| `mpstat` | `mpstat -P ALL` | Per-core CPU, imbalance ratio, saturation | No |
| `vmstat` | `vmstat` | Memory pressure, swap, blocked processes | No |
| `node_state` | `scontrol show node` | Node allocation, drain reasons, CPU load | No |
| `gpu` | `nvidia-smi` | GPU util, memory, temp, power | Yes (if no GPU) |
| `nfs` | `nfsiostat` | NFS ops/sec, throughput, RTT | Yes (if no NFS) |
| `job_monitor` | `/proc/[pid]/io` | Per-job NFS vs local I/O attribution | No |
| `interactive` | Process table | RStudio/Jupyter sessions, idle state, memory | No |

### 17-Dimension Feature Vector

NØMADE builds job similarity networks using a comprehensive feature vector:

| Source | Features |
|--------|----------|
| `sacct` | health_score, cpu_efficiency, memory_efficiency, used_gpu, had_swap |
| `job_monitor` | total_write_gb, write_rate_mbps, nfs_ratio, runtime_minutes, write_intensity |
| `iostat` | avg_iowait, peak_iowait, device_util |
| `mpstat` | avg_core_busy, imbalance_ratio, max_core_busy |
| `vmstat` | memory_pressure, swap_activity, procs_blocked |

---

## Prediction Capabilities

### Cosine Similarity Network

NØMADE uses **cosine similarity** on Z-score normalized feature vectors to build job similarity networks:

- **Continuous metrics**: Raw quantitative values, no arbitrary binning or categorical labels
- **Non-redundant features**: Each dimension captures unique information
- **Similarity threshold**: Default ≥ 0.7 cosine similarity to form network edges
- **Continuous health score**: 0.0 (catastrophic) → 1.0 (perfect)
- **Time-correlated system state**: iostat/mpstat/vmstat data aligned to job runtime windows

### Fruchterman-Reingold Network Visualization

The 3D network view uses a **Fruchterman-Reingold force-directed layout**:

- **Repulsive forces** between all node pairs: F = k² / distance (pushes unrelated jobs apart)
- **Attractive forces** along edges: F = distance² / k × similarity (pulls similar jobs together)
- **Result**: Natural clustering where groups of similar jobs form visible communities

Three view modes are available:
- **Force Layout**: Jobs positioned by network structure — connected jobs cluster together
- **Feature Axes**: Jobs positioned by selected feature dimensions (e.g., NFS ratio × CPU efficiency × I/O wait)
- **PCA View**: Jobs positioned by principal components — reveals emergent patterns

### ML Models

- **GNN**: Graph neural network for network-aware prediction (PyTorch Geometric)
- **LSTM**: Temporal pattern detection across job sequences
- **Autoencoder**: Anomaly detection for outlier jobs
- **Ensemble**: Weighted voting across model types

---

## Derivative Analysis

A key innovation in NØMADE is the use of first and second derivatives for early warning:

```
VALUE (0th derivative):     "Disk is at 850 GB"
FIRST DERIVATIVE:           "Disk is filling at 15 GB/day"
SECOND DERIVATIVE:          "Fill rate is ACCELERATING at 3 GB/day²"
```

By monitoring the second derivative (acceleration), NØMADE detects exponential growth, sudden usage spikes, and developing problems before linear projections underestimate the risk.

| Metric | Accelerating (d²>0) | Decelerating (d²<0) |
|--------|---------------------|---------------------|
| Disk usage | ⚠ Exponential fill | ✓ Cleanup in progress |
| Queue depth | ⚠ System issue | ✓ Draining normally |
| Failure rate | ⚠ Cascading problem | ✓ Issue resolving |
| NFS latency | ⚠ I/O storm developing | ✓ Load decreasing |

---

## Configuration

NØMADE uses a TOML configuration file (`~/.config/nomade/nomade.toml` or `/etc/nomade/nomade.toml`):

```toml
[general]
log_level = "info"
data_dir = "/var/lib/nomade"

[collectors]
enabled = ["disk", "slurm", "node_state"]
interval = 60

[collectors.disk]
filesystems = ["/", "/home", "/scratch", "/localscratch"]

[collectors.slurm]
partitions = []  # Empty = all partitions

# Cluster topology (optional — auto-detected from database if not defined)
# [clusters]
# name = "my-cluster"
#
# [clusters.partitions.general]
# description = "General CPU partition"
# nodes = ["node01", "node02", "node03"]
#
# [clusters.partitions.gpu]
# description = "GPU partition"
# nodes = ["gpu01", "gpu02"]
# gpu_nodes = ["gpu01", "gpu02"]

[alerts]
enabled = true
min_severity = "warning"
cooldown_minutes = 15

[alerts.thresholds.interactive]
idle_sessions_warning = 50
idle_sessions_critical = 100
memory_gb_warning = 32
memory_gb_critical = 64

[dashboard]
host = "127.0.0.1"
port = 8050
```

---

## Usage

### Command Line Interface

```bash
# System status
nomade status              # Full system status
nomade syscheck            # Verify requirements

# Data collection
nomade collect --once      # Single collection cycle
nomade collect -C disk,slurm   # Specific collectors

# Dashboard
nomade dashboard           # Launch web interface
nomade demo                # Launch with demo data

# Interactive session monitoring
nomade report-interactive          # Full report
nomade report-interactive --quiet  # Alerts only
nomade report-interactive --json   # JSON output

# Analysis
nomade disk /home --hours 24       # Filesystem trends
nomade jobs --user jsmith          # Job history
nomade similarity                  # Similarity analysis

# ML
nomade train               # Train prediction models
nomade predict             # Run predictions
nomade report              # Generate ML report

# Alerts
nomade alerts              # View recent alerts
nomade alerts --unresolved # Unresolved only
```

### Bash Helper Functions

```bash
source ~/nomade/scripts/nomade.sh
nhelp      # Show all commands
```

| Command | Description |
|---------|-------------|
| `nstatus` | Quick status overview |
| `nwatch [s]` | Live status updates |
| `ndisk PATH` | Filesystem trend analysis |
| `njobs` | Recent job history |
| `nalerts` | View alerts |
| `ncollect` | Run data collection |

---

## Installation

### Requirements

- Python 3.9+ (standalone interactive report works on Python 3.6+)
- SQLite 3.35+
- SLURM (for queue and job monitoring)
- sysstat package (iostat, mpstat)

Optional:
- nvidia-smi (for GPU monitoring)
- nfs-common with nfsiostat (for NFS monitoring)

### System Check

```bash
nomade syscheck
```

### SLURM Integration (Optional)

For per-job metrics collection:

```bash
sudo cp scripts/prolog.sh /etc/slurm/prolog.d/nomade.sh
sudo cp scripts/epilog.sh /etc/slurm/epilog.d/nomade.sh
sudo systemctl restart slurmctld
```

---

## Theoretical Background

### From Biogeography to HPC

NØMADE's prediction engine draws inspiration from biogeographical network analysis, particularly the concept of mapping emergent regions from observational data (Vilhena & Antonelli, 2015). Just as biogeographical regions emerge from species distribution patterns rather than being predefined, NØMADE allows job behavior patterns to emerge from metric data.

However, NØMADE uses **cosine similarity on continuous feature vectors** rather than the Simpson similarity on categorical presence/absence data used in biogeography. This approach better captures the quantitative, multi-dimensional nature of HPC job metrics — where the *magnitude* of CPU efficiency or I/O throughput matters, not just whether a job "used" a resource.

| Biogeography Concept | NØMADE Analog |
|---------------------|---------------|
| Species | Jobs |
| Geographic regions | Compute resources (nodes, partitions) |
| Emergent biomes | Job behavior clusters |
| Species ranges | Resource usage patterns |
| Transition zones | Domain boundaries (CPU↔GPU, NFS↔local) |

---

## Roadmap

See [ROADMAP.md](ROADMAP.md) for the full development plan. Highlights:

### Completed (v1.1.0)
- [x] Multi-cluster tabs with partition grouping
- [x] Interactive session monitoring (RStudio/Jupyter)
- [x] Dashboard Interactive tab with alerts
- [x] Standalone report script for Python 3.6 systems
- [x] JupyterHub idle-culler integration
- [x] Node health reflects CPU/memory pressure
- [x] Failed jobs modal with clickable categories
- [x] 3D force-directed network visualization
- [x] ML prediction models (GNN, LSTM, Autoencoder, Ensemble)

### Next Up
- [ ] Job queue panel per partition (squeue-like view)
- [ ] Partition utilization sparkline history
- [ ] User leaderboard and fairshare status
- [ ] Job efficiency analysis (requested vs used)
- [ ] Multi-site federation
- [ ] Real-time SLURM prolog scoring hook

---

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](docs/CONTRIBUTING.md) for guidelines.

```bash
git clone https://github.com/jtonini/nomade.git
cd nomade
python -m venv venv
source venv/bin/activate
pip install -e ".[dev]"
pytest
```

---

## License

NOMADE is dual-licensed:

- **AGPL v3**: Free for academic, educational, and open-source use
- **Commercial License**: Available for proprietary/commercial deployments

See [LICENSE](LICENSE) for details.

---

## Citation

```bibtex
@software{nomade2026,
  author = {Tonini, Joao},
  title = {NOMADE: A Lightweight HPC Monitoring and Prediction Tool},
  year = {2026},
  url = {https://github.com/jtonini/nomade}
}
```

---

## Contact

- **Author**: João Tonini
- **Email**: jtonini@richmond.edu
- **Issues**: [GitHub Issues](https://github.com/jtonini/nomade/issues)

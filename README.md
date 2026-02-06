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
- **Educational Analytics**: Measure computational proficiency development over time — not just resource consumption, but learning outcomes
- **Interactive Session Monitoring**: Track RStudio and Jupyter sessions across clusters — identify idle sessions, memory hogs, and stale notebooks
- **Real-time Monitoring**: Track disk usage, SLURM queues, node health, license servers, and job metrics
- **Derivative Analysis**: Detect accelerating trends before they become critical (not just threshold alerts)
- **Predictive Analytics**: ML-based job health prediction using cosine similarity networks
- **Community Dataset**: Export anonymized job fingerprints for cross-institutional research
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

This generates synthetic data and launches the dashboard at http://localhost:8050, complete with multi-cluster views, partition grouping, and interactive session monitoring.

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

## NØMADE Edu — Educational Analytics

NØMADE bridges the gap between infrastructure monitoring and educational outcomes by capturing per-job behavioral fingerprints — CPU efficiency, memory pressure, I/O patterns, and core utilization — that enable administrators and faculty to measure not just HPC adoption, but the development of computational proficiency over time.

### Job Analysis

Explain any job in plain language with proficiency scores and actionable recommendations:

```bash
nomade edu explain 12345
```

```
  NØMADE Job Analysis — 12345
  ────────────────────────────────────────────────────────
  User: student01    Partition: compute    Node: node04
  State: COMPLETED    Runtime: 6h 34m / 8h 00m requested

  Proficiency Scores
  ────────────────────────────────────────────────────────
    CPU Efficiency       ███████░░░   65.2%   Good
    Memory Efficiency    █████████░   89.9%   Excellent
    Time Estimation      █████████░   87.3%   Excellent
    I/O Awareness        █████████░   90.3%   Excellent
    ────────────────────────────────────────────────────
    Overall Score        ████████░░   83.2%   Good

  Your Progress (last 30 jobs)
  ────────────────────────────────────────────────────────
    CPU Efficiency        48.3% →  65.2%  ↑ improving
    Memory Efficiency     84.0% →  89.9%  ↑ improving
```

### Proficiency Dimensions

Each job is scored across five dimensions (0-100 scale):

| Dimension | What It Measures | Common Issues |
|-----------|------------------|---------------|
| **CPU Efficiency** | Cores used vs requested | Requesting 32 cores for single-threaded code |
| **Memory Efficiency** | Peak memory vs requested | Copy-pasting `--mem=64G` for 2GB jobs |
| **Time Estimation** | Runtime vs walltime request | Requesting 48h for 20-minute jobs |
| **I/O Awareness** | Local scratch vs NFS usage | Heavy writes to network filesystem |
| **GPU Utilization** | Whether requested GPUs were used | Requesting GPU for CPU-only code |

### User Trajectory

Track a student's proficiency development over time:

```bash
nomade edu trajectory student01 --days 90
```

```
  NØMADE Proficiency Trajectory — student01
  ────────────────────────────────────────────────────────
  Jobs analyzed: 149    Period: 2026-01-15 → 2026-04-15
  Strong improvement (+18% overall)

  Score Progression
  ────────────────────────────────────────────────────────
    2026-01-15  ████░░░░  52.3%  (12 jobs)
    2026-02-01  █████░░░  61.8%  (28 jobs)
    2026-03-01  ██████░░  68.4%  (35 jobs)
    2026-04-01  ███████░  70.1%  (42 jobs)
```

### Course Reports

Generate aggregate proficiency reports for courses or lab groups:

```bash
nomade edu report bio301 --days 120
```

```
  NØMADE Group Report — bio301
  ────────────────────────────────────────────────────────
  Members: 20    Jobs: 1,847
  Period: 2026-01-15 → 2026-05-15

  Key Insight
  ────────────────────────────────────────────────────────
    15/20 students improved overall proficiency

  Group Proficiency
  ────────────────────────────────────────────────────────
    Memory Efficiency    █████████░   85.2%  ↑ +12.3%
    Time Estimation      ████████░░   78.9%  ↑ +8.4%
    I/O Awareness        ███████░░░   72.7%  ↑ +15.1%
    CPU Efficiency       █████░░░░░   53.3%  ↑ +4.2%

    Weakest area:   CPU Efficiency  |  Strongest: Memory Efficiency
```

This is the kind of insight that matters for grant reports and curriculum development: **measuring learning outcomes, not just resource consumption**.

---

## NØMADE Community — Cross-Institutional Research

Export anonymized job fingerprints for cross-institutional HPC research. Sensitive information (usernames, job names, paths) is cryptographically hashed while preserving the behavioral patterns needed for analysis.

### Export Anonymized Data

```bash
# Generate a unique salt for your institution (keep this secret!)
openssl rand -hex 32 > ~/.nomade_salt

# Export anonymized data
nomade community export \
  --output jobs_2026q1.parquet \
  --salt-file ~/.nomade_salt \
  --institution-type academic \
  --cluster-type mixed_medium \
  --start-date 2026-01-01 \
  --end-date 2026-03-31
```

### Preview Before Sharing

```bash
nomade community preview jobs_2026q1.parquet
```

Shows sample records, field distributions, and confirms no sensitive data leakage.

### Verify Export

```bash
nomade community verify jobs_2026q1.parquet
```

Validates the export meets community dataset standards (field completeness, anonymization verification, schema compliance).

### What's Anonymized

| Original Field | Anonymized As |
|----------------|---------------|
| `user_name` | `user_hash` (SHA-256 with salt) |
| `job_name` | `job_name_hash` |
| `node_list` | `node_hash` |
| `submit_time` | Rounded to day, offset by random hours |

### What's Preserved

Behavioral fingerprints that enable cross-institutional research:

- CPU/memory efficiency metrics
- I/O patterns (NFS ratio, write intensity)
- Runtime characteristics
- Resource request patterns
- Job health scores

---

## Dashboard

NØMADE's web dashboard provides a comprehensive overview of your HPC infrastructure through multiple views:

### Cluster Tabs

Each cluster appears as a top-level tab. Within each cluster, nodes are grouped by partition with:

- **Partition headers** showing name, description, node count, and down-node alerts
- **Utilization bars** — CPU, Memory, and GPU usage per partition
- **Job summary** — total jobs, succeeded, and failed counts at a glance
- **Node cards** — color-coded circles reflecting job success rate

Click any node to open a detailed sidebar with job statistics, resource utilization bars, failure breakdown, and top users.

### Resources Tab

View resource consumption by group and user:

- **Filter by cluster, group, and time period**
- **CPU-hours and GPU-hours** by group with visual bar charts
- **Per-user breakdown** with sortable columns
- **Group membership** from SLURM accounting or LDAP

### Activity Tab

Visualize job submission patterns:

- **7×24 heatmap** showing jobs by day-of-week and hour
- **Peak usage identification** for capacity planning
- **Filter by cluster and group**

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
│  │   Cluster Tabs · Resources · Activity · Interactive · Network   │    │
│  └─────────────────────────────┬───────────────────────────────────┘    │
│                                │                                        │
│  ┌──────────────┬──────────────┴──────────────┬──────────────────┐      │
│  │  EDU MODULE  │       ALERT ENGINE          │ COMMUNITY EXPORT │      │
│  │  Proficiency │  Rules · Derivatives        │ Anonymization    │      │
│  │  Trajectories│  Email · Slack · Webhook    │ Parquet/JSON     │      │
│  └──────┬───────┴──────────────┬──────────────┴────────┬─────────┘      │
│         │                      │                       │                │
│         └──────────────────────┼───────────────────────┘                │
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
│  │  node_state · gpu · nfs · interactive · groups                   │   │
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
| `node_state` | `scontrol show node` | Node allocation, drain reasons, CPU load | No |
| `groups` | `getent group`, `sacct` | Group membership, job accounting by user | No |
| `iostat` | `iostat -x` | %iowait, device utilization, latency | No |
| `mpstat` | `mpstat -P ALL` | Per-core CPU, imbalance ratio, saturation | No |
| `vmstat` | `vmstat` | Memory pressure, swap, blocked processes | No |
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
cluster_name = "my-cluster"  # Used for multi-cluster identification

[collectors]
enabled = ["disk", "slurm", "node_state", "groups"]
interval = 60

[collectors.disk]
filesystems = ["/", "/home", "/scratch", "/localscratch"]

[collectors.slurm]
partitions = []  # Empty = all partitions

[collectors.groups]
min_gid = 1000  # Skip system groups
group_filters = ["cs", "bio", "phys"]  # Only these prefixes (empty = all)
accounting_days = 30  # Job accounting lookback

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
nomade collect -C disk,slurm,groups  # Specific collectors

# Dashboard
nomade dashboard           # Launch web interface
nomade demo                # Launch with demo data

# Educational analytics
nomade edu explain 12345           # Explain a job
nomade edu trajectory student01    # User proficiency over time
nomade edu report bio301           # Course/group report

# Community dataset
nomade community export -o data.parquet --salt-file ~/.nomade_salt
nomade community preview data.parquet
nomade community verify data.parquet

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

### Install from PyPI

```bash
pip install nomade-hpc
```

This installs the `nomade` command globally (or in your virtual environment).

### Install from Source

```bash
git clone https://github.com/jtonini/nomade.git
cd nomade
pip install -e .
```

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

### Completed (v1.2.0)
- [x] Multi-cluster tabs with partition grouping
- [x] Interactive session monitoring (RStudio/Jupyter)
- [x] Dashboard Interactive tab with alerts
- [x] Resources and Activity dashboard tabs
- [x] Educational analytics (`nomade edu`)
- [x] Community dataset export (`nomade community`)
- [x] Group membership and job accounting collector
- [x] Standalone report script for Python 3.6 systems
- [x] JupyterHub idle-culler integration
- [x] Node health reflects CPU/memory pressure
- [x] Failed jobs modal with clickable categories
- [x] 3D force-directed network visualization
- [x] ML prediction models (GNN, LSTM, Autoencoder, Ensemble)

### Next Up
- [ ] Dashboard Edu tab (classroom view for faculty)
- [ ] Job templates with educational comments
- [ ] `nomade learn` student onboarding wizard
- [ ] Job queue panel per partition (squeue-like view)
- [ ] Partition utilization sparkline history
- [ ] User leaderboard and fairshare status
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

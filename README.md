<p align="center">
  <img src="docs/images/nomad-banner.png" alt="NØMAD-HPC" width="900">
</p>

# NØMAÐ-HPC

**NØde Monitoring And Diagnostics** — Lightweight HPC monitoring, visualization, and predictive analytics.

> *"Travels light, adapts to its environment, and doesn't need permanent infrastructure."*

[![PyPI](https://img.shields.io/pypi/v/nomad-hpc.svg)](https://pypi.org/project/nomad-hpc/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.18614517.svg)](https://doi.org/10.5281/zenodo.18614517)

---

📖 **[Full Documentation](https://jtonini.github.io/nomad-hpc/)** — Installation guides, configuration, CLI reference, network methodology, ML framework, and more.

---

## Quick Start
```bash
pip install nomad-hpc
nomad demo                    # Try with synthetic data
```

For production:
```bash
nomad init                    # Interactive setup wizard
nomad collect                 # Start data collection
nomad dashboard               # Launch web interface
```

---

## What's New in v1.4.0

### Multi-Cluster Monitoring
Monitor multiple clusters, interactive servers, and workstation groups from a single dashboard. The `nomad sync` command merges databases from remote sites into a combined view with per-cluster tabs, partition-aware layouts, and cross-site insights.

### Alert Pipeline
End-to-end alerting from data collection through email notification. The DiskCollector detects filesystem usage above thresholds, the ThresholdChecker fires severity-graded alerts, and the AlertDispatcher persists them to the database with deduplication and cooldown. Daily email reports via system `mail` — no SMTP configuration required.

### Per-Cluster Dynamics
The Insight Engine runs diversity, niche overlap, capacity, and resilience computations independently per cluster in combined databases. Each signal is tagged with its cluster name for clear attribution.

### Workstation Monitoring
Monitor departmental workstations via SSH from a central machine. The WorkstationCollector gathers CPU load, memory, disk, logged-in users, process counts, and zombie detection. Workstation groups appear in the Workstations page with department-level grouping.

### Disk Signals with Derivative Analysis
Filesystem signals now include fill rate and projected days-until-full from derivative analysis. The Insight Engine reads from the `filesystems` table and surfaces actionable warnings before disks reach critical capacity.

### Umbrella Group Filter
Niche overlap analysis excludes groups that contain more than 80% of all users, eliminating false contention warnings from universal groups.

---

## Features

| Feature | Description | Command |
|---------|-------------|---------|
| **Multi-Cluster Dashboard** | Real-time monitoring across HPC clusters, interactive servers, and workstations | `nomad dashboard` |
| **Multi-Site Sync** | Merge databases from remote sites into a combined view | `nomad sync` |
| **Workstation Monitoring** | Track departmental machines via SSH (CPU, memory, disk, users) | Dashboard → Workstations |
| **Storage Monitoring** | Filesystem health grouped by server with usage bars | Dashboard → Storage |
| **Interactive Sessions** | Monitor RStudio/Jupyter sessions with memory and idle detection | Dashboard → Interactive |
| **Alert Pipeline** | Threshold + derivative alerts with email, Slack, and webhook delivery | `nomad alerts` |
| **Insight Engine** | Operational narratives from multi-signal, per-cluster analysis | `nomad insights brief` |
| **System Dynamics** | Ecological and economic metrics for resource analysis | `nomad dyn` |
| **ML Prediction** | Job failure prediction using similarity networks | `nomad predict` |
| **Data Readiness** | Assess ML model readiness with sample size and variance analysis | `nomad readiness` |
| **Diagnostics** | Analyze network, storage, and node-level bottlenecks | `nomad diag` |
| **Educational Analytics** | Track computational proficiency development | `nomad edu explain <job>` |
| **Cloud Monitoring** | AWS/Azure/GCP metrics with cost and utilization analysis | `nomad cloud status` |
| **Community Export** | Anonymized datasets for cross-institutional research | `nomad community export` |
| **Reference** | Built-in documentation, code navigation, and search | `nomad ref` |
| **Developer Toolchain** | Scaffolding, validation, and contribution pipeline | `nomad dev` |
| **Issue Reporting** | Submit bugs, features, questions from any interface | `nomad issue report` |

---

## Dashboard Views

The web dashboard includes multiple views accessible via tabs:

- **Cluster Overview**: Real-time node status with health rings, per-partition layout, and live running/pending counts from queue state
- **Network View**: 3D job similarity network with failure clustering analysis
- **Resources**: CPU-hours, GPU-hours, and usage breakdown by group/user with cluster filtering
- **Activity**: Job submission heatmap showing patterns by day and hour
- **Interactive**: Active RStudio and Jupyter sessions with memory usage and idle detection
- **Workstations**: Departmental machines grouped by site and department with CPU, memory, disk, and user counts
- **Storage**: Filesystem health grouped by server with color-coded usage bars
- **Cloud**: AWS, Azure, and GCP resource utilization and cost tracking
- **Insights**: Operational narratives from multi-signal, per-cluster analysis
- **Dynamics**: Diversity indices, niche overlap, carrying capacity, resilience scoring
- **Readiness**: Collection health, uptime, cycles, and prediction readiness
- **Report Issue**: Submit bugs, feature requests, and questions with auto-populated system info

Toggle between light and dark themes with the Theme button.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              NØMAÐ                                          │
├───────────────┬───────────────┬───────────────┬───────────┬────────────────┤
│  Collectors   │   Analysis    │     Viz       │  Alerts   │  Intelligence  │
├───────────────┼───────────────┼───────────────┼───────────┼────────────────┤
│ disk          │ derivatives   │ dashboard     │ thresholds│ insights       │
│ iostat        │ similarity    │ network 3D    │ predictive│ dynamics       │
│ nfs           │ community     │ partitions    │ flapping  │ reference      │
│ slurm         │ ML ensemble   │ workstations  │ email     │ edu scoring    │
│ gpu           │ readiness     │ storage       │ slack     │                │
│ workstation   │ diagnostics   │ interactive   │ webhooks  │                │
│ storage       │               │ cloud         │           │                │
│ cloud         │               │ insights      │           │                │
│ groups        │               │ dynamics      │           │                │
│ interactive   │               │ readiness     │           │                │
└───────────────┴───────────────┴───────────────┴───────────┴────────────────┘
                                │
                      ┌─────────┴─────────┐
                      │  SQLite Database  │
                      │  (per-site + combined via sync)  │
                      └───────────────────┘
```

---

## Multi-Site Deployment

NØMAÐ supports monitoring multiple sites from a single dashboard:

```bash
# On each site
nomad init                    # Configure for local environment
nomad collect                 # Start data collection

# On a central machine
nomad sync                    # Pull and merge all site databases
nomad dashboard --db combined.db  # Unified view
```

The `nomad sync` command pulls databases via SCP, merges them with `source_site` tagging, and copies partition metadata for per-cluster dashboard filtering. Set up a cron for automatic syncing:

```
*/10 * * * * /path/to/nomad sync 2>/dev/null
```

---

## CLI Reference

### Core Commands
```bash
nomad init                    # Interactive setup wizard
nomad collect                 # Start collectors
nomad collect --once          # Single collection cycle
nomad dashboard               # Web interface
nomad dashboard --db file.db  # Use specific database
nomad sync                    # Merge remote databases
nomad demo                    # Demo mode with synthetic data
nomad status                  # System status
nomad syscheck                # Verify environment
```

### Insight Engine
```bash
nomad insights brief          # Executive summary
nomad insights detail         # Comprehensive report
nomad insights json           # Machine-readable output
nomad insights slack          # Slack-formatted report
```

### System Dynamics
```bash
nomad dyn summary             # Full dynamics narrative
nomad dyn diversity           # Workload diversity indices
nomad dyn diversity --by partition  # By partition
nomad dyn niche               # Resource overlap between groups
nomad dyn capacity            # Carrying capacity, binding constraint
nomad dyn resilience          # Recovery time after disturbances
nomad dyn externality         # Inter-group impact scoring
```

### Data Readiness & Diagnostics
```bash
nomad readiness               # Check ML training readiness
nomad readiness -v            # Verbose with feature details
nomad diag network            # Network performance analysis
nomad diag storage            # Storage health and I/O patterns
nomad diag node               # Node-level resource bottlenecks
```

### Educational Analytics
```bash
nomad edu explain <job_id>    # Job analysis with recommendations
nomad edu trajectory <user>   # User proficiency over time
nomad edu report <group>      # Course/group report
```

### Analysis & Prediction
```bash
nomad disk /path              # Filesystem trends
nomad jobs --user <user>      # Job history
nomad similarity              # Network analysis
nomad train                   # Train ML models
nomad predict                 # Run predictions
```

### Alerts & Community
```bash
nomad alerts                  # View alerts
nomad alerts --unresolved     # Unresolved only
nomad community export        # Export anonymized data
nomad community preview       # Preview export
```

### Reference
```bash
nomad ref                     # Browse all topics
nomad ref dyn diversity       # Look up any topic
nomad ref search "regime"     # Search across documentation
```

### Issue Reporting
```bash
nomad issue report            # Interactive bug/feature/question form
nomad issue report -c bug     # Pre-select category
nomad issue search disk       # Search existing issues
nomad issue info              # Preview system info
```

### Developer Toolchain
```bash
nomad dev guide               # Interactive contribution wizard
nomad dev new collector zfs   # Scaffold a new module
nomad dev check               # Validate codebase health
nomad dev check --fix         # Auto-fix registration issues
nomad dev test changed        # Test only modified files
nomad dev status              # Current branch and readiness
nomad dev submit              # Full contribution pipeline
nomad dev bump patch          # Version management
nomad dev deps collector disk # Module dependency graph
```

---

## Installation

### From PyPI
```bash
pip install nomad-hpc
```

### From Source
```bash
git clone https://github.com/jtonini/nomad-hpc
cd nomad-hpc && pip install -e .
```

### Requirements
- Python 3.10+
- SQLite 3.35+
- Optional: sysstat (`iostat`, `mpstat`), SLURM, nvidia-smi, nfsiostat

### System Check
```bash
nomad syscheck
```

---

## Documentation

📖 **[jtonini.github.io/nomad-hpc](https://jtonini.github.io/nomad-hpc/)**

- [Installation & Configuration](https://jtonini.github.io/nomad-hpc/installation/)
- [Dashboard Guide](https://jtonini.github.io/nomad-hpc/dashboard/)
- [CLI Reference](https://jtonini.github.io/nomad-hpc/cli/)
- [Configuration Options](https://jtonini.github.io/nomad-hpc/config/)
- [Network Methodology](https://jtonini.github.io/nomad-hpc/network/)
- [ML Framework](https://jtonini.github.io/nomad-hpc/ml/)
- [System Dynamics](https://jtonini.github.io/nomad-hpc/dynamics/)
- [Educational Analytics](https://jtonini.github.io/nomad-hpc/edu/)
- [Cloud Monitoring](https://jtonini.github.io/nomad-hpc/cloud/)
- [Reference System](https://jtonini.github.io/nomad-hpc/reference/)
- [Issue Reporting](https://jtonini.github.io/nomad-hpc/issue/)

---

## License

Dual-licensed:
- **AGPL v3** — Free for academic, educational, and open-source use
- **Commercial License** — Available for proprietary deployments

---

## Citation

```bibtex
@software{nomad2026,
  author = {Tonini, João Filipe Riva},
  title = {NØMAÐ: Lightweight HPC Monitoring with Machine Learning-Based Failure Prediction},
  year = {2026},
  url = {https://github.com/jtonini/nomad-hpc},
  doi = {10.5281/zenodo.18614517}
}

@article{tonini2026nomad,
  author = {Tonini, João Filipe Riva},
  title = {NØMAÐ: Lightweight HPC Monitoring with Machine Learning-Based Failure Prediction},
  journal = {Journal of Open Research Software},
  volume = {14},
  pages = {17},
  year = {2026},
  doi = {10.5334/jors.686}
}
```

---

## Contributing

See [CONTRIBUTING.md](docs/CONTRIBUTING.md) for guidelines.

---

## Contact

- **Author**: João Tonini
- **Email**: jtonini@richmond.edu
- **Issues**: [GitHub Issues](https://github.com/jtonini/nomad-hpc/issues)

# NØMAD

**NØde Monitoring And Diagnostics** — Lightweight HPC monitoring, visualization, and predictive analytics.

> *"Travels light, adapts to its environment, and doesn't need permanent infrastructure."*

[![PyPI](https://img.shields.io/pypi/v/nomad-hpc.svg)](https://pypi.org/project/nomad-hpc/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
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
nomad init                    # Configure for your cluster
nomad collect                 # Start data collection
nomad dashboard               # Launch web interface
```

---

## Features

| Feature | Description | Command |
|---------|-------------|---------|
| **Dashboard** | Real-time multi-cluster monitoring with partition views | `nomad dashboard` |
| **Educational Analytics** | Track computational proficiency development | `nomad edu explain <job>` |
| **Alerts** | Threshold + predictive alerts (email, Slack, webhook) | `nomad alerts` |
| **ML Prediction** | Job failure prediction using similarity networks | `nomad predict` |
| **Community Export** | Anonymized datasets for cross-institutional research | `nomad community export` |
| **Interactive Sessions** | Monitor RStudio/Jupyter sessions | `nomad report-interactive` |
| **Derivative Analysis** | Detect accelerating trends before thresholds | Built into alerts |

---

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│                         NØMAD                             │
├──────────────┬──────────────┬──────────────┬───────────────┤
│  Collectors  │   Analysis   │     Viz      │    Alerts     │
├──────────────┼──────────────┼──────────────┼───────────────┤
│ disk         │ derivatives  │ dashboard    │ thresholds    │
│ iostat       │ similarity   │ network 3D   │ predictive    │
│ slurm        │ ML ensemble  │ partitions   │ email/slack   │
│ gpu          │ edu scoring  │ edu views    │ webhooks      │
│ nfs          │              │              │               │
└──────────────┴──────────────┴──────────────┴───────────────┘
                              │
                    ┌─────────┴─────────┐
                    │  SQLite Database  │
                    └───────────────────┘
```

---

## CLI Reference

### Core Commands
```bash
nomad init                    # Setup wizard
nomad collect                 # Start collectors
nomad dashboard               # Web interface
nomad demo                    # Demo mode
nomad status                  # System status
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

### Community & Alerts
```bash
nomad community export        # Export anonymized data
nomad community preview       # Preview export
nomad alerts                  # View alerts
nomad alerts --unresolved     # Unresolved only
```

---

## Installation

### From PyPI
```bash
pip install nomad-hpc
```

### From Source
```bash
git clone https://github.com/jtonini/nomad.git
cd nomad && pip install -e .
```

### Requirements
- Python 3.9+
- SQLite 3.35+
- sysstat package (`iostat`, `mpstat`)
- Optional: SLURM, nvidia-smi, nfsiostat

### System Check
```bash
nomad syscheck
```

---

## Documentation

📖 **[jtonini.github.io/nomad](https://jtonini.github.io/nomad-hpc/)**

- [Installation & Configuration](https://jtonini.github.io/nomad-hpc/installation/)
- [System Install (`--system`)](https://jtonini.github.io/nomad-hpc/system-install/)
- [Dashboard Guide](https://jtonini.github.io/nomad-hpc/dashboard/)
- [Educational Analytics](https://jtonini.github.io/nomad-hpc/edu/)
- [Network Methodology](https://jtonini.github.io/nomad-hpc/network/)
- [ML Framework](https://jtonini.github.io/nomad-hpc/ml/)
- [Proficiency Scoring](https://jtonini.github.io/nomad-hpc/proficiency/)
- [CLI Reference](https://jtonini.github.io/nomad-hpc/cli/)
- [Configuration Options](https://jtonini.github.io/nomad-hpc/config/)

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
  title = {NØMAD: Lightweight HPC Monitoring with Machine Learning-Based Failure Prediction},
  year = {2026},
  url = {https://github.com/jtonini/nomad},
  doi = {10.5281/zenodo.18614517}
}
```

---

## Contributing

See [CONTRIBUTING.md](docs/CONTRIBUTING.md) for guidelines.

---

## Contact

- **Author**: João Tonini
- **Email**: jtonini@richmond.edu
- **Issues**: [GitHub Issues](https://github.com/jtonini/nomad/issues)

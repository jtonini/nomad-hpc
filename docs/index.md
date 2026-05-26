<picture>
  <source srcset="assets/nomad_hero.webp" type="image/webp">
  <img src="assets/nomad_hero.jpg"
       alt="NØMAĐ — Node Monitoring And Diagnostics"
       style="width: 100%; border-radius: 6px; margin-bottom: 1.5rem;">
</picture>

# NØMAÐ

**NØde Monitoring And Diagnostics** — Lightweight HPC monitoring, visualization, and predictive analytics.

> *"Travels light, adapts to its environment, and doesn't need permanent infrastructure."*

## What is NØMAÐ?

NØMAÐ is a self-contained monitoring and prediction system for HPC clusters. Unlike heavyweight solutions requiring complex infrastructure, NØMAÐ deploys quickly, runs with minimal resources, and provides actionable insights through:

- **Real-time monitoring** of disk, CPU, memory, GPU, and SLURM jobs
- **Predictive analytics** using machine learning and similarity networks
- **Educational analytics** tracking computational proficiency development
- **Multi-cluster dashboards** with partition-level views
- **Derivative analysis** detecting accelerating trends before thresholds

## Philosophy

Inspired by nomadic principles:

| Principle | Implementation |
|-----------|----------------|
| **Travels light** | SQLite database, minimal dependencies, no external services |
| **Adapts to environment** | Configurable collectors, flexible alerts, cluster-agnostic |
| **Leaves no trace** | Clean uninstall, no system modifications required |

## Quick Start
```bash
pip install nomad-hpc
nomad demo                    # Try with synthetic data
nomad dashboard               # Open http://localhost:8050
```

For production deployment, see [Installation](installation.md).

## Features at a Glance

| Feature | Description | Learn More |
|---------|-------------|------------|
| Dashboard | Multi-cluster real-time monitoring | [Dashboard](dashboard.md) |
| Educational Analytics | Track proficiency development | [Edu Module](edu.md) |
| ML Prediction | Job failure prediction | [ML Framework](ml.md) |
| Network Analysis | Similarity-based clustering | [Network Methodology](network.md) |
| Alerts | Threshold + predictive alerts | [Alerts](alerts.md) |
| Community Export | Anonymized cross-institutional data | [CLI Reference](cli.md) |

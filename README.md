<p align="center">
  <img src="docs/images/nomad-banner.png" alt="NØMAÐ-HPC" width="900">
</p>

# NØMAÐ-HPC

**NØde Monitoring And Ðiagnostics**: monitoring, diagnostics and plain-language
guidance for research computing. It covers HPC clusters, interactive servers,
lab workstations and storage.

> *"Travels light, adapts to its environment, and doesn't need permanent infrastructure."*

[![PyPI](https://img.shields.io/pypi/v/nomad-hpc.svg)](https://pypi.org/project/nomad-hpc/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.18614517.svg)](https://doi.org/10.5281/zenodo.18614517)

**Website: [nomad-hpc.com](https://nomad-hpc.com)** · **Documentation: [jtonini.github.io/nomad-hpc](https://jtonini.github.io/nomad-hpc/)**

---

## What it is

NØMAÐ reads what a research computing center's machines are doing: Slurm's
jobs and queues, compute nodes and GPUs, login nodes, RStudio and JupyterHub
servers, lab workstations and storage servers. It keeps what it reads in
SQLite and turns it into answers for three groups of people: those who run
jobs, the faculty who lead labs and teach courses, and the staff who run the
machines.

| Part | What it does | Where it runs |
|---|---|---|
| **Collectors** (`nomad collect`) | Read the machine every few minutes into that site's own database | Each cluster's head node, each interactive server, and a machine that reaches the lab workstations |
| **The hub** (`nomad sync`) | Brings every site's new rows into one combined database, and sends alerts, reports and emails on a schedule | One machine |
| **The Console** | The web interface: each person signs in and sees what their role allows | The hub, usually behind HTTPS |

A small center can run all three on one machine.

## Quick start

```bash
pip install nomad-hpc
nomad demo --no-launch                      # a demo database: jobs, nodes, GPUs, storage
nomad status --db ~/nomad_demo.db
nomad insights brief --db ~/nomad_demo.db
```

On a real site:

```bash
nomad init                                  # the setup wizard writes nomad.toml
nomad syscheck                              # what this machine has, and what is missing
nomad collect --once                        # one round of the collectors
# then from cron, every 5 minutes:
#   */5 * * * * /path/to/nomad collect --once
```

On the hub, list the sites under `[hub]` in its `nomad.toml` and run
`nomad sync` from cron or a systemd timer. See
[Installation](https://jtonini.github.io/nomad-hpc/installation/).

## What it does

| For | What | Docs |
|---|---|---|
| People who run jobs | What a job should ask for, and submitting it (`nomad size`, `nomad submit`, or the Console) | [Sizing and submitting](https://jtonini.github.io/nomad-hpc/submit/) |
| | Their own jobs explained, with advice (`nomad edu me`, `nomad edu explain JOB`) | [Educational analytics](https://jtonini.github.io/nomad-hpc/edu/) |
| | Practice runs, files from the browser, help requests, a weekly email about their own use | [Practice runs](https://jtonini.github.io/nomad-hpc/demos/), [My files](https://jtonini.github.io/nomad-hpc/files/), [Ask](https://jtonini.github.io/nomad-hpc/help-requests/), [Notices](https://jtonini.github.io/nomad-hpc/notices/) |
| Faculty | Their lab's members, work, workstations and storage; course views | [Configuration](https://jtonini.github.io/nomad-hpc/config/#console-access) |
| | Term reports, condo reports, the lab membership check, account requests | [Lab reports](https://jtonini.github.io/nomad-hpc/lab-reports/), [Condo reports](https://jtonini.github.io/nomad-hpc/condo-reports/), [Lab check](https://jtonini.github.io/nomad-hpc/lab-check/), [Accounts](https://jtonini.github.io/nomad-hpc/lab-accounts/) |
| Staff | Alerts, with stale-site detection and declared maintenance | [Alerts](https://jtonini.github.io/nomad-hpc/alerts/) |
| | Node health: temperatures, power, CPU clocks, GPUs (DCGM), interactive sessions | [Collectors](https://jtonini.github.io/nomad-hpc/collectors/), [GPU](https://jtonini.github.io/nomad-hpc/gpu/) |
| | Actions from the Console: drain or resume a node, manage RStudio and Jupyter sessions | [Actions](https://jtonini.github.io/nomad-hpc/actions/) |
| | Storage use and cold data, license end dates, the policy watch | [Storage](https://jtonini.github.io/nomad-hpc/storage/), [Licenses](https://jtonini.github.io/nomad-hpc/licenses/), [Policy watch](https://jtonini.github.io/nomad-hpc/policy_watch/) |
| | The usage report, impact before and after, Prometheus and Grafana | [Usage report](https://jtonini.github.io/nomad-hpc/usage-report/), [Impact](https://jtonini.github.io/nomad-hpc/impact/), [Metrics](https://jtonini.github.io/nomad-hpc/metrics/) |
| Analysis | Insight Engine, system dynamics, failure prediction (TESSERA), proficiency scoring | [Insights](https://jtonini.github.io/nomad-hpc/insights/), [Dynamics](https://jtonini.github.io/nomad-hpc/dynamics/), [ML](https://jtonini.github.io/nomad-hpc/ml/) |

Every command is listed in the
[CLI reference](https://jtonini.github.io/nomad-hpc/cli/), and each release's
changes are in the [Changelog](https://jtonini.github.io/nomad-hpc/changelog/).

## Installation

```bash
pip install nomad-hpc                       # Python 3.10 or later
pip install "nomad-hpc[report]"             # with PDF condo reports
```

From source:

```bash
git clone https://github.com/jtonini/nomad-hpc
cd nomad-hpc && pip install -e ".[dev]"
```

Requirements: Python 3.10+, SQLite 3.35+. Each collector uses the standard
tool for what it reads (`sacct`, `squeue` and `scontrol` for Slurm, `iostat`
and `mpstat` from sysstat, `nvidia-smi` and DCGM for GPUs, `nfsiostat`), and
a collector whose tool is missing says so. `nomad syscheck` and
`nomad collectors` show what this machine has.

## License

GNU Affero General Public License v3.0 or later
([AGPL-3.0-or-later](LICENSE)).

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

## Contributing

See [CONTRIBUTING.md](docs/CONTRIBUTING.md), and
[Extending NØMAÐ](https://jtonini.github.io/nomad-hpc/extending/) for adding a
collector.

## Contact

- **Author**: João Tonini
- **Email**: jtonini@richmond.edu
- **Issues**: [GitHub Issues](https://github.com/jtonini/nomad-hpc/issues)

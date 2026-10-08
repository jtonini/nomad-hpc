# CLI Reference

## Core Commands

| Command | Description |
|---------|-------------|
| `nomad init` | Setup wizard |
| `nomad collect` | Start data collection |
| `nomad collectors` | Which collectors run here, why, and how their runs went |
| `nomad dashboard` | Launch web interface |
| `nomad console launch` | Open the Console in your browser through an SSH tunnel ([Console access](console.md)) |
| `nomad demo` | Demo mode with synthetic data |
| `nomad status` | System status |
| `nomad syscheck` | Verify requirements |
| `nomad config check` | Does nomad.toml read? If not, the line and what is wrong ([Configuration](config.md)) |

## Educational Analytics

| Command | Description |
|---------|-------------|
| `nomad edu explain <job_id>` | Analyze job with recommendations |
| `nomad edu trajectory <user>` | User proficiency over time |
| `nomad edu report <group>` | Group/course report |

**Options for all edu commands**:
- `--db PATH` — Database path
- `--json` — JSON output
- `--days N` — Lookback period (trajectory/report)

## Analysis

| Command | Description |
|---------|-------------|
| `nomad disk <path>` | Filesystem trends |
| `nomad jobs` | Job history |
| `nomad similarity` | Network analysis |
| `nomad alerts` | View alerts |

## ML & Prediction

| Command | Description |
|---------|-------------|
| `nomad train` | Train prediction models |
| `nomad predict` | Run predictions |
| `nomad report` | Generate ML report |

## Community Dataset

| Command | Description |
|---------|-------------|
| `nomad community export` | Export anonymized data |
| `nomad community preview` | Preview export |
| `nomad community verify` | Verify export integrity |

## Data Collection
```bash
# Continuous collection
nomad collect

# Single cycle
nomad collect --once

# Specific collectors
nomad collect -C disk,slurm,groups

# Which collectors run here, why, and whether they collect anything
nomad collectors

# On the hub: how every site's collectors ran in the last 7 days
nomad collectors --db ~/.local/share/nomad/combined.db

# Jobs from before nomad (or not collected): an sacct export, or sacct itself
nomad import sacct EXPORT [--apply]
nomad import sacct --from 2025-10-01 [--to 2026-10-01] [--apply]
```

See [Collectors](collectors.md) (and "Jobs from before nomad" there).

## Global Options

| Option | Description |
|--------|-------------|
| `--config PATH` | Config file path |
| `--db PATH` | Database path |
| `--verbose` / `-v` | Verbose output |
| `--quiet` / `-q` | Suppress output |
| `--help` | Show help |

## Insight Engine

| Command | Description |
|---------|-------------|
| `nomad insights brief` | Executive summary |
| `nomad insights detail` | Comprehensive report |
| `nomad insights json` | Machine-readable output |
| `nomad insights slack` | Slack-formatted message |
| `nomad insights digest` | Periodic digest |

**Options**: `--hours N` (lookback window), `--db PATH`, `--cluster NAME`


## System Dynamics

| Command | Description |
|---------|-------------|
| `nomad dyn summary` | Full dynamics narrative |
| `nomad dyn diversity` | Simpson/Shannon diversity indices |
| `nomad dyn niche` | Pianka niche overlap matrix |
| `nomad dyn capacity` | Multi-dimensional carrying capacity |
| `nomad dyn resilience` | Disturbance detection and recovery time |
| `nomad dyn externality` | Cross-group impact scoring |

**Options**: `--hours N`, `--db PATH`, `--by {user,group,partition}`, `--json`


## Cloud Monitoring

| Command | Description |
|---------|-------------|
| `nomad cloud collect` | Collect metrics from cloud providers |
| `nomad cloud status` | Current resource utilization |
| `nomad cloud diag` | Cost and performance diagnostics |
| `nomad cloud edu` | Usage recommendations |

**Options**: `--provider {aws,azure,gcp}`, `--db PATH`


## Reference

| Command | Description |
|---------|-------------|
| `nomad ref` | Browse all topics |
| `nomad ref <topic>` | Look up a specific topic |
| `nomad ref search <query>` | Full-text search |

Topics cover all commands, configuration, concepts, and mathematical foundations.


## Issue Reporting

| Command | Description |
|---------|-------------|
| `nomad issue report` | Interactive bug/feature/question form |
| `nomad issue search <keywords>` | Search existing GitHub issues |
| `nomad issue info` | Preview auto-collected system info |

**Options for report**:

- `-c, --category {bug,feature,question}` — Issue category
- `-m, --component {collectors,alerts,dashboard,...}` — Affected component
- `-t, --title TEXT` — Issue title
- `--email` — Send via email instead of GitHub
- `--json` — Output formatted issue as JSON
- `--no-duplicate-check` — Skip duplicate search
- `--db PATH` — Database path for system info

When a GitHub token is configured in `nomad.toml` under `[issue_reporting]`, issues
are submitted directly via the GitHub API with auto-labeling. Without a token,
opens a pre-filled GitHub issue form in the browser.

### `nomad diag gpu`

GPU diagnostic report: utilization summary, workload distribution, temperature
status, and hardware health.

```
nomad diag gpu                    # All nodes, last 24h
nomad diag gpu --node node51      # Specific node
nomad diag gpu --hours 48         # Extended window
nomad diag gpu --health           # Hardware health only (PCIe, ECC, row remap)
```

**Options:**

| Option | Description |
|--------|-------------|
| `--node TEXT` | Limit to a specific node |
| `--hours INT` | History window in hours (default: 24) |
| `--health` | Show hardware health report only |
| `--db PATH` | Database path override |

### `nomad insights gpu`

Narrative summary of GPU usage patterns and health concerns. Highlights workload
classification, the utilization gap between nvidia-smi and Real Util, and any
DCGM health alerts.

```
nomad insights gpu
nomad insights gpu --hours 12
nomad insights gpu --node spdr16
```

**Options:**

| Option | Description |
|--------|-------------|
| `--node TEXT` | Limit to a specific node |
| `--hours INT` | History window in hours (default: 6) |
| `--db PATH` | Database path override |

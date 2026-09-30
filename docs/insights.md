# Insight Engine

The NØMAÐ Insight Engine translates analytical output into actionable, human-readable narratives. Instead of presenting raw numbers, charts, and threshold alerts, the engine explains **what is happening**, **why it matters**, and **what to do about it**.

## The Distinction

| Layer | Example | Role |
|-------|---------|------|
| **Alerts** | `WARN: /scratch usage 92%` | Reactive threshold triggers |
| **Reports** | `94.2% success rate this week` | Backward-looking, descriptive |
| **Insights** | `/scratch is 87% full (40.3 TB free). Growing about 10.2 TB a month; full in about 4 months at that rate.` | Interpretive, forward-looking |

Reports provide evidence. Insights provide understanding. Both coexist.

## Quick Start
```bash
# Generate demo data with stress scenarios
nomad demo --no-launch

# Get a concise operational briefing
nomad insights brief --db ~/nomad_demo.db --cluster demo-cluster --hours 168

# Full detailed report
nomad insights detail --db ~/nomad_demo.db --hours 168

# One site of a hub's combined database
nomad insights brief --db ~/.local/share/nomad/combined.db --site spydur --hours 168

# JSON output (for API/Console integration)
nomad insights json --db ~/nomad_demo.db

# Slack-formatted message
nomad insights slack --db ~/nomad_demo.db --cluster demo-cluster

# Email digest
nomad insights digest --db ~/nomad_demo.db --period daily
```

## How It Works

The engine runs a four-step pipeline:
```
DB tables → Signal Readers → Template Narration → Correlation Engine → Output
              (Level 1)         (Level 1)            (Level 2)
```

### Step 1: Signal Readers

Ten domain-specific readers query the database and produce typed `Signal` objects with severity, metrics, and affected entities.

| Reader | Data Source | Signals Produced |
|--------|------------|------------------|
| Jobs | `jobs` | How jobs ended, the partition where problems concentrate, out of memory, out of time, change against the previous window |
| Storage | `filesystems`, `storage_state` | How full each filesystem is, its growth over 30 days and time to full at that rate |
| GPU | `jobs` (GPU subset) | GPU jobs that failed or hit a limit, and whether they are one person's |
| Queue | `queue_state`, `jobs` | Backlog in the latest snapshot, median wait to start |
| Network | `network_perf` | Latency, packet loss |
| Alerts | `alerts` | Alerts raised in the window, grouped by condition |
| Nodes | `node_state` | Nodes down, not responding or drained in the latest snapshot |
| Cloud | `cloud_metrics` | Cost summary, underutilized instances |
| Workstations | `workstation_state` | Load, memory, disk, zombie processes |
| Dynamics | `jobs`, `node_state`, ... | One person running most jobs, a resource near its limit, resilience (see [dynamics](dynamics.md)) |

### How jobs are counted

A job counts once, by how it ended: **completed**; **failed** (`FAILED`, `BOOT_FAIL`); **hit a limit** (`TIMEOUT`, `OUT_OF_MEMORY`, `DEADLINE`); **lost to a node failure** (`NODE_FAIL`). **Cancelled** jobs (Slurm writes `CANCELLED by <uid>`) and preempted ones are reported but not counted as failures. The rate is "failed or hit a limit" over the jobs that ran to an end.

Below 50 such jobs (`MIN_JOBS`) nothing about jobs rises above a notice and no change is reported between windows: 2 failures in 31 jobs is 6.5%, which says nothing. A partition is singled out only with at least 50 jobs of its own, 10 problems, and a rate at least twice the rest of the site's.

### Alerts

nomad stores alerts but never marks them resolved, so none is called "active": the reader reports alerts **raised** in the window, grouped by condition, with how many times and when last. A condition that persists is raised again after each cooldown (`cooldown_minutes`); that is not flapping, and nothing claims it is. Both column layouts are read: nomad's (`category`, `source` = host) and the demo database's (`source`, `host`).

### What was measured

Next to the signals the engine keeps a **coverage** list: for each source, *measured*, *stale* (a snapshot source — nodes, filesystems, queue, workstations — whose newest reading is over 2 hours old), *no data* (not collected here, or nothing in the window), or *failed* (the reader raised; the error is given). A missing table is "no data"; any other database error is reported as a failure instead of being swallowed.

Health rests on it: with nothing measured it is **unknown**, not "good". A stale or failed source counts as a warning, and a `data_stale` signal names what stopped updating (`No new readings from spydur: nodes (last reading 13:05, 3h ago)`).

### Step 2: Template Narration (Level 1)

Each signal is passed through a narrative template that states what the data shows:

- "7,896 jobs ended in the last 7 days (and 50 were cancelled, which is not counted as a failure); 2.5% failed or hit a limit. 191 failed, 3 ran out of time."
- "In the 'gpu' partition 400 of 2,000 jobs failed or hit a limit (20.0%), against 1.5% in the other partitions."
- "20% of GPU jobs failed or hit a limit (400 of 2,000). 95% of them are one person's (of 6 people)."

Templates say only what the numbers support. A template that fails falls back to the signal's own detail, so one malformed signal can't take down the brief or the Console page.

### Step 3: Correlation Engine (Level 2)

The engine examines multiple signals together to find causal or co-occurring patterns. Instead of three separate alerts, it produces one coherent finding:

| Correlation Rule | Signals Combined | Insight |
|-----------------|-----------------|---------|
| Disk pressure + job failures | `disk_fill_projection` + `job_success_rate` | The two may be connected: check whether failing jobs write there |
| GPU jobs out of memory + partition failures | `gpu_oom` + `partition_failure_concentration` | GPU jobs stopped for memory (`--mem`, the node's memory, not GPU memory) |
| Queue pressure + wait times | `queue_pressure` + `high_wait_time` | Partition bottleneck |
| Network issues + job failures | `high_network_latency` + `job_success_rate` | I/O-related failures |
| Cloud cost + underutilization | `cloud_cost_summary` + `underutilized_instance` | Cost optimization |
| Several workstations busy | `workstation_high_cpu` / `workstation_high_memory` on 2+ machines | People working on them will find them slow |

Correlated insights include a **recommendation** with specific actions. Signals are correlated within one site only.

### Step 4: Output Formatting

| Format | Use Case |
|--------|----------|
| CLI brief | Concise terminal briefing |
| CLI detail | Full report with metrics |
| JSON | API and Console integration |
| Slack | Channel notifications (supports webhook) |
| Email digest | Daily/weekly summaries |

## Signal Suppression

Some signals only make sense when there's enough activity or appropriate cluster type to support them. The engine suppresses signals that would be misleading or noisy in low-data conditions.

### Concentration

The diversity signal is by **person**: "One person ran 94% of the 7,946 jobs submitted in this window". It fires with at least 50 jobs, two people, and one of them over 60%. It matters for reading everything else: figures counted over jobs then mostly describe that person's work. Group-based signals (niche overlap, externalities) appear only where each job can be placed in one group; see [dynamics](dynamics.md#groups-need-a-group-per-job).

### Capacity binding constraint

A resource is called the binding constraint only at **75%** or more (`BINDING_AT`). Below that nothing binds, and no signal is raised; Dynamics names the busiest resource instead.

### Cluster type considerations

NØMAÐ monitors three cluster types: HPC clusters (SLURM), workstation groups (per-user processes), and interactive servers (RStudio/Jupyter sessions). Some signals only fire for specific types — for example, SLURM-related signals only run when there are jobs in the database, and workstation pressure signals only run when `workstation_state` has data.

## CLI Reference

All commands accept `--db PATH`, `--hours N`, `--cluster NAME` and `--site NAME`.

### One site of a combined database

A hub's combined database (`nomad sync`) holds every site's rows. `--site` (`InsightEngine(..., site=...)`) reads one site through read-only views — nothing is copied — and labels the report with it; a site the database doesn't hold is an error. Without it, every site is read separately and each finding is labelled with its site; sites are never pooled, since pooling mixes one site's filesystems and nodes with another's.

A filesystem is reported only while it is still reported: a path whose last reading is more than a day older than the site's newest is taken as retired. Node states from a snapshot over 2 hours old are worded as "was ... at the last report".

### `nomad insights brief`

Concise operational briefing with health assessment, correlated findings, and individual signals.

### `nomad insights detail`

Full report with all signals, metrics, and affected entities.

### `nomad insights json`

JSON output for programmatic use:
```json
{
  "site": "spydur",
  "hours": 168,
  "measured": true,
  "overall_health": "degraded",
  "coverage": [{"source": "jobs", "label": "Jobs", "status": "measured", "newest": "...", "detail": "", "signals": 3}, ...],
  "signal_count": 15,
  "insight_count": 3,
  "insights": [...],
  "signals": [...]
}
```

### `nomad insights slack`

Slack-formatted message. Add `--webhook URL` to post directly.

### `nomad insights digest`

Email digest with `--period daily|weekly`.

## Dashboard Integration

Available in `nomad dashboard` as the **Insights** tab, and through the `/api/insights` endpoint.

## Architecture
```
nomad/insights/
    engine.py         — InsightEngine orchestrator
    signals.py        — 10 signal readers, coverage
    templates.py      — narrative templates
    correlator.py     — correlation rules (within one site)
    formatters.py     — Output formatters
    inject_stress.py  — Demo stress scenarios
```

## Implementation Levels

| Level | Description | Status |
|-------|-------------|--------|
| **Level 1** | Template-based narratives | Implemented |
| **Level 2** | Multi-signal correlation | Implemented |
| **Level 3** | LLM-powered interpretation | Planned (CSSI Year 2-3) |

## Programmatic Use
```python
from nomad.insights import InsightEngine

engine = InsightEngine("/path/to/nomad.db", hours=168, cluster_name="mycluster")
engine = InsightEngine("/path/to/combined.db", hours=168, site="spydur")

print(engine.overall_health)    # "good", "nominal", "degraded", "impaired", "unknown"
print(engine.coverage)          # per source: measured / stale / no_data / failed
print(engine.signal_count)
data = engine.to_dict()         # Python dict
print(engine.to_slack())        # Slack markdown
subject, body = engine.to_email("daily")
```

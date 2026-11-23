# Architecture

NØMAÐ is one Python package, `nomad-hpc`, and a web interface, the Console.
The same package runs on every machine; what a machine does depends on which
sections its `nomad.toml` has.

```
 SITES (each keeps its own SQLite database)
 ┌─────────────────────────┐ ┌─────────────────────────┐ ┌─────────────────────────┐
 │ cluster head node       │ │ interactive server      │ │ lab head / storage host │
 │ nomad collect --once    │ │ nomad collect --once    │ │ nomad collect --once    │
 │ (cron, every 5 minutes) │ │ RStudio, Jupyter        │ │ workstations, NAS (ssh) │
 │        nomad.db         │ │        nomad.db         │ │        nomad.db         │
 └────────────┬────────────┘ └────────────┬────────────┘ └────────────┬────────────┘
              │      ssh: the hub's exporter, run with python3,       │
              └───────────── only the rows that changed ──────────────┘
                                          │
 HUB                                      ▼
 ┌────────────────────────────────────────────────────────────────────────────┐
 │ nomad sync (systemd timer)  ──►  combined.db  (every site's rows,          │
 │                                               tagged with source_site)     │
 │ timers: alerts, weekly notices, lab check, term and condo reports,         │
 │         license reminders, cold data, Prometheus exporter                  │
 │                                                                            │
 │ Console: FastAPI + React on 127.0.0.1:8000, behind a reverse proxy (HTTPS) │
 │          reads combined.db and nomad.toml; signs people in; scopes answers │
 └──────────┬───────────────────────────────────────────────┬─────────────────┘
            │ ssh, forced commands                          │ HTTPS
            ▼                                               ▼
  sites: nomad agent (actions)                     browsers: people, PIs, staff
         nomad-gate (as the person)
```

## Roles

| Role | Section in nomad.toml | What it runs |
|---|---|---|
| Site | `[collectors.*]` (and `[clusters.*]`) | `nomad collect --once`, from cron or a timer |
| Hub | `[hub]` with `[[hub.sites]]` | `nomad sync`, and the scheduled reports and emails |
| Console | `[console.*]` | the web interface, on the machine with the combined database |

A machine with no `[hub]` is a site only. A hub can be a site of its own (a
`localhost` site in `[[hub.sites]]`, read without ssh), which is how lab
workstations and storage servers that no cluster reaches are collected. A
center with one cluster can run all three roles on one machine.

## Collection

`nomad collect --once` runs the collectors this machine's nomad.toml turns
on. Each one reads one kind of thing with the standard tool for it, writes
its own tables, and logs each run (`collection_log`), so that
`nomad collectors` can say what ran, what it needs that this host lacks, and
whether it collected anything.

| Area | Collectors |
|---|---|
| Slurm: jobs, queues, nodes | `slurm`, `job_metrics`, `job_live`, `node_state`, `slurm_usage`, `preemptions` |
| The machine itself | `disk`, `iostat`, `mpstat`, `vmstat`, `pressure`, `nfs` |
| Node hardware | `gpu` (nvidia-smi and DCGM), `temps`, `power`, `clock`, `node_disks` |
| People and their sessions | `groups`, `per_user` (login nodes), `oom`, `interactive` (RStudio, Jupyter), `workstation` |
| Storage | `storage` (ZFS and NFS servers), `storage_use` (who holds the space) |
| Software | `modules` (Lmod), `licenses`, `license_tries` |
| Security | `policy_watch` (tunnels, miners, relays, exposed services, AI-agent misuse) |
| nomad itself | `databases` |
| Elsewhere | `network_perf`, `cloud` |

[Collectors](collectors.md) has what each one reads, where it belongs and the
tables it writes. Readings that need root (board sensors, storage scans,
failed `sudo`) come from small helpers that root's cron runs and that only
read; the collector reads the file they write.

Jobs from before nomad was installed can be loaded from Slurm's accounting
with `nomad import sacct`.

## The hub's sync

Each site keeps a log of the rows its collectors add, change and delete
(triggers on its own tables). Each `nomad sync` sends the site a small
exporter over ssh, runs it there with `python3`, and applies what it returns
to the combined database in place, one transaction per site. A run writes
megabytes; nothing on the site needs installing for it. A site is copied
whole only the first time, after its database was replaced or restored, or
when the hub has fallen too far behind.

`sync_sites` records, for each site, when it was last brought up to date,
how, and why not. A site that stops answering, or answers but has stopped
collecting, raises an alert from the hub, and every page shows how old each
site's data is. A site taken down on purpose is declared with
`nomad maintenance start`, which holds its alerts.

The details are in
[Setup Wizard: multi-site setup](setup-wizard.md#multi-site-setup).

## The Console

The Console reads the combined database and the hub's nomad.toml. It imports
nomad's own code for its answers (the same functions `nomad facts`,
`nomad edu` and `nomad size` use), so the command line and the web interface
give the same figures. Its JSON API (`/api/v1`) is what
[`nomad facts`](facts.md) prints.

Who sees what is decided on the server, from nomad.toml
([Configuration: Console access](config.md#console-access)):

| Who | Sees |
|---|---|
| Everyone signed in | the clusters' capacity, queues and waits; their own jobs, scores and advice |
| A lab's PI, leads and research assistants | that lab's members one by one, their work, its workstations and storage |
| An instructor | the courses they teach this term |
| Operators | everything; change no settings |
| Admins | everything, settings included |

Non-staff pages keep the staff pages' structure and drill-downs, scoped to
what the person may see.

The Console never reaches a cluster directly. Two keys of the hub's, each held
on the site by sshd to one forced command, do it for it:

- **Actions** (`nomad agent`): a fixed catalog of commands staff may start
  from the Console, such as draining a node or clearing stale Jupyter
  sessions. Each is a dry run unless asked otherwise, and each is recorded
  with who asked. See [Actions](actions.md).
- **The gate** (`nomad-gate`): acting on a cluster as the person signed in,
  with the cluster account's own `sudo -u PERSON`, never as root. It sizes
  and submits their jobs, copies their files, and cancels their jobs. See
  [Sizing and submitting](submit.md#from-the-console-nomad-gate) and
  [My files](files.md).

## Databases

| Database | Where | Holds |
|---|---|---|
| `nomad.db` (or the site's name) | each site | what that site's collectors read, and its change log |
| `combined.db` | the hub | every site's rows, with `source_site`; `sync_sites` |
| `notices.db`, `lab_check.db`, and others beside it | the hub | what people answered and what was sent: notices, the lab check, account and help requests, cold-data cases, course answers |

All are SQLite. Their size and growth, and old copies left beside them, are
watched by the `databases` collector and `nomad db`
([nomad's own databases](databases.md)).

## Analysis

| Module | What | Docs |
|---|---|---|
| `nomad.facts` | The figures every page and command shares: sites, a person, a lab, a course, a node over a window | [Facts](facts.md) |
| `nomad.edu` | Each job scored on CPU, memory, time, I/O and GPU use, with advice in plain language | [Educational analytics](edu.md), [Proficiency scoring](proficiency.md) |
| `nomad.submit` | What a job should ask for, and where it starts soonest | [Sizing and submitting](submit.md) |
| `nomad.alerts` | Thresholds, forecasts (a disk filling), stale sources, the hub's alerts for its sites | [Alerts](alerts.md) |
| `nomad.insights` | Signals from every source, linked into findings, as sentences | [Insight Engine](insights.md) |
| `nomad.dynamics` | Diversity, niche overlap, capacity, resilience, externalities | [System dynamics](dynamics.md) |
| `nomad.usage` | The administrators' report, condo and lab reports | [Usage report](usage-report.md) |
| `nomad.ml` | Job failure prediction with TESSERA; training on a GPU worker | [ML framework](ml.md), [Training the model](ml-training.md) |
| `nomad.metrics` | What nomad knows, for Prometheus and Grafana | [Prometheus metrics](metrics.md) |

## Code layout

| Path | What |
|---|---|
| `nomad/cli.py` and `nomad/*_cli.py` | the `nomad` command |
| `nomad/collectors/` | one module per collector; `base.py` has `BaseCollector` and the registry |
| `nomad/db/` | the schema, migrations, and the sync's site exporter (`delta_site.py`) |
| `nomad/config/` | the packaged defaults (`default.toml`) and the config reader |
| `nomad/facts/`, `nomad/edu/`, `nomad/submit/`, `nomad/usage/` | the analysis above |
| `nomad/actions/`, `nomad/console/` | the actions catalog and agent; `nomad console launch` |
| `nomad/demos/` | the practice-run kits |
| `nomad/viz/` | the standalone dashboard (`nomad dashboard`) |
| `scripts/` | the install for users, and the root helpers |
| `tests/` | the test suite (`pytest`) |

[Extending NØMAÐ](extending.md) shows how to add a collector.

<picture>
  <source srcset="assets/nomad_card.webp" type="image/webp">
  <img src="assets/nomad_card.jpg"
       alt="NØMAÐ — NØde Monitoring And Ðiagnostics. Open-source monitoring and diagnostics for HPC clusters."
       style="width: 100%; border-radius: 6px; margin-bottom: 1.5rem;">
</picture>

# NØMAÐ

**NØde Monitoring And Ðiagnostics**: monitoring, diagnostics and plain-language
guidance for research computing. It covers HPC clusters, interactive servers,
lab workstations and storage.

> *"Travels light, adapts to its environment, and doesn't need permanent infrastructure."*

## What it is

NØMAÐ reads what a research computing center's machines are doing: Slurm's
jobs and queues, compute nodes and their GPUs, login nodes, RStudio and
JupyterHub servers, lab workstations and storage servers. It keeps what it
reads in SQLite and turns it into answers for three groups of people: those
who run jobs, the faculty who lead labs and teach courses, and the staff who
run the machines.

It has three parts:

| Part | What it does | Where it runs |
|---|---|---|
| **Collectors** (`nomad collect`) | Read the machine every few minutes into that site's own database | Each cluster's head node, each interactive server, and a machine that reaches the lab workstations |
| **The hub** (`nomad sync`) | Brings every site's new rows into one combined database, and sends the alerts, reports and emails on a schedule | One machine |
| **The Console** | The web interface: each person signs in and sees what their role allows | The hub, usually behind HTTPS |

A small center can run all three on one machine. See
[Architecture](ARCHITECTURE.md) for how the parts fit together.

## What each person gets

**People who run jobs** (researchers, students):

- their own jobs, running and waiting, and why a job waits;
- what a job should ask for (cores, memory, time, GPUs, partition), each value
  with where it comes from ([Sizing and submitting](submit.md));
- submitting from the Console as themselves, and
  [practice runs](demos.md) that show how a program's jobs run;
- [their files from the browser](files.md), [help requests](help-requests.md)
  with the context attached, and a [weekly email](notices.md) about their own
  use;
- `nomad edu me` and `nomad edu explain JOB` on the command line
  ([Educational analytics](edu.md)).

**Faculty** (PIs, instructors):

- their lab's members and work, its workstations and storage, and what needs
  their attention ([Configuration: Console access](config.md#console-access));
- a [report each term](lab-reports.md), and monthly
  [condo reports](condo-reports.md) for labs that own nodes;
- the [lab membership check](lab-check.md) and
  [account requests](lab-accounts.md), answered in the Console;
- a view of each course they teach.

**Staff** (administrators, operators):

- [alerts](alerts.md) that say what is real: a site that stopped reporting, a
  disk filling, a node braked by a failed power supply, and declared
  maintenance windows that hold them;
- node health: [temperatures, power, CPU clocks](collectors.md),
  [GPUs with DCGM](gpu.md), [interactive sessions](interactive.md);
- [actions](actions.md) started from the Console: drain or resume a node,
  clear stale RStudio or Jupyter sessions;
- [storage use](storage.md) and data unchanged for a year,
  [license end dates](licenses.md), the [policy watch](policy_watch.md);
- the [usage report](usage-report.md), [impact](impact.md) before and after
  NØMAÐ, [Prometheus metrics](metrics.md) for Grafana, and
  [nomad's own databases](databases.md).

**Analysis**: the [Insight Engine](insights.md),
[system dynamics](dynamics.md), [job failure prediction](ml.md) with
[TESSERA](ml-training.md), and [proficiency scoring](proficiency.md).

## Principles

| Principle | In practice |
|---|---|
| **Travels light** | SQLite, the standard Linux and Slurm tools, and cron or systemd timers. For the hub to read a site, the site needs only `python3`. |
| **Adapts to its environment** | Each machine runs the collectors that fit its role. Anything another institution could set differently (quotas, limits, names, who may see what) is set in `nomad.toml`, not in the code. |
| **Leaves no trace** | It runs as its own account and needs no root to collect. The few readings that do need root come from small helpers that an administrator installs. A command that changes something shows the change first and acts only with `--apply`. |
| **Says what it knows** | Every figure comes with its period and the age of its data. Something not measured is shown as "not measured", never as "good", and the rule behind a figure is stated where the figure appears. |

## Try it

```bash
pip install nomad-hpc
nomad demo --no-launch                      # a demo database: jobs, nodes, GPUs, storage
nomad status --db ~/nomad_demo.db
nomad insights brief --db ~/nomad_demo.db
nomad facts sites --db ~/nomad_demo.db
```

`nomad demo --console DIR` makes a demo for the Console, with a cast of
made-up people to sign in as. See [Quick Start](quickstart.md), then
[Installation](installation.md) for a real deployment.

## License and citation

NØMAÐ is free software under the
[GNU AGPL v3](https://www.gnu.org/licenses/agpl-3.0) (or later). To cite it,
see the [README](https://github.com/jtonini/nomad-hpc#citation). Changes in
each release are in the [Changelog](changelog.md).

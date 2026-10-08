# Usage report

`nomad usage-report` writes the administrators' report for a period: how
much a cluster was used, by how many people, how long work waited, what was
held and not used, what ran on the GPUs, how storage grows, and when demand
passes capacity. It is the report a provost's office or a CFO asks for when
hardware has to be justified.

```bash
nomad usage-report --from 2025-10-01 --to 2026-10-07 --cluster c1
nomad usage-report init --cluster c1        # draft ~/.config/nomad/report.toml
nomad usage-report people --cluster c1 --out users.csv   # for a user map
```

It writes `usage-c1-2025-10-01-to-2026-10-07.md` and `.json` to `--out`
(default: the current directory) and prints each section's finding.

## Privacy

The report is made of aggregates. People are counted, never named; job
names and working directories become application families; lab-owned
(condo) partitions become "condo".

This is enforced in code:

- The layer that reads the data turns people into numbers before any
  section sees them.
- Labels that come from the data and are printed (filesystem paths,
  departments and schools of the user map, session types) have any
  username, group, account or partition in them replaced by `*`
  (`/home/*`).
- Before anything is written, every username, group, account, job name,
  working-directory part and partition seen in the data is looked for in
  the finished text (the Markdown, and the JSON's text values, not its keys
  or fact ids). Words the report itself is made of don't count, nor do
  numbers and dates. If a name is there, nothing is written, and the
  command says how many names of each kind it found.
- `--guard-details FILE` writes those names to a file readable only by
  you, to find where they came from. A word that is not a name in your
  report (a job called "rescale", say) can be let through with
  `--allow WORD`.
- `--min-cell N` prints counts of fewer than N people as "fewer than N",
  for wider audiences, where "1 person in department X" names someone.

## The sections

Each section opens with one sentence: its finding and the number behind it.
A section with no data says "Not measured" and why; it never shows a zero
it didn't measure.

| # | Section | Source |
|---|---|---|
| 1 | Headline: core-hours delivered as a share of the maximum, institutional and condo; people; jobs; the largest single person's share; change on the same months a year earlier | job records; Slurm's monthly totals |
| 2 | Multi-year load: core-hours allocated each year, idle, earmarked for waiting jobs, down; growth, compound rate, summers, the busiest month | Slurm's monthly totals (`slurm_usage` collector) |
| 3 | Who uses what: people per partition class, per-node utilization, people per node | job records; node samples |
| 4 | Waits: share of core-hours that waited more than a day, by month submitted; people who waited; median wait per class | job records |
| 5 | Held vs used: cores allocated and node load; CPU time used by jobs | node samples; job metrics |
| 6 | Memory: requested and peak used, per partition class; the largest peak each node class has held | job metrics; job records |
| 7 | GPUs: GPU-hours as a share of card-hours, card activity, GPU-hours and people by application | job records; GPU samples |
| 8 | What runs: job lengths, multi-node work, core-hours by application | job records |
| 9 | Storage: highest use per month, growth since the last cleanup, when full | filesystem samples |
| 10 | Reliability: how jobs ended, core-hours lost to down nodes, nodes out of service, outages | job records; Slurm's totals; node samples |
| 11 | Policy indicators: no time limit, running from /home, CPU-only jobs on GPU nodes, memory over-request | job records |
| 12 | Departments and schools (with `--user-map`) | user map |
| 13 | Teaching server (with `--teaching SITE`) | interactive sessions |
| 14 | Capacity and projection: practical capacity, projected demand, the year each capacity line is passed | sections 1 and 2; report.toml |

After them come a table of where each section's figures come from (source,
period covered, gaps, and whether the figures are measured, estimated or
projected), what was set aside with counts (accounts that are not people,
jobs that never started, jobs outside the tier map, impossible start times,
memory peaks larger than any node), and the assumptions.

## Definitions

- **Core-hours** = allocated CPUs × elapsed hours, split evenly across a
  job's nodes, clipped to the period. The JSON also gives each job counted
  whole, as reports that didn't clip have done.
- **Maximum** = cores × hours in the period (exact hours, never 730 a month).
- **Waiting** = more than 24 hours from submission to start, weighted by
  core-hours: one array of thousands of short jobs must not read as "most
  jobs waited". Jobs that never started have no wait; jobs submitted in the
  period that started after it count in the month they were submitted.
- **People** are those with a job that ran or waited in the period. A job
  that never ran and has no end counts only if it was submitted in the
  period (or, still waiting, at most 30 days before it). In an export, a
  pending array range (one row for tasks not yet started) is one waiting
  job; nomad doesn't store those rows.
- **More than one class** counts node classes (the tiers, GPU, condo);
  overlay partitions span classes and aren't one.
- **A job's class** for waits, job lengths and per-node figures is the tier
  all its nodes belong to, whatever partition it was submitted to. "Who
  uses what", CPU time used and memory requested and used go by the
  partition a job ran in (submitted to several: overlay), so that many small
  jobs sent through an overlay partition don't hide a tier's own work. The
  memory section also gives each node class's largest peak, every job on
  its nodes counted: that is what a node of the class has had to hold.
- **Partition classes** come from the nodes each partition holds (one tier:
  that tier; several: overlay; only condo nodes: condo), unless report.toml
  says otherwise.
- **CPU time used** = Σ(CPU use × cores × elapsed) ÷ Σ(cores × elapsed),
  over jobs with NØMAÐ job metrics. Node load and per-job accounting
  disagree; the report shows both.
- **GPU-hours** = GPUs in the allocation × hours, from when Slurm began
  accounting GPUs (report.toml's `gpu_accounting_start`, else the first job
  with GPUs in its allocation in all the records). A job with no allocation
  recorded counts the GPUs it asked for. The cards are those of the
  institution's GPU tiers (institutional tiers whose nodes all have GPUs);
  a lab's GPU node doesn't make its tier a GPU tier. Card activity is not
  the activity of GPU jobs where Slurm doesn't confine devices.
- **Nodes** are those sampled in the period, each as last seen in it (a
  node retired before the period is not counted); report.toml's cores and
  GPUs win where given.
- **No time limit** is measured over jobs whose records carry the
  allocation (read from sacct's full format: an export, or nomad 1.7.42
  on). Elsewhere "no limit" and "not recorded" look alike.
- **Storage growth** is the slope of each month's highest use after the
  last fall of more than 5% (a cleanup). The time to full starts from the
  filesystem's last reading of the period, since a month's highest can be a
  spike cleaned up days later; both are shown. When the last reading is
  more than 5% below the latest month's highest, that highest was a spike
  and is left out of the slope. Decimal terabytes.
- **Practical capacity** = cores × weight × hours × 75%: above that, queues
  form. Growth rates default to the lowest year-over-year growth, the
  compound rate and the highest, over the full years of Slurm's totals.
- Slurm's monthly totals for a month it may still revise (until two days
  after the month ends) are left out of the yearly figures.
- **Times** are local, as Slurm and nomad write them; a job across a change
  to or from daylight saving time keeps its own times. Ends nomad assumed
  before 1.7.19 (in UTC) are not taken as ends.

## Where it runs

On the hub, against `combined.db`, with `--cluster` naming the site; or on
a site's head node against its own database. `--db` chooses another
database.

`--sacct EXPORT` reads the jobs from an `sacct -a -X -P -o ALL` export
instead (plain or gzipped). The database, when there is one, still gives
node samples, job metrics and Slurm's totals. It is how a report is checked
against Slurm's own records, and how periods older than nomad's are
reported.

## report.toml

What nomad can't know by itself: which nodes form which tier, which tiers
are the institution's (the rest count as condo), partitions whose nodes
don't decide their class, accounts that are not people, how job names map
to applications, and the capacity assumptions. It is site data: keep it on
the machine that runs the report.

`nomad usage-report init` drafts it from the latest node samples (nodes
grouped by cores, memory and GPUs; partitions and their nodes, as
comments). Without a tier map every node is one tier, "all nodes".

See [report.example.toml](report.example.toml) for every setting. On
Python 3.10 (which reads TOML with an older library), a node list used as a
key in an inline table can't contain a comma: write
`{ "n[01-08]" = 52, "g[01-03]" = 52 }`, not `{ "n[01-08],g[01-03]" = 52 }`.

## Department and school figures

```bash
nomad usage-report people --cluster c1 --from 2025-10-01 --to 2026-10-07 --out users.csv
```

writes the period's usernames to a CSV (`user,department,school`) readable
only by you, and prints only how many there are. Fill it in, then pass it
with `--user-map users.csv`. Other columns (a PI, say) are read only so the
privacy check knows those names too.

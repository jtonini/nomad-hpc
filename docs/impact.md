# Impact: before and after NØMAÐ

A year or more after people start getting NØMAÐ's feedback, the question is
what changed: do jobs ask for closer to what they use, do fewer fail or run
out of memory, do they wait less? `[impact]` in nomad.toml records when that
feedback began and what else changed along the way; `nomad impact` compares
the jobs before and after.

```toml
[impact]
launch = 2026-10-19            # when people started getting NØMAÐ's feedback
baseline_from = 2025-08-24     # where "before" starts (default: a year before launch)
events = [
  { date = 2026-10-06, what = "Console open to staff" },
  { date = 2027-01-11, what = "four nodes added to the large partition" },
]
exclude_users = ["svc-backup"] # accounts that aren't people (1.13.2)
```

`launch` splits the jobs into before and after. `events` is a log of
everything else that changes how people use the machines: nodes added, a
Slurm limit, a workshop, a new part of NØMAÐ itself. Write them down when
they happen; a report a year later names them instead of crediting NØMAÐ
with what they did. `exclude_users` names accounts that aren't people (a
service account that runs test jobs, signs in to every workstation, or holds
a login session open): they are left out of every figure. `nomad config
check` says what in the section it can't use.

## What the before period needs

```
nomad impact status
```

For each site, the jobs that ended before and after launch: how many,
how many measured, how many measured the 1.13 way, how many with an
eligible time. Then what to run:

* **Jobs the database doesn't reach back to.** Slurm's accounting usually
  keeps far more than nomad has collected. On the site,
  `nomad import sacct --from BASELINE` (a dry run, then `--apply`) brings
  them, with their eligible times.
* **Measurements read the old way.** Before 1.13, CPU use came from Slurm's
  AveCPU, the average over a step's tasks: a job running 16 tasks read about
  a sixteenth of its use. Set against 1.13's figures, that would look like
  an improvement NØMAÐ didn't make. On the site, `nomad impact backfill`
  (a dry run, then `--apply`) reads the before period again from Slurm, a
  week at a time, and stores CPU and memory as the collector does now.
  Slurm keeps step usage only as long as slurmdbd's purge settings allow,
  often far less time than it keeps the jobs; a job whose usage it no
  longer has keeps what nomad stored (1.13.2), and the status says how much
  is still unmeasured.

Both run on each site, not on the hub; the next sync brings the results.

### Bringing back measurements from an older copy

In 1.13.0 and 1.13.1, a job read again without usage (Slurm had purged its
steps) was stored as "not measured" over the measurement nomad had taken
when the job ended: `nomad impact backfill` could erase months of them.
1.13.2 never writes an empty reading over a stored one. What was erased can
come back from an older copy of the site's database, or of a hub's combined
database, from before the backfill:

```
nomad impact extract --from OLD/combined.db --site SITE --out SITE-measurements.db
nomad impact restore --from SITE-measurements.db            # on the site: a dry run
nomad impact restore --from SITE-measurements.db --apply
```

`extract` reads the copy (read-only) and writes a small file with one row
per measured job: its number, submit time and measurements, nothing else.
`restore` runs on the site: it fills in only jobs that have no measurement
now, and only the same job: the submit times are known on both sides and
agree (a job kept under NUMBER@SUBMIT when a later job took its number is
found there). A value the copy doesn't have never replaces one stored. It
counts the rest: measured since, another job under the same number, no
submit time to tell (left alone), not on the site. Restored rows were
measured the old way and the report counts them so (below). An older copy
of the site's own database works as `--from` too, without `extract`. Not on
the hub: the next sync brings them there. `--apply` first copies the
database next to itself (`--backup PATH` elsewhere; SQLite's backup, safe
while the collector writes) and stops if it can't.

## The report

```
nomad impact report [--site SITE] [--source PART] [--advice-db PATH] [--json]
```

`--source` is `clusters`, `interactive`, `workstations`, `login`, `advice`
or `all` (the default: every part that has data).

### Clusters

Month by month from `baseline_from`: jobs, people and how many were new,
the share of jobs measured (of those that ran 10 s or more: the others'
figures rest on them), CPU used of the core-hours measured jobs held, memory peak of what was
asked for, time used of the time limits of completed jobs, the share that
failed, timed out or ran out of memory, out-of-memory jobs, and the median
wait from when a job could start. Events appear in their month. Then the
periods side by side: before launch, after it, and the same days a year
earlier where the data reaches back (students and courses follow the
academic year). The figures are the ones the Console and `nomad facts`
use. When more than a fifth of the measured jobs before launch were
measured before 1.13 (CPU from AveCPU, which reads jobs running several
tasks low), the report says their CPU can't be set against after launch;
memory, time, failures and waits can. Counts and shares only: no names.

### Interactive servers

From the interactive collector's daily totals: people, hours with at least
one session open (in all and per person), the share of the measured time
notebooks, kernels and RStudio sessions were computing, and their measured
session-hours that weren't. The Jupyter server's own process isn't
counted: it polls all the time, a few percent of a core, which until 1.13.2
made anyone with JupyterLab open look busy all the time (the report reads
the sessions' own rows, so this holds for the history too). Days rebuilt
from the raw snapshots (`nomad interactive backfill`) have hours but no
CPU, so the computing share starts where the collector began keeping CPU;
the report says from when.

### Lab workstations

From the workstation collector's per-person snapshots: sessions (from
signing in to the last program ending, counted in the month they ended),
hours, core-hours used, long CPU-heavy runs (12 hours or more keeping half
the machine's cores busy: what the Console advises moving to a cluster) and
sessions whose programs held 80% or more of the machine's memory.

A session is the person's slice on the machine (systemd's user-UID.slice)
for as long as it lives. Until 1.13.2 sessions were told apart by the
slice directory's change time, which the kernel moves whenever a login,
terminal, ssh connection or cron job starts or ends there: one person's day
came out as many sessions. Now a session runs while the slice's CPU time
and memory peak keep growing, as they do until it is made again; this holds
for the readings already collected too.

Memory is the programs' own (memory.stat anon + shmem, read from 1.13.2
on). memory.peak, all there was before, counts the file cache: a session
that read large files looked as if it had filled the machine. Sessions
without the new reading aren't counted as pressured or not (a dash), and
the Console doesn't advise on their memory.

### Shared login hosts

From the per_user collector: heavy-use episodes as `nomad per-user` merges
them (and how many were actionable), the people flagged, everyone who used
the host and the core-hours they used there.

### Advice shown and taken

The Console notes, once a day, the advice it showed someone about their own
work (Console fix 33 and later; the Console account's
`~/.nomad-console/activity.db`, or `[impact] advice_db = "PATH"`, or
`--advice-db`). For each piece of advice on a cluster partition, the
person's typical request there in the 60 days before they first saw it and
the 60 days after: "took it" when it moved at least half the way to the
suggestion, "partly", "no change", or "too early" while there aren't three
later jobs yet. For the advice to move long workstation runs to a cluster:
whether the person's long runs fell to half or less and their jobs on that
cluster grew. Plus how many people opened their own pages each month.

There is no comparison group here (people with the same advice who never
saw it), so part of a change may be requests drifting toward use anyway.

### Reading it

What the report can say is what changed after launch, not why. The people
and courses change from one term to the next, and the events changed things
too. The closest thing to cause is whether a person's next jobs changed
after they were shown advice (above). The other sources started at
different times (workstations and login hosts only recently, at most
sites), so their before periods can be short: `nomad impact status` says
from when each has data, and the same months a year apart will say more
than the first before and after.

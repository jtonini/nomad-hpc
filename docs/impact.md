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
```

`launch` splits the jobs into before and after. `events` is a log of
everything else that changes how people use the machines: nodes added, a
Slurm limit, a workshop, a new part of NØMAÐ itself. Write them down when
they happen; a report a year later names them instead of crediting NØMAÐ
with what they did. `nomad config check` says what in the section it can't
use.

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
  Slurm keeps step usage only as long as slurmdbd's purge settings allow;
  what it no longer has stays unmeasured, and the status says how much.

Both run on each site, not on the hub; the next sync brings the results.

## The report

```
nomad impact report [--site SITE] [--json]
```

Month by month from `baseline_from`: jobs, people and how many were new,
CPU used of the core-hours measured jobs held, memory peak of what was
asked for, time used of the time limits of completed jobs, the share that
failed, timed out or ran out of memory, out-of-memory jobs, and the median
wait from when a job could start. Events appear in their month. Then the
periods side by side: before launch, after it, and the same days a year
earlier where the data reaches back (students and courses follow the
academic year). The figures are the ones the Console and `nomad facts`
use. Counts and shares only: no names.

What the report can say is what changed after launch, not why. The people
and courses change from one term to the next, and the events changed things
too. The closest thing to cause is whether a person's next jobs changed
after they were shown advice; that needs a record of who was shown what,
which the Console keeps from the version that records it.

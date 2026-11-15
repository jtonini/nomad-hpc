# Lab reports each term

Usage policies often promise each lab's sponsor a report every term: what
the lab used of the clusters and of research storage, and how well its jobs
fit what they asked for. `nomad usage-report labs` writes one for each lab,
for the PI who leads it, and a summary of every lab for research computing.

```bash
nomad usage-report labs                         # the last term that ended, written
nomad usage-report labs --send                  # ... and mailed (review_to while it is set)
nomad usage-report labs --term current          # the term under way, so far
nomad usage-report labs --lab smith$ --term current
nomad usage-report labs --from 2026-08-25 --to 2027-01-09
sudo nomad usage-report labs timer --apply      # make and mail them as each term begins
```

Labs that hold condos get the monthly condo report ([Condo reports](condo-reports.md))
and are left out here, unless `term_condo_owners = true` in `[report.labs]`
(or `--condo-owners` for one run).

## Setting it up

The term reports use the same `report.toml` as the condo reports, and the
same delivery settings in `[report.labs]`: `mail_domain` (a PI's address is
NETID@mail_domain), `send_from`, `review_to`, `summary_to`, `formats`,
`name_members`, `contact` and the branding. Three settings are their own:

```toml
[report.labs]
terms = { "08-25" = "Fall", "01-10" = "Spring", "05-10" = "Summer" }
term_condo_owners = false
term_out_dir = "/var/lib/nomad/reports/labs"
```

- `terms`: the day each term begins (MM-DD), with its name. Each term runs
  to the next one's first day. A list (`["08-25", "01-10", "05-10"]`) works
  too; the reports then go by dates. The default is the three terms above.
- `term_condo_owners`: labs that hold condos get a term report as well.
- `term_out_dir`: where the files go (default: the data directory's
  `reports/labs`). Each lab's are in a folder of its own, named for the
  term (`2026-08-25_2027-01-10.html`), and the summary's in `summary/`. They
  are readable only by the account that writes them.

Then, on the hub:

1. `nomad usage-report labs --term current` writes the term so far: read a
   few labs' reports.
2. With `review_to` set, `nomad usage-report labs --send` mails every report
   there, each marked with the PI it would go to. A term under way is never
   sent: its reports go out once it is over.
3. `sudo nomad usage-report labs timer --apply` installs the timer: on the
   first day of each term, at 07:30 (`--at`), it makes and mails the reports
   of the term that ended. Run it again after changing `terms`.
4. When the reviewed reports read right, clear `review_to`: from then on each
   goes to its PI.

What was sent is kept beside the summary (`summary/TERM.sent.json`): a second
run doesn't mail a report twice (`--resend` does).

## What a PI gets

1. **The term:** its dates, how many of the lab's people ran jobs, on how many
   clusters, and the PI.
2. **Core-hours, GPU-hours, jobs ended** (the share that failed) and **the
   median wait**, then the same by cluster, with how much of the cores the
   jobs held they kept busy and how much of the memory they asked for they
   used at their peak.
3. **How jobs ended:** completed, failed, out of memory, timed out,
   cancelled, node failure. When the lab's jobs used less than `use_below`
   (`[report.policy]`, a quarter by default) of the cores or memory they
   asked for, it says that asking for nearer what jobs use lets them start
   sooner.
4. **Members:** each person by NetID (`name_members`), with their core-hours,
   GPU-hours, jobs, failures, CPU and memory used, and storage; members who
   ran nothing and hold no space are counted.
5. **Storage:** the lab's space on each server path, and how it changed over
   the term. Paths that hold copies of another server's are left out.
6. **Compared with the term before:** core-hours, GPU-hours, jobs ended and
   people, where NØMAÐ has job records from then.
7. **What the figures rest on.**

Research computing's summary has a row per lab: its PI, how many of its
people ran jobs, core-hours, GPU-hours, jobs, failures, CPU and memory used,
the median wait and storage, and which labs were left out because they hold
condos.

## How the figures are made

- **Labs and members** are the lab check's: every lab by `[console.labs]` in
  nomad.toml (`group_pattern`, `leads`, `not_labs`; not a course) that has a
  PI, with its members as the groups collector reads them now, and the PI. A
  lab of its PI only gets a report too. Someone who left during the term
  isn't counted; someone in two labs counts in each. A lab whose people ran
  no job and held no space all term gets no report; the summary names it
  (asked for by `--lab`, it is written anyway).
- **Core-hours and GPU-hours** are each job's cores (Slurm's allocation,
  else the request) or GPUs times the hours it ran inside the term, on every
  cluster the hub reads.
- **How jobs ended, CPU and memory used** are for the jobs that ended in the
  term and were measured (job_summary), as How I'm doing works them out.
  Failed is of the jobs that ran to an end: failed, out of memory or timed
  out.
- **Waits** are for the jobs that started in the term: from when each could
  start (Slurm's Eligible time, else its submission) to its start.
- **Storage** is the latest reading of who holds each path's space before the
  term ended, against the latest before it began.
- **The term before** is the one before by `terms`; for `--from`/`--to`, the
  same length before.

# Facts

Three questions every front to nomad asks, answered once, as data:

| | What it answers | Who may ask, in the Console |
|---|---|---|
| **sites** | every site now (freshness, nodes, CPU, memory and GPUs in use, jobs running and waiting, storage, alerts, workstations) and its jobs over a window | anyone signed in: aggregates only |
| **me** | one person's work: their jobs over a window, running and waiting now, score and advice | that person |
| **lab** | the labs a person leads: members, their jobs taken together, the lab's workstations and storage | the lab's PI; staff for any lab |
| **course** | the courses a person teaches, this term: the class together, each student as a row, when the class works | the course's instructors; staff for any course |

The Console serves them at `/api/v1/sites`, `/api/v1/me` and `/api/v1/lab`;
a dashboard widget elsewhere (an Open OnDemand page, say) or an assistant
answering questions reads the same answers, so no two of them can disagree.
On the command line:

```bash
nomad facts sites                 # every site, jobs over the last 30 days
nomad facts me NETID --days 90    # one person
nomad facts lab NETID             # the labs NETID leads
nomad facts sites --json          # the data itself
nomad facts lab NETID --mask      # no NetID, lab, machine or storage names, for sharing
nomad facts course --instructor NETID   # the courses NETID teaches (needs [console.courses])
nomad facts course GROUP              # one course
```

They read the hub's `combined.db` when there is one, else this host's own
database (`--db` to choose). Nothing is written.

## What every answer carries

```json
{
  "api": "nomad.facts/1",
  "kind": "sites",
  "generated_at": "2026-10-11T07:00:00",
  "window": {"days": 30, "since": "2026-09-11T07:00:00", "until": "2026-10-11T07:00:00"},
  "notes": ["..."]
}
```

`api` changes only when a field changes meaning or goes away; new fields
can appear at any time.

Two rules hold throughout:

* **Nothing measured, no number.** A share that rests on no measurement is
  `null`; "CPU used" is over the jobs whose CPU use nomad measured, and says
  how many those were (`jobs_measured`). Nothing is shown as 0 for lack of
  data.
* **Each site against its own latest reading.** A site that has not reported
  for two hours is `stale`: its figures are what it last said, never
  averaged in as if current, and it is never dropped. Its nodes, GPUs and
  queue are judged by the node collector's own latest reading; jobs
  "running now" at a site that stopped reporting are listed, marked stale,
  and left out of the totals.
* **No figure that is one person's in disguise.** Where the caller says so
  (the Console does, for anyone but staff), a site's job figures resting on
  3 people or fewer, and a lab's totals (and how many members ran jobs)
  resting on fewer than 3 members besides the one asking, are left out
  (`"suppressed": true` and why). The Console also gives anyone but staff
  fixed windows (7, 30, 90 or 365 days), so answers a day apart can't be
  subtracted. That stops reading one person off one answer, not combining
  several: two windows, two labs, or a site's total and a lab's can still
  come down to one person. These figures are not anonymised; what a PI may
  see of each student is the institution's decision.

## Jobs, wherever they appear

The same calculation for a site, a person and a lab, over the jobs that
ended in the window:

| Field | Meaning |
|---|---|
| `finished` | jobs that ended in the window |
| `outcomes` | completed, failed, timeout, out_of_memory, node_fail, cancelled, preempted, other |
| `ran_to_an_end` | finished, less cancelled, preempted and other |
| `failed_pct` | failed, timed out or out of memory, of those that ran to an end; node failures are the machine's and in neither count (the Console's Dashboard counts them as failures, of every job that ended, until it reads these facts) |
| `people` | how many people's jobs these are (a count, never names) |
| `core_hours.requested` | cores × run time asked for, all jobs |
| `core_hours.used_pct` | core-hours used over core-hours requested, over the jobs measured |
| `memory.used_pct` | memory at peak over memory requested, by memory-hours, over the jobs measured |
| `memory.set_aside` | peaks larger than any node at the job's site: not measurements (Slurm's `jobacct_gather/linux` counts shared memory once per process) |
| `gpus.jobs`, `gpus.gpu_hours_requested` | GPUs asked for; their use per job is not measured here |
| `wait_minutes.median`, `.p90` | time from submission to start |

## sites

For each site (the hub's `sync_sites`, so a site whose sync fails is still
listed):

* `status`: `ok`, `stale` or `no_data`; `last_data` and `age_minutes`, from
  its newest reading in any table; `sync.failing` and `sync.last_success`
  on a hub (the reason stays in the hub's sync log).
* For a Slurm site: `nodes` (up, drained, down, not reporting, and the
  unavailable ones; Slurm's `reason` for each only when asked for, since an
  admin's free text can name people: the Console gives it to staff), `cpu` and `memory` (`allocated_pct`:
  what Slurm has handed out, not what jobs use), `gpus` (`busy_pct` from
  DCGM, over the GPUs it measured: nvidia-smi's "utilization" is any kernel
  running at all, and is not used), `queue` (running and waiting now) and
  `jobs` over the window.
* `workstations`: how many it collects and how many are online now.
* `storage`: each filesystem read in the last week: size, use, how fast it
  is filling over 30 days (least squares over daily averages of the fall in
  free space, which also catches a shared pool filling while this
  filesystem's own use stays flat) and days until full at that rate;
  `same_as` when another site or path reports the same size and use.
* `alerts`: conditions holding now, by severity (counts only: an alert's
  message can name things a viewer should not see).

## me

`person.username`, `jobs` (as above), `jobs_by_site`, `now` (running and
waiting, by site, with whether that site is stale), and `score` and `advice`:
the same as `nomad edu me` and the Console's My Activity, from the same code.

## lab

For each lab the person leads, by nomad.toml's rules (`[console.labs]`,
`[console.storage]`; the same labs as `nomad console roles NETID` and the
Console's My Lab):

* `members`: current members (seen in the groups collector's latest run,
  with an account), how many ran jobs in the window, how many the group
  lists without an account or no longer lists.
* `jobs` and `now`: the members' jobs taken together.
* `workstations`: the lab's machines, online or not, last seen.
* `storage`: each server or export the lab has, with its size and use;
  `shares_free_with` for exports of one pool (their free space is one).

Lab-wide figures only: no row is about one member. How much a PI sees of
each student is the institution's decision, and the Console's own pages
carry whatever it decides. A member's jobs count wholly toward the lab, and
someone in two labs counts in both: group membership is all there is to go
on.

## course

For each course the person teaches this term, by nomad.toml's
`[console.courses]` (see docs/config.md, Courses):

* `students`: the group's members who are not faculty (`listed`); this
  term's (`this_term`: joined since the term began, or used an interactive
  server or a cluster since); those left out, as from earlier terms (nomad
  saw them join before) or not known (in the group since before nomad 1.11
  recorded join dates, and not active: earlier students, or some who haven't
  started); `used_a_server`, `not_yet`, and `open_never_computing` (had
  sessions open while computing was measured, and never computed: possibly
  stuck).
* `interactive`: hours open and computing for the class, week by week, and
  from when each was recorded.
* `each_student`: one row per student, busiest first: hours open in a
  typical week (the median over the term's complete weeks) and the range of
  their weeks (10th to 90th percentile), the same for hours computing over
  the weeks it was measured, CPU-hours, peak memory, memory kills, weeks
  active. This is what a caterpillar plot draws: one row per student, a dot
  for the typical week, a bar for the range. `nomad facts course` prints it.
  Rows are "student 1", "student 2"... unless `[console.courses] names` is
  true (the institution's decision).
* `when`: students on, and computing, on average at each hour of each
  weekday (from nomad 1.11.1's hourly totals).
* `oom`: the students' sessions the kernel killed for lack of memory.
* `jobs`: their cluster jobs, taken together.

A week before a figure was recorded is left out of it, never counted as
zero: hours open from nomad 1.11 (earlier weeks once `nomad interactive
backfill` has rebuilt them), computing from 1.11, hours of the week from
1.11.1. With `min_students` set, a course with fewer students this term shows
counts only. Unnamed rows are not anonymous to an instructor who knows the
class: in a course of three, the rows are three people.

## Using the functions

```python
from nomad import facts
conn = facts.open_readonly("~/.local/share/nomad/combined.db")
facts.sites(conn, days=30, config=config)
facts.person(conn, "jdoe", days=30, config=config, db_path=path)   # db_path: score and advice
facts.labs(conn, config, "pi1", days=30, min_others=3)             # what a PI is given
facts.labs(conn, config, groups=["pi1$"])                            # staff, by group
facts.sites(conn, reasons=True)                                      # staff: node reasons
```

The functions decide nothing about who may ask: the caller does.

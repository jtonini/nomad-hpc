# Facts

Three questions every front to nomad asks, answered once, as data:

| | What it answers | Who may ask, in the Console |
|---|---|---|
| **sites** | every site now (freshness, nodes, CPU, memory and GPUs in use, jobs running and waiting, storage, alerts, workstations) and its jobs over a window | anyone signed in: aggregates only |
| **me** | one person's work: their jobs over a window, running and waiting now, score and advice | that person |
| **lab** | the labs a person leads: members, their jobs taken together, the lab's workstations and storage | the lab's PI; staff for any lab |
| **course** | the courses a person teaches, this term: the class together, each student as a row, when the class works | the course's instructors; staff for any course |
| **busy** | when each cluster, interactive server and group of workstations is busiest: the most in use at once and when, a typical week, the busiest and quietest hours (1.14.7) | anyone signed in; each workstation by name for staff only |

The Console serves them at `/api/v1/sites`, `/api/v1/me` and `/api/v1/lab`;
a dashboard widget elsewhere (an Open OnDemand page, say) or an assistant
answering questions reads the same answers, so no two of them can disagree.
On the command line:

```bash
nomad facts sites                 # every site, jobs over the last 30 days
nomad facts me NETID --days 90    # one person
nomad facts lab NETID             # the labs NETID leads
nomad facts lab NETID --each      # each member too, by NetID, and what needs attention
nomad facts sites --json          # the data itself
nomad facts lab NETID --mask      # no NetID, lab, machine or storage names, for sharing
nomad facts course --instructor NETID   # the courses NETID teaches (needs [console.courses])
nomad facts course GROUP              # one course
nomad facts busy --days 90 --each     # when each machine is busiest, each workstation too
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

* `each` (with `names=True`, `--each` on the command line): a row per
  member by NetID: jobs finished in the window, core-hours held and the
  share used, memory used of what was asked, GPU-hours, the median wait,
  and jobs running and waiting now. Most core-hours first.
* `attention` (with names too, 1.17.6): what needs the PI's attention now,
  most pressing first, each item with who (`user`, or none for a machine),
  how many, up to three of the jobs it is about (`job_id`, `site`,
  `submit_time`), and a sentence saying it (`says`); `attention_days` is the
  window it looks at. From the last 7 days, what is waiting now, and the
  latest storage readings:

  | `kind` | When |
  |---|---|
  | `waiting` | a member's jobs waiting more than 24 hours (and under 60 days) for something that won't change by itself: a job it depends on that can never run, a hold (also `REQUEUE_HOLD` and the like), a request beyond a partition's or a per-job limit, an account or QOS that won't take it. Not limits that lift as jobs end (per user or per group), nor the cluster's own reasons (its turn, free nodes, nodes down, drained or reserved, a partition down), nor at a site that stopped reporting. One item a member, the longest wait first |
  | `out_of_memory` | a member's jobs that ran out of memory |
  | `timeout` | a member's jobs that hit their time limit or their `--deadline` |
  | `failing` | 3 or more failed jobs, at least half of a member's jobs that ran to an end (node failures aside) |
  | `quota` | a member at 90% or more of a quota, where there are quotas |
  | `storage` | a lab storage server or export 90% full or more (a current reading) |
  | `data` | the lab's 3 biggest holders of data (100 GB or more each), and anyone whose data grew by 100 GB or more in the last 7 days: how much, where (server and path, with the note `[console.storage]` gives the server, and "the lab's" on its own storage), how much it changed, and how much the latest scans (of the last 8 days) found unchanged for a year (modified, not read). From the servers the `storage_use` collector reads; copies (backups, replicas) and owners the server can only number are left out. A reading more than 2 days old is said with its date, and no change is said from it, nor from readings much further apart than the window |
  | `over_cpu` | a member whose measured jobs held 100 core-hours or more and used under 25% of them |
  | `over_memory` | a member whose jobs used under 20% of the memory they asked for, over 3 measured jobs or more, where Slurm holds the memory a job asks for |
  | `workstation` | a lab workstation that hasn't answered for 2 hours |

  Every limit is nomad.toml's to change, `[console.labs.attention]` (see
  [configuration](config.md)). Nothing else is listed, and nothing says
  "all fine": an empty list is a lab where none of these checks found
  anything, which may be because nothing was measured; `attention_unchecked`
  names a check that couldn't be made (`storage`: the readings couldn't be
  read just then). The one asking is
  left out (their own jobs and data are on their Home and in
  `nomad facts me`), and a research assistant isn't told about the lab's
  leaders, as in `each`; staff, naming the lab, see every member.

Lab-wide figures unless names are asked for. How much a PI sees of each
student is the institution's decision: the Console gives each member's row
to staff and to the lab's own PI, for that lab. Without names, no row is
about one member. A member's jobs count wholly toward the lab, and
someone in two labs counts in both: group membership is all there is to go
on.

## course

For each course the person teaches this term, by nomad.toml's
`[console.courses]` (see docs/config.md, Courses):

* `students`: the course's people who are not faculty (`listed`: its
  group's members, and whoever used its environments this term, on two
  days or more); this term's (`this_term`: joined since the term began, or
  used an interactive server or a cluster since; for a course whose
  environment's users are mostly not in its group, `by_environment`:
  joined, or used the environment); those left out, as from earlier terms (nomad
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

Each course says who teaches it and why (`instructors_why`: `added` by
name, `confirmed` (they said so this term), `only` faculty member among its
people, `joined` its group this term, `environment`: used its environment
this term, `active` this term where its students work, those sites in
`active_on`; see Courses in config.md), and how many of its faculty members
were left out or said no. `environments` lists the environments whose users
this term are among its people (nomad 1.12.5). Asked for every course, one
nobody teaches this term is left out and listed in `not_shown` with the
reason. `answers` (from `nomad.config.course_answers.load`) is what faculty
members said this term.

For the Console's questions: `facts.courses.questions(conn, config, netid,
answers)` lists the courses that have `netid` as a faculty member, in its
group or a user of the environment named for it (with students this term, or already
answered): their answer and whether nomad takes them as teaching it;
`facts.courses.may_answer(conn, config, netid, group)` says whether they may
answer for a course. `facts.teaches(conn, config, netid, answers)` names the
courses a person teaches without their figures (a menu), and
`facts.every_course(conn, config)` every course this term, for staff to
choose from.

For whoever sets courses up: `facts.courses.discover(conn, config)` (what
`nomad course discover` prints) lists the environments people had a session
in this term (people, faculty members among them, the courses each counts
for, whether it is named like a course, the group most of its other users
are in) and the groups that are not courses with a faculty member and people
active this term (whose own group each is, the environment most of them
used), with how many courses there are and how many nobody teaches. It
names NetIDs, groups and environments.

A week before a figure was recorded is left out of it, never counted as
zero: hours open from nomad 1.11 (earlier weeks once `nomad interactive
backfill` has rebuilt them), computing from 1.11, hours of the week from
1.11.1. With `min_students` set, a course with fewer students this term shows
counts only. Unnamed rows are not anonymous to an instructor who knows the
class: in a course of three, the rows are three people.

## busy

For each cluster, interactive server and group of workstations over the
window: the most in use at once and when, the average, a typical week (each
weekday's hour averaged over the window) and the three busiest and quietest
hours. Machines only, never a person. Workstations go by lab: a machine
`[console.labs.resources]` lists for a lab, or its collector tags with the
lab's group (its latest `department`, as for My Lab), is the lab's, in every
lab that lists or tags it; machines in no lab go by the site that collects
them. A machine read by two sites counts once. Anything nomad.toml lists as
storage (a lab's storage, `[console.storage]` servers and their names, the
storage collector's devices) is left out, even when the workstation
collector reads it too; names match in any case and without their domain
(a NAS another site collects needs its `[console.storage]` entry on the
hub). Machines and labs are listed by
name (`each`, `label`), and both can be named for people, so the Console
shows a lab to staff and to its own PI only, and `--mask` numbers them.
CPUs are as each system counts them: Slurm's CPUs on the clusters, and a
hardware thread on servers and workstations (two per core with
hyperthreading).

- **Clusters** (node_state): cores allocated (CPUAlloc) of the cores of the
  nodes in service. A node down, not responding, failing, in maintenance, or
  drained with nothing running is out of service: left out of the totals and
  counted apart (`nodes_out`: on average and at most).
- **Load** (CPULoad): each node's counts up to its cores, for the most at
  once as for the average. Load counts threads ready to run as well as
  running, and processes waiting on storage, so a node whose storage stalls
  can read far above its cores; `over_cores_pct` and `over_cores_readings`
  say how often a node read above its cores. Where jobs are held to their
  cores (task/cgroup), that means jobs started more threads than cores they
  asked for, or were waiting on storage. Load is not cores in use.
- **Memory allocated** (AllocMem), or `"tracked": false` where Slurm doesn't
  count memory (SelectTypeParameters without `_Memory`), never 0%.
- **Memory held**: on the nodes running jobs, each node's memory less what
  Slurm reports free, as a share of the memory of all the nodes in service.
  Slurm's free memory is the kernel's free memory (MemFree), which counts
  the file cache as used, so this is the most the jobs could have used: jobs
  used this much or less. Nodes running nothing count as none (whatever they
  hold is cache, or left behind, not a job's). A free figure of 0 (Slurm's
  `FreeMem=N/A`, from a node that hasn't reported) is not counted.
- A cluster whose every reading in the window had every node out of
  service has no figures; it is listed in `clusters_all_out` instead.
- Readings that aren't numbers (text in a number column) are not counted.
- **Interactive servers** (interactive_hourly): people with a session open
  (the most in an hour, and the hour's average), people computing, the
  memory their sessions held, and the machine's own busy cores
  (mpstat_summary) where the collector runs on it (one server at the site).
- **Workstations**: the cores people's sessions kept busy, from each
  person's CPU counters (workstation_user_snapshot, accounts from uid 1000):
  everything in their user slice, logins, terminals and cron jobs; the
  machine's own services are not included. Memory in use is the machine's,
  cache left out (workstation_state). A machine is busy with a core or more
  in use. The group's figures add each machine's ten-minute averages
  (machines aren't read at the same instant). The workstation collector's
  own CPU percentages were never measured, and are not used. A machine whose
  per-person probe read only its own accounts (the collector's root login,
  the login screen: uid below 1000) reads 0, nobody used it; one with no
  per-person readings at all says its CPU is not measured.

An hour of the week is ranked busiest or quietest only with readings on two
different days or more, and nothing is ranked when every hour is the same.
A site whose last reading is more than two hours old is marked `stopped`. Times
are as the collectors wrote them: each site's local time.

## Using the functions

```python
from nomad import facts
conn = facts.open_readonly("~/.local/share/nomad/combined.db")
facts.sites(conn, days=30, config=config)
facts.person(conn, "jdoe", days=30, config=config, db_path=path)   # db_path: score and advice
facts.labs(conn, config, "pi1", days=30, min_others=3)             # what a PI is given
facts.labs(conn, config, groups=["pi1$"])                            # staff, by group
facts.sites(conn, reasons=True)                                      # staff: node reasons
facts.busy(conn, days=30, names=False)                               # machines numbered, not named
```

The functions decide nothing about who may ask: the caller does.

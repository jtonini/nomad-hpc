# Weekly notices to people (1.39.0)

What nomad tells a person on the Console's My activity reaches them only if
they look. Once a week at most, nomad emails each person who has something
new there, with what happened, the better way, and a link to My activity.

## What an email says

- **On the login nodes**: what of theirs a login node's monitoring caught in
  the last week, as My activity shows it ([collectors](collectors.md#told-to-the-person-1380)):
  a long computation, many programs busy at once, a lot of memory, a large
  transfer. Copying data in and out (`scp`, `sftp`, `rsync`, and the `sshd`
  sessions they run in) is never told.
- **Your jobs**: a pattern over their recent jobs of asking for far more
  than they used: most of at least `min_jobs` (5) jobs on one partition, of
  severity `high` or `critical` by the same rules as `nomad edu me`, with
  the value to ask for instead (`#SBATCH --mem=8G`). Only for people who
  finished jobs in the last week.
- **Asking for far more time than the jobs ran** (1.62.0) is told only when
  it cost the person waiting. A long time request matters when the cluster
  is busy: the scheduler can't fit the job into gaps a shorter one would
  fit. On a quiet cluster every job starts at once, whatever it asks for,
  and an email about it would only be noise. So nomad compares how long
  the person's jobs waited to start (for the queue: an array by its first
  task) with other people's jobs on the same partition, sent in the same
  days (a day either side), of the same size (half to twice the cores; GPU
  jobs with GPU jobs) that asked for about the time they ran (at most twice
  it, or an hour more). The note goes when theirs waited at least
  `waited_minutes` (30) longer and `waited_times` (2) as long, with at
  least `compare_min_jobs` (10) jobs to go by, and gives both waits. Without
  enough jobs like theirs nomad can't tell, and it isn't told. My activity
  shows the advice either way. `time_if_waited = false` tells it as before.
- **Jobs that ran short** (1.51.0): jobs that reached their time limit or
  ran out of memory, or came close (at least `short_min_jobs`, 2), with the
  value to ask for instead from the person's own jobs, and that the work
  since a job last saved its progress is lost. Jobs sent from NØMAÐ's
  Submit page are checked for this before they go.
- **Jobs that stopped within a minute** (1.51.0): at least `quick_min_jobs`
  (3) jobs on a cluster in the last week that stopped with an error within
  `quick_seconds` (60) of starting, by job ID, with what usually causes it
  (a path with a typo, a module the script doesn't load, a program the
  cluster doesn't know, a script that isn't executable) and that the first
  lines of the job's output file say which. Jobs killed for memory or
  stopped at their time limit are told as jobs that ran short instead.
- **Getting started** (1.51.0): someone whose first job on a cluster was in
  the last `new_days` (30), most of whose jobs there haven't completed,
  hears that this is common when starting out and gets better quickly (on
  the clusters NØMAÐ was first run on, people's first ten jobs failed about
  half the time, about three in ten after), and that Research Computing
  will look at a script with them. Once a cluster.

What is alike is one note: the times of one kind of login-node use (up to
five shown, the rest on My activity), or one kind of asking for too much
over a person's partitions, each with its `#SBATCH` line; why it matters
and the better way are said once a note (1.39.1). Sizes and times are in
words ("32 GB", "1 h 38 min"), and the email has no contractions.

**How it is laid out (1.62.0).** The email goes formatted (HTML), with a
plain copy for mail programs that show only text. Its notes are numbered:
login nodes first, then jobs that stopped within a minute, jobs that ran
short and getting started, and asking for too much last. Each note has a
heading, then what NØMAÐ saw, the jobs it is about, what to ask for instead
(the `#SBATCH` line in a box of its own, and the value in words: "9 days
15 h: enough for the longest of these jobs, with some margin"), what to do,
and why. What they used is said as most jobs' use and, where it differs a
lot, the longest or the most, so the value to ask for makes sense. The
formatted copy sets no colours for text or background, so a mail program's
dark mode reads it as well as its light one.

```
Subject: Research computing: 2 notes on your recent work

Hello,

NØMAÐ, the monitoring that Academic Research Computing runs on the clusters,
noticed 2 things in your recent work that could go better. Nothing is wrong
and nothing has been changed: this is only to help.

1. Some of your jobs stopped within a minute

On hpc3, 186 of your jobs this week stopped with an error within a minute of starting.
Jobs: array 13213 (180 tasks) and array 12269 (6 tasks)

What to do: Look at the first lines of the output file of one of these jobs (slurm-JOBID.out, ...): they usually say what was missing.

Why: A job that stops this soon usually could not find or start something: ...

2. Your jobs ask for more time than they use

On hpc3 (cpu), 15 of your last 15 jobs asked for more time than they ran: they typically asked for 365 days; most ran about 2 h 10 min, the longest 6 days 10 h.
They waited about 3 h 20 min to start (the median). Jobs of the same size on the same partition, sent in the same days, that asked for about the time they ran waited about 4 min (the median of 37 jobs).
Jobs: array 13212 (15 tasks)
Ask for this instead:
    #SBATCH --time=9-15:00:00
That is 9 days 15 h: enough for the longest of these jobs, with some margin.

Why: Jobs that ask for less time start sooner: ...

Your recent work in detail, and other ways to do it: https://nomad.example.edu/me

These emails come at most once every 7 days, and only with something new. To
stop them, turn off "Email me about my own use" at https://nomad.example.edu/me.
```

Each jobs note names the jobs it is about, newest first (eight, then "and N
more"; 1.49.0), so a person can look at each one; a job array once, with
its tasks counted (1.62.0).

## Who gets one

- At most one email a person every `every_days` (7), and only with something
  new: a login-node entry is told once; a jobs pattern again only after
  `repeat_days` (30), if it still holds.
- **On for everyone by default.** A person turns it off on My activity in the
  Console ("Email me about my own use"); an admin can for them
  (`nomad notices set NETID off --apply`), for someone who asked by email.
- **Accounts only for a course are left out**: in a course group this term
  ([courses](config.md)), in no lab (as a member, a leader or a research
  assistant) and not in the faculty group. `courses = true` includes them.
  Someone nomad knows no groups for is emailed.
- System accounts (`root`, `slurm`, ...) and `skip_users` never are: list
  the site's own service and admin accounts there (`skip_users = ["monitor",
  "hpcadmin"]`), whose builds and installs on a login node are the job. An
  address is the NetID at `mail_domain`.

## Review first

While `review_to` holds addresses, every email goes there instead, its
subject and first line saying whom it was for: read a week or two of them
before people get any. Emails go to people only once `[notices]` says
`review_to = []`. Without `review_to` in `[notices]`, report.toml's
`review_to` holds them; with none there, nothing is sent (`nomad notices` says
so). The lab check's own `review_to` never decides these: switching that
check on does not switch these on.

What review saw is still news to the person: going live, they get what is new
for them, whatever review was sent.

## Commands (on the hub)

```bash
nomad notices                          # how it is set, and what went lately
nomad notices mail                     # who is due now, and why others are not (nothing is sent)
nomad notices mail --counts            # the same as counts only, no names (to show someone)
nomad notices mail --user NETID --text # one person's email, as it would go
nomad notices mail --html FOLDER       # each email's formatted copy, FOLDER/NETID.html (1.62.0)
nomad notices mail --send              # send them (while review_to is set, to review_to)
nomad notices set NETID off --apply    # someone asked not to be emailed
nomad notices timer --apply            # every Monday at 09:10, from systemd (as root)
```

`nomad notices timer` writes `nomad-notices.service` and `.timer` (run as the
nomad account; `--at` takes systemd's calendar, `--user` another account);
without `--apply` it prints them.

## Settings

```toml
[notices]
kinds = ["login_node", "jobs"]       # what the email tells
every_days = 7                       # one email a person at most this often
link = "https://nomad.example.edu"   # the Console (default: the lab check's link, without /lab)
review_to = ["rc@example.edu"]       # while set, every email goes here; [] sends to people
greeting = "Hello,"
# mail_domain, send_from, signature, contact: as the lab check's (report.toml's)
courses = false                      # true: accounts only in a course are emailed too
skip_users = []                      # never emailed
# db = "/home/nomad/.local/share/nomad/notices.db"   # default: beside combined.db

[notices.jobs]
days = 30                            # the jobs a pattern is read from
min_jobs = 5                         # at least this many of them on the partition
severities = ["critical", "high"]
repeat_days = 30                     # the same pattern told again after this, if it still holds
short_min_jobs = 2                   # jobs that ran out of time or memory: at least this many (1.51.0)
quick_seconds = 60                   # a job that stopped with an error this soon (1.51.0)
quick_min_jobs = 3                   # ... at least this many on a cluster in a week (0: never)
new_days = 30                        # first job on a cluster this recently: getting started (0: never)
time_if_waited = true                # asking for too much time: told only when it made them wait (1.62.0)
waited_minutes = 30                  # ... at least this much longer than jobs like theirs
waited_times = 2                     # ... and this many times as long
compare_min_jobs = 10                # ... with at least this many jobs like theirs to go by

[notices.login_node]                 # what counts on a login node (collectors.md)
```

The record (`notices.db`, beside the combined database: sync never rewrites
it) keeps each email (to whom, when, review or not, how many notes), each note
told (so it isn't told twice), and each person's choice, with who made it and
when. `nomad config check` says what is wrong in `[notices]`.

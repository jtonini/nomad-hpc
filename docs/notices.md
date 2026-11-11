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

What is alike is one note: the times of one kind of login-node use (up to
five shown, the rest on My activity), or one kind of asking for too much
over a person's partitions, each with its `#SBATCH` line; why it matters
and the better way are said once a note (1.39.1). Sizes and times are in
words ("32 GB", "1 h 38 min"), and the email has no contractions.

```
Subject: Research computing: 2 notes on your recent work

Hello,

NØMAÐ, the monitoring that Academic Research Computing runs on the clusters,
noticed something in your recent work that could run better. Nothing is wrong
and nothing has been done about it; this is only to help.

On the login nodes
------------------
* A long computation on the login node (3 times)
  - python3 on spydur, Tue 10 Nov 09:29, for 40 min (1.5 cores)
    python3 fit.py --part 2
  - ...
  One program kept the processor busy for a long time on the login node, ...
  Run it as a job: in the Console, Submit a job; or start a session on a compute node
    srun --pty -c 4 --mem=16G --time=4:00:00 bash
  and run the same command there.

Your jobs
---------
* Your jobs ask for more time than they use
  - On spydur (cpunodes), 20 of your last 22 jobs asked for more time than they used (typically 1 day; they used about 1 h 38 min). Ask for this instead:
      #SBATCH --time=04:00:00
  - On spydur (gpunodes), ...
  Jobs that ask for less time start sooner: the scheduler can fit them into gaps ...

The details, and how to do it another way: https://nomad.example.edu/me

These emails come at most once every 7 days, and only with something new. To
stop them, turn off "Email me about my own use" at https://nomad.example.edu/me.
```

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
  the site's own service and admin accounts there (`skip_users = ["zeus",
  "installer"]`), whose builds and installs on a login node are the job. An
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

[notices.login_node]                 # what counts on a login node (collectors.md)
```

The record (`notices.db`, beside the combined database: sync never rewrites
it) keeps each email (to whom, when, review or not, how many notes), each note
told (so it isn't told twice), and each person's choice, with who made it and
when. `nomad config check` says what is wrong in `[notices]`.

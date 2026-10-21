# Condo reports

A condo is a set of nodes a lab bought, lent to everyone else through a
guest partition while the lab isn't using them. `nomad usage-report condos`
writes each condo's monthly report for its owner, and a summary of every
condo for research computing, grouped by cluster.

```bash
nomad usage-report condos init               # draft the condos into report.toml
nomad usage-report condos init --apply       # ... and add them to it (in a terminal, it asks how reports go out)
nomad usage-report condos status             # what is left to set up
nomad usage-report condos set --labs mail_domain=example.edu --labs review_to=rc@example.edu \
                              --confirm smith,jones --owner lee=klee --apply
nomad usage-report condos                    # last month's reports, written
nomad usage-report condos --send             # ... and mailed
nomad usage-report condos --month 2026-09 --cluster c1 --condo smith
sudo nomad usage-report condos timer --apply # make and mail them on the 1st
```

The Console does the same for admins, under Settings > Condo reports: the
settings, the condos with nomad's owner guesses, a preview of each report,
and sending a month for review.

## Setting it up

`nomad init` asks whether to register condos: for each Slurm cluster, the
guest partition (the one named like "condo" by default), then each condo it
finds there with its owner's NetID (an owner typed there is confirmed; left
blank, it is set later), the owners' mail domain and who reviews the reports
first. It writes them to report.toml beside nomad.toml. The reports are made
where a cluster's data is gathered: on that machine, or on the hub that
syncs it (copy report.toml there, or set it up there as below).

On a hub:

1. `nomad usage-report condos init --apply` drafts every
   cluster's condos into report.toml, each owner guessed and not confirmed.
   In a terminal it then asks what a first setup needs and isn't set yet:
   the owners' mail domain, who reviews the reports first (`review_to`),
   the sender and the line at the foot (`--no-input` skips this).
2. Check each owner and confirm it (`condos set --confirm NAME --apply`, or
   the Console).
3. Read last month's reports (`nomad usage-report condos`). With
   `review_to` set, `--send` mails them all there; without it, `--send`
   mails each confirmed owner theirs.
4. `nomad usage-report condos timer --apply`, as root, installs the timer
   that makes and mails them on the 1st.
5. When the reviewed reports read right, clear `review_to`: from then on
   each goes to its owner.

`nomad usage-report condos status` says what is left at any point:

- condo partitions on the nodes that report.toml doesn't list (a new
  condo), and condos it lists that are gone from the nodes;
- owners not confirmed (their reports don't go out);
- confirmed condos without an address (no `mail_domain`, no `email`), and
  no sender;
- the timer, not installed or not enabled;
- notes: review mode (every report goes to review_to) and no PDF (fpdf2
  missing).

Most lines say what to run. `nomad config check` on a hub (or wherever
report.toml lists condos) shows the same in counts, naming no condos or
owners, and the Console lists it at the top of Settings > Condo reports.

It reads the hub's combined database (`--db` names another) and the same
`report.toml` as `nomad usage-report` (`--config` names another).

## What an owner gets

Each report names the cluster it covers, in its title and on a badge, so an
owner with condos on two clusters can tell them apart.

1. **The nodes:** how many, their cores, memory and GPUs, and the partition.
2. **The month on the nodes:** of the core-hours the nodes were in service,
   the share the owner's lab used, the share guests used, and the share
   nobody used, as a bar and as figures; the node-hours in service, down,
   drained, in maintenance and not read; how many guest jobs ran there, how
   many were preempted, and how many ran longer than the guest limit.
3. **The lab's jobs on its own nodes:** of the jobs submitted to the condo's
   partition in the month, the share that started within the guarantee
   (`owner_start_minutes`, from when the job could start: Slurm's Eligible
   time, else its submission), whenever they started: a job submitted on the
   30th that starts on the 1st is the month's. A job still waiting when the
   report is made counts as late once it has waited longer than the
   guarantee. A later start is put down to guest jobs on the nodes at the
   time, the lab's own jobs filling them, or neither (dependencies, a request
   larger than the nodes, nodes out of service). Then how the jobs that
   ended did (completed, failed, out of memory, timed out, cancelled) and how
   much of the cores and memory they held they used.
4. **The lab elsewhere:** the lab's use of the cluster's shared nodes, of
   other condos as guests, and of other clusters: core-hours, GPU-hours,
   people, failures, CPU and memory used, and median wait.
5. **Members:** each member of the lab who ran anything, by NetID, with their
   core-hours on the condo, on the shared nodes, on other condos and on other
   clusters (`name_members`); the others are counted. People who ran in the
   condo's partition without being members are listed too, marked, with
   their jobs there only. Guests on the owner's nodes are only ever totals.
6. **Storage:** the lab's scratch quota and the home quota from
   `[report.policy]`. Not measured yet: once NØMAÐ reads quotas, this shows
   what is used.
7. **The past 12 months:** the lab, guest and idle shares month by month.
8. **What the figures rest on:** node-hours not read, jobs not measured, and
   what a figure can't count.

A run limited by `--cluster` or `--condo` writes and sends no summary: a
summary of some condos would stand for the month's.

Research computing's summary has one table per cluster, a row per condo
(owner, nodes, shares, guest core-hours, preemptions, how many owner jobs
started within the guarantee, node-hours out of service) and a bar per
condo. A cluster without condos is listed as having none. It also lists
what needs attention: condo partitions on the nodes that report.toml
doesn't list, condos whose partition is gone, and owners not confirmed.

## How the figures are made

- **Node-hours** come from the node readings (every 5 minutes). Each reading
  stands for the time until the node's next one, up to twice the node's
  usual interval that month (the median gap, and at least 10 minutes); the
  rest is "not read", counted as in service, and the report says how much
  there was. The interval is each month's own, so a site that read its nodes
  more often earlier in the year isn't measured by that interval now. A node counts from its first reading only when
  the cluster's other nodes were reporting before it (a new node), and until
  its last only when they went on after it (a node retired). Months before
  a cluster's first reading have no readings.
- **Core-hours** are each job's cores (Slurm's allocation, else the request)
  times the hours it ran inside the month, split evenly over its nodes. A
  job that left the queue with no outcome recorded (UNKNOWN) ran until
  NØMAÐ saw it gone. GPUs are the allocation's, or the request's where the
  site's accounting doesn't count GPUs.
- **Owner or guest:** a job on the condo's nodes is the owner's when it ran in
  the condo's partition, under the lab's Slurm account (`account`), or for a
  member of the lab; any other job there is a guest's. Idle is what is left
  of the in-service core-hours.
- **Preempted guest jobs** are those that ended PREEMPTED. A guest job that
  was requeued keeps its job number and only its last run is kept, so the
  count is a lower bound.
- **Members:** `members` in report.toml (a Unix group, a Slurm account, or a
  list); else each owner's labs by `[console.labs]` in nomad.toml (the group
  named for them by `group_pattern`, and the groups they `lead`), and the
  owners.

## Finding the condos

Nothing new runs at the sites: each node reading carries the node's
partitions. The guest partition is the one named in `guest_partitions`, or
one named like "condo". A condo is a partition whose nodes all sit inside
the guest partition. A partition over every condo node is another way in for
guests, unless it is the only condo there is.

The owner is guessed from two kinds of evidence, because each alone
misleads: a partition can be named for another of the PI's surnames, and a
condo lent out a lot is used mostly by other labs.

- **The name.** `owner_patterns` (`{name}`, `{name}lab`, `{name}_lab` and
  `{name}-lab` by default) give a last name, a number at the end being a
  second condo of the same lab (`smith2`). The owner is a NetID of up to two
  initials and that name, perhaps with a number after it (`jsmith3`), cut
  short where the institution's NetIDs stop (`netid_max_length`, 8:
  `tanderso` for Anderson): first one with a lab group named for it, then
  one that leads a lab in nomad.toml, then one that ran jobs in the
  partition, then an account. Several accounts that fit, none of them a
  lab's PI, are listed rather than guessed from. Two last names joined
  (`smithjones`) are two owners, when one of them at least is a lab's PI.
- **The users.** The lab groups (by `[console.labs] group_pattern`) of the
  people who ran jobs in the partition over the past year, the one holding
  the most of them first. When it holds more than half of them (two at
  least), it names the owner where the name names nobody, or only an
  account.

When the two point at different labs, the name's owner is kept and the
draft says to check. `init` writes each guess with its evidence and
`confirmed = false`; a report goes to its owners only once someone sets it
to true.

## Changing the settings

`nomad usage-report condos set` changes report.toml without opening it:

- `--labs KEY=VALUE` and `--policy KEY=VALUE`: a `[report.labs]` or
  `[report.policy]` setting; lists as `A,B`, numbers and `true`/`false` as such;
- `--confirm A,B` and `--unconfirm A,B`: which condos' reports may go to their
  owners;
- `--owner CONDO=NETID[,NETID]`: a condo's owner, or owners.

A condo is its name, or `CLUSTER/NAME` when two clusters have one of that
name. Without `--apply` it shows the change and writes nothing. With it, the
file is copied first and keeps its owner and permissions, and nothing is
written unless the result reads as condo settings (a misspelled setting or
an address that isn't one is refused). `--mask` prints counts instead of
names.

## Delivery

Reports are written to `OUT/<cluster>/<condo>/<YYYY-MM>.{html,pdf,json,md}`
and `OUT/summary/<YYYY-MM>.*`, readable only by the account that writes them:
they name people. `OUT` is `out_dir` in `[report.labs]`, else
`reports/condos` in nomad's data directory, else `--out`.

The email is HTML made for mail readers (tables, inline styles), with a
plain-text part and the PDF attached. Each report is headed like the
Console's sign-in page, from `[console.branding]` in nomad.toml: the logo,
the institution and the unit lines under it (`unit = ["Research
Computing", "Office of the Provost"]`). The logo must be a PNG or JPEG at a
full path; it goes inside the HTML file, inline in the email (so mail
readers show it without fetching anything), and in the PDF. `branding =
false` in `[report.labs]` leaves it all out; without `[console.branding]`,
`institution` in `[report.labs]` names the sender. The PDF needs fpdf2:

```bash
pip install 'nomad-hpc[report]'
```

Without it the reports are written and sent without a PDF, and the email
says so.

Mail goes through `[mail]` in nomad.toml. While `review_to` is set, every
report goes there, marked with whom it would have gone to, and so does the
summary when `summary_to` names someone else; clear it to send reports to
their owners and the summary to `summary_to`. What was sent each month is kept in
`OUT/summary/<YYYY-MM>.sent.json`, so a second run doesn't mail the same
report twice (`--resend` does).

### On the first of each month

`nomad usage-report condos timer` prints the timer for this install (the
nomad in use, its nomad.toml, and the nomad account, or `--user`), and
`--apply` (as root) installs and starts it; `--at` changes when (systemd's
OnCalendar, the 1st at 07:00 by default, after the night's syncs). A timer
already set up with the same settings is left as it is. It is this, on the
hub:

```ini
# /etc/systemd/system/nomad-condo-reports.service
[Unit]
Description=NOMAD condo reports
After=network-online.target

[Service]
Type=oneshot
User=nomad
ExecStart=/opt/nomad/venv/bin/nomad -c /etc/nomad/nomad.toml usage-report condos --send

# /etc/systemd/system/nomad-condo-reports.timer
[Unit]
Description=NOMAD condo reports, on the 1st

[Timer]
OnCalendar=*-*-01 07:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nomad-condo-reports.timer
```

## Settings

Everything one institution may do differently goes in report.toml:

```toml
[report.labs]                          # how the reports go out
mail_domain = "example.edu"            # owner's address: NETID@mail_domain
send_from = "Research Computing <rc@example.edu>"
review_to = ["rc@example.edu"]         # while set, every report goes here
summary_to = ["rc@example.edu"]        # the summary (default: review_to)
name_members = true                    # an owner's report names their lab's members
formats = ["html", "pdf", "json"]      # also "md"
owner_patterns = ["{name}", "{name}lab", "{name}_lab", "{name}-lab"]
netid_max_length = 8                   # NetIDs cut short here; 0: never
contact = "Questions: rc@example.edu"  # the line at the foot of each report
out_dir = "/var/lib/nomad/reports/condos"
institution = "Research Computing"     # the name at the top, without [console.branding]
branding = true                        # head reports with [console.branding]: logo and lines

[report.policy]                        # what the reports check against
owner_start_minutes = 5                # an owner's job starts on its own nodes within
guest_max_days = 10                    # the longest a guest job may run
guest_grace_minutes = 5
home_quota_gb = { soft = 50, hard = 60 }
lab_scratch_quota_tb = { soft = 20, hard = 24 }
scratch_path = "/scratch/{lab}"
use_below = 0.25                       # below this share of what jobs ask for, say so

[report.clusters.c1]
label = "Cluster 1"                    # how the reports name the cluster
guest_partitions = ["condos"]          # default: partitions named like "condo"

[report.clusters.c1.condos.smith]      # drafted by `nomad usage-report condos init`
owner = "jsmith"                       # or several: ["jsmith", "rjones"]
partition = "smith"                    # default: the table's name
nodes = "c[01-04]"                     # default: the partition's nodes
members = "group:smith$"               # or "account:smith", or a list of NetIDs
account = "smith"                      # the lab's Slurm account, if it has one
email = "jsmith@example.edu"           # or a list; default: each owner@mail_domain
cc = ["manager@example.edu"]
confirmed = true
lab_scratch_quota_tb = 30              # an agreed larger quota
```

A cluster with a `condos` table is reported as listed there. One without is
reported from what its nodes say, every condo unconfirmed, until `init`
drafts it. A setting nobody reads is refused (`reveiw_to` would otherwise
send reports to owners). A site's own database, with no hub, is the
cluster `this-cluster`, or the one cluster of its nomad.toml that report.toml
lists condos for (as `nomad init` writes them).

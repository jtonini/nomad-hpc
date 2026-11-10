# Alerts

NØMAÐ supports both threshold-based and predictive alerts.

## Alert Types

### Threshold Alerts

Trigger when metrics exceed configured limits:

```toml
[alerts.thresholds.disk]
used_percent_warning = 80
used_percent_critical = 95

[alerts.thresholds.nfs]
retrans_percent_warning = 1.0
avg_rtt_ms_critical = 100
```

Before 1.7.16 `[alerts.thresholds]` was not read: every site alerted at the
built-in values. The flat keys the example config used to show for disks
(`disk_warning_percent`, `disk_critical_percent`) are read too.

The disk thresholds also cover the disks of other machines, each alert about
that machine rather than the host that read it:

- `node_disk` (1.14): a compute node's local disk, read from the head node
  (`[collectors.node_disks]`). Since 1.14.3 the message names the three
  largest owners of the data when the collector counted them in that sweep
  (it does for a disk that has just filled): who to ask.
  `owners_in_alerts = false` keeps the names out.
- `workstation_disk` (1.14.2): a workstation's own disks (`/`, `/home` on a
  local drive, `/localscratch`, a data disk), from the workstation
  collector's mount readings. Network filesystems, fuse mounts and drives
  plugged in under `/media` are left out, and a device mounted at several
  places (btrfs subvolumes, a bind mount) is one disk. A machine collected
  as `localhost` is named as the host that collects it.

### Disk fill forecasts

Each disk reading fits the readings of the last `forecast_window_hours`
(`[collectors.disk]`, default 6) with a straight line. When the filesystem is
filling, the reading stores the rate and when it will be full
(`filesystems.fill_rate_bytes_per_day`, `days_until_full`), and an alert
(`disk_forecast`) says so:

```
[WARNING] NØMAÐ spydur: Disk /home will be full in about 45 hours (filling 2.0 TB/day over the last 6 hours; 84% used)
```

```toml
[alerts.thresholds.disk]
full_within_hours_warning = 72
full_within_hours_critical = 24
full_within_hours_past_critical = 6   # once past used_percent_critical

[alerts.predictive]
enabled = true          # false: no forecasts
```

The forecast fits the space taken (size less free space), so a dataset on a
shared pool is forecast as the pool fills. It needs four readings over at
least an hour and growth of at least 0.1% of the filesystem over the window;
readings of another filesystem (the path unmounted, `df` reading the one
beneath: a different size *and* usage) are left out, and a disk already full gets the threshold
alert instead. Past its critical threshold a disk has that alert, reminded
of daily; a forecast is added only when the rest will be gone within
`full_within_hours_past_critical` (6) hours.
`days_until_full_*` under `[alerts.predictive]` and
`disk_fill_days_warning`, which older configs carry for a forecast that never
ran, are not read.

### Collection stopped (the hub, 1.14.5)

A site can keep syncing a database nothing writes to any more, and then the
`sync` alert never fires while the pages show old or partial data. After each
sync the hub reads the merged database and raises, with source `collection`
(host = the site):

- **collection**: nothing at all collected at the site for
  `alert_after_minutes` (120): nomad's cron or service there stopped.
- **collector NAME**: a collector that runs regularly has not run, has failed
  every run, or -- one that nearly always collects something -- has collected
  nothing, for three of its usual intervals and at least
  `alert_after_minutes`. A failing one quotes its last message, with folders
  under /home, /scratch and the like, `user@host`, `~user`, `user=...` and
  the usernames the hub knows taken out.
- **a GPU node**: no GPU samples from it while the site's other GPU nodes
  report, for `alert_after_minutes` or three of the gpu collector's usual
  intervals there; all of them quiet is one alert for the site (or none, when
  the gpu collector's own alert says why).
- **DCGM on a GPU node**: no DCGM samples from it for `gpu_fallback_hours`
  (24) while nvidia-smi still answers. GPU utilization in scores comes only
  from DCGM, so its jobs say "not measured" until DCGM (nv-hostengine) is
  back. Critical after a week. A node that never had DCGM is not this. The
  gpu collector now asks each node whether DCGM answers (and again every
  hour); it used to ask the first node and use that answer for all.

A warning, critical after a day; sent once, again when worse, then every
`reminder_hours`, and forgotten when it recovers (kept in
`collection_alerts.json` beside the hub's cached copies).

What is not raised:

- A site is judged as of the hub's last live pull from it, so a site whose
  pull failed (the `sync` alert says so), or a combined database the hub held
  back, is not taken for one that stopped.
- A collector is watched once it has run six times over more than a day in
  the last 30 days; its usual interval comes from its latest runs (runs under
  a minute apart count as one), and the longest gap of its last week counts
  too, so a collector moved to a longer schedule or tried by hand isn't
  "stopped". One that has not worked once in 30 days isn't a stop (`nomad
  collectors` says why). One disabled in the site's own settings (the copy
  the hub keeps) isn't watched.
- A GPU node is watched once it has sent samples over more than a day.
  Collectors and nodes silent for more than 30 days are taken as retired.

Also (1.17.3):

- **runaway jobs**: jobs Slurm's controller no longer lists (`squeue -a`)
  but its accounting still has running or waiting, for over a day. They
  count as running or waiting wherever sacct is read, nomad's pages included,
  and only Slurm's admin can clear them: `sacctmgr show runawayjobs` lists
  them and offers to fix them. The site's Slurm collector keeps them in
  `runaway_jobs`: each job its sacct pull or a lookup has active that squeue
  doesn't list, with when it was first and last seen so. When squeue may hide
  other people's jobs from the collecting account (`PrivateData` in
  `scontrol show config` includes `jobs`, read once a day, and the account
  is neither root nor SlurmUser; if that can't be read, when squeue listed no
  other account's job), a running job squeue doesn't list counts only
  once it is a day past its start and time limit (60 days with no limit), and
  a waiting one doesn't; a pending array range, whose id squeue cuts short,
  never does (a job only just
  ended is there until sacct catches up). One squeue lists again, that sacct
  has ended, or that is no longer stored as active goes, at the next run.
  The idle-condo report leaves the ones that never started out of the jobs
  that waited. A warning, with the count and the oldest one's submit date,
  no names.

To leave some out (`name` or `site:name`, shell wildcards allowed):

```toml
[hub]
alert_after_minutes = 120
gpu_fallback_hours = 24
quiet_collectors = []          # e.g. ["slurm_usage", "lab:workstation"]
quiet_nodes = []               # e.g. ["oldgpu07", "lab:*"]
collection_alerts = true
```

`nomad collection-check` lists the same, read-only (`--send` raises them; run
it as the account that runs the sync).

### Node alerts sent by the hub (1.17.3)

A site's node alerts (a node down, not responding or draining) are raised by
that site's own nomad, through the email, Slack or webhook in its nomad.toml.
A site that can't send any -- a head node with no way to the mail server, or
one set up with `nomad init --quick`, which writes nothing in `[alerts]` --
can leave them to the hub:

```toml
[[hub.sites]]
name = "cluster-b"
host = "headnode-b"
ssh_user = "monitor"
db_path = "~/.local/share/nomad/cluster-b.db"
alerts = "hub"                 # the hub sends this site's node alerts
```

After each sync the hub reads that site's latest node reading and raises
each unhealthy node as the site's own collector would (critical for DOWN,
NOT_RESPONDING, FAIL...; warning for DRAIN...; the hub's own
`[alerts.node_state]` states apply), through the hub's `[alerts]` backends,
with source `node_state` and the node as host. Sent once, again when worse,
then every `reminder_hours`; a node healthy again is forgotten, so its return
is news (kept in `node_alerts.json` beside the hub's cached copies). Each
alert names the site, not the hub, in its subject and in the hub's alert
records; two sites' nodes of the same name are separate alerts. A node's
reason is sent without the name of whoever set it (Slurm's
`[user@time]`) and with what could name a person masked. `[hub]
quiet_node_alerts` (`name`, `site:name`, `site:*`, shell wildcards) leaves
nodes out, a node kept down on purpose say; `quiet_nodes` is for the
collection checks only (a GPU node without DCGM) and doesn't silence a node
going down. A site not read for two hours is skipped: the
sync and collection alerts say so. A send that failed is tried again when
the hub's alert database says its retry is due (after 15 minutes, then
longer), not a day later. Leave the site's own `[alerts]` without backends,
or it sends them too.

### The hub stopped reading a site (1.37.0)

The hub says so when a site stops answering. When the hub itself stops (its
disk died, it lost the network, its sync timer stopped), every page shows the
data it had and nobody is told. Each site now notices.

Each time the hub reads a site (`nomad sync`), it leaves a note in the site's
database: its name and when it read. After each collection the site's own
nomad looks at the notes, and when a hub that read it regularly has not for
`after_minutes`, it raises an alert through the site's own `[alerts]`
backends, with source `hub`:

> The hub mingus has not read spydur for 1 h (last at 2026-11-10 12:59): its
> pages show spydur as of then. If it reads no site, it is down, cannot reach
> the sites, or its sync has stopped.

A warning, critical after a day; once, again when worse, then every
`reminder_hours` while it lasts. A hub that reads again is forgotten, so its
next silence is news. Every site with alert backends says so, each about
itself: that several do is how you know it is the hub.

- A hub counts once it has read the site twenty times over at least a day:
  a trial sync is not a hub to miss. So after upgrading, the watch starts a
  day later.
- One that reads seldom is given three of its usual intervals when that is
  longer than `after_minutes`: a hub that syncs every two hours is not
  missed after one.
- Only silence the site saw counts. After the site's own outage (a reboot,
  its collection stopped for ten minutes or more), the hub has
  `after_minutes` again from when collection resumed: the site was not
  there to be read.
- A hub silent for a week is taken as retired: the site stops saying so (the
  last reminders say when), and takes out the change log it kept for it.
- A hub taken out on purpose says so first: remove the site from its
  `[hub] sites` and sync once (the site forgets it), or declare its
  maintenance (below).
- Needs nomad 1.37 on the hub and on the site, and alert backends at the
  site (`[alerts]` there): a site with none has nobody to tell.
- A site the hub can only copy whole (it cannot run its exporter there:
  `nomad sync` says "copying it whole") gets no note: it does not watch the
  hub, and of the alerts about it only the hub's are held in maintenance.

```toml
# at each site
[alerts.hub]
enabled = true          # watch the hub (default)
after_minutes = 60      # silent this long: say so
```

### Declared maintenance (1.37.0)

A site taken down on purpose and one that broke look the same from the hub.
A window says which:

```bash
nomad maintenance start cluster-a --until "2026-11-12 18:00" --reason "OS patches" --apply
nomad maintenance start hub --from 14:00 --until 16:00 --reason "new disk" --apply
nomad maintenance start all --until +4h --reason "switch firmware" --apply
nomad maintenance end cluster-a --apply
nomad maintenance                   # now, coming, the last 7 days
```

Run them on the hub. `start` and `end` show what they would do; `--apply`
does it. Times: `"2026-11-12 18:00"`, `18:00` (today; tomorrow when that has
passed), `+4h`, `+90m`, `+2d`. `--until` counts from the start: with `--from
14:00`, `--until +2h` is 16:00, and `--until 18:00` is the first 18:00 after
14:00. The first line of what `start` shows is the window as it will be.

While a site's window lasts, and for `maintenance_grace_minutes` (60) after
it for the site to come back:

- **the hub holds every alert it raises about that site**: not synced, not
  collecting, a collector or a GPU node stopped, and for sites whose alerts
  the hub sends, their nodes, clocks and power supplies;
- **the site's own nomad holds its alerts too**: the hub tells it of the
  window each time it reads it, and at once when the window is declared
  (`[alerts] maintenance_hold = false` at a site keeps its own coming);
- **the Console shows it as in maintenance**, with the reason, not as stale.

What still holds once the window and its grace are over is raised then, as
news, saying when the window ended. What only happened during it and is
over by then (an entry in a board's event log, a node that went down and
came back) is not sent afterwards. Test alerts (`nomad test-alerts`) are
always sent.

`start --apply` and `end --apply` tell each site at once and say how it
went: `told`, or `not reached now; told when the hub next reads it` (a site
already down holds nothing of its own to send anyway). A site holds its
alerts on its hub's word only while that hub keeps reading it: a day after
the hub's last read the hold lapses, so a hub that died after declaring a
long window does not keep a site quiet.

`nomad maintenance end SITE` ends one thing at a time: the window running;
else the grace after one just over (the site is back: its alerts are no
longer held); else the window coming, which is called off. A window ended by
hand has no grace. A window declared while the grace of the one before
still holds is told to the site as one with it.

`hub` is the hub itself: during its window the sites don't report it as
silent; one still silent an hour after the window is reported, with when
the window ended. Nothing of the hub's own is held for it. Declare it before
taking the hub down: the sites learn of it from the hub.

A window always ends: it needs `--until`, and can't be longer than
`maintenance_max_days` (14, the most a site takes). One window per site at a
time: declaring another replaces it. The windows, past ones too, are kept in
`maintenance.json` beside the hub's combined database (`[hub] output_db`):
run the commands as the account the hub's nomad runs as, which is the one
that can write there. A file that can't be read is no windows: alerts go on,
and a sync never stops for it. `nomad maintenance` at a site shows what its
hub told it.

```toml
# on the hub
[hub]
maintenance_grace_minutes = 60     # alerts held this long after a window, for the site to come back
maintenance_max_days = 14          # the longest window (14 at most)
# maintenance_file = "/var/lib/nomad/maintenance.json"   # default: beside the combined database
```

### A node that fails every job at start (1.25.4)

A node that is up, idle and taking jobs, but whose jobs all fail within
seconds (a mount missing after a reboot, slurmd set up wrong after a
reinstall, a program gone) looks healthy to Slurm and to the node-state
alerts, and each job it fails frees it at once for the next: it can take a
whole array in minutes. At each collection, the node_state collector reads
this site's `jobs` table: a node where, among the single-node jobs that
started there in the last `window_minutes`, at least `min_jobs` failed within
`fail_fast_seconds` and they are at least `share` of them, while the very
same work ran on other nodes in that time -- the same array (its job id
before the `_`), else the same person's jobs of the same name: at least one
of them ran past `fail_fast_seconds` elsewhere and at most a quarter of them
failed fast there, for at least 2 of the node's failures. A broken script
fails everywhere, and a person's other work proves nothing about the node:
neither raises it. It can't be sure: an array whose last tasks lack their
input fails them as fast, all on the node that frees first. So the warning
names one of the jobs to look at before draining, and says how many
different works failed there (several: likely the node). Such a node raises
a warning (source `node_jobs`, the node as host): how many
failed, of how many, between when, and for how many people (named to no
one), with the `scontrol` line that drains it. A node already drained or
down is left out. Once, again when worse, then every reminder, as other
alerts.

```toml
[alerts.node_jobs]
enabled = true
window_minutes = 60
fail_fast_seconds = 60
min_jobs = 5
share = 0.8
```

### CPU temperatures (1.25.6)

The `temps` collector ([Collectors](collectors.md), "Temperatures") reads
each listed host's sensors; its CPU package's own (k10temp Tctl, zenpower
Tctl or Tdie, coretemp Package id N) raises a warning or a critical (source
`temps`, the host as host) when every reading for `sustain_minutes` is at or
above the limit: a moment's spike is not an alert. It says how hot, for how
long, the CPU's other sensors, and what to check. Limits are the CPU's: a
Ryzen 7000 runs at 95 °C by design under full load, a Ryzen X3D's limit is
89 °C, so a host's own table can set its own.

```toml
[alerts.temps]
warning = 90
critical = 95
sustain_minutes = 3           # every reading for this long; 0: at once
[alerts.temps.peterslabworkstation]
warning = 85
critical = 89                 # its Ryzen 9 7900X3D's limit: it throttles there
```

Sent from where the collector runs, through its [alerts] backends (the hub's
mail, for workstations the hub reads). `nomad temps --check --warning 70
--send` sends a test now, through the same path.

### Power (1.25.8)

The `power` collector ([Collectors](collectors.md), "Power supplies, voltages
and fans") raises, for each host, one alert for each of `power supply`
(no input power, failed, failing, misconfigured, redundancy lost, a supply's
fan stopped, one supply carrying the node), `voltage` (past its board's
thresholds: critical past the critical one) and `fans` (stopped or slow),
while the problem lasts: once, again when worse, then every reminder. A new
problem entry in a board's log (an AC loss that came back before the next
read, say) is a `power events` alert, each batch once; what the log held at
its first read is history. Source `power`, the host as host. A threshold of 0
is never an alert.

```toml
[alerts.power]
# enabled = true
# fans = true                      # false: no fan alerts
# ignore = ["node03:PMBPower"]     # host:sensor (shell patterns): never alerted
```

Two sensors of one name on a host (older Dells name both supplies' "Status")
are told apart by their sensor number: "Status #60h", "Status #61h"; ignore
them with a pattern ("r720:Status*").

A site whose alerts the hub sends (`alerts = "hub"` in its [[hub.sites]]:
vadur) gets these from the hub after each sync, through the hub's [alerts],
as its node alerts ("Node alerts sent by the hub"); the hub's own
`[alerts.power]` applies. A host not read, or read more than 2 hours ago,
is held as it was. `nomad power --check --send` sends a test.

### CPU clock (1.27.0)

The `clock` collector ([Collectors](collectors.md), "CPU clocks") raises one
alert a host whose CPU runs far below what it should give when busy (a power
brake: arachne's node01-03 ran at 400 MHz instead of 3,700 for a month after
a supply lost its input), after `sustain` low readings in a row, while it
lasts: once, again when worse, then every reminder. Source `clock`, subject
`cpu clock`, the host as host; **critical** when the host's power readings
(the `power` collector, same site) show a supply problem too, else warning.
The message says what it runs at, what it should, how much slower jobs run,
its supplies' state (or that its board can't be read in-band), and what to
do: drain it, restore the supply, reset the board controller if the brake
stays on.

```toml
[alerts.clock]
# enabled = true
# min_fraction = 0.5               # low below this share of what it should give
# sha_min_fraction = 0.25          # judged by the sha256 rate alone (no cycle counter)
# sustain = 2                      # readings in a row (every_minutes apart)
# ignore = ["gpu0*"]               # hosts never alerted (shell patterns)
# [alerts.clock.node07]
# min_fraction = 0.3               # one host's own
```

A site whose alerts the hub sends (`alerts = "hub"`) gets these from the hub
after each sync; a host read more than 2 hours ago is held as it was. `nomad
clock --check --send` sends a test.

## Backends

### Email
```toml
[mail]                       # shared by everything that sends mail
host = "smtp.example.edu"
port = 587
starttls = "required"        # required | if-offered | off
from = "hpc@example.edu"     # a sender your mail server accepts

[alerts.email]
enabled = true
recipients = ["admin@example.edu", "hpc-team@example.edu"]
```

Alert email goes through `[mail]`; see `nomad.toml.example` for its other
options (a certificate name, a missing intermediate, a login). Any of `[mail]`'s
keys set in `[alerts.email]` itself override it, for alerts that must use a
different server. `nomad test-alerts --email` checks the connection.

Each message names its cluster -- the name `[clusters]` gives, else the host
name -- in the subject with the problem itself, so an inbox shared by several
sites reads at a glance:

```
[WARNING] NØMAÐ spydur: Disk /scratch at 85.0% (1.9 TB free; threshold: 80%)
```

### Slack
```toml
[alerts.slack]
enabled = true
webhook_url = "https://hooks.slack.com/services/T00/B00/xxx"
channel = "#hpc-alerts"
```

### Webhook
```toml
[alerts.webhook]
enabled = true
url = "https://your-service.example.edu/alerts"
headers = { Authorization = "Bearer xxx" }
```

## CLI
```bash
# View recent alerts
nomad alerts

# Unresolved only
nomad alerts --unresolved

# Test alert backends
nomad alerts test
```

## Repeats

Each alert is about a condition: its source, its host, what on the host it
is about -- the disk path, the NFS mount, the GPU, the node -- and the metric. `/home` and
`/scratch` on one head node are two conditions. A condition is raised (stored
and sent):

- when it appears,
- when it gets worse (warning → critical),
- and once every `reminder_hours` while it lasts.

```toml
[alerts]
reminder_hours = 24
episode_gap_minutes = 60   # not seen this long: it has ended
```

What has been raised is kept in the `alert_state` table, so this holds across
`nomad collect --once` runs from cron. A condition not seen for
`episode_gap_minutes` has ended; if it comes back it is raised again (keep the
gap longer than the collection interval). A short dip back (critical →
warning → critical) raises nothing new; after `episode_gap_minutes` below its
worst, getting worse again is raised. Two metrics of one thing (an NFS
mount's latency and its retransmissions) are two conditions. If every backend
fails to send, the alert is sent again after 15 minutes, then 30, an hour and
so on up to `reminder_hours` (stored once). If the database cannot be written
(nomad's own database on the disk that filled), what was sent is remembered
in a small file in the local temporary directory instead, and the readings
are checked even though they could not be stored. `nomad test-alerts`
always sends its test.

Before 1.7.16 an alert was "the same" when its source, host and severity
matched, whatever the path, with a 15-minute cooldown: a disk at 87% for
weeks was stored and sent every 15 minutes, and a second disk's warning
waited behind it. `cooldown_minutes` now applies only where alerts have no
database.

`nomad alerts` lists the conditions active now (seen within the episode gap)
before the alerts raised.

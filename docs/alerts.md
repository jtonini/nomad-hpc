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

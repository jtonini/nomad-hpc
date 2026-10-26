# Setup Wizard

The `nomad init` wizard configures NØMAÐ for your environment. It detects available tools, asks about your infrastructure, and generates a configuration file at `~/.config/nomad/nomad.toml`.

```bash
nomad init
```

This guide walks through each step of the wizard with explanations and recommendations.

---

## Step 1: Connection Mode

```
Where is NØMAÐ running?
  1) On the cluster headnode
  2) On a separate machine (laptop, desktop, etc.)
```

**Option 1 — On the headnode** is the most common choice. NØMAÐ runs directly on the machine that has access to SLURM commands (`sinfo`, `squeue`, `sacct`) and local filesystems. This is the simplest setup with no SSH configuration needed for the local cluster.

**Option 2 — Remote machine** is for cases where you want to run NØMAÐ from a laptop or desktop that connects to the cluster via SSH. NØMAÐ will SSH into the cluster to run commands. You will need to provide SSH credentials (hostname, user, key path).

!!! tip "Recommendation"
    Choose **Option 1** whenever possible. Running on the headnode gives direct access to SLURM, filesystems, and system tools without SSH overhead.

---

## Step 2: Deployment Strategy

NØMAÐ supports two deployment strategies. The wizard explains both and asks how many systems this instance will monitor.

### Strategy A — One NØMAÐ per machine + sync

Install NØMAÐ on each machine independently. Each instance monitors its own environment and stores data in its own database. Use `nomad sync` on a central machine to merge all databases into a unified dashboard.

This is the **recommended approach** when each machine has a different role — for example, an HPC cluster with SLURM, an interactive server with Jupyter/RStudio, and a workstation management server.

In this case, answer **1** on each machine:

- **HPC headnode**: 1 cluster (HPC type) — monitors partitions, jobs, nodes
- **Interactive server**: 1 cluster (HPC type, SLURM auto-skipped) — monitors sessions, disk, CPU
- **Workstation manager**: 1 cluster (Workstation type) — monitors lab machines via SSH

Then on a central machine, configure `nomad sync` to pull all databases together:

```bash
nomad sync                        # Merge all site databases
nomad dashboard --db combined.db  # Unified view across all sites
```

### Strategy B — One NØMAÐ monitors everything

A single NØMAÐ instance monitors multiple systems from one machine, using SSH for remote access. This works when you have a machine that can reach all targets.

For example, to monitor both an HPC cluster and a set of workstations from the same headnode, answer **2**:

- Cluster 1: HPC cluster (SLURM, local)
- Cluster 2: Workstation group (department machines, via SSH)

### Which strategy to choose?

| Scenario | Strategy | Why |
|----------|----------|-----|
| Machines have different roles (HPC, interactive, workstations) | A — one per machine + sync | Each instance uses the right collectors for its environment |
| Everything is reachable from one machine | B — single instance | Simpler setup, no sync needed |
| Machines are on different networks | A — one per machine + sync | SSH may not work across network boundaries |
| You want resilience (one site going down doesn't stop others) | A — one per machine + sync | Each site collects independently |

!!! tip "You can always start with Strategy B and split later"
    If you start with a single instance monitoring everything and later want to split, just run `nomad init` on each machine separately and set up `nomad sync`. The data format is the same either way.

---

## Step 3: Cluster Configuration

For each cluster, the wizard asks for a name and type.

### Cluster Name

```
Cluster name [cluster-1]: spydur
```

Choose a short, descriptive name. This appears in the dashboard navigation tabs and in the combined database when using `nomad sync`. Use lowercase with no spaces.

### Cluster Type

```
What type of system is this?
  1) HPC cluster (managed by SLURM)
  2) Workstation group (department machines, not SLURM)
```

**Option 1 — HPC cluster** enables SLURM collectors (`squeue`, `sacct`, `sinfo`), job metrics, node state, group membership, and job accounting. The wizard will auto-detect SLURM partitions.

**Option 2 — Workstation group** disables SLURM collectors and enables the WorkstationCollector, which monitors remote machines via SSH. You will be asked for department names and hostnames. Workstation data appears in the Workstations page of the dashboard.

!!! info "What gets disabled for workstation groups"
    When you select workstation group, the following collectors are automatically disabled since they require SLURM: `slurm`, `job_metrics`, `node_state`. The `disk`, `vmstat`, `gpu`, `nfs`, `groups` (membership only; job accounting needs Slurm) and `workstation` collectors remain active.

---

## Step 3a: HPC Cluster — Partition Detection

If you selected HPC cluster, the wizard auto-detects SLURM partitions:

```
Detecting SLURM partitions...
Found partitions: basic, medium, large, gpunodes, chemlab
```

If auto-detection fails (SLURM not installed locally, or running in remote mode), you can type partition names manually:

```
Type your partition names separated by commas:
Partitions: basic, medium, large, gpunodes
```

!!! tip
    You can find partition names by running `sinfo -h -o "%P"` on the cluster headnode.

---

## Step 3b: Workstation Group — Departments and Machines

If you selected workstation group, the wizard asks for department organization:

```
Type your department/lab names, separated by commas:
Departments: smith-lab
```

Then for each department, provide the hostnames of machines to monitor:

```
Type the hostnames for 'smith-lab', separated by commas:
Nodes for smith-lab: ws01, ws02, ws03, ws04, ws05, ws06
```

The WorkstationCollector will SSH to each machine and collect CPU, memory, disk, load, and user session data.

!!! warning "SSH Access Required"
    The current user must be able to SSH to each workstation with key-based authentication (no password). Test with: `ssh -o BatchMode=yes hostname "hostname"`. The wizard writes the current username as `ssh_user` in the configuration.

---

## Step 4: Filesystem Monitoring

```
Which filesystems should NØMAÐ monitor for disk usage?
Filesystems (comma-separated) [/, /home]: /, /home, /scratch
```

These are the local filesystems on the machine running NØMAÐ. The DiskCollector monitors these paths, computes fill rates using derivative analysis, and fires alerts when usage exceeds thresholds.

Common paths:

- `/` — Root filesystem
- `/home` — User home directories
- `/scratch` — Scratch storage (often the most critical to monitor)
- `/localscratch` — Node-local scratch

!!! note "Workstation filesystems"
    For workstation groups, this setting applies to the machine running NØMAÐ (not the workstations). The WorkstationCollector independently collects disk usage from each remote workstation via SSH.

---

## Step 5: Optional Features

The wizard probes for available tools and asks about optional collectors:

### GPU Monitoring

```
✓ nvidia-smi found (NVIDIA GPU detected)
Enable GPU monitoring? [Y/n]: y
```

Enabled when `nvidia-smi` is available. Collects GPU utilization, memory, temperature, and power draw.

### NFS Monitoring

```
✓ NFS monitoring available (nfsiostat found)
Enable NFS monitoring? [Y/n]: y
```

Enabled when `nfsiostat` is found. Monitors NFS mount performance including read/write throughput and latency.

### Interactive Sessions

```
✓ JupyterHub detected
Enable interactive session monitoring? [Y/n]: y
```

Detects running JupyterHub or RStudio Server processes. When enabled, the InteractiveCollector tracks active sessions, memory usage, idle time, and session type.

---

## Step 6: Alerts

```
NØMAÐ can send you email alerts when something needs
attention (disk filling up, nodes going down, etc.).
Your email address (press Enter to skip): admin@example.com
```

Providing an email address enables the alert pipeline. Alerts are always stored in the database and visible in the dashboard regardless of email configuration.

For email delivery, NØMAÐ uses the system `mail` command if available. No SMTP server configuration is required. You can set up a daily report via cron:

```bash
0 8 * * * /path/to/nomad insights brief | mail -s "NØMAÐ Daily Report" admin@example.com
```

!!! tip "Advanced alert configuration"
    After the wizard, you can edit `~/.config/nomad/nomad.toml` to configure Slack webhooks, custom thresholds, severity filters, and cooldown periods. See the [Alerts documentation](alerts.md) for details.

---

## Step 7: Dashboard Port

```
The NØMAÐ dashboard is a web page you open in your
browser to view cluster status, node health, and alerts.
Dashboard port [8050]:
```

The default port 8050 works for most installations. Choose a different port if 8050 is already in use.

For remote access, set up an SSH tunnel:

```bash
ssh -L 8050:localhost:8050 user@headnode
```

Then open `http://localhost:8050` in your browser.

---

## Step 8: Condo reports

```
Register condos for the monthly condo reports? [y/N]:
```

A condo is a set of nodes a lab bought, lent to everyone else through a
guest partition while the lab isn't using them. Answer yes and the wizard
asks, for each Slurm cluster, which partition is the guest partition (the
one named like "condo" by default), lists the condos in it (the partitions
whose nodes all sit inside it), and asks each one's owner NetID; then the
owners' mail domain and who should review the reports first. It writes
report.toml beside nomad.toml, the cluster named as in nomad.toml (condos on
several clusters monitored from one machine go together as `this-cluster`:
that machine's database holds them all); written as root, the nomad account
can read it. See [Condo reports](condo-reports.md).

---

## Summary and Next Steps

After completing the wizard, you will see a summary:

```
✓ NØMAÐ configured!

Config:  ~/.config/nomad/nomad.toml
Data:    ~/.local/share/nomad

Clusters:
  • spydur: 5 partitions, 30 nodes (local)

Enabled: GPU monitoring, NFS monitoring

What to do next:
  1. Review your config:     nano ~/.config/nomad/nomad.toml
  2. Verify environment:     nomad syscheck
  3. Start collecting data:  nomad collect
  4. Open the dashboard:     nomad dashboard
```

### Verify Your Environment

```bash
nomad syscheck
```

This checks Python version, SLURM availability, system tools, database state, and filesystem access. Fix any warnings before starting collection.

### Test Collection

```bash
nomad collect --once
```

Run a single collection cycle to verify everything works. Check for errors in the output — successful collectors show `✓` with a record count.

### Start Continuous Collection

```bash
nohup nomad collect > ~/.local/share/nomad/logs/collect.log 2>&1 &
```

For production, consider running the collector as a systemd user service for automatic restart on failure.

### Launch the Dashboard

```bash
nomad dashboard
```

Open `http://localhost:8050` in your browser.

---

## Multi-Site Setup

To monitor multiple sites from a single dashboard:

1. Install and configure NØMAÐ on each site using `nomad init`
2. On a central machine (the hub), list the sites under `[hub]` in its
   nomad.toml (`~/.config/nomad/nomad.toml`, or `/etc/nomad/nomad.toml` for a
   service account; `nomad -c FILE sync` names another):

```toml
[hub]
output_db = "~/.local/share/nomad/combined.db"   # the default

[[hub.sites]]
name = "cluster-a"
host = "headnode-a"
ssh_user = "monitor"
db_path = "~/.local/share/nomad/cluster-a.db"

[[hub.sites]]
name = "cluster-b"
host = "headnode-b"
ssh_user = "monitor"
db_path = "~/.local/share/nomad/cluster-b.db"

[[hub.sites]]
name = "hub"                  # collectors on the hub itself (lab workstations, say)
host = "localhost"            # read directly: no ssh to itself
db_path = "/var/lib/nomad/nomad.db"
```

   A relative `output_db` is in the data directory
   (`~/.local/share/nomad`). A `localhost` site's partitions come from the
   nomad.toml in use, and an action run on it (`nomad actions run --site`)
   runs on the hub itself, without the agent key. `~/.config/nomad/sync.toml` with top-level
   `[[sites]]` still works, with a warning.

   A site reached through a jump host (`ProxyJump` in the hub account's
   `~/.ssh/config`) works from a service account too: ssh runs a jump
   through `$SHELL`, which for an account with no login shell is
   `/sbin/nologin`, so since 1.17.3 nomad sets `SHELL=/bin/sh` for itself
   and its ssh and scp when `$SHELL` is missing or can't run one. A shell
   that works is left as it is.

   A site that can't send its own alerts (no way to the mail server, or set
   up with `nomad init --quick`, which writes no `[alerts]`) can have the hub
   send its node alerts: `alerts = "hub"` in its `[[hub.sites]]` entry
   ([alerts](alerts.md)).

3. Run sync and launch the combined dashboard:

```bash
nomad sync
nomad dashboard --db ~/.local/share/nomad/combined.db
```

4. Set up a cron for automatic syncing:

```
*/10 * * * * /path/to/nomad sync 2>/dev/null
```

5. See how the syncs go (it only reads; the Console's `sync.status` action
   runs it):

```bash
nomad sync status
```

   For each site in `[hub] sites`: when its data was last brought up to date,
   how (only what changed, copied whole, or kept what the hub had because the
   sync failed), how many rows, the last live pull, whether it is late (more
   than `alert_after_minutes`, 120), what the next sync will ask for, how many
   attempts failed in a row and the last error. A site never synced, and one
   no longer in `[hub] sites` (its rows go at the next sync), are said too.
   `nomad sync -c FILE -o DB status` reads the hub a sync with those options
   would.

How a site's database reaches the hub, and what happens when it doesn't:

* **Only what changed (1.17).** Each site keeps a log of the rows its
  collectors add, change and delete (triggers on its tables, in its own
  database: the tables `_nomad_changes`, `_nomad_capture` and
  `_nomad_capture_meta`, and triggers named `_nomad_*`). Each sync asks for
  what is in the log past the point the hub has, and the hub applies it to
  the combined database in place, one transaction per site; the log is then
  dropped up to that point. A run writes megabytes. Until 1.16 every sync
  copied every site's whole database and rebuilt the combined database: on a
  four-site hub about 24 GB of writes a run, which wore out the hub's SSD,
  and each site wrote a full snapshot of its database for every pull.
* The site needs only `python3` (3.6 or later): the hub sends the exporter
  (`nomad/db/delta_site.py`) with each request and runs it there with
  `python3 -`, so nothing else is installed for it. A site where it can't
  run (no python3, or one without its sqlite3 module) is copied whole at
  every sync, as before, and so is one whose changes could not be had three
  syncs in a row; each sync tries the log again first.
* The exporter never keeps a collector waiting: it reads a batch per
  statement and holds nothing open while the hub reads its answer, and it
  drops the log a batch at a time; it reads the log a part at a time, so
  it needs a few tens of MB however far behind the hub is. The hub spools
  each answer to a file and
  applies it a record at a time, so a table sent whole (after a migration)
  takes no more memory than a few rows.
* A site is copied whole once (a snapshot, as below, removed when applied):
  the first time; when its log was put back (its database replaced); when
  its database was restored from a backup (each answer carries a token the
  hub sends back, which a restored database doesn't know); when the hub is
  more than 3 million changes behind (silent for a day or more); or when the
  log no longer reaches back to where the hub is. A hub whose combined
  database moved, or that moved to another host, is a new hub to the sites:
  each is copied whole once. A table recreated, renamed or
  given new columns or unique keys by a migration is sent whole, once. A
  migration that drops a column must first call
  `nomad.db.delta_site.untrack(conn, table)` (the triggers name the
  columns); the next sync puts the triggers back and sends the table whole.
* `nomad sync --full` copies every site whole again, into the combined
  database in place. The first sync after upgrading to 1.17 builds the
  combined database whole once, in the form later syncs update, and copies
  it into the published file with SQLite's backup (one write the Console
  sees whole or not at all) instead of renaming a new file over it: a file
  renamed over one in use, under its write-ahead log, comes out corrupted
  once the hub writes in place. The site copies it was built from are not
  kept.
* Each hub has its own place in a site's log (by host and combined
  database), so a trial `nomad sync -o /tmp/x.db` does not make the real hub
  copy everything again; the log is kept back to the oldest place among the
  hubs heard from in the last two days. Once more than 6 million changes
  wait, the hubs more than 3 million behind stop holding the log back (each
  copies the site if it comes back); the others are not affected.
* A site whose sync fails keeps what the hub had of it (`sync_sites.source`
  says `kept`, `synced_at` and `last_success` stay at the last good sync) and
  is asked again next time, from where it was. A site taken out of
  `[hub] sites` has its rows removed at the next sync, and is told: its log
  and triggers are taken out there unless another hub reads it. If it can't
  be told, its own `nomad collect` (1.17) takes out a log no hub has read
  for a week.
* After each sync the hub folds its write-ahead log into the combined
  database and cuts it back (after a whole copy it would otherwise stay the
  size of that site's data).
* Going back to a nomad older than 1.17: run `nomad sync --remove-logs` on
  the hub first (with 1.17). It takes the log and triggers out of every
  site's database and empties the combined database's write-ahead log, which
  the older nomad's rebuild needs (it renames its new file over the old
  one); if something kept reading it, stop the Console before that first
  older sync.

The copies the full rebuild, and a site's first sync, are made of:

* Each site writes a consistent snapshot of its database (SQLite's
  `.backup`, safe while its collectors keep writing; with the `sqlite3`
  command, or with its python3 3.7+ when it has no `sqlite3` command)
  in `/tmp` when it has ample room, otherwise **beside the database**, and
  the hub copies it. A site with neither is copied as its live file, which can
  be torn without looking it: such a copy is not trusted to say where the
  site's log stands: instead, when the log could be set up there, the
  site's exporter sends every table whole, as of a place in its log, and
  from then on only changes come. If that fails (it can take long on a big
  database), the live file is copied after all, and the exporter is not
  asked again for a day. A login
  node's `/tmp` is often a few GB shared by everyone, smaller than the
  database. When there is room in neither, the reason says so ("no room for
  a snapshot: the database is 6.0 GB; …").
* The snapshot is always removed afterwards; any left by a run that died
  are removed after two hours. Time limits follow the database's size.
* The copy replaces the hub's cached copy of that site only once it is
  complete. In a full rebuild, when a site's pull fails, its cached copy
  stands in (marked as such in `sync_sites`, its `last_success` left at the
  last live pull), so one site's trouble doesn't hold back the others. Once
  the hub syncs only changes it keeps no copies (`sync_cache` is emptied).
* A site whose last live pull is older than `alert_after_minutes` (120 by
  default) raises a `sync` alert through the hub's `[alerts]` settings:
  a warning, critical after a day, with the reason, then reminders while it
  lasts:

```toml
[hub]
alert_after_minutes = 120
```

* A site that syncs but has stopped collecting, a collector that stopped
  or fails every run, a GPU node gone quiet and a GPU node whose DCGM stopped
  answering raise `collection` alerts after the sync (1.14.5; see
  [alerts](alerts.md#collection-stopped-the-hub-1145)); `nomad
  collection-check` lists them.

---

## Configuration Reference

The wizard generates `~/.config/nomad/nomad.toml`. Key sections:

| Section | Purpose |
|---------|---------|
| `[general]` | Data directory, cluster name |
| `[database]` | Database filename |
| `[collectors.disk]` | Filesystem paths to monitor |
| `[collectors.slurm]` | SLURM partitions (HPC only) |
| `[collectors.workstation]` | SSH user, workstation hostnames |
| `[collectors.gpu]` | GPU monitoring toggle |
| `[collectors.nfs]` | NFS mount monitoring |
| `[collectors.interactive]` | Jupyter/RStudio session detection |
| `[alerts]` | Email, Slack, webhook, thresholds |
| `[dashboard]` | Port, host binding |
| `[clusters.*]` | Cluster metadata and partition descriptions |

See the [Configuration documentation](config.md) for the full reference.

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
Found partitions: basic, medium, large, gpunodes, dias
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
Departments: parish-lab
```

Then for each department, provide the hostnames of machines to monitor:

```
Type the hostnames for 'parish-lab', separated by commas:
Nodes for parish-lab: aamy, adam, alexis, boyi, camryn, cooper
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
2. On a central machine, create `~/.config/nomad/sync.toml`:

```toml
[[sites]]
name = "cluster-a"
host = "headnode-a"
user = "monitor"
db_path = "~/.local/share/nomad/cluster-a.db"

[[sites]]
name = "cluster-b"
host = "headnode-b"
user = "monitor"
db_path = "~/.local/share/nomad/cluster-b.db"
```

3. Run sync and launch the combined dashboard:

```bash
nomad sync
nomad dashboard --db ~/.local/share/nomad/combined.db
```

4. Set up a cron for automatic syncing:

```
*/10 * * * * /path/to/nomad sync 2>/dev/null
```

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

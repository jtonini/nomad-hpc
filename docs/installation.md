# Installation

## Requirements

| Component | Version | For |
|---|---|---|
| Python | 3.10 or later | nomad itself |
| SQLite | 3.35 or later | every database nomad keeps |
| `python3` on each site | 3.6 or later, with its `sqlite3` module | the hub's sync (the hub sends the small exporter it runs there) |

Each collector uses the standard tool for what it reads, and one whose tool is
missing says so and collects nothing (see [Collectors](collectors.md)):

| Tool | Package | Read by |
|---|---|---|
| `sacct`, `squeue`, `scontrol`, `sinfo`, `sreport` | Slurm | `slurm`, `job_metrics`, `node_state`, `slurm_usage`, `job_live` |
| `iostat`, `mpstat` | sysstat | `iostat`, `mpstat` |
| `vmstat`, `df` | procps, coreutils | `vmstat`, `disk` |
| `nvidia-smi`, DCGM (`dcgmi`) | NVIDIA driver, datacenter-gpu-manager | `gpu` |
| `nfsiostat` | nfs-utils (in `/usr/sbin`) | `nfs` |
| `ssh` | OpenSSH | `workstation`, `storage`, and the hub's sync |

`nomad syscheck` says what this machine has, and `nomad collectors` which
collectors run here and how their runs went.

### Optional extras

```bash
pip install "nomad-hpc[report]"     # PDF condo reports (fpdf2)
pip install "nomad-hpc[predict]"    # job failure prediction with TESSERA
pip install "nomad-hpc[ml]"         # the older ML ensemble (PyTorch, PyTorch Geometric)
pip install "nomad-hpc[dev]"        # tests and linters, for contributing
```

## What goes where

NØMAÐ has three roles. A machine can have one, two or all three.

| Role | Runs | Typical machine |
|---|---|---|
| **Site** | `nomad collect --once` every 5 minutes, into its own database | A cluster's head node, an interactive server, a machine that reaches the lab workstations or storage servers |
| **Hub** | `nomad sync`, which brings every site's new rows into one combined database; the scheduled reports and emails | One machine that can reach every site over ssh |
| **Console** | The web interface, reading the hub's combined database | The hub |

A center with one cluster can run all three on its head node, or keep the hub
and the Console on a small separate machine so that the cluster's own work is
never in the way.

## Installing nomad

Use a virtual environment of its own:

```bash
python3 -m venv ~/nomad-env
~/nomad-env/bin/pip install nomad-hpc
~/nomad-env/bin/nomad --version
```

With conda: `conda create -n nomad python=3.12`, then `pip install nomad-hpc`
in it. From source:

```bash
git clone https://github.com/jtonini/nomad-hpc
cd nomad-hpc
python3 -m venv venv && venv/bin/pip install -e ".[dev]"
venv/bin/pytest
```

## A site

```bash
nomad init               # the setup wizard: what this machine is, what it monitors
nomad syscheck           # what it has, and what is missing
nomad collect --once     # one round of the collectors, each saying what it read
nomad collectors         # which collectors run here, why, and how their runs went
```

The wizard ([Setup Wizard](setup-wizard.md)) writes
`~/.config/nomad/nomad.toml`; the data goes in `~/.local/share/nomad/`.
`nomad init --quick` takes the detected defaults without asking. Then run the
collectors from cron, every 5 minutes:

```
*/5 * * * * /home/ACCOUNT/nomad-env/bin/nomad collect --once
```

An ordinary account that can run Slurm's commands is enough: collecting needs
no root. A few readings do need root, and each comes from a small helper that
root's cron runs and that only reads, so nothing of nomad runs as root:

- the boards' power supplies, voltages and fans ([Collectors: power](collectors.md#power-supplies-voltages-and-fans-power-1258));
- who holds the space on a storage server ([Storage use](storage.md));
- failed `sudo` and new setuid files, for the [policy watch](policy_watch.md).

Where nomad reads its settings: the file given with `-c`, else
`~/.config/nomad/nomad.toml`, else `/etc/nomad/nomad.toml`
([Configuration](config.md)). `nomad config check` says whether it reads, and
where not.

## The hub

List the sites in the hub's nomad.toml:

```toml
[hub]
output_db = "~/.local/share/nomad/combined.db"   # the default
sync_interval_minutes = 10

[[hub.sites]]
name = "cluster-a"
host = "headnode-a"
ssh_user = "monitor"
db_path = "~/.local/share/nomad/cluster-a.db"

[[hub.sites]]
name = "hub"                  # collectors on the hub itself (lab workstations, storage)
host = "localhost"            # read directly: no ssh to itself
db_path = "/var/lib/nomad/nomad.db"
```

The hub reaches each site with an ssh key of its own. On each site, put it in
`authorized_keys` with `restrict` and a `from=` the hub's address. Then:

```bash
nomad sync               # bring the combined database up to date
nomad sync status        # how each site's last syncs went
```

A sync copies only what changed since the last one (since 1.17), so a run
writes megabytes, not the whole database. A site that stops answering, or
answers but has stopped collecting, raises an alert from the hub. The
[Setup Wizard](setup-wizard.md#multi-site-setup) page has the details:
change logs, whole copies, what happens when a site is down, and
`nomad sync timer`.

### As its own account

On a hub that serves other people, run nomad as a system account of its own
(`nomad`), as Slurm runs as `slurm`. One layout that works:

| Path | What | Owner and mode |
|---|---|---|
| `/etc/nomad/nomad.toml` | settings, with `[hub]` | `nomad:nomad 0640`, in `/etc/nomad` (`root:nomad 2770`), so the Console and `nomad course` / `nomad lab` can edit it |
| `/etc/nomad/report.toml` | the condo and lab reports' settings | as above |
| `~nomad/.local/share/nomad/` | combined.db, the hub's own nomad.db, reports, answers | `nomad` |
| `/var/lib/nomad` | a link to the data directory | |
| `/opt/nomad/venv` | Python, nomad and the Console's packages | root |
| `/usr/local/bin/nomad` | a small script that runs `sudo -u nomad /opt/nomad/venv/bin/nomad "$@"` | root |

Keep the `nomad` group to administrators. The hub's own scheduled work then
runs as `nomad` from systemd:

| Unit | Runs | Set up with |
|---|---|---|
| `nomad-sync.timer` | `nomad sync` | a unit of your own; `sudo nomad sync timer --apply` keeps it at `[hub] sync_interval_minutes` |
| a collect timer or cron line | `nomad collect --once` for the hub's own collectors | yourself |
| `nomad-console.service` | the Console | yourself (below) |
| `nomad-notices.timer` | the [weekly notices](notices.md) | `nomad notices timer` |
| `nomad-lab-check.timer` | the [lab membership check](lab-check.md)'s emails | `nomad lab check timer` |
| `nomad-lab-reports.timer` | the [term reports](lab-reports.md) | `nomad usage-report labs timer` |
| `nomad-condo-reports.timer` | the [condo reports](condo-reports.md), on the 1st | `nomad usage-report condos timer` |
| `nomad-cold-data.timer` | the weekly [cold data](storage.md#data-unchanged-for-a-year) email | `nomad storage cold timer` |
| `nomad-licenses.timer` | the [license reminders](licenses.md) | `nomad licenses timer` |
| `nomad-metrics.service` | the [Prometheus exporter](metrics.md) | `nomad metrics service` |
| `nomad-ml-worker.timer` (on the worker) | [training the model](ml-training.md) | `nomad ml timer` |

Each `timer` or `service` command prints the units it would write, and
writes them only with `--apply` (as root). None runs as root itself.

## The Console

The Console is the web interface: a FastAPI backend that serves its built
React frontend, reading the hub's combined database and the same nomad.toml.
It imports nomad's own code for its answers, so it runs in the same
environment as nomad (`/opt/nomad/venv` above), as the same account.

!!! note "Where the Console's code comes from"
    The Console's code is released apart from the `nomad-hpc` package. This
    section will say where to get it, and how to install it, once that is
    published.

How it is usually run:

- **As a service** (`nomad-console.service`) on `127.0.0.1:8000`, restarted by
  systemd when it stops, its `NOMAD_CONSOLE_*` settings (database paths, host,
  port, sign-in) in an environment file readable only by root and `nomad`.
- **Reached** through a reverse proxy (Apache or nginx) on the same machine,
  with HTTPS, on a name of its own (`https://nomad.example.edu`), open only
  where it should be (the campus network and its VPN, say). Until that is in place, people reach it through
  an SSH tunnel: [Console access](console.md).
- **Signing in** with the Console's own accounts, or with people's campus
  accounts (Kerberos or LDAP), so that access follows the university's own
  accounts.
- **Who sees what** comes from nomad.toml: staff roles, labs and their leads,
  research assistants, courses and their instructors
  ([Configuration: Console access](config.md#console-access)).

## For users on a cluster

People run one central install on each cluster (`nomad size`, `nomad submit`,
`nomad edu me`, `nomad console`), never one of their own.
`scripts/install_nomad_for_users.sh` makes it in `/usr/local/sw/nomad`, each
release in its own folder with its own Python, and switches to a new release
in one step: [Sizing and submitting: installing it for users](submit.md#installing-it-for-users).

## Upgrading

```bash
pip install --upgrade nomad-hpc
```

on each site and on the hub (in the Console's environment too, then
`sudo systemctl restart nomad-console`), and
`install_nomad_for_users.sh NEW-VERSION` for each cluster's install for
users. Databases are migrated by nomad itself the first time it opens them.
The hub sends its own exporter to the sites with each sync, so a site's sync
doesn't wait for the site to be upgraded. A migration that adds columns to a
table makes the next sync send that table whole, once.

## Uninstalling

1. On the hub, `nomad sync --remove-logs`: it takes the change log and its
   triggers out of every site's database.
2. Remove the cron lines, and the timers and services
   (`systemctl disable --now nomad-...`).
3. `pip uninstall nomad-hpc` (or remove the virtual environment).
4. If you want the data gone too: `~/.config/nomad`, `~/.local/share/nomad`
   (or `/etc/nomad` and the nomad account's data directory).

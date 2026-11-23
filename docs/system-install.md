# System Install

How to run NØMAÐ as a service on a machine other people rely on: its own
account, settings in `/etc/nomad`, and systemd. [Installation](installation.md)
has the roles (site, hub, Console) and the hub's layout; this page has the
permissions and what nomad touches on the system.

## User install or system install

| | User install | System install |
|---|---|---|
| Settings | `~/.config/nomad/nomad.toml` | `/etc/nomad/nomad.toml` |
| Data | `~/.local/share/nomad/` | `/var/lib/nomad/` (or the nomad account's home, linked from there) |
| Logs | `~/.local/share/nomad/logs/` | `/var/log/nomad/` and the journal |
| Runs as | you | a `nomad` system account |
| Scheduled by | your crontab | systemd timers, or the nomad account's crontab |

nomad reads the file given with `-c`, else `~/.config/nomad/nomad.toml`, else
`/etc/nomad/nomad.toml`. An account with no settings of its own (the `nomad`
account) therefore reads `/etc/nomad/nomad.toml`.

## The nomad account

```bash
sudo groupadd --system nomad
sudo useradd --system -g nomad -d /home/nomad -m -s /sbin/nologin nomad
sudo python3.12 -m venv /opt/nomad/venv
sudo /opt/nomad/venv/bin/pip install nomad-hpc
sudo /opt/nomad/venv/bin/nomad init --system   # writes /etc/nomad/nomad.toml; makes /var/lib/nomad, /var/log/nomad
```

Then give the files to the account:

| Path | Owner and mode | Why |
|---|---|---|
| `/etc/nomad/` | `root:nomad 2770` | nomad (and the Console) can edit nomad.toml (`nomad lab`, `nomad course`, `nomad console role` with `--apply`); new files keep the group |
| `/etc/nomad/nomad.toml` | `nomad:nomad 0640` | it may hold a mail password or tokens; group members (admins) can read it |
| data directory | `nomad:nomad 0750` | the databases hold usernames, job names and paths |
| `/var/log/nomad/` | `nomad:nomad 0750` | |

Keep the `nomad` group to administrators. For them to run nomad as the
account, a small `/usr/local/bin/nomad`:

```bash
#!/bin/bash
exec sudo -u nomad -H /opt/nomad/venv/bin/nomad "$@"
```

with a sudoers line letting the group do just that
(`%nomad ALL=(nomad) NOPASSWD: /opt/nomad/venv/bin/nomad`). As root, type the
full path: sudo's PATH has no `/usr/local/bin`.

nomad's own commands never need root, and those that would write to
`/etc/systemd/system` (each `timer` and `service` command) print what they
would write and write it only with `--apply`, run as root.

## Collecting from systemd

From cron, every 5 minutes, is the usual way:

```
*/5 * * * * /opt/nomad/venv/bin/nomad collect --once
```

in the nomad account's crontab (`sudo crontab -u nomad -e`). Or with a
timer:

```ini
# /etc/systemd/system/nomad-collect.service
[Unit]
Description=NOMAD collectors
After=network-online.target

[Service]
Type=oneshot
User=nomad
Group=nomad
ExecStart=/opt/nomad/venv/bin/nomad collect --once
```

```ini
# /etc/systemd/system/nomad-collect.timer
[Unit]
Description=NOMAD collectors, every 5 minutes

[Timer]
OnCalendar=*:0/5
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now nomad-collect.timer
systemctl list-timers 'nomad-*'
journalctl -u nomad-collect
```

The hub's sync, and its reports and emails, run the same way: see
[Installation: as its own account](installation.md#as-its-own-account).

## What nomad reads and runs

### Commands

| Command | Package | For |
|---|---|---|
| `squeue`, `sacct`, `sinfo`, `scontrol`, `sreport`, `sacctmgr` | Slurm | jobs, queues, nodes, partitions, accounts |
| `sstat` | Slurm | what running jobs use now (`job_live`, as Slurm's user through sudo) |
| `iostat`, `mpstat` | sysstat | disk I/O, per-core CPU |
| `vmstat`, `df` | procps, coreutils | memory, swap, filesystems |
| `nvidia-smi`, `dcgmi` | NVIDIA driver, DCGM | GPUs |
| `nfsiostat` | nfs-utils | NFS client I/O |
| `getent` | glibc | group membership, accounts |
| `journalctl` | systemd | out-of-memory kills (`oom`) |
| `ssh`, `scp` | OpenSSH | nodes, workstations, storage servers, the hub's sync |

Commands are looked for on the PATH and in `/usr/sbin`, `/sbin`,
`/usr/local/sbin` and `/usr/local/bin` (cron's PATH is only `/usr/bin:/bin`).

### Files

| Path | For |
|---|---|
| `/proc/*/stat`, `/proc/*/io`, `/proc/*/status` | processes on login nodes and interactive servers (`per_user`, `interactive`, `policy_watch`) |
| `/proc/meminfo`, `/proc/loadavg`, `/proc/pressure/*` | memory, load, pressure stalls |
| `/sys/class/hwmon` | temperatures (`temps`) |
| `/sys/fs/cgroup` | per-user CPU on login nodes |

### What needs root

Nothing of nomad runs as root. Three readings need it, and each comes from a
small helper that root's cron runs, that only reads, and that writes a file
the collector then reads:

| Reading | Helper | Docs |
|---|---|---|
| Power supplies, voltages, fans (IPMI) | `scripts/power_root_probe.py` | [Collectors](collectors.md#power-supplies-voltages-and-fans-power-1258) |
| Who holds a storage server's space | `nomad storage script` (`nomad-storage.sh`) | [Storage use](storage.md) |
| Failed `sudo`, new setuid files | `scripts/watch_root_probe.py` | [Policy watch](policy_watch.md) |

## Slurm

nomad reads Slurm with its ordinary commands, so the account needs only to see
every user's jobs (Slurm's `PrivateData` off for jobs and usage, or the
account an operator). Two things go further, and both are off until set up:

- **Draining and resuming nodes** from the Console: the account runs
  `scontrol update` as Slurm's user through one sudoers line
  ([Actions](actions.md#draining-and-resuming-a-node)).
- **A prolog that scores each job** against the trained model:
  `nomad/hooks/prolog.py`, copied into `/etc/slurm/prolog.d/` and named in
  `slurm.conf` (`Prolog=`); it reads `/var/lib/nomad/nomad.db` and logs
  high-risk jobs to `/var/log/nomad/`.

## Security

- **The databases** hold usernames, job names, folders and command lines.
  Keep them readable by the nomad account and administrators only: what
  anyone else sees of other people goes through the Console's scoping.
- **nomad.toml** may hold a mail password or tokens: `0640`, group `nomad`.
  Where a setting takes a file instead (`[metrics] token_file`, say), keep
  the secret there, readable by the account only.
- **ssh keys**: each key the hub uses on a site is held there to what it is
  for, with `restrict` and `from=` the hub's address, and where it is for one
  command, `command=` (the actions agent, the gate).
- **The Console** listens on `127.0.0.1` only: reach it through a reverse
  proxy with HTTPS, or an SSH tunnel ([Console access](console.md)).

## Troubleshooting

**Permission denied on a database**: check the data directory's owner
(`nomad`) and that you ran the command as the account
(`sudo -u nomad -H ...`, or the wrapper above).

**A collector collects nothing**: `nomad collectors` says what it needs that
this host lacks, and how its last runs went.

**Slurm's commands fail as the account**: try them as it
(`sudo -u nomad squeue`); the account may need to be a Slurm operator, or
Slurm's `PrivateData` may hide other users' jobs.

**nomad.toml doesn't read**: `nomad config check` names the line.

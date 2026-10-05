# Collectors

`nomad collect` (usually `nomad collect --once` from cron, every 5 minutes) runs the collectors this host's `nomad.toml` turns on. Each writes its own tables; `nomad sync` copies every site's tables to the hub.

## Which run

Each collector is turned on or off in its own table:

```toml
[collectors.nfs]
enabled = false
```

| Collector | Default | Where it belongs | Needs | Writes |
|---|---|---|---|---|
| `disk` | on | every host | `df` | `filesystems` |
| `slurm` | on | Slurm head node | `squeue`, `sacct` | `queue_state`, `jobs` |
| `job_metrics` | on | Slurm head node | `sacct` | `jobs`, `job_summary` |
| `iostat` | on | every host | `iostat` (sysstat) | `iostat_device`, `iostat_cpu` |
| `mpstat` | on | every host | `mpstat` (sysstat) | `mpstat_summary`, `mpstat_core` |
| `vmstat` | on | every host | `vmstat` | `vmstat` |
| `node_state` | on | Slurm head node | `scontrol` | `node_state` |
| `gpu` | on | hosts with NVIDIA GPUs, or a head node reaching GPU nodes over SSH | `nvidia-smi` (DCGM if present) | `gpu_stats`, `gpu_health` |
| `nfs` | on | hosts that mount NFS (client side) | `nfsiostat` (nfs-utils, in `/usr/sbin`) | `nfs_stats` |
| `groups` | on | every site | `getent`; `sacct` for job accounting | `group_membership`, `job_accounting` |
| `interactive` | off | hosts running RStudio or Jupyter | — | `interactive_sessions`, `interactive_summary` |
| `workstation` | off | a hub reaching workstations over SSH | `ssh` | `workstation_state`, `workstation_user_snapshot`, ... |
| `per_user` | off | login nodes, shared interactive hosts | Python `psutil` | `per_user_sample`, `per_user_alert`, `per_user_daily` |
| `storage` | off | a host reaching ZFS/NFS servers over SSH | `ssh` | `storage_state` |
| `network_perf` | off | any host; tests paths that start there | `ping`; `iperf3` for throughput | `network_perf` |
| `cloud.aws` | off | sites with AWS resources | Python `boto3` | `cloud_metrics` |

An `enabled = [...]` list under `[collectors]`, which older example configs carried, is not read.

`groups` reads membership with `getent group`: on the head node of each `[clusters]` entry (over SSH when it has a `host`), on the first reachable workstation of a `type = "workstations"` cluster with an `ssh_user`, and on this host otherwise. Job accounting needs `sacct` and is skipped without it.

`nomad collect -C disk,nfs` (or `-C disk -C nfs`) runs only those, if enabled. An unknown name is warned about and skipped; if no name is known, nothing runs and the command says so.

## Is it working?

```bash
nomad collectors
```

On a site, lists every collector: on or off and why, what it needs that this host lacks, and how its runs went over the last 7 days (from `collection_log`):

```
  nfs           on   on by default
                     needs: nfsiostat
                     2,016 runs, never any data, last 2026-10-01T07:20
                     nfsiostat not installed (nfs-utils)
```

On the hub, `nomad collectors --db ~/.local/share/nomad/combined.db` shows each site's collectors and marks those that run but collect nothing.

A collector that runs but has nothing to collect logs why — `nfsiostat not installed`, `no NFS mounts on this host`, `no NVIDIA GPU here`, `psutil not installed: nothing collected`, `no network_tests configured` — in `collection_log.error_message` of a successful run. Before 1.7.13 these logged "success, 0 records" (or, for `groups` and `per_user`, "1 records" whatever they held), which looked like a working collector. A command that isn't installed fails the run at once with the reason, without retrying.

Commands are looked for on the PATH and in `/usr/sbin`, `/sbin`, `/usr/local/sbin` and `/usr/local/bin`: cron's PATH is only `/usr/bin:/bin`, and nfs-utils installs `nfsiostat` in `/usr/sbin`.

## Jobs

The `slurm` collector reads running and pending jobs from `squeue` and every
user's jobs of the last `job_history_days` (7) from `sacct --allusers`
(without it, `sacct` run by anyone but root shows only that account's own
jobs). `partitions` limits both to the partitions listed; empty means all.

A job that ended between two runs leaves `squeue`; if that run's `sacct`
pull did not report it, it is looked up in `sacct` by number, and the answer
is taken only if its submit time matches (Slurm reuses job numbers after its
counter restarts). When `sacct` cannot account for the job yet, it is marked
`UNKNOWN`: it has ended, its outcome is not known, it has no exit code or
failure reason, and no success or failure count includes it. The next `sacct`
pull corrects it. A pending array range (`123_[5-10]`) that left the queue is
dropped, since each of its tasks has a row of its own. Nothing is concluded
about any job in a run where `squeue` did not answer.

Before 1.7.19 such jobs were marked `COMPLETED`, with an end time in UTC
where every other job time is local, so a failed job could count as a
success. Those rows (`COMPLETED`, an end time without a `T`) are looked up in
`sacct` too, 200 a run, and become `UNKNOWN` (end time moved to local) if
`sacct` no longer has them.

## Disks

`disk` reads each of `filesystems` with `df` every run. Each reading also
fits the readings of the last `forecast_window_hours` (default 6) with a
straight line and stores the fill rate and when the filesystem will be full
(`fill_rate_bytes_per_day`, `days_until_full`); a disk that will be full
soon raises a `disk_forecast` alert (see `docs/alerts.md`).

## NFS

On a host that mounts NFS, each run reads `nfsiostat 5 2` and keeps the second report: what each mount did in those 5 seconds (`sample_seconds` in `[collectors.nfs]`). The first report averages everything since the share was mounted. Per mount: operations, read and write rates, and round-trip and execution times and retransmissions weighted over reads and writes — empty (NULL) for a mount with no reads or writes in the sample, rather than 0 ms. A host that mounts nothing says "no NFS mounts on this host"; turn nfs off there.

## Network

Latency and packet loss from this host to the hosts you list, every run (ten pings, about two seconds per path):

```toml
[collectors.network_perf]
enabled = true

[[collectors.network_perf.network_tests]]
dest = "nas1"
path_type = "switch"
```

- Each path starts on the host that runs it. A `source` naming another host (by name or address) is skipped, and the run says so; list that path in the other host's config.
- A path with no reply at all is stored as `unreachable`.
- **Throughput** is off unless `throughput = true`. It then runs at most once per `throughput_interval` (3600 s) per path — checked against the table, so it holds under cron — for `iperf_duration` seconds with `iperf3`, which needs `iperf3 -s` running on the destination. Without iperf3, or when no `iperf3 -s` answers, it times an SSH copy of 50 MB, which encryption may limit below what the network carries; if that fails too, no throughput is stored (never a 0).

## Heavy use of login nodes (per_user)

On a login node or another host people share, `per_user` says who used it heavily, with what, and for how long. Turn it on there:

```toml
[collectors.per_user]
enabled = true
```

Each run (every 5 minutes from cron) reads every process and averages its CPU and I/O over the 5 minutes since the previous run, from counters that run left in `per_user_state`. A process seen for the first time gets its average since it started; if that is above a rule's threshold, the next reading confirms it is still busy, and the flag is dated from the process's start — a process that has run flat out for two days is flagged as such five minutes after per_user is turned on. Where systemd puts each user's logins in a slice (`user-<uid>.slice`, cgroup v2 or v1 with CPU accounting), it also reads each user's CPU there, which counts processes that started and ended between two readings — a parallel `make`, a loop starting short programs. Work of another account inside a user's slice (`sudo`, `su`), and nomad's own when its cron session runs in its account's slice, is not counted as that user's.

What it keeps:

- **A sample** (`per_user_sample`, with the command line) only for a process above a floor: `floor_cpu_percent` (10), `floor_memory_gb` (2) or `floor_io_mb_per_s` (10). Idle shells and daemons leave no row. Raw samples are deleted after `sample_retention_days` (30; 0 keeps them), a batch of 100,000 per run.
- **Flags** (`per_user_alert`), kept. One row per episode — a process (or a user's processes together) above a rule's threshold without a break — and rule, written once the condition has held for the rule's duration and updated every run while it goes on: `last_seen`, `sustained_for_seconds`, `occurrences` (readings), and the episode's peaks.
- **Daily totals** (`per_user_daily`), kept: for each user, host and day (local date), CPU seconds used, time busy (the user's processes together above the CPU floor), peaks of CPU and memory, and I/O where readable. An interval is added to the day it ends; one longer than two hours (the collector was stopped) is left out.

The rules (a duration is a minimum: readings come every 5 minutes, so a 2-minute CPU rule fires on the first 5-minute average above it):

| Rule | Over | Condition | Severity |
|---|---|---|---|
| `cpu_10pct_5min` | one process | CPU ≥ 10% of a core for 5 min | informational |
| `cpu_50pct_2min` | one process | CPU ≥ 50% for 2 min | actionable |
| `memory_4gb_10min` | one process | resident memory ≥ 4 GB for 10 min | informational |
| `memory_16gb_2min` | one process | ≥ 16 GB for 2 min | actionable |
| `user_cpu_200pct_10min` | a user's processes | together ≥ 2 cores for 10 min | actionable |
| `user_memory_32gb_10min` | a user's processes | together ≥ 32 GB for 10 min | actionable |
| `io_50mbs_10min` | one process | reads + writes ≥ 50 MB/s for 10 min | informational |

A user's memory is the resident memory of their processes added up, so memory that forked workers share counts once for each.

`[[collectors.per_user.rules]]` tables replace them (see `nomad.toml.example`). System accounts (uid below `min_uid`), `users`, `user_commands` and processes started from `parent_paths` are never flagged; they are stored, marked, when above the floor.

I/O is what a process read and wrote through system calls — files on any filesystem, NFS included, pipes and sockets — so a large copy to NFS or a transfer through the host shows. Reading another user's I/O counters, and the executable path that `parent_paths` matches, needs root: run as another account, only that account's own processes have them. For a script run by an interpreter (`python3 /usr/local/sw/backup/backup.py`), `parent_paths` and `user_commands` also match the script, read from the command line, which needs no root: `user_commands = [["backupuser", "backup.py"]]`.

If `/proc` is mounted with `hidepid`, an account other than root sees only its own processes; the run says so (`nomad collectors`).

```bash
nomad per-user                       # this host's database
nomad per-user --db ~/.local/share/nomad/combined.db --days 30   # every site, on the hub
nomad per-user --mask                # user and command names replaced, for sharing
```

Each flagged line is a process (or a user's processes together) and a stretch of time: from when the condition began to when it was last seen, the rules it broke (`!` actionable, `i` informational), and its peaks.

## Storage servers

```toml
[collectors.storage]
enabled = true

[[collectors.storage.storage_devices]]
hostname = "nas1"
type = "zfs"

[[collectors.storage.storage_devices]]
hostname = "nfs-home"
type = "nfs"
paths = ["/export/home"]
```

Reached over SSH with `BatchMode` (a key, no password prompt) unless `hostname` is this host. `type = "zfs"` reads pool health and capacity; any type reads NFS exports and connected clients. Without ZFS, capacity comes only from the `paths` listed (df) — never from the server's root disk. An unreachable server is stored as `offline` with its capacity unknown (NULL), not as an empty server.

`[[storage_devices]]` and `[[network_tests]]` at the top level of `nomad.toml`, where older examples put them, are still read, with a warning to move them.

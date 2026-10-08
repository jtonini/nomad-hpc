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

`groups` reads membership with `getent group`: on the head node of each `[clusters]` entry (over SSH when it has a `host`), on the first reachable workstation of a `type = "workstations"` cluster with an `ssh_user`, and on this host otherwise. Job accounting needs `sacct` and is skipped without it. On the same host it looks up each member with `getent passwd` and records whether they still have an account (`has_account`): a hand-kept `/etc/group` goes on listing people whose accounts were deleted long ago. "No account" needs every run for two days to find none (getent says the same for an unreachable directory), and a run where the lookup fails keeps the last known answer. Every run stamps what it saw with one time (`collected_at`), so a membership no longer listed stops being stamped. A lab's members are the people listed in their cluster's latest run (give or take two days, for a failed run; a cluster not collected for 30 days while its site goes on is retired) who have an account; `nomad console roles NETID` says how many are not counted, and why.

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

From `sacct` (1.7.42) each job also gets `account`, `alloc_tres` (what Slurm
allocated, e.g. `cpu=6,gres/gpu:a40=1,gres/gpu=1,mem=32G,node=1`),
`alloc_gpus` (the GPUs in it: `gres/gpu` and `gres/gpu:TYPE`, never
`gres/gpumem` or `gres/gpuutil`), and two parts of its working directory:
`work_root`, the first (`/home`, `/scratch`), and `work_tail`, the last two
(`proj/run1`). The whole path is not kept. `squeue` doesn't report these;
what `sacct` stored stays.

`node_list` is kept as Slurm writes it, in range form (`n[01-04,07]`). Code
that needs the nodes themselves reads it with `nomad.hostlist.expand_hostlist`
(or `node_in`); splitting on commas or matching with LIKE misses ranges.

### Job numbers that come back

Slurm reuses job numbers: after its counter restarts (an outage that loses
its state) or wraps at `MaxJobId`, number 4711 is a new job. Before 1.7.42 the
new job's state, nodes and times were written onto the stored row, under the
earlier job's user, partition, name and submit time. Now, when a job arrives
whose number is stored with another submit time, and the stored job is
another job (another user or job name, or it ended more than 30 days before,
or it is still marked active but was submitted more than its time limit plus
30 days before -- a year without a limit -- as rows left RUNNING by an outage
that lost Slurm's state are), the earlier job moves aside to
`NUMBER@SUBMIT-TIME` (`4711@2026-03-01T10:00:00`; `~2` and so on if that is
taken) in every table with a job id (`jobs`, `job_summary`, `job_metrics`,
`job_similarity`, ...), and the number is the new job's. Rows in those
tables dated (`submit_time` or `timestamp`) from the new job's submission on
stay: they were written for the new job. A job moved aside while still marked
active becomes `UNKNOWN`. A requeued job (same user and name) stays one job,
and its submit time follows Slurm's (a requeue gets a new one). See
`nomad.db.jobkeys.place`.

### Start times

`squeue` reports a pending job's start as Slurm's *estimate*. It is never
stored (before 16 Sep 2026 it was, and when the job's outcome then came from
`job_metrics`, which did not update the start, the estimate stayed, with a
wait to match). `job_metrics` now takes `sacct`'s real start. Once per
database, finished jobs whose start disagrees with end − runtime by more than
two minutes are looked up in `sacct` and, when `sacct`'s job is the same job,
stored as `sacct` has it (no start for a job cancelled before it ran); those
`sacct` no longer has are corrected to end − runtime only when their start is
impossible (after the end, or before submission). At most 1,000 are looked
up a run, so a backlog takes several runs; how far it got is kept in
`config` (`repair.job_start_times.after`), and when done, what was done is
recorded under `repair.job_start_times`. Rows from before 1.7.19 with an
assumed end time are left to the outcome lookups above.

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

## Workstations over SSH

`workstation` runs its commands and probes on each machine over SSH
(`BatchMode`: a key, no password prompt), as the user `~/.ssh/config` gives
for that host, and runs them directly on a machine listed under the
collecting host's own name. Some lab machines print a banner on every login,
interactive or not, from a shell startup file. Since 1.7.27 nomad prints a
marker before each command and reads only what follows it, so such a banner
is never read as the machine's figures. To keep the banner off
non-interactive logins anyway, wrap it in `if [[ $- == *i* ]]; then ... fi`.

## Workstation mounts

Each `workstation` run also checks every mount on each machine (NFS, and
local filesystems other than the system's own). A small probe goes over the
same SSH connection to the machine's own `python3` (3.6 or later), so
nothing is installed on the workstations. It checks all mounts at once, each
in its own thread with a 3-second limit, and records whether `stat()`
answered (`workstation_mount_state`: `is_responsive`, `response_ms`). A call
on a dead NFS server cannot be interrupted, so a thread still waiting at the
limit is abandoned, the mount recorded as not responding, and the probe ends
anyway: a dead NAS costs one limit, and the other mounts are still reported.

Since 1.7.24 it also records the size of the filesystem behind each mount,
as `df` shows it: `total_bytes`, `used_bytes` and `avail_bytes` (what users
can still write). These are empty for a mount that did not answer, and in
rows from before 1.7.24. For an NFS mount they are the server's figures for
that export, so the lab machines report their NAS's space without nomad
reaching the NAS. "Used" is the whole export's, whoever wrote it.

Exports that are datasets of one ZFS pool each report their own used space
but share the pool's free space, and each one's total is its own used space
plus that shared free space. So their totals overlap and must not be added
up. When two exports of one server are read with the same free space
(within 0.1%, at most 1 GiB) in one run, whether by the same lab machine or
by two (an export mounted on a single machine is read by no other),
`nomad console roles` says "free space shared with ...", leaves out each
one's total, and adds a line with their space together: their used space
summed, the free space counted once. Exports on different server addresses
are never put together, even if the same NAS answers on both.

Only the collecting host needs 1.7.24 for the sizes; the hub's combined
database gains the columns at its next sync.

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

Reached over SSH with `BatchMode` (a key, no password prompt) unless `hostname` is this host. `type = "zfs"` reads pool health and capacity: the space users have, from each pool's root dataset (`zfs list`), so RAIDZ parity isn't counted as it is in `zpool list`, and a boot pool (`boot-pool`, `freenas-boot`, `bpool`) isn't counted at all. It works on TrueNAS CORE (FreeBSD) as on Linux. Any type reads NFS exports and connected clients. A login banner is skipped, as for workstations. `nomad lab add-nas` adds a NAS (see docs/config.md). Without ZFS, capacity comes only from the `paths` listed (df) — never from the server's root disk. An unreachable server is stored as `offline` with its capacity unknown (NULL), not as an empty server.

`[[storage_devices]]` and `[[network_tests]]` at the top level of `nomad.toml`, where older examples put them, are still read, with a warning to move them.

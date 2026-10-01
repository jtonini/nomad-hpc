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
| `nfs` | on | hosts that mount NFS (client side) | `nfsiostat` (nfs-utils) | `nfs_stats` |
| `groups` | on | every site | `getent`; `sacct` for job accounting | `group_membership`, `job_accounting` |
| `interactive` | off | hosts running RStudio or Jupyter | — | `interactive_sessions`, `interactive_summary` |
| `workstation` | off | a hub reaching workstations over SSH | `ssh` | `workstation_state`, `workstation_user_snapshot`, ... |
| `per_user` | off | login nodes, shared interactive hosts | Python `psutil` | `per_user_sample`, `per_user_alert` |
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

# Infrastructure Monitoring

Beyond the clusters, NØMAÐ watches the lab workstations people compute on and
the storage servers everyone's data lives on. Each appears in the Console's
Infrastructure page beside the clusters and interactive servers (one card per
monitored source, with how old its data is), and to a lab's PI on My lab for
the lab's own machines and storage.

## Workstations

The `workstation` collector reads each machine over ssh, with a key and no
password prompt, from the host that collects it: a lab's own head node, or
the hub for a lab without one. Nothing is installed on the workstations: a
small probe goes to each machine's own `python3` over the same connection.

| Read | |
|---|---|
| Status | online, offline, unreachable |
| CPU | load and use, the CPU model and cores |
| Memory | used and total |
| Disks | each local disk's use (and `workstation_disk` alerts at the disk thresholds) |
| Mounts | each NFS and local mount: whether it answers, and its size, used and free space |
| People | who is logged in, and per-person detail |
| Processes | counts, and zombies |
| Sessions | long computations, scored like jobs ([Educational analytics](edu.md)) |

Add a lab's machine with `nomad lab add-machine` on the host that collects it
(it turns the collector on there and says whether the machine answers over
ssh), or list machines in nomad.toml:

```toml
[collectors.workstation]
enabled = true

[[workstations]]
hostname = "ws-physics-01"
department = "physics"        # a lab's group ("NETID$") assigns it to that lab
```

```bash
nomad lab add-machine smith ws-chem-03          # shows the change
nomad lab add-machine smith ws-chem-03 --apply
nomad diag workstation ws-chem-03               # what was read, and what it needs
```

[Collectors: workstations over SSH](collectors.md#workstations-over-ssh) and
[workstation mounts](collectors.md#workstation-mounts) have the details;
[Configuration](config.md#adding-machines-and-labs-nomad-lab) has
`nomad lab`.

## Storage servers

The `storage` collector reads ZFS and NFS servers over ssh: pool health
(online, degraded, faulted), the space users have (from each pool's root
dataset, so RAIDZ parity isn't counted), NFS exports and connected clients.
An unreachable server is stored as `offline` with its capacity unknown, not
as an empty server.

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

or `nomad lab add-nas LAB HOST` (LAB `shared` for storage that is
everyone's). The lab machines' NFS mounts also report their exports' space,
so a lab's NAS shows its size even where nomad doesn't reach the NAS itself.

Who holds each server's space, and on what, is a separate collector,
`storage_use`, fed by a small script on each server:
[Storage use](storage.md).

```bash
nomad diag nas nas1            # a storage server's health and trend
nomad storage use              # who holds each path's space
nomad storage status           # what the storage_use collector read, and when
```

## Alerts

Disks filling (with a forecast of when they fill), a workstation's own disk,
a degraded pool, and a source that stopped reporting are alerted as the
clusters are: [Alerts](alerts.md).

## Using it

- **A job failed with I/O errors**: check the storage server's health and its
  mounts' response times on the lab machines (`nomad diag nas`, the Console's
  Infrastructure page).
- **A lab's machines are idle or overloaded**: the Console's workstation
  pages, and `nomad facts busy` for when each group of workstations is
  busiest.
- **Capacity**: the [usage report](usage-report.md)'s storage section, and
  data unchanged for a year ([Storage use](storage.md#data-unchanged-for-a-year)).

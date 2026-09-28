# Infrastructure Monitoring

NØMAÐ extends beyond compute nodes to monitor research workstations and storage systems, providing a holistic view of your computing environment.

## Dashboard Views

Access these views from the web dashboard tabs:
```bash
nomad dashboard
# Then click: Workstations | Storage
```

---

## Workstation Monitoring

Track departmental workstations across your institution.

### What's Monitored

| Metric | Description |
|--------|-------------|
| Status | Online/offline/unreachable |
| CPU Load | Current utilization percentage |
| Memory | Used/total RAM |
| Disk | Root filesystem usage |
| Users | Currently logged-in users |
| Uptime | Time since last reboot |

### Dashboard View

The Workstations tab shows machines grouped by department:
```
----------------------------------------------------------------------
 Workstations                                             14 online
----------------------------------------------------------------------

 Biology (4)
 +---------+--------+--------+--------+---------+---------+
 | Machine | Status | CPU    | Memory | Disk    | Users   |
 +---------+--------+--------+--------+---------+---------+
 | bio-ws1 | UP     | 23%    | 8/32GB | 45%     | alice   |
 | bio-ws2 | UP     | 67%    | 24/32GB| 52%     | bob,chen|
 | bio-ws3 | UP     | 5%     | 4/32GB | 38%     | -       |
 | bio-ws4 | DOWN   | -      | -      | -       | -       |
 +---------+--------+--------+--------+---------+---------+

 Chemistry (3)
 ...
----------------------------------------------------------------------
```

### Configuration

Add workstations to your config file:
```toml
# ~/.config/nomad/nomad.toml

[[workstations]]
name = "bio-ws1"
host = "bio-ws1.dept.edu"
department = "Biology"

[[workstations]]
name = "bio-ws2"
host = "bio-ws2.dept.edu"
department = "Biology"

[[workstations]]
name = "chem-ws1"
host = "chem-ws1.dept.edu"
department = "Chemistry"
```

Or use the collector:
```bash
nomad collect workstations --discover   # Auto-discover via DNS
nomad collect workstations --add bio-ws1.dept.edu
```

---

## Storage Monitoring

Monitor NFS servers, ZFS pools, and storage capacity.

### What's Monitored

| Metric | Description |
|--------|-------------|
| Capacity | Total/used/available space |
| Utilization | Percentage used |
| ZFS Health | Pool status (online/degraded/faulted) |
| IOPS | Read/write operations per second |
| Throughput | MB/s read/write |
| Latency | Average I/O response time |
| NFS Clients | Connected client count |

### Dashboard View

The Storage tab displays server status and pool health:
```
----------------------------------------------------------------------
 Storage Servers                                          3 healthy
----------------------------------------------------------------------

 storage01 - Primary Home Directories
 +-----------+---------+---------+--------+----------------+
 | Pool      | Status  | Used    | IOPS   | Clients        |
 +-----------+---------+---------+--------+----------------+
 | tank/home | ONLINE  | 45/100TB| 2.3K   | 47 connected   |
 | tank/apps | ONLINE  | 2/10TB  | 450    | 47 connected   |
 +-----------+---------+---------+--------+----------------+

 storage02 - Scratch Space
 +-----------+---------+---------+--------+----------------+
 | Pool      | Status  | Used    | IOPS   | Clients        |
 +-----------+---------+---------+--------+----------------+
 | scratch   | DEGRADED| 82/100TB| 5.1K   | 89 connected   |
 +-----------+---------+---------+--------+----------------+
 [!] storage02/scratch: 1 drive faulted, resilver in progress

----------------------------------------------------------------------
```

### Configuration
```toml
# ~/.config/nomad/nomad.toml

[[storage]]
name = "storage01"
host = "storage01.cluster.edu"
type = "nfs"
pools = ["tank/home", "tank/apps"]

[[storage]]
name = "storage02"
host = "storage02.cluster.edu"
type = "nfs"
pools = ["scratch"]
```

### Alerts

Set up alerts for storage conditions:
```toml
[alerts.storage]
capacity_warning = 80      # Warn at 80% full
capacity_critical = 95     # Critical at 95% full
inode_warning = 80
inode_critical = 95
latency_warning_ms = 50    # Warn if latency > 50ms
pool_degraded = true       # Alert on ZFS degraded state
```

---

## Use Cases

### Correlating Job Failures

When jobs fail, check infrastructure:

1. Job failed with I/O errors - Check Storage tab for NFS issues
2. Job timed out - Check if storage latency spiked
3. Multiple failures from one department - Check their workstations

### Capacity Planning

Track trends over time:
```bash
nomad report storage --days 30    # 30-day storage trend
nomad report workstations --idle  # Find underutilized machines
```

### Proactive Maintenance

Get notified before problems occur:

- Storage at 80% - Plan cleanup or expansion
- ZFS pool degraded - Replace failing drive
- Workstation offline - Check before users complain

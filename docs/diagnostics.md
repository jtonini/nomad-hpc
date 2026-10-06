# Diagnostics

The `nomad diag` commands provide targeted analysis for common HPC issues, helping administrators quickly identify root causes of performance problems.

## Commands
```bash
nomad diag network    # Network performance analysis
nomad diag storage    # Storage health and I/O patterns
nomad diag node       # Node-level resource bottlenecks
nomad diag workstation HOST   # A departmental workstation's latest reading
```

---

## Network Diagnostics

Analyze network performance between nodes.
```bash
nomad diag network
nomad diag network --src node01 --dst node02
nomad diag network --threshold 100   # Flag if <100 MB/s
```

### What It Checks

- **Bandwidth**: Measures throughput between node pairs
- **Latency**: Round-trip time for packets
- **Packet loss**: Percentage of dropped packets
- **Path analysis**: Identifies bottleneck links

The network collector pings every run. It measures throughput (and the TCP
retransmits during it) only with `throughput = true` under
`[collectors.network_perf]`, at most hourly, with `iperf3 -s` (or ssh) at the
far end. Without it the diagnostic says "Throughput Not Measured" rather than
showing 0 Mbps as a reading, and it shows the last measured throughput when
the newest row is ping only.

### Output Example
```
Network Diagnostics Report
======================================================================

Path Analysis: node01 -> node02
  Hops: 2 (node01 -> switch01 -> node02)
  Latency: 0.3ms (excellent)
  Bandwidth: 9.2 Gbps (92% of 10G link)
  Packet Loss: 0.00%

Path Analysis: node01 -> storage01
  Hops: 3 (node01 -> switch01 -> switch02 -> storage01)
  Latency: 0.8ms (good)
  Bandwidth: 4.1 Gbps (41% of 10G link)  [!] Below expected
  Packet Loss: 0.01%

Summary:
  Total paths tested: 12
  Degraded paths: 1
  Recommendation: Check switch02 port utilization
```

---

## Storage Diagnostics

Examine storage health and I/O patterns.
```bash
nomad diag storage
nomad diag storage --path /home
nomad diag storage --detailed
```

### What It Checks

- **NFS performance**: Read/write throughput, latency
- **Disk utilization**: Space usage, inode counts
- **I/O patterns**: Sequential vs random, read/write ratio
- **Queue depth**: Outstanding I/O requests
- **Error rates**: Failed operations, retries
- **Fill rate**: growth by least squares over daily averages, from at least
  a week of readings in the last 30 days, and the days until full at that
  rate: under 30 days is noted, under 14 is flagged, under 3 is high. A
  server read for less than a week has no fill rate.

### Output Example
```
Storage Diagnostics Report
======================================================================

/home (NFS: storage01:/export/home)
  Capacity: 45.2 TB / 100 TB (45%)
  Inodes: 12M / 50M (24%)
  Read throughput: 850 MB/s (good)
  Write throughput: 420 MB/s (good)
  Latency: 2.1ms avg, 15ms p99
  Active clients: 47

/scratch (NFS: storage02:/export/scratch)
  Capacity: 82.1 TB / 100 TB (82%)  [!] High utilization
  Inodes: 45M / 50M (90%)  [!] Critical
  Read throughput: 1.2 GB/s (excellent)
  Write throughput: 180 MB/s  [!] Degraded
  Latency: 8.3ms avg, 250ms p99  [!] High latency spikes
  Active clients: 89

Recommendations:
  - /scratch approaching inode limit - clean old files
  - /scratch write performance degraded - check ZFS pool status
```

---

## Node Diagnostics

Drill into node-level resource bottlenecks.
```bash
nomad diag node
nomad diag node gpu-01
nomad diag node --all
```

### What It Checks

- **CPU**: Utilization, load average, steal time
- **Memory**: Usage, swap activity, OOM events
- **GPU**: Utilization, temperature, memory, errors
- **I/O**: Wait percentage, disk throughput
- **Network**: Interface utilization, errors

### Output Example
```
Node Diagnostics: gpu-03
======================================================================

CPU
  Utilization: 78% (32/40 cores active)
  Load Average: 28.5, 27.2, 25.8
  Steal Time: 0.0%
  Status: [OK] Normal

Memory
  Used: 187 GB / 256 GB (73%)
  Swap: 2.1 GB / 32 GB (6%)  [!] Swap activity detected
  OOM Events (24h): 0
  Status: [!] Monitor swap usage

GPU (4x NVIDIA A100)
  GPU 0: 92% util, 68C, 38/40 GB mem
  GPU 1: 88% util, 65C, 35/40 GB mem
  GPU 2: 0% util, 42C, 0/40 GB mem  [!] Idle
  GPU 3: 95% util, 71C, 39/40 GB mem
  ECC Errors: 0
  Status: [!] GPU 2 underutilized

I/O
  Wait: 3.2%
  Read: 450 MB/s
  Write: 120 MB/s
  Status: [OK] Normal

Recommendations:
  - Swap activity on memory-bound workload - consider job memory limits
  - GPU 2 idle while others near capacity - check job GPU allocation
```

---

## Workstation Diagnostics

A workstation's latest reading from the workstation collector, judged; the
Console's Workstation Diagnostic panel shows the same.
```bash
nomad diag workstation labws1
```

### What It Checks

- **Memory**: above 85% used is flagged, above 95% critical
- **Local disk**: above 85% is flagged, above 95% critical
- **Load**: above the core count is flagged, above twice the core count critical
- **Swap**: more than 1 GB in use
- **Zombie processes**: any; more than 10 is flagged as many
- **Not reporting**: the latest reading is more than an hour old
- **Incomplete reading**: a figure the collector couldn't read (it stores
  zeros when a command fails on the machine) is named as missing, not
  passed as 0%

The last day's readings are summarized (samples, average load and memory,
most users). They are not judged for trends: readings five minutes apart are
too noisy for a slope and acceleration from the last three.

---

## Common Options

All `nomad diag` commands support:

| Option | Description |
|--------|-------------|
| `--db PATH` | Specify database path |
| `--json` | Output as JSON |
| `--quiet` | Minimal output (exit code only) |
| `-v, --verbose` | Detailed output |

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | All checks passed |
| 1 | Warnings detected |
| 2 | Critical issues found |
| 3 | Error running diagnostics |

Useful for scripting and automated monitoring.

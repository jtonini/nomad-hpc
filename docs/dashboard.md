# Standalone dashboard

!!! note "The Console replaces it"
    `nomad dashboard` is NØMAÐ's first web view: one process, one database,
    no sign-in. The [Console](console.md) now does everything it did, for
    many people at once and with each seeing what their role allows, so new
    deployments should use the Console. The dashboard still ships, for
    `nomad demo` and for a quick look at one database on one machine.

## Launching
```bash
nomad dashboard                      # the database nomad.toml names, else ~/nomad_demo.db
nomad dashboard --db combined.db     # another database
nomad demo                           # the demo database, then the dashboard
```

Open http://localhost:8050 in your browser. It has no sign-in: keep it on
`127.0.0.1` (the default) and reach it through an SSH tunnel.

## Tabs

### Cluster Tabs

Per-cluster views showing:
- Node status (idle, allocated, down)
- Partition utilization
- CPU and memory pressure indicators
- Active jobs count

### Network

3D job similarity network visualization:
- Jobs as nodes, colored by health
- Edges connect similar jobs
- "Safe zone" and "danger zone" regions emerge from data
- Failure clustering analysis

### Resources

Resource usage views for administrators:
- CPU hours by cluster/group
- Filter by cluster, group, and time period
- Resource consumption patterns
- Quietest hours for job scheduling

### Activity

Job activity and history:
- Recent job completions
- Failed job categories
- Interactive session monitoring (RStudio/Jupyter)

## Remote Access

The dashboard listens on localhost only. Reach it through an SSH tunnel:

```bash
ssh -L 8050:localhost:8050 user@hpc-head
```

It has no sign-in and no scoping, so don't put it behind a proxy for other
people: that is what the Console is for.

## GPU Panel

The node detail panel shows GPU metrics for any node with NVIDIA GPUs.

### nvidia-smi utilization

The primary GPU utilization bar reflects the kernel-launch duty cycle reported
by `nvidia-smi`. This is the standard metric available on all NVIDIA GPU nodes.

### Real Utilization (DCGM)

When DCGM is active on a node, a second bar labeled **Real Util** appears below
the nvidia-smi bar. Real Utilization is a weighted composite of pipeline activity
counters (SM, Tensor, DRAM, GR engine) that reflects what the GPU is actually
computing, not just whether any kernel is scheduled.

A significant gap between nvidia-smi utilization and Real Util is a signal worth
investigating — see [GPU Monitoring](gpu.md#interpreting-the-utilization-gap).

### Workload badge

A color-coded badge below the utilization bars shows the dominant workload
category for the node's GPUs: `tensor-heavy compute`, `FP64 / HPC compute`,
`memory-bound`, `compute-active`, `idle`, and others. Colors follow the
Okabe-Ito colorblind-safe palette. See [Workload Classification](gpu.md#workload-classification)
for the full category definitions.

### Health indicator

A **WARN**, **HOT**, or **CRIT** badge appears next to the GPU name when
hardware health issues are detected via DCGM (PCIe replay errors, high
temperature, ECC errors, or row remap failures). No badge means OK.

### DCGM pill

A small **DCGM** pill next to the GPU name indicates that enhanced metrics are
active for that node. Nodes without DCGM show the nvidia-smi bar only and no
badge or Real Util row.

# GPU Monitoring

NØMAÐ collects GPU metrics from NVIDIA GPUs using `nvidia-smi` as the standard
source, with optional enhanced metrics via DCGM (NVIDIA Data Center GPU Manager)
when available. The two sources are transparent to the user — NØMAÐ detects DCGM
automatically and falls back to `nvidia-smi` silently.

---

## Data Sources

| Source | Metrics | Availability |
|--------|---------|--------------|
| `nvidia-smi` | Utilization, memory, temperature, power | Any NVIDIA GPU node |
| DCGM | All of the above + Real Util, pipeline counters, hardware health | Datacenter GPUs with DCGM installed |

The `data_source` column in `gpu_stats` records which source was used for each
record. The dashboard shows a **DCGM** pill next to the GPU name when enhanced
metrics are active.

---

## Real Utilization

`nvidia-smi` reports GPU utilization as the fraction of time at least one kernel
was running — a kernel-launch duty cycle. A GPU can show 100% utilization while
its tensor cores are completely idle.

**Real Utilization** is a weighted composite of DCGM pipeline activity counters
that reflects what the GPU is actually doing:

```
Real Util = clamp(0, 100,
    W_sm     × SM_ACTIVE
  + W_tensor × TENSOR_ACTIVE
  + W_dram   × DRAM_ACTIVE
  + W_gr     × GR_ENGINE_ACTIVE)
```

Three presets are available, configurable in `nomad.toml`:

```toml
[collectors.gpu]
real_util_weights = "ai"   # ai | hpc | memory | [custom list]
```

| Preset | Weights (SM, Tensor, DRAM, GR) | Best for |
|--------|-------------------------------|----------|
| `ai` | 0.35, 0.35, 0.20, 0.10 | DL training, LLM inference |
| `hpc` | 0.45, 0.15, 0.25, 0.15 | Scientific computing, mixed CUDA |
| `memory` | 0.35, 0.10, 0.40, 0.15 | Bandwidth-heavy, stencil codes |

Custom weights are also supported:

```toml
real_util_weights = [0.40, 0.30, 0.20, 0.10]  # SM, Tensor, DRAM, GR
```

Weights are auto-normalized to sum to 1.

The Real Utilization metric and workload classification framework are adapted
from [KempnerPulse](https://github.com/KempnerInstitute/kempnerpulse)
(Kempner Institute, MIT license), based on NVIDIA DCGM profiling metric guidance.

---

## Workload Classification

When DCGM data is available, each GPU record is assigned one of 12 workload
categories based on pipeline counter patterns. Categories are evaluated in
priority order; the first match wins.

| Category | Signal | Meaning |
|----------|--------|---------|
| **tensor-heavy compute** | Tensor ≥ 50%, SM ≥ 60% | DL training, large-scale inference. GPU used for its intended purpose. |
| **tensor compute** | Tensor ≥ 15%, SM ≥ 40% | Mixed-precision, moderate neural network activity. |
| **FP64 / HPC compute** | FP64 ≥ 20%, SM ≥ 50% | Classical scientific computing: molecular dynamics, quantum chemistry, climate simulation. |
| **I/O or data-loading** | PCIe ≥ 1 GB/s, SM < 30% | GPU waiting for data. Common during dataset loading or checkpoint I/O. |
| **memory-bound** | DRAM ≥ 50%, SM < 50% | Bandwidth is the bottleneck. The workload may benefit from data layout optimization. |
| **compute-heavy** | SM ≥ 80% | High SM occupancy. General CUDA compute, older code without tensor core use. |
| **compute-active** | SM ≥ 50% | Moderate SM activity. Mixed or general-purpose CUDA kernels. |
| **memory-active** | DRAM ≥ 40% | Significant DRAM traffic without SM saturation. |
| **busy, low SM use** | GR ≥ 40%, SM < 25% | Kernel launch overhead, synchronization barriers, or driver activity. |
| **low utilization** | GR < 15%, SM < 15%, DRAM < 15% | Barely active — job may be stalled or using the GPU incidentally. |
| **idle** | Real Util < 5%, all counters low | Nothing running. |
| **mixed / moderate** | (fallthrough) | No single dominant pattern. |

The dashboard displays the workload category as a color-coded badge using the
[Okabe-Ito colorblind-safe palette](https://jfly.uni-koeln.de/color/):

- **Blue** — tensor workloads (tensor-heavy, tensor)
- **Teal** — FP64 / HPC compute
- **Amber** — memory and I/O workloads
- **Mauve** — compute workloads
- **Orange** — I/O or data-loading
- **Gray** — idle, low utilization, mixed

### Interpreting the utilization gap

The difference between nvidia-smi utilization and Real Utilization is often the
most actionable signal:

- **Small gap** (< 10 pts): GPU is well-utilized; pipeline stages match the
  workload type.
- **Large gap** (> 20 pts) with `memory-bound`: Data movement is the bottleneck.
  Consider larger batch sizes, prefetching, or NVLink if available.
- **Large gap** with `busy, low SM use`: Many small kernels or excessive
  synchronization. Kernel fusion or async execution may help.
- **High nvidia-smi, low Real Util, `idle`**: A single process holds the GPU
  but is not actively computing (e.g., a job in a data-loading loop).

---

## Hardware Health Monitoring

When DCGM is active, NØMAÐ collects hardware health signals that precede
visible performance degradation:

| Signal | Meaning |
|--------|---------|
| **PCIe replay rate** | Link errors causing retransmissions. Any sustained rate > 0/s indicates link issues. |
| **ECC correctable errors** | Transient memory bit flips. Monitor for trends. |
| **ECC uncorrectable errors** | Serious memory failures. Investigate immediately. |
| **Row remap failure** | HBM memory (A100, H100) permanently degraded. Remove from production. |

Health status per GPU:

| Status | Condition |
|--------|-----------|
| **OK** | Normal operation |
| **WARN** | PCIe replay rate > 0/s |
| **HOT** | Temperature at or above model threshold (A100: 93°C, RTX 6000 Ada: 92°C, H100: 95°C) |
| **CRIT** | Row remap failure or uncorrectable ECC errors |

The dashboard shows a colored health badge (WARN/HOT/CRIT) next to the GPU
name in the node detail panel. CRIT nodes should be removed from production.

---

## SSH Mode

For headnode deployments where GPUs are on compute nodes, configure SSH mode:

```toml
[collectors.gpu]
enabled = true
ssh_user = "zeus"
gpu_nodes = ["node51", "node52", "node53"]
```

NØMAÐ SSHes to each node and runs `nvidia-smi` (and `dcgmi` if available)
remotely. The same fallback logic applies per node.

---

## SLURM / CUDA GPU Visibility

User-facing commands (`nomad edu explain_job`, etc.) filter GPU data to the
GPUs visible to the current job. Detection priority:

1. `CUDA_VISIBLE_DEVICES`
2. `NVIDIA_VISIBLE_DEVICES`
3. `SLURM_STEP_GPUS`
4. `SLURM_JOB_GPUS`
5. All GPUs (admin mode)

---

## CLI Commands

### `nomad diag gpu`

Utilization summary, workload distribution, and temperature status.

```
nomad diag gpu                    # All nodes, last 24h
nomad diag gpu --node node51      # Specific node
nomad diag gpu --hours 48         # Longer window
nomad diag gpu --health           # Hardware health only (PCIe, ECC, remap)
```

### `nomad insights gpu`

Narrative summary of GPU usage patterns and health concerns. Flags the
utilization gap when nvidia-smi reports significantly higher utilization
than Real Util.

```
nomad insights gpu                # All nodes, last 6h
nomad insights gpu --hours 12     # Longer window
nomad insights gpu --node spdr16  # Specific node
```

---

## Installing DCGM

DCGM is free and available for all supported datacenter GPUs.

**Rocky Linux / RHEL:**
```bash
dnf install -y epel-release
dnf install -y datacenter-gpu-manager
systemctl enable --now nvidia-dcgm
```

**Ubuntu / Debian:**
```bash
apt-get install -y datacenter-gpu-manager
systemctl enable --now nvidia-dcgm
```

Verify:
```bash
dcgmi discovery -l
dcgmi dmon -e 1001 -c 1
```

NØMAÐ detects `dcgmi` in PATH automatically on the next collection cycle.
No configuration change is required.

### Supported GPUs

| GPU | nvidia-smi | DCGM |
|-----|-----------|------|
| NVIDIA A100 | ✓ | ✓ |
| NVIDIA A40 | ✓ | ✓ |
| NVIDIA H100 / H200 | ✓ | ✓ |
| NVIDIA RTX 6000 Ada | ✓ | ✓ (DCGM 3.3.9+, field IDs 1001-1005) |
| NVIDIA RTX 4090 / 3090 / 3050 | ✓ | Management only — profiling not supported |
| NVIDIA GTX series (Maxwell/Pascal) | ✓ | — |
| AWS p3/p4/g4dn/g5 instances | ✓ | ✓ (if installed) |
| AMD GPUs | — | — |

**Profiling counter availability is GPU-class dependent.** NVIDIA gates the
profiling fields (SM_ACTIVE, TENSOR_ACTIVE, DRAM_ACTIVE, GR_ENGINE_ACTIVE) to
datacenter and professional GPUs. Consumer GeForce cards (RTX 40/30/20 series)
support DCGM for management metrics (utilization, memory, temperature, power)
but profiling counters return error -36 ("Profiling is not supported"). The
collector detects this automatically and falls back to nvidia-smi data with
no DCGM enrichment fields populated. Real Utilization and workload
classification are unavailable for these GPUs.

NØMAÐ has been verified with DCGM on:
- **NVIDIA RTX 6000 Ada Generation** with DCGM 3.3.9 (full profiling support)
- **NVIDIA A100, A40, H100** (full profiling support — datacenter GPUs)
- **NVIDIA RTX 4090** (DCGM 4.5+ — management only, no profiling)

AMD GPU support is a future initiative (ROCm).

---

## Insight Engine Integration

GPU signals are automatically included in `nomad insights brief`, `nomad insights detail`,
and the dashboard Insights tab. No configuration is required — signals fire when
the relevant data is present in the database.

### Signals

| Signal | Severity | Condition | Requires DCGM |
|--------|----------|-----------|---------------|
| **GPU Job Failures** | WARNING | > 20% of GPU jobs failing | No |
| **GPU Out of Memory** | WARNING/NOTICE | Any GPU jobs hit VRAM limit | No |
| **GPU Utilization Gap** | WARNING/NOTICE | nvidia-smi avg > 30%, Real Util lags > 20 pts | Yes |
| **GPU Workload Pattern** | NOTICE | memory-bound ≥ 50% or idle ≥ 70% of samples | Yes |
| **GPU Workload Pattern** | INFO | tensor-heavy or FP64 dominant ≥ 60% | Yes |
| **GPU Hardware Health** | CRITICAL | Row remap failure or uncorrectable ECC | Yes |
| **GPU Hardware Health** | WARNING | Temperature at or above model threshold | Yes |
| **GPU Hardware Health** | NOTICE | PCIe replay errors detected | Yes |

### Example narratives

**GPU Utilization Gap (NOTICE):**
> node51 shows a 30-point gap between nvidia-smi utilization (54%) and Real
> Utilization (24%). The GPU appears busy but the compute pipeline is underused.
> Consider larger batch sizes, kernel fusion, or data prefetching.

**GPU Workload Pattern — memory-bound (NOTICE):**
> node51 has been running memory-bound workloads 65% of the time. GPU compute
> pipeline is underutilized relative to memory bandwidth. Possible improvements:
> increase batch size, optimize data layout, or use prefetching to overlap
> compute and data transfer.

**GPU Workload Pattern — productive (INFO):**
> spdr16 is running tensor-heavy compute workloads 72% of the time — GPU
> resources are being used effectively.

**GPU Hardware Health — CRITICAL:**
> node51 GPU 2 has a row remap failure — HBM memory is permanently degraded.
> This GPU should be removed from production and scheduled for replacement.

**GPU Hardware Health — NOTICE:**
> spdr17 GPU 0 is logging PCIe replay errors (0.023/s). This indicates link
> instability — check the PCIe slot, riser card, or cable. Left unaddressed,
> this typically escalates to link failure.

### Signal thresholds

Utilization gap and workload pattern signals require at least 3 samples within
the analysis window before firing, avoiding false positives from transient
conditions. Hardware health signals fire on any occurrence within the window —
PCIe errors and ECC events are always worth flagging.



```sql
-- Extended gpu_stats table
CREATE TABLE gpu_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    node_name TEXT,
    gpu_index INTEGER,
    gpu_name TEXT,
    gpu_util_percent REAL,       -- nvidia-smi kernel duty cycle
    memory_util_percent REAL,
    memory_used_mb INTEGER,
    memory_total_mb INTEGER,
    memory_free_mb INTEGER,
    temperature_c INTEGER,
    power_draw_w REAL,
    power_limit_w REAL,
    compute_processes INTEGER,
    -- DCGM fields (NULL when nvidia-smi only)
    sm_active_pct REAL,          -- Streaming multiprocessor activity
    tensor_active_pct REAL,      -- Tensor core activity
    dram_active_pct REAL,        -- DRAM bandwidth utilization
    gr_engine_active_pct REAL,   -- Graphics engine activity
    fp64_active_pct REAL,        -- FP64 pipeline activity
    real_util_pct REAL,          -- Weighted composite (see above)
    workload_class TEXT,         -- 12-category classification
    data_source TEXT             -- 'nvidia-smi' or 'dcgm'
);

-- Hardware health time series
CREATE TABLE gpu_health (
    timestamp DATETIME NOT NULL,
    node TEXT NOT NULL,
    gpu_id INTEGER NOT NULL,
    pcie_replay_count INTEGER,
    pcie_replay_rate_per_sec REAL,
    ecc_correctable_total INTEGER,
    ecc_uncorrectable_total INTEGER,
    rows_remapped_correctable INTEGER,
    rows_remapped_uncorrectable INTEGER,
    rows_remapped_pending INTEGER,
    row_remap_failure INTEGER,
    health_status TEXT,          -- OK | WARN | HOT | CRIT
    PRIMARY KEY (timestamp, node, gpu_id)
);
```

Existing databases are migrated automatically on the next `nomad collect` run.
No manual schema changes are required.

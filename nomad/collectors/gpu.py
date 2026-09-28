# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
NØMAÐ GPU Collector

Collects NVIDIA GPU metrics from nvidia-smi (standard) or DCGM (enhanced).
DCGM is used opportunistically when available; nvidia-smi is the fallback.

Real Utilization metric and workload classification framework
adapted from KempnerPulse (Kempner Institute, MIT license)
https://github.com/KempnerInstitute/kempnerpulse
Based on NVIDIA DCGM profiling metric guidance.
"""

import logging
import os
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .base import BaseCollector, registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Weight presets for Real Utilization composite metric
# Order: [SM_ACTIVE, TENSOR_ACTIVE, DRAM_ACTIVE, GR_ENGINE_ACTIVE]
# ---------------------------------------------------------------------------
REAL_UTIL_PRESETS: dict[str, list[float]] = {
    "ai":     [0.35, 0.35, 0.20, 0.10],  # DL training, LLM inference
    "hpc":    [0.45, 0.15, 0.25, 0.15],  # Scientific computing, mixed CUDA
    "memory": [0.35, 0.10, 0.40, 0.15],  # Bandwidth-heavy, stencil codes
}

# Per-model temperature warning thresholds (°C)
GPU_TEMP_WARN: dict[str, int] = {
    "A100":           93,
    "H100":           95,
    "H200":           95,
    "RTX 6000 Ada":   92,
    "RTX 6000":       92,
    "A40":            93,
    "DEFAULT":        93,
}

# DCGM profiling field IDs used with dcgmi dmon
DCGM_FIELDS = {
    "gr_engine":      1001,  # DCGM_FI_PROF_GR_ENGINE_ACTIVE
    "sm_active":      1002,  # DCGM_FI_PROF_SM_ACTIVE
    "sm_occupancy":   1003,  # DCGM_FI_PROF_SM_OCCUPANCY
    "tensor_active":  1004,  # DCGM_FI_PROF_PIPE_TENSOR_ACTIVE
    "dram_active":    1005,  # DCGM_FI_PROF_DRAM_ACTIVE
    "fp64_active":    1006,  # DCGM_FI_PROF_PIPE_FP64_ACTIVE (not on RTX 6000 Ada)
    "fp32_active":    1007,  # DCGM_FI_PROF_PIPE_FP32_ACTIVE
    "fp16_active":    1008,  # DCGM_FI_PROF_PIPE_FP16_ACTIVE
    "pcie_tx_bytes":  1009,  # DCGM_FI_PROF_PCIE_TX_BYTES
    "pcie_rx_bytes":  1010,  # DCGM_FI_PROF_PCIE_RX_BYTES
    "pcie_replay":     312,  # DCGM_FI_DEV_PCIE_REPLAY_COUNTER
    "ecc_correctable": 312,  # DCGM_FI_DEV_ECC_SBE_AGG_TOTAL
    "ecc_uncorrectable": 313,  # DCGM_FI_DEV_ECC_DBE_AGG_TOTAL
}


@dataclass
class GPUStats:
    """NVIDIA GPU statistics (nvidia-smi baseline)."""
    gpu_index: int
    gpu_name: str
    gpu_util_percent: float
    memory_util_percent: float
    memory_used_mb: int
    memory_total_mb: int
    memory_free_mb: int
    temperature_c: int
    power_draw_w: float
    power_limit_w: float
    compute_processes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            'gpu_index': self.gpu_index,
            'gpu_name': self.gpu_name,
            'gpu_util_percent': self.gpu_util_percent,
            'memory_util_percent': self.memory_util_percent,
            'memory_used_mb': self.memory_used_mb,
            'memory_total_mb': self.memory_total_mb,
            'memory_free_mb': self.memory_free_mb,
            'temperature_c': self.temperature_c,
            'power_draw_w': self.power_draw_w,
            'power_limit_w': self.power_limit_w,
            'compute_processes': self.compute_processes,
        }


@dataclass
class DCGMStats:
    """Extended GPU statistics from DCGM profiling counters."""
    gpu_index: int
    sm_active_pct: float = 0.0
    tensor_active_pct: float = 0.0
    dram_active_pct: float = 0.0
    gr_engine_active_pct: float = 0.0
    fp64_active_pct: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            'sm_active_pct': self.sm_active_pct,
            'tensor_active_pct': self.tensor_active_pct,
            'dram_active_pct': self.dram_active_pct,
            'gr_engine_active_pct': self.gr_engine_active_pct,
            'fp64_active_pct': self.fp64_active_pct,
        }


@dataclass
class GPUHealthRecord:
    """GPU hardware health metrics from DCGM."""
    timestamp: str
    node: str
    gpu_id: int
    pcie_replay_count: int = 0
    pcie_replay_rate_per_sec: float = 0.0
    ecc_correctable_total: int = 0
    ecc_uncorrectable_total: int = 0
    rows_remapped_correctable: int = 0
    rows_remapped_uncorrectable: int = 0
    rows_remapped_pending: int = 0
    row_remap_failure: int = 0
    health_status: str = "OK"


def _get_temp_threshold(gpu_name: str) -> int:
    """Return temperature warning threshold for a GPU model."""
    name_upper = gpu_name.upper()
    for key, thresh in GPU_TEMP_WARN.items():
        if key.upper() in name_upper:
            return thresh
    return GPU_TEMP_WARN["DEFAULT"]


def _compute_real_util(
    sm: float,
    tensor: float,
    dram: float,
    gr: float,
    weights: list[float],
) -> float:
    """
    Compute Real Utilization as a weighted composite of DCGM pipeline counters.

    Adapted from KempnerPulse (Kempner Institute, MIT license).
    Weights are auto-normalized to sum to 1.
    """
    total = sum(weights)
    if total <= 0:
        return 0.0
    w_sm, w_tensor, w_dram, w_gr = [w / total for w in weights]
    raw = w_sm * sm + w_tensor * tensor + w_dram * dram + w_gr * gr
    return max(0.0, min(100.0, raw))


def _classify_workload(
    sm: float,
    tensor: float,
    dram: float,
    gr: float,
    fp64: float,
    real_util: float,
    memcpy_active: bool = False,
    pcie_gbps: float = 0.0,
) -> str:
    """
    Classify GPU workload into one of 12 categories.

    Categories evaluated in priority order; first match wins.
    Adapted from KempnerPulse (Kempner Institute, MIT license)
    and NVIDIA DCGM profiling metric guidance.
    """
    if real_util < 5 and gr < 5 and dram < 5:
        return "idle"
    if tensor >= 50 and sm >= 60:
        return "tensor-heavy compute"
    if tensor >= 15 and sm >= 40:
        return "tensor compute"
    if fp64 >= 20 and sm >= 50:
        return "FP64 / HPC compute"
    if (memcpy_active and pcie_gbps >= 1.0) and sm < 30:
        return "I/O or data-loading"
    if dram >= 50 and sm < 50:
        return "memory-bound"
    if sm >= 80:
        return "compute-heavy"
    if sm >= 50:
        return "compute-active"
    if dram >= 40:
        return "memory-active"
    if gr >= 40 and sm < 25:
        return "busy, low SM use"
    if gr < 15 and sm < 15 and dram < 15:
        return "low utilization"
    return "mixed / moderate"


def _determine_health_status(
    pcie_replay_rate: float,
    row_remap_failure: int,
    ecc_uncorrectable: int,
    temperature_c: int,
    gpu_name: str,
) -> str:
    """Return health status string: OK, WARN, HOT, or CRIT."""
    if row_remap_failure > 0 or ecc_uncorrectable > 0:
        return "CRIT"
    temp_thresh = _get_temp_threshold(gpu_name)
    if temperature_c >= temp_thresh:
        return "HOT"
    if pcie_replay_rate > 0:
        return "WARN"
    return "OK"


@registry.register
class GPUCollector(BaseCollector):
    """
    Collector for NVIDIA GPU statistics.

    Uses DCGM opportunistically when available (dcgmi dmon or dcgm-exporter).
    Falls back to nvidia-smi when DCGM is not present. Both paths work
    transparently; data_source column records which was used per record.

    SSH mode: when gpu_nodes is set in config, nvidia-smi/dcgmi is run
    on remote nodes via SSH.
    """

    name = "gpu"
    description = "NVIDIA GPU statistics (nvidia-smi or DCGM)"
    default_interval = 60

    def __init__(self, config: dict[str, Any], db_path: str):
        super().__init__(config, db_path)

        self._gpu_available: bool | None = None
        self._dcgm_mode: str | None = None   # 'dcgmi', 'exporter', or None
        self._dcgm_checked: bool = False
        self._prev_pcie_replay: dict[tuple[str, int], int] = {}  # (node, gpu_id) -> count

        gpu_config = self.config
        self._gpu_nodes: list[str] = gpu_config.get('gpu_nodes', [])
        self._ssh_user: str = gpu_config.get('ssh_user', os.getenv('USER', ''))

        # Real Util weights — preset name or custom list
        weight_config = gpu_config.get('real_util_weights', 'ai')
        if isinstance(weight_config, str):
            self._real_util_weights = REAL_UTIL_PRESETS.get(weight_config, REAL_UTIL_PRESETS['ai'])
        elif isinstance(weight_config, list) and len(weight_config) == 4:
            self._real_util_weights = weight_config
        else:
            self._real_util_weights = REAL_UTIL_PRESETS['ai']

        if self._gpu_nodes:
            logger.info(f"GPUCollector SSH mode: {len(self._gpu_nodes)} nodes configured")

    # ------------------------------------------------------------------
    # SSH helpers
    # ------------------------------------------------------------------

    def _run_command_ssh(self, cmd: str, host: str) -> str | None:
        """Run a command on a remote host via SSH."""
        ssh_cmd = [
            'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'StrictHostKeyChecking=no',
            f'{self._ssh_user}@{host}', cmd,
        ]
        try:
            result = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
            if result.returncode != 0:
                logger.debug(f"SSH to {host} failed (rc={result.returncode}): {result.stderr.strip()}")
            return None
        except subprocess.TimeoutExpired:
            logger.warning(f"SSH to {host} timed out")
            return None
        except Exception as e:
            logger.debug(f"SSH to {host} error: {e}")
            return None

    def _run_local(self, cmd: list[str]) -> str | None:
        """Run a command locally and return stdout, or None on failure."""
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.debug(f"Local command failed: {e}")
        return None

    def _run(self, cmd: str | list[str], host: str | None = None) -> str | None:
        """Run a command locally or on a remote host."""
        if host:
            return self._run_command_ssh(cmd if isinstance(cmd, str) else ' '.join(cmd), host)
        return self._run_local(cmd if isinstance(cmd, list) else cmd.split())

    # ------------------------------------------------------------------
    # DCGM detection
    # ------------------------------------------------------------------

    def _detect_dcgm(self, host: str | None = None) -> str | None:
        """
        Detect DCGM availability on a node.

        Returns 'dcgmi' if dcgmi dmon is available, None otherwise.
        dcgm-exporter Prometheus endpoint support can be added in a
        future iteration.
        """
        # Try dcgmi dmon with a 1-sample, 1-second poll to verify it works
        probe = 'dcgmi dmon -e 1001,1002,1004,1005 -c 1 2>/dev/null | head -5'
        output = self._run(probe, host)
        if output and 'dcgm' not in output.lower().replace('dcgmi', ''):
            # dcgmi returned data (not just an error containing "dcgm")
            return 'dcgmi'
        # Simpler existence check
        check = 'which dcgmi 2>/dev/null'
        if self._run(check, host):
            return 'dcgmi'
        return None

    def _check_dcgm_available(self, host: str | None = None) -> bool:
        """Check and cache DCGM availability for a node."""
        if not self._dcgm_checked:
            self._dcgm_mode = self._detect_dcgm(host)
            self._dcgm_checked = True
            if self._dcgm_mode:
                logger.info(f"DCGM available via {self._dcgm_mode} — using enhanced metrics")
            else:
                logger.info("DCGM not available — using nvidia-smi (standard metrics)")
        return self._dcgm_mode is not None

    # ------------------------------------------------------------------
    # GPU availability check
    # ------------------------------------------------------------------

    def _get_nvidia_smi_cmd(self) -> str:
        nvidia_path = self.config.get('nvidia_smi_path', 'nvidia-smi')
        return (
            f"{nvidia_path} --query-gpu="
            "index,name,utilization.gpu,utilization.memory,"
            "memory.used,memory.total,memory.free,"
            "temperature.gpu,power.draw,power.limit "
            "--format=csv,noheader,nounits"
        )

    def _check_gpu_available(self) -> bool:
        if self._gpu_available is not None:
            return self._gpu_available

        if self._gpu_nodes:
            cmd = self._get_nvidia_smi_cmd()
            for node in self._gpu_nodes:
                if self._run(cmd, node):
                    self._gpu_available = True
                    logger.info(f"GPU SSH mode verified via {node}")
                    return True
            self._gpu_available = False
            logger.warning(f"GPU SSH mode: none of {self._gpu_nodes} responded")
            return False

        output = self._run(['nvidia-smi', '--query-gpu=count', '--format=csv,noheader'])
        self._gpu_available = output is not None
        if not self._gpu_available:
            logger.info("No NVIDIA GPUs detected — GPU collector will be skipped")
        return self._gpu_available

    # ------------------------------------------------------------------
    # DCGM collection
    # ------------------------------------------------------------------

    def _collect_dcgm(self, host: str | None = None) -> dict[int, DCGMStats]:
        """
        Collect DCGM profiling counters for all GPUs on a node.

        Returns a mapping of gpu_index -> DCGMStats.
        Uses dcgmi dmon with a single sample (-c 1).
        """
        # Field IDs per dcgmi profile --list (verified on RTX 6000 Ada):
        # GR_ENGINE=1001, SM_ACTIVE=1002, TENSOR=1004, DRAM=1005, FP32=1007
        # FP64=1006 not available on all GPUs (absent on RTX 6000 Ada)
        field_ids = "1001,1002,1004,1005,1007"
        cmd = f"dcgmi dmon -e {field_ids} -c 1 -d 1000 2>/dev/null"
        output = self._run(cmd, host)
        if not output:
            return {}

        results: dict[int, DCGMStats] = {}
        for line in output.strip().split('\n'):
            line = line.strip()
            # Skip header/separator lines
            if not line or line.startswith('#') or line.startswith('-') or line.startswith('Id'):
                continue
            parts = line.split()
            # Expected: gpu_id gr_engine sm_active tensor dram fp32
            # Field order matches: 1001,1002,1004,1005,1007
            if len(parts) < 6:
                continue
            try:
                # Handle both "N ..." and "GPU N ..." formats
                if parts[0].upper() == 'GPU':
                    gpu_id = int(parts[1])
                    vals = parts[2:]
                else:
                    gpu_id = int(parts[0])
                    vals = parts[1:]
                if len(vals) < 4:
                    continue
                results[gpu_id] = DCGMStats(
                    gpu_index=gpu_id,
                    gr_engine_active_pct=float(vals[0]) * 100,
                    sm_active_pct=float(vals[1]) * 100,
                    tensor_active_pct=float(vals[2]) * 100,
                    dram_active_pct=float(vals[3]) * 100,
                    fp64_active_pct=0.0,  # fp32_active (1007); fp64 not universal
                )
            except (ValueError, IndexError):
                continue
        return results

    # ------------------------------------------------------------------
    # Hardware health collection
    # ------------------------------------------------------------------

    def _collect_health(self, host: str, gpu_records: list[dict]) -> list[GPUHealthRecord]:
        """
        Collect hardware health metrics via DCGM for a node.

        Returns a list of GPUHealthRecord for each GPU.
        """
        if not self._check_dcgm_available(host):
            return []

        timestamp = datetime.now().isoformat()
        health_records = []

        # Row remap status via dcgmi health or diag
        # Use dcgmi dmon for ECC and PCIe replay counters
        # Fields: ECC_SBE=312, ECC_DBE=313, PCIE_REPLAY=319
        field_ids = "312,313,319"
        cmd = f"dcgmi dmon -e {field_ids} -c 1 -d 1000 2>/dev/null"
        output = self._run(cmd, host)

        # Parse existing gpu_records to find GPU names and temperatures
        gpu_names: dict[int, str] = {r['gpu_index']: r.get('gpu_name', '') for r in gpu_records}
        gpu_temps: dict[int, int] = {r['gpu_index']: r.get('temperature_c', 0) for r in gpu_records}

        if output:
            for line in output.strip().split('\n'):
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('-') or line.startswith('Id'):
                    continue
                parts = line.split()
                if len(parts) < 4:
                    continue
                try:
                    gpu_id = int(parts[0])
                    ecc_correctable = int(float(parts[1])) if parts[1] not in ('N/A', 'Unknown') else 0
                    ecc_uncorrectable = int(float(parts[2])) if parts[2] not in ('N/A', 'Unknown') else 0
                    pcie_replay_count = int(float(parts[3])) if parts[3] not in ('N/A', 'Unknown') else 0

                    # Compute PCIe replay rate from previous sample
                    key = (host, gpu_id)
                    prev = self._prev_pcie_replay.get(key, pcie_replay_count)
                    pcie_rate = max(0.0, float(pcie_replay_count - prev)) / 60.0  # per-second at 60s interval
                    self._prev_pcie_replay[key] = pcie_replay_count

                    gpu_name = gpu_names.get(gpu_id, '')
                    temp = gpu_temps.get(gpu_id, 0)

                    health = _determine_health_status(
                        pcie_rate, 0, ecc_uncorrectable, temp, gpu_name
                    )

                    health_records.append(GPUHealthRecord(
                        timestamp=timestamp,
                        node=host,
                        gpu_id=gpu_id,
                        pcie_replay_count=pcie_replay_count,
                        pcie_replay_rate_per_sec=pcie_rate,
                        ecc_correctable_total=ecc_correctable,
                        ecc_uncorrectable_total=ecc_uncorrectable,
                        health_status=health,
                    ))
                except (ValueError, IndexError):
                    continue

        return health_records

    # ------------------------------------------------------------------
    # SLURM/CUDA GPU visibility filter
    # ------------------------------------------------------------------

    @staticmethod
    def visible_gpu_indices() -> list[int] | None:
        """
        Return the set of GPU indices visible to the current job/user.

        Detection priority (matching KempnerPulse):
          1. CUDA_VISIBLE_DEVICES
          2. NVIDIA_VISIBLE_DEVICES
          3. SLURM_STEP_GPUS
          4. SLURM_JOB_GPUS
          5. None (show all — admin/collect mode)
        """
        for env_var in ('CUDA_VISIBLE_DEVICES', 'NVIDIA_VISIBLE_DEVICES',
                        'SLURM_STEP_GPUS', 'SLURM_JOB_GPUS'):
            val = os.environ.get(env_var, '').strip()
            if val and val not in ('NoDevFiles', 'all', ''):
                try:
                    return [int(x) for x in val.split(',') if x.strip().isdigit()]
                except ValueError:
                    continue
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_nvidia_output(self, output: str, hostname: str = '') -> list[dict[str, Any]]:
        """Parse nvidia-smi CSV output into records."""
        records = []
        timestamp = datetime.now()

        for line in output.strip().split('\n'):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) < 10:
                continue
            try:
                def safe_float(v: str, default: float = 0.0) -> float:
                    return float(v) if v not in ('[N/A]', 'N/A', '') else default

                stats = GPUStats(
                    gpu_index=int(parts[0]),
                    gpu_name=parts[1],
                    gpu_util_percent=safe_float(parts[2]),
                    memory_util_percent=safe_float(parts[3]),
                    memory_used_mb=int(safe_float(parts[4])),
                    memory_total_mb=int(safe_float(parts[5])),
                    memory_free_mb=int(safe_float(parts[6])),
                    temperature_c=int(safe_float(parts[7])),
                    power_draw_w=safe_float(parts[8]),
                    power_limit_w=safe_float(parts[9]),
                    compute_processes=0,
                )
                records.append({
                    'type': 'gpu',
                    'timestamp': timestamp.isoformat(),
                    'node_name': hostname,
                    'data_source': 'nvidia-smi',
                    **stats.to_dict(),
                })
            except (ValueError, IndexError) as e:
                logger.debug(f"Failed to parse GPU line: {e}")

        return records

    def _enrich_with_dcgm(
        self,
        records: list[dict[str, Any]],
        dcgm_data: dict[int, DCGMStats],
    ) -> list[dict[str, Any]]:
        """
        Merge DCGM profiling counters into existing nvidia-smi records.
        Computes Real Util and workload classification in-place.
        """
        for record in records:
            gpu_id = record['gpu_index']
            dcgm = dcgm_data.get(gpu_id)
            if dcgm:
                record.update(dcgm.to_dict())
                record['data_source'] = 'dcgm'
                record['real_util_pct'] = _compute_real_util(
                    dcgm.sm_active_pct,
                    dcgm.tensor_active_pct,
                    dcgm.dram_active_pct,
                    dcgm.gr_engine_active_pct,
                    self._real_util_weights,
                )
                record['workload_class'] = _classify_workload(
                    sm=dcgm.sm_active_pct,
                    tensor=dcgm.tensor_active_pct,
                    dram=dcgm.dram_active_pct,
                    gr=dcgm.gr_engine_active_pct,
                    fp64=dcgm.fp64_active_pct,
                    real_util=record['real_util_pct'],
                )
            else:
                # nvidia-smi only — set DCGM fields to None
                record.setdefault('sm_active_pct', None)
                record.setdefault('tensor_active_pct', None)
                record.setdefault('dram_active_pct', None)
                record.setdefault('gr_engine_active_pct', None)
                record.setdefault('fp64_active_pct', None)
                record.setdefault('real_util_pct', None)
                record.setdefault('workload_class', None)
        return records

    # ------------------------------------------------------------------
    # Main collect()
    # ------------------------------------------------------------------

    def collect(self) -> list[dict[str, Any]]:
        """Collect GPU statistics (DCGM if available, nvidia-smi otherwise)."""
        if not self._check_gpu_available():
            return []

        all_records: list[dict[str, Any]] = []
        all_health: list[GPUHealthRecord] = []

        nodes = self._gpu_nodes if self._gpu_nodes else [None]  # None = local

        for node in nodes:
            hostname = node if node else socket.gethostname().split('.')[0]
            cmd = self._get_nvidia_smi_cmd()

            # Collect baseline nvidia-smi metrics
            if node:
                output = self._run(cmd, node)
            else:
                output = self._run(cmd.split())

            if not output:
                if node:
                    logger.warning(f"Failed to collect GPU data from {node}")
                continue

            records = self._parse_nvidia_output(output, hostname=hostname)

            # Opportunistically enrich with DCGM if available
            if self._check_dcgm_available(node):
                dcgm_data = self._collect_dcgm(node)
                if dcgm_data:
                    records = self._enrich_with_dcgm(records, dcgm_data)
                    health = self._collect_health(hostname, records)
                    all_health.extend(health)
            else:
                records = self._enrich_with_dcgm(records, {})  # sets None fields

            all_records.extend(records)
            logger.debug(f"Collected {len(records)} GPU records from {hostname}")

        # Attach health records to return payload
        for hr in all_health:
            all_records.append({'type': 'gpu_health', **hr.__dict__})

        return all_records

    # ------------------------------------------------------------------
    # Store
    # ------------------------------------------------------------------

    def store(self, data: list[dict[str, Any]]) -> None:
        """Store GPU metrics and health records."""
        if not data:
            return

        with self.get_db_connection() as conn:
            self._ensure_schema(conn)

            gpu_records = [r for r in data if r.get('type') == 'gpu']
            health_records = [r for r in data if r.get('type') == 'gpu_health']

            for record in gpu_records:
                conn.execute(
                    """
                    INSERT INTO gpu_stats (
                        timestamp, node_name, gpu_index, gpu_name,
                        gpu_util_percent, memory_util_percent,
                        memory_used_mb, memory_total_mb, memory_free_mb,
                        temperature_c, power_draw_w, power_limit_w, compute_processes,
                        sm_active_pct, tensor_active_pct, dram_active_pct,
                        gr_engine_active_pct, fp64_active_pct,
                        real_util_pct, workload_class, data_source
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record['timestamp'],
                        record.get('node_name', ''),
                        record['gpu_index'],
                        record['gpu_name'],
                        record['gpu_util_percent'],
                        record['memory_util_percent'],
                        record['memory_used_mb'],
                        record['memory_total_mb'],
                        record['memory_free_mb'],
                        record['temperature_c'],
                        record['power_draw_w'],
                        record['power_limit_w'],
                        record['compute_processes'],
                        record.get('sm_active_pct'),
                        record.get('tensor_active_pct'),
                        record.get('dram_active_pct'),
                        record.get('gr_engine_active_pct'),
                        record.get('fp64_active_pct'),
                        record.get('real_util_pct'),
                        record.get('workload_class'),
                        record.get('data_source', 'nvidia-smi'),
                    )
                )

            for record in health_records:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO gpu_health (
                        timestamp, node, gpu_id,
                        pcie_replay_count, pcie_replay_rate_per_sec,
                        ecc_correctable_total, ecc_uncorrectable_total,
                        rows_remapped_correctable, rows_remapped_uncorrectable,
                        rows_remapped_pending, row_remap_failure, health_status
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        record['timestamp'],
                        record['node'],
                        record['gpu_id'],
                        record.get('pcie_replay_count', 0),
                        record.get('pcie_replay_rate_per_sec', 0.0),
                        record.get('ecc_correctable_total', 0),
                        record.get('ecc_uncorrectable_total', 0),
                        record.get('rows_remapped_correctable', 0),
                        record.get('rows_remapped_uncorrectable', 0),
                        record.get('rows_remapped_pending', 0),
                        record.get('row_remap_failure', 0),
                        record.get('health_status', 'OK'),
                    )
                )

            conn.commit()
            logger.debug(f"Stored {len(gpu_records)} GPU records, {len(health_records)} health records")

    def _ensure_schema(self, conn) -> None:
        """Create or migrate gpu_stats and gpu_health tables."""
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gpu_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                node_name TEXT,
                gpu_index INTEGER,
                gpu_name TEXT,
                gpu_util_percent REAL,
                memory_util_percent REAL,
                memory_used_mb INTEGER,
                memory_total_mb INTEGER,
                memory_free_mb INTEGER,
                temperature_c INTEGER,
                power_draw_w REAL,
                power_limit_w REAL,
                compute_processes INTEGER,
                sm_active_pct REAL,
                tensor_active_pct REAL,
                dram_active_pct REAL,
                gr_engine_active_pct REAL,
                fp64_active_pct REAL,
                real_util_pct REAL,
                workload_class TEXT,
                data_source TEXT DEFAULT 'nvidia-smi'
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_gpu_stats_ts
            ON gpu_stats(timestamp)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_gpu_stats_gpu
            ON gpu_stats(gpu_index, timestamp)
        """)

        # Add new columns to existing databases (safe no-ops if columns exist)
        new_columns = [
            ("sm_active_pct",          "REAL"),
            ("tensor_active_pct",      "REAL"),
            ("dram_active_pct",        "REAL"),
            ("gr_engine_active_pct",   "REAL"),
            ("fp64_active_pct",        "REAL"),
            ("real_util_pct",          "REAL"),
            ("workload_class",         "TEXT"),
            ("data_source",            "TEXT DEFAULT 'nvidia-smi'"),
            ("node_name",              "TEXT"),
        ]
        for col, col_type in new_columns:
            try:
                conn.execute(f"ALTER TABLE gpu_stats ADD COLUMN {col} {col_type}")
            except Exception:
                pass  # Column already exists

        conn.execute("""
            CREATE TABLE IF NOT EXISTS gpu_health (
                timestamp DATETIME NOT NULL,
                node TEXT NOT NULL,
                gpu_id INTEGER NOT NULL,
                pcie_replay_count INTEGER DEFAULT 0,
                pcie_replay_rate_per_sec REAL DEFAULT 0,
                ecc_correctable_total INTEGER DEFAULT 0,
                ecc_uncorrectable_total INTEGER DEFAULT 0,
                rows_remapped_correctable INTEGER DEFAULT 0,
                rows_remapped_uncorrectable INTEGER DEFAULT 0,
                rows_remapped_pending INTEGER DEFAULT 0,
                row_remap_failure INTEGER DEFAULT 0,
                health_status TEXT DEFAULT 'OK',
                PRIMARY KEY (timestamp, node, gpu_id)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_gpu_health_node_ts
            ON gpu_health(node, timestamp)
        """)

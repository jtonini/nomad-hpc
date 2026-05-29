# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Energy consumption computation for the NØMAD energy module.

Computes per-job and cluster-level electrical energy and converts it to CO2
equivalent. Energy is real where the data is real (DCGM power_draw_w from
gpu_stats) and estimated where it is not (CPU power, GPU jobs without DCGM
coverage). Waste decomposition lives in waste.py; carbon in carbon.py.

Energy chain:
    watts x hours  = watt-hours (Wh)
    Wh / 1000      = kilowatt-hours (kWh)
    kWh x gCO2/kWh = grams CO2 equivalent      (see carbon.py)

Real schema this reads (verified against the demo database):
    jobs(job_id, cluster, partition, node_list, req_cpus, req_gpus,
         req_mem_mb, req_time_seconds, runtime_seconds, end_time, state)
    job_accounting(job_id, cluster, alloc_cpus, gpu_count, elapsed_sec)
    gpu_stats(timestamp, node_name, gpu_index, gpu_name, power_draw_w,
              real_util_pct, workload_class, data_source)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .carbon import CarbonIntensity, resolve_intensity
from .waste import JobEnergyRow, WasteBreakdown, compute_waste, MODE_PHYSICAL


# ── Hardware power references (estimate fallback / allocated valuation) ────
GPU_TDP_WATTS: dict[str, float] = {
    "H100": 700.0, "A100": 400.0, "A40": 300.0,
    "RTX 6000 ADA": 300.0, "RTX 6000": 300.0, "V100": 300.0,
    "L40": 300.0, "T4": 70.0,
}
DEFAULT_GPU_TDP_WATTS = 350.0

IDLE_REAL_UTIL_PCT = 5.0
IDLE_WORKLOAD_CLASSES = {"idle", "low utilization", "low-utilization"}


def gpu_tdp(gpu_name: str | None) -> float:
    """Look up GPU TDP by model-name substring; default if unknown."""
    if not gpu_name:
        return DEFAULT_GPU_TDP_WATTS
    name = gpu_name.upper()
    for key, watts in GPU_TDP_WATTS.items():
        if key in name:
            return watts
    return DEFAULT_GPU_TDP_WATTS


# ── Aggregate result ──────────────────────────────────────────────────────
@dataclass
class EnergySnapshot:
    """Aggregate energy + waste for a time window."""
    window_start: datetime
    window_end: datetime

    consumed_wh: float = 0.0
    allocated_wh: float = 0.0
    waste: WasteBreakdown = field(default_factory=WasteBreakdown)

    gpu_consumed_wh: float = 0.0
    cpu_consumed_wh: float = 0.0
    overhead_wh: float = 0.0

    n_jobs: int = 0
    n_gpu_jobs: int = 0
    dcgm_present: bool = False

    intensity: CarbonIntensity | None = None

    @property
    def consumed_kwh(self) -> float:
        return self.consumed_wh / 1000.0

    @property
    def wasted_kwh(self) -> float:
        return self.waste.total_wh / 1000.0

    @property
    def efficiency_pct(self) -> float:
        """Share of consumed-plus-recoverable energy that was productive.

        Efficiency is measured against consumed + recoverable waste, so it
        cannot exceed 100% even when waste is valued by allocation.
        """
        denom = self.consumed_wh + self.waste.total_wh
        if denom <= 0:
            return 0.0
        return 100.0 * (1.0 - self.waste.total_wh / denom)

    def grams_co2(self) -> float:
        return self.intensity.grams_co2(self.consumed_kwh) if self.intensity else 0.0

    def wasted_grams_co2(self) -> float:
        return self.intensity.grams_co2(self.wasted_kwh) if self.intensity else 0.0


# ── Window helpers ──────────────────────────────────────────────────────
def _reference_now(conn: sqlite3.Connection) -> datetime:
    """Reference 'now' = latest job end_time when present, else wall clock.

    Lets the module behave identically on live data and a static demo db.
    """
    row = conn.execute("SELECT MAX(end_time) FROM jobs").fetchone()
    if row and row[0]:
        dt = _parse_dt(row[0])
        if dt:
            return dt
    return datetime.now()


def _window(conn: sqlite3.Connection, hours: int) -> tuple[datetime, datetime]:
    end = _reference_now(conn)
    return end - timedelta(hours=hours), end


# ── Per-job energy ──────────────────────────────────────────────────────
def iter_job_energy(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    config: dict,
    cluster_name: str | None = None,
) -> tuple[list[JobEnergyRow], float]:
    """Build per-job energy rows for jobs ending in the window.

    Returns (rows, cpu_consumed_wh). GPU per-job attribution from DCGM is not
    yet wired (needs time-correlated samples), so gpu_actual_wh is 0 here and
    real GPU energy/idle is taken cluster-wide via gpu_energy_from_dcgm.
    """
    energy_cfg = (config or {}).get("energy", {}) or {}
    cpu_tdp_watts = float(energy_cfg.get("cpu_tdp_watts", 150.0))
    cores_per_socket = float(energy_cfg.get("cores_per_socket", 32.0))
    cpu_watts_per_core = cpu_tdp_watts / cores_per_socket
    cpu_assumed_util = float(energy_cfg.get("cpu_assumed_util", 0.4))

    clause, params = "", []
    if cluster_name:
        clause, params = "WHERE j.cluster = ?", [cluster_name]
    raw = conn.execute(
        f"""SELECT j.job_id, j.partition, j.node_list, j.req_cpus, j.req_gpus,
                   j.req_time_seconds, j.runtime_seconds, j.end_time,
                   j.user_name, ja.account
            FROM jobs j
            LEFT JOIN job_accounting ja
              ON j.job_id = ja.job_id AND j.cluster = ja.cluster
            {clause}""",
        params,
    ).fetchall()

    rows: list[JobEnergyRow] = []
    cpu_consumed_wh = 0.0
    for j in raw:
        end_t = _parse_dt(j["end_time"])
        if end_t is None or not (start <= end_t <= end):
            continue
        req_cpus = j["req_cpus"] or 0
        req_gpus = j["req_gpus"] or 0
        run_h = (j["runtime_seconds"] or 0) / 3600.0
        req_time_h = (j["req_time_seconds"] or 0) / 3600.0

        gpu_full_w = req_gpus * _job_gpu_tdp(conn, j["node_list"])
        cpu_full_w = req_cpus * cpu_watts_per_core
        cpu_actual_wh = cpu_full_w * cpu_assumed_util * run_h
        cpu_consumed_wh += cpu_actual_wh

        rows.append(JobEnergyRow(
            job_id=str(j["job_id"]), partition=j["partition"] or "",
            req_time_h=req_time_h, run_h=run_h, alloc_cpus=req_cpus,
            gpu_full_w=gpu_full_w, cpu_full_w=cpu_full_w,
            gpu_actual_wh=0.0, cpu_actual_wh=cpu_actual_wh,
            user=j["user_name"] or "", account=j["account"] or "",
        ))
    return rows, cpu_consumed_wh


# ── GPU energy from DCGM samples ──────────────────────────────────────────
def gpu_energy_from_dcgm(
    conn: sqlite3.Connection, start: datetime, end: datetime
) -> tuple[float, float, bool]:
    """Integrate real GPU power over the window.

    Returns (total_gpu_wh, idle_gpu_wh, dcgm_present). Energy = sum over
    samples of power_draw_w x median inter-sample interval (hours).
    """
    rows = conn.execute(
        """SELECT node_name, gpu_index, timestamp, power_draw_w,
                  real_util_pct, workload_class
           FROM gpu_stats
           WHERE timestamp BETWEEN ? AND ? AND power_draw_w IS NOT NULL
           ORDER BY node_name, gpu_index, timestamp""",
        (start.isoformat(sep=" "), end.isoformat(sep=" ")),
    ).fetchall()
    if not rows:
        rows = conn.execute(
            """SELECT node_name, gpu_index, timestamp, power_draw_w,
                      real_util_pct, workload_class
               FROM gpu_stats WHERE power_draw_w IS NOT NULL
               ORDER BY node_name, gpu_index, timestamp"""
        ).fetchall()
    if not rows:
        return 0.0, 0.0, False

    interval_h = _median_interval_hours(rows)
    total_wh = idle_wh = 0.0
    for r in rows:
        wh = (r["power_draw_w"] or 0.0) * interval_h
        total_wh += wh
        wc = (r["workload_class"] or "").lower()
        if wc in IDLE_WORKLOAD_CLASSES or (r["real_util_pct"] or 0.0) < IDLE_REAL_UTIL_PCT:
            idle_wh += wh
    return total_wh, idle_wh, True


def _median_interval_hours(rows, default_sec: float = 60.0) -> float:
    deltas: list[float] = []
    prev_key = prev_ts = None
    for r in rows:
        key = (r["node_name"], r["gpu_index"])
        ts = _parse_dt(r["timestamp"])
        if ts and prev_ts and key == prev_key:
            d = (ts - prev_ts).total_seconds()
            if 0 < d < 24 * 3600:
                deltas.append(d)
        prev_key, prev_ts = key, ts
    if not deltas:
        return default_sec / 3600.0
    deltas.sort()
    return deltas[len(deltas) // 2] / 3600.0


def _job_gpu_tdp(conn: sqlite3.Connection, node_list: str | None) -> float:
    if not node_list:
        return DEFAULT_GPU_TDP_WATTS
    first = node_list.split(",")[0].strip()
    row = conn.execute(
        "SELECT gpu_name FROM gpu_stats WHERE node_name = ? LIMIT 1", (first,)
    ).fetchone()
    return gpu_tdp(row["gpu_name"] if row else None)


def _parse_dt(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


# ── Orchestration ──────────────────────────────────────────────────────
def aggregate(
    rows: list[JobEnergyRow],
    cpu_consumed_wh: float,
    gpu_total_wh: float,
    gpu_idle_wh: float,
    dcgm_present: bool,
    config: dict,
    window: tuple[datetime, datetime],
    mode: str = MODE_PHYSICAL,
    region_override: str | None = None,
) -> EnergySnapshot:
    """Build an EnergySnapshot from a (sub)set of job rows.

    Reused for the whole cluster and for per-partition / per-group / per-user
    breakdowns: the engine partitions `rows` by key and calls this per subset.
    GPU energy is passed in (cluster-level from DCGM) because per-job GPU
    attribution is not yet wired; subsets receive 0 until it is.
    """
    energy_cfg = (config or {}).get("energy", {}) or {}
    overhead_factor = float(energy_cfg.get("overhead_factor", 1.3))

    snap = EnergySnapshot(window_start=window[0], window_end=window[1])
    snap.n_jobs = len(rows)
    snap.n_gpu_jobs = sum(1 for r in rows if r.gpu_full_w > 0)
    snap.dcgm_present = dcgm_present
    snap.gpu_consumed_wh = gpu_total_wh
    snap.cpu_consumed_wh = cpu_consumed_wh
    snap.allocated_wh = sum(r.full_w * r.req_time_h for r in rows)

    base_wh = gpu_total_wh + cpu_consumed_wh
    snap.overhead_wh = base_wh * (overhead_factor - 1.0)
    snap.consumed_wh = base_wh * overhead_factor

    snap.waste = compute_waste(rows, gpu_idle_wh, dcgm_present, config, mode=mode)
    snap.intensity = resolve_intensity(config, region_override)
    return snap


def compute_energy(
    db_path: str,
    config: dict,
    hours: int = 168,
    cluster_name: str | None = None,
    region_override: str | None = None,
    mode: str = MODE_PHYSICAL,
) -> EnergySnapshot:
    """Compute aggregate energy and waste for the window under `mode`.

    Carbon and savings figures should use mode='physical' (the default).
    Use mode='allocation' for the capacity-reserved view.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        window = _window(conn, hours)
        rows, cpu_wh = iter_job_energy(conn, *window, config, cluster_name)
        gpu_total_wh, gpu_idle_wh, dcgm = gpu_energy_from_dcgm(conn, *window)
        return aggregate(rows, cpu_wh, gpu_total_wh, gpu_idle_wh, dcgm,
                         config, window, mode=mode, region_override=region_override)
    finally:
        conn.close()

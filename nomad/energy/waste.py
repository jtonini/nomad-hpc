# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Energy-waste decomposition for the NØMAD energy module.

Waste is split into three sources and valued under one of two modes. The
mode determines what "wasted energy" means, and the choice is consequential:

    physical    Energy physically dissipated that could have been avoided.
                This is the defensible basis for any carbon-savings claim:
                  - gpu_idle      : measured DCGM watts drawn while a GPU was
                                    not computing (real).
                  - cpu_underutil : idle draw of allocated-but-unused cores
                                    during the run (estimate).
                  - time_overest. : ZERO. A completed batch job releases its
                                    nodes at the end; the over-requested hours
                                    are never physically drawn. The behavioral
                                    signal is preserved as over_request_seconds
                                    (a scheduling/queue cost, not carbon).

    allocation  Energy capacity reserved but not put to productive use, valued
                at full TDP. Answers "how much of the cluster is tied up?" but
                overstates carbon: it counts power that was never drawn.

For the same workload, allocation waste can exceed total consumption (you
cannot waste more carbon than you emit) -- which is precisely why carbon and
savings figures must use the physical mode.

    total_waste = time_overestimation + gpu_idle + cpu_underutil   (Wh)
"""
from __future__ import annotations

from dataclasses import dataclass, field


# Mode constants
MODE_PHYSICAL = "physical"
MODE_ALLOCATION = "allocation"


@dataclass
class JobEnergyRow:
    """Per-job energy inputs the waste model needs.

    Built by power.iter_job_energy from the jobs/job_accounting join.
    All energy in watt-hours, power in watts, durations in hours.
    """
    job_id: str
    partition: str
    req_time_h: float          # requested wall time
    run_h: float               # actual runtime
    alloc_cpus: int
    gpu_full_w: float          # req_gpus x GPU TDP (allocated GPU power)
    cpu_full_w: float          # alloc_cpus x per-core TDP (allocated CPU power)
    gpu_actual_wh: float       # measured/estimated GPU energy consumed
    cpu_actual_wh: float       # estimated CPU energy consumed
    user: str = ""             # job owner (for per-user breakdown)
    account: str = ""          # account/group (for per-group breakdown)

    @property
    def full_w(self) -> float:
        return self.gpu_full_w + self.cpu_full_w


@dataclass
class Provenance:
    quality: str   # "real" | "estimate" | "partial" | "none"
    detail: str


@dataclass
class WasteBreakdown:
    """Energy waste decomposed by source (Wh), under a given valuation mode."""
    mode: str = MODE_PHYSICAL
    time_overestimation_wh: float = 0.0
    gpu_idle_wh: float = 0.0
    cpu_underutil_wh: float = 0.0
    total_wh: float = 0.0

    # Behavioral signal preserved independent of energy valuation: total
    # wall-time requested but not used. Drives scheduling/queue messaging.
    over_request_seconds: float = 0.0

    # Allocated-but-unused memory, tracked separately (not an energy figure
    # because per-job peak-used memory is not recorded in the schema).
    memory_overalloc_gb_hours: float = 0.0

    provenance: dict[str, Provenance] = field(default_factory=dict)


def compute_waste(
    rows: list[JobEnergyRow],
    gpu_idle_wh: float,
    dcgm_present: bool,
    config: dict,
    mode: str = MODE_PHYSICAL,
) -> WasteBreakdown:
    """Decompose energy waste across jobs under the chosen valuation mode.

    gpu_idle_wh is the cluster-level measured GPU idle energy (from DCGM),
    identical in both modes because it is physically observed.
    """
    energy_cfg = (config or {}).get("energy", {}) or {}
    cpu_assumed_util = float(energy_cfg.get("cpu_assumed_util", 0.4))
    cpu_idle_fraction = float(energy_cfg.get("cpu_idle_fraction", 0.3))

    wb = WasteBreakdown(mode=mode)
    wb.gpu_idle_wh = gpu_idle_wh

    time_wh = 0.0
    cpu_under_wh = 0.0

    for r in rows:
        over_h = max(0.0, r.req_time_h - r.run_h)
        wb.over_request_seconds += over_h * 3600.0

        if mode == MODE_ALLOCATION:
            # Capacity reserved but unused, valued at full TDP.
            time_wh += r.full_w * over_h
            # During the run, CPU drew less than full TDP -> the gap is waste.
            cpu_under_wh += max(0.0, r.cpu_full_w * r.run_h - r.cpu_actual_wh)
        else:  # MODE_PHYSICAL
            # Over-requested time is released at job end -> no energy drawn.
            time_wh += 0.0
            # Physical waste = idle draw of the cores that sat unused.
            idle_per_core_w = (r.cpu_full_w / r.alloc_cpus) * cpu_idle_fraction \
                if r.alloc_cpus else 0.0
            unused_cores = r.alloc_cpus * (1.0 - cpu_assumed_util)
            cpu_under_wh += max(0.0, unused_cores * idle_per_core_w * r.run_h)

    wb.time_overestimation_wh = time_wh
    wb.cpu_underutil_wh = cpu_under_wh
    wb.total_wh = wb.time_overestimation_wh + wb.gpu_idle_wh + wb.cpu_underutil_wh

    # Provenance, mode-dependent.
    wb.provenance["gpu_idle"] = (
        Provenance("real", "DCGM power_draw_w during idle/low-util samples")
        if dcgm_present else
        Provenance("estimate", "no DCGM coverage; GPU idle not measured")
    )
    if mode == MODE_ALLOCATION:
        wb.provenance["time_overestimation"] = Provenance(
            "real", "req_time - runtime (measured), valued at full TDP")
        wb.provenance["cpu_underutil"] = Provenance(
            "estimate", "full-TDP minus estimated actual CPU energy")
    else:
        wb.provenance["time_overestimation"] = Provenance(
            "none", "released at job end; reported as over_request_seconds, not energy")
        wb.provenance["cpu_underutil"] = Provenance(
            "estimate", "idle draw of allocated-but-unused cores")
    wb.provenance["memory_overalloc"] = Provenance(
        "partial", "req_mem allocated; per-job peak-used not recorded")
    return wb

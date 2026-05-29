# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAD energy module — energy consumption, waste, and carbon-footprint
monitoring for research computing.

Public entry points:
    EnergyEngine       orchestrator (engine.py): summary, report, user profile
    compute_energy     one-shot aggregate snapshot (power.py)
    resolve_intensity  carbon-intensity factor with provenance (carbon.py)

Valuation modes (waste.py):
    MODE_PHYSICAL      energy physically dissipated; basis for carbon claims
    MODE_ALLOCATION    capacity reserved-but-unused, valued at full TDP
"""
from .carbon import CarbonIntensity, resolve_intensity
from .waste import WasteBreakdown, compute_waste, JobEnergyRow, MODE_PHYSICAL, MODE_ALLOCATION
from .power import compute_energy, EnergySnapshot, aggregate
from .engine import EnergyEngine

__all__ = [
    "EnergyEngine", "compute_energy", "resolve_intensity",
    "CarbonIntensity", "EnergySnapshot", "aggregate",
    "WasteBreakdown", "compute_waste", "JobEnergyRow",
    "MODE_PHYSICAL", "MODE_ALLOCATION",
]

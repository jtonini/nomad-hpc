# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Carbon-intensity lookup for the NØMAD energy module.

Converts electrical energy (kWh) into CO2-equivalent emissions (grams)
using regional grid carbon-intensity factors. Two sources are supported:

    epa_egrid : US EPA eGRID subregion emission rates (gCO2/kWh).
    iea       : International Energy Agency country-level factors.

A manual override (carbon_intensity_g_per_kwh in config) bypasses the table.

Reference:
    EPA eGRID 2022 subregion output emission rates (CO2 equivalent).
    IEA Emissions Factors 2023, country electricity carbon intensity.

The same identical workload has a ~90x different carbon footprint between
the lowest-intensity grid (Norway hydro, ~8 g/kWh) and the highest in this
table (India coal-heavy, ~720 g/kWh). That contrast is itself a finding:
behavioral interventions matter most in carbon-intensive regions.
"""
from __future__ import annotations

from dataclasses import dataclass


# US EPA eGRID subregions (gCO2eq/kWh). Subset of common research-computing
# locations; extend as deployment sites are added.
EGRID_SUBREGIONS: dict[str, float] = {
    "SRVC": 380.0,   # SERC Virginia/Carolina  (University of Richmond)
    "MROE": 470.0,   # MRO East  (Iowa region, more coal)
    "CAMX": 220.0,   # WECC California  (more renewables)
    "RFCE": 320.0,   # RFC East  (Mid-Atlantic)
    "NEWE": 240.0,   # NPCC New England
    "ERCT": 410.0,   # ERCOT All  (Texas)
    "RMPA": 540.0,   # WECC Rockies
    "AZNM": 430.0,   # WECC Southwest
    "NWPP": 300.0,   # WECC Northwest
}

# IEA country-level factors (gCO2eq/kWh). For non-US institutions.
IEA_COUNTRIES: dict[str, float] = {
    "US": 370.0,
    "NO": 8.0,       # Norway, almost all hydro
    "IN": 720.0,     # India, coal-heavy, emerging HPC market
    "BR": 100.0,     # Brazil, hydro-dominant
    "FR": 55.0,      # France, nuclear
    "DE": 350.0,     # Germany
    "GB": 210.0,     # United Kingdom
    "CN": 580.0,     # China
}

# Fallback used only when no region resolves and no override is set.
DEFAULT_INTENSITY_G_PER_KWH = 400.0


@dataclass(frozen=True)
class CarbonIntensity:
    """A resolved carbon-intensity factor with provenance.

    Provenance fields feed `nomad energy --explain` (Idea 15): the user can
    see exactly which source and region produced the gCO2/kWh figure.
    """
    g_per_kwh: float
    source: str       # "epa_egrid" | "iea" | "manual" | "default"
    region: str       # subregion / country code / "override" / "unknown"

    def grams_co2(self, kwh: float) -> float:
        """Convert kWh -> grams CO2eq."""
        return kwh * self.g_per_kwh

    def explain(self) -> str:
        """One-line provenance string for --explain output."""
        return (
            f"carbon intensity = {self.g_per_kwh:.0f} gCO2/kWh "
            f"(source: {self.source}, region: {self.region})"
        )


def resolve_intensity(config: dict, region_override: str | None = None) -> CarbonIntensity:
    """Resolve the carbon-intensity factor from config and optional override.

    Resolution order:
        1. region_override (CLI --region) against the configured source table.
        2. manual carbon_intensity_g_per_kwh in [energy] config.
        3. configured carbon_source + carbon_region lookup.
        4. default fallback.

    The config block (nomad.toml):

        [energy]
        carbon_source = "epa_egrid"   # epa_egrid | iea | manual
        carbon_region = "SRVC"        # eGRID subregion or country code
        # carbon_intensity_g_per_kwh = 380   # manual override
    """
    energy_cfg = (config or {}).get("energy", {}) or {}
    source = energy_cfg.get("carbon_source", "epa_egrid")
    region = region_override or energy_cfg.get("carbon_region")

    # 2. explicit manual override takes precedence when no region was forced.
    manual = energy_cfg.get("carbon_intensity_g_per_kwh")
    if region_override is None and (source == "manual" or manual is not None):
        if manual is not None:
            return CarbonIntensity(float(manual), "manual", "override")

    # 1 & 3. table lookup by source.
    if region:
        if source == "iea":
            val = IEA_COUNTRIES.get(region.upper())
            if val is not None:
                return CarbonIntensity(val, "iea", region.upper())
        else:  # epa_egrid (default)
            val = EGRID_SUBREGIONS.get(region.upper())
            if val is not None:
                return CarbonIntensity(val, "epa_egrid", region.upper())

    # 4. fallback.
    return CarbonIntensity(DEFAULT_INTENSITY_G_PER_KWH, "default", "unknown")

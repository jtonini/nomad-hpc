# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Output formatting for the NØMAD energy module.

Two design rules, both deliberate:

1. Non-punitive framing. Waste is presented as a recoverable *opportunity*,
   never as a personal failure. We say "matching requested time to actual
   runtime could save ~X kWh", not "you wasted X kWh". Improvements are
   framed as achievements. This mirrors the education module's principle and
   is what makes per-user energy feedback land as help rather than blame.

2. Provenance is visible. Real (measured) and estimated figures are labeled
   so a reader always knows which numbers are observations and which are
   models -- the CLI surface of Idea 15.

Real-world equivalences use US-average household electricity:
    ~30 kWh/day, ~10,500 kWh/year.
"""
from __future__ import annotations

import json

HOUSEHOLD_KWH_PER_DAY = 30.0
HOUSEHOLD_KWH_PER_YEAR = 10_500.0


# ── small helpers ──────────────────────────────────────────────────────
def _kwh(wh: float) -> str:
    return f"{wh / 1000:,.1f} kWh"


def _co2(grams: float) -> str:
    kg = grams / 1000.0
    if kg >= 1000:
        return f"{kg / 1000:,.2f} t CO2"
    return f"{kg:,.1f} kg CO2"


def equivalence(kwh: float) -> str:
    """A relatable comparison for an energy quantity."""
    if kwh <= 0:
        return "negligible"
    if kwh >= HOUSEHOLD_KWH_PER_YEAR:
        return f"~{kwh / HOUSEHOLD_KWH_PER_YEAR:.1f} years of a typical home's electricity"
    days = kwh / HOUSEHOLD_KWH_PER_DAY
    if days >= 1:
        return f"~{days:.0f} days of powering a typical home"
    return f"~{days * 24:.0f} hours of powering a typical home"


def _quality_tag(prov) -> str:
    return f"[{prov.quality}]" if prov else ""


# ── cluster summary ──────────────────────────────────────────────────────
def format_summary_cli(snap, cluster_name: str, mode: str, explain: bool = False) -> str:
    w = snap.waste
    intensity = snap.intensity
    lines = []
    lines.append(f"NØMAD Energy — {cluster_name or 'all clusters'}")
    lines.append(
        f"  window {snap.window_start:%Y-%m-%d} .. {snap.window_end:%Y-%m-%d}"
        f"   ·   valuation: {mode}"
    )
    lines.append("")
    lines.append(f"  Consumed        {_kwh(snap.consumed_wh):>14}"
                 f"   ({_co2(snap.grams_co2())})")
    lines.append(f"    GPU (real)    {_kwh(snap.gpu_consumed_wh):>14}")
    lines.append(f"    CPU (est.)    {_kwh(snap.cpu_consumed_wh):>14}")
    lines.append(f"    overhead      {_kwh(snap.overhead_wh):>14}   (PUE factor)")
    lines.append("")
    lines.append(f"  Efficiency      {snap.efficiency_pct:>12.1f}%"
                 "   (productive share of recoverable energy)")
    lines.append("")
    lines.append(f"  Opportunity     {_kwh(w.total_wh):>14}"
                 f"   ({_co2(snap.wasted_grams_co2())}) recoverable")
    lines.append(f"    GPU idle      {_kwh(w.gpu_idle_wh):>14}   "
                 f"{_quality_tag(w.provenance.get('gpu_idle'))}")
    lines.append(f"    CPU underutil {_kwh(w.cpu_underutil_wh):>14}   "
                 f"{_quality_tag(w.provenance.get('cpu_underutil'))}")
    lines.append(f"    time overest. {_kwh(w.time_overestimation_wh):>14}   "
                 f"{_quality_tag(w.provenance.get('time_overestimation'))}")
    lines.append(f"      ≈ {equivalence(w.total_wh / 1000)}")
    lines.append("")
    over_h = w.over_request_seconds / 3600.0
    lines.append(f"  Largest lever: {over_h:,.0f} hours of wall time were requested "
                 "beyond what jobs used.")
    lines.append("  Matching requested time to actual runtime is the simplest, "
                 "highest-impact change.")

    if explain and intensity is not None:
        lines.append("")
        lines.append("  Provenance:")
        lines.append(f"    {intensity.explain()}")
        lines.append("    energy chain: watts × hours → Wh → kWh → kWh × gCO2/kWh")
        for name, prov in w.provenance.items():
            lines.append(f"    {name}: {prov.quality} — {prov.detail}")
    return "\n".join(lines)


def format_summary_json(snap, cluster_name: str, mode: str) -> dict:
    w = snap.waste
    return {
        "cluster": cluster_name,
        "mode": mode,
        "window": {"start": snap.window_start.isoformat(),
                   "end": snap.window_end.isoformat()},
        "consumed_kwh": round(snap.consumed_kwh, 3),
        "consumed_breakdown_kwh": {
            "gpu": round(snap.gpu_consumed_wh / 1000, 3),
            "cpu": round(snap.cpu_consumed_wh / 1000, 3),
            "overhead": round(snap.overhead_wh / 1000, 3),
        },
        "efficiency_pct": round(snap.efficiency_pct, 1),
        "recoverable_kwh": round(w.total_wh / 1000, 3),
        "recoverable_breakdown_kwh": {
            "gpu_idle": round(w.gpu_idle_wh / 1000, 3),
            "cpu_underutil": round(w.cpu_underutil_wh / 1000, 3),
            "time_overestimation": round(w.time_overestimation_wh / 1000, 3),
        },
        "over_request_hours": round(w.over_request_seconds / 3600, 1),
        "carbon": {
            "consumed_kg": round(snap.grams_co2() / 1000, 2),
            "recoverable_kg": round(snap.wasted_grams_co2() / 1000, 2),
            "source": snap.intensity.source if snap.intensity else None,
            "region": snap.intensity.region if snap.intensity else None,
            "g_per_kwh": snap.intensity.g_per_kwh if snap.intensity else None,
        },
        "provenance": {k: {"quality": v.quality, "detail": v.detail}
                       for k, v in w.provenance.items()},
    }


# ── breakdown report ──────────────────────────────────────────────────────
def format_report_cli(breakdown: dict, group_by: str, mode: str) -> str:
    """`breakdown` maps group key -> EnergySnapshot. Ranked by recoverable."""
    lines = [f"Energy by {group_by}   (valuation: {mode})", ""]
    lines.append(f"  {'':16} {'consumed':>12} {'recoverable':>13} {'efficiency':>11}")
    items = sorted(breakdown.items(),
                   key=lambda kv: kv[1].waste.total_wh, reverse=True)
    for key, snap in items:
        label = (key or "(unset)")[:16]
        lines.append(
            f"  {label:16} {_kwh(snap.consumed_wh):>12} "
            f"{_kwh(snap.waste.total_wh):>13} {snap.efficiency_pct:>10.1f}%"
        )
    if items:
        top_key, top = items[0]
        lines.append("")
        lines.append(f"  Biggest opportunity: {top_key or '(unset)'} — "
                     f"{_kwh(top.waste.total_wh)} recoverable "
                     f"(≈ {equivalence(top.waste.total_wh / 1000)}).")
    return "\n".join(lines)


# ── per-user profile ──────────────────────────────────────────────────────
def format_user_cli(username: str, snap, recommendations: list[str],
                    mode: str, explain: bool = False) -> str:
    w = snap.waste
    score = max(0, min(100, round(snap.efficiency_pct)))
    lines = [f"Energy profile — {username}   (valuation: {mode})", ""]
    lines.append(f"  Consumed        {_kwh(snap.consumed_wh):>14}"
                 f"   ({_co2(snap.grams_co2())})")
    lines.append(f"  Efficiency score {score:>11}/100")
    lines.append(f"  Recoverable     {_kwh(w.total_wh):>14}"
                 f"   (≈ {equivalence(w.total_wh / 1000)})")
    if recommendations:
        lines.append("")
        lines.append("  Ways to improve:")
        for rec in recommendations:
            lines.append(f"    • {rec}")
    if explain:
        lines.append("")
        for name, prov in w.provenance.items():
            lines.append(f"    {name}: {prov.quality} — {prov.detail}")
    return "\n".join(lines)


def to_json_str(obj: dict) -> str:
    return json.dumps(obj, indent=2)

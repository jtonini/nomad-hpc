# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What makes something in a lab need its PI's attention (1.17.6): the
limits nomad.facts.attention uses, each settable in nomad.toml.

    [console.labs.attention]
    # days = 7                 # the jobs, and the growth of data, looked at
    # waiting_hours = 24       # a job waiting longer than this for something
    #                          # that won't change by itself
    # waiting_max_days = 60    # ... and no longer (an old record, not a job)
    # failing_jobs = 3         # a member with this many failed jobs or more,
    #                          # at least half of theirs that ran to an end
    # storage_pct = 90         # a lab storage server this full or more
    # over_cpu_hours = 100     # a member who held this many core-hours or
    # over_cpu_pct = 25        # more (measured jobs) and used under this share
    # over_memory_pct = 20     # jobs that used under this share of the
    # over_memory_jobs = 3     # memory they asked for, over this many jobs
    # holders = 3              # the lab's biggest holders of data named (0: none)
    # holder_gb = 100          # ... each holding this much or more
    # growth_gb = 100          # and anyone whose data grew this much in `days` (0: none)
    # quota_pct = 90           # a member this close to a quota
    # workstation_hours = 2    # a lab workstation silent this long

Sizes in GB are decimal (1 GB = 10^9 bytes), as df -H and the Console say
them.
"""
from __future__ import annotations

import math

DEFAULTS = {
    "days": 7,
    "waiting_hours": 24,
    "waiting_max_days": 60,
    "failing_jobs": 3,
    "storage_pct": 90,
    "over_cpu_hours": 100,
    "over_cpu_pct": 25,
    "over_memory_pct": 20,
    "over_memory_jobs": 3,
    "holders": 3,
    "holder_gb": 100,
    "growth_gb": 100,
    "quota_pct": 90,
    "workstation_hours": 2,
}
# Whole numbers; a share is 0-100; days at most 90 (the storage readings are
# kept every reading for 30 days, then one a day).
_WHOLE = {"days", "failing_jobs", "over_memory_jobs", "holders", "waiting_max_days"}
_PCT = {"storage_pct", "over_cpu_pct", "over_memory_pct", "quota_pct"}
_RANGE = {"days": (1, 90), "waiting_max_days": (1, 3650), "failing_jobs": (1, 10**6),
          "over_memory_jobs": (1, 10**6), "holders": (0, 50)}


def settings(raw) -> tuple[dict, list]:
    """([console.labs.attention] over DEFAULTS, problems). A value that
    isn't usable keeps its default, and says so."""
    out = dict(DEFAULTS)
    problems: list = []
    if raw is None:
        return out, problems
    where = "[console.labs.attention]"
    if not isinstance(raw, dict):
        return out, [f"{where} should be a table; the defaults apply"]
    for key, value in raw.items():
        if key not in DEFAULTS:
            problems.append(f"{where} {key}: unknown setting; the settings are {', '.join(DEFAULTS)}")
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            problems.append(f"{where} {key} should be a number; {DEFAULTS[key]} applies")
            continue
        if key in _WHOLE and value != int(value):
            problems.append(f"{where} {key} should be a whole number; {DEFAULTS[key]} applies")
            continue
        lo, hi = _RANGE.get(key, (0, 100 if key in _PCT else float("inf")))
        if not lo <= value <= hi:
            problems.append(f"{where} {key} = {value}: should be from {lo} to "
                            f"{'any number' if hi == float('inf') else f'{hi:,}'}; {DEFAULTS[key]} applies")
            continue
        out[key] = int(value) if key in _WHOLE else value
    return out, problems


def from_config(config: dict | None) -> dict:
    """The settings nomad.toml gives (problems aside: `nomad config check`
    reports them through nomad.config.access)."""
    labs = ((config or {}).get("console") or {}).get("labs") or {}
    return settings(labs.get("attention") if isinstance(labs, dict) else None)[0]

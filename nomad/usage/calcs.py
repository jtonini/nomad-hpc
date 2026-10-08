# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The definitions behind every figure of the usage report.

These follow the reference implementation that produced the October 2026
replacement plan (handoff §6), with its corrections: admin accounts are not
people, partitions are classed by the nodes they hold, core-hours are
clipped to the period.

Conventions:
- A job is one allocation (sacct -X): array tasks are separate jobs.
- Core-hours = allocated CPUs x elapsed hours, split evenly across a job's
  nodes, and clipped to the period.
- Times are naive local datetimes, as Slurm and nomad's collectors write them.
"""
from __future__ import annotations

import calendar
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta


# --- time -------------------------------------------------------------------

def month_hours(year: int, month: int) -> int:
    """Exact hours in a calendar month: never a fixed 730."""
    return calendar.monthrange(year, month)[1] * 24


def period_hours(t0: datetime, t1: datetime) -> float:
    return max(0.0, (t1 - t0).total_seconds() / 3600.0)


def overlap_hours(start: datetime, end: datetime, t0: datetime, t1: datetime) -> float:
    """Hours of [start, end) inside [t0, t1)."""
    a, b = max(start, t0), min(end, t1)
    return max(0.0, (b - a).total_seconds() / 3600.0)


def annualize(value: float, days: float) -> float:
    """A total over ``days`` scaled to 365 days."""
    return value * 365.0 / days if days else float("nan")


def month_key(t: datetime) -> str:
    return f"{t.year:04d}-{t.month:02d}"


def months_between(t0: datetime, t1: datetime) -> list[str]:
    """Every calendar month that [t0, t1) touches."""
    out, y, m = [], t0.year, t0.month
    while (y, m) < (t1.year, t1.month) or ((y, m) == (t1.year, t1.month) and t1 > datetime(y, m, 1)):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_bounds(key: str) -> tuple[datetime, datetime]:
    y, m = int(key[:4]), int(key[5:7])
    a = datetime(y, m, 1)
    b = datetime(y + 1, 1, 1) if m == 12 else datetime(y, m + 1, 1)
    return a, b


def full_months(t0: datetime, t1: datetime) -> list[str]:
    """Calendar months lying entirely inside [t0, t1)."""
    return [k for k in months_between(t0, t1)
            if month_bounds(k)[0] >= t0 and month_bounds(k)[1] <= t1]


# --- jobs ---------------------------------------------------------------------

def job_hours(job, t0: datetime | None, t1: datetime | None, clip: bool) -> float:
    """Elapsed hours of a job: inside [t0, t1) when clipping, else the whole job."""
    if not clip:
        return job.elapsed / 3600.0
    if job.start is None or job.end is None:
        return 0.0
    return overlap_hours(job.start, job.end, t0, t1)


def per_node_core_hours(jobs: Iterable, t0: datetime | None = None, t1: datetime | None = None,
                        clip: bool = True) -> tuple[Counter, dict[str, set]]:
    """Core-hours and distinct people per node; a multi-node job's core-hours
    are split evenly across its nodes."""
    ch: Counter = Counter()
    people: dict[str, set] = defaultdict(set)
    for j in jobs:
        if not j.nodes:
            continue
        hours = job_hours(j, t0, t1, clip)
        if hours <= 0:
            continue
        share = j.cpus / len(j.nodes) * hours
        for n in j.nodes:
            ch[n] += share
            people[n].add(j.user)
    return ch, people


def utilization(core_hours: float, cores: float, hours: float) -> float:
    return core_hours / (cores * hours) if cores and hours else float("nan")


def waiting_share(jobs: Iterable, scope_nodes: Iterable[str], threshold_hours: float = 24.0,
                  ) -> dict[str, dict]:
    """Per month submitted: core-hours of the jobs whose nodes all lie in
    ``scope_nodes``, the core-hours of those that waited more than the
    threshold, and the people. Weighted by core-hours, so one array of
    thousands of short jobs can't read as "91% of jobs waited". Jobs that
    never started have no wait and are left out."""
    scope = set(scope_nodes)
    acc: dict[str, dict] = defaultdict(lambda: {"core_hours": 0.0, "waiting_core_hours": 0.0,
                                                "users": set(), "users_waited": set(),
                                                "jobs": 0, "jobs_waited": 0})
    for j in jobs:
        if not j.nodes or j.start is None or j.submit is None:
            continue
        if not all(n in scope for n in j.nodes):
            continue
        r = acc[month_key(j.submit)]
        c = j.cpus * j.elapsed / 3600.0
        r["core_hours"] += c
        r["users"].add(j.user)
        r["jobs"] += 1
        if (j.start - j.submit).total_seconds() > threshold_hours * 3600:
            r["waiting_core_hours"] += c
            r["users_waited"].add(j.user)
            r["jobs_waited"] += 1
    out = {}
    for m, r in sorted(acc.items()):
        out[m] = {
            "core_hours": r["core_hours"],
            "waiting_core_hours": r["waiting_core_hours"],
            "share": r["waiting_core_hours"] / r["core_hours"] if r["core_hours"] else 0.0,
            "users": len(r["users"]),
            "users_waited": len(r["users_waited"]),
            "jobs": r["jobs"],
            "jobs_waited": r["jobs_waited"],
        }
    return out


def cpu_efficiency(rows: Iterable[tuple[float | None, float, float]]) -> float:
    """Share of held core-time used: sum(pct/100 x cpus x runtime) / sum(cpus x
    runtime), over rows whose pct is known."""
    num = den = 0.0
    for pct, cpus, rt in rows:
        if pct is None or not cpus or not rt:
            continue
        num += pct / 100.0 * cpus * rt
        den += cpus * rt
    return num / den if den else float("nan")


def median(values: Sequence[float]) -> float:
    vals = sorted(values)
    if not vals:
        return float("nan")
    k = len(vals) // 2
    return vals[k] if len(vals) % 2 else (vals[k - 1] + vals[k]) / 2


def largest_share(amounts: dict) -> float:
    """The largest single contributor's share of a total."""
    total = sum(amounts.values())
    return max(amounts.values()) / total if total else float("nan")


# --- GPUs -----------------------------------------------------------------------

def gpu_hours(job, t0: datetime | None = None, t1: datetime | None = None) -> float:
    """GPUs allocated x hours, clipped to [t0, t1) when both are given."""
    if not job.gpus:
        return 0.0
    if t0 is None or t1 is None:
        return job.gpus * job.elapsed / 3600.0
    if job.start is None or job.end is None:
        return 0.0
    return job.gpus * overlap_hours(job.start, job.end, t0, t1)


def classify(families: Sequence, *texts: str | None) -> tuple[str, str | None]:
    """(family, how): the first family whose regex matches the first text,
    then the next text; ("unclassified", None) when none does."""
    for how, text in zip(("name", "workdir"), texts):
        text = (text or "").lower()
        if not text:
            continue
        for fam in families:
            if fam.regex.search(text):
                return fam.name, how
    return "unclassified", None


def primary_family(hours_by_family: Counter, jobs_by_family: Counter) -> str:
    """A person's main family: most GPU-hours; with none, most jobs
    ('unclassified' only when it is all there is)."""
    if sum(hours_by_family.values()) > 0:
        return max(hours_by_family, key=hours_by_family.get)
    ranked = [f for f, _ in jobs_by_family.most_common() if f != "unclassified"]
    return ranked[0] if ranked else "unclassified"


def gres_gpus(gres: str | None) -> int:
    """GPUs in a node's GRES ('gpu:a40:8(S:0-1)', 'gpu:2,shard:4') -> 8, 2."""
    total = 0
    for part in str(gres or "").split(","):
        bits = part.split("(")[0].split(":")
        if not bits or bits[0].strip() != "gpu":
            continue
        try:
            total += int(bits[-1])
        except ValueError:
            continue
    return total


# --- long-run load (sreport) ----------------------------------------------------

def sreport_annual(rows: Iterable[dict], tres: str = "cpu") -> dict[str, dict]:
    """Annual totals of sreport monthly rows for one TRES. Allocated, idle and
    planned are shares of up-hours (reported - down); down of all hours."""
    acc: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r.get("tres", "cpu") != tres:
            continue
        y = r["month"][:4]
        acc[y]["months"] += 1
        for k in ("allocated_h", "down_h", "planned_down_h", "idle_h", "planned_h", "reported_h"):
            acc[y][k] += float(r.get(k) or 0.0)
    out = {}
    for y, c in sorted(acc.items()):
        up = c["reported_h"] - c["down_h"]
        out[y] = {
            "months": int(c["months"]),
            "allocated_h": c["allocated_h"],
            "allocated_share": c["allocated_h"] / up if up else float("nan"),
            "idle_share": c["idle_h"] / up if up else float("nan"),
            "planned_share": c["planned_h"] / up if up else float("nan"),
            "down_share": c["down_h"] / c["reported_h"] if c["reported_h"] else float("nan"),
        }
    return out


def growth_rates(annual: dict[str, float]) -> dict[str, float]:
    """Year-over-year growth for consecutive years: {'2023': 0.34, ...}."""
    ys = sorted(annual)
    return {y: annual[y] / annual[p] - 1 for p, y in zip(ys, ys[1:])
            if annual[p] and int(y) == int(p) + 1}


def cagr(first: float, last: float, years: float) -> float:
    return (last / first) ** (1 / years) - 1 if first > 0 and years > 0 else float("nan")


# --- capacity and projection ----------------------------------------------------------

def capacity_core_hours(cores: float, hours: float = 8760, weight: float = 1.0,
                        practical: float = 0.75) -> float:
    """Practical capacity: cores x weight x hours x practical share."""
    return cores * weight * hours * practical


def projection(base: float, rate: float, years: int, base_year: int) -> dict[int, float]:
    return {base_year + k: base * (1 + rate) ** k for k in range(years + 1)}


def year_capacity_crossed(base: float, rate: float, capacity: float, base_year: int,
                          horizon: int = 15) -> int | None:
    """First year in which projected demand exceeds the capacity."""
    for y, d in projection(base, rate, horizon, base_year).items():
        if d > capacity:
            return y
    return None


def floor_new_cores(current_cores: float, weight: float = 1.45) -> int:
    """New cores that do the work of today's: ceil(current / weight)."""
    return math.ceil(current_cores / weight) if weight else 0


# --- storage --------------------------------------------------------------------------

def storage_growth(monthly_max: Sequence[tuple[str, float]], drop: float = 0.05
                   ) -> tuple[float, str, str | None]:
    """Growth per month after the last cleanup (a month-on-month fall larger
    than ``drop``): (growth per month, first month used, cleanup month)."""
    start, cleanup = 0, None
    for i in range(1, len(monthly_max)):
        prev, cur = monthly_max[i - 1][1], monthly_max[i][1]
        if prev and (prev - cur) / prev > drop:
            start, cleanup = i, monthly_max[i][0]
    seg = monthly_max[start:]
    if len(seg) < 2:
        return float("nan"), (seg[0][0] if seg else ""), cleanup
    return (seg[-1][1] - seg[0][1]) / (len(seg) - 1), seg[0][0], cleanup


def months_to_full(capacity: float, used: float, growth_per_month: float) -> float:
    if not growth_per_month or growth_per_month <= 0 or math.isnan(growth_per_month):
        return float("inf")
    return max(0.0, (capacity - used) / growth_per_month)


def add_months(key: str, months: float) -> str:
    """'2026-10' + 4.4 months -> '2027-02' (the month the date falls in)."""
    a, _ = month_bounds(key)
    days = months * 30.4375
    t = a + timedelta(days=days)
    return month_key(t)


def is_nan(x) -> bool:
    return isinstance(x, float) and math.isnan(x)

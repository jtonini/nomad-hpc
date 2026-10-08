# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The fourteen sections of the usage report.

Each builder takes the name-free ``Data`` and returns a ``Section``: one
sentence stating its finding with the number behind it, the facts, and the
tables. A section without data says "not measured" and why; it never shows
zeros it didn't measure.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from nomad.usage import calcs, fmt
from nomad.usage.config import ALL_NODES, CONDO, OTHER, OVERLAY
from nomad.usage.facts import (ESTIMATED, MEASURED, NOT_MEASURED, PARTIAL, PROJECTED, Coverage,
                               Range, Report, Section, SetAside, Table)
from nomad.usage.sources import MIXED, OUTSIDE, Data

JOBS = "jobs"
SREPORT = "Slurm's monthly usage totals (sreport cluster utilization)"
NODE_SAMPLES = "NØMAÐ node samples (node_state)"
JOB_METRICS = "NØMAÐ job metrics (job_summary)"
GPU_SAMPLES = "NØMAÐ GPU samples (gpu_stats)"
FS_SAMPLES = "NØMAÐ filesystem samples (filesystems)"
WAIT_HOURS = 24.0


class Ctx:
    """What several sections share, computed once."""

    def __init__(self, d: Data):
        self.d, self.cfg = d, d.cfg
        self.period = fmt.period(d.t0, d.t1)
        self.days = d.hours / 24.0
        self.started = [j for j in d.jobs if j.started]
        self.ch, self.node_people = calcs.per_node_core_hours(self.started, d.t0, d.t1, clip=True)
        self.ch_whole, _ = calcs.per_node_core_hours(self.started, clip=False)
        cfg = self.cfg
        self.inst_tiers = sorted(cfg.institutional, key=self._tier_order)
        self.tiers = list(cfg.tiers) if cfg.has_tier_map else [ALL_NODES]
        self.other_tiers = [t for t in self.tiers if t not in cfg.institutional]
        self.inst_nodes = d.tier_nodes(cfg.institutional)
        self.other_nodes = d.tier_nodes(self.other_tiers)
        # The institution's GPU tiers: institutional tiers whose nodes all
        # have GPUs (a lab's GPU node doesn't make its tier one). Without a
        # tier map, every node with GPUs.
        if cfg.has_tier_map:
            self.gpu_tiers = [t for t in self.inst_tiers
                              if d.tier_nodes([t]) and all((d.nodes.get(n) and d.nodes[n].gpus)
                                                           for n in d.tier_nodes([t]))]
            self.gpu_nodes = set(d.tier_nodes(self.gpu_tiers))
        else:
            self.gpu_tiers = []
            self.gpu_nodes = set(d.gpu_nodes())
        self.cards = sum(d.nodes[n].gpus for n in self.gpu_nodes if n in d.nodes)
        self.full_months = calcs.full_months(d.t0, d.t1)
        self.jobs_source = d.source
        self.job_people = len({j.user for j in d.jobs})
        # GPU accounting: from report.toml, else the first job with GPUs in
        # its allocation in all the records (not only the period's).
        self.gpu_start = cfg.gpu_accounting_start
        self.gpu_start_detected = False
        if self.gpu_start is None and d.gpu_first_alloc is not None:
            self.gpu_start = d.gpu_first_alloc
            self.gpu_start_detected = True

    def _tier_order(self, t):
        return list(self.cfg.tiers).index(t) if t in self.cfg.tiers else 0

    def cores(self, nodes) -> int | None:
        vals = [self.d.cores(n) for n in nodes]
        if not vals or any(v is None for v in vals):
            return None
        return sum(vals)

    def pp(self, n) -> str:
        """A count of people as the report may print it (min_cell)."""
        mc = self.cfg.min_cell
        if mc and isinstance(n, (int, float)) and 0 < n < mc:
            return f"fewer than {mc}"
        return fmt.num(n)


def ran_in_period(j, d: Data) -> bool:
    """A job that ran for some time inside the period (or, lasting no time,
    started inside it)."""
    if j.start is None or j.end is None:
        return False
    if j.end > j.start:
        return j.start < d.t1 and j.end > d.t0
    return d.t0 <= j.start < d.t1


def _section(n: int, key: str, title: str, ctx: Ctx, source: str = "", period: str | None = None) -> Section:
    return Section(n, key, title, source=source, period=ctx.period if period is None else period)


def _not_measured(s: Section, why: str) -> Section:
    s.status = NOT_MEASURED
    s.finding = f"Not measured: {why}"
    return s


# 1 -----------------------------------------------------------------------------------

def s01_headline(ctx: Ctx) -> Section:
    d = ctx.d
    s = _section(1, "headline", "Headline", ctx, source=ctx.jobs_source)
    if not d.jobs:
        return _not_measured(s, "no job records in the period.")
    H = d.hours
    rows = []
    groups = [("institutional", ctx.inst_nodes)]
    if ctx.other_nodes:
        groups.append(("condo" if ctx.other_tiers == [CONDO] else "other tiers", ctx.other_nodes))
    for label, nodes in groups:
        key = "institutional" if label == "institutional" else "condo"
        cores = ctx.cores(nodes)
        ch = sum(ctx.ch[n] for n in nodes)
        chw = sum(ctx.ch_whole[n] for n in nodes)
        util = calcs.utilization(ch, cores, H) if cores else float("nan")
        utilw = calcs.utilization(chw, cores, H) if cores else float("nan")
        s.fact(f"{key}_core_hours", f"{label} core-hours, clipped to the period", ch, "core-hours")
        s.fact(f"{key}_utilization", f"{label} core-hours as a share of the maximum", util, "share",
               note="" if cores else "cores per node unknown")
        s.fact(f"{key}_core_hours_whole", f"{label} core-hours, each job counted whole", chw, "core-hours",
               note="as in the October 2026 documents; includes hours of jobs outside the period")
        s.fact(f"{key}_utilization_whole", f"{label} share of the maximum, each job counted whole", utilw, "share")
        s.fact(f"{key}_nodes", f"{label} nodes", len(nodes), "nodes")
        s.fact(f"{key}_cores", f"{label} cores", cores, "cores")
        rows.append([f"{label.capitalize()} core-hours", fmt.big(ch), fmt.big(chw)])
        rows.append([f"{label.capitalize()} share of maximum ({fmt.num(len(nodes))} nodes, "
                     f"{fmt.num(cores) if cores else '?'} cores)", fmt.pct(util, 1), fmt.pct(utilw, 1)])
    people = ctx.job_people
    per_month = defaultdict(set)
    for j in d.jobs:
        if j.submit is not None:
            per_month[calcs.month_key(j.submit)].add(j.user)
    counts = [len(per_month[m]) for m in ctx.full_months]
    s.fact("people", "people with jobs in the period", people, "people", people=True,
           note="anyone with a job that ran or waited in the period, admin accounts excluded")
    if counts:
        s.fact("people_per_month_min", "people per month (by month submitted), lowest", min(counts),
               "people", people=True)
        s.fact("people_per_month_max", "people per month (by month submitted), highest", max(counts),
               "people", people=True)
    ran = [j for j in ctx.started if ran_in_period(j, d)]
    s.fact("jobs_run", "jobs that ran in the period", len(ran), "jobs",
           note="array tasks count as jobs; read next to core-hours")
    s.fact("period_days", "days in the period", round(ctx.days, 2), "days")
    by_person = Counter()
    for j in ctx.started:
        for n in j.nodes:
            if n in set(ctx.inst_nodes):
                by_person[j.user] += j.cpus / len(j.nodes) * calcs.job_hours(j, d.t0, d.t1, True)
    top = calcs.largest_share(by_person) if by_person else float("nan")
    s.fact("largest_person_share", "largest single person's share of institutional core-hours", top, "share")
    rows.append(["People", people, ""])
    if counts:
        rows.append(["People in one month, lowest–highest (by month submitted)", Range(min(counts), max(counts)), ""])
    rows.append(["Jobs that ran", fmt.num(len(ran)), ""])
    rows.append(["Largest single person's share of institutional core-hours", fmt.pct(top), ""])
    # Growth against the same months a year earlier, from Slurm's totals.
    growth = _same_months_growth(d, ctx.full_months)
    if growth is not None:
        g, months = growth
        s.fact("growth_same_months_last_year", "core-hours allocated, change on the same months a year earlier",
               g, "share", source=SREPORT, period=f"{fmt.month(months[0])} – {fmt.month(months[-1])}")
        rows.append(["Change on the same months a year earlier (Slurm's totals, whole cluster)",
                     fmt.pct(g), ""])
    s.tables.append(Table("Core-hours delivered, " + ctx.period,
                          ["Measure", "Clipped to the period", "Each job counted whole"], rows,
                          note="Core-hours = allocated CPUs × elapsed hours, split evenly across a job's "
                               "nodes. The maximum is cores × hours in the period.", people_columns=[1]))
    ch = s.get("institutional_core_hours")
    util = s.get("institutional_utilization")
    dominant = not fmt.missing(top) and top > 0.3
    s.finding = (f"The institutional nodes delivered {fmt.big(ch)} core-hours"
                 + (f", {fmt.pct(util)} of their maximum," if util is not None else "")
                 + f" to {ctx.pp(people)} people between {fmt.day(d.t0)} and {fmt.day(d.t1 - timedelta(seconds=1))}"
                 + (f"; {fmt.pct(top)} of them went to the busiest single person." if dominant else "."))
    if dominant:
        s.notes.append("One person ran a large share of the work: medians and job counts describe them "
                       "more than anyone else.")
    return s


def _same_months_growth(d: Data, months: list[str]):
    rows = {r["month"]: r for r in d.usage_rows if r.get("tres", "cpu") == "cpu"}
    if not months:
        return None
    cur = prev = 0.0
    used = []
    for m in months:
        y, mm = int(m[:4]), m[5:]
        p = f"{y - 1:04d}-{mm}"
        if m in rows and p in rows:
            cur += float(rows[m]["allocated_h"] or 0)
            prev += float(rows[p]["allocated_h"] or 0)
            used.append(m)
    if not used or not prev:
        return None
    return cur / prev - 1, used


# 2 -----------------------------------------------------------------------------------

def s02_load(ctx: Ctx) -> Section:
    d = ctx.d
    s = _section(2, "multi_year_load", "Multi-year load", ctx, source=SREPORT, period="")
    rows = [r for r in d.usage_rows if r.get("tres", "cpu") == "cpu"]
    if not rows:
        return _not_measured(s, "Slurm's monthly totals aren't in this database (the slurm_usage "
                                "collector, nomad 1.7.44 or later, keeps them).")
    first, last = rows[0]["month"], rows[-1]["month"]
    s.period = f"{fmt.month(first)} – {fmt.month(last)}"
    unsettled = [r["month"] for r in rows if not r.get("settled", 1)]
    # A month Slurm may still revise is left out of the totals below.
    rows = [r for r in rows if r.get("settled", 1)]
    if not rows:
        return _not_measured(s, "Slurm's monthly totals hold no finished month yet.")
    annual = calcs.sreport_annual(rows)
    full = {y: a["allocated_h"] for y, a in annual.items() if a["months"] == 12}
    yoy = calcs.growth_rates(full)
    table = []
    months_of = defaultdict(list)
    for r in rows:
        months_of[r["month"][:4]].append(r["month"])
    for y, a in annual.items():
        change = ""
        if y in yoy:
            change = f"{yoy[y] * 100:+.0f}%"
            s.fact(f"growth.{y}", f"core-hours allocated in {y}, change on {int(y) - 1}", yoy[y], "share")
        elif a["months"] < 12 and str(int(y) - 1) in annual:
            ms = [m[5:] for m in months_of[y]]
            prev = sum(float(r["allocated_h"] or 0) for r in rows
                       if r["month"][:4] == str(int(y) - 1) and r["month"][5:] in ms)
            if prev and len([r for r in rows if r["month"][:4] == str(int(y) - 1) and r["month"][5:] in ms]) == len(ms):
                g = a["allocated_h"] / prev - 1
                change = f"{g * 100:+.0f}% on the same months of {int(y) - 1}"
                s.fact(f"growth_same_months.{y}", f"core-hours allocated in {y}'s months, change on "
                       f"the same months of {int(y) - 1}", g, "share")
        label = y if a["months"] == 12 else f"{y}, {fmt.month(months_of[y][0])[:3]}–{fmt.month(months_of[y][-1])[:3]}"
        table.append([label, fmt.millions(a["allocated_h"]), change, fmt.pct(a["allocated_share"]),
                      fmt.pct(a["idle_share"]), fmt.pct(a["planned_share"]), fmt.pct(a["down_share"], 1)])
        for k in ("allocated_h", "allocated_share", "idle_share", "planned_share", "down_share"):
            s.fact(f"annual.{y}.{k}", f"{y}: {k.replace('_', ' ')}", a[k],
                   "core-hours" if k == "allocated_h" else "share", note=f"{a['months']} months")
    s.tables.append(Table("Slurm's monthly totals by year (CPU)",
                          ["Year", "Core-hours allocated", "Change", "Allocated", "Idle, no job waiting",
                           "Earmarked for waiting jobs", "Down"], table,
                          note="Allocated, idle and earmarked are shares of the hours when nodes were up; "
                               "down is a share of all hours. CPU rows only: GPU rows are not core-hours."))
    for r in rows:
        s.fact(f"month.{r['month']}.allocated_h", f"{r['month']} core-hours allocated",
               float(r["allocated_h"] or 0), "core-hours")
    years = sorted(full)
    cg = float("nan")
    if len(years) >= 2:
        cg = calcs.cagr(full[years[0]], full[years[-1]], int(years[-1]) - int(years[0]))
        s.fact("cagr", f"compound growth {years[0]}–{years[-1]}", cg, "share")
        if yoy:
            s.fact("growth_low", "lowest year-over-year growth of the full years", min(yoy.values()), "share")
            s.fact("growth_high", "highest year-over-year growth of the full years", max(yoy.values()), "share")
        else:
            s.notes.append("No two full years in a row: the compound rate stands for low, central and high.")
    busiest = max(rows, key=lambda r: float(r["allocated_h"] or 0))
    s.fact("busiest_month", "busiest month", busiest["month"], "month")
    s.fact("busiest_month_core_hours", "core-hours allocated in the busiest month",
           float(busiest["allocated_h"] or 0), "core-hours")
    summers = {}
    for y in months_of:
        ms = [r for r in rows if r["month"][:4] == y and r["month"][5:] in ("06", "07", "08")]
        if len(ms) == 3:
            summers[y] = sum(float(r["allocated_h"] or 0) for r in ms)
    if summers:
        sg = calcs.growth_rates(summers)
        srows = [[y, fmt.millions(v), f"{sg[y] * 100:+.0f}%" if y in sg else ""] for y, v in summers.items()]
        for y, v in summers.items():
            s.fact(f"summer.{y}", f"June–August {y} core-hours allocated", v, "core-hours")
        s.tables.append(Table("Summers (June–August)", ["Year", "Core-hours allocated", "Change"], srows))
    if unsettled:
        s.notes.append(f"{', '.join(fmt.month(m) for m in unsettled)} left out: Slurm may still revise "
                       "a month's totals until two days after it ends.")
    if d.usage_clusters > 1:
        s.notes.append(f"Slurm reports {d.usage_clusters} clusters here: their totals are added together.")
    s.notes.append("The totals have no split by tier, no users, and GPU use only where Slurm accounts GPUs.")
    if len(years) >= 2:
        s.finding = (f"Core-hours allocated grew from {fmt.millions(full[years[0]])} in {years[0]} to "
                     f"{fmt.millions(full[years[-1]])} in {years[-1]}, {fmt.pct(cg)} a year"
                     + (f"; the busiest month was {fmt.month(busiest['month'])}." ))
    else:
        s.finding = (f"Slurm's totals cover {fmt.month(first)} – {fmt.month(last)}, too few full years "
                     f"for a growth rate; the busiest month was {fmt.month(busiest['month'])}.")
        s.status = PARTIAL
    return s


# 3 -----------------------------------------------------------------------------------

def s03_who(ctx: Ctx) -> Section:
    d, cfg = ctx.d, ctx.cfg
    s = _section(3, "who_uses_what", "Who uses what", ctx, source=ctx.jobs_source)
    if not d.jobs:
        return _not_measured(s, "no job records in the period.")
    H = d.hours
    by_class = defaultdict(set)
    classes_of = defaultdict(set)
    gpu_label = "GPU partitions or GPU requests"
    for j in d.jobs:
        for k in j.pclasses:
            by_class[k].add(j.user)
            # A GPU tier's partitions and asking for GPUs are one class here.
            classes_of[j.user].add(gpu_label if k in ctx.gpu_tiers else k)
        if j.gpu_request or any(k in ctx.gpu_tiers for k in j.pclasses):
            by_class[gpu_label].add(j.user)
            classes_of[j.user].add(gpu_label)
    overlay_only = sum(1 for u in by_class.get(OVERLAY, ()) if classes_of[u] == {OVERLAY})
    several = sum(1 for ks in classes_of.values() if len(ks) > 1)
    table = []
    for t in ctx.tiers:
        nodes = d.tier_nodes([t])
        utils = []
        for n in nodes:
            c = d.cores(n)
            if c:
                utils.append(ctx.ch[n] / (c * H))
        ppl = [len(ctx.node_people.get(n, ())) for n in nodes]
        people = len(by_class.get(t, ()))
        s.fact(f"people.{t}", f"people who submitted to {t} partitions", people, "people", people=True)
        if utils:
            s.fact(f"node_utilization_min.{t}", f"{t}: lowest utilization of a node", min(utils), "share")
            s.fact(f"node_utilization_max.{t}", f"{t}: highest utilization of a node", max(utils), "share")
        if ppl:
            s.fact(f"people_per_node_min.{t}", f"{t}: fewest people on a node", min(ppl), "people", people=True)
            s.fact(f"people_per_node_max.{t}", f"{t}: most people on a node", max(ppl), "people", people=True)
        table.append([t, len(nodes), people,
                      fmt.span(min(utils), max(utils)) if utils else "–",
                      Range(min(ppl), max(ppl)) if ppl else "–"])
    extra = [(gpu_label, "gpu_partitions_or_requests"), (OVERLAY, "overlay"), (OTHER, "other")]
    if cfg.has_tier_map and CONDO not in cfg.tiers:
        extra.insert(2, (CONDO, "condo_partitions"))
    for label, key in extra:
        if label in by_class:
            n = len(by_class[label])
            s.fact(f"people.{key}", f"people: {label}", n, "people", people=True)
            table.append([label, "", n, "", ""])
    s.fact("people_overlay_only", "people who ran only through overlay partitions", overlay_only, "people", people=True)
    s.fact("people_several_classes", "people who used more than one class", several, "people", people=True)
    inst_people = [len(ctx.node_people.get(n, ())) for n in ctx.inst_nodes]
    if inst_people:
        s.fact("people_per_institutional_node_min", "fewest people on an institutional node", min(inst_people),
               "people", people=True)
        s.fact("people_per_institutional_node_max", "most people on an institutional node", max(inst_people),
               "people", people=True)
    s.tables.append(Table("People and use by node class, " + ctx.period,
                          ["Node class", "Nodes", "People", "Utilization per node", "People per node"], table,
                          note="People are those who submitted to the class's own partitions (for GPUs, also "
                               "anyone who asked for a GPU). Utilization is core-hours run on a node, clipped to "
                               "the period, as a share of its maximum. Classes overlap: one person can count in "
                               "several.", people_columns=[2, 4]))
    busiest = None
    for t in ctx.inst_tiers:
        hi = s.get(f"node_utilization_max.{t}")
        if hi is not None and (busiest is None or hi > busiest[1]):
            busiest = (t, hi)
    s.finding = (f"{ctx.pp(ctx.job_people)} people ran jobs and {ctx.pp(several)} of them used more than one "
                 "node class"
                 + (f"; the {busiest[0]} nodes were the most used, up to {fmt.pct(busiest[1])} of a node's "
                    "maximum." if busiest else "."))
    if overlay_only or OVERLAY in by_class:
        s.notes.append(f"{ctx.pp(len(by_class.get(OVERLAY, ())))} people submitted to overlay partitions, which "
                       f"span node classes; {ctx.pp(overlay_only) if overlay_only else 'none'} of them only there. "
                       "Tier figures come from the nodes jobs ran on, whatever the partition.")
    return s


# 4 -----------------------------------------------------------------------------------

def _wait_scope(ctx: Ctx) -> list[str]:
    if ctx.cfg.wait_tiers:
        return ctx.cfg.wait_tiers
    return [t for t in ctx.inst_tiers if t not in ctx.gpu_tiers] or ctx.inst_tiers


def s04_waits(ctx: Ctx) -> Section:
    d = ctx.d
    s = _section(4, "waits", "Waits", ctx, source=ctx.jobs_source)
    if not ctx.started:
        return _not_measured(s, "no job in the period started.")
    scope_tiers = _wait_scope(ctx)
    scope = d.tier_nodes(scope_tiers)
    label = " + ".join(scope_tiers)
    # Jobs submitted in the period that started after it waited in it too.
    w = calcs.waiting_share(ctx.started + d.late_jobs, scope, WAIT_HOURS)
    table = []
    for m, v in w.items():
        partial = m not in ctx.full_months
        s.fact(f"core_hours.{m}", f"{m}: core-hours of jobs submitted, {label}", v["core_hours"], "core-hours")
        s.fact(f"waiting_core_hours.{m}", f"{m}: core-hours of jobs that waited over a day",
               v["waiting_core_hours"], "core-hours")
        s.fact(f"waiting_share.{m}", f"{m}: share of core-hours that waited over a day", v["share"], "share")
        s.fact(f"people.{m}", f"{m}: people with jobs", v["users"], "people", people=True)
        s.fact(f"people_waited.{m}", f"{m}: people who waited over a day", v["users_waited"], "people", people=True)
        table.append([fmt.month(m) + (" (part)" if partial else ""), fmt.thousands(v["core_hours"]),
                      fmt.thousands(v["waiting_core_hours"]), fmt.pct(v["share"], 1),
                      (v["users_waited"], v["users"])])
    s.tables.append(Table(f"Work that waited more than a day, by month submitted ({label} nodes)",
                          ["Month submitted", "Core-hours (K)", "Waited > 1 day (K)", "Share", "People who waited / people"],
                          table, note="Every job that ran entirely on these nodes, whichever partition it was "
                                      "submitted to. Wait = start − submission; jobs that never started have none "
                                      "and are left out. Weighted by core-hours: one large array must not read as "
                                      "most jobs waiting.", people_columns=[4]))
    # Per tier: median wait and who the waiting jobs belong to.
    trows = []
    for t in ctx.tiers:
        nodes = set(d.tier_nodes([t]))
        jobs = [j for j in ctx.started + d.late_jobs if j.submit and j.nodes and all(n in nodes for n in j.nodes)
                and d.t0 <= j.submit < d.t1]
        if not jobs:
            continue
        waits = [(j.start - j.submit).total_seconds() / 3600 for j in jobs]
        waited = Counter(j.user for j in jobs if (j.start - j.submit).total_seconds() > WAIT_HOURS * 3600)
        med = calcs.median(waits)
        s.fact(f"median_wait_hours.{t}", f"{t}: median wait of jobs submitted in the period", med, "hours",
               n=len(jobs))
        top = calcs.largest_share(waited) if waited else float("nan")
        if waited:
            s.fact(f"largest_person_share_of_waiting_jobs.{t}",
                   f"{t}: largest single person's share of jobs that waited over a day", top, "share",
                   n=sum(waited.values()))
        trows.append([t, fmt.num(len(jobs)), _hours(med), fmt.num(sum(waited.values())),
                      fmt.pct(top) if waited else "–"])
    if trows:
        s.tables.append(Table("Waits by node class, jobs submitted in the period",
                              ["Node class", "Jobs", "Median wait", "Jobs that waited > 1 day",
                               "Largest person's share of those"], trows))
    # People who waited, per tier partition and month (for comparisons).
    for t in ctx.tiers:
        own = [j for j in ctx.started + d.late_jobs if j.pclasses == (t,)]
        if own:
            wt = calcs.waiting_share(own, d.tier_nodes([t]), WAIT_HOURS)
            for m, v in wt.items():
                s.fact(f"partition.{t}.people_waited.{m}", f"{t} partitions, {m}: people who waited over a day",
                       v["users_waited"], "people", people=True)
                s.fact(f"partition.{t}.people.{m}", f"{t} partitions, {m}: people", v["users"], "people",
                       people=True)
    tot = sum(v["core_hours"] for v in w.values())
    totw = sum(v["waiting_core_hours"] for v in w.values())
    s.fact("waiting_share", f"share of core-hours that waited over a day, {label}",
           totw / tot if tot else float("nan"), "share")
    full = {m: v for m, v in w.items() if m in ctx.full_months and v["core_hours"]}
    if full:
        worst = max(full, key=lambda m: full[m]["share"])
        v = full[worst]
        s.finding = (f"In {fmt.month(worst)}, {fmt.pct(v['share'])} of the core-hours run on the {label} nodes "
                     f"waited more than a day to start ({ctx.pp(v['users_waited'])} of {ctx.pp(v['users'])} "
                     "people); over "
                     f"the period, {fmt.pct(totw / tot if tot else float('nan'))}.")
    else:
        s.finding = (f"{fmt.pct(totw / tot if tot else float('nan'))} of the core-hours run on the {label} nodes "
                     "waited more than a day to start.")
    return s


def _hours(h) -> str:
    if fmt.missing(h):
        return "–"
    if h < 1:
        return f"{h * 60:.0f} min"
    if h < 48:
        return f"{h:.1f} h"
    return f"{h / 24:.1f} days"


# 5 -----------------------------------------------------------------------------------

def s05_held(ctx: Ctx) -> Section:
    d = ctx.d
    s = _section(5, "held_vs_used", "Held vs used", ctx, source=f"{NODE_SAMPLES}; {JOB_METRICS}")
    have_nodes = bool(d.node_months)
    measured = [j for j in ctx.started if j.cpu_pct is not None and j.cpus and j.elapsed]
    if not have_nodes and not measured:
        return _not_measured(s, "no node samples and no job metrics for the period.")
    alloc_rows = []
    if have_nodes:
        months = sorted({m for (_, m) in d.node_months})
        for t in ctx.tiers:
            nodes = set(d.tier_nodes([t]))
            tot = [0.0, 0.0, 0.0]
            for m in months:
                acc = [0.0, 0.0, 0.0]
                for (n, mm), v in d.node_months.items():
                    if mm == m and n in nodes:
                        for i in range(3):
                            acc[i] += v[i]
                if acc[0]:
                    s.fact(f"allocation.{t}.{m}", f"{t}, {m}: mean share of cores allocated", acc[1] / acc[0], "share",
                           source=NODE_SAMPLES, n=int(acc[0]))
                    s.fact(f"load.{t}.{m}", f"{t}, {m}: mean node load as a share of cores", acc[2] / acc[0], "share",
                           source=NODE_SAMPLES, n=int(acc[0]))
                    for i in range(3):
                        tot[i] += acc[i]
            if tot[0]:
                a, l = tot[1] / tot[0], tot[2] / tot[0]
                s.fact(f"allocation.{t}", f"{t}: mean share of cores allocated", a, "share", source=NODE_SAMPLES,
                       n=int(tot[0]))
                s.fact(f"load.{t}", f"{t}: mean node load as a share of cores", l, "share", source=NODE_SAMPLES,
                       n=int(tot[0]))
                alloc_rows.append([t, fmt.pct(a), fmt.pct(l)])
    eff_rows = []
    by_class = defaultdict(list)
    for j in measured:
        k = j.pclasses[0] if len(j.pclasses) == 1 else (OVERLAY if j.pclasses else OTHER)
        by_class[k].append((j.cpu_pct, j.cpus, j.elapsed))
    for k in [*ctx.tiers, OVERLAY, CONDO, OTHER]:
        rows = by_class.pop(k, None)
        if not rows:
            continue
        e = calcs.cpu_efficiency(rows)
        held = sum(c * r for _, c, r in rows) / 3600
        s.fact(f"cpu_efficiency.{k}", f"{k} partitions: share of held core time used", e, "share",
               source=JOB_METRICS, n=len(rows))
        eff_rows.append([k, fmt.num(len(rows)), fmt.big(held), fmt.big(held * e), fmt.pct(e)])
    if measured:
        e_all = calcs.cpu_efficiency((j.cpu_pct, j.cpus, j.elapsed) for j in measured)
        s.fact("cpu_efficiency", "share of held core time used, all measured jobs", e_all, "share",
               source=JOB_METRICS, n=len(measured))
        s.fact("jobs_measured_share", "share of jobs that ran with CPU metrics",
               len(measured) / len(ctx.started) if ctx.started else float("nan"), "share")
    if alloc_rows:
        s.tables.append(Table("Allocation and node load, mean of the node samples in the period",
                              ["Node class", "Cores allocated", "Node load"], alloc_rows,
                              note="Allocated = cores held by jobs ÷ cores; load = min(load average, cores) ÷ "
                                   "cores. Load overstates use when jobs wait on I/O."))
    if eff_rows:
        s.tables.append(Table("CPU time used by jobs, by partition class",
                              ["Partition class", "Jobs measured", "Core-hours held", "Core-hours used",
                               "Share used"], eff_rows,
                              note="Σ(CPU use × cores × elapsed) ÷ Σ(cores × elapsed) over jobs with NØMAÐ job "
                                   "metrics. It understates multi-node jobs whose remote ranks start outside "
                                   "Slurm. The two measures disagree; the truth lies between them."))
    parts = []
    inst_nodes = set(ctx.inst_nodes)
    tot = [0.0, 0.0, 0.0]
    for (n, _), v in d.node_months.items():
        if n in inst_nodes:
            for i in range(3):
                tot[i] += v[i]
    if tot[0]:
        a, l = tot[1] / tot[0], tot[2] / tot[0]
        s.fact("allocation.institutional", "institutional nodes: mean share of cores allocated", a, "share",
               source=NODE_SAMPLES, n=int(tot[0]))
        s.fact("load.institutional", "institutional nodes: mean node load as a share of cores", l, "share",
               source=NODE_SAMPLES, n=int(tot[0]))
        parts.append(f"institutional nodes were {fmt.pct(a)} allocated and {fmt.pct(l)} loaded on average")
    if measured:
        parts.append(f"jobs' own accounting finds {fmt.pct(s.get('cpu_efficiency'))} of the core time they held used")
    s.finding = (parts[0][0].upper() + parts[0][1:] + ("; " + parts[1] if len(parts) > 1 else "") + ".") if parts else ""
    if not have_nodes:
        s.status = PARTIAL
        s.notes.append("No node samples in the period: allocation and load not measured.")
    if not measured:
        s.status = PARTIAL
        s.notes.append("No job metrics in the period: CPU time used not measured.")
    return s


# 6 -----------------------------------------------------------------------------------

def s06_memory(ctx: Ctx) -> Section:
    d = ctx.d
    s = _section(6, "memory", "Memory", ctx, source=f"{JOB_METRICS}; {ctx.jobs_source}")
    ceiling = d.node_memory_mb_max / 1024 if d.node_memory_mb_max else None
    jobs = [j for j in ctx.started if j.peak_mem_gb is not None]
    impossible = [j for j in jobs if ceiling and j.peak_mem_gb > ceiling]
    if impossible:
        d.set_aside["memory peak larger than any node"] += len(impossible)
    jobs = [j for j in jobs if not (ceiling and j.peak_mem_gb > ceiling)]
    if not jobs:
        return _not_measured(s, "no job in the period has a measured memory peak (NØMAÐ job metrics).")
    rows = []
    worst = None
    for t in ctx.tiers:
        js = [j for j in jobs if j.tier == t]
        if not js:
            continue
        req = [j.req_mem_mb / 1024 for j in js if j.req_mem_mb]
        peaks = [j.peak_mem_gb for j in js]
        mreq = sum(req) / len(req) if req else float("nan")
        mpeak = sum(peaks) / len(peaks)
        big = sum(1 for p in peaks if p > 512) / len(peaks)
        over_tb = sum(1 for r in req if r > 1024) / len(req) if req else float("nan")
        ratio = mreq / mpeak if mpeak and req else float("nan")
        for key, label, v, unit in (("requested_gb", "mean memory requested", mreq, "GB"),
                                    ("peak_gb", "mean peak used", mpeak, "GB"),
                                    ("largest_peak_gb", "largest peak", max(peaks), "GB"),
                                    ("share_over_512gb", "share of jobs peaking above 512 GB", big, "share"),
                                    ("share_requesting_over_1tb", "share of jobs requesting more than 1 TB", over_tb,
                                     "share"),
                                    ("request_to_use", "mean requested ÷ mean peak", ratio, "times")):
            s.fact(f"{key}.{t}", f"{t}: {label}", v, unit, n=len(js))
        rows.append([t, fmt.num(len(js)), fmt.gb(mreq), fmt.gb(mpeak), fmt.gb(max(peaks)), fmt.pct(big, 1),
                     fmt.pct(over_tb, 1), (f"{ratio:.0f}×" if not fmt.missing(ratio) else "–")])
        if not fmt.missing(ratio) and (worst is None or ratio > worst[1]):
            worst = (t, ratio, mreq, mpeak)
    s.tables.append(Table("Memory requested and used, jobs with a measured peak",
                          ["Node class", "Jobs", "Mean requested", "Mean peak used", "Largest peak",
                           "Jobs peaking > 512 GB", "Jobs requesting > 1 TB", "Requested ÷ used"], rows,
                          note="Peaks from NØMAÐ job metrics; requests from Slurm (GB = 1,024 MB). A job counts in "
                               "a class when all its nodes are in it."))
    s.fact("jobs_measured", "jobs with a measured memory peak", len(jobs), "jobs")
    if worst:
        s.finding = (f"Jobs request far more memory than they use: on the {worst[0]} nodes the average job "
                     f"requested {fmt.gb(worst[2])} and peaked at {fmt.gb(worst[3])}, {worst[1]:.0f} times less.")
    else:
        s.finding = f"{len(jobs)} jobs have a measured memory peak; requests are not recorded for them."
    if impossible:
        s.notes.append(f"{len(impossible)} peaks larger than any node's memory were left out (an accounting "
                       "artefact, not a measurement).")
    return s


# 7 -----------------------------------------------------------------------------------

def s07_gpus(ctx: Ctx) -> Section:
    d, cfg = ctx.d, ctx.cfg
    s = _section(7, "gpus", "GPUs", ctx, source=f"{ctx.jobs_source}; {GPU_SAMPLES}")
    cards = ctx.cards
    if not cards:
        return _not_measured(s, "no GPUs on the institution's nodes (report.toml gpus_per_node, or NØMAÐ "
                                "node samples).")
    s.fact("cards", "GPU cards", cards, "cards", source=d.inventory_source or "report.toml")
    # GPU work on the institution's GPU nodes (all of a job's GPUs are on its
    # GPU nodes).
    gjobs = [j for j in ctx.started if j.gpus and any(n in ctx.gpu_nodes for n in j.nodes)]
    g0 = ctx.gpu_start
    table = []
    if g0 is not None and g0 < d.t1:
        a = max(g0, d.t0)
        gh_total = sum(calcs.gpu_hours(j, a, d.t1) for j in gjobs)
        share = gh_total / (cards * calcs.period_hours(a, d.t1)) if d.t1 > a else float("nan")
        per = f"{fmt.day(a)} – {fmt.day(d.t1 - timedelta(seconds=1))}"
        s.fact("gpu_hours", "GPU-hours allocated", gh_total, "GPU-hours", period=per)
        s.fact("allocation_share", "GPU-hours allocated as a share of card-hours", share, "share", period=per,
               note=("accounting start detected from the first job with GPUs in its allocation"
                     if ctx.gpu_start_detected else "from the GPU accounting start in report.toml"))
        for m in calcs.months_between(a, d.t1):
            ma, mb = calcs.month_bounds(m)
            ma, mb = max(ma, a), min(mb, d.t1)
            gh = sum(calcs.gpu_hours(j, ma, mb) for j in gjobs)
            sh = gh / (cards * calcs.period_hours(ma, mb)) if mb > ma else float("nan")
            s.fact(f"gpu_hours.{m}", f"{m}: GPU-hours allocated", gh, "GPU-hours")
            s.fact(f"allocation_share.{m}", f"{m}: share of card-hours allocated", sh, "share")
            act = d.gpu_months.get(m)
            active = act[1] / act[0] if act and act[0] else float("nan")
            util = act[2] / act[0] / 100 if act and act[0] else float("nan")
            if act:
                s.fact(f"card_active_share.{m}", f"{m}: share of GPU samples with the card computing", active,
                       "share", source=GPU_SAMPLES, n=int(act[0]))
                s.fact(f"mean_utilization.{m}", f"{m}: mean GPU utilization", util, "share", source=GPU_SAMPLES)
            whole = calcs.month_bounds(m)
            if (ma, mb) == whole:
                label = fmt.month(m)
            elif ma != whole[0]:
                label = f"{fmt.month(m)} (from {fmt.day(ma)})"
            else:
                label = f"{fmt.month(m)} (to {fmt.day(mb - timedelta(seconds=1))})"
            table.append([label, fmt.num(gh), fmt.pct(sh, 1), fmt.pct(active), fmt.pct(util)])
        # By application family.
        fam_h = Counter()
        ph, pc = defaultdict(Counter), defaultdict(Counter)
        how = Counter()
        for j in gjobs:
            h = calcs.gpu_hours(j, a, d.t1)
            if h <= 0:
                continue
            fam_h[j.gpu_family] += h
            ph[j.user][j.gpu_family] += h
            pc[j.user][j.gpu_family] += 1
            how[j.gpu_family_how or "none"] += 1
        main = Counter(calcs.primary_family(ph[u], pc[u]) for u in ph)
        frows = []
        for f, h in sorted(fam_h.items(), key=lambda kv: (kv[0] == "unclassified", -kv[1])):
            sh = h / gh_total if gh_total else float("nan")
            s.fact(f"gpu_hours_by_family.{f}", f"GPU-hours: {f}", h, "GPU-hours", period=per)
            s.fact(f"gpu_hours_share_by_family.{f}", f"share of GPU-hours: {f}", sh, "share", period=per)
            s.fact(f"people_with_gpu_hours_by_family.{f}", f"people with GPU-hours whose main application is {f}",
                   main.get(f, 0), "people", people=True, period=per)
            frows.append([f, fmt.num(h), fmt.pct(sh, 1), main.get(f, 0)])
        s.fact("people_with_gpu_hours", "people with GPU-hours", len(ph), "people", people=True, period=per)
        if fam_h:
            s.fact("gpu_hours_unclassified_share", "share of GPU-hours whose application is unclassified",
                   fam_h.get("unclassified", 0) / gh_total if gh_total else float("nan"), "share", period=per)
            s.tables.append(Table(f"GPU-hours by application, {per}",
                                  ["Application", "GPU-hours", "Share", "People (by main application)"], frows,
                                  note="Applications from job names, then the end of the working directory, "
                                       f"with report.toml's patterns ({fmt.plural(how.get('name', 0), 'job')} "
                                       f"matched by name, {fmt.num(how.get('workdir', 0))} by directory). Each "
                                       "person counts once, under "
                                       "their main application.", people_columns=[3]))
    else:
        s.status = PARTIAL
        s.notes.append("Slurm recorded no GPU allocation in the period: GPU-hours not measured. Card activity "
                       "below comes from NØMAÐ's GPU samples.")
    if g0 is not None and g0 > d.t0:
        s.notes.append(f"GPU allocations exist only from {fmt.day(g0)}"
                       + (" (the first job with GPUs in its allocation)" if ctx.gpu_start_detected
                          else ", when Slurm began accounting GPUs")
                       + "; earlier GPU use is not in any job record.")
    if d.gpus_from_request:
        s.notes.append(f"{fmt.num(d.gpus_from_request)} jobs have no allocation recorded: their GPUs are "
                       "the ones they asked for.")
    if table:
        s.tables.insert(0, Table("GPU allocation and activity by month",
                                 ["Month", "GPU-hours allocated", "Share of card-hours", "Cards computing",
                                  "Mean utilization"], table,
                                 note=f"{cards} cards. Cards computing = GPU samples with utilization above 0. "
                                      "Jobs that did not request a GPU can use the cards where Slurm doesn't "
                                      "confine devices, so card activity is not the activity of GPU jobs."))
    elif d.gpu_months:
        rows = []
        for m, v in sorted(d.gpu_months.items()):
            rows.append([fmt.month(m), fmt.pct(v[1] / v[0]), fmt.pct(v[2] / v[0] / 100)])
            s.fact(f"card_active_share.{m}", f"{m}: share of GPU samples with the card computing", v[1] / v[0],
                   "share", source=GPU_SAMPLES, n=int(v[0]))
        s.tables.append(Table("GPU activity by month", ["Month", "Cards computing", "Mean utilization"], rows))
    # People in GPU partitions or asking for GPUs, over the whole period.
    gpu_people = set()
    hrs, cnt = defaultdict(Counter), defaultdict(Counter)
    month_people = defaultdict(set)
    for j in d.jobs:
        if j.gpu_request or any(k in ctx.gpu_tiers for k in j.pclasses):
            gpu_people.add(j.user)
            hrs[j.user][j.gpu_family] += calcs.gpu_hours(j)
            cnt[j.user][j.gpu_family] += 1
            if j.submit:
                month_people[calcs.month_key(j.submit)].add(j.user)
    s.fact("gpu_people", "people in GPU partitions or asking for GPUs", len(gpu_people), "people", people=True)
    counts = [len(month_people[m]) for m in ctx.full_months]
    if counts:
        s.fact("gpu_people_per_month_min", "people in GPU partitions or asking for GPUs, lowest month",
               min(counts), "people", people=True)
        s.fact("gpu_people_per_month_max", "people in GPU partitions or asking for GPUs, highest month",
               max(counts), "people", people=True)
    mains = Counter(calcs.primary_family(hrs[u], cnt[u]) for u in gpu_people)
    for f, n in mains.most_common():
        s.fact(f"gpu_people_by_family.{f}", f"people in GPU partitions or asking for GPUs, main application {f}",
               n, "people", people=True)
    reached = {j.user for j in d.jobs if any(n in ctx.gpu_nodes for n in j.nodes)} - gpu_people
    s.fact("gpu_nodes_without_gpu_use", "people whose jobs reached GPU nodes without asking for GPUs",
           len(reached), "people", people=True)
    if mains:
        ordered = sorted(mains.items(), key=lambda kv: (kv[0] == "unclassified", -kv[1]))
        note = (f"{ctx.pp(min(counts))}–{ctx.pp(max(counts))} in any one month. " if counts else "")
        note += (f"Another {ctx.pp(len(reached))} people's jobs reached the GPU nodes without asking for GPUs."
                 if reached else "No one else's jobs reached the GPU nodes.")
        s.tables.append(Table("People in GPU partitions or asking for GPUs, by main application",
                              ["Application", "People"], [[f, n] for f, n in ordered],
                              note=note, people_columns=[1]))
    share = s.get("allocation_share")
    if share is not None:
        fam = [(f.id.split(".", 2)[2], f.value) for f in s.facts if f.id.startswith("s07.gpu_hours_share_by_family.")]
        fam = [x for x in fam if x[0] != "unclassified"]
        top = max(fam, key=lambda x: x[1] or 0) if fam else None
        s.finding = (f"Since GPU accounting began, jobs held {fmt.pct(share)} of the {cards} cards' hours"
                     + (f"; {top[0]} took {fmt.pct(top[1])} of GPU-hours." if top else "."))
    else:
        s.finding = (f"{ctx.pp(len(gpu_people))} people worked in the GPU partitions or asked for GPUs; Slurm "
                     "recorded no "
                     "GPU allocation for the period.")
    return s


# 8 -----------------------------------------------------------------------------------

def s08_what_runs(ctx: Ctx) -> Section:
    d, cfg = ctx.d, ctx.cfg
    s = _section(8, "what_runs", "What runs", ctx, source=ctx.jobs_source)
    jobs = [j for j in ctx.started if j.cpus]
    if not jobs:
        return _not_measured(s, "no job in the period ran.")
    ch = {id(j): j.cpus * calcs.job_hours(j, d.t0, d.t1, True) for j in jobs}
    groups = [(t, [j for j in jobs if j.tier == t]) for t in ctx.tiers] + [("all", jobs)]
    lrows = []
    for t, js in groups:
        if not js:
            continue
        tot = sum(ch[id(j)] for j in js)
        short = sum(1 for j in js if j.elapsed < 3600) / len(js)
        day = sum(ch[id(j)] for j in js if j.elapsed > 86400) / tot if tot else float("nan")
        week = sum(ch[id(j)] for j in js if j.elapsed > 7 * 86400) / tot if tot else float("nan")
        multi = sum(ch[id(j)] for j in js if len(j.nodes) > 1) / tot if tot else float("nan")
        s.fact(f"jobs_under_1h.{t}", f"{t}: share of jobs shorter than an hour", short, "share", n=len(js))
        s.fact(f"core_hours_over_1day.{t}", f"{t}: share of core-hours in jobs longer than a day", day, "share")
        s.fact(f"core_hours_over_7days.{t}", f"{t}: share of core-hours in jobs longer than a week", week, "share")
        s.fact(f"core_hours_multi_node.{t}", f"{t}: share of core-hours in multi-node jobs", multi, "share")
        lrows.append([t, fmt.num(len(js)), fmt.pct(short), fmt.pct(day), fmt.pct(week), fmt.pct(multi)])
    s.tables.append(Table("Job lengths, jobs that ran in the period",
                          ["Node class", "Jobs", "Jobs under 1 h", "Core-hours in jobs > 1 day",
                           "Core-hours in jobs > 7 days", "Core-hours in multi-node jobs"], lrows))
    if cfg.families:
        by = defaultdict(Counter)
        how = Counter()
        for t, js in groups:
            for j in js:
                by[t][j.family] += ch[id(j)]
                if t == "all":
                    how[j.family_how or "none"] += 1
        fams = [f.name for f in cfg.families] + ["unclassified"]
        cols = [t for t, js in groups if js]
        frows = []
        for f in fams:
            row = [f]
            show = False
            for t in cols:
                tot = sum(by[t].values())
                v = by[t].get(f, 0) / tot if tot else float("nan")
                s.fact(f"family_share.{t}.{f}", f"{t}: share of core-hours, {f}", v, "share")
                show = show or (not fmt.missing(v) and v >= 0.01)
                row.append(fmt.pct(v))
            if show:
                frows.append(row)
        s.tables.append(Table("Share of core-hours by application", ["Application", *cols], frows,
                              note=f"From job names, then the end of the working directory, with report.toml's "
                                   f"patterns ({fmt.plural(how.get('name', 0), 'job')} matched by name, "
                                   f"{fmt.num(how.get('workdir', 0))} by directory, {fmt.num(how.get('none', 0))} "
                                   "unclassified)."))
        allv = s.get("family_share.all.unclassified")
        s.fact("unclassified_share", "share of core-hours whose application is unclassified", allv, "share")
    else:
        s.notes.append("No application families in report.toml: core-hours are not split by application.")
    short = s.get("jobs_under_1h.all")
    day = s.get("core_hours_over_1day.all")
    s.finding = (f"Most jobs are short ({fmt.pct(short)} under an hour), but jobs longer than a day take "
                 f"{fmt.pct(day)} of the core-hours.")
    return s


# 9 -----------------------------------------------------------------------------------

def s09_storage(ctx: Ctx) -> Section:
    d = ctx.d
    s = _section(9, "storage", "Storage", ctx, source=FS_SAMPLES)
    if not d.filesystems:
        return _not_measured(s, "no filesystem samples in this database (the disk collector).")
    rows, findings = [], []
    first = min(r[0] for v in d.filesystems.values() for r in v)
    last = max(r[0] for v in d.filesystems.values() for r in v)
    s.period = f"{fmt.month(first)} – {fmt.month(last)}"
    for raw, months in sorted(d.filesystems.items(), key=lambda kv: d.fs_labels.get(kv[0], kv[0])):
        path = d.fs_labels.get(raw, raw)
        series = [(m, used) for m, used, _, _ in months]
        cap = months[-1][2]
        used = months[-1][1]
        g, since, cleanup = calcs.storage_growth(series)
        full_in = calcs.months_to_full(cap, used, g)
        when = None
        if not math.isinf(full_in) and months[-1][3] is not None:
            when = months[-1][3] + timedelta(days=full_in * 30.4375)
        for m, u, tot, _ in months:
            s.fact(f"monthly_max_used.{path}.{m}", f"{path}, {m}: highest use", u, "bytes")
        s.fact(f"capacity.{path}", f"{path}: capacity", cap, "bytes")
        s.fact(f"used.{path}", f"{path}: highest use in {months[-1][0]}", used, "bytes")
        s.fact(f"used_share.{path}", f"{path}: share full", used / cap if cap else float("nan"), "share")
        s.fact(f"growth_per_month.{path}", f"{path}: growth per month since {since}", g, "bytes/month",
               kind=ESTIMATED, note=f"slope of the monthly highest use from {since}"
                                    + (f", after the cleanup of {cleanup}" if cleanup else ""))
        s.fact(f"months_to_full.{path}", f"{path}: months until full at that rate", full_in, "months",
               kind=PROJECTED)
        if cleanup:
            s.fact(f"cleanup_month.{path}", f"{path}: month of the last cleanup", cleanup, "month")
        rows.append([path, fmt.tb(cap), fmt.tb(used), fmt.pct(used / cap if cap else float("nan")),
                     (fmt.tb(g) + "/month") if not fmt.missing(g) else "–",
                     fmt.month(since) if since else "–",
                     (f"{full_in:.1f} months" if not math.isinf(full_in) else "not filling"),
                     (fmt.month(calcs.month_key(when), long=True) if when else "–")])
        if not fmt.missing(g) and g > 0 and not math.isinf(full_in):
            findings.append((full_in, path, g, since, used / cap if cap else None, when, cleanup))
    s.tables.append(Table("Filesystems: highest use per month, growth since the last cleanup",
                          ["Filesystem", "Capacity", "Used (latest month)", "Full", "Growth", "Since",
                           "Full in", "Around"], rows,
                          note="Decimal terabytes (1 TB = 10¹² bytes; df -h shows TiB). Growth is the slope of "
                               "each month's highest use after the last fall of more than 5% (a cleanup)."))
    if findings:
        full_in, path, g, since, share, when, cleanup = min(findings)
        s.finding = (f"{path} has grown {fmt.tb(g)} a month since {fmt.month(since)}"
                     + (", after a cleanup," if cleanup else "")
                     + f" and is {fmt.pct(share)} full: full in about {full_in:.1f} months"
                     + (f", around {fmt.month(calcs.month_key(when), long=True)}." if when else "."))
    else:
        s.finding = "No filesystem is growing towards full at its recent rate."
    return s


# 10 ----------------------------------------------------------------------------------

_OUTCOMES = ("COMPLETED", "FAILED", "TIMEOUT", "CANCELLED", "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED",
             "BOOT_FAIL", "DEADLINE")


def s10_reliability(ctx: Ctx) -> Section:
    d = ctx.d
    s = _section(10, "reliability", "Reliability", ctx, source=f"{SREPORT}; {NODE_SAMPLES}; {ctx.jobs_source}")
    ended = [j for j in d.jobs if j.ended and j.end is not None and d.t0 <= j.end < d.t1]
    if not ended and not d.usage_rows and not d.node_state_classes:
        return _not_measured(s, "no ended jobs, node samples or Slurm totals for the period.")
    parts = []
    if ended:
        n = len(ended)
        chs = Counter()
        cnt = Counter()
        for j in ended:
            k = j.state if j.state in _OUTCOMES else "other"
            cnt[k] += 1
            chs[k] += j.cpus * j.elapsed / 3600
        tot = sum(chs.values())
        rows = []
        for k in (*_OUTCOMES, "other"):
            if not cnt.get(k):
                continue
            s.fact(f"jobs_share.{k}", f"share of jobs that ended {k}", cnt[k] / n, "share", n=n)
            s.fact(f"core_hours_share.{k}", f"share of core-hours in jobs that ended {k}",
                   chs[k] / tot if tot else float("nan"), "share")
            rows.append([k, fmt.num(cnt[k]), fmt.pct(cnt[k] / n, 1), fmt.pct(chs[k] / tot if tot else float('nan'), 1)])
        s.tables.append(Table("How jobs ended, jobs that ended in the period",
                              ["Outcome", "Jobs", "Share of jobs", "Share of core-hours"], rows,
                              note="Cancelled jobs include those cancelled before they ran."))
        parts.append(f"{fmt.pct(cnt['FAILED'] / n)} of the jobs that ended failed, "
                     f"{fmt.pct(cnt['TIMEOUT'] / n)} hit their time limit and {fmt.pct(cnt['CANCELLED'] / n)} were "
                     "cancelled")
    months = [r for r in d.usage_rows if r.get("tres", "cpu") == "cpu"
              and r["month"] in calcs.months_between(d.t0, d.t1)]
    if months:
        down = sum(float(r.get("down_h") or 0) for r in months)
        rep = sum(float(r.get("reported_h") or 0) for r in months)
        share = down / rep if rep else float("nan")
        s.fact("down_share", "share of core-hours lost to nodes that were down (Slurm's totals)", share, "share",
               source=SREPORT)
        mrows = []
        for r in months:
            sh = float(r.get("down_h") or 0) / float(r["reported_h"]) if r.get("reported_h") else float("nan")
            s.fact(f"down_share.{r['month']}", f"{r['month']}: share of core-hours down", sh, "share", source=SREPORT)
            mrows.append([fmt.month(r["month"]), fmt.big(float(r.get("down_h") or 0)), fmt.pct(sh, 1)])
        s.tables.append(Table("Core-hours lost to down nodes, by month (Slurm's totals)",
                              ["Month", "Core-hours down", "Share of all core-hours"], mrows))
        parts.append(f"nodes were down for {fmt.pct(share, 1)} of the core-hours")
    if d.node_state_classes:
        # A node's share of samples down (or drained), times the period: gaps
        # in collection don't count as either.
        counts = defaultdict(Counter)
        for (n, m), c in d.node_state_classes.items():
            counts[n].update(c)
        per_node = {}
        for n, c in counts.items():
            total = sum(c.values())
            span = d.node_spans.get(n)
            # The hours this node's samples cover in the period (one more
            # interval than first to last).
            covered = ((span[1] - span[0]).total_seconds() / 3600 + d.node_sample_hours.get(n, 0.0)
                       if span else 0.0)
            covered = min(covered, d.hours)
            if total and covered:
                per_node[n] = [c.get("down", 0) / total * covered, c.get("drained", 0) / total * covered]
        nrows = []
        for t in ctx.tiers:
            nodes = d.tier_nodes([t])
            down = sum(per_node[n][0] for n in nodes if n in per_node)
            drained = sum(per_node[n][1] for n in nodes if n in per_node)
            long_out = sum(1 for n in nodes if n in per_node and sum(per_node[n]) > 7 * 24)
            s.fact(f"node_days_down.{t}", f"{t}: node-days down", down / 24, "node-days", source=NODE_SAMPLES)
            s.fact(f"node_days_drained.{t}", f"{t}: node-days drained", drained / 24, "node-days", source=NODE_SAMPLES)
            s.fact(f"nodes_out_over_7_days.{t}", f"{t}: nodes out of service more than 7 days in all", long_out,
                   "nodes", source=NODE_SAMPLES)
            nrows.append([t, fmt.num(down / 24, 1), fmt.num(drained / 24, 1), long_out])
        span = (f"{fmt.day(d.node_state_span[0])} – {fmt.day(d.node_state_span[1])}"
                if d.node_state_span else "")
        s.tables.append(Table("Nodes out of service, from NØMAÐ's node samples",
                              ["Node class", "Node-days down", "Node-days drained", "Nodes out > 7 days in all"],
                              nrows, note="Down includes not responding; drained nodes take no new jobs. Over "
                                          f"the time the node samples cover ({span}), not the whole period."
                              if span and d.node_state_span[0] > d.t0 + timedelta(days=1) else
                              "Down includes not responding; drained nodes take no new jobs."))
        out_h = sum((b - a).total_seconds() / 3600 for a, b in d.outages)
        s.fact("outages", "whole-cluster outages (90% of nodes or more down)", len(d.outages), "outages",
               source=NODE_SAMPLES)
        s.fact("outage_hours", "hours of whole-cluster outages", out_h, "hours", source=NODE_SAMPLES)
        if d.outages:
            months_out = sorted({calcs.month_key(a) for a, _ in d.outages})
            s.notes.append(f"{len(d.outages)} whole-cluster outage(s), {out_h:.0f} hours in all, in "
                           f"{', '.join(fmt.month(m) for m in months_out)}.")
    s.finding = (parts[0][0].upper() + parts[0][1:] + ("; " + parts[1] if len(parts) > 1 else "") + ".") if parts else \
        "Node samples only: see the table."
    return s


# 11 ----------------------------------------------------------------------------------

def s11_policy(ctx: Ctx, s01: Section, s06: Section) -> Section:
    d = ctx.d
    s = _section(11, "policy", "Policy indicators", ctx, source=ctx.jobs_source)
    jobs = list(ctx.started)
    if not jobs:
        return _not_measured(s, "no job in the period ran.")
    rows, parts = [], []
    ch = {id(j): j.cpus * calcs.job_hours(j, d.t0, d.t1, True) for j in jobs}
    known = [j for j in jobs if j.time_limit_known]
    if not known:
        s.notes.append("Time limits are not recorded for these jobs (records from before nomad 1.7.42 that "
                       "`nomad import sacct` didn't complete): no-limit share not measured.")
    if known:
        none = [j for j in known if j.req_time_s is None]
        tot = sum(ch[id(j)] for j in known)
        sj = len(none) / len(known)
        sc = sum(ch[id(j)] for j in none) / tot if tot else float("nan")
        s.fact("no_time_limit_jobs", "share of jobs with no time limit", sj, "share", n=len(known))
        s.fact("no_time_limit_core_hours", "share of core-hours in jobs with no time limit", sc, "share")
        rows.append(["Jobs with no time limit", fmt.pct(sj), fmt.pct(sc)])
        parts.append(f"{fmt.pct(sj)} of jobs ran with no time limit")
        if len(known) < len(jobs):
            s.notes.append(f"Time limits are known for {len(known)} of {len(jobs)} jobs that ran (the rest came "
                           "from records without one).")
    rooted = [j for j in jobs if j.work_root]
    if rooted:
        home = [j for j in rooted if j.work_root == "/home"]
        tot = sum(ch[id(j)] for j in rooted)
        sj = len(home) / len(rooted)
        sc = sum(ch[id(j)] for j in home) / tot if tot else float("nan")
        s.fact("from_home_jobs", "share of jobs that ran from /home", sj, "share", n=len(rooted))
        s.fact("from_home_core_hours", "share of core-hours in jobs that ran from /home", sc, "share")
        rows.append(["Jobs running from /home", fmt.pct(sj), fmt.pct(sc)])
        parts.append(f"{fmt.pct(sj)} ran from /home rather than scratch")
        if len(rooted) < len(jobs):
            s.notes.append(f"Working directories are known for {len(rooted)} of {len(jobs)} jobs that ran.")
    if ctx.gpu_nodes:
        a = max(ctx.gpu_start, d.t0) if ctx.gpu_start else None
        if a is not None:
            on_gpu = [j for j in jobs if j.start and j.start >= a and any(n in ctx.gpu_nodes for n in j.nodes)]
            cpu_only = [j for j in on_gpu if not j.gpu_request]
            tot = sum(ch[id(j)] for j in on_gpu)
            sc = sum(ch[id(j)] for j in cpu_only) / tot if tot else float("nan")
            s.fact("cpu_only_on_gpu_nodes_jobs", "jobs on GPU nodes that asked for no GPU", len(cpu_only), "jobs",
                   period=f"{fmt.day(a)} – {fmt.day(d.t1 - timedelta(seconds=1))}")
            s.fact("cpu_only_on_gpu_nodes_people", "people whose jobs on GPU nodes asked for no GPU",
                   len({j.user for j in cpu_only}), "people", people=True)
            s.fact("cpu_only_on_gpu_nodes_core_hours", "share of GPU nodes' core-hours in jobs that asked for no GPU",
                   sc, "share")
            rows.append(["Jobs on GPU nodes that asked for no GPU (since GPU accounting)",
                         fmt.num(len(cpu_only)), fmt.pct(sc)])
    iu = s01.get("institutional_utilization")
    cu = s01.get("condo_utilization")
    if iu is not None and cu is not None:
        rows.append(["Utilization, institutional / condo nodes", f"{fmt.pct(iu)} / {fmt.pct(cu)}", ""])
        s.notes.append("Condo nodes are reserved for their owners: their lower utilization reflects policy, "
                       "not a lack of demand.")
    ratios = [(f.id.split(".")[-1], f.value) for f in s06.facts
              if f.id.startswith("s06.request_to_use.") and f.value is not None]
    if ratios:
        rows.append(["Memory requested ÷ used, by node class",
                     ", ".join(f"{t} {v:.0f}×" for t, v in ratios), ""])
    s.tables.append(Table("Indicators that argue for no-cost changes", ["Indicator", "Jobs (or value)",
                                                                        "Core-hours"], rows))
    s.finding = (parts[0][0].upper() + parts[0][1:] + ("; " + "; ".join(parts[1:]) if len(parts) > 1 else "") + ".") \
        if parts else "Time limits and working directories are not recorded for these jobs."
    return s


# 12 ----------------------------------------------------------------------------------

def s12_departments(ctx: Ctx) -> Section:
    d = ctx.d
    s = _section(12, "departments", "Departments and schools", ctx, source=f"{ctx.jobs_source}; user map")
    if not d.user_map_given:
        return _not_measured(s, "no user map (--user-map FILE: user, department, school). "
                                "`nomad usage-report people` writes the period's usernames to fill in.")
    ch = Counter()
    people = defaultdict(set)
    sch_ch, sch_people = Counter(), defaultdict(set)
    for j in d.jobs:
        h = j.cpus * calcs.job_hours(j, d.t0, d.t1, True) if j.started else 0.0
        dept = j.dept or "not in the map"
        school = j.school or "not in the map"
        ch[dept] += h
        people[dept].add(j.user)
        sch_ch[school] += h
        sch_people[school].add(j.user)
    tot = sum(ch.values())
    for title, c, p, key in (("By school", sch_ch, sch_people, "school"), ("By department", ch, people, "department")):
        rows = []
        for k, v in c.most_common():
            s.fact(f"{key}_people.{k}", f"people: {k}", len(p[k]), "people", people=True)
            s.fact(f"{key}_core_hours.{k}", f"core-hours: {k}", v, "core-hours")
            rows.append([k, len(p[k]), fmt.big(v), fmt.pct(v / tot if tot else float("nan"))])
        s.tables.append(Table(title, [key.capitalize(), "People", "Core-hours", "Share"], rows, people_columns=[1]))
    s.fact("unmapped_people", "people not in the user map", d.unmapped_people, "people", people=True)
    top = sch_ch.most_common(1)
    s.finding = (f"{top[0][0]} used {fmt.pct(top[0][1] / tot if tot else float('nan'))} of the core-hours."
                 if top and top[0][0] != "not in the map" else
                 f"{ctx.pp(d.unmapped_people)} people are not in the user map.")
    return s


# 13 ----------------------------------------------------------------------------------

def s13_teaching(ctx: Ctx) -> Section:
    d = ctx.d
    s = _section(13, "teaching_server", "Teaching server", ctx, source="NØMAÐ interactive sessions")
    if not d.teaching_site:
        return _not_measured(s, "no teaching server given (--teaching SITE).")
    if not d.teaching:
        return _not_measured(s, f"no interactive sessions recorded at {d.teaching_site} in the period "
                                "(the interactive collector).")
    t = d.teaching
    rows = []
    total = sum(r["samples"] for r in t["by_type"]) or 1
    for r in t["by_type"]:
        typ = r["type"]
        s.fact(f"people.{typ}", f"{typ}: people", r["people"], "people", people=True)
        s.fact(f"session_share.{typ}", f"{typ}: share of session samples", r["samples"] / total, "share")
        s.fact(f"idle_share.{typ}", f"{typ}: share of samples idle", r["idle"], "share")
        s.fact(f"cpu_percent.{typ}", f"{typ}: mean CPU per session", r["cpu_pct"], "percent of a core")
        s.fact(f"memory_gb.{typ}", f"{typ}: mean memory per session", (r["mem_mb"] or 0) / 1024, "GB")
        rows.append([typ, r["people"], fmt.pct(r["samples"] / total), fmt.pct(r["idle"]),
                     f"{fmt.num(r['cpu_pct'])}%", fmt.gb((r["mem_mb"] or 0) / 1024),
                     fmt.gb((r["max_mem_mb"] or 0) / 1024)])
    s.fact("people", "people with sessions", t["people"], "people", people=True)
    s.tables.append(Table(f"Sessions by type ({d.teaching_site})",
                          ["Session type", "People", "Share of sessions", "Idle", "CPU per session",
                           "Memory per session", "Largest"], rows, people_columns=[1]))
    conc = t.get("concurrency")
    if conc:
        s.fact("peak_sessions", "most sessions at once", conc["peak"], "sessions")
        s.fact("mean_sessions", "mean sessions at once", conc["mean"], "sessions")
        s.finding = (f"{ctx.pp(t['people'])} people used the teaching server, with up to "
                     f"{fmt.num(conc['peak'])} sessions at once.")
    else:
        s.finding = f"{ctx.pp(t['people'])} people used the teaching server."
    return s


# 14 ----------------------------------------------------------------------------------

def s14_capacity(ctx: Ctx, s01: Section, s02: Section) -> Section:
    d, cap = ctx.d, ctx.cfg.capacity
    s = _section(14, "capacity", "Capacity and projection", ctx, source="Sections 1 and 2; report.toml")
    cores = s01.get("institutional_cores")
    ch = s01.get("institutional_core_hours" if cap.base == "clipped" else "institutional_core_hours_whole")
    if not cores or ch is None:
        return _not_measured(s, "the institutional nodes' cores or core-hours are unknown.")
    days = ctx.days
    base = calcs.annualize(ch, days)
    other_ch = s01.get("institutional_core_hours_whole" if cap.base == "clipped" else "institutional_core_hours")
    other = calcs.annualize(other_ch, days) if other_ch is not None else None
    base_year = (d.t1 - timedelta(seconds=1)).year
    s.fact("base", f"institutional demand, core-hours a year ({cap.base} core-hours × 365 / {days:.0f} days)", base,
           "core-hours/year", kind=MEASURED)
    if other is not None:
        s.fact("base_other", "the same demand with the other way of counting jobs", other, "core-hours/year",
               note="each job counted whole" if cap.base == "clipped" else "clipped to the period")
    if cap.growth:
        rates = dict(cap.growth)
        rate_src = "report.toml"
    elif s02.get("cagr") is not None:
        cg = s02.get("cagr")
        rates = {"low": s02.get("growth_low") if s02.get("growth_low") is not None else cg, "central": cg,
                 "high": s02.get("growth_high") if s02.get("growth_high") is not None else cg}
        rate_src = "Slurm's totals, full years (lowest, compound, highest)"
    else:
        rates = None
        rate_src = ""
    lines = [("today's institutional nodes", calcs.capacity_core_hours(cores, cap.hours_per_year, 1.0, cap.practical))]
    planned_total = 0.0
    for p in cap.planned:
        c = calcs.capacity_core_hours(p.cores, cap.hours_per_year, p.weight, cap.practical)
        lines.append((p.label, c))
        planned_total += c
    if cap.planned:
        lines.append(("today's and planned together", lines[0][1] + planned_total))
    s.fact("floor_new_cores", f"new cores that do the work of today's {cores} (at {cap.core_weight}× per core)",
           calcs.floor_new_cores(cores, cap.core_weight), "cores", kind=ESTIMATED)
    rows = []
    crossings = {}
    for label, c in lines:
        key = label.replace("'", "").replace(" ", "_")
        s.fact(f"capacity.{key}", f"practical capacity: {label}", c, "core-hours/year", kind=ESTIMATED)
        row = [label, fmt.millions(c)]
        if rates:
            for r in ("low", "central", "high"):
                y = calcs.year_capacity_crossed(base, rates[r], c, base_year, cap.horizon_years)
                crossings[(label, r)] = y
                s.fact(f"crossed.{key}.{r}", f"year demand passes {label} at {r} growth", y, "year", kind=PROJECTED)
                row.append(str(y) if y else f"after {base_year + cap.horizon_years}")
        rows.append(row)
    cols = ["Capacity line", "Practical core-hours a year"] + (
        [f"Passed at {fmt.pct(rates[r])} ({r})" for r in ("low", "central", "high")] if rates else [])
    s.tables.append(Table("Practical capacity and the year demand passes it", cols, rows,
                          note=f"Practical capacity = cores × weight × {cap.hours_per_year:.0f} h × "
                               f"{fmt.pct(cap.practical)}: above that, queues form. Demand starts at "
                               f"{fmt.millions(base)} core-hours in {base_year}."))
    if rates:
        for r in ("low", "central", "high"):
            s.fact(f"growth.{r}", f"{r} growth rate", rates[r], "share", kind=ESTIMATED, source=rate_src)
        prow = []
        for y in range(base_year, base_year + min(cap.horizon_years, 8) + 1):
            vals = [base * (1 + rates[r]) ** (y - base_year) for r in ("low", "central", "high")]
            for r, v in zip(("low", "central", "high"), vals):
                s.fact(f"projection.{r}.{y}", f"projected demand {y}, {r}", v, "core-hours", kind=PROJECTED)
            prow.append([str(y), *(fmt.millions(v) for v in vals)])
        s.tables.append(Table("Projected institutional demand (core-hours a year)",
                              ["Year", *(f"{r} ({fmt.pct(rates[r])})" for r in ("low", "central", "high"))], prow))
        y = crossings.get((lines[0][0], "central"))
        when = ("already" if y == base_year else f"in {y}" if y else f"after {base_year + cap.horizon_years}")
        s.finding = (f"At {fmt.pct(rates['central'])} a year from {fmt.millions(base)} core-hours, demand "
                     + ("already exceeds" if y == base_year else "passes")
                     + " the practical capacity of today's institutional nodes"
                     + ("" if y == base_year else f" {when}")
                     + (f", and {lines[-1][0]} in {crossings.get((lines[-1][0], 'central'))}"
                        if len(lines) > 1 and crossings.get((lines[-1][0], 'central')) else "") + ".")
        if other is not None:
            y2 = calcs.year_capacity_crossed(other, rates["central"], lines[0][1], base_year, cap.horizon_years)
            if y2 != y:
                s.notes.append(f"Counting each job of the period whole instead (as the October 2026 plan did) "
                               f"gives {fmt.millions(other)} core-hours a year, and today's nodes are passed in "
                               f"{y2 or 'a later year'} at the central rate."
                               if cap.base == "clipped" else
                               f"Clipping jobs to the period gives {fmt.millions(other)} core-hours a year, and "
                               f"today's nodes are passed in {y2 or 'a later year'} at the central rate.")
    else:
        s.status = PARTIAL
        s.finding = (f"Today's institutional nodes give {fmt.millions(lines[0][1])} practical core-hours a year "
                     f"against a demand of {fmt.millions(base)}; no growth rate is known (Slurm's totals or "
                     "report.toml), so nothing is projected.")
    return s


# ------------------------------------------------------------------------------------

def coverage(ctx: Ctx, report: Report) -> None:
    d = ctx.d
    secs = {s.number: s for s in report.sections}
    gaps = [m for m in ctx.full_months if not d.jobs_by_month.get(m)]
    job_cov = (f"jobs submitted from {fmt.day(min(j.submit for j in d.jobs if j.submit))}"
               if any(j.submit for j in d.jobs) else "none")
    job_gaps = ", ".join(fmt.month(m) for m in gaps) if gaps else "none found"
    ns = (f"{fmt.day(d.node_state_span[0])} – {fmt.day(d.node_state_span[1])}" if d.node_state_span else "none")
    gs = (f"{fmt.day(d.gpu_stats_span[0])} – {fmt.day(d.gpu_stats_span[1])}" if d.gpu_stats_span else "none")
    cpu_rows = [r for r in d.usage_rows if r.get("tres", "cpu") == "cpu"]
    gpu_rows = [r for r in d.usage_rows if r.get("tres") == "gres/gpu"]
    sr = f"{fmt.month(cpu_rows[0]['month'])} – {fmt.month(cpu_rows[-1]['month'])}" if cpu_rows else "none"
    gstart = (fmt.day(ctx.gpu_start) + (" (first job with GPUs)" if ctx.gpu_start_detected else ""))\
        if ctx.gpu_start else "none in the period"
    measured = sum(1 for j in ctx.started if j.cpu_pct is not None)
    fs = ", ".join(sorted(d.fs_labels.get(p, p) for p in d.filesystems)) or "none"
    def kind(n, default=MEASURED):
        s = secs.get(n)
        return NOT_MEASURED if s and s.status == NOT_MEASURED else default
    rows = [
        (1, ctx.jobs_source, job_cov, job_gaps, kind(1)),
        (2, SREPORT, sr, "GPU rows " + (f"from {fmt.month(gpu_rows[0]['month'])}" if gpu_rows else "none"), kind(2)),
        (3, ctx.jobs_source + "; node inventory", job_cov, "partition classes from report.toml and the nodes", kind(3)),
        (4, ctx.jobs_source, job_cov, f"{fmt.plural(d.set_aside.get('never started', 0), 'job')} never started "
         "(no wait)", kind(4)),
        (5, f"{NODE_SAMPLES}; {JOB_METRICS}", f"node samples {ns}",
         f"CPU metrics for {fmt.num(measured)} of {fmt.num(len(ctx.started))} jobs that ran", kind(5)),
        (6, JOB_METRICS, f"{fmt.num(sum(1 for j in ctx.started if j.peak_mem_gb is not None))} jobs with a memory "
         "peak",
         "jobs without NØMAÐ metrics are not in it", kind(6)),
        (7, f"{ctx.jobs_source}; {GPU_SAMPLES}", f"GPU allocations from {gstart}; GPU samples {gs}",
         "no GPU-hours before GPU accounting", kind(7)),
        (8, ctx.jobs_source, job_cov, "applications from job names and directories", kind(8, ESTIMATED)),
        (9, FS_SAMPLES, fs, "growth after the last cleanup", kind(9, ESTIMATED)),
        (10, f"{SREPORT}; {NODE_SAMPLES}; {ctx.jobs_source}", f"node samples {ns}", "", kind(10)),
        (11, ctx.jobs_source, job_cov, "time limits and directories where recorded", kind(11)),
        (12, "user map", "given" if d.user_map_given else "none", f"{ctx.pp(d.unmapped_people)} people not in the map"
         if d.user_map_given else "", kind(12)),
        (13, "NØMAÐ interactive sessions", d.teaching_site or "none", "", kind(13)),
        (14, "Sections 1 and 2; report.toml", "", "", kind(14, PROJECTED)),
    ]
    for n, src, cov, gap, k in rows:
        title = secs[n].title if n in secs else str(n)
        report.coverage.append(Coverage(f"{n}. {title}", src, cov, gap, k))


def set_aside(ctx: Ctx, report: Report) -> None:
    d = ctx.d
    if d.excluded_accounts:
        report.set_aside.append(SetAside("accounts that are not people (report.toml exclude_users)",
                                         d.excluded_accounts, f"{d.excluded_jobs} jobs"))
    for what, n in sorted(d.set_aside.items()):
        note = ""
        if what == "outside the tier map":
            note = f"{fmt.big(d.outside_map_core_hours)} core-hours in the period"
        elif what == "start corrected":
            note = "a start after the end, before the submission, or off end − elapsed: taken from end − elapsed"
        elif what == "never started":
            note = "no wait, no core-hours; counted among people"
        report.set_aside.append(SetAside(f"jobs {what}" if not what.startswith(("memory", "unreadable"))
                                         else what, n, note))


def assumptions(ctx: Ctx, report: Report, s14: Section) -> None:
    cap = ctx.cfg.capacity
    a = report.assumptions
    a.append(f"Core-hours = allocated CPUs × elapsed hours, split evenly across a job's nodes, clipped to the "
             f"period ({ctx.period}).")
    a.append(f"Waiting means more than {WAIT_HOURS:.0f} hours from submission to start.")
    a.append(f"A new-generation core does the work of {cap.core_weight} of today's (range "
             f"{cap.core_weight_range[0]}–{cap.core_weight_range[1]}).")
    a.append(f"Practical capacity is {fmt.pct(cap.practical)} of all core-hours: above it, queues form.")
    lo, ce, hi = (s14.get("growth.low"), s14.get("growth.central"), s14.get("growth.high"))
    if ce is not None:
        a.append(f"Growth {fmt.pct(lo)} / {fmt.pct(ce)} / {fmt.pct(hi)} a year (low / central / high), from "
                 + ("report.toml." if cap.growth else "the full years of Slurm's totals (lowest year, compound "
                    "rate, highest year)."))
    if ctx.gpu_start:
        a.append(f"GPU accounting from {fmt.day(ctx.gpu_start)}"
                 + (" (the first job with GPUs in its allocation)." if ctx.gpu_start_detected else " (report.toml)."))
    a.append("Memory in GB of 1,024 MB as Slurm counts it; storage in decimal TB.")
    if ctx.cfg.families or ctx.cfg.gpu_families:
        a.append("Applications are classified from job names and the end of working directories by report.toml's "
                 "patterns, first match wins; the unclassified share is shown.")


def build(d: Data, *, produced: str, nomad_version: str) -> Report:
    ctx = Ctx(d)
    src = d.source + (f"; {d.db_label}" if d.db_label and d.source != "NØMAÐ job records" else
                      (f" ({d.db_label})" if d.db_label else ""))
    report = Report(cluster=d.cfg.name, period_from=d.t0.isoformat(), period_to=d.t1.isoformat(),
                    produced=produced, nomad_version=nomad_version, source=src)
    s01 = s01_headline(ctx)
    s02 = s02_load(ctx)
    s06 = s06_memory(ctx)
    secs = [s01, s02, s03_who(ctx), s04_waits(ctx), s05_held(ctx), s06, s07_gpus(ctx), s08_what_runs(ctx),
            s09_storage(ctx), s10_reliability(ctx), s11_policy(ctx, s01, s06), s12_departments(ctx),
            s13_teaching(ctx)]
    s14 = s14_capacity(ctx, s01, s02)
    secs.append(s14)
    for s in secs:
        if s.finding:
            s.finding = s.finding[0].upper() + s.finding[1:]
    report.sections = sorted(secs, key=lambda s: s.number)
    coverage(ctx, report)
    set_aside(ctx, report)
    assumptions(ctx, report, s14)
    if not d.cfg.has_tier_map:
        report.assumptions.insert(0, "No tier map in report.toml: every node counts as one institutional tier, "
                                     "\"all nodes\". `nomad usage-report init` drafts one.")
    return report

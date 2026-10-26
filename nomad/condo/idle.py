# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Idle condo capacity while jobs that would have fit waited elsewhere.

For each condo and month, from the node readings (node_state, every
collection) and the jobs:

- **Idle cores:** free cores (CPUTot - CPUAlloc) on the condo's nodes while in
  service, as core-hours, as the monthly condo report counts them. **Open**:
  those on nodes that take jobs (not draining, reserved or planned for a
  job); only open cores can take a waiting job. Each reading stands for the
  time until the next one, at most twice the usual interval (never less than
  10 minutes).
- **Waiting elsewhere:** jobs waiting at that moment in partitions that don't
  reach the condo's nodes (not the condo's own, not a guest partition over
  it): jobs the partition setup kept off those nodes. Once a job runs, Slurm
  records only the partition it ran in, so a job that also listed a guest
  partition while it waited, and ran elsewhere, is counted.
- **Waiting for the cluster:** since squeue last began showing the job waiting
  for Resources or Priority (or for its partition's nodes, down) without a
  break; a job held by its own limits (QOS, association, dependency, a hold)
  waited for those, and one never seen waiting for the cluster is left out.
  Jobs submitted in the month that started after it count too. nomad records this from 1.18 on; for older jobs it isn't known, they
  count from when they could start (Eligible, else submission), and the
  figures say how many there were.
- **Would have fit:** one node asked for, whose cores, memory and GPUs were
  free on one of the condo's nodes at that moment, with a time limit within
  the condo policy's guest_max_days (a job with no limit stored counts by how
  long it ran, once it ended; one still running or that never ran is left
  out). For a job sacct hasn't reported (never started, or not yet) nomad has
  only what squeue showed: its memory is read per core (squeue's figure is per
  core under --mem-per-cpu, so the larger reading), and GPUs asked for with
  --gpus aren't seen.
- **Open core-hours a waiting job could have used:** at each reading, the
  waiting jobs that would have fit are placed on the free nodes (first fit),
  the longest-waiting first, and the cores placed are counted. It is
  the opportunity at each moment, not a simulation of what would have
  happened: a job placed at one reading is placed again at the next if it was
  in fact still waiting. Each condo is measured on its own: the same waiting
  job may count at several, so condos' figures don't add up.
- **Jobs that could have started earlier:** each such job's first reading at
  which it would have been placed, and how much earlier than its real start
  (or than when it was given up, or the month's end, still waiting) that was,
  when at least 10 minutes. Two jobs counted here may have wanted the same
  free cores later on, so the hours are at most. The cluster's line counts
  each job once.
- **Idle stretches:** how long the condo's nodes stayed wholly idle and open at
  a time (taking jobs, no core allocated), as shares of the idle node-hours in
  stretches of at least 2, 6, 12 and 24 hours, and the time-weighted median
  (half the idle node-hours were in stretches at least that long). Long
  stretches are the ones guest jobs could use without being pushed off.

No job or person is named; condos are named as report.toml names them
(``--mask`` for stand-ins).
"""
from __future__ import annotations

import heapq
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from nomad.condo import discover
from nomad.condo.config import Condo, Settings
from nomad.condo.figures import (DEFAULT_SAMPLE, WAITING_AT_MOST, _LIVE, _WAITING, Job, _int, _num,
                                 load_jobs, month_bounds, month_name, node_class)
from nomad.db.jobkeys import waiting_for_cluster
from nomad.facts._common import chunks, columns, parse_time

STRETCH_HOURS = (2, 6, 12, 24)
# Earlier by less than this is scheduling, not waiting.
MIN_EARLIER = timedelta(minutes=10)
# A node in one of these takes no new job, whatever its free cores.
_NO_NEW_JOBS = ("DRAIN", "DRNG", "RESERVED", "RESV", "PLANNED", "MAINT")

def takes_jobs(state) -> bool:
    s = str(state or "").upper()
    return not any(k in s for k in _NO_NEW_JOBS)


@dataclass(slots=True)
class Waiting:
    job: Job
    since: datetime
    until: datetime
    cpus: int
    mem_mb: float | None
    gpus: int
    reason_known: bool
    n: int = 0                       # its place in the longest-waiting order


@dataclass
class CondoIdle:
    condo: str
    label: str
    nodes: int = 0
    cores: int = 0
    read_hours: float = 0.0
    in_service_core_hours: float = 0.0
    idle_core_hours: float = 0.0           # free cores in service, as the monthly report counts them
    open_core_hours: float = 0.0           # ... on nodes taking jobs (not draining, reserved, planned)
    usable_core_hours: float = 0.0         # open cores a waiting job could have used
    hours_with_taker: float = 0.0          # hours with at least one such job
    waiting_jobs: int = 0                  # waiting elsewhere in the month, single-node etc.
    earlier_jobs: int = 0
    earlier_hours: list = field(default_factory=list)
    reason_unknown: int = 0                # of earlier_jobs, waiting reason not recorded
    left_out: dict = field(default_factory=lambda: {"several_nodes": 0, "too_long": 0, "no_time_limit": 0,
                                                    "own_limits": 0, "too_big": 0})
    idle_node_hours: float = 0.0
    stretches: list = field(default_factory=list)   # hours of each wholly idle stretch
    earlier_keys: set = field(default_factory=set)  # (site, job) counted in earlier_jobs

    def as_dict(self) -> dict:
        hrs = sorted(self.earlier_hours)
        total_idle = sum(self.stretches)
        share = {}
        for h in STRETCH_HOURS:
            long = sum(s for s in self.stretches if s >= h - 1e-6)   # sums of readings
            share[str(h)] = round(100.0 * long / total_idle, 1) if total_idle else None
        median = None
        if total_idle:
            acc = 0.0
            for s in sorted(self.stretches, reverse=True):
                acc += s
                if acc >= total_idle / 2:
                    median = round(s, 1)
                    break
        return {
            "condo": self.condo, "label": self.label, "nodes": self.nodes, "cores": self.cores,
            "read_hours": round(self.read_hours, 1),
            "idle_core_hours": round(self.idle_core_hours, 1),
            "idle_pct": (round(100.0 * self.idle_core_hours / self.in_service_core_hours, 1)
                         if self.in_service_core_hours else None),
            "open_core_hours": round(self.open_core_hours, 1),
            "usable_core_hours": round(self.usable_core_hours, 1),
            "usable_pct_of_open": (round(100.0 * self.usable_core_hours / self.open_core_hours, 1)
                                   if self.open_core_hours else None),
            "hours_with_taker": round(self.hours_with_taker, 1),
            "waiting_jobs": self.waiting_jobs,
            "earlier_jobs": self.earlier_jobs,
            "earlier_hours_median": round(hrs[len(hrs) // 2], 1) if hrs else None,
            "earlier_hours_total": round(sum(hrs), 1),
            "reason_unknown": self.reason_unknown,
            "left_out": dict(self.left_out),
            "idle_node_hours": round(self.idle_node_hours, 1),
            "stretch_share_pct": share,
            "stretch_median_hours": median,
        }


def _rounds(conn, site, nodes, t0, t1):
    """[(time, {node: (state, cores, alloc, mem_total, mem_alloc)})] in order."""
    cols = columns(conn, "node_state")
    if not nodes or not {"node_name", "timestamp"} <= cols:
        return []
    pick = ", ".join(c if c in cols else f"NULL AS {c}"
                     for c in ("state", "cpus_total", "cpus_alloc", "memory_total_mb", "memory_alloc_mb"))
    where = " AND source_site = ?" if site is not None and "source_site" in cols else ""
    by_time = defaultdict(dict)
    for part in chunks(sorted(nodes), 400):
        sql = (f"SELECT timestamp, node_name, {pick} FROM node_state WHERE timestamp >= ? AND timestamp < ?"
               f"{where} AND node_name IN ({','.join('?' * len(part))})")
        args = [t0.isoformat(), t1.isoformat()] + ([site] if where else []) + list(part)
        for ts, node, st, cores, alloc, mt, ma in conn.execute(sql, args):
            t = parse_time(ts)
            if t is not None:
                by_time[t][node] = (st, _int(cores), _int(alloc), _num(mt), _num(ma))
    return sorted(by_time.items())


def _weights(times, t1) -> list[float]:
    """Seconds each round stands for: until the next, at most twice the usual
    gap (never less than twice DEFAULT_SAMPLE); the last, half of that."""
    if not times:
        return []
    gaps = sorted((b - a).total_seconds() for a, b in zip(times, times[1:]))
    usual = gaps[len(gaps) // 2] if gaps else DEFAULT_SAMPLE.total_seconds()
    cap = max(2 * usual, 2 * DEFAULT_SAMPLE.total_seconds())
    out = [min((b - a).total_seconds(), cap) for a, b in zip(times, times[1:])]
    out.append(max(0.0, min(cap / 2, (t1 - times[-1]).total_seconds())))
    return out


def _candidates(out: CondoIdle, jobs, reach, biggest, t0, t1, now, guest_max) -> list[Waiting]:
    """The jobs waiting elsewhere in the month that a condo node could have
    taken; what is left out is counted in ``out.left_out``."""
    waiting = []
    seen = set()
    for j in jobs:
        if j.ref is None or not j.partitions or (reach & set(j.partitions)):
            continue
        if (j.site, j.job_id) in seen:
            continue
        if j.start is not None:
            until = j.start
        elif j.ended and j.end is not None:
            until = j.end                      # given up while it waited
        elif j.state in _WAITING or j.state in _LIVE or not j.state:
            until = now
        else:
            continue
        why = waiting_for_cluster(j.pending_reason)
        if j.ready_since is not None:
            since = j.ready_since              # waited for the cluster from then on
        elif why is False:
            out.left_out["own_limits"] += 1    # never seen waiting for the cluster
            continue
        else:
            since = j.ref
        since = max(since, j.ref, t0)
        until = min(until, j.ref + WAITING_AT_MOST, t1)
        if until <= since:
            continue
        if (j.req_nodes or 1) > 1 or len(j.nodes) > 1:
            out.left_out["several_nodes"] += 1
            continue
        need = j.req_time_s
        if need is None and j.ended and j.start is not None and j.end is not None:
            need = (j.end - j.start).total_seconds()     # no limit stored: what it ran
        if need is None:
            out.left_out["no_time_limit"] += 1
            continue
        if need > guest_max.total_seconds():
            out.left_out["too_long"] += 1
            continue
        cpus = max(1, j.cpus)
        if cpus > biggest:
            out.left_out["too_big"] += 1
            continue
        mem = j.req_mem_mb if (j.req_mem_mb or 0) > 0 else None
        if mem is not None and j.alloc_tres is None:
            mem *= cpus                        # squeue's figure (no sacct record yet) may be per core
        seen.add((j.site, j.job_id))
        waiting.append(Waiting(j, since, until, cpus, mem, max(0, j.gpus),
                               j.ready_since is not None or why is not None))
    # Longest waiting first: by when it began waiting for the cluster, then
    # by when it could start (every job waiting since before the month begins
    # it at the month's start).
    waiting.sort(key=lambda w: (w.since, w.job.ref, w.job.job_id))
    for i, w in enumerate(waiting):
        w.n = i
    return waiting


def condo_idle(conn, site, condo: Condo, nodes: set, found: discover.Found, jobs: list[Job],
               t0: datetime, t1: datetime, now: datetime, guest_max: timedelta) -> CondoIdle:
    out = CondoIdle(condo=condo.name, label=condo.label or condo.name)
    info = {n: found.nodes.get(n) for n in nodes}
    out.nodes = len(nodes)
    out.cores = sum(i.cores for i in info.values() if i)
    reach = {p for p, ns in found.partitions.items() if ns & nodes}
    if condo.part:
        reach.add(condo.part)
    biggest = max((i.cores for i in info.values() if i), default=0)
    waiting = _candidates(out, jobs, reach, biggest, t0, t1, now, guest_max)
    out.waiting_jobs = len(waiting)

    # Requests of one size (cores, memory, GPUs) wait in one line, longest
    # waiting first. At each reading the lines' oldest jobs are merged
    # longest waiting first; a line whose oldest doesn't fit is done for that
    # reading (none of its size would fit). Jobs join their line when they
    # begin waiting and leave it, lazily, when they stop.
    lines: dict = {}                          # size -> [live jobs, how many of them have stopped]
    stops: list = []                          # heap of (until, n, size)
    joined = 0
    active: set = set()

    # GPUs in use on each node: the running jobs', split over their nodes.
    running = sorted((j for j in jobs if j.start is not None and j.end is not None and j.gpus
                      and any(n in nodes for n in j.nodes)), key=lambda j: j.start)
    next_run = 0
    run_now: list = []

    rounds = _rounds(conn, site, nodes, t0, t1)
    weights = _weights([t for t, _ in rounds], t1)
    order = sorted(nodes)
    first_fit: dict = {}
    idle_run = defaultdict(float)             # node -> hours of the current wholly idle stretch
    for (t, readings), secs in zip(rounds, weights):
        h = secs / 3600.0
        out.read_hours += h
        while next_run < len(running) and running[next_run].start <= t:
            run_now.append(running[next_run])
            next_run += 1
        if run_now:
            run_now = [x for x in run_now if t < x.end]
        while joined < len(waiting) and waiting[joined].since <= t:
            w = waiting[joined]
            size = (w.cpus, w.mem_mb, w.gpus)
            lines.setdefault(size, [[], 0])[0].append(w)
            heapq.heappush(stops, (w.until, joined, size))
            active.add(size)
            joined += 1
        while stops and stops[0][0] <= t:
            _, _, size = heapq.heappop(stops)
            line = lines[size]
            line[1] += 1
            if line[1] * 2 > len(line[0]):
                line[0] = [w for w in line[0] if w.until > t]
                line[1] = 0
                if not line[0]:
                    active.discard(size)
        free = {}
        for n in order:
            r = readings.get(n)
            if r is None:
                if idle_run.get(n):
                    out.stretches.append(idle_run.pop(n))
                continue
            st, cores, alloc, mt, ma = r
            is_idle = alloc == 0
            if node_class(st, is_idle) != "in_service":
                if idle_run.get(n):
                    out.stretches.append(idle_run.pop(n))
                continue
            spare = max(0, cores - alloc)
            out.in_service_core_hours += cores * h
            out.idle_core_hours += spare * h
            if not takes_jobs(st):
                if idle_run.get(n):
                    out.stretches.append(idle_run.pop(n))
                continue
            out.open_core_hours += spare * h
            if is_idle and cores:
                idle_run[n] += h
                out.idle_node_hours += h
            elif idle_run.get(n):
                out.stretches.append(idle_run.pop(n))
            if spare:
                gpus_here = info.get(n).gpus if info.get(n) else 0
                used = sum(x.gpus / max(1, len(x.nodes)) for x in run_now if n in x.nodes)
                free[n] = [spare, (mt - ma) if mt is not None and ma is not None else None,
                           max(0.0, gpus_here - used)]
        if not free or not active:
            continue
        top_c = max(f[0] for f in free.values())
        mems = [f[1] for f in free.values()]
        top_m = None if any(m is None for m in mems) else max(mems)
        top_g = max(f[2] for f in free.values())
        heads = []
        for size in active:
            cpus, mem, gpus = size
            if cpus > top_c or gpus > top_g or (mem is not None and top_m is not None and mem > top_m):
                continue
            live = lines[size][0]
            i = _next_live(live, 0, t)
            if i is not None:
                heads.append((live[i].n, size, i))
        heapq.heapify(heads)
        placed = 0
        left = sum(f[0] for f in free.values())
        while heads and left > 0:
            _, size, i = heapq.heappop(heads)
            cpus, mem, gpus = size
            spot = None
            for n in order:
                f = free.get(n)
                if f and f[0] >= cpus and (mem is None or f[1] is None or f[1] >= mem) and f[2] >= gpus:
                    spot = f
                    break
            if spot is None:
                continue                       # nothing of this size fits now
            live = lines[size][0]
            w = live[i]
            spot[0] -= cpus
            if mem is not None and spot[1] is not None:
                spot[1] -= mem
            spot[2] -= gpus
            placed += cpus
            left -= cpus
            key_job = (w.job.site, w.job.job_id)
            if key_job not in first_fit:
                first_fit[key_job] = (t, w)
            j = _next_live(live, i + 1, t)
            if j is not None:
                heapq.heappush(heads, (live[j].n, size, j))
        if placed:
            out.usable_core_hours += placed * h
            out.hours_with_taker += h
    out.stretches += [v for v in idle_run.values() if v]
    for t, w in first_fit.values():
        real = w.job.start or w.until
        if real - t >= MIN_EARLIER:
            out.earlier_jobs += 1
            out.earlier_keys.add((w.job.site, w.job.job_id))
            out.earlier_hours.append((real - t).total_seconds() / 3600.0)
            if not w.reason_known:
                out.reason_unknown += 1
    return out


def _next_live(live, i, t):
    """The first job from ``i`` on still waiting at ``t`` (they joined by then)."""
    while i < len(live):
        if live[i].until > t:
            return i
        i += 1
    return None


def runaway_ids(conn, site) -> set:
    """The site's jobs Slurm no longer runs but its accounting lists as
    running or waiting (nomad.collectors.slurm record_runaways), by number."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(runaway_jobs)")}
        if "job_id" not in cols:
            return set()
        if "source_site" in cols:
            rows = conn.execute("SELECT job_id FROM runaway_jobs WHERE source_site IS ?", (site,))
        else:
            rows = conn.execute("SELECT job_id FROM runaway_jobs")
        return {str(r[0]) for r in rows}
    except sqlite3.Error:
        return set()


def build(conn, settings: Settings, month: str, *, nomad_config: dict | None = None,
          clusters: list[str] | None = None, only: list[str] | None = None,
          now: datetime | None = None) -> dict:
    """Every condo's idle-while-waiting figures for ``month``, by cluster.
    ``only``: these condos (by name or partition); an unknown one is an error."""
    now = now or datetime.now()
    t0, t1 = month_bounds(month)
    t1 = min(t1, now)
    sites = discover.node_sites(conn, since=min(t0, now - timedelta(days=60)))
    local = discover.local_name(settings, nomad_config)
    names = {s: (s if s is not None else local) for s in sites}
    if clusters:
        unknown = sorted(set(clusters) - set(names.values()) - set(settings.clusters))
        if unknown:
            raise ValueError(f"no cluster {', '.join(unknown)} (clusters with node readings: "
                             f"{', '.join(sorted(names.values())) or 'none'})")
    guest_max = timedelta(days=settings.policy.guest_max_days)
    out = {"month": month, "month_name": month_name(month), "generated": now.isoformat(timespec="seconds"),
           "period": {"from": t0.isoformat(), "to": t1.isoformat()},
           "guest_max_days": settings.policy.guest_max_days, "clusters": []}
    matched = set()
    for site in sites:
        cname = names[site]
        if clusters and cname not in clusters:
            continue
        cc = settings.cluster(cname)
        found = discover.read_nodes(conn, site, until=t1)
        if not found.nodes:
            found = discover.read_nodes(conn, site, until=now)
        discover.find(found, cc.guest_partitions)
        condos = list(cc.condos.values())
        if not cc.configured and found.condos:
            condos = [Condo(name=p, cluster=cname, confirmed=False) for p in found.condos]
        if only:
            condos = [c for c in condos if c.name in only or c.part in only]
            matched |= {c.name for c in condos} | {c.part for c in condos}
        entry = {"name": cname, "label": cc.shown, "condos": [], "earlier_jobs_any": 0}
        out["clusters"].append(entry)
        if not condos:
            continue
        # The month's jobs, and those that waited in it and started after it.
        jobs = load_jobs(conn, site, t0, min(now, t1 + WAITING_AT_MOST), now,
                         keep=lambda r, before=t1.isoformat(): str(r.get("submit_time") or "") < before)
        # Not jobs waiting: records Slurm no longer runs that never started
        # (1.17.3). One that started waited for real before it did.
        gone = runaway_ids(conn, site)
        if gone:
            out_ = [j for j in jobs if j.job_id in gone and j.start is None]
            if out_:
                entry["runaway_left_out"] = len(out_)
                jobs = [j for j in jobs if not (j.job_id in gone and j.start is None)]
        any_earlier = set()
        for c in condos:
            nodes = set(c.nodes) if c.nodes else set(found.condos.get(c.part, ()))
            if not nodes:
                continue
            ci = condo_idle(conn, site, c, nodes, found, jobs, t0, t1, now, guest_max)
            entry["condos"].append(ci.as_dict())
            any_earlier |= ci.earlier_keys
        entry["earlier_jobs_any"] = len(any_earlier)
    if only:
        missing = sorted(set(only) - matched)
        if missing:
            raise ValueError(f"no condo {', '.join(missing)}")
    return out

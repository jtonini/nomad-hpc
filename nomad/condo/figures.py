# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A condo's month in figures, from the hub's database.

- **Node hours** come from the node readings (node_state, every 5 minutes):
  in service, down, drained (no job left on it), in maintenance, and not
  read (the collector didn't report; counted as in service). A node that
  first reports during the month counts from then.
- **Core-hours** are each job's cores (Slurm's allocation, else the request)
  times the hours it ran inside the month, split evenly over its nodes; the
  share on the condo's nodes is the condo's.
- **Owner or guest:** a job on the condo's nodes is the owner's when it ran
  in the condo's partition, under the lab's Slurm account (``account``), or
  for a member of the lab; any other job there is a guest's. Idle is what
  is left of the in-service core-hours.
- **Starts:** an owner's job in the condo's partition starts within
  ``owner_start_minutes`` of when it could start (Slurm's Eligible time, else
  its submission). A later start is put down to guests on the nodes at the
  time, the lab's own jobs filling them, or neither (out-of-service nodes,
  dependencies, a request larger than the condo).
- **Preempted guest jobs** are those that ended PREEMPTED. A guest job that
  was requeued keeps its job number, and NØMAÐ keeps only its last run, so
  this is a lower bound.
- **Members:** report.toml's ``members``; else the owner's labs by
  [console.labs] in nomad.toml (the group named for them, the groups they
  lead), and the owner. People who ran in the condo's partition without
  being a member are listed too, marked as such.
"""
from __future__ import annotations

import bisect
import calendar
import math
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from nomad.condo import discover
from nomad.condo.config import Condo, Settings
from nomad.facts import jobs as jobfacts
from nomad.facts._common import chunks, columns, parse_time
from nomad.hostlist import expand_hostlist

TREND_MONTHS = 12
WAITING_AT_MOST = timedelta(days=60)
DEFAULT_SAMPLE = timedelta(minutes=5)
_CPU = re.compile(r"(?:^|,)cpu=(\d+)")
_LIVE = {"RUNNING", "COMPLETING", "CONFIGURING", "SUSPENDED", "STOPPED", "RESIZING", "SIGNALING"}
_WAITING = {"PENDING", "REQUEUED", "REQUEUE_HOLD", "REQUEUE_FED", "SPECIAL_EXIT"}
OUT_CLASSES = ("down", "drained", "maintenance")
_POWER = ("POWERED_DOWN", "POWER_DOWN", "POWERING_DOWN", "POWERING_UP")


# --- months ----------------------------------------------------------------------

def month_bounds(key: str) -> tuple[datetime, datetime]:
    y, m = (int(x) for x in key.split("-"))
    t0 = datetime(y, m, 1)
    t1 = datetime(y + (m == 12), m % 12 + 1, 1)
    return t0, t1


def month_key(t: datetime) -> str:
    return f"{t.year:04d}-{t.month:02d}"


def previous_month(now: datetime) -> str:
    return month_key(datetime(now.year, now.month, 1) - timedelta(days=1))


def months_back(key: str, n: int) -> list[str]:
    """``n`` months ending with ``key``, oldest first."""
    y, m = (int(x) for x in key.split("-"))
    out = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out[::-1]


def month_name(key: str) -> str:
    y, m = (int(x) for x in key.split("-"))
    return f"{calendar.month_name[m]} {y}"


def _hours(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 3600.0)


def _overlap(a0, a1, b0, b1) -> float:
    if a0 is None or a1 is None:
        return 0.0
    return _hours(max(a0, b0), min(a1, b1))


# --- jobs ------------------------------------------------------------------------

@dataclass(slots=True)
class Job:
    job_id: str
    user: str
    site: str | None
    partitions: tuple[str, ...]
    nodes: tuple[str, ...]
    state: str
    submit: datetime | None
    ref: datetime | None          # when it could start: Eligible, else submission
    start: datetime | None
    end: datetime | None          # the end, or as long as it was seen to run
    ended: bool
    cpus: int
    gpus: int
    req_mem_mb: float | None
    account: str | None
    alloc_tres: str | None
    runtime_s: float
    avg_cpu: float | None = None
    peak_gb: float | None = None

    def hours_in(self, t0: datetime, t1: datetime) -> float:
        return _overlap(self.start, self.end, t0, t1)

    @property
    def wait_s(self) -> float | None:
        if self.start is None or self.ref is None:
            return None
        return max(0.0, (self.start - self.ref).total_seconds())


_COLS = ("job_id", "user_name", "partition", "node_list", "state", "submit_time", "eligible_time",
         "start_time", "end_time", "req_cpus", "req_mem_mb", "req_gpus", "alloc_tres", "alloc_gpus",
         "runtime_seconds", "account", "source_site")


def state_word(text) -> str:
    s = str(text or "").split(" by ")[0].strip().split()
    return s[0].rstrip("+").upper() if s else ""


def _num(v) -> float | None:
    """A number, or None for anything else (text, NaN, infinity)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _int(v) -> int:
    x = _num(v)
    return int(x) if x is not None and x > 0 else 0


def make_job(r: dict, lo: datetime, now: datetime) -> Job | None:
    """A Job from a jobs row; None for one outside the window."""
    state = state_word(r.get("state"))
    submit = parse_time(r.get("submit_time"))
    start = parse_time(r.get("start_time"))
    elig = parse_time(r.get("eligible_time"))
    ref = elig if elig is not None and (submit is None or elig >= submit) else submit
    runtime = max(0.0, _num(r.get("runtime_seconds")) or 0.0)
    end = None
    ended = False
    if state not in _LIVE and state not in _WAITING and state not in ("", "UNKNOWN"):
        end = parse_time(r.get("end_time"))
        ended = end is not None
    elif state == "UNKNOWN" and start is not None:
        # Gone from the queue with no outcome recorded: it ran until nomad
        # saw it gone, at the latest. Not an outcome (ended stays False).
        end = parse_time(r.get("end_time"))
        if end is None or end < start:
            end = start + timedelta(seconds=runtime)
    if start is not None and end is not None and end < start:
        end = start + timedelta(seconds=runtime)
    if start is not None and end is None:
        end = start + timedelta(seconds=runtime) if runtime > 0 else now
        if state in _LIVE:
            end = min(end, now)
    if start is None:
        if ended and end is not None and end < lo:
            return None
        if not ended and (submit is None or submit < lo - WAITING_AT_MOST):
            return None
    elif end is not None and end < lo:
        return None
    alloc = r.get("alloc_tres")
    alloc = str(alloc) if alloc else None
    m = _CPU.search(alloc or "")
    cpus = int(m.group(1)) if m else _int(r.get("req_cpus"))
    gpus = _int(r.get("alloc_gpus"))
    if not gpus and alloc:
        from nomad.collectors.slurm import gpu_count
        gpus = gpu_count(alloc)
    if not gpus and "gres/gpu" not in (alloc or ""):
        # No GPUs in the allocation where the site's accounting doesn't count
        # them (AccountingStorageTRES without gres/gpu): the request.
        gpus = _int(r.get("req_gpus"))
    return Job(
        job_id=str(r.get("job_id")), user=str(r.get("user_name") or "").strip(),
        site=r.get("source_site"),
        partitions=tuple(p.strip() for p in str(r.get("partition") or "").split(",") if p.strip()),
        nodes=tuple(expand_hostlist(r.get("node_list"))) if start is not None else (),
        state=state, submit=submit, ref=ref, start=start, end=end, ended=ended,
        cpus=cpus, gpus=int(gpus or 0), req_mem_mb=_num(r.get("req_mem_mb")),
        account=(str(r.get("account")).strip() or None) if r.get("account") else None,
        alloc_tres=alloc, runtime_s=runtime)


def load_jobs(conn, site: str | None, lo: datetime, hi: datetime, now: datetime, *,
              users=None, keep=None) -> list[Job]:
    """Jobs that ran in [lo, hi) or were waiting in it. ``site`` None: every
    site. ``users``: only theirs. ``keep(row)``: a cheap test on the raw row
    before a Job is made of it."""
    cols = columns(conn, "jobs")
    if not {"user_name", "start_time", "end_time"} <= cols:
        return []
    sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in _COLS)
    base = f"SELECT {sel} FROM jobs WHERE COALESCE(start_time, submit_time) < ?"
    args: list = [hi.isoformat()]
    if site is not None and "source_site" in cols:
        base += " AND source_site = ?"
        args.append(site)
    # Separate reads, so each can use the hub's (source_site, end_time) index.
    timed = [(" AND end_time >= ?", [(lo - timedelta(days=1)).isoformat()[:10]]),
             (" AND end_time IS NULL", []),
             (" AND end_time IN ('', 'Unknown', 'None')", [])]
    out = []
    groups = [None] if users is None else list(chunks(sorted({u for u in users if u})))
    for part in groups:
        who, who_args = ("", []) if part is None else (
            f" AND user_name IN ({','.join('?' * len(part))})", part)
        for clause, extra in timed:
            cur = conn.execute(base + clause + who, args + extra + who_args)
            names = [d[0] for d in cur.description]
            for row in cur:
                r = dict(zip(names, row))
                if keep is not None and not keep(r):
                    continue
                j = make_job(r, lo, now)
                if j is not None:
                    out.append(j)
    seen = set()
    unique = []
    for j in out:
        k = (j.site, j.job_id)
        if k not in seen:
            seen.add(k)
            unique.append(j)
    return unique


def load_submitted(conn, site: str | None, partitions: set, t0: datetime, t1: datetime,
                   now: datetime) -> list[Job]:
    """Jobs submitted in [t0, t1) to ``partitions``, whenever they started
    (a job submitted on the 30th that starts on the 1st is the month's)."""
    cols = columns(conn, "jobs")
    if not {"user_name", "submit_time", "partition"} <= cols or not partitions:
        return []
    sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in _COLS)
    sql = f"SELECT {sel} FROM jobs WHERE submit_time >= ? AND submit_time < ?"
    args: list = [(t0 - timedelta(days=1)).isoformat()[:10], (t1 + timedelta(days=1)).isoformat()[:10]]
    if site is not None and "source_site" in cols:
        sql += " AND source_site = ?"
        args.append(site)
    cur = conn.execute(sql, args)
    names = [d[0] for d in cur.description]
    out = []
    for row in cur:
        r = dict(zip(names, row))
        if not any(p.strip() in partitions for p in str(r.get("partition") or "").split(",")):
            continue
        j = make_job(r, t0, now)
        if j is not None and j.submit is not None and t0 <= j.submit < t1:
            out.append(j)
    return out


def attach_measured(conn, jobs: list[Job]) -> None:
    """What each job used (job_summary): CPU % and peak memory."""
    cols = columns(conn, "job_summary")
    if not {"job_id", "avg_cpu_percent", "peak_memory_gb"} <= cols or not jobs:
        return
    by_id = defaultdict(list)
    for j in jobs:
        by_id[j.job_id].append(j)
    same_site = "source_site" in cols
    for part in chunks(sorted(by_id)):
        sql = (f"SELECT job_id, avg_cpu_percent, peak_memory_gb, {'source_site' if same_site else 'NULL'} "
               f"FROM job_summary WHERE job_id IN ({','.join('?' * len(part))})")
        for jid, cpu, peak, site in conn.execute(sql, part):
            for j in by_id.get(str(jid), ()):
                if site is None or j.site is None or site == j.site:
                    j.avg_cpu, j.peak_gb = _num(cpu), _num(peak)


def summary_row(j: Job) -> dict:
    """A job as facts.jobs.summarize reads one."""
    run = j.runtime_s or (_hours(j.start, j.end) * 3600 if j.start and j.end else 0)
    return {"state": j.state, "user_name": j.user, "req_cpus": j.cpus, "req_mem_mb": j.req_mem_mb,
            "req_gpus": j.gpus, "runtime": run, "avg_cpu": j.avg_cpu, "peak_gb": j.peak_gb,
            "alloc_tres": j.alloc_tres, "wait_seconds": j.wait_s,
            "submit_time": j.submit.isoformat() if j.submit else None,
            "start_time": j.start.isoformat() if j.start else None, "site": j.site}


# --- node hours ------------------------------------------------------------------

def node_class(state, idle: bool) -> str:
    s = str(state or "").upper()
    for k in _POWER:
        s = s.replace(k, "")
    if not s.strip():
        return "down"
    if "MAINT" in s:
        return "maintenance"
    if s.endswith("*") or any(k in s for k in ("DOWN", "FAIL", "NOT_RESPONDING", "NO_RESPOND", "UNKNOWN")):
        return "down"
    if "DRAIN" in s and idle:
        return "drained"
    return "in_service"


@dataclass
class NodeMonth:
    hours: float = 0.0             # the hours the node existed in the month
    classes: dict = field(default_factory=lambda: Counter())   # class -> hours read
    read: float = 0.0
    cores: int = 0
    gpus: int = 0

    @property
    def out(self) -> float:
        return sum(self.classes.get(k, 0.0) for k in OUT_CLASSES)

    @property
    def not_read(self) -> float:
        return max(0.0, self.hours - self.read)

    @property
    def in_service(self) -> float:
        return max(0.0, self.hours - self.out)


def node_months(conn, site: str | None, nodes, months: list[str], gpus: dict | None = None,
                until: datetime | None = None) -> dict[tuple[str, str], NodeMonth]:
    """(node, month) -> NodeMonth over ``months`` (consecutive), from node_state.

    Each reading stands for the time until the node's next one, up to twice
    the node's usual interval; the rest is "not read". A node is counted
    from its first reading only when the site's other nodes were reporting
    before it (a new node), and until its last only when they went on
    reporting after it (a node retired); a collector that stopped for the
    whole site leaves hours not read."""
    nodes = sorted(set(nodes))
    cols = columns(conn, "node_state")
    if not nodes or not months or not {"node_name", "timestamp"} <= cols:
        return {}
    lo, hi = month_bounds(months[0])[0], month_bounds(months[-1])[1]
    has_state = "state" in cols
    state = "state" if has_state else "NULL"
    idle = "(COALESCE(cpus_alloc, 0) = 0)" if "cpus_alloc" in cols else "1"
    cores = "cpus_total" if "cpus_total" in cols else "0"
    site_sql = " AND source_site = ?" if site is not None and "source_site" in cols else ""
    rng = [lo.isoformat(), hi.isoformat()] + ([site] if site_sql else [])
    span = conn.execute(f"SELECT MIN(timestamp), MAX(timestamp) FROM node_state "
                        f"WHERE timestamp >= ? AND timestamp < ?{site_sql}", rng).fetchone()
    site_first, site_last = (parse_time(span[0]), parse_time(span[1])) if span else (None, None)
    rows = []
    for part in chunks(nodes, 400):
        # Each reading's gap to the node's next, in half-minutes, counted.
        rows += conn.execute(
            f"SELECT node_name, m, st, idle, CAST(ROUND(gap / 30.0) AS INTEGER), COUNT(*), MAX(c), "
            f"MIN(timestamp), MAX(timestamp) FROM ("
            f"SELECT node_name, timestamp, substr(timestamp, 1, 7) AS m, {state} AS st, {idle} AS idle, "
            f"{cores} AS c, (julianday(replace(LEAD(timestamp) OVER (PARTITION BY node_name ORDER BY timestamp), "
            f"'T', ' ')) - julianday(replace(timestamp, 'T', ' '))) * 86400.0 AS gap "
            f"FROM node_state WHERE timestamp >= ? AND timestamp < ?{site_sql} "
            f"AND node_name IN ({','.join('?' * len(part))})) GROUP BY 1, 2, 3, 4, 5",
            rng + part).fetchall()
    first: dict = {}
    last: dict = {}
    core: dict = {}
    gaps: dict = defaultdict(Counter)        # node -> half-minutes -> readings
    for node, _, _, _, b, n, c, a, z in rows:
        a, z = parse_time(a), parse_time(z)
        if a and (node not in first or a < first[node]):
            first[node] = a
        if z and (node not in last or z > last[node]):
            last[node] = z
        core[node] = max(core.get(node, 0), _int(c))
        if b is not None:
            gaps[node][int(b)] += n
    cap = {}
    for node in first:
        g = gaps.get(node)
        if g:
            half = sum(g.values()) / 2.0
            seen = 0
            usual = 0
            for b in sorted(g):
                seen += g[b]
                if seen >= half:
                    usual = b
                    break
            cap[node] = max(2 * usual * 30.0, 60.0)
        else:
            cap[node] = DEFAULT_SAMPLE.total_seconds()
    out: dict = {}
    for node, month, st, is_idle, b, n, *_ in rows:
        nm = out.setdefault((node, month), NodeMonth(cores=core.get(node, 0),
                                                       gpus=(gpus or {}).get(node, 0)))
        secs = cap[node] / 2.0 if b is None else min(b * 30.0, cap[node])
        h = n * secs / 3600.0
        cls = node_class(st, bool(is_idle)) if has_state else "in_service"
        nm.classes[cls] += h
        nm.read += h
    day = timedelta(days=1)
    for node in first:
        new = site_first is not None and first[node] > max(lo, site_first) + day
        gone = site_last is not None and last[node] < min(hi, site_last) - day
        # Before the site's first reading there is nothing to go by: its
        # months have no readings (the month it began counts whole, as its
        # jobs do).
        begin = first[node] if new else max(lo, datetime(site_first.year, site_first.month, 1)
                                            if site_first else lo)
        stop = last[node] + timedelta(seconds=cap[node]) if gone else hi
        if until is not None:
            stop = min(stop, max(until, begin))
        for month in months:
            t0, t1 = month_bounds(month)
            h = _overlap(begin, stop, t0, t1)
            nm = out.get((node, month))
            if h <= 0 and nm is None:
                continue
            if nm is None:
                nm = out[(node, month)] = NodeMonth(cores=core.get(node, 0), gpus=(gpus or {}).get(node, 0))
            nm.hours = h
            if nm.read > h > 0:
                k = h / nm.read
                for c in list(nm.classes):
                    nm.classes[c] *= k
                nm.read = h
    return out


# --- one condo -------------------------------------------------------------------

def _pct(part, whole, digits=1):
    return None if not whole else round(100.0 * part / whole, digits)


def _r(x, d=1):
    return None if x is None else round(x, d)


@dataclass
class Context:
    """What every condo of one cluster shares."""
    cluster: str
    label: str
    site: str | None
    month: str
    t0: datetime
    t1: datetime
    now: datetime
    found: discover.Found
    jobs: list[Job]                       # the cluster's jobs in the month
    trend_jobs: list[Job]                 # ... on condo nodes, in the months before
    node_hours: dict                      # (node, month) -> NodeMonth
    all_condo_nodes: set
    ceilings: dict
    policy: object
    delivery: object
    labels: dict = field(default_factory=dict)     # site -> how the reports name its cluster


class Owner:
    """Whose a job on the condo's nodes is."""

    def __init__(self, condo: Condo, members: set):
        self.part, self.members, self.account = condo.part, members, condo.account

    def __call__(self, j: Job) -> bool:
        return (self.part in j.partitions or j.user in self.members
                or (self.account is not None and j.account == self.account))


def _split(jobs, nodes: set, t0, t1, owner: Owner):
    """Core-hours and GPU-hours on ``nodes`` in [t0, t1), owner's and guests'."""
    out = {"owner": 0.0, "guest": 0.0, "owner_gpu": 0.0, "guest_gpu": 0.0}
    by_user: Counter = Counter()
    guests: set = set()
    guest_jobs = 0
    for j in jobs:
        if not j.nodes:
            continue
        on = [n for n in j.nodes if n in nodes]
        if not on:
            continue
        h = j.hours_in(t0, t1)
        if h <= 0:
            continue
        frac = len(on) / len(j.nodes)
        ch = j.cpus * h * frac
        gh = j.gpus * h * frac
        if owner(j):
            out["owner"] += ch
            out["owner_gpu"] += gh
            by_user[j.user] += ch
        else:
            out["guest"] += ch
            out["guest_gpu"] += gh
            guests.add(j.user)
            guest_jobs += 1
    return out, by_user, guests, guest_jobs


def _capacity(node_hours: dict, nodes, month: str) -> dict:
    cap = {"hours": 0.0, "core_hours": 0.0, "gpu_hours": 0.0, "out_core_hours": 0.0,
           "classes": Counter(), "not_read": 0.0, "node_hours": 0.0}
    for n in nodes:
        nm = node_hours.get((n, month))
        if nm is None:
            continue
        cap["node_hours"] += nm.hours
        cap["core_hours"] += nm.cores * nm.in_service
        cap["gpu_hours"] += nm.gpus * nm.in_service
        cap["out_core_hours"] += nm.cores * nm.out
        for k, v in nm.classes.items():
            cap["classes"][k] += v
        cap["not_read"] += nm.not_read
    return cap


def _shares(cap: dict, split: dict) -> dict:
    whole = cap["core_hours"]
    owner, guest = split["owner"], split["guest"]
    idle = max(0.0, whole - owner - guest)
    total = cap["core_hours"] + cap["out_core_hours"]
    return {"owner_pct": _pct(owner, whole), "guest_pct": _pct(guest, whole),
            "idle_pct": _pct(idle, whole),
            # Of all the condo's core-hours, in service or not.
            "out_pct": _pct(cap["out_core_hours"], total),
            "over_capacity": whole > 0 and owner + guest > whole * 1.02}


class OnNodes:
    """The jobs that ran on a set of nodes, to ask what was running when."""

    def __init__(self, jobs, nodes: set):
        self.jobs = sorted((j for j in jobs if j.start is not None and j.end is not None
                            and any(n in nodes for n in j.nodes)), key=lambda j: j.start)
        self.starts = [j.start for j in self.jobs]

    def at(self, t: datetime, test) -> list[Job]:
        return [j for j in self.jobs[:bisect.bisect_right(self.starts, t)] if t < j.end and test(j)]


def resolve_members(conn, condo: Condo, acc, members_of, exists, site: str | None = None,
                    now: datetime | None = None) -> tuple[set, str]:
    """The lab's members, and where they came from, in words."""
    spec = condo.members
    people: set = set()
    how = ""
    if isinstance(spec, list):
        people, how = set(spec), "listed in report.toml"
    elif isinstance(spec, str) and spec.startswith("group:"):
        g = spec[len("group:"):]
        people = set(members_of(g)) if members_of else set()
        how = f"group {g}"
    elif isinstance(spec, str) and spec.startswith("account:"):
        a = spec[len("account:"):]
        cols = columns(conn, "jobs")
        if "account" in cols:
            since = ((now or datetime.now()) - timedelta(days=365)).isoformat()[:10]
            where = " AND source_site = ?" if site is not None and "source_site" in cols else ""
            people = {u for (u,) in conn.execute(
                f"SELECT DISTINCT user_name FROM jobs WHERE end_time >= ?{where} AND account = ?",
                [since] + ([site] if where else []) + [a]) if u}
        how = f"Slurm account {a}, who used it this past year"
    elif condo.owners and acc is not None:
        groups = []
        for o in condo.owners:
            for g in (acc.lab_groups(o, exists) if members_of else []):
                if g not in groups:
                    groups.append(g)
        for g in groups:
            people |= set(members_of(g))
        how = (("lab groups " if len(groups) > 1 else "lab group ") + ", ".join(groups)) if groups else ""
    people |= set(condo.owners)
    return people, how or "not found: the owner, and who ran in the condo's partition"


def condo_figures(ctx: Context, condo: Condo, members: set, members_how: str,
                  member_jobs: list[Job], name_members: bool) -> dict:
    t0, t1, month = ctx.t0, ctx.t1, ctx.month
    pol = ctx.policy
    found = ctx.found
    nodes = set(condo.nodes) if condo.nodes else set(found.condos.get(condo.part, ()))
    owner = Owner(condo, members)
    info = [found.nodes.get(n) for n in sorted(nodes)]
    cores = sum(i.cores for i in info if i)
    gpus = sum(i.gpus for i in info if i)
    mem_gb = sum(i.memory_mb for i in info if i) / 1024
    cap = _capacity(ctx.node_hours, nodes, month)
    split, by_user, guests, guest_jobs = _split(ctx.jobs, nodes, t0, t1, owner)
    shares = _shares(cap, split)

    # Owner's jobs in the condo's partition: how soon they started.
    guarantee = timedelta(minutes=pol.owner_start_minutes)
    submitted = [j for j in ctx.jobs if condo.part in j.partitions and j.ref is not None and t0 <= j.ref < t1]
    started = [j for j in submitted if j.start is not None]
    waiting = [j for j in submitted if j.start is None and not j.ended and j.state in _WAITING]
    # Still waiting when the report is made: late once past the guarantee.
    overdue = [j for j in waiting if ctx.now - j.ref > guarantee]
    tracked = started + overdue
    within = 0
    late = Counter()
    longest = None
    on = OnNodes(ctx.jobs, nodes)
    for j in tracked:
        wait = (j.start or ctx.now) - j.ref
        if wait <= guarantee:
            within += 1
            continue
        at = j.ref + guarantee
        if on.at(at, lambda x: not owner(x)):
            why = "guests"
        else:
            busy = sum(x.cpus * len([n for n in x.nodes if n in nodes]) / max(1, len(x.nodes))
                       for x in on.at(at, owner))
            why = "own" if cores and cores - busy < max(1, j.cpus) else "other"
        late[why] += 1
        if longest is None or wait > longest[0]:
            longest = (wait, why, j.start is None)
    ended = [j for j in ctx.jobs if j.ended and j.end is not None and t0 <= j.end < t1
             and owner(j) and any(n in nodes for n in j.nodes)]
    stats = jobfacts.summarize([summary_row(j) for j in ended], ctx.ceilings) or {}

    # Guests, as totals only.
    guest_on = [j for j in ctx.jobs if not owner(j) and any(n in nodes for n in j.nodes)]
    preempted = sum(1 for j in guest_on if j.state == "PREEMPTED" and j.end and t0 <= j.end < t1)
    too_long = timedelta(days=pol.guest_max_days, minutes=pol.guest_grace_minutes)
    over_limit = sum(1 for j in guest_on if j.start and j.end and j.end - j.start > too_long
                     and j.end > t0 and j.start < t1)

    # The lab beyond its own nodes.
    elsewhere = _elsewhere(ctx, nodes, members, member_jobs)

    # Each member, by NetID.
    rows = _members(ctx, nodes, members, member_jobs, by_user, owner, t0, t1) if name_members else []
    member_count = len(members | set(by_user))

    trend = []
    for m in months_back(month, TREND_MONTHS):
        m0, m1 = month_bounds(m)
        c = _capacity(ctx.node_hours, nodes, m)
        if not c["node_hours"]:
            trend.append({"month": m, "measured": False})
            continue
        jobs = ctx.jobs if m == month else ctx.trend_jobs
        s, *_ = _split(jobs, nodes, m0, m1, owner)
        trend.append({"month": m, "measured": True, **_shares(c, s)})

    quota = condo.lab_scratch_quota_tb or pol.lab_scratch_quota_tb
    hours = _hours(t0, t1)
    coverage = []
    if cap["node_hours"] and cap["not_read"] > 0.02 * cap["node_hours"]:
        coverage.append(f"The nodes went unread for {cap['not_read']:,.0f} of {cap['node_hours']:,.0f} "
                        "node-hours (the collector didn't report); those hours count as in service.")
    if not cap["node_hours"]:
        coverage.append("No node readings for these nodes this month: the shares can't be worked out.")
    if shares["over_capacity"]:
        coverage.append("Jobs are recorded on these nodes for more core-hours than the nodes were in "
                        "service (jobs on drained or unread nodes): the idle share is 0.")
    if not condo.nodes and not found.condos.get(condo.part):
        coverage.append(f"Partition {condo.part} wasn't seen on the nodes at the month's end.")
    measured = stats.get("core_hours", {}).get("jobs_measured", 0) if stats else 0
    if ended and measured < len(ended):
        coverage.append(f"What jobs used was measured for {measured} of the {len(ended)} that ended.")
    coverage.append("Preempted guest jobs: those that ended PREEMPTED; a requeued job keeps its number "
                    "and only its last run is kept, so this is at least the number.")
    coverage.append("Storage per lab isn't measured yet.")

    out = {
        "cluster": {"name": ctx.cluster, "label": ctx.label},
        "condo": {"name": condo.name, "label": condo.label or condo.name, "partition": condo.part,
                  "owner": condo.owner, "owners": list(condo.owners), "confirmed": condo.confirmed,
                  "nodes": sorted(nodes), "node_list": discover.compress_hostlist(nodes) if nodes else "",
                  "cores": cores, "gpus": gpus, "memory_gb": round(mem_gb)},
        "month": month, "period": {"from": t0.isoformat(), "to": t1.isoformat(), "hours": hours},
        "generated": ctx.now.isoformat(timespec="seconds"),
        "nodes": {"node_hours": round(cap["node_hours"], 1),
                  "in_service": round(cap["classes"].get("in_service", 0.0), 1),
                  "down": round(cap["classes"].get("down", 0.0), 1),
                  "drained": round(cap["classes"].get("drained", 0.0), 1),
                  "maintenance": round(cap["classes"].get("maintenance", 0.0), 1),
                  "not_read": round(cap["not_read"], 1)},
        "use": {"capacity_core_hours": round(cap["core_hours"], 1),
                "owner_core_hours": round(split["owner"], 1),
                "guest_core_hours": round(split["guest"], 1),
                "idle_core_hours": round(max(0.0, cap["core_hours"] - split["owner"] - split["guest"]), 1),
                **shares,
                "gpu": None if not gpus else {
                    "capacity_gpu_hours": round(cap["gpu_hours"], 1),
                    "owner_gpu_hours": round(split["owner_gpu"], 1),
                    "guest_gpu_hours": round(split["guest_gpu"], 1),
                    "owner_pct": _pct(split["owner_gpu"], cap["gpu_hours"]),
                    "guest_pct": _pct(split["guest_gpu"], cap["gpu_hours"])}},
        "guests": {"people": len(guests), "jobs": guest_jobs,
                   "core_hours": round(split["guest"], 1), "gpu_hours": round(split["guest_gpu"], 1),
                   "preempted": preempted, "over_time_limit": over_limit,
                   "time_limit_days": pol.guest_max_days},
        "owner_jobs": {
            "tracked": len(tracked), "within": within, "within_pct": _pct(within, len(tracked)),
            "guarantee_minutes": pol.owner_start_minutes,
            "late": {"guests": late["guests"], "own": late["own"], "other": late["other"]},
            "still_waiting": len(waiting), "still_waiting_late": len(overdue),
            "longest_wait_minutes": _r(longest[0].total_seconds() / 60) if longest else None,
            "longest_reason": longest[1] if longest else None,
            "longest_still_waiting": bool(longest and longest[2]),
            "ended": len(ended), "summary": stats},
        "elsewhere": elsewhere,
        "members": {"how": members_how, "count": member_count, "named": name_members, "each": rows},
        "storage": {"measured": False,
                    "scratch_path": pol.scratch_path.replace("{lab}", condo.name),
                    "lab_scratch_quota_tb": quota.as_dict(),
                    "home_quota_gb": pol.home_quota_gb.as_dict()},
        "trend": trend,
        "policy": pol.as_dict(),
        "coverage": coverage,
    }
    return out


def _elsewhere(ctx: Context, nodes: set, members: set, member_jobs: list[Job]) -> list[dict]:
    """The lab's jobs off its own nodes: this cluster's shared nodes, other
    condos (as guests there), and other clusters."""
    t0, t1 = ctx.t0, ctx.t1
    buckets: dict[tuple, list[Job]] = defaultdict(list)
    for j in member_jobs:
        if j.user not in members or not j.nodes or j.hours_in(t0, t1) <= 0:
            continue
        if j.site == ctx.site or (ctx.site is None and j.site is None):
            if any(n in nodes for n in j.nodes):
                continue
            where = "other condos" if any(n in ctx.all_condo_nodes for n in j.nodes) else "shared nodes"
            buckets[(ctx.cluster, where)].append(j)
        else:
            buckets[(j.site or "?", "all nodes")].append(j)
    out = []
    for (site, where), jobs in sorted(buckets.items(), key=lambda kv: (kv[0][0] != ctx.cluster, kv[0])):
        ch = sum(j.cpus * j.hours_in(t0, t1) for j in jobs)
        gh = sum(j.gpus * j.hours_in(t0, t1) for j in jobs)
        ended = [j for j in jobs if j.ended and j.end and t0 <= j.end < t1]
        st = jobfacts.summarize([summary_row(j) for j in ended], ctx.ceilings) or {}
        out.append({"cluster": site, "label": ctx.labels.get(site, site), "where": where,
                    "core_hours": round(ch, 1), "gpu_hours": round(gh, 1),
                    "people": len({j.user for j in jobs}), "jobs_ended": len(ended),
                    "failed_pct": st.get("failed_pct"),
                    "cpu_used_pct": (st.get("core_hours") or {}).get("used_pct"),
                    "memory_used_pct": (st.get("memory") or {}).get("used_pct"),
                    "wait_median_minutes": (st.get("wait_minutes") or {}).get("median")})
    return out


def _members(ctx, nodes, members, member_jobs, by_user, owner, t0, t1) -> list[dict]:
    """A row per member, and per person who ran in the condo's partition
    without being one (their jobs there only, never their guest jobs)."""
    rows: dict[str, dict] = {}

    def row(u):
        return rows.setdefault(u, {"user": u, "member": u in members, "condo_core_hours": 0.0,
                                   "shared_core_hours": 0.0, "other_condo_core_hours": 0.0,
                                   "elsewhere_core_hours": 0.0, "gpu_hours": 0.0, "jobs_ended": 0,
                                   "failed": 0})

    def ended(j):
        return j.ended and j.end is not None and t0 <= j.end < t1

    for u, ch in by_user.items():
        row(u)["condo_core_hours"] += ch
    for j in member_jobs:
        if j.user not in members:
            continue
        r = row(j.user)
        h = j.hours_in(t0, t1)
        here = j.site == ctx.site or (ctx.site is None and j.site is None)
        if h > 0 and j.nodes:
            r["gpu_hours"] += j.gpus * h
            if not here:
                r["elsewhere_core_hours"] += j.cpus * h
            elif not any(n in nodes for n in j.nodes):
                key = ("other_condo_core_hours" if any(n in ctx.all_condo_nodes for n in j.nodes)
                       else "shared_core_hours")
                r[key] += j.cpus * h
        if ended(j):
            r["jobs_ended"] += 1
            r["failed"] += jobfacts.OUTCOMES.get(j.state) in jobfacts.PROBLEMS
    for j in ctx.jobs:
        if j.user in rows and j.user not in members and owner(j) and any(n in nodes for n in j.nodes):
            r = rows[j.user]
            r["gpu_hours"] += j.gpus * j.hours_in(t0, t1) * len([n for n in j.nodes if n in nodes]) / len(j.nodes)
            if ended(j):
                r["jobs_ended"] += 1
                r["failed"] += jobfacts.OUTCOMES.get(j.state) in jobfacts.PROBLEMS
    for u in members:
        row(u)
    out = []
    for r in rows.values():
        for k in ("condo_core_hours", "shared_core_hours", "other_condo_core_hours",
                  "elsewhere_core_hours", "gpu_hours"):
            r[k] = round(r[k], 1)
        out.append(r)
    out.sort(key=lambda r: (-(r["condo_core_hours"] + r["shared_core_hours"] + r["other_condo_core_hours"]
                              + r["elsewhere_core_hours"]), r["user"]))
    return out


# --- every condo -----------------------------------------------------------------

def build(conn: sqlite3.Connection, settings: Settings, month: str, *, nomad_config: dict | None = None,
          clusters: list[str] | None = None, only: list[str] | None = None,
          now: datetime | None = None) -> dict:
    """Every condo's figures for ``month``, and the summary. ``clusters``:
    only those; ``only``: only the condos of these names (either makes the
    summary partial: ``partial`` is then True)."""
    from nomad.config.access import access_from, members_lookup
    from nomad.edu.progress import memory_ceilings
    now = now or datetime.now()
    t0, t1 = month_bounds(month)
    acc = access_from(nomad_config or {}, log=False)
    exists, members_of = members_lookup(conn)
    ceilings = memory_ceilings(conn)
    # Clusters with readings since the month began (or lately): an old
    # month's report still has the clusters of then.
    sites = discover.node_sites(conn, since=min(t0, now - timedelta(days=60)))
    names = {s: (s if s is not None else discover.LOCAL) for s in sites}
    if clusters:
        unknown = sorted(set(clusters) - set(names.values()) - set(settings.clusters))
        if unknown:
            raise ValueError(f"no cluster {', '.join(unknown)} (clusters with node readings: "
                             f"{', '.join(sorted(names.values())) or 'none'})")
    wanted = [s for s in sites if not clusters or names[s] in clusters]
    out = {"month": month, "month_name": month_name(month), "generated": now.isoformat(timespec="seconds"),
           "period": {"from": t0.isoformat(), "to": t1.isoformat()}, "clusters": [], "reports": [],
           "partial": bool(clusters or only)}
    # report.toml's clusters with condos but no node readings: said, not dropped.
    for cname, cc in sorted(settings.clusters.items()):
        if cc.condos and cname not in names.values() and (not clusters or cname in clusters):
            out["clusters"].append({"name": cname, "label": cc.shown, "guest_partitions": [], "condos": [],
                                    "flags": [{"kind": "no_readings", "condos": sorted(cc.condos)}]})
    trend_months = months_back(month, TREND_MONTHS)
    for site in wanted:
        cname = names[site]
        cc = settings.cluster(cname)
        found = discover.read_nodes(conn, site, until=t1)
        if not found.nodes:
            found = discover.read_nodes(conn, site, until=now)
        discover.find(found, cc.guest_partitions)
        latest = discover.find(discover.read_nodes(conn, site, until=now), cc.guest_partitions)
        condos = list(cc.condos.values())
        listed = {c.part for c in condos}
        flags = []
        new = sorted(set(latest.condos) - listed)
        if cc.configured and new:
            flags.append({"kind": "new", "partitions": new})
        if not cc.configured and found.condos:
            # Not drafted yet: report what the nodes say, unconfirmed.
            condos = [Condo(name=p, cluster=cname, confirmed=False) for p in found.condos]
            flags.append({"kind": "not_configured", "partitions": sorted(found.condos)})
        gone = sorted(c.part for c in condos if not c.nodes and c.part not in found.condos)
        if gone:
            flags.append({"kind": "partition_gone", "partitions": gone})
        unconfirmed = sorted(c.name for c in condos if not c.confirmed or not c.owner)
        if unconfirmed and cc.configured:
            flags.append({"kind": "unconfirmed", "condos": unconfirmed})
        entry = {"name": cname, "label": cc.shown, "guest_partitions": found.guest_partitions,
                 "condos": [], "flags": flags}
        out["clusters"].append(entry)
        if only:
            condos = [c for c in condos if c.name in only or c.part in only]
        if not condos:
            continue
        all_nodes = set(found.condo_nodes)
        for c in condos:
            all_nodes |= set(c.nodes) if c.nodes else set(found.condos.get(c.part, ()))
        touching = {p for p, ns in found.partitions.items() if ns & all_nodes} | listed

        def keep(r, touching=touching):
            return any(p.strip() in touching for p in str(r.get("partition") or "").split(","))
        jobs = load_jobs(conn, site, t0, t1, now)
        have = {j.job_id for j in jobs}
        jobs += [j for j in load_submitted(conn, site, listed | set(found.condos), t0, t1, now)
                 if j.job_id not in have]
        trend_lo = month_bounds(trend_months[0])[0]
        trend_jobs = load_jobs(conn, site, trend_lo, t0, now, keep=keep) if trend_lo < t0 else []
        gpus = {n: i.gpus for n, i in found.nodes.items()}
        hours = node_months(conn, site, all_nodes, trend_months, gpus, until=now)
        ctx = Context(cluster=cname, label=cc.shown, site=site, month=month, t0=t0, t1=t1, now=now,
                      found=found, jobs=jobs, trend_jobs=trend_jobs, node_hours=hours,
                      all_condo_nodes=all_nodes, ceilings=ceilings, policy=settings.policy,
                      delivery=settings.delivery,
                      labels={names[s]: settings.cluster(names[s]).shown for s in sites})
        resolved = {}
        everyone = set()
        for c in condos:
            people, how = resolve_members(conn, c, acc, members_of, exists, site, now)
            resolved[c.name] = (people, how)
            everyone |= people
        member_jobs = load_jobs(conn, None, t0, t1, now, users=everyone) if everyone else []
        on_condos = [j for j in jobs if any(n in all_nodes for n in j.nodes)]
        attach_measured(conn, [j for j in on_condos + member_jobs if j.ended])
        for c in condos:
            people, how = resolved[c.name]
            fig = condo_figures(ctx, c, people, how, member_jobs, settings.delivery.name_members)
            out["reports"].append(fig)
            entry["condos"].append(_summary_row(fig))
    return out


def _summary_row(fig: dict) -> dict:
    u, o, g, n = fig["use"], fig["owner_jobs"], fig["guests"], fig["nodes"]
    return {"condo": fig["condo"]["name"], "label": fig["condo"]["label"], "owner": fig["condo"]["owner"],
            "owners": fig["condo"].get("owners") or ([fig["condo"]["owner"]] if fig["condo"]["owner"] else []),
            "confirmed": fig["condo"]["confirmed"], "nodes": len(fig["condo"]["nodes"]),
            "cores": fig["condo"]["cores"], "gpus": fig["condo"]["gpus"],
            "owner_pct": u["owner_pct"], "guest_pct": u["guest_pct"], "idle_pct": u["idle_pct"],
            "out_pct": u["out_pct"], "guest_core_hours": g["core_hours"], "guest_people": g["people"],
            "preempted": g["preempted"], "owner_jobs": o["tracked"], "within_pct": o["within_pct"],
            "late_for_guests": o["late"]["guests"],
            "out_node_hours": round(n["down"] + n["drained"] + n["maintenance"], 1)}

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""When each machine is busiest, over a window: Slurm clusters (the node
collector's readings), interactive servers (the interactive collector's
hourly totals and the machine's own CPU readings) and lab workstations (the
workstation collector's readings).

For each: the most in use at once and when, the average over the window, a
typical week (each weekday and hour, averaged over the window) and the
busiest and quietest hours. Aggregates of machines only, never a person.
Workstations go by lab (nomad's rules: [console.labs] in nomad.toml, or the
collector's tag), and machines in no lab by the site that collects them;
storage read by the workstation collector is left out. Machines and labs are
listed by name, and both can be named for people, so the answer is for staff
and for a lab's own PI; ``names=False`` numbers them instead.

What each figure measures:

- Cluster cores allocated: what Slurm handed out (CPUAlloc), of the cores of
  the nodes in service in the reading. A node that is down, not responding,
  in maintenance, or drained with nothing running is out of service: it is
  left out of the totals and counted apart (``nodes_out``).
- Cluster load: Slurm's CPULoad, each node's counted up to its cores (the
  most at once and the average alike), summed over the nodes in service.
  Load counts threads ready to run as well as running, and processes
  waiting on storage, so a node can read far above its cores (storage that
  stalls); how often a node read above its cores is counted apart
  (``over_cores_pct``), and how many nodes were when the load was highest
  (``peak.nodes_above_cores``). Where jobs are held to their cores (task/cgroup),
  that means jobs started more threads than cores they asked for, or were
  waiting on storage.
- Cluster memory allocated: Slurm's AllocMem. Where Slurm doesn't count
  memory (SelectTypeParameters without _Memory) it is always 0, and is said
  to be not tracked rather than shown as 0%.
- Cluster memory held: on the nodes running jobs, each node's memory less
  what Slurm reports free, of the memory of all the nodes in service. Slurm's free memory is the kernel's (MemFree),
  which counts the file cache as used, so this is the most the jobs could
  have used, not what they used: jobs used this much or less. Nodes running
  nothing count as none (whatever they hold is the cache or left behind, not
  a job's).
- Interactive servers: people with a session open (the most in an hour, and
  the hour's average), people computing, the memory their sessions held, and
  the machine's own busy cores (mpstat) where its collector runs on it.
- Workstations: the cores people's sessions kept busy, from each person's
  CPU counters (workstation_user_snapshot); the machine's own services are
  not included. Memory in use is the machine's (cache left out). A machine
  is busy with a core or more in use. The group's figures add the machines'
  ten-minute averages (machines aren't read at the same instant). (The
  workstation collector's own CPU percentages were never measured, and are
  not used.)

An hour of the week is ranked busiest or quietest only with readings on two
different days or more, and nothing is ranked when every hour is the same.
Times are as the collectors wrote them: each site's local time.
"""
from __future__ import annotations

import re
import sqlite3
import statistics
from datetime import datetime, timedelta

from nomad.facts._common import (STALE_AFTER_HOURS, columns, envelope, fetch, iso, num,
                                 parse_time, site_expr, window)

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
TOP = 3                    # busiest and quietest hours named
MIN_DAYS = 2               # different days an hour of the week needs to be ranked
FLAT = 0.5                 # busiest and quietest closer than this: nothing to rank
BUSY_CORE = 1.0            # a workstation with this many cores in use is busy
BUCKET = 15                # 'YYYY-MM-DDTHH:M': workstations' readings in 10 minutes
SESSION_GAP = 2 * 3600     # seconds between a person's readings that end a span

# A node out of service: down, not responding (Slurm's "*"), failing, in
# maintenance, or drained with nothing running (a draining node still
# running jobs is in service until they end).
_OUT = ("(state IS NULL OR state LIKE '%*%' OR UPPER(state) LIKE '%DOWN%' "
        "OR UPPER(state) LIKE '%FAIL%' OR UPPER(state) LIKE '%MAINT%' "
        "OR UPPER(state) LIKE '%NOT_RESPONDING%' OR UPPER(state) LIKE '%NO_RESPOND%' "
        "OR (UPPER(state) LIKE '%DRAIN%' AND COALESCE(cpus_alloc, 0) = 0))")


class Week:
    """Averages for each weekday and hour, and the days they rest on."""

    def __init__(self):
        self.sum = [[0.0] * 24 for _ in range(7)]
        self.n = [[0] * 24 for _ in range(7)]
        self.days = [[set() for _ in range(24)] for _ in range(7)]

    def add(self, t: datetime, value) -> None:
        if value is None or t is None:
            return
        d, h = t.weekday(), t.hour
        self.sum[d][h] += value
        self.n[d][h] += 1
        self.days[d][h].add(t.date())

    def means(self, digits: int = 1) -> list:
        return [[round(s / n, digits) if n else None for s, n in zip(srow, nrow)]
                for srow, nrow in zip(self.sum, self.n)]

    def ranked(self, top: int = TOP) -> tuple[list, list]:
        """(busiest, quietest) hours of the week seen on MIN_DAYS days or more;
        both empty when there is too little, or nothing to tell them apart."""
        cells = [(self.sum[d][h] / self.n[d][h], d, h)
                 for d in range(7) for h in range(24) if len(self.days[d][h]) >= MIN_DAYS]
        if not cells or max(c[0] for c in cells) - min(c[0] for c in cells) < FLAT:
            return [], []

        def out(c):
            return {"weekday": WEEKDAYS[c[1]], "hour": c[2], "value": round(c[0], 1)}
        busiest = sorted(cells, key=lambda c: (-c[0], c[1], c[2]))[:top]
        quietest = sorted(cells, key=lambda c: (c[0], c[1], c[2]))[:top]
        return [out(c) for c in busiest], [out(c) for c in quietest]


class Peak:
    """The largest value seen, of what total, and when (the first time)."""

    def __init__(self):
        self.value = self.total = self.at = None

    def add(self, value, total, at) -> None:
        if value is None:
            return
        if self.value is None or value > self.value:
            self.value, self.total, self.at = value, total, at

    def out(self, digits: int = 0, scale: float = 1.0) -> dict | None:
        if self.value is None:
            return None
        v = round(self.value / scale, digits)
        t = None if self.total is None else round(self.total / scale, digits)
        return {"value": int(v) if digits == 0 else v,
                "of": (int(t) if digits == 0 else t) if t is not None else None,
                "pct": round(100 * self.value / self.total, 1) if self.total else None,
                "at": iso(self.at) if self.at else None}


def _with(peak: dict | None, **more) -> dict | None:
    return {**peak, **more} if peak else None


def _share(part, whole):
    return 100.0 * part / whole if part is not None and whole else None


def _mean(values: list, digits: int = 1):
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), digits) if values else None


def _every_minutes(times: list) -> float | None:
    gaps = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:]) if b > a]
    return round(statistics.median(gaps), 1) if gaps else None


def _n(value):
    """num(), quickly for what SQLite's aggregates return (numbers or None)."""
    return value if type(value) in (int, float) else num(value)


def _indexed_first(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Whether an index on the table starts with the column."""
    try:
        for idx in conn.execute(f"PRAGMA index_list({table})").fetchall():
            first = conn.execute(f"PRAGMA index_info({idx[1]!r})").fetchone()
            if first and first[2] == column:
                return True
    except sqlite3.Error:
        pass
    return False


def _distinct(conn: sqlite3.Connection, table: str, column: str) -> list:
    """The column's values, not NULL, one index lookup each (a skip-scan:
    SQLite would otherwise read every entry of the index)."""
    return [r[0] for r in conn.execute(f"""
        WITH RECURSIVE v(x) AS (
            SELECT MIN({column}) FROM {table}
            UNION ALL SELECT (SELECT MIN({column}) FROM {table} WHERE {column} > x) FROM v
            WHERE x IS NOT NULL)
        SELECT x FROM v WHERE x IS NOT NULL""")]


def _site_name(site, default):
    return default if site is None else str(site)


def _span(times: list, now: datetime) -> dict:
    """From, until, and whether the readings stopped (the last older than
    STALE_AFTER_HOURS before now)."""
    last = times[-1]
    return {"from": iso(times[0]), "until": iso(last),
            "stopped": (now - last) > timedelta(hours=STALE_AFTER_HOURS)}


# ── Clusters ────────────────────────────────────────────────────────────

def clusters(conn: sqlite3.Connection, since: str, until: str, now: datetime,
             all_out: list | None = None) -> list:
    """Each cluster's entry. A cluster whose every reading in the window had
    every node out of service has no figures: it goes in ``all_out`` (site,
    readings, first and last) instead of being left out without a word."""
    cols = columns(conn, "node_state")
    if not {"timestamp", "cpus_total", "cpus_alloc"} <= cols:
        return []
    site = site_expr(cols) or "NULL"
    out_of_service = _OUT if "state" in cols else "0"
    has = {c: c in cols for c in ("cpu_load", "memory_total_mb", "memory_alloc_mb", "memory_free_mb")}
    free_known = has["memory_free_mb"] and has["memory_total_mb"]
    used = "MAX(memory_total_mb - memory_free_mb, 0)"
    # Numbers only: SQLite compares text above every number, so a stray
    # 'N/A' would read as a full node. Slurm prints FreeMem=N/A for a node
    # that hasn't reported, which the collector stores as 0: free memory
    # counts only when above 0.
    numbers = ["cpus_total", "cpus_alloc"] + [c for c, ok in has.items() if ok]
    typed = [f"CASE WHEN typeof({c}) IN ('integer', 'real') THEN {c} END AS {c}" for c in numbers]

    def in_service(expr, ok=True):
        return f"SUM(CASE WHEN out = 0 THEN {expr} END)" if ok else "NULL"

    def query(where: str, group: str, args: tuple) -> list:
        # Each reading's "out of service" worked out once: the subquery's
        # LIMIT keeps SQLite from copying the expression into every sum
        # (seven LIKEs a use, ten uses: most of the time on a hub).
        inner = ", ".join(["timestamp", f"{group} AS site",
                           f"CASE WHEN {out_of_service} THEN 1 ELSE 0 END AS out", *typed])
        return fetch(conn, f"""
            SELECT site, timestamp AS t, SUM(1 - out) AS nodes, SUM(out) AS nodes_out,
                   {in_service('cpus_total')} AS cores, {in_service('cpus_alloc')} AS alloc,
                   {in_service('cpu_load', has['cpu_load'])} AS load,
                   {in_service('MIN(cpu_load, cpus_total)', has['cpu_load'])} AS capped,
                   {in_service('CASE WHEN cpu_load > cpus_total THEN 1 ELSE 0 END', has['cpu_load'])} AS over,
                   {in_service('memory_total_mb', has['memory_total_mb'])} AS mem,
                   {in_service('memory_alloc_mb', has['memory_alloc_mb'])} AS mem_alloc,
                   {in_service(f'CASE WHEN memory_free_mb > 0 '
                               f'THEN (CASE WHEN cpus_alloc > 0 THEN {used} ELSE 0 END) END',
                               free_known)} AS mem_held
            FROM (SELECT {inner} FROM node_state WHERE {where} LIMIT -1)
            GROUP BY 1, 2 ORDER BY 1, 2""", args)
    window_only = "timestamp >= ? AND timestamp <= ?"
    if "source_site" in cols and _indexed_first(conn, "node_state", "source_site"):
        # A hub: each site through its own index, (source_site, timestamp),
        # rather than every site's readings by time, interleaved.
        rows = []
        for name in _distinct(conn, "node_state", "source_site"):
            rows += query(f"source_site = ? AND {window_only}", "source_site", (name, since, until))
        rows += query(f"source_site IS NULL AND {window_only}",
                      "cluster" if "cluster" in cols else "NULL", (since, until))
    else:
        rows = query(window_only, site, (since, until))
    by_site: dict = {}
    for r in rows:
        by_site.setdefault(r["site"], []).append(r)
    out = []
    for name, runs in by_site.items():
        times, nodes, nodes_out = [], 0, []
        p_alloc, p_load, p_mem_alloc, p_mem_held = Peak(), Peak(), Peak(), Peak()
        w_alloc, w_load, w_mem_held, w_mem_alloc = Week(), Week(), Week(), Week()
        alloc_pct, load_pct, mem_held_pct, mem_alloc_pct = [], [], [], []
        over_runs, most_mem_alloc, over_at_peak = 0, 0.0, 0
        seen = []
        for r in runs:
            t = parse_time(r["t"])
            if t is None:
                continue
            seen.append(t)
            nodes_out.append(r["nodes_out"] or 0)
            cores, mem = num(r["cores"]), num(r["mem"])
            if not cores:
                continue                  # every node out of service
            times.append(t)
            nodes = max(nodes, r["nodes"] or 0)
            a = num(r["alloc"])
            p_alloc.add(a, cores, t)
            share = _share(a, cores)
            alloc_pct.append(share)
            w_alloc.add(t, share)
            capped, over = num(r["capped"]), int(num(r["over"]) or 0)
            if capped is not None and (p_load.value is None or capped > p_load.value):
                over_at_peak = over        # nodes above their cores when the load was highest
            p_load.add(capped, cores, t)
            share = _share(capped, cores)
            load_pct.append(share)
            w_load.add(t, share)
            if over > 0:
                over_runs += 1
            ma = num(r["mem_alloc"])
            most_mem_alloc = max(most_mem_alloc, ma or 0)
            p_mem_alloc.add(ma, mem, t)
            share = _share(ma, mem)
            mem_alloc_pct.append(share)
            w_mem_alloc.add(t, share)
            mh = num(r["mem_held"])
            p_mem_held.add(mh, mem, t)
            share = _share(mh, mem)
            mem_held_pct.append(share)
            w_mem_held.add(t, share)
        if not times:
            if seen and all_out is not None:
                all_out.append({"site": _site_name(name, "this site"), "readings": len(seen),
                                "from": iso(seen[0]), "until": iso(seen[-1])})
            continue
        tracked = most_mem_alloc > 0
        held = ({"peak": p_mem_held.out(1, 1024), "mean_pct": _mean(mem_held_pct)}
                if p_mem_held.value is not None else None)
        busiest, quietest = w_alloc.ranked()
        out.append({
            "site": _site_name(name, "this site"),
            "readings": len(times), **_span(times, now),
            "every_minutes": _every_minutes(times), "nodes": nodes,
            "nodes_out": {"most": max(nodes_out, default=0), "mean": _mean(nodes_out)},
            "cores": {
                "allocated": {"peak": p_alloc.out(), "mean_pct": _mean(alloc_pct)},
                "load": {"peak": _with(p_load.out(), nodes_above_cores=over_at_peak),
                         "mean_pct": _mean(load_pct),
                         "over_cores_pct": round(100 * over_runs / len(times), 1),
                         "over_cores_readings": over_runs},
            },
            "memory": {
                "allocated": ({"peak": p_mem_alloc.out(1, 1024), "mean_pct": _mean(mem_alloc_pct)}
                              if tracked else {"tracked": False}),
                "held": held,
            },
            "week": {"cores_allocated_pct": w_alloc.means(), "load_pct": w_load.means(),
                     "memory_held_pct": w_mem_held.means() if held else None,
                     "memory_allocated_pct": w_mem_alloc.means() if tracked else None},
            "busiest": busiest, "quietest": quietest, "ranked_by": "cores_allocated_pct",
        })
    return out


# ── Interactive servers ─────────────────────────────────────────────────

def _machine(conn: sqlite3.Connection, site, since: str, until: str) -> dict | None:
    """The machine's own busy cores, from mpstat_summary at that site."""
    cols = columns(conn, "mpstat_summary")
    if not {"timestamp", "num_cores", "avg_busy_percent"} <= cols:
        return None
    where, args = "", []
    expr = site_expr(cols)
    if site is not None:
        if expr is None:
            return None
        where, args = f" AND {expr} = ?", [site]
    rows = fetch(conn, "SELECT timestamp AS t, num_cores AS cores, avg_busy_percent AS busy "
                       f"FROM mpstat_summary WHERE timestamp >= ? AND timestamp <= ?{where} "
                       "ORDER BY timestamp", [since, until] + args)
    peak, week, shares, times = Peak(), Week(), [], []
    for r in rows:
        t, cores, busy = parse_time(r["t"]), num(r["cores"]), num(r["busy"])
        if t is None or not cores or busy is None:
            continue
        times.append(t)
        peak.add(cores * busy / 100, cores, t)
        shares.append(busy)
        week.add(t, busy)
    if not times:
        return None
    return {"readings": len(times), "every_minutes": _every_minutes(times),
            "busy_cores": {"peak": peak.out(1), "mean_pct": _mean(shares)},
            "week": {"busy_pct": week.means()}}


def interactive(conn: sqlite3.Connection, since: str, until: str, now: datetime) -> list:
    cols = columns(conn, "interactive_hourly")
    if not {"hour", "server_id", "runs", "people_sum", "people_max"} <= cols:
        return []
    site = site_expr(cols) or "NULL"

    def col(name):
        return name if name in cols else "NULL"
    rows = fetch(conn, f"""
        SELECT {site} AS site, server_id AS server, hour, runs, {col('measured_runs')} AS mruns,
               people_sum, people_max, {col('busy_people_sum')} AS bsum,
               {col('busy_people_max')} AS bmax, {col('mem_mb_max')} AS mem
        FROM interactive_hourly WHERE hour >= ? AND hour <= ? ORDER BY 1, 2, 3""",
                 (since[:13], until[:13]))
    groups: dict = {}
    for r in rows:
        groups.setdefault((r["site"], r["server"]), []).append(r)
    servers_at: dict = {}
    for s, _server in groups:
        servers_at[s] = servers_at.get(s, 0) + 1
    out = []
    for (name, server), hours in groups.items():
        p_people, p_busy, p_mem = Peak(), Peak(), Peak()
        w_people, w_busy = Week(), Week()
        people, busy, times = [], [], []
        for r in hours:
            try:
                t = datetime.strptime(str(r["hour"])[:13], "%Y-%m-%dT%H")
            except ValueError:
                continue
            runs = num(r["runs"]) or 0
            if not runs:
                continue
            times.append(t)
            p_people.add(num(r["people_max"]), None, t)
            mean = num(r["people_sum"]) / runs if num(r["people_sum"]) is not None else None
            people.append(mean)
            w_people.add(t, mean)
            mruns = num(r["mruns"])
            if mruns:
                p_busy.add(num(r["bmax"]), None, t)
                b = num(r["bsum"]) / mruns if num(r["bsum"]) is not None else None
                busy.append(b)
                w_busy.add(t, b)
            p_mem.add(num(r["mem"]), None, t)
        if not times:
            continue
        busiest, quietest = w_people.ranked()
        entry = {
            "site": _site_name(name, "this site"), "server": server,
            "hours": len(times), **_span(times, now),
            "people": {"peak": p_people.out(), "mean": _mean(people)},
            "computing": {"peak": p_busy.out(), "mean": _mean(busy)} if busy else None,
            "session_memory_gb": {"peak": p_mem.out(1, 1024)} if p_mem.value else None,
            "week": {"people": w_people.means(), "computing": w_busy.means() if busy else None},
            "busiest": busiest, "quietest": quietest, "ranked_by": "people",
        }
        # The machine's own CPU: where its collector runs on it (one server at the site).
        if servers_at.get(name) == 1:
            m = _machine(conn, name, iso(times[0]), until)
            if m is not None:
                entry["machine"] = m
        out.append(entry)
    return out


# ── Workstations ────────────────────────────────────────────────────────

# Accounts below this uid are the system's (root, the login screen, services).
SYSTEM_UID_BELOW = 1000


def _people_cpu(conn: sqlite3.Connection, since: str, until: str) -> dict:
    """{(site, host): {ten-minute bucket: cores}}: the cores people's sessions
    kept busy, from each person's CPU counter (workstation_user_snapshot).
    Each person's ten minutes is the average of their readings' intervals
    ending in it; the machine's is the sum over its people, and 0 where the
    probe read only the machine's own accounts (nobody used it).

    An interval counts within one life of the person's slice, as
    nomad.workstation_sessions tells them apart: not across a gap longer
    than SESSION_GAP, nor where the CPU time or the memory peak goes down
    (the slice was made again). One pass over the readings in order, the
    times worked out by SQLite (seconds since an epoch, from julianday)."""
    cols = columns(conn, "workstation_user_snapshot")
    if not {"timestamp", "hostname", "uid", "cpu_usage_usec"} <= cols:
        return {}
    site = site_expr(cols) or "NULL"
    peak = ("CASE WHEN typeof(memory_peak_bytes) IN ('integer', 'real') THEN memory_peak_bytes END"
            if "memory_peak_bytes" in cols else "NULL")
    rows = conn.execute(f"""
        SELECT {site}, hostname, uid, julianday(timestamp) * 86400.0,
               CASE WHEN typeof(cpu_usage_usec) IN ('integer', 'real') THEN cpu_usage_usec END,
               {peak}, substr(timestamp, 1, {BUCKET})
        FROM workstation_user_snapshot
        WHERE timestamp >= ? AND timestamp <= ? AND cpu_usage_usec IS NOT NULL
        ORDER BY 1, 2, 3, timestamp""", (since, until))
    out: dict = {}
    key = machine = bucket = None
    t0 = c0 = p0 = None
    total, n = 0.0, 0
    for s, host, uid, t, c, pk, b in rows:
        if not isinstance(uid, int):
            try:
                uid = int(float(uid))
            except (TypeError, ValueError):
                uid = -1
        if uid < SYSTEM_UID_BELOW:
            # The machine's own accounts (its root logins, the login screen):
            # not people, but they show the probe ran then, so a machine
            # nobody used reads 0 rather than "not measured".
            out.setdefault((s, host), {}).setdefault(b, 0.0)
            continue
        if (s, host, uid) != key:
            if n:
                machine[bucket] = machine.get(bucket, 0.0) + total / n
            key, bucket, total, n = (s, host, uid), None, 0.0, 0
            machine = out.setdefault((s, host), {})
            t0, c0, p0 = t, c, pk
            continue
        if b != bucket:
            if n:
                machine[bucket] = machine.get(bucket, 0.0) + total / n
            bucket, total, n = b, 0.0, 0
        if t is not None and t0 is not None and type(c) in (int, float) and type(c0) in (int, float):
            dt, dc = t - t0, c - c0
            if 0 < dt <= SESSION_GAP and dc >= 0 and not (pk is not None and p0 is not None and pk < p0):
                total += dc / 1e6 / dt
                n += 1
        t0, c0, p0 = t, c, pk
    if n:
        machine[bucket] = machine.get(bucket, 0.0) + total / n
    return out


_ADDRESS = re.compile(r"^[0-9.:]+$")


def _machine_key(name) -> str:
    """A machine's name for matching storage: lower case, without its domain
    (an address stays whole): "NAS.example.edu" and "nas" are one machine."""
    n = str(name or "").strip().lower()
    return n if _ADDRESS.match(n) else n.split(".")[0]


def _labs(conn: sqlite3.Connection, config: dict | None, now: datetime) -> tuple:
    """(host -> set of lab groups, storage keys, group -> the lab's name), by
    nomad's rules for labs, the same as the Console's My Lab and
    nomad.facts.labs: a machine is a lab's when [console.labs.resources]
    lists it, or when its collector tags it with the lab's group (its latest
    ``department`` in the last TAG_WINDOW), every lab that does. Storage is
    anything nomad.toml lists as storage: a lab's storage, [console.storage]
    servers and their names, the storage collector's devices (matched by
    _machine_key); a NAS the workstation collector reads too is not a
    workstation."""
    if not config:
        return {}, set(), {}
    from nomad.config.access import TAG_WINDOW, access_from
    acc = access_from(config, log=False)
    labs: dict = {}
    storage: set = set()
    hidden = getattr(acc, "not_labs", frozenset())      # never labs (1.17.10)
    for group, kinds in acc.resources.items():
        for host in kinds.get("workstations", ()):
            if group not in hidden:
                labs.setdefault(host, set()).add(group)
        storage |= {_machine_key(entry.partition(":")[0]) for entry in kinds.get("storage", ())}
    for server, (name, _note) in acc.servers.items():
        storage |= {_machine_key(server)} | ({_machine_key(name)} if name else set())
    st = (config.get("collectors") or {}).get("storage") or {}
    for entry in (st.get("storage_devices") or config.get("storage_devices") or []):
        if isinstance(entry, dict) and isinstance(entry.get("hostname"), str):
            storage.add(_machine_key(entry["hostname"]))
    cols = columns(conn, "workstation_state")
    if {"hostname", "timestamp", "department"} <= cols:
        latest: dict = {}
        for host, when, dept in conn.execute(
                "SELECT hostname, timestamp, department FROM workstation_state WHERE timestamp >= ?",
                ((now - TAG_WINDOW).isoformat(),)):
            if host not in latest or str(when) > latest[host][0]:
                latest[host] = (str(when), dept)
        for host, (_, dept) in latest.items():
            if isinstance(dept, str) and dept.strip() and dept.strip() not in hidden:
                labs.setdefault(host, set()).add(dept.strip())
    return labs, storage, dict(acc.lab_names)


def workstations(conn: sqlite3.Connection, since: str, until: str, now: datetime,
                 names: bool = True, config: dict | None = None) -> list:
    """Each lab's workstations together (nomad's rules for labs: see _labs;
    a machine two labs share counts in both), and machines in no lab by the
    site that collects them. Storage the workstation collector also reads is
    left out. A machine read by two sites (its collection moved, or two
    collectors read it) is one machine: each ten minutes counts once, with
    the most either site saw, and its people's CPU from either."""
    cols = columns(conn, "workstation_state")
    if not {"timestamp", "hostname", "cpu_count"} <= cols:
        return []
    site = site_expr(cols) or "NULL"
    mem = "AVG(memory_used_mb)" if "memory_used_mb" in cols else "NULL"
    mem_top = "MAX(memory_used_mb)" if "memory_used_mb" in cols else "NULL"
    memtot = "MAX(memory_total_mb)" if "memory_total_mb" in cols else "NULL"
    # Each machine's readings in ten minutes: when it reported, its size and
    # its memory in use (averaged for the group, the highest for itself).
    rows = conn.execute(f"""
        SELECT {site}, hostname, substr(timestamp, 1, {BUCKET}),
               MAX(cpu_count), {memtot}, {mem}, {mem_top}
        FROM workstation_state
        WHERE timestamp >= ? AND timestamp <= ? AND cpu_count > 0
        GROUP BY 1, 2, 3 ORDER BY 1, 3""", (since, until)).fetchall()
    cpu = _people_cpu(conn, since, until)
    lab_of, storage, lab_names = _labs(conn, config, now)
    # One reading per machine and ten minutes, whichever sites read it.
    slots: dict = {}            # (host, bucket) -> [site set, cores, memtot, mem, mem_top, busy cores, measured]
    machine_sites: dict = {}
    for _site, host, bucket, cores, memtot_mb, m, m_top in rows:
        if _machine_key(host) in storage:
            continue
        cores, memtot_mb, m, m_top = _n(cores), _n(memtot_mb), _n(m), _n(m_top)
        if not cores:
            continue
        people = cpu.get((_site, host))
        busy = people.get(bucket) if people else None
        slot = slots.get((host, bucket))
        if slot is None:
            slots[(host, bucket)] = [{_site}, cores, memtot_mb, m, m_top, busy, bool(people)]
        else:
            slot[0].add(_site)
            slot[1] = max(slot[1], cores)
            slot[2] = max(slot[2] or 0, memtot_mb or 0) or None
            slot[3] = max(slot[3] or 0, m or 0) if (slot[3] is not None or m is not None) else None
            slot[4] = max(slot[4] or 0, m_top or 0) if (slot[4] is not None or m_top is not None) else None
            slot[5] = busy if slot[5] is None else (slot[5] if busy is None else max(slot[5], busy))
            slot[6] = slot[6] or bool(people)
        machine_sites.setdefault(host, set()).add(_site)
    groups: dict = {}
    for (host, bucket), slot in slots.items():
        labs = lab_of.get(host)
        if labs:
            for lab in labs:
                groups.setdefault(("lab", lab), []).append((host, bucket, slot))
        else:
            # In no lab: by the site that collects it (the first, when two do).
            first = sorted(machine_sites[host], key=str)[0]
            groups.setdefault(("site", first), []).append((host, bucket, slot))
    when: dict = {}                         # a ten-minute bucket's text -> its time
    out = []
    lab_number = 0
    for (kind, name), readings in sorted(groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]))):
        hosts: dict = {}
        sites: set = set()
        buckets: dict = {}
        measured_hosts = {h for h, _b, sl in readings if sl[6]}
        for host, bucket, (slot_sites, cores, memtot_mb, m, m_top, people_busy, _meas) in readings:
            t = when.get(bucket)
            if t is None and bucket not in when:
                t = when[bucket] = parse_time(str(bucket) + "0")
            if t is None:
                continue
            sites |= slot_sites
            h = hosts.get(host)
            if h is None:
                h = hosts[host] = {"cores": 0, "memtot": 0, "peak": Peak(), "peak_mem": Peak(),
                                   "busy": [], "week": Week(), "cpu": host in measured_hosts}
            h["cores"] = max(h["cores"], cores)
            h["memtot"] = max(h["memtot"], memtot_mb or 0)
            busy = min(people_busy or 0.0, cores)          # no one's sessions: none
            h["peak"].add(busy, cores, t)
            h["peak_mem"].add(m_top, memtot_mb, t)
            share = 100 * busy / cores
            h["busy"].append(share)
            h["week"].add(t, share)
            b = buckets.get(t)
            if b is None:
                b = buckets[t] = [0.0, 0.0, 0]
            b[0] += busy
            b[1] += m or 0
            b[2] += busy >= BUSY_CORE
        if not hosts:
            continue
        measured = any(h["cpu"] for h in hosts.values())
        cores_total = sum(h["cores"] for h in hosts.values())
        mem_total = sum(h["memtot"] for h in hosts.values())
        p_busy, p_mem, p_machines = Peak(), Peak(), Peak()
        w_busy, w_machines = Week(), Week()
        busy_pct, machines = [], []
        times = sorted(buckets)
        for t in times:
            busy, m, n = buckets[t]
            p_busy.add(busy, cores_total, t)
            p_mem.add(m, mem_total or None, t)
            p_machines.add(n, len(hosts), t)
            share = _share(busy, cores_total)
            busy_pct.append(share)
            w_busy.add(t, share)
            machines.append(n)
            w_machines.add(t, n)
        busiest, quietest = w_busy.ranked() if measured else ([], [])
        detail = []
        for i, (host, h) in enumerate(sorted(hosts.items(),
                                             key=lambda kv: (-statistics.fmean(kv[1]["busy"]), kv[0]))):
            top, _ = h["week"].ranked(1)
            detail.append({
                "name": host if names else f"machine {i + 1}",
                "cores": int(h["cores"]),
                "memory_gb": round(h["memtot"] / 1024, 1) if h["memtot"] else None,
                "cpu_measured": bool(h["cpu"]),
                "mean_busy_pct": _mean(h["busy"]) if h["cpu"] else None,
                "peak_cores": h["peak"].out(1) if h["cpu"] else None,
                "peak_memory_gb": h["peak_mem"].out(1, 1024),
                "busiest": (top[0] if top else None) if h["cpu"] else None,
            })
        site_names = sorted(_site_name(x, "this site") for x in sites)
        lab, label = None, _site_name(name, "this site")
        if kind == "lab":
            lab_number += 1
            # A lab is named for its PI: numbered too when names are left out.
            lab, label = (name, lab_names.get(name) or name) if names else (f"lab {lab_number}",) * 2
        out.append({
            # A lab's machines (lab: its group; label: its name), or a site's
            # machines in no lab (lab None; label: the site).
            "lab": lab,
            "label": label,
            # A lab's own head node can name it: no sites for a lab when
            # names are left out.
            "site": (None if kind == "lab" and not names
                     else site_names[0] if len(site_names) == 1 else ", ".join(site_names)),
            "sites": [] if kind == "lab" and not names else site_names,
            "machines": len(hosts), **_span(times, now),
            "cores": int(cores_total), "memory_gb": round(mem_total / 1024, 1) if mem_total else None,
            "cpu_measured": measured,
            "busy_cores": ({"peak": p_busy.out(1), "mean_pct": _mean(busy_pct)} if measured else None),
            "memory_in_use": {"peak": p_mem.out(1, 1024)} if mem_total else None,
            "machines_busy": ({"peak": p_machines.out(), "mean": _mean(machines)} if measured else None),
            "week": {"busy_cores_pct": w_busy.means() if measured else None,
                     "machines_busy": w_machines.means() if measured else None},
            "busiest": busiest, "quietest": quietest, "ranked_by": "busy_cores_pct",
            "each": detail,
        })
    return out


def busy(conn: sqlite3.Connection, days: int = 30, now: datetime | None = None,
         names: bool = True, config: dict | None = None) -> dict:
    """When each cluster, interactive server and group of workstations is
    busiest over the last ``days``. ``names``: list workstations by name
    (for staff). ``config`` (nomad.toml): workstations by lab, storage left
    out; without it, by the site that collects them."""
    now = now or datetime.now()
    _since, _until, win = window(days, now)
    since = iso(now - timedelta(days=win["days"]))
    until = now.isoformat()                 # with its microseconds: the last reading counts
    result = envelope("busy", now, win)
    all_out: list = []
    result["clusters"] = clusters(conn, since, until, now, all_out)
    result["clusters_all_out"] = all_out          # every node out of service all window
    result["interactive"] = interactive(conn, since, until, now)
    result["workstations"] = workstations(conn, since, until, now, names=names, config=config)
    result["weekdays"] = list(WEEKDAYS)
    result["notes"] = [
        "Cores allocated is what Slurm handed out, of the cores of the nodes in service (down, "
        "not responding, in maintenance or drained with nothing running: left out, counted in "
        "nodes_out).",
        "Load counts threads ready to run as well as running, and processes waiting on storage; "
        "each node's counts up to its cores. over_cores_pct: readings with a node above its cores "
        "(where jobs are held to their cores, task/cgroup: jobs started more threads than cores "
        "they asked for, or were waiting on storage). It is not cores in use.",
        "Memory allocated is Slurm's AllocMem; 'not tracked' where Slurm doesn't count memory "
        "(SelectTypeParameters without _Memory). Memory held is, on the nodes running jobs, each "
        "node's memory less what Slurm reports free, the kernel's free memory, which counts the "
        "file cache as used: jobs used this much or less. Nodes running nothing count as none; the "
        "share is of the memory of all the nodes in service. A free figure of 0 (Slurm's N/A) is "
        "not counted.",
        "Interactive servers: people with a session open, people computing, the memory their "
        "sessions held, and the machine's own busy cores where its collector runs on it.",
        "Workstations, by lab (a machine nomad.toml lists for a lab, or its collector tags with "
        "the lab's group; others by the site that collects them; storage left out): the CPUs "
        "people's sessions kept busy, from their CPU counters (the machines' own services not "
        "included); a machine is busy with a CPU or more in use. A CPU is what the system counts "
        "as one: a hardware thread, two per core with hyperthreading. Clusters' CPUs are "
        "Slurm's.",
        f"A typical week averages each weekday's hour over the window; an hour is ranked only "
        f"with readings on {MIN_DAYS} days or more. Times are each site's local time; 'stopped' "
        f"means the last reading is more than {STALE_AFTER_HOURS} hours old.",
    ]
    return result

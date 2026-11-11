# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""One node over a window: its readings and what happened on it (1.41.0).

    node(conn, "spydur", "n012", since, until, staff=True)

The question Grafana answers with a query ("what was this node doing on
Tuesday afternoon?"), answered once, so the Console's node timeline, a link
someone shares, the command line (``nomad facts node``) and, later, the local
AI model's tool all get the same numbers.

Readings (``series``), each a list of [time, value] at one ``step`` (the
smallest of STEPS giving no more than MAX_POINTS points), averaged within a
step (temperatures: the highest):

- cores allocated, and the load (Slurm's CPULoad), as shares of the node's cores;
- memory allocated (where Slurm allocates it) and memory held: the node's
  memory less what Slurm reports free, so the file cache counts as held;
- GPUs busy (DCGM's measure; nvidia-smi's "utilization" is not used, as on
  Overview), GPU memory in use and the hottest GPU;
- the hottest CPU or board sensor (hw_temps; hourly figures where the
  readings themselves are no longer kept);
- the CPU clock as a share of its top speed (cpu_clock: throttling);
- the fullest local disk (node_disks).

What happened (``events``), each with when (and until when, for a span):

- ``state``: the node drained, down or back in service (and, for staff,
  Slurm's reason: free text an admin wrote, which can name people);
- ``gap``: no node reading for a while (the site, or nomad, not reporting);
- ``job``: a job started or ended on it. Staff: every job, with whose; anyone
  else: their own jobs, and the others only counted (``jobs.others``);
- ``oom``: the kernel ended a process for want of memory: whose, for staff
  and for the person themself; for anyone else, only that it happened;
- ``power``: a power supply's event (IPMI);
- ``alert``: nomad's alerts about the node (staff: the text can name people);
- ``maintenance``: a declared window (nomad maintenance) for its site; who
  declared it, for staff.

``summary`` says the same in sentences, for the command line and a reader
that wants words.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from nomad.facts._common import columns, envelope, fetch, iso, num, parse_time

STEPS = (5, 15, 30, 60, 180, 360, 720, 1440)      # minutes
MAX_POINTS = 400
MAX_DAYS = 92
GAP_FACTOR = 3            # no reading for this many usual intervals: a gap
GAP_LEAST_MINUTES = 20
JOBS_SHOWN = 300          # job events kept (the latest), the rest counted
EVENTS_SHOWN = 200        # of each other kind
LOOKBACK_DAYS = 30        # a job started this long before the window may still be running in it
RUNNING = ("RUNNING", "COMPLETING", "CONFIGURING", "SUSPENDED")

SERIES = {
    "cpu_alloc_pct": {"label": "Cores allocated", "unit": "%", "group": "cpu"},
    "load_pct": {"label": "Load, of its cores", "unit": "%", "group": "cpu"},
    "mem_alloc_pct": {"label": "Memory allocated", "unit": "%", "group": "memory"},
    "mem_held_pct": {"label": "Memory held, file cache counted", "unit": "%", "group": "memory"},
    "gpu_busy_pct": {"label": "GPUs busy (DCGM)", "unit": "%", "group": "gpu"},
    "gpu_mem_pct": {"label": "GPU memory in use", "unit": "%", "group": "gpu"},
    "gpu_temp_c": {"label": "Hottest GPU", "unit": "°C", "group": "temperature"},
    "temp_c": {"label": "Hottest CPU or board sensor", "unit": "°C", "group": "temperature"},
    "clock_pct": {"label": "CPU clock, of its top speed", "unit": "%", "group": "clock"},
    "disk_used_pct": {"label": "Fullest local disk", "unit": "%", "group": "disk"},
}


class NodeFactsError(ValueError):
    """What was asked can't be answered as asked; said as it is."""


def step_minutes(since: datetime, until: datetime) -> int:
    span = (until - since).total_seconds() / 60
    return next((s for s in STEPS if span / s <= MAX_POINTS), STEPS[-1])


def _site(cols: set, site, alias: str = "") -> tuple[str, list]:
    a = f"{alias}." if alias else ""
    if site is None:
        return "", []
    if "source_site" in cols:
        return f" AND {a}source_site = ?", [site]
    if "cluster" in cols:
        return f" AND {a}cluster = ?", [site]
    return "", []


class _Buckets:
    """Values per step: averaged, or the highest (``high``)."""

    def __init__(self, since: datetime, step: int, high: bool = False):
        self.since, self.step, self.high = since, step * 60, high
        self.cells: dict = {}

    def add(self, t: datetime | None, v) -> None:
        if t is None or v is None:
            return
        i = int((t - self.since).total_seconds() // self.step)
        if i < 0:
            return
        c = self.cells.setdefault(i, [0.0, 0, None])
        c[0] += v
        c[1] += 1
        c[2] = v if c[2] is None else max(c[2], v)

    def points(self) -> list:
        out = []
        for i in sorted(self.cells):
            s, n, top = self.cells[i]
            v = top if self.high else s / n
            out.append([iso(self.since + timedelta(seconds=i * self.step))[:16], round(v, 1)])
        return out


def _condition(state, alloc) -> str:
    """up, draining (drained but still running jobs), drained or down."""
    from nomad.facts.sites import _condition as cond
    c = cond(state)
    if c == "drained" and (alloc or 0) > 0:
        return "draining"
    return c


CONDITION_WORDS = {"up": "in service", "draining": "draining (no new jobs; its jobs finishing)",
                   "drained": "drained (out of service)", "down": "down or not responding"}


def _node_rows(conn, cols, site, name, since, until) -> tuple[list, dict | None]:
    want = [c for c in ("timestamp", "state", "cpus_total", "cpus_alloc", "cpu_load", "memory_total_mb",
                        "memory_alloc_mb", "memory_free_mb", "partitions", "reason", "gres") if c in cols]
    where, args = _site(cols, site)
    rows = fetch(conn, f"SELECT {', '.join(want)} FROM node_state WHERE node_name = ? AND timestamp >= ? "
                       f"AND timestamp <= ?{where} ORDER BY timestamp", [name, iso(since), iso(until)] + args)
    before = fetch(conn, f"SELECT {', '.join(want)} FROM node_state WHERE node_name = ? AND timestamp < ?"
                         f"{where} ORDER BY timestamp DESC LIMIT 1", [name, iso(since)] + args)
    return rows, (before[0] if before else None)


def _gaps(times: list, since: datetime, until: datetime, now: datetime) -> list:
    if len(times) < 2:
        return []
    deltas = sorted((b - a).total_seconds() / 60 for a, b in zip(times, times[1:]) if b > a)
    usual = deltas[len(deltas) // 2] if deltas else 5
    least = max(GAP_LEAST_MINUTES, GAP_FACTOR * usual)
    out = []
    edges = list(zip(times, times[1:]))
    end = min(until, now)
    if (end - times[-1]).total_seconds() / 60 > least:
        edges.append((times[-1], end))
    for a, b in edges:
        if (b - a).total_seconds() / 60 > least:
            out.append({"at": iso(a), "until": iso(b), "kind": "gap",
                        "text": f"no reading for {_dur((b - a).total_seconds() / 60)}"})
    return out


def _dur(minutes: float) -> str:
    if minutes < 90:
        return f"{round(minutes)} min"
    if minutes < 48 * 60:
        return f"{minutes / 60:.1f} h".replace(".0 h", " h")
    return f"{minutes / 1440:.1f} days".replace(".0 days", " days")


def _jobs(conn, site, name, since, until, staff, user) -> tuple[list, dict]:
    from nomad.hostlist import expand_hostlist
    jcols = columns(conn, "jobs")
    counts = {"started": 0, "ended": 0, "failed": 0, "node_fail": 0, "running_at_end": 0,
              "others": {"started": 0, "ended": 0}}
    if not {"node_list", "start_time", "state"} <= jcols:
        return [], counts
    where, args = _site(jcols, site)

    def col(c):
        return c if c in jcols else f"NULL AS {c}"
    rows = fetch(conn,
                 f"SELECT {col('job_id')}, {col('user_name')}, {col('job_name')}, state, node_list, start_time, "
                 f"{col('end_time')}, {col('req_cpus')}, {col('req_gpus')} FROM jobs "
                 "WHERE start_time >= ? AND start_time <= ? "
                 "AND (end_time IS NULL OR end_time >= ?)" + where,
                 [iso(since - timedelta(days=LOOKBACK_DAYS)), iso(until), iso(since)] + args)
    events = []
    me = (user or "").lower()
    for r in rows:
        if name not in expand_hostlist(r["node_list"]):
            continue
        state = str(r["state"] or "").split(" ")[0].upper()
        start, end = parse_time(r["start_time"]), parse_time(r["end_time"])
        mine = staff or (me and str(r["user_name"] or "").lower() == me)
        for t, what in ((start, "started"), (end, "ended")):
            if t is None or not (since <= t <= until):
                continue
            counts[what] += 1
            # How a job ended is its owner's: counted for staff, and for a person, their own.
            if what == "ended" and mine and state in ("FAILED", "TIMEOUT", "OUT_OF_MEMORY"):
                counts["failed"] += 1
            if what == "ended" and state == "NODE_FAIL":
                counts["node_fail"] += 1
            if not mine:
                counts["others"][what] += 1
                continue
            e = {"at": iso(t), "kind": "job", "what": what, "job_id": str(r["job_id"] or ""),
                 "state": state if what == "ended" else None, "cpus": num(r["req_cpus"]),
                 "gpus": num(r["req_gpus"]) or None, "mine": bool(me and str(r["user_name"] or "").lower() == me)}
            if staff:
                e["user"] = r["user_name"]
            e["text"] = (f"job {e['job_id']} {what}" + (f" ({state.lower().replace('_', ' ')})" if e["state"] else "")
                         + (f", {r['user_name']}" if staff and r["user_name"] else ""))
            events.append(e)
        if state in RUNNING or end is None or end > until:
            counts["running_at_end"] += 1
    events.sort(key=lambda e: e["at"])
    if len(events) > JOBS_SHOWN:
        counts["not_shown"] = len(events) - JOBS_SHOWN
        events = events[-JOBS_SHOWN:]
    return events, counts


def _rows(conn, table, node_col, name, time_col, since, until, site, want) -> list:
    cols = columns(conn, table)
    if not {node_col, time_col} <= cols:
        return []
    where, args = _site(cols, site)
    have = [c for c in want if c in cols]
    return fetch(conn, f"SELECT {time_col} AS t, {', '.join(have) or 'NULL'} FROM {table} "
                       f"WHERE {node_col} = ? AND {time_col} >= ? AND {time_col} <= ?{where} "
                       f"ORDER BY {time_col} LIMIT 200000", [name, iso(since), iso(until)] + args)


def node(conn: sqlite3.Connection, site, name: str, since: datetime, until: datetime, *,
         now: datetime | None = None, staff: bool = False, user: str | None = None,
         config: dict | None = None, maintenance: list | None = None) -> dict:
    """The answer described at the top. ``staff``: everything; else what
    ``user`` (a NetID) may see. ``maintenance``: nomad.maintenance windows
    (default: as nomad.toml says where they are kept)."""
    now = now or datetime.now()
    if until <= since:
        raise NodeFactsError("the window ends before it starts")
    if (until - since).days > MAX_DAYS:
        raise NodeFactsError(f"a window of {MAX_DAYS} days at most")
    step = step_minutes(since, until)
    result = envelope("node", now, {"since": iso(since), "until": iso(until), "step_minutes": step})
    cols = columns(conn, "node_state")
    if not {"node_name", "timestamp"} <= cols:
        raise NodeFactsError("this database has no node readings")
    rows, before = _node_rows(conn, cols, site, name, since, until)
    latest = rows[-1] if rows else before
    if latest is None:
        raise NodeFactsError(f"no readings of {name}" + (f" at {site}" if site else ""))

    from nomad.facts.sites import _gpu_count
    info = {"site": site, "name": name, "partitions": (latest.get("partitions") or "").replace("*", ""),
            "cpus": int(num(latest.get("cpus_total")) or 0),
            "memory_gb": round((num(latest.get("memory_total_mb")) or 0) / 1024, 1),
            "gpus": _gpu_count(latest.get("gres")),
            "condition": _condition(latest.get("state"), num(latest.get("cpus_alloc"))),
            "as_of": str(latest.get("timestamp")), "readings": len(rows)}
    if staff and latest.get("reason"):
        info["reason"] = latest["reason"]
    try:
        from nomad.config.access import access_from
        note = access_from(config or {}, log=False).note_of(name)
        if note:
            info["note"] = note
    except Exception:
        pass
    result["node"] = info

    b = {k: _Buckets(since, step, high=k in ("gpu_temp_c", "temp_c", "disk_used_pct")) for k in SERIES}
    events: list = []
    times = []
    prev = _condition(before.get("state"), num(before.get("cpus_alloc"))) if before else None
    mem_tracked = False
    for r in rows:
        t = parse_time(r["timestamp"])
        if t is None:
            continue
        times.append(t)
        total, alloc = num(r.get("cpus_total")), num(r.get("cpus_alloc"))
        cond = _condition(r.get("state"), alloc)
        if cond != prev and not (prev is None and cond == "up"):    # in service from the start: nothing to say
            e = {"at": iso(t), "kind": "state", "condition": cond, "was": prev,
                 "text": CONDITION_WORDS.get(cond, cond)}
            if staff and cond != "up" and r.get("reason"):
                e["reason"] = r["reason"]
                e["text"] += f": {r['reason']}"
            events.append(e)
            prev = cond
        if cond in ("drained", "down"):
            continue                       # out of service: no figures for the time
        if total:
            b["cpu_alloc_pct"].add(t, 100 * (alloc or 0) / total)
            load = num(r.get("cpu_load"))
            if load is not None:
                b["load_pct"].add(t, 100 * load / total)
        mem = num(r.get("memory_total_mb"))
        if mem:
            ma = num(r.get("memory_alloc_mb"))
            if ma is not None:
                mem_tracked = mem_tracked or ma > 0
                b["mem_alloc_pct"].add(t, 100 * ma / mem)
            free = num(r.get("memory_free_mb"))
            if free and free > 0:
                b["mem_held_pct"].add(t, 100 * max(0.0, mem - free) / mem)
    if not mem_tracked:
        b["mem_alloc_pct"].cells.clear()   # Slurm here doesn't allocate memory: always 0, not "none in use"
    # A span for each time out of service, closed by the next change (or the window's end).
    spans = [e for e in events if e["kind"] == "state"]
    for e, nxt in zip(spans, spans[1:] + [None]):
        if e["condition"] != "up":
            e["until"] = nxt["at"] if nxt else iso(min(until, now))
    events += _gaps(times, since, until, now)

    for r in _rows(conn, "gpu_stats", "node_name", name, "timestamp", since, until, site,
                   ("gpu_index", "real_util_pct", "data_source", "memory_used_mb", "memory_total_mb",
                    "temperature_c")):
        t = parse_time(r["t"])
        if r.get("data_source") == "dcgm":
            b["gpu_busy_pct"].add(t, num(r.get("real_util_pct")))
        used, tot = num(r.get("memory_used_mb")), num(r.get("memory_total_mb"))
        if used is not None and tot:
            b["gpu_mem_pct"].add(t, 100 * used / tot)
        b["gpu_temp_c"].add(t, num(r.get("temperature_c")))

    temps = _rows(conn, "hw_temps", "node", name, "timestamp", since, until, site, ("temp_c", "crit_c"))
    crit = [num(r.get("crit_c")) for r in temps if num(r.get("crit_c"))]
    for r in temps:
        b["temp_c"].add(parse_time(r["t"]), num(r.get("temp_c")))
    if not temps:
        for r in _rows(conn, "hw_temps_hourly", "node", name, "hour", since, until, site, ("max_c",)):
            b["temp_c"].add(parse_time(r["t"]), num(r.get("max_c")))

    for r in _rows(conn, "cpu_clock", "node", name, "timestamp", since, until, site, ("mhz", "max_mhz")):
        mhz, top = num(r.get("mhz")), num(r.get("max_mhz"))
        if mhz and top:
            b["clock_pct"].add(parse_time(r["t"]), 100 * mhz / top)

    disks = set()
    for r in _rows(conn, "node_disks", "node", name, "timestamp", since, until, site, ("path", "used_percent")):
        b["disk_used_pct"].add(parse_time(r["t"]), num(r.get("used_percent")))
        disks.add(r.get("path"))

    series, info_series = {}, {}
    for k, buckets in b.items():
        pts = buckets.points()
        if pts:
            series[k] = pts
            info_series[k] = dict(SERIES[k])
    if "temp_c" in info_series and crit:
        info_series["temp_c"]["critical"] = min(crit)
    if "disk_used_pct" in info_series:
        info_series["disk_used_pct"]["paths"] = sorted(p for p in disks if p)
    result["series"] = series
    result["series_info"] = info_series

    job_events, counts = _jobs(conn, site, name, since, until, staff, user)
    events += job_events

    me = (user or "").lower()
    for r in _rows(conn, "oom_kills", "host", name, "timestamp", since, until, site, ("process", "username")):
        own = staff or (me and str(r.get("username") or "").lower() == me)
        e = {"at": str(r["t"]), "kind": "oom", "text": "out of memory: the kernel ended a process"}
        if own:
            e["process"] = r.get("process")
            e["text"] += f" ({r.get('process')}" + (f", {r.get('username')})" if staff else ")")
            if staff:
                e["user"] = r.get("username")
        events.append(e)

    pcols = columns(conn, "power_events")
    if {"node", "first_seen"} <= pcols:
        for r in _rows(conn, "power_events", "node", name, "first_seen", since, until, site,
                       ("logged", "sensor", "event", "detail", "severity", "problem")):
            events.append({"at": str(r["t"]), "kind": "power", "severity": r.get("severity"),
                           "text": " ".join(str(x) for x in (r.get("sensor"), r.get("event") or r.get("detail")) if x)
                           or "a power event"})

    if staff:
        for r in _rows(conn, "alerts", "host", name, "timestamp", since, until, site,
                       ("severity", "source", "message")):
            events.append({"at": str(r["t"]), "kind": "alert", "severity": r.get("severity"),
                           "text": str(r.get("message") or r.get("source") or "an alert")[:300]})

    for w in _maintenance(config, maintenance):
        if site is not None and w.site not in (site, "all"):
            continue
        a, z = w.starts, w.ends
        if a <= until and z >= since:
            e = {"at": iso(max(a, since)), "until": iso(min(z, until)), "kind": "maintenance",
                 "text": f"maintenance: {w.reason}" if w.reason else "maintenance"}
            if staff and w.by:
                e["by"] = w.by
            events.append(e)

    kinds = {}
    kept = []
    for e in sorted(events, key=lambda e: e["at"]):
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        if e["kind"] == "job" or kinds[e["kind"]] <= EVENTS_SHOWN:
            kept.append(e)
    result["events"] = kept
    result["jobs"] = counts
    result["scope"] = "staff" if staff else "person"
    result["summary"] = _summary(result, since, until)
    return result


def site_nodes(conn: sqlite3.Connection, site) -> list:
    """The site's nodes in its latest node reading, in name order (numbers as numbers)."""
    import re
    cols = columns(conn, "node_state")
    if not {"node_name", "timestamp"} <= cols:
        return []
    where, args = _site(cols, site)
    rows = fetch(conn, f"SELECT DISTINCT node_name FROM node_state WHERE timestamp = (SELECT MAX(timestamp) "
                       f"FROM node_state WHERE 1=1{where}){where}", args + args)
    return sorted((r["node_name"] for r in rows if r["node_name"]),
                  key=lambda n: [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", n)])


def _maintenance(config, given) -> list:
    if given is not None:
        return given
    try:
        from nomad import maintenance
        return maintenance.load(config)
    except Exception:
        return []


def _when(text) -> str:
    t = parse_time(text)
    return t.strftime("%a %d %b %H:%M") if t else str(text)


def _summary(r: dict, since: datetime, until: datetime) -> list:
    n = r["node"]
    s = r["series"]
    out = [f"{n['site'] + ' ' if n['site'] else ''}{n['name']} ({n['cpus']} cores, {n['memory_gb']:g} GB"
           + (f", {n['gpus']} GPUs" if n["gpus"] else "") + f"), {_when(iso(since))} to {_when(iso(until))}: "
           f"now {CONDITION_WORDS.get(n['condition'], n['condition'])}."]

    def spread(key, unit="%"):
        pts = [v for _, v in s.get(key, [])]
        if not pts:
            return None
        lo, hi, mean = round(min(pts)), round(max(pts)), round(sum(pts) / len(pts))
        return (f"{lo}{unit}" if lo == hi else f"{lo} to {hi}{unit}, {mean}{unit} on average")
    for key, words in (("cpu_alloc_pct", "Cores allocated"), ("load_pct", "Load"),
                       ("mem_alloc_pct", "Memory allocated"), ("gpu_busy_pct", "GPUs busy")):
        got = spread(key)
        if got:
            out.append(f"{words}: {got}.")
    for key, words in (("temp_c", "Hottest sensor"), ("gpu_temp_c", "Hottest GPU")):
        pts = s.get(key) or []
        if pts:
            t, v = max(pts, key=lambda p: p[1])
            out.append(f"{words}: {v:g} °C at {_when(t)}.")
    out_spans = [e for e in r["events"] if e["kind"] == "state" and e["condition"] in ("drained", "down")]
    for e in out_spans[:5]:
        words = CONDITION_WORDS.get(e["condition"], e["condition"])
        out.append(f"{words[:1].upper() + words[1:]} from {_when(e['at'])} to {_when(e.get('until'))}"
                   + (f" ({e['reason']})" if e.get("reason") else "") + ".")
    gaps = [e for e in r["events"] if e["kind"] == "gap"]
    if gaps:
        longest = max(gaps, key=lambda e: (parse_time(e["until"]) - parse_time(e["at"])).total_seconds())
        out.append(f"{len(gaps)} gap{'s' if len(gaps) > 1 else ''} in the readings, the longest "
                   f"{longest['text'].replace('no reading for ', '')}, from {_when(longest['at'])}.")
    j = r["jobs"]
    if j["started"] or j["ended"]:
        line = f"{j['started']} job{'s' if j['started'] != 1 else ''} started and {j['ended']} ended on it"
        if j["failed"]:
            line += (f" ({j['failed']} failed, timed out or ran out of memory)" if r["scope"] == "staff"
                     else f" ({j['failed']} of yours failed, timed out or ran out of memory)")
        if j["node_fail"]:
            line += f"; {j['node_fail']} ended by a node failure"
        out.append(line + ".")
    for kind, words in (("oom", "out-of-memory kill"), ("power", "power event"), ("alert", "alert")):
        k = sum(1 for e in r["events"] if e["kind"] == kind)
        if k:
            out.append(f"{k} {words}{'s' if k > 1 else ''}.")
    if any(e["kind"] == "maintenance" for e in r["events"]):
        out.append("A declared maintenance window falls in this time.")
    return out

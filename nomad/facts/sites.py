"""Each site, now: how fresh its data is, its nodes, CPU, memory and GPUs in
use, jobs running and waiting, storage, active alerts and workstations; and
its jobs over a window.

Aggregates only -- counts, shares and sizes, never anyone's name -- so the
answer is for anyone signed in. Two things are left out unless the caller
asks (staff): the reason Slurm gives for an unavailable node, which is free
text an admin wrote and can name people ("jsmith's job wedged it",
"[admin@...]"), and a site's job figures when they rest on fewer than
``min_people`` people besides whoever is asking (one person's outcomes are
theirs; the answer is the same for every asker, so it takes min_people + 1).
Jobs running and waiting are counted whatever their number: Slurm shows
every job in the queue to every user by default.

Every site is read against its OWN latest reading: a site a few hours behind
is shown as stale with what it last said, never averaged in as if current,
and never dropped. Nodes, GPUs and the queue are judged by the node
collector's own latest reading: a site whose disk readings still arrive but
whose node readings stopped is not current about its nodes.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from nomad.facts import jobs as jobfacts
from nomad.facts._common import (MACHINE_STALE_MINUTES, STALE_AFTER_HOURS, age_minutes,
                                 columns, envelope, fetch, iso, num, parse_time, pct, window)

# Tables whose newest row says when a site last reported.
FRESHNESS_TABLES = ("node_state", "filesystems", "workstation_state", "gpu_stats",
                    "queue_state", "storage_state", "iostat_device")
STORAGE_SEEN_DAYS = 7          # a path not read for this long is no longer monitored
STORAGE_TREND_DAYS = 30
STORAGE_MIN_TREND_DAYS = 7
STORAGE_HORIZON_DAYS = 730
WORKSTATION_SEEN_DAYS = 7

# How a Slurm node state reads, as the Console's Dashboard reads it: Slurm
# composes states ("IDLE+DRAIN", "DOWN+NOT_RESPONDING") and marks a node that
# stopped responding with "*" ("IDLE*"); states are read as whole tokens.
_DOWN = {"DOWN", "FAIL", "FAILING", "FAILG", "NOT_RESPONDING", "NO_RESPOND", "UNKNOWN", "UNK",
         "INVALID", "INVALID_REG", "INVAL", "ERROR"}
_DRAIN = {"DRAIN", "DRAINED", "DRAINING", "DRNG"}


def _where_site(cols: set, site, alias: str = "") -> tuple[str, list]:
    """(" AND <site column> = ?", [site]) or ("", []) for one site's database."""
    a = f"{alias}." if alias else ""
    if site is None:
        return "", []
    if "source_site" in cols:
        return f" AND {a}source_site = ?", [site]
    if "cluster" in cols:
        return f" AND {a}cluster = ?", [site]
    return "", []


def site_names(conn: sqlite3.Connection) -> list:
    """The sites in this database: the hub's list (every site sync tried,
    working or not), else the sites the data names, else [None] for one
    site's own database."""
    sync = columns(conn, "sync_sites")
    if "name" in sync:
        names = [r["name"] for r in fetch(conn, "SELECT name FROM sync_sites ORDER BY name")
                 if r["name"]]
        if names:
            return names
    found = set()
    for table in ("node_state", "filesystems", "workstation_state"):
        if "source_site" in columns(conn, table):
            found |= {r["s"] for r in fetch(conn, f"SELECT DISTINCT source_site AS s FROM {table}")
                      if r["s"]}
    return sorted(found) or [None]


def freshness(conn: sqlite3.Connection, site) -> dict:
    """{"newest": time of the site's newest reading, "by_table": {...}}."""
    by_table = {}
    for table in FRESHNESS_TABLES:
        cols = columns(conn, table)
        if "timestamp" not in cols:
            continue
        where, args = _where_site(cols, site)
        if site is not None and not where:
            continue                       # the table can't say whose a row is
        row = fetch(conn, f"SELECT MAX(timestamp) AS t FROM {table} WHERE 1=1{where}", args)
        if row and row[0]["t"]:
            by_table[table] = str(row[0]["t"])
    newest = max(by_table.values(), key=lambda t: parse_time(t) or datetime.min) if by_table else None
    return {"newest": newest, "by_table": by_table}


def queue_current(conn: sqlite3.Connection, site, now: datetime) -> tuple:
    """(as_of, stale) for what a site says about its jobs running and
    waiting: as current as its node collector's latest reading (the same
    cron run records both), else its newest reading of any kind."""
    fresh = freshness(conn, site)
    when = fresh["by_table"].get("node_state") or fresh["newest"]
    age = age_minutes(when, now)
    return when, age is None or age > STALE_AFTER_HOURS * 60


def _condition(state) -> str:
    """'up', 'drained' or 'down' for a Slurm node state."""
    s = str(state or "").strip().upper()
    if not s or "*" in s:
        return "down"                        # no state, or not responding
    tokens = {t.strip("~#%$@^!-") for t in s.split("+")}
    if tokens & _DOWN:
        return "down"
    if tokens & _DRAIN:
        return "drained"
    return "up"


def _gpu_count(gres) -> int:
    """GPUs on a node from its GRES: 'gpu:a100:4(S:0-1)' -> 4, 'gpu:2,gpu:t4:1' -> 3."""
    total = 0
    for part in str(gres or "").split(","):
        part = part.strip()
        if not part.lower().startswith("gpu"):
            continue
        fields = part.split("(")[0].split(":")
        count = next((f for f in reversed(fields[1:]) if f.isdigit()), None)
        total += int(count) if count else 1
    return total


def nodes(conn: sqlite3.Connection, site, now: datetime, stale: bool,
          reasons: bool = False) -> dict | None:
    """The site's nodes in its latest snapshot; None for a site with none.
    ``reasons``: include Slurm's reason for each unavailable node (free text
    an admin wrote; for staff)."""
    cols = columns(conn, "node_state")
    if not {"node_name", "state", "timestamp"} <= cols:
        return None
    where, args = _where_site(cols, site)
    if site is not None and not where:
        return None

    def col(c):
        return c if c in cols else f"NULL AS {c}"
    rows = fetch(conn,
                 f"SELECT node_name, state, timestamp, {col('cpus_total')}, {col('cpus_alloc')}, "
                 f"{col('memory_total_mb')}, {col('memory_alloc_mb')}, {col('gres')}, {col('reason')} "
                 f"FROM node_state WHERE timestamp = (SELECT MAX(timestamp) FROM node_state "
                 f"WHERE 1=1{where}){where}", args + args)
    if not rows:
        return None
    counts = {"up": 0, "drained": 0, "down": 0, "not_reporting": 0}
    unavailable = []
    cores = cores_alloc = mem = mem_alloc = gpus = 0
    for r in rows:
        r.update({k: num(r[k]) or 0 for k in ("cpus_total", "cpus_alloc", "memory_total_mb",
                                               "memory_alloc_mb")})
        cores += r["cpus_total"]
        mem += r["memory_total_mb"]
        gpus += _gpu_count(r["gres"])
        if stale:
            counts["not_reporting"] += 1      # its last reading is not the state now
            continue
        c = _condition(r["state"])
        counts[c] += 1
        if c != "up":
            node = {"node": r["node_name"], "condition": c, "state": r["state"]}
            if reasons:
                node["reason"] = r["reason"]
            unavailable.append(node)
        cores_alloc += r["cpus_alloc"]
        mem_alloc += r["memory_alloc_mb"]
    return {
        "as_of": str(rows[0]["timestamp"]),
        "total": len(rows), **counts,
        "unavailable": sorted(unavailable, key=lambda u: u["node"])[:25],
        "cpu": {"cores": int(cores),
                "allocated_pct": None if stale else pct(cores_alloc, cores)},
        "memory": {"total_gb": round(mem / 1024, 1),
                   "allocated_pct": None if stale else pct(mem_alloc, mem)},
        "gpus": {"count": gpus},
    }


def gpu_use(conn: sqlite3.Connection, site, now: datetime) -> dict | None:
    """GPUs busy now, as DCGM measures it (the share of time the GPU's
    engines were active), over its readings of the last STALE_AFTER_HOURS.
    nvidia-smi's "utilization" -- any kernel at all running -- is not used:
    a GPU can show 100% by it while nearly idle. None when nothing measured."""
    cols = columns(conn, "gpu_stats")
    if not {"timestamp", "data_source", "real_util_pct", "node_name", "gpu_index"} <= cols:
        return None
    where, args = _where_site(cols, site)
    if site is not None and not where:
        return None
    outer, _ = _where_site(cols, site, "g")
    recent = iso(now - timedelta(hours=STALE_AFTER_HOURS))
    rows = fetch(conn,
                 "SELECT g.real_util_pct AS u FROM gpu_stats g JOIN ("
                 "  SELECT node_name, gpu_index, MAX(timestamp) AS t FROM gpu_stats "
                 f"  WHERE data_source = 'dcgm' AND timestamp >= ?{where} GROUP BY node_name, gpu_index"
                 ") l ON g.node_name = l.node_name AND g.gpu_index = l.gpu_index AND g.timestamp = l.t "
                 f"WHERE g.data_source = 'dcgm' AND g.real_util_pct IS NOT NULL{outer}",
                 [recent] + args + args)
    values = [v for v in (num(r["u"]) for r in rows) if v is not None]
    if not values:
        return None
    return {"busy_pct": round(sum(values) / len(values), 1),
            "measured": len(values), "source": "dcgm"}


def _slope_per_day(points):
    n = len(points)
    if n < 2:
        return None
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    return sum((x - mx) * (y - my) for x, y in points) / sxx if sxx else None


def storage(conn: sqlite3.Connection, site, now: datetime) -> list:
    """Each filesystem the site reads (seen in the last STORAGE_SEEN_DAYS):
    size, use, how fast it is filling over STORAGE_TREND_DAYS and when it
    would be full at that rate.

    The rate is the fall in free space (least squares over daily averages,
    so one cleanup or one burst doesn't set it), as nomad's disk forecast
    reads it: on a pool shared with other datasets, free space falls while
    this one's own use stays flat. Where free space isn't recorded, the
    rise in use."""
    cols = columns(conn, "filesystems")
    if not {"path", "total_bytes", "used_bytes", "timestamp"} <= cols:
        return []
    where, args = _where_site(cols, site)
    if site is not None and not where:
        return []
    seen = iso(now - timedelta(days=STORAGE_SEEN_DAYS))
    avail = "f.available_bytes" if "available_bytes" in cols else "NULL AS available_bytes"
    used_pct = "f.used_percent" if "used_percent" in cols else "NULL AS used_percent"
    outer, _ = _where_site(cols, site, "f")
    latest = fetch(conn,
                   f"SELECT f.path, f.timestamp, f.total_bytes, f.used_bytes, {avail}, {used_pct} "
                   "FROM filesystems f JOIN (SELECT path AS p, MAX(timestamp) AS m FROM filesystems "
                   f"WHERE timestamp >= ?{where} GROUP BY path) l ON f.path = l.p AND f.timestamp = l.m"
                   f"{outer}", [seen] + args + args)
    since = iso(now - timedelta(days=STORAGE_TREND_DAYS))
    series: dict = {}
    # Filling: the fall in free space where it is recorded, else the rise in use.
    measure = "-AVG(available_bytes)" if "available_bytes" in cols else "AVG(used_bytes)"
    for r in fetch(conn,
                   f"SELECT path, substr(timestamp, 1, 10) AS day, {measure} AS filled "
                   f"FROM filesystems WHERE timestamp >= ?{where} GROUP BY 1, 2", [since] + args):
        d, v = parse_time(r["day"]), num(r["filled"])
        if d is not None and v is not None:
            series.setdefault(r["path"], []).append(((d - now).days, v))
    out = []
    for r in latest:
        total, used = num(r["total_bytes"]) or 0, num(r["used_bytes"]) or 0
        avail = num(r["available_bytes"])
        free = avail if avail is not None else max(total - used, 0)
        share = num(r["used_percent"])
        if share is None and used + free:
            share = round(100 * used / (used + free), 1)
        pts = series.get(r["path"], [])
        span = (max(x for x, _ in pts) - min(x for x, _ in pts) + 1) if pts else 0
        slope = _slope_per_day(pts) if span >= STORAGE_MIN_TREND_DAYS else None
        full_in = round(free / slope) if slope and slope > 0 and free > 0 else None
        age = age_minutes(r["timestamp"], now)
        out.append({
            "path": r["path"], "total_bytes": int(total), "used_bytes": int(used),
            "free_bytes": int(free),
            "used_pct": share, "as_of": str(r["timestamp"]),
            "stale": age is None or age > STALE_AFTER_HOURS * 60,
            "trend_days": span,
            # None: too little history to say; 0 or less: not filling
            "growth_bytes_per_day": round(slope) if slope is not None else None,
            "days_until_full": full_in if full_in is not None and full_in <= STORAGE_HORIZON_DAYS
            else None,
        })
    return sorted(out, key=lambda s: (-(s["used_pct"] or 0), s["path"]))


def alerts(conn: sqlite3.Connection, site, now: datetime, gap_minutes: float) -> dict | None:
    """Conditions holding now (seen within the alert episode gap), by
    severity. Counts only: an alert's message can name things a viewer
    should not see."""
    cols = columns(conn, "alert_state")
    if not {"severity", "last_seen"} <= cols:
        return None
    where, args = _where_site(cols, site)
    if site is not None and not where:
        return None
    sev = "COALESCE(last_severity, severity)" if "last_severity" in cols else "severity"
    since = iso(now - timedelta(minutes=gap_minutes))
    counts = {"critical": 0, "warning": 0, "info": 0}
    for r in fetch(conn, f"SELECT {sev} AS s, COUNT(*) AS n FROM alert_state "
                         f"WHERE last_seen >= ?{where} GROUP BY 1", [since] + args):
        key = str(r["s"] or "info").lower()
        counts[key if key in counts else "info"] += int(r["n"])
    return counts


def workstations(conn: sqlite3.Connection, site, now: datetime) -> dict | None:
    """Workstations the site collects (any record in WORKSTATION_SEEN_DAYS):
    how many, and how many are online now."""
    cols = columns(conn, "workstation_state")
    if not {"hostname", "timestamp"} <= cols:
        return None
    where, args = _where_site(cols, site)
    if site is not None and not where:
        return None
    status = "status" if "status" in cols else "NULL AS status"
    rows = fetch(conn, f"SELECT hostname, MAX(timestamp) AS t, {status} FROM workstation_state "
                       f"WHERE timestamp >= ?{where} GROUP BY hostname",
                 [iso(now - timedelta(days=WORKSTATION_SEEN_DAYS))] + args)
    if not rows:
        return None
    online = 0
    for r in rows:
        age = age_minutes(r["t"], now)
        if age is not None and age <= MACHINE_STALE_MINUTES and \
                (r["status"] is None or r["status"] in ("online", "degraded")):
            online += 1
    return {"total": len(rows), "online": online,
            "as_of": max((str(r["t"]) for r in rows), default=None)}


def _sync(conn: sqlite3.Connection) -> dict:
    cols = columns(conn, "sync_sites")
    if "name" not in cols:
        return {}
    want = [c for c in ("synced_at", "last_success", "error") if c in cols]
    if not want:
        return {}
    return {r["name"]: r for r in fetch(conn, f"SELECT name, {', '.join(want)} FROM sync_sites")}


def sites(conn: sqlite3.Connection, days: int = 30, now: datetime | None = None,
          config: dict | None = None, reasons: bool = False, min_people: int = 0) -> dict:
    """Every site's state now, and its jobs over the last ``days``.

    ``reasons``: include Slurm's reason for each unavailable node (for
    staff). ``min_people``: leave out a site's job figures when fewer people
    than this ran the jobs they rest on (one person's are theirs alone).
    """
    from nomad.edu.progress import memory_ceilings
    now = now or datetime.now()
    since, until, win = window(days, now)
    alerts_cfg = (config or {}).get("alerts")
    gap = num((alerts_cfg if isinstance(alerts_cfg, dict) else {}).get("episode_gap_minutes")) or 60
    sync = _sync(conn)
    active = jobfacts.active(conn)
    ceilings = memory_ceilings(conn)
    out = []
    for name in site_names(conn):
        fresh = freshness(conn, name)
        age = age_minutes(fresh["newest"], now)
        stale = age is None or age > STALE_AFTER_HOURS * 60
        entry = {"name": name if name is not None else _local_name(config),
                 "status": "no_data" if fresh["newest"] is None else "stale" if stale else "ok",
                 "last_data": fresh["newest"], "age_minutes": age}
        s = sync.get(name)
        if s is not None:
            entry["sync"] = {"last_success": s.get("last_success"),
                             "last_attempt": s.get("synced_at"),
                             # the reason stays in the hub's sync log
                             "failing": bool(s.get("error"))}
        # Nodes, GPUs and the queue: as current as the node collector's own reading.
        node_age = age_minutes(fresh["by_table"].get("node_state"), now)
        node_stale = node_age is None or node_age > STALE_AFTER_HOURS * 60
        n = nodes(conn, name, now, node_stale, reasons=reasons)
        if n is not None:
            g = None if node_stale else gpu_use(conn, name, now)
            if n["gpus"]["count"]:
                n["gpus"].update(g or {"busy_pct": None, "measured": 0, "source": None})
            entry["nodes"] = {k: n[k] for k in ("as_of", "total", "up", "drained", "down",
                                                "not_reporting", "unavailable")}
            entry["cpu"], entry["memory"], entry["gpus"] = n["cpu"], n["memory"], n["gpus"]
            a = active.get(name, {"running": 0, "pending": 0})
            entry["queue"] = {**a, "as_of": fresh["by_table"].get("node_state"), "stale": node_stale}
            stats = jobfacts.summarize(jobfacts.finished_rows(conn, since, until, site=name), ceilings)
            # The same answer for every asker, who may be one of the people.
            if stats is not None and min_people and 0 < stats["people"] <= min_people:
                stats = {"finished": None, "suppressed": True,
                         "why": f"too few people ran jobs here in the window "
                                f"({min_people} or fewer)"}
            entry["jobs"] = stats
        w = workstations(conn, name, now)
        if w is not None:
            entry["workstations"] = w
        fs = storage(conn, name, now)
        if fs:
            entry["storage"] = fs
        al = alerts(conn, name, now, gap)
        if al is not None:
            entry["alerts"] = al
        out.append(entry)
    _same_filesystem(out)
    result = envelope("sites", now, win)
    result["sites"] = out
    result["notes"] = [
        "A stale site's figures are what it last reported, not the state now.",
        "Job failure shares leave node failures out: they are the machine's, not the job's.",
        "CPU and memory 'allocated' is what Slurm has handed out, not what jobs use.",
        "GPU 'busy' is DCGM's measure; GPUs without DCGM readings are not measured.",
    ]
    return result


def _local_name(config) -> str:
    general = (config or {}).get("general") or {}
    name = general.get("cluster_name") or general.get("site_name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    import socket
    return socket.gethostname().split(".")[0]


def _same_filesystem(entries: list) -> None:
    """Mark filesystems that look like one mounted at two sites or two paths
    (same size, use within 0.1%, read within the hour): "probably the same",
    never merged."""
    flat = [(e["name"], f) for e in entries for f in e.get("storage", ())]
    for site_a, a in flat:
        a["same_as"] = []
        for site_b, b in flat:
            if a is b or not a["total_bytes"] or a["total_bytes"] != b["total_bytes"]:
                continue
            ta, tb = parse_time(a["as_of"]), parse_time(b["as_of"])
            close = abs(a["used_bytes"] - b["used_bytes"]) <= 0.001 * a["total_bytes"]
            if close and ta and tb and abs((ta - tb).total_seconds()) <= 3600:
                a["same_as"].append({"site": site_b, "path": b["path"]})

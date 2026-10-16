# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Before and after NØMAÐ beyond the clusters: interactive servers, lab
workstations and shared login hosts (nomad 1.13.1). The periods, months and
events are nomad.impact's; each source has its own measures, from what its
collector already keeps:

* interactive servers (``interactive_daily``): people and hours open (the
  "(any)" rows: a person's time with at least one session open), and from
  the sessions' own rows, without the Jupyter server's polling (1.13.2), the
  share of their measured time spent computing and the measured
  session-hours that weren't. Days rebuilt from the raw snapshots (``nomad interactive
  backfill``) have hours but no CPU, so the computing share rests on the
  days after the collector kept CPU;
* lab workstations (``workstation_user_snapshot``: a person's session on a
  machine from sign-in to the last program ending): sessions, hours,
  core-hours used, long CPU-heavy runs (12 h or more keeping half the
  machine's cores busy: what the Console advises moving to a cluster) and
  sessions that held 80% or more of the machine's memory. A session counts
  in the month it ended;
* shared login hosts (``per_user_alert`` and ``per_user_daily``): flagged
  heavy use (episodes, as ``nomad per-user`` merges them; actionable ones),
  people flagged, and the core-hours everyone used there.

Counts and shares only: no names.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from nomad.impact import Impact, _cols, _months, month_phase, spans

# A long CPU-heavy workstation run, as the Console's advice to use a cluster
# counts one (nomad.edu.insights): this long, keeping this share of the
# machine's cores (and at least this many) busy on average.
LONG_RUN_HOURS = 12.0
LONG_RUN_HOST_SHARE = 0.5
LONG_RUN_MIN_CORES = 2.0
# A session that held this share of the machine's memory at its peak.
PRESSURED_SHARE = 0.8


def _site_col(cols: set) -> str:
    return "source_site" if "source_site" in cols else "NULL"


def _bucket(records: list, imp: Impact, today: date, agg) -> dict:
    """records: (YYYY-MM-DD, record) pairs. Months from baseline_from, and the
    periods (nomad.impact.spans), each agg(records in it)."""
    records = sorted(records, key=lambda r: r[0])
    data_from = records[0][0] if records else None

    def within(a: date, b: date):
        a, b = a.isoformat(), b.isoformat()
        return [r for d, r in records if a <= d < b]
    months = [{"month": m0.strftime("%Y-%m"), "phase": month_phase(m0, m1, imp.launch),
               "events": [e.what for e in imp.events if m0 <= e.when < m1], **agg(within(m0, m1))}
              for m0, m1 in _months(imp.baseline_from, today)]
    periods = {key: {"from": a.isoformat(), "to": (b - timedelta(days=1)).isoformat(),
                     **agg(within(a, b))} for key, a, b in spans(imp, today, data_from)}
    return {"data_from": data_from, "months": months, "periods": periods}


def _pct(part, whole):
    return round(100 * part / whole, 1) if whole else None


# -- interactive servers ---------------------------------------------------------------

def _interactive_agg(rows: list) -> dict:
    people = {u for u, open_s, *_ in rows if open_s}
    open_s = sum(r[1] for r in rows)
    busy = sum(r[2] for r in rows)
    measured = sum(r[3] for r in rows)
    return {"people": len(people), "open_hours": round(open_s / 3600, 1),
            "computing_pct": _pct(busy, measured),
            "idle_hours": round((measured - busy) / 3600, 1) if measured else None,
            "hours_per_person": round(open_s / 3600 / len(people), 1) if people else None}


def interactive(conn, imp: Impact, site=None, today: date | None = None) -> list[dict]:
    today = today or date.today()
    cols = _cols(conn, "interactive_daily")
    if not {"day", "user", "session_type", "session_seconds", "busy_seconds",
            "cpu_measured_seconds"} <= cols:
        return []
    sc = _site_col(cols)
    where, args = "", [imp.baseline_from.isoformat()]
    if site is not None and sc != "NULL":
        where, args = f" AND {sc} = ?", args + [site]
    from nomad.collectors.interactive_daily import ANY, NOT_COMPUTING
    # Hours open: the "(any)" rows (a person's time with a session open, once).
    # Computing: the other rows' busy time over their measured time, without
    # the Jupyter server's own polling (NOT_COMPUTING), which made anyone with
    # JupyterLab open look busy all the time; so for the history too.
    days: dict = {}
    for s, day, user, kind, open_s, busy, measured in conn.execute(
            f"SELECT {sc}, day, user, session_type, session_seconds, busy_seconds, "
            f"cpu_measured_seconds FROM interactive_daily WHERE day >= ?{where}", args):
        if user in imp.exclude_users or kind in NOT_COMPUTING:
            continue
        d = days.setdefault((s, str(day)[:10], user), [0.0, 0.0, 0.0])
        if kind == ANY:
            d[0] += open_s or 0
        else:
            d[1] += busy or 0
            d[2] += measured or 0
    by_site = defaultdict(list)
    for (s, day, user), (open_s, busy, measured) in days.items():
        by_site[s].append((day, (user, open_s, busy, measured)))
    out = []
    for s in sorted(by_site, key=str):
        rec = by_site[s]
        computing_from = min((d for d, r in rec if r[3]), default=None)
        out.append({"site": s, "computing_from": computing_from,
                    **_bucket(rec, imp, today, _interactive_agg)})
    return out


# -- lab workstations -------------------------------------------------------------------

def workstation_sessions(conn, since: date, site=None, exclude=()) -> list[dict]:
    """Each person's sessions on each machine (uid 1000 and up; not the
    accounts in ``exclude``) seen on or after ``since``: when, how long, CPU
    and memory, the machine's size, and whether it was a long CPU-heavy run
    or memory-pressured. A session is the person's slice on the machine
    while its counters keep growing (nomad.workstation_sessions, 1.13.2).
    Pressured needs the programs' own memory (memory_anon_bytes, 1.13.2):
    memory.peak counts the file cache; without it, ``pressured`` is None."""
    from nomad.workstation_sessions import busy_runs, split
    cols = _cols(conn, "workstation_user_snapshot")
    if not {"timestamp", "hostname", "username", "uid", "session_epoch",
            "cpu_usage_usec", "memory_peak_bytes"} <= cols:
        return []
    sc = _site_col(cols)
    anon = "memory_anon_bytes" if "memory_anon_bytes" in cols else "NULL"
    where, args = "", [since.isoformat()]
    if site is not None and sc != "NULL":
        where, args = f" AND {sc} = ?", args + [site]
    hosts: dict = {}
    hcols = _cols(conn, "workstation_state")
    if {"hostname", "timestamp", "memory_total_mb", "cpu_count"} <= hcols:
        hsc = _site_col(hcols)
        for s, h, mem, cores, _ts in conn.execute(
                f"SELECT {hsc}, hostname, memory_total_mb, cpu_count, MAX(timestamp) "
                f"FROM workstation_state GROUP BY 1, 2"):
            hosts[(s, h)] = (mem or 0, cores or 0)
    skip = set(exclude or ())
    snaps = conn.execute(
        f"SELECT COALESCE({sc}, '') || char(0) || COALESCE(hostname, '') || char(0) || uid, "
        f"timestamp, session_epoch, "
        f"cpu_usage_usec, memory_peak_bytes, {sc}, hostname, username, {anon} "
        f"FROM workstation_user_snapshot WHERE timestamp >= ? AND uid >= 1000{where} "
        f"ORDER BY {sc}, hostname, uid, timestamp", args)
    out = []
    for rows in split(r for r in snaps if r[7] not in skip):
        first, last = rows[0], rows[-1]
        s, h, user = first[5], first[6], first[7]
        try:
            t0 = datetime.fromisoformat(str(first[1])[:19])
            t1 = datetime.fromisoformat(str(last[1])[:19])
        except ValueError:
            continue
        span = (t1 - t0).total_seconds() / 3600
        cpus = [r[3] for r in rows if r[3] is not None]
        core_h = (max(cpus) - min(cpus)) / 3.6e9 if cpus else None
        anons = [r[8] for r in rows if r[8] is not None]
        mem_mb, cores = hosts.get((s, h), (0, 0))
        # Long runs inside the session: it can be weeks long, and its own
        # average says little about a two-day job in it (1.13.3).
        runs = busy_runs(((r[1], r[3]) for r in rows),
                         max(LONG_RUN_MIN_CORES, LONG_RUN_HOST_SHARE * cores),
                         LONG_RUN_HOURS) if cores else []
        out.append({
            "site": s, "host": h, "user": user, "end": str(last[1])[:10], "hours": span,
            "core_hours": core_h,
            "long_run": bool(runs), "runs": runs,
            "pressured": (bool(mem_mb and max(anons) >= PRESSURED_SHARE * mem_mb * 2 ** 20)
                          if anons else None),
        })
    return out


def _ws_agg(records: list) -> dict:
    """records: sessions (dicts, by the day they ended) and long runs
    ((hours, cores, ended, user), by the day they ended: a session can run
    for weeks, and a run in it belongs to its own month; its person counts
    there too)."""
    sessions = [x for x in records if isinstance(x, dict)]
    long_runs = [x for x in records if not isinstance(x, dict)]     # (hours, cores, ended, user)
    measured = [x for x in sessions if x["core_hours"] is not None]
    people = {x["user"] for x in sessions} | {run[3] for run in long_runs}
    return {"people": len(people), "sessions": len(sessions),
            "hours": round(sum(x["hours"] for x in sessions), 1),
            "core_hours": round(sum(x["core_hours"] for x in measured), 1) if measured else None,
            "long_runs": len(long_runs),
            "long_run_hours": round(sum(run[0] for run in long_runs), 1),
            # Of the sessions with the programs' own memory read (1.13.2).
            "pressured": (sum(1 for x in sessions if x["pressured"])
                          if any(x["pressured"] is not None for x in sessions) else None),
            "memory_read": sum(1 for x in sessions if x["pressured"] is not None)}


def workstations(conn, imp: Impact, site=None, today: date | None = None,
                 sessions: list | None = None) -> list[dict]:
    today = today or date.today()
    if sessions is None:
        sessions = workstation_sessions(conn, imp.baseline_from, site, imp.exclude_users)
    by_site = defaultdict(list)
    for x in sessions:
        by_site[x["site"]].append((x["end"], x))
        for run in x.get("runs", ()):
            by_site[x["site"]].append((run[2][:10], (*run, x["user"])))
    return [{"site": s, "machines": len({r["host"] for _d, r in by_site[s]
                                         if isinstance(r, dict)}),
             **_bucket(by_site[s], imp, today, _ws_agg)}
            for s in sorted(by_site, key=str)]


# -- shared login hosts -----------------------------------------------------------------

def _local_day(utc_text) -> str | None:
    try:
        t = datetime.strptime(str(utc_text)[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return t.replace(tzinfo=timezone.utc).astimezone().date().isoformat()


def _login_agg(rows: list) -> dict:
    eps = [r for r in rows if r[0] == "episode"]
    days = [r for r in rows if r[0] == "day"]
    return {"episodes": len(eps), "actionable": sum(1 for r in eps if r[2]),
            "people_flagged": len({r[1] for r in eps}),
            "people": len({r[1] for r in days}),
            "core_hours": round(sum(r[2] for r in days) / 3600, 1) if days else None}


def login_hosts(conn, imp: Impact, site=None, today: date | None = None) -> list[dict]:
    from nomad.collectors.per_user.report import _episodes
    today = today or date.today()
    acols, dcols = _cols(conn, "per_user_alert"), _cols(conn, "per_user_daily")
    by_site = defaultdict(list)
    since_utc = (datetime.combine(imp.baseline_from, datetime.min.time())
                 - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    if {"process_session_id", "username", "last_seen", "severity"} <= acols:
        sc = _site_col(acols)
        where, args = "", [since_utc]
        if site is not None and sc != "NULL":
            where, args = f" AND {sc} = ?", args + [site]
        alerts = defaultdict(list)
        for row in conn.execute(
                f"SELECT {sc}, process_session_id, username, command, rule_type, "
                f"threshold_value, severity, last_seen, sustained_for_seconds, "
                f"peak_cpu_percent, peak_memory_bytes FROM per_user_alert "
                f"WHERE last_seen >= ?{where}", args):
            alerts[row[0]].append(row[1:])
        for s, rows in alerts.items():
            for e in _episodes(rows):
                day = _local_day(e["last"])
                if day and e["user"] not in imp.exclude_users:
                    by_site[s].append((day, ("episode", e["user"], e["actionable"])))
    if {"day", "username", "cpu_seconds"} <= dcols:
        sc = _site_col(dcols)
        where, args = "", [imp.baseline_from.isoformat()]
        if site is not None and sc != "NULL":
            where, args = f" AND {sc} = ?", args + [site]
        for s, day, user, cpu in conn.execute(
                f"SELECT {sc}, day, username, cpu_seconds FROM per_user_daily "
                f"WHERE day >= ?{where}", args):
            if user not in imp.exclude_users:
                by_site[s].append((str(day)[:10], ("day", user, cpu or 0)))
    return [{"site": s, **_bucket(by_site[s], imp, today, _login_agg)}
            for s in sorted(by_site, key=str)]


def data_from(conn) -> dict:
    """For nomad impact status: per source and site, the first day held."""
    out: dict = {}

    def first(table, day_sql, extra="", what=None):
        cols = _cols(conn, table)
        if not cols:
            return
        sc = _site_col(cols)
        try:
            for s, d in conn.execute(f"SELECT {sc}, MIN({day_sql}) FROM {table} "
                                     f"WHERE 1=1{extra} GROUP BY 1"):
                if d:
                    out.setdefault(what or table, {})[s] = str(d)[:10]
        except sqlite3.Error:
            return
    first("interactive_daily", "day", " AND session_type = '(any)'", "interactive")
    first("workstation_user_snapshot", "timestamp", " AND uid >= 1000", "workstations")
    first("per_user_daily", "day", "", "login_hosts")
    return out


# -- text -------------------------------------------------------------------------------

def _f(v, kind: str) -> str:
    if v is None:
        return "—"
    if kind == "pct":
        return f"{v:.0f}%"
    if kind == "h":
        return f"{v:,.0f} h" if v >= 10 else f"{v:.1f} h"
    return f"{v:,}" if isinstance(v, int) else f"{v:,.1f}"


# (title, what each column is, [(key, header, kind)])
SECTIONS = {
    "interactive": (
        "Interactive servers",
        "hours: with at least one session open; computing: share of the measured time "
        "notebooks, kernels and RStudio sessions were busy (not the Jupyter server's own "
        "polling); idle: their measured session-hours that weren't",
        [("people", "people", "n"), ("open_hours", "hours", "h"),
         ("hours_per_person", "per person", "h"), ("computing_pct", "computing", "pct"),
         ("idle_hours", "idle", "h")]),
    "workstations": (
        "Lab workstations",
        "a session runs from sign-in to the last program ending, counted in the month it "
        "ended; long runs: stretches of 12 h or more inside a session keeping half the "
        "machine's cores busy, and their hours; pressured: "
        "its programs held 80% or more of the machine's memory (read from nomad 1.13.2 on: "
        "the readings before include the file cache)",
        [("people", "people", "n"), ("sessions", "sessions", "n"), ("hours", "hours", "h"),
         ("core_hours", "core-h", "h"), ("long_runs", "long runs", "n"),
         ("long_run_hours", "their h", "h"), ("pressured", "pressured", "n")]),
    "login_hosts": (
        "Shared login hosts",
        "flagged: heavy-use episodes (actionable ones); people: everyone who used the "
        "host; core-h: CPU they used there",
        [("episodes", "flagged", "n"), ("actionable", "actionable", "n"),
         ("people_flagged", "people flagged", "n"), ("people", "people", "n"),
         ("core_hours", "core-h", "h")]),
}
LABELS = {"before": "Before launch", "after": "After launch",
          "year_earlier": "Same days a year earlier"}


def render(kind: str, reps: list[dict]) -> list[str]:
    title, note, columns = SECTIONS[kind]
    out = [title]
    if not reps:
        return out + ["  Nothing collected here."]
    for rep in reps:
        name = rep["site"] or "this site"
        extra = ""
        if kind == "interactive" and rep.get("computing_from"):
            extra = f"; computing measured from {rep['computing_from']}"
        if kind == "workstations":
            extra = f"; {rep['machines']} machine{'' if rep['machines'] == 1 else 's'}"
        out += ["", f"  {name}: data from {rep['data_from'] or '—'}{extra}"]
        widths = [max(len(h), 8) for _k, h, _t in columns]
        out.append(f"    {'month':<8} {'':<6} " + " ".join(
            f"{h:>{w}}" for (_k, h, _t), w in zip(columns, widths)))
        for m in rep["months"]:
            if rep["data_from"] and m["month"] < rep["data_from"][:7]:
                continue                    # before the data begins
            out.append(f"    {m['month']:<8} {m['phase']:<6} " + " ".join(
                f"{_f(m.get(k), t):>{w}}" for (k, _h, t), w in zip(columns, widths)))
            for what in m["events"]:
                out.append(f"    {'':<8} ↳ {what}")
        for key, p in rep["periods"].items():
            out.append(f"    {LABELS[key]:<26} {p['from']} to {p['to']}: " + ", ".join(
                f"{h} {_f(p.get(k), t)}" for k, h, t in columns))
    out += [f"  ({note}.)"]
    return out

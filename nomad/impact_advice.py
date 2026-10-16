# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Advice shown, and whether it was taken (nomad 1.13.1).

The Console notes, once a day, which advice it showed someone about their
own work (its own database, ``advice_shown`` and ``pages_viewed``; the
Console's app/models/advice.py). Set against the jobs and sessions that
followed, that is the closest NØMAÐ comes to saying its advice made a
difference:

* advice on a cluster partition (memory, time, cores, GPUs): the person's
  typical request there in the WINDOW_DAYS before they first saw it, and in
  the WINDOW_DAYS after. "Took it": the typical request moved at least half
  the way to the suggestion; "partly": some of the way; "no change": not at
  all, or away. Too few jobs after (MIN_AFTER_JOBS) is "too early" while the
  window is still open, "no later jobs" after;
* advice to move long workstation runs to a cluster: the person's long
  CPU-heavy workstation runs (nomad.impact_sources) and their jobs at that
  cluster, before and after. "Moved": half the long-run hours or less after,
  and more jobs there than before; "fewer long runs": the first without the
  second.

Without people who had the same advice and never saw it, this can't rule
out that requests drift toward what jobs use anyway; the report says so.
Counts only: no names.
"""
from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from nomad.impact import Impact, _cols, _site_col

WINDOW_DAYS = 60
MIN_AFTER_JOBS = 3
TOOK = 0.5                  # the share of the way to the suggestion that counts as taking it
REQUEST_COLUMN = {"mem": "req_mem_mb", "time": "req_time_seconds", "cores": "req_cpus",
                  "cpus-per-task": "req_cpus", "ntasks": "req_cpus", "gres": "req_gpus"}
OUTCOMES = ("took", "partly", "no_change", "too_early", "no_later_jobs")
MOVE_OUTCOMES = ("moved", "fewer_long_runs", "no_change", "too_early")


def advice_db_path(imp: Impact) -> Path | None:
    """[impact] advice_db, else the Console's own database when this account
    has one (the Console and nomad run as the same account on a hub)."""
    if imp.advice_db:
        return Path(imp.advice_db).expanduser()
    p = Path.home() / ".nomad-console" / "activity.db"
    return p if p.exists() else None


def _shown(adv, site=None) -> list[dict]:
    """Each piece of advice once: who, where, what, and when it was first shown,
    with the values shown then."""
    rows = adv.execute(
        "SELECT username, site, partition, dimension, directive, current_value, "
        "suggested_value, first_at FROM advice_shown ORDER BY first_at").fetchall()
    first: dict = {}
    for user, s, part, dim, directive, cur, sug, at in rows:
        if site is not None and s != site:
            continue
        key = (user, s, part, dim, directive)
        if key not in first:
            first[key] = {"user": user, "site": s, "partition": part, "dimension": dim,
                          "directive": directive, "current": cur, "suggested": sug,
                          "at": str(at)[:19]}
    return list(first.values())


def _requests(conn, user, site, partition, column, a: datetime, b: datetime) -> list[float]:
    jcols = _cols(conn, "jobs")
    if column not in jcols or "end_time" not in jcols:
        return []
    sc = _site_col(jcols)
    where, args = "", [user, a.isoformat(timespec="seconds"), b.isoformat(timespec="seconds")]
    if sc and site:
        where += f" AND {sc} = ?"
        args.append(site)
    if partition:
        where += " AND partition = ?"
        args.append(partition)
    return [float(v) for (v,) in conn.execute(
        f"SELECT {column} FROM jobs WHERE user_name = ? AND end_time >= ? AND end_time < ?"
        f" AND {column} > 0{where}", args)]


def _jobs_count(conn, user, site, a: datetime, b: datetime) -> int:
    jcols = _cols(conn, "jobs")
    if "end_time" not in jcols:
        return 0
    sc = _site_col(jcols)
    where, args = "", [user, a.isoformat(timespec="seconds"), b.isoformat(timespec="seconds")]
    if sc and site:
        where, args = f" AND {sc} = ?", args + [site]
    return conn.execute(f"SELECT COUNT(*) FROM jobs WHERE user_name = ? AND end_time >= ? "
                        f"AND end_time < ?{where}", args).fetchone()[0]


def took(conn, a: dict, now: datetime) -> tuple[str, float | None]:
    """What became of one piece of cluster advice: an outcome (OUTCOMES) and
    the share of the way to the suggestion the typical request moved."""
    column = REQUEST_COLUMN.get(a["directive"])
    if column is None or a["suggested"] is None:
        return "", None
    at = datetime.fromisoformat(a["at"])
    window = timedelta(days=WINDOW_DAYS)
    before = _requests(conn, a["user"], a["site"], a["partition"], column, at - window, at)
    after = _requests(conn, a["user"], a["site"], a["partition"], column, at, min(at + window, now))
    if len(after) < MIN_AFTER_JOBS:
        return ("too_early" if at + window > now else "no_later_jobs"), None
    b = statistics.median(before) if before else a["current"]
    if b is None:
        return "", None
    gap = b - a["suggested"]
    if gap == 0:
        return "", None                     # nothing to change
    moved = (b - statistics.median(after)) / gap
    return ("took" if moved >= TOOK else "partly" if moved > 0 else "no_change"), moved


def moved_to_cluster(conn, a: dict, sessions_by_user: dict, now: datetime) -> str:
    at = datetime.fromisoformat(a["at"])
    window = timedelta(days=WINDOW_DAYS)
    if at + timedelta(days=WINDOW_DAYS // 2) > now:
        return "too_early"
    d0, d1, d2 = ((at - window).date().isoformat(), at.date().isoformat(),
                  min(at + window, now).date().isoformat())
    runs = [x for x in sessions_by_user.get(a["user"], []) if x["long_run"]]
    before = sum(x["hours"] for x in runs if d0 <= x["end"] < d1)
    after = sum(x["hours"] for x in runs if d1 <= x["end"] <= d2)
    jobs_before = _jobs_count(conn, a["user"], a["site"], at - window, at)
    jobs_after = _jobs_count(conn, a["user"], a["site"], at, min(at + window, now))
    fewer = after <= 0.5 * before if before else after == 0
    if fewer and jobs_after > jobs_before:
        return "moved"
    return "fewer_long_runs" if fewer and before else "no_change"


def report(conn, imp: Impact, site=None, today: date | None = None,
           path: Path | None = None) -> dict:
    """Advice shown and taken, by dimension; pages opened, by month."""
    now = datetime.combine((today or date.today()) + timedelta(days=1), datetime.min.time())
    p = path or advice_db_path(imp)
    if p is None or not Path(p).exists():
        return {"available": False,
                "reason": "No record of advice shown here: it is the Console's (Console fix "
                          "33 or later), in the Console account's ~/.nomad-console/activity.db; "
                          "give it with [impact] advice_db or --advice-db."}
    try:
        adv = sqlite3.connect(f"file:{Path(p).expanduser()}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return {"available": False, "reason": f"Can't read {p}: {e}"}
    try:
        if not {"advice_shown", "pages_viewed"} <= {
                r[0] for r in adv.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
            return {"available": False, "reason": f"{p} has no record of advice shown yet "
                                                  "(Console fix 33 or later writes it)."}
        since = adv.execute("SELECT MIN(day) FROM pages_viewed").fetchone()[0]
        views: dict = defaultdict(dict)
        for month, page, n in adv.execute(
                "SELECT substr(day, 1, 7), page, COUNT(DISTINCT username) FROM pages_viewed "
                "GROUP BY 1, 2 ORDER BY 1"):
            views[month][page] = n
        shown = [a for a in _shown(adv, site) if a["user"] not in imp.exclude_users]
    finally:
        adv.close()

    cluster: dict = defaultdict(lambda: {"shown": 0, "people": set(), **dict.fromkeys(OUTCOMES, 0),
                                         "moved": []})
    where_to_run = {"shown": 0, "people": set(), **dict.fromkeys(MOVE_OUTCOMES, 0)}
    sessions_by_user: dict | None = None
    for a in shown:
        if a["dimension"].startswith("verdict:"):
            if a["dimension"] != "verdict:promote":
                continue
            if sessions_by_user is None:
                from nomad.impact_sources import workstation_sessions
                sessions_by_user = defaultdict(list)
                start = (datetime.fromisoformat(shown[0]["at"]) - timedelta(days=WINDOW_DAYS)).date()
                for x in workstation_sessions(conn, start):
                    sessions_by_user[x["user"]].append(x)
            where_to_run["shown"] += 1
            where_to_run["people"].add(a["user"])
            where_to_run[moved_to_cluster(conn, a, sessions_by_user, now)] += 1
            continue
        outcome, moved = took(conn, a, now)
        if not outcome:
            continue
        d = cluster[a["dimension"]]
        d["shown"] += 1
        d["people"].add(a["user"])
        d[outcome] += 1
        if moved is not None:
            d["moved"].append(moved)
    by_dim = {}
    for dim, d in sorted(cluster.items()):
        moves = d.pop("moved")
        by_dim[dim] = {**{k: v for k, v in d.items() if k != "people"}, "people": len(d["people"]),
                       "median_moved": round(statistics.median(moves), 2) if moves else None}
    where_to_run["people"] = len(where_to_run["people"])
    return {"available": True, "recorded_since": since, "window_days": WINDOW_DAYS,
            "views": dict(views), "cluster": by_dim, "where_to_run": where_to_run,
            "people_shown": len({a["user"] for a in shown})}


def render(rep: dict) -> list[str]:
    out = ["Advice shown and taken"]
    if not rep.get("available"):
        return out + [f"  {rep.get('reason')}"]
    n = rep["people_shown"]
    out.append(f"  Recorded since {rep['recorded_since'] or '—'}; "
               f"{n:,} {'person' if n == 1 else 'people'} shown advice.")
    if rep["views"]:
        out.append("  People who opened their own pages, by month:")
        for month, pages in rep["views"].items():
            out.append(f"    {month}  " + ", ".join(
                f"{p.replace('_', ' ')} {n}" for p, n in sorted(pages.items())))
    if rep["cluster"]:
        out.append(f"  Cluster advice, {rep['window_days']} days before and after it was first "
                   "seen (typical request against the suggestion):")
        out.append(f"    {'':<10} {'shown':>6} {'people':>6} {'took':>5} {'partly':>6} "
                   f"{'no change':>9} {'too early':>9} {'no jobs':>7}")
        for dim, d in rep["cluster"].items():
            out.append(f"    {dim:<10} {d['shown']:>6} {d['people']:>6} {d['took']:>5} "
                       f"{d['partly']:>6} {d['no_change']:>9} {d['too_early']:>9} "
                       f"{d['no_later_jobs']:>7}")
    w = rep["where_to_run"]
    if w["shown"]:
        out.append(f"  Advice to move long workstation runs to a cluster: {w['shown']} shown "
                   f"({w['people']} {'person' if w['people'] == 1 else 'people'}); "
                   f"moved {w['moved']}, fewer long runs "
                   f"{w['fewer_long_runs']}, no change {w['no_change']}, too early "
                   f"{w['too_early']}.")
    out.append("  Took it: the typical request moved at least half the way to the suggestion. "
               "Nobody here is a comparison")
    out.append("  group (the same advice, never seen), so some of this may be requests "
               "drifting toward use anyway.")
    return out

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The interactive totals for the weeks before nomad kept them, rebuilt
roughly from the raw snapshots.

nomad 1.11 started keeping each person's day on an interactive server
(``interactive_daily``), 1.11.1 each hour (``interactive_hourly``,
``interactive_person_hourly``). The raw snapshots (``interactive_sessions``,
every five minutes, kept unless pruned) go back further. From them this
rebuilds, for the days and hours before the totals start:

* who was on, at each reading, and so each person's time with a session open
  (the ``(any)`` row of each day, and the person's hours);
* the memory they held, and the most everyone held at once;
* readings and people on per hour.

Not what was computing: the old snapshots kept ``ps``'s lifetime average, not
the CPU used between readings. Rebuilt rows say so: ``rebuilt = 1``, nothing
measured (``cpu_measured_seconds`` and ``measured_runs`` 0), and no session
count (the old snapshots counted processes, not sessions: ``sessions`` 0).

Readings come from the collector's own log (``collection_log``): a run with
nobody on wrote no snapshot, but was a reading all the same (zero people).
Without the log, the snapshots' own times, each crediting at most the usual
interval. Names longer than 8 characters were cut by ``ps aux``
("abcdefg+"): matched to the one known name they can be (groups, later
totals), else kept as they are.
"""
from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from nomad.collectors import interactive_daily as D
from nomad.facts.courses import resolve_name

ANY = D.ANY
TABLES = ("interactive_daily", "interactive_hourly", "interactive_person_hourly")
RUN_MATCH_SECONDS = 120           # a snapshot belongs to the run that started this soon before it
SAME_READING_SECONDS = 60         # snapshot rows this close together are one reading (before
                                  # 1.11 each session was stamped as it was read)


def _parse(ts) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts)[:19])
    except (TypeError, ValueError):
        return None


def _known_names(conn) -> set:
    names: set = set()
    for sql in ("SELECT DISTINCT username FROM group_membership",
                "SELECT DISTINCT user FROM interactive_daily",
                "SELECT DISTINCT username FROM oom_kills"):
        try:
            names |= {r[0] for r in conn.execute(sql) if r[0]}
        except sqlite3.Error:
            pass
    return names


def _runs(conn, until: str) -> list[datetime]:
    try:
        rows = conn.execute("SELECT started_at FROM collection_log WHERE collector = 'interactive' "
                            "AND success = 1 AND started_at < ? ORDER BY started_at",
                            (until,)).fetchall()
    except sqlite3.Error:
        return []
    return [t for t in (_parse(r[0]) for r in rows) if t is not None]


def _all_limits(conn) -> dict:
    """server -> (first day, first hour) the totals were kept for."""
    out: dict = {}
    for server, day in conn.execute("SELECT server_id, MIN(day) FROM interactive_daily "
                                    "WHERE rebuilt = 0 GROUP BY server_id"):
        out[server] = [day, None]
    for server, hour in conn.execute("SELECT server_id, MIN(hour) FROM interactive_hourly "
                                     "WHERE rebuilt = 0 GROUP BY server_id"):
        out.setdefault(server, [None, None])[1] = hour
    return out


PROGRESS_ROWS = 500_000


def rebuild(conn: sqlite3.Connection, apply: bool = False, redo: bool = False,
            progress=None) -> dict:
    """What would be (``apply``: is) rebuilt, per server: days, hours, people,
    readings and names that could not be matched. ``redo``: replace earlier
    rebuilt rows. One pass over the snapshots, in the order they are stored
    (``progress(rows, timestamp)`` every PROGRESS_ROWS); commits a day at a
    time, so the collector is never held up for long."""
    D.ensure(conn)
    conn.commit()
    have = {r[1] for r in conn.execute("PRAGMA table_info(interactive_sessions)")}
    if not {"timestamp", "user", "server_id"} <= have:
        return {"servers": [], "note": "no raw snapshots (interactive_sessions) here"}
    if "source_site" in have:
        return {"servers": [], "note": "this is a hub's combined database: run the backfill on "
                                       "the interactive server itself (the hub copies it at its "
                                       "next sync)"}
    already = sum(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE rebuilt = 1").fetchone()[0]
                  for t in TABLES)
    if already and not redo:
        return {"servers": [], "already": already,
                "note": "rows were rebuilt before; --redo replaces them"}
    if apply and already:
        for t in TABLES:
            conn.execute(f"DELETE FROM {t} WHERE rebuilt = 1")
        conn.commit()
    known = _known_names(conn)
    mem_col = "mem_mb" if "mem_mb" in have else "NULL"
    limits = _all_limits(conn)
    # One pass, in storage order (no index lookups); each name matched once.
    names: dict = {}
    times: dict = {}
    bounds: dict = {}
    by_server: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    rows = 0
    for server, ts, user, mem in conn.execute(
            f"SELECT server_id, timestamp, user, {mem_col} FROM interactive_sessions"):
        rows += 1
        if progress is not None and rows % PROGRESS_ROWS == 0:
            progress(rows, ts)
        if not user or not ts:
            continue
        bound = bounds.get(server)
        if bound is None:
            day_limit, hour_limit = limits.get(server, [None, None])
            day_limit = day_limit or "9999-12-31"
            hour_limit = hour_limit or "9999-12-31T23"
            bound = bounds[server] = (max(day_limit + "T00:00:00", hour_limit + ":00:00"),
                                      day_limit, hour_limit)
        key = str(ts)[:19]
        if key >= bound[0]:
            continue
        t = times.get(key)
        if t is None:
            t = times[key] = _parse(key)
        if t is None:
            continue
        name = names.get(user)
        if name is None:
            name = names[user] = resolve_name(str(user), known) or str(user)
        by_server[server][t][name] += float(mem or 0.0)
    times.clear()
    out = []
    for server, snaps in by_server.items():
        bound, day_limit, hour_limit = bounds[server]
        unmatched = {n for n in {n for people in snaps.values() for n in people} if n.endswith("+")}
        # One reading's rows, stamped seconds apart, are one reading.
        merged: dict = {}
        start = None
        for t in sorted(snaps):
            if start is None or (t - start).total_seconds() > SAME_READING_SECONDS:
                start = t
                merged[start] = defaultdict(float)
            for user, mb in snaps[t].items():
                merged[start][user] += mb
        snaps = merged
        times = sorted(snaps)
        # The run log has no server: this server's runs are those from its
        # first snapshot to its last, or, when it went on to keep its own
        # totals, to where they begin (readings with nobody on in between are
        # still readings). A server renamed in nomad.toml is two.
        lo = times[0] - timedelta(seconds=RUN_MATCH_SECONDS)
        live = not (day_limit.startswith("9999") and hour_limit.startswith("9999"))
        hi = _parse(bound) if live else times[-1]
        runs = [r for r in _runs(conn, bound) if lo <= r <= hi]
        if runs:
            # Each snapshot to the run that took it; runs with no snapshot had nobody on.
            readings: dict = {r: {} for r in runs}
            j = 0
            for t in times:
                while j + 1 < len(runs) and runs[j + 1] <= t:
                    j += 1
                r = runs[j]
                key = r if r <= t and (t - r).total_seconds() <= RUN_MATCH_SECONDS else t
                into = readings.setdefault(key, {})
                for user, mb in snaps[t].items():
                    into[user] = into.get(user, 0.0) + mb
            source = "the collector's run log"
            step = None
        else:
            readings = {t: snaps[t] for t in times}
            gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])
                    if 0 < (b - a).total_seconds() <= D.MAX_GAP_SECONDS]
            step = min(900.0, statistics.median(gaps)) if gaps else 300.0
            source = "the snapshots' own times (no run log)"
        daily: dict = defaultdict(lambda: [0.0, 0.0, 0.0])          # (day, user) -> s, MB*s, peak
        hourly: dict = defaultdict(lambda: [0, 0, 0, 0.0, 0.0])     # hour -> runs, ppl sum, max, open s, MB max
        person: dict = defaultdict(lambda: [0, 0.0, 0.0])           # (hour, user) -> runs, s, MB max
        prev = None
        for t in sorted(readings):
            people = readings[t]
            gap = (t - prev).total_seconds() if prev is not None else None
            prev = t
            dt = gap if gap is not None and 0 < gap <= D.MAX_GAP_SECONDS else None
            if dt is not None and step is not None:
                dt = min(dt, step)
            day, hour = t.date().isoformat(), t.strftime("%Y-%m-%dT%H")
            if hour < hour_limit:
                h = hourly[hour]
                h[0] += 1
                h[1] += len(people)
                h[2] = max(h[2], len(people))
                h[3] += len(people) * (dt or 0.0)
                h[4] = max(h[4], sum(people.values()))
                for user, mb in people.items():
                    p = person[(hour, user)]
                    p[0] += 1
                    p[1] += dt or 0.0
                    p[2] = max(p[2], mb)
            if day < day_limit:
                for user, mb in people.items():
                    d = daily[(day, user)]
                    d[0] += dt or 0.0
                    d[1] += mb * (dt or 0.0)
                    d[2] = max(d[2], mb)
        days = sorted({k[0] for k in daily})
        summary = {"server": server, "readings_from": source,
                   "days": len(days), "first_day": days[0] if days else None,
                   "last_day": days[-1] if days else None,
                   "hours": len(hourly), "people": len({k[1] for k in daily} | {k[1] for k in person}),
                   "names_not_matched": len(unmatched),
                   "before_day": None if day_limit.startswith("9999") else day_limit,
                   "before_hour": None if hour_limit.startswith("9999") else hour_limit}
        out.append(summary)
        if not apply:
            continue
        stamp = datetime.now().isoformat(timespec="seconds")
        by_day: dict = defaultdict(list)
        for (day, user), (s, mbs, peak) in daily.items():
            by_day[day].append((day, server, user, ANY, 0, s, 0.0, 0.0, 0.0, mbs, peak, stamp, 1))
        for day in sorted(by_day):
            conn.executemany(
                "INSERT OR IGNORE INTO interactive_daily (day, server_id, user, session_type, "
                "sessions, session_seconds, busy_seconds, cpu_measured_seconds, cpu_seconds, "
                "mem_mb_seconds, peak_mem_mb, updated_at, rebuilt) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                by_day[day])
            conn.commit()
        conn.executemany(
            "INSERT OR IGNORE INTO interactive_hourly (hour, server_id, runs, measured_runs, "
            "people_sum, people_max, measured_people_sum, busy_people_sum, busy_people_max, "
            "sessions_max, open_seconds, busy_seconds, cpu_seconds, mem_mb_max, updated_at, rebuilt) "
            "VALUES (?,?,?,0,?,?,0,0,0,0,?,0,0,?,?,1)",
            [(hour, server, r, ps, pm, op, mm, stamp) for hour, (r, ps, pm, op, mm) in hourly.items()])
        conn.executemany(
            "INSERT OR IGNORE INTO interactive_person_hourly (hour, server_id, user, runs, "
            "open_seconds, busy_seconds, measured_seconds, cpu_seconds, mem_mb_max, updated_at, "
            "rebuilt) VALUES (?,?,?,?,?,0,0,0,?,?,1)",
            [(hour, server, user, r, s, mm, stamp) for (hour, user), (r, s, mm) in person.items()])
        conn.commit()
    return {"servers": out, "already": already}

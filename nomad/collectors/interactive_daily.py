# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Each person's interactive sessions, day by day, kept for good.

The interactive collector records every RStudio and Jupyter process at each
run (``interactive_sessions``): a snapshot every five minutes, around 30,000
rows a day on a busy teaching server. Questions over a term or a year -- how
many people, how many hours, by course, how much of it idle -- need small
totals instead, and those are kept here:

``interactive_daily``: one row per day, server, person and session type:

* ``sessions``: sessions open that day (a session running across midnight
  counts on both days);
* ``session_seconds``: time sessions were open, summed over sessions (two
  notebooks open for an hour are two session-hours);
* ``busy_seconds`` of those: time a session used CPU (at least
  BUSY_PERCENT of one core, averaged between two runs), and
  ``cpu_measured_seconds``: the time over which that could be judged;
* ``cpu_seconds``: CPU used;
* ``mem_mb_seconds``: memory held over time (MB x seconds; / 3.6e6 is
  GB-hours) and ``peak_mem_mb``: the most one session of that type held.

A session is a process tree: workers a session forked are part of it, a
kernel is a session of its own (a person with JupyterLab open and two
notebooks has three: the server and two kernels). So that a person's time is
not counted once per session, each person also has a row of session type
``(any)``: ``session_seconds`` there is time with at least one session open,
``busy_seconds`` time with at least one busy, and memory and CPU are their
sessions' together; ``peak_mem_mb`` the most they held at once.

Memory is resident memory added up over a session's processes, so pages that
forked workers share count once per worker.

By the hour, for peaks and for when a class works (also kept for good):

``interactive_hourly``: one row per hour and server. ``runs`` readings fell in
the hour (``measured_runs`` of them with a known interval); ``people_max`` /
``people_sum`` the most people on at once and their sum over readings
(``people_sum / runs`` is the hour's average), ``busy_people_max`` /
``busy_people_sum`` the same for people with a busy session, over measured
readings only, out of ``measured_people_sum`` (people whose CPU was measured:
``busy_people_sum / measured_people_sum`` is the share busy);
``sessions_max``; ``open_seconds`` and ``busy_seconds`` (person-seconds,
summed over people), ``cpu_seconds``, and ``mem_mb_max``, the most memory
everyone held at once. A reading with nobody on counts (zero people), so
averages are over the whole hour; an hour the collector did not run has no
row. As for the days, the time between two readings goes to the later one's
hour. Busy is computing (BUSY_PERCENT of a core): someone reading or typing
in a notebook is on, not busy.

``interactive_person_hourly``: one row per hour, server and person with a
session: ``open_seconds``, ``busy_seconds``, ``measured_seconds``,
``cpu_seconds``, ``mem_mb_max`` (the most they held at once) and ``runs``.

By environment, for courses (nomad 1.12.5; kept for good):

``interactive_env_daily``: one row per day, server, person, environment a
session ran from (a conda environment, often a course's Jupyter kernel; see
nomad.collectors.interactive.environment_of), where it is (``path``, its
directory) and ``own`` (1: the person's own, which they own or keep in their
home -- anyone can name one like a course): ``sessions`` opened that day,
``session_seconds`` with at least one session in it open, ``busy_seconds``
with one busy, and ``cpu_seconds``. Sessions from the base installation have no environment and
no row. Who used a course's environment this term is who is in the class,
whatever groups they are in.

Hours are local time (``YYYY-MM-DDTHH``): the hour repeated when daylight
time ends holds both (twice the readings; averages stay right), and the hour
skipped when it starts has no row.

The time between two runs is added to the day the later one falls on. A gap
longer than MAX_GAP_SECONDS (the collector was stopped) adds no time: the
sessions are counted, their hours are not guessed. CPU comes from each
process's own counter, read at every run (``interactive_state``, this host's
working state, not synced): ps's %CPU is an average over the process's whole
life, which says a notebook busy now is idle when it was idle for a day.
"""
from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from datetime import datetime

MAX_GAP_SECONDS = 2 * 3600
BUSY_PERCENT = 2.0
ANY = "(any)"
# The Jupyter server's own process (its kernels and terminals are not in its
# CPU) polls all the time, a few percent of a core: that is not the person
# computing. Its own row keeps its busy time; a person, an hour and an
# environment count as busy only for their other sessions (1.13.2; before,
# anyone with JupyterLab open looked busy all the time).
NOT_COMPUTING = ("Jupyter Server",)

DAILY = """
CREATE TABLE IF NOT EXISTS interactive_daily (
    day                  TEXT NOT NULL,
    server_id            TEXT NOT NULL,
    user                 TEXT NOT NULL,
    session_type         TEXT NOT NULL,
    sessions             INTEGER NOT NULL DEFAULT 0,
    session_seconds      REAL NOT NULL DEFAULT 0,
    busy_seconds         REAL NOT NULL DEFAULT 0,
    cpu_measured_seconds REAL NOT NULL DEFAULT 0,
    cpu_seconds          REAL NOT NULL DEFAULT 0,
    mem_mb_seconds       REAL NOT NULL DEFAULT 0,
    peak_mem_mb          REAL,
    updated_at           TEXT,
    PRIMARY KEY (day, server_id, user, session_type)
)"""
HOURLY = """
CREATE TABLE IF NOT EXISTS interactive_hourly (
    hour             TEXT NOT NULL,
    server_id        TEXT NOT NULL,
    runs             INTEGER NOT NULL DEFAULT 0,
    measured_runs    INTEGER NOT NULL DEFAULT 0,
    people_sum       INTEGER NOT NULL DEFAULT 0,
    people_max       INTEGER NOT NULL DEFAULT 0,
    measured_people_sum INTEGER NOT NULL DEFAULT 0,
    busy_people_sum  INTEGER NOT NULL DEFAULT 0,
    busy_people_max  INTEGER NOT NULL DEFAULT 0,
    sessions_max     INTEGER NOT NULL DEFAULT 0,
    open_seconds     REAL NOT NULL DEFAULT 0,
    busy_seconds     REAL NOT NULL DEFAULT 0,
    cpu_seconds      REAL NOT NULL DEFAULT 0,
    mem_mb_max       REAL NOT NULL DEFAULT 0,
    updated_at       TEXT,
    PRIMARY KEY (hour, server_id)
)"""
PERSON_HOURLY = """
CREATE TABLE IF NOT EXISTS interactive_person_hourly (
    hour              TEXT NOT NULL,
    server_id         TEXT NOT NULL,
    user              TEXT NOT NULL,
    runs              INTEGER NOT NULL DEFAULT 0,
    open_seconds      REAL NOT NULL DEFAULT 0,
    busy_seconds      REAL NOT NULL DEFAULT 0,
    measured_seconds  REAL NOT NULL DEFAULT 0,
    cpu_seconds       REAL NOT NULL DEFAULT 0,
    mem_mb_max        REAL NOT NULL DEFAULT 0,
    updated_at        TEXT,
    PRIMARY KEY (hour, server_id, user)
)"""
ENV_DAILY = """
CREATE TABLE IF NOT EXISTS interactive_env_daily (
    day              TEXT NOT NULL,
    server_id        TEXT NOT NULL,
    user             TEXT NOT NULL,
    environment      TEXT NOT NULL,
    path             TEXT NOT NULL DEFAULT '',
    own              INTEGER NOT NULL DEFAULT 0,
    sessions         INTEGER NOT NULL DEFAULT 0,
    session_seconds  REAL NOT NULL DEFAULT 0,
    busy_seconds     REAL NOT NULL DEFAULT 0,
    cpu_seconds      REAL NOT NULL DEFAULT 0,
    updated_at       TEXT,
    PRIMARY KEY (day, server_id, user, environment, path, own)
)"""
STATE = """
CREATE TABLE IF NOT EXISTS interactive_state (
    key        TEXT PRIMARY KEY,
    cpu_ticks  REAL,
    day        TEXT,
    seen_at    TEXT
)"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.execute(DAILY)
    conn.execute(HOURLY)
    conn.execute(PERSON_HOURLY)
    conn.execute(ENV_DAILY)
    conn.execute(STATE)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_interactive_env_daily_env "
                 "ON interactive_env_daily(environment, day)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_interactive_daily_user "
                 "ON interactive_daily(user, day)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_interactive_person_hourly_user "
                 "ON interactive_person_hourly(user, hour)")
    # Rows rebuilt from the raw snapshots (nomad interactive backfill) say so.
    for table in ("interactive_daily", "interactive_hourly", "interactive_person_hourly"):
        if "rebuilt" not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN rebuilt INTEGER NOT NULL DEFAULT 0")


def _add(conn: sqlite3.Connection, table: str, keys: dict, add: dict, top: dict,
         stamp: str) -> None:
    """Add ``add`` to the row's columns and raise ``top``'s to at least the
    values given, creating the row. SELECT, then UPDATE or INSERT: no
    ON CONFLICT, which older SQLite libraries lack."""
    where = " AND ".join(f"{k} = ?" for k in keys)
    cols = list(add) + list(top)
    row = conn.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE {where}",
                       tuple(keys.values())).fetchone()
    if row is None:
        names = list(keys) + cols + ["updated_at"]
        values = list(keys.values()) + list(add.values()) + list(top.values()) + [stamp]
        conn.execute(f"INSERT INTO {table} ({', '.join(names)}) "
                     f"VALUES ({', '.join('?' * len(names))})", values)
        return
    old = dict(zip(cols, row))
    values = [old[k] + v for k, v in add.items()] + [max(old[k] or 0, v) for k, v in top.items()]
    sets = ", ".join(f"{c} = ?" for c in cols)
    conn.execute(f"UPDATE {table} SET {sets}, updated_at = ? WHERE {where}",
                 values + [stamp] + list(keys.values()))


def _key(s: dict) -> str:
    return f"{s.get('pid')}:{s.get('start_time')}"


def update(conn: sqlite3.Connection, sessions: list, now: datetime, server_id: str,
           clk_tck: float = 100.0, epoch: float | None = None) -> dict:
    """Add this run's sessions to today's totals. ``now`` (local time) says
    which day; the time between runs is measured on ``epoch`` (seconds, by
    default the system clock), so a change to or from daylight time adds or
    loses no hour. Returns the rows touched ({(user, type): totals}) for
    tests. Run in one write transaction, so two runs at once cannot both
    add the same interval."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    ensure(conn)
    epoch = time.time() if epoch is None else epoch
    prev_epoch = None
    prev = {}
    for key, ticks, day, _seen_at in conn.execute(
            "SELECT key, cpu_ticks, day, seen_at FROM interactive_state"):
        if key == "last_run":
            prev_epoch = ticks
        else:
            prev[key] = (ticks, day)
    dt = None
    if prev_epoch is not None:
        gap = epoch - prev_epoch
        if 0 < gap <= MAX_GAP_SECONDS:
            dt = gap
    day = now.date().isoformat()

    totals = defaultdict(lambda: {"sessions": 0, "session_seconds": 0.0, "busy_seconds": 0.0,
                                  "cpu_measured_seconds": 0.0, "cpu_seconds": 0.0,
                                  "mem_mb_seconds": 0.0, "peak_mem_mb": None})
    state = []
    person = defaultdict(lambda: {"busy": False, "measured": False, "mem": 0.0, "cpu": 0.0,
                                  "new": 0})
    # (user, environment, path, own) -> sessions new today, busy, CPU: a
    # person's time in an environment, once however many of its sessions
    # they had open.
    envs = defaultdict(lambda: {"new": 0, "busy": False, "cpu": 0.0})
    counted = 0
    for s in sessions:
        user, kind = s.get("user"), s.get("session_type")
        if not user or not kind:
            continue
        counted += 1
        me = person[user]
        key = _key(s)
        ticks = s.get("cpu_ticks")
        before = prev.get(key)
        t = totals[(user, kind)]
        env = s.get("environment")
        e = envs[(user, env, s.get("environment_path") or "",
                  1 if s.get("environment_own") else 0)] if env else None
        if before is None or before[1] != day:
            t["sessions"] += 1
            me["new"] += 1
            if e is not None:
                e["new"] += 1
        mem = s.get("mem_mb") or 0.0
        me["mem"] += mem
        t["peak_mem_mb"] = mem if t["peak_mem_mb"] is None else max(t["peak_mem_mb"], mem)
        if dt:
            t["session_seconds"] += dt
            t["mem_mb_seconds"] += mem * dt
            cpu = None
            if ticks is not None:
                if before is not None and before[0] is not None and ticks >= before[0]:
                    cpu = (ticks - before[0]) / clk_tck
                elif before is None:
                    # New since the last run: its CPU since it started, if it
                    # started within the interval (else nothing is known).
                    age = s.get("age_seconds")
                    if age is None and s.get("age_hours") is not None:
                        age = s["age_hours"] * 3600
                    if age is not None and age <= dt + 1:
                        cpu = ticks / clk_tck
            if cpu is not None:
                t["cpu_seconds"] += cpu
                t["cpu_measured_seconds"] += dt
                me["cpu"] += cpu
                me["measured"] = True
                if e is not None:
                    e["cpu"] += cpu
                if cpu / dt * 100.0 >= BUSY_PERCENT:
                    t["busy_seconds"] += dt
                    if kind not in NOT_COMPUTING:
                        me["busy"] = True
                        if e is not None:
                            e["busy"] = True
        state.append((key, ticks, day, now.isoformat()))

    for user, me in person.items():
        t = totals[(user, ANY)]
        t["sessions"] += me["new"]
        t["peak_mem_mb"] = me["mem"]
        if dt:
            t["session_seconds"] += dt
            t["mem_mb_seconds"] += me["mem"] * dt
            if me["measured"]:
                t["cpu_measured_seconds"] += dt
                t["cpu_seconds"] += me["cpu"]
            if me["busy"]:
                t["busy_seconds"] += dt

    stamp = now.isoformat()
    hour = now.strftime("%Y-%m-%dT%H")
    busy_people = sum(1 for me in person.values() if me["busy"])
    measured_people = sum(1 for me in person.values() if me["measured"])
    _add(conn, "interactive_hourly", {"hour": hour, "server_id": server_id},
         {"runs": 1, "measured_runs": 1 if dt else 0,
          "people_sum": len(person), "busy_people_sum": busy_people if dt else 0,
          "measured_people_sum": measured_people if dt else 0,
          "open_seconds": len(person) * dt if dt else 0.0,
          "busy_seconds": busy_people * dt if dt else 0.0,
          "cpu_seconds": sum(me["cpu"] for me in person.values())},
         {"people_max": len(person), "busy_people_max": busy_people if dt else 0,
          "sessions_max": counted, "mem_mb_max": sum(me["mem"] for me in person.values())},
         stamp)
    for user, me in person.items():
        _add(conn, "interactive_person_hourly",
             {"hour": hour, "server_id": server_id, "user": user},
             {"runs": 1, "open_seconds": dt or 0.0,
              "busy_seconds": dt if dt and me["busy"] else 0.0,
              "measured_seconds": dt if dt and me["measured"] else 0.0,
              "cpu_seconds": me["cpu"]},
             {"mem_mb_max": me["mem"]}, stamp)
    for (user, env, path, own), e in envs.items():
        _add(conn, "interactive_env_daily",
             {"day": day, "server_id": server_id, "user": user, "environment": env,
              "path": path, "own": own},
             {"sessions": e["new"], "session_seconds": dt or 0.0,
              "busy_seconds": dt if dt and e["busy"] else 0.0, "cpu_seconds": e["cpu"]},
             {}, stamp)
    for (user, kind), t in totals.items():
        row = conn.execute(
            "SELECT sessions, session_seconds, busy_seconds, cpu_measured_seconds, cpu_seconds, "
            "mem_mb_seconds, peak_mem_mb FROM interactive_daily "
            "WHERE day = ? AND server_id = ? AND user = ? AND session_type = ?",
            (day, server_id, user, kind)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO interactive_daily (day, server_id, user, session_type, sessions, "
                "session_seconds, busy_seconds, cpu_measured_seconds, cpu_seconds, "
                "mem_mb_seconds, peak_mem_mb, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (day, server_id, user, kind, t["sessions"], t["session_seconds"],
                 t["busy_seconds"], t["cpu_measured_seconds"], t["cpu_seconds"],
                 t["mem_mb_seconds"], t["peak_mem_mb"], stamp))
        else:
            peak = t["peak_mem_mb"] if row[6] is None else max(row[6], t["peak_mem_mb"] or 0)
            conn.execute(
                "UPDATE interactive_daily SET sessions = ?, session_seconds = ?, "
                "busy_seconds = ?, cpu_measured_seconds = ?, cpu_seconds = ?, "
                "mem_mb_seconds = ?, peak_mem_mb = ?, updated_at = ? "
                "WHERE day = ? AND server_id = ? AND user = ? AND session_type = ?",
                (row[0] + t["sessions"], row[1] + t["session_seconds"],
                 row[2] + t["busy_seconds"], row[3] + t["cpu_measured_seconds"],
                 row[4] + t["cpu_seconds"], row[5] + t["mem_mb_seconds"], peak, stamp,
                 day, server_id, user, kind))
    conn.execute("DELETE FROM interactive_state")
    conn.executemany("INSERT OR REPLACE INTO interactive_state (key, cpu_ticks, day, seen_at) "
                     "VALUES (?, ?, ?, ?)", state)
    conn.execute("INSERT OR REPLACE INTO interactive_state (key, cpu_ticks, day, seen_at) "
                 "VALUES ('last_run', ?, ?, ?)", (epoch, day, stamp))
    return dict(totals)


def prune(conn: sqlite3.Connection, retention_days: int, now: datetime,
          batch: int = 100_000) -> int:
    """Delete raw snapshots older than ``retention_days`` (0: keep them),
    a batch at a time. The daily totals stay."""
    if not retention_days or retention_days <= 0:
        return 0
    from datetime import timedelta
    cutoff = (now - timedelta(days=retention_days)).isoformat()
    cur = conn.execute(
        "DELETE FROM interactive_sessions WHERE rowid IN (SELECT rowid FROM "
        "interactive_sessions WHERE timestamp < ? LIMIT ?)", (cutoff, batch))
    return cur.rowcount or 0

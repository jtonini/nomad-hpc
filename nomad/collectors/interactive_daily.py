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
STATE = """
CREATE TABLE IF NOT EXISTS interactive_state (
    key        TEXT PRIMARY KEY,
    cpu_ticks  REAL,
    day        TEXT,
    seen_at    TEXT
)"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.execute(DAILY)
    conn.execute(STATE)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_interactive_daily_user "
                 "ON interactive_daily(user, day)")


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
    for s in sessions:
        user, kind = s.get("user"), s.get("session_type")
        if not user or not kind:
            continue
        me = person[user]
        key = _key(s)
        ticks = s.get("cpu_ticks")
        before = prev.get(key)
        t = totals[(user, kind)]
        if before is None or before[1] != day:
            t["sessions"] += 1
            me["new"] += 1
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
                if cpu / dt * 100.0 >= BUSY_PERCENT:
                    t["busy_seconds"] += dt
                    me["busy"] = True
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

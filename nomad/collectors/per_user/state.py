# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ per-user collector — what one run leaves for the next.

The collector runs from cron, a fresh process each time. To turn cumulative
counters into rates over the interval, and to know how long a rule's
condition has held, each run saves a row per live process (and per user,
per user slice, and one for the run itself) in ``per_user_state``, and the
next run reads them back. Rows of processes that are gone are dropped, so
the table stays the size of the process table.

The table holds counters, not names: its key is the process session id
(a hash), ``user:<uid>`` or ``slice:<uid>``. It is local working state and
``nomad sync`` does not copy it to the hub.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field

RUN_KEY = "run"

STATE_SQL = """
CREATE TABLE IF NOT EXISTS per_user_state (
    hostname            TEXT NOT NULL,
    key                 TEXT NOT NULL,
    kind                TEXT NOT NULL,
    seen_at             REAL NOT NULL,
    cpu_seconds         REAL,
    io_read_bytes       INTEGER,
    io_write_bytes      INTEGER,
    rules               TEXT,
    peak_cpu_percent    REAL,
    peak_memory_bytes   INTEGER,
    PRIMARY KEY (hostname, key)
)
"""


def make_session_id(hostname: str, pid: int, start_time: float) -> str:
    """Deterministic session id for a process: a recycled pid gets a new
    one. 16 hex chars (64 bits) is plenty within a host's history."""
    h = hashlib.sha1(f"{hostname}|{pid}|{int(start_time)}".encode()).hexdigest()
    return h[:16]


def alert_key(hostname: str, session_id: str, rule_id: str) -> str:
    """per_user_alert.dedup_key: one alert row per process (or episode) and rule."""
    return f"{hostname}|{session_id}|{rule_id}"


@dataclass
class Held:
    """A rule's condition on a process or user: since when it has held, and
    the peaks while it has (an episode's own, not the process's lifetime)."""
    since: float
    peak_cpu_percent: float | None = None
    peak_memory_bytes: int | None = None


@dataclass
class StateRow:
    key: str
    kind: str                              # process | user | slice | run
    seen_at: float
    cpu_seconds: float | None = None
    io_read_bytes: int | None = None
    io_write_bytes: int | None = None
    rules: dict = field(default_factory=dict)   # rule_id -> Held
    peak_cpu_percent: float | None = None
    peak_memory_bytes: int | None = None


def _held(value) -> Held | None:
    if isinstance(value, (int, float)):
        return Held(float(value))
    if isinstance(value, list) and value and isinstance(value[0], (int, float)):
        rest = list(value[1:3]) + [None] * (2 - len(value[1:3]))
        return Held(float(value[0]), rest[0], rest[1])
    return None


def load(conn: sqlite3.Connection, hostname: str) -> dict[str, StateRow]:
    try:
        rows = conn.execute(
            "SELECT key, kind, seen_at, cpu_seconds, io_read_bytes, io_write_bytes, "
            "rules, peak_cpu_percent, peak_memory_bytes FROM per_user_state "
            "WHERE hostname = ?", (hostname,)).fetchall()
    except sqlite3.OperationalError:       # table not created yet
        return {}
    out = {}
    for key, kind, seen_at, cpu, rd, wr, rules, pcpu, pmem in rows:
        try:
            parsed = json.loads(rules) if rules else {}
        except ValueError:
            parsed = {}
        held = {k: h for k, h in ((k, _held(v)) for k, v in parsed.items()) if h is not None}
        out[key] = StateRow(key, kind, seen_at, cpu, rd, wr, held, pcpu, pmem)
    return out


def save(conn: sqlite3.Connection, hostname: str, rows: list[StateRow]) -> None:
    """Replace this host's state with ``rows`` (in the caller's transaction)."""
    conn.execute(STATE_SQL)
    conn.execute("DELETE FROM per_user_state WHERE hostname = ?", (hostname,))
    conn.executemany(
        "INSERT INTO per_user_state (hostname, key, kind, seen_at, cpu_seconds, "
        "io_read_bytes, io_write_bytes, rules, peak_cpu_percent, peak_memory_bytes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(hostname, r.key, r.kind, r.seen_at, r.cpu_seconds, r.io_read_bytes,
          r.io_write_bytes,
          json.dumps({k: [h.since, h.peak_cpu_percent, h.peak_memory_bytes]
                      for k, h in r.rules.items()}) if r.rules else None,
          r.peak_cpu_percent, r.peak_memory_bytes) for r in rows])

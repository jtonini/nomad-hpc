# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Jobs on lab machines, the hub's side (1.67.0): who the Console lets send
a job to a lab machine ([[console.files.machines]], the gate alone, started
there by nomad.submit.machine_run), how much of a machine nomad's jobs may
hold together, and the Console's record of the jobs it started.

    [console.machine_runs]
    open = "staff"         # "off" (the default), "staff", or "labs": staff, and each lab's
                           # members on their own lab's machines (as My files offers them)
    share = 50             # percent of a machine's cores and of its memory nomad's jobs may
                           # hold together (10 to 90); whoever sits at it keeps the rest
    most_hours = 72        # the longest time a job may ask for (1 to 720)

A machine starts jobs only where its own gate says so too (nomad-gate
install-key --runs there: `serve --runs`), and never where Slurm is.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

OPENS = ("off", "staff", "labs")
SHARE = 50
MOST_HOURS = 72
KEEP_DAYS = 30
OPS = ("machine", "start", "runs", "run", "stop")


@dataclass(frozen=True)
class Settings:
    open: str = "off"
    share: int = SHARE
    most_hours: int = MOST_HOURS
    problems: tuple = field(default_factory=tuple)


def settings(config: dict | None) -> Settings:
    console = (config or {}).get("console") if isinstance((config or {}).get("console"), dict) else {}
    raw = console.get("machine_runs") if isinstance(console.get("machine_runs"), dict) else {}
    problems = []
    opened = raw.get("open", "off")
    if not isinstance(opened, str) or opened.strip().lower() not in OPENS:
        problems.append(f"[console.machine_runs] open: one of {', '.join(OPENS)} (left off)")
        opened = "off"
    opened = opened.strip().lower()

    def number(key, lo, hi, default):
        v = raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            problems.append(f"[console.machine_runs] {key}: a whole number from {lo} to {hi} ({default} used)")
            return default
        return v
    share = number("share", 10, 90, SHARE)
    most = number("most_hours", 1, 720, MOST_HOURS)
    unknown = sorted(set(raw) - {"open", "share", "most_hours"})
    if unknown:
        problems.append(f"[console.machine_runs]: {', '.join(unknown)} isn't a setting here")
    return Settings(open=opened, share=share, most_hours=most, problems=tuple(problems))


def opens_to(s: Settings, role: str | None, member: bool = False) -> bool:
    """Whether a caller may send jobs to lab machines at all: staff once it
    is on; with open = "labs", also someone in the machine's lab (``member``:
    the caller has a machine of their lab, as My files offers them)."""
    staff = role in ("admin", "operator")
    if s.open == "off":
        return False
    if s.open == "staff":
        return staff
    return staff or member


def seconds_of(text: str) -> int:
    from nomad.submit.machine_run import seconds_of as sec
    return sec(text)


def within(s: Settings, time_text: str) -> str | None:
    """Why a time asked for is too long here, else None."""
    from nomad.submit.machine_run import RunError
    try:
        secs = seconds_of(time_text)
    except RunError as e:
        return str(e)
    if secs > s.most_hours * 3600:
        return f"at most {s.most_hours} hours on a lab machine here"
    return None


# ── the Console's record of the jobs it started ─────────────────────────────

class Store:
    """Which machine each job the Console started for a person is on, so
    their pages ask only those machines (each machine keeps the job's own
    record). One file, beside the Console's other records."""

    def __init__(self, path: str):
        self.path = str(path)
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS machine_runs (user TEXT NOT NULL, machine TEXT NOT NULL, "
                       "run TEXT NOT NULL, started TEXT NOT NULL, script TEXT, cpus INTEGER, ended TEXT, "
                       "PRIMARY KEY (user, machine, run))")
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def add(self, user: str, machine: str, run: str, script: str | None, cpus: int | None,
            started: str | None = None) -> None:
        with self._lock, self._db() as db:
            db.execute("INSERT OR IGNORE INTO machine_runs (user, machine, run, started, script, cpus) "
                       "VALUES (?, ?, ?, ?, ?, ?)",
                       (user, machine, run, started or datetime.now().isoformat(timespec="seconds"), script, cpus))

    def ended(self, user: str, machine: str, run: str, at: str | None = None) -> None:
        with self._lock, self._db() as db:
            db.execute("UPDATE machine_runs SET ended = ? WHERE user = ? AND machine = ? AND run = ? "
                       "AND ended IS NULL", (at or datetime.now().isoformat(timespec="seconds"), user, machine, run))

    def mine(self, user: str, days: int = 14) -> list:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self._lock, self._db() as db:
            rows = db.execute("SELECT * FROM machine_runs WHERE user = ? AND (ended IS NULL OR started >= ?) "
                              "ORDER BY started DESC LIMIT 100", (user, since)).fetchall()
        return [dict(r) for r in rows]

    def tidy(self) -> int:
        cut = (datetime.now() - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
        with self._lock, self._db() as db:
            return db.execute("DELETE FROM machine_runs WHERE ended IS NOT NULL AND ended < ?", (cut,)).rowcount

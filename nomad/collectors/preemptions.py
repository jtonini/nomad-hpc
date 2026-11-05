# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The jobs Slurm preempted (1.28.0): a guest's job stopped because a
condo's owners needed their nodes -- requeued (it runs again, from the top of
its script) or cancelled.

Each run asks Slurm's accounting for the job instances that ended PREEMPTED
in the last ``hours`` (2), every instance (``sacct -D``: a requeued job keeps
its number, so each run of it is its own record; a REQUEUED record is an
admin's requeue or a failed prolog, not a preemption), then whether each came
back (another instance of that job: requeued, else cancelled), and keeps
each once: ``job_preemptions``. The hub tells the person, when
``[submit.preemption] notices = true`` (nomad.alerts.hub_preemptions).

    [collectors.preemptions]
    enabled = true                 # a Slurm head node whose account sees every user's jobs
    # hours = 2
"""
from __future__ import annotations

import logging
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta
from typing import Any

from .base import BaseCollector, CollectionError, find_tool, tool_env

logger = logging.getLogger(__name__)

SEP = "\x1f"
FIELDS = ("JobID", "JobName", "User", "Partition", "State", "Start", "End", "Elapsed", "NodeList")
HOURS = 2
# Slurm puts a preempted job back in the queue within moments; an instance
# that ended this recently with no other found yet is "not known yet"
HOLD_MINUTES = 15
_JOB = re.compile(r"[0-9]{1,12}(?:_[0-9]{1,9})?")


def parse(text: str) -> list[dict]:
    out = []
    for line in (text or "").splitlines():
        parts = line.split(SEP)
        if len(parts) < len(FIELDS):
            continue
        d = dict(zip(("job_id", "job_name", "user", "partition", "state", "start", "end", "elapsed", "nodes"),
                     (x.strip() for x in parts)))
        state = d["state"].split()[0].upper() if d["state"] else ""
        if not _JOB.fullmatch(d["job_id"]) or state != "PREEMPTED" or not d["user"]:
            continue
        d["state"] = state
        d["job_name"] = d["job_name"][:128]
        out.append(d)
    return out


def _after(start: str, than: str) -> bool:
    """An instance that started after ``than``, or hasn't started (pending:
    Slurm's "Unknown" or "None"); an earlier one is an earlier run's."""
    v = (start or "").strip()
    if not v or v.lower() in ("unknown", "none", "n/a"):
        return True
    return v > (than or "")


def _ended_within(end: str | None, now: datetime, minutes: int) -> bool:
    try:
        return now - datetime.fromisoformat(str(end)) < timedelta(minutes=minutes)
    except (TypeError, ValueError):
        return False


class PreemptionsCollector(BaseCollector):
    """The job instances Slurm preempted or requeued, from its accounting."""

    name = "preemptions"
    description = "Jobs Slurm preempted or requeued (a guest on a condo), from sacct"
    default_interval = 300

    def __init__(self, config: dict[str, Any], db_path):
        super().__init__(config, db_path)
        h = self.config.get("hours", HOURS)
        self.hours = max(1, min(48, int(h))) if isinstance(h, (int, float)) and not isinstance(h, bool) else HOURS

    def collect(self) -> list[dict[str, Any]]:
        sacct = find_tool("sacct")
        if not sacct:
            raise CollectionError("no sacct here")
        argv = [sacct, "-a", "-D", "-X", "-n", "-P", f"--delimiter={SEP}", "-S", f"now-{self.hours}hours",
                "-s", "PR", "-o", ",".join(FIELDS)]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=60,
                               stdin=subprocess.DEVNULL, env=tool_env())
        except subprocess.TimeoutExpired:
            raise CollectionError("sacct: no answer in 60 s") from None
        except OSError as e:
            raise CollectionError(f"sacct: {e}") from None
        if r.returncode != 0:
            raise CollectionError(f"sacct: {(r.stderr or '').strip()[-200:] or f'exit {r.returncode}'}")
        found = parse(r.stdout)
        if found:
            self._came_back(sacct, found)
        return found

    def _came_back(self, sacct: str, found: list, now: datetime | None = None) -> None:
        """Each preempted instance: requeued (the job has an instance that
        started after it, or hasn't started: pending), cancelled (none, and it
        ended over HOLD_MINUTES ago) or not known yet (None: read again at the
        next run, while sacct still lists it)."""
        ids = sorted({d["job_id"] for d in found})
        argv = [sacct, "-a", "-D", "-X", "-n", "-P", f"--delimiter={SEP}", "-j", ",".join(ids),
                "-o", "JobID,State,Start"]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=60,
                               stdin=subprocess.DEVNULL, env=tool_env())
            lines = r.stdout.splitlines() if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            lines = None
        if lines is None:
            return                              # unknown: said as unknown
        runs: dict = {}
        for line in lines:
            parts = line.split(SEP)
            if len(parts) >= 3:
                runs.setdefault(parts[0].strip(), []).append((parts[1].strip().split(" ")[0].upper(),
                                                             parts[2].strip()))
        now = now or datetime.now()
        for d in found:
            later = [x for x in runs.get(d["job_id"], [])
                     if not (x[0] == "PREEMPTED" and x[1] == d["start"]) and _after(x[1], d["start"])]
            if later:
                d["requeued"] = 1
            elif _ended_within(d.get("end"), now, HOLD_MINUTES):
                d["requeued"] = None
            else:
                d["requeued"] = 0

    def count_records(self, data) -> int:
        return len(data)

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS job_preemptions (
            job_id TEXT NOT NULL, job_name TEXT, user TEXT NOT NULL, partition TEXT, state TEXT,
            start TEXT NOT NULL, end TEXT, elapsed TEXT, nodes TEXT, first_seen TEXT, requeued INTEGER,
            PRIMARY KEY (job_id, start));
        CREATE INDEX IF NOT EXISTS idx_job_preemptions_seen ON job_preemptions(first_seen);
    """

    def store(self, data: list[dict[str, Any]]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.executescript(self.SCHEMA)
            for d in data:
                conn.execute("INSERT OR IGNORE INTO job_preemptions (job_id, job_name, user, partition, state, start, "
                             "end, elapsed, nodes, first_seen, requeued) VALUES (:job_id, :job_name, :user, :partition, "
                             ":state, :start, :end, :elapsed, :nodes, :first_seen, :requeued)",
                             dict(d, first_seen=now, requeued=d.get("requeued")))
                if d.get("requeued") is not None:  # known since: a job came back after a first read said not
                    conn.execute("UPDATE job_preemptions SET requeued = ? WHERE job_id = ? AND start = ? AND "
                                 "COALESCE(requeued, -1) != ?", (d["requeued"], d["job_id"], d["start"], d["requeued"]))
            conn.commit()
        finally:
            conn.close()


__all__ = ["PreemptionsCollector", "parse", "FIELDS", "HOLD_MINUTES"]

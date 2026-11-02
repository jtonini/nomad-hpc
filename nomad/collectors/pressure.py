# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Pressure stall information (PSI, 1.25.3): how much of the time work on
this host waited for CPU, memory or I/O.

Linux 4.20 and later keep it in /proc/pressure/{cpu,memory,io}, each
with two lines:

    some avg10=1.53 avg60=0.87 avg300=0.40 total=4318923
    full avg10=0.00 avg60=0.12 avg300=0.05 total=118734

"some": the share of the time at least one task waited for that resource
(work slowed down); "full": the share when every task did at once (time
lost outright; CPU has it only on newer kernels). avg10/60/300: over the
last 10 seconds, minute and five minutes, in percent; total: microseconds
waited since boot. Unlike CPU use, it tells a busy host from one where
work is waiting: an I/O-bound job on NFS shows as idle CPU and high I/O
pressure.

RHEL and Rocky 8 and 9 build it in but leave it off (psi=0 by default):
the host then has no /proc/pressure, and nomad says so -- "PSI is off here:
boot with psi=1" -- instead of failing; it records as soon as the host is
booted with it. With cgroup v2, each cgroup has the same three files: a
person's slice on a workstation, a Slurm job's cgroup on a node (not read
yet: per person and per job come later, where PSI is on).
"""
from __future__ import annotations

import logging
import os
import socket
from datetime import datetime
from typing import Any

from .base import BaseCollector

logger = logging.getLogger(__name__)

ROOT = "/proc/pressure"
RESOURCES = ("cpu", "memory", "io")
KINDS = ("some", "full")
FIELDS = ("avg10", "avg60", "avg300", "total")
OFF = "PSI is off here: boot with psi=1 (a kernel argument; RHEL and Rocky leave it off) to record it"


def parse(text: str) -> dict:
    """{"some": {"avg10": 1.53, ..., "total": 4318923}, "full": {...}} from a
    pressure file's text; a line it can't read is left out."""
    out: dict = {}
    for line in (text or "").splitlines():
        words = line.split()
        if not words or words[0] not in KINDS:
            continue
        vals = {}
        for w in words[1:]:
            k, _, v = w.partition("=")
            if k in FIELDS:
                try:
                    vals[k] = int(v) if k == "total" else float(v)
                except ValueError:
                    pass
        if len(vals) == len(FIELDS):
            out[words[0]] = vals
    return out


def read(root: str = ROOT) -> dict | None:
    """{resource: parse(...)} for this host, or None when PSI is off (no
    such folder, or no file that can be read)."""
    got = {}
    for r in RESOURCES:
        try:
            with open(os.path.join(root, r), encoding="ascii", errors="replace") as f:
                parsed = parse(f.read())
        except OSError:
            continue
        if parsed:
            got[r] = parsed
    return got or None


def rows(found: dict, host: str, when: str) -> list[dict]:
    """One row per resource, as stored."""
    out = []
    for r in RESOURCES:
        p = found.get(r)
        if not p:
            continue
        row = {"type": "pressure", "timestamp": when, "hostname": host, "resource": r}
        for kind in KINDS:
            for f in FIELDS:
                row[f"{kind}_{f}"] = (p.get(kind) or {}).get(f)
        out.append(row)
    return out


class PressureCollector(BaseCollector):
    """Pressure stall information (PSI) for the host it runs on."""

    name = "pressure"
    description = "How much of the time work waited for CPU, memory and I/O (PSI)"
    default_interval = 60

    def __init__(self, config: dict[str, Any], db_path: str):
        super().__init__(config, db_path)
        self.root = config.get("root") or ROOT

    def collect(self) -> list[dict[str, Any]]:
        host = socket.gethostname().split(".")[0]
        when = datetime.now().isoformat(timespec="seconds")
        found = read(self.root)
        if found is None:
            self.note = OFF
            return [{"type": "pressure_status", "timestamp": when, "hostname": host, "available": 0, "why": OFF}]
        return [{"type": "pressure_status", "timestamp": when, "hostname": host, "available": 1, "why": None},
                *rows(found, host, when)]

    def store(self, data: list[dict[str, Any]]) -> None:
        with self.get_db_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pressure (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME NOT NULL,
                    hostname TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    some_avg10 REAL, some_avg60 REAL, some_avg300 REAL, some_total INTEGER,
                    full_avg10 REAL, full_avg60 REAL, full_avg300 REAL, full_total INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pressure_ts ON pressure(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pressure_host ON pressure(hostname, resource, timestamp)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pressure_status (
                    hostname TEXT PRIMARY KEY,
                    checked DATETIME NOT NULL,
                    available INTEGER NOT NULL,
                    why TEXT
                )
            """)
            for r in data:
                if r.get("type") == "pressure_status":
                    conn.execute("INSERT OR REPLACE INTO pressure_status (hostname, checked, available, why) "
                                 "VALUES (?, ?, ?, ?)", (r["hostname"], r["timestamp"], r["available"], r["why"]))
                elif r.get("type") == "pressure":
                    conn.execute(
                        "INSERT INTO pressure (timestamp, hostname, resource, some_avg10, some_avg60, some_avg300, "
                        "some_total, full_avg10, full_avg60, full_avg300, full_total) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (r["timestamp"], r["hostname"], r["resource"], r["some_avg10"], r["some_avg60"],
                         r["some_avg300"], r["some_total"], r["full_avg10"], r["full_avg60"], r["full_avg300"],
                         r["full_total"]))
            conn.commit()

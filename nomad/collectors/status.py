# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""How collectors have been running, from ``collection_log``.

Every collector run is logged: success, records, and either the error or --
on a successful run that collected nothing -- the reason (``note``). This
module reads that log for ``nomad collectors``: on a site's own database for
the collectors configured there, and on a hub's combined database for every
site at once.

A collector that runs and finds nothing used to look like one that works
(success, 0 records) -- nfs at every UR site for months, per_user on arachne
since May. Here "runs, never any data" and its reason are said plainly.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class RunStats:
    runs: int = 0
    failed: int = 0
    with_data: int = 0
    last_run: str | None = None
    last_data: str | None = None     # ever, not only in the window
    last_message: str | None = None  # the newest error or note in the window

    def summary(self) -> str:
        if not self.runs:
            return "no runs in the window" + (
                f" (last data {self.last_data[:10]})" if self.last_data else "")
        ok = self.runs - self.failed
        parts = [f"{self.runs:,} run{'s' if self.runs != 1 else ''}"]
        if self.failed:
            parts.append("all failed" if not ok else f"{self.failed:,} failed")
        if ok:
            if self.with_data == ok:
                parts.append("all with data")
            elif self.with_data:
                parts.append(f"{self.with_data:,} with data")
            elif self.last_data:
                parts.append(f"none with data since {self.last_data[:10]}")
            else:
                parts.append("never any data")
        parts.append(f"last {str(self.last_run)[:16]}")
        return ", ".join(parts)

    @property
    def working(self) -> bool:
        return bool(self.runs) and self.with_data > 0


def read(conn: sqlite3.Connection, days: int = 7) -> dict[tuple, RunStats]:
    """{(site, collector): RunStats} over the last ``days``; site is None
    on a site's own database (no source_site column)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(collection_log)")}
    if not cols:
        return {}
    site = "source_site" if "source_site" in cols else "NULL"
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    out: dict[tuple, RunStats] = {}
    for s, k, runs, failed, with_data, last in conn.execute(
            f"SELECT {site}, collector, COUNT(*), "
            "SUM(CASE WHEN success THEN 0 ELSE 1 END), "
            "SUM(CASE WHEN success AND records_collected > 0 THEN 1 ELSE 0 END), "
            "MAX(started_at) FROM collection_log WHERE started_at >= ? GROUP BY 1, 2",
            (since,)):
        out[(s, k)] = RunStats(runs, failed or 0, with_data or 0, last)
    # The newest message in the window, error or note (SQLite returns the
    # row holding MAX() for the bare column).
    for s, k, msg, _ in conn.execute(
            f"SELECT {site}, collector, error_message, MAX(started_at) FROM collection_log "
            "WHERE started_at >= ? AND error_message IS NOT NULL AND error_message != '' "
            "GROUP BY 1, 2", (since,)):
        out.setdefault((s, k), RunStats()).last_message = " ".join(str(msg).split())
    for s, k, last in conn.execute(
            f"SELECT {site}, collector, MAX(started_at) FROM collection_log "
            "WHERE success AND records_collected > 0 GROUP BY 1, 2"):
        out.setdefault((s, k), RunStats()).last_data = last
    return out


def sites(conn: sqlite3.Connection) -> list:
    """Sites in a hub's collection_log (empty on a site's own database)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(collection_log)")}
    if "source_site" not in cols:
        return []
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT source_site FROM collection_log ORDER BY 1")]

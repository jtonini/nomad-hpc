# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""nomad's own databases (1.61.0): every ``every_hours`` (6), each one's
size, free space inside and on its disk, and each table's highest row
number (nomad.db_health says which databases and how). Warns when one grew
unusually in a day; gives free space back once a week where a database is
set for incremental vacuuming. On by default wherever nomad collects.
(1.63.0) Once a day it goes through the keep rules (nomad.keep_rules) on
the database nomad collects into, once ``nomad db keep --apply`` has run on
it, and never more than ``keep_max_rows`` rows at once.

    [collectors.databases]
    # enabled = true
    # every_hours = 6
    # keep_rules = true
"""
from __future__ import annotations

import logging
import socket
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from nomad import db_health as H
from nomad import keep_rules as K

from .base import BaseCollector

logger = logging.getLogger(__name__)


class DatabasesCollector(BaseCollector):
    """nomad's own databases: size, growth, free space, old copies."""

    name = "databases"
    description = "nomad's own databases: size, growth, free space inside, old copies"
    default_interval = 3600

    def __init__(self, config: dict[str, Any], db_path):
        super().__init__(config, db_path)
        self.s = H.settings(self.config)
        self.k = K.settings(self.config)
        # what nomad.toml says of where the databases are (passed by the plan)
        self.where = {k: self.config.get(k) for k in ("general", "database", "hub") if self.config.get(k)}

    def _due(self, conn) -> bool:
        last = H.last_reading(conn)
        return not (self.s.every_hours > 0 and last and datetime.now() - last < timedelta(hours=self.s.every_hours))

    def collect(self) -> list[dict[str, Any]]:
        for p in (*self.s.problems, *self.k.problems):
            logger.warning("databases: %s", p)
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            H.ensure(conn)
            if not self._due(conn):
                self.note = f"read less than {self.s.every_hours:g} h ago"
                return []
            now = datetime.now()
            found = H.files(self.where, self.s, site=Path(self.db_path))
            readings, unread = [], 0
            for path, role in found:
                try:
                    r = H.measure(path)
                except (OSError, sqlite3.Error) as e:
                    unread += 1
                    logger.info("databases: %s not read: %s", path, e)
                    continue
                r["role"] = role
                readings.append(r)
            for r in readings:
                H.record(conn, r, r["role"], now)
            H.prune(conn, self.s.keep_days, now)
            conn.commit()
            alerts = self._growth_alerts(conn, readings)
            kept = self._keep(conn, now) if self.k.enabled else ""
            freed = self._weekly_vacuum(conn, readings, now) if self.s.incremental_vacuum else 0
            conn.commit()
        finally:
            conn.close()
        total = sum(r["bytes"] for r in readings)
        bits = [f"{len(readings)} database(s), {H.size(total)}"]
        if unread:
            bits.append(f"{unread} not read")
        copies = sum(1 for r in readings if r["role"] in ("old copy", "empty"))
        if copies:
            bits.append(f"{copies} old cop(ies) or empty: nomad db tidy")
        if alerts:
            bits.append(f"{alerts} growing unusually")
        if kept:
            bits.append(kept)
        if freed:
            bits.append(f"{H.size(freed)} given back")
        self.note = "; ".join(bits)
        return [{"path": r["path"], "role": r["role"], "bytes": r["bytes"]} for r in readings]

    def store(self, data: list[dict[str, Any]]) -> None:
        """Kept already, in collect."""
        return None

    def _growth_alerts(self, conn, readings) -> int:
        n = 0
        for r in readings:
            if r["role"] in ("old copy", "empty"):
                continue
            got = H.unusual_growth(conn, r["path"], self.s)
            if not got:
                continue
            last, usual = got
            n += 1
            usual_txt = f", against {H.size(usual)} on a usual day" if usual else ""
            message = (f"nomad's database {Path(r['path']).name} grew {H.size(last)} in a day{usual_txt} "
                       f"({H.size(r['bytes'])} now, {H.size(r['disk_free'])} free on its disk): "
                       "nomad db status --tables says which tables")
            try:
                from nomad.alerts.dispatcher import send_alert
                send_alert(severity="warning", source="database_growth", message=message,
                           host=socket.gethostname(), subject=r["path"],
                           details={"metric": "bytes_per_day", "value": last, "usual": usual,
                                    "threshold": self.s.alert_growth_mb * 1024 * 1024,
                                    "path": r["path"], "bytes": r["bytes"], "disk_free": r["disk_free"]})
            except Exception as e:                        # noqa: BLE001 -- a note, never the reading
                logger.warning("databases: growth alert not sent: %s", e)
        return n

    def _keep(self, conn, now) -> str:
        """The keep rules, once a day, on the database nomad collects into:
        each rule only once nomad db keep --apply has gone through it by hand
        (the first run removes many rows at once; a rule new to the database,
        1.64.0, waits for it too), and a table with more rows due than
        keep_max_rows waits for it again."""
        try:
            K.ensure(conn)
            conn.commit()
            covered = K.covered(conn)
            if not covered:
                return ""
            last = K.last_run(conn)
            if last and now - last < timedelta(hours=20):
                return ""
            done = K.run(conn, self.k, "daily", now.date(), limit=self.k.max_rows, only=tuple(sorted(covered)))
        except sqlite3.Error as e:
            logger.info("databases: keep rules not gone through: %s", e)
            return ""
        gone = sum(max(0, d["before"] - d["after"]) for d in done if not d["note"])
        waits = [d["table"] for d in done if d["note"]]
        bits = []
        if gone:
            bits.append(f"{gone:,} older readings rolled up")
        if waits:
            bits.append(f"{', '.join(waits)}: nomad db keep --apply")
        return "; ".join(bits)

    def _weekly_vacuum(self, conn, readings, now) -> int:
        freed = 0
        for r in readings:
            if r["auto_vacuum"] != "incremental" or r["free_bytes"] < 64 * 1024 * 1024:
                continue
            last = H.last_vacuum(conn, r["path"])
            if last and now - last < timedelta(days=7):
                continue
            t0 = time.time()
            try:
                if Path(r["path"]).resolve() == Path(self.db_path).resolve():
                    conn.commit()
                    # stepped to the end: each step gives back one page
                    conn.execute("PRAGMA incremental_vacuum").fetchall()
                    conn.commit()
                    before, after = r["bytes"], Path(r["path"]).stat().st_size
                else:
                    before, after = H.incremental(Path(r["path"]))
            except sqlite3.Error as e:
                logger.info("databases: %s not vacuumed: %s", r["path"], e)
                continue
            H.vacuum_record(conn, r["path"], "incremental", before, after, time.time() - t0, now)
            freed += max(0, before - after)
        return freed


__all__ = ["DatabasesCollector"]

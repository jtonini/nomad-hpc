# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""When this machine's licenses end (1.56.0): each [[collectors.licenses.items]]
read where it is kept, a few times a day (nomad.licenses says how), one row a
license in ``license_status``. The hub lists them and reminds the center.

    [collectors.licenses]
    enabled = true
    # min_hours = 6             # read at most this often

    [[collectors.licenses.items]]
    name = "Posit Workbench"
    kind = "posit"
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import date, datetime, timedelta
from typing import Any

from nomad import licenses as L

from .base import BaseCollector

logger = logging.getLogger(__name__)


class LicensesCollector(BaseCollector):
    """When this machine's licenses end, read a few times a day."""

    name = "licenses"
    description = "When this machine's licenses end, read where each is kept"
    default_interval = 3600

    def __init__(self, config: dict[str, Any], db_path):
        super().__init__(config, db_path)
        self.items, self.problems = L.items_of(self.config)
        try:
            self.min_hours = float(self.config.get("min_hours", L.MIN_HOURS))
        except (TypeError, ValueError):
            self.min_hours = float(L.MIN_HOURS)

    def _recent(self) -> bool:
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            try:
                last = L.last_read(conn, [it.name for it in self.items])
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        return bool(last and datetime.now() - last < timedelta(hours=self.min_hours))

    def _forget_all(self) -> None:
        """No licenses listed here any more: none kept either."""
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=30)
            try:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (L.TABLE,)).fetchone():
                    with conn:
                        conn.execute(f"DELETE FROM {L.TABLE}")
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    def collect(self) -> list[dict[str, Any]]:
        for p in self.problems:
            logger.warning("licenses: %s", p)
        if not self.items:
            self._forget_all()
            self.note = "no [[collectors.licenses.items]]" + (f" read ({self.problems[0]})" if self.problems else "")
            return []
        if self.min_hours > 0 and self._recent():
            self.note = f"read less than {self.min_hours:g} h ago"
            return []
        today = date.today()
        return [{"item": it, "reading": L.read(it, today)} for it in self.items]

    def store(self, data: list[dict[str, Any]]) -> None:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            L.store(conn, self.items, {d["item"].name: d["reading"] for d in data})
        finally:
            conn.close()


__all__ = ["LicensesCollector"]

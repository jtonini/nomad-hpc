# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Whether licensed programs run on the lab machines this site reaches
(1.59.0): twice a day, a tiny job with each install of each program listed,
on each workstation (nomad.license_tries says how). The hub lists them and
says each change in the daily license email.

    [collectors.license_tries]
    enabled = true

    [[collectors.license_tries.programs]]
    name = "Q-Chem"
    recipe = "qchem"
    min_version = "6"
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from nomad import license_tries as T

from .base import BaseCollector

logger = logging.getLogger(__name__)


class LicenseTriesCollector(BaseCollector):
    """Whether licensed programs run on the lab machines, twice a day."""

    name = "license_tries"
    description = "Whether licensed programs run on the lab machines (a tiny job on each)"
    default_interval = 3600

    def __init__(self, config: dict[str, Any], db_path):
        super().__init__(config, db_path)
        self.s = T.settings(self.config)

    def _recent(self) -> bool:
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            try:
                last = T.last_tried(conn)
            finally:
                conn.close()
        except sqlite3.Error:
            return False
        return bool(last and datetime.now() - last < timedelta(hours=self.s.min_hours))

    def collect(self) -> list[dict[str, Any]]:
        for p in self.s.problems:
            logger.warning("license_tries: %s", p)
        if not self.s.programs:
            self.note = "no [[collectors.license_tries.programs]]"
            return []
        if not self.s.machines:
            self.note = "no machines: [collectors.workstation] workstations, or machines = [...]"
            return []
        if self.s.min_hours > 0 and self._recent():
            self.note = f"tried less than {self.s.min_hours:g} h ago"
            return []
        try:
            with T.one_round(self.db_path):
                # asked again inside the lock: a round that just ended has kept its rows
                if self.s.min_hours > 0 and self._recent():
                    self.note = f"tried less than {self.s.min_hours:g} h ago"
                    return []
                results = T.try_all(self.s)
                # kept here, still inside the lock, so the next run sees them
                conn = sqlite3.connect(str(self.db_path), timeout=30)
                try:
                    T.store(conn, results, self.s.programs)
                finally:
                    conn.close()
        except T.Busy:
            self.note = "another round of tries is going on"
            return []
        reached = sum(1 for r in results if r["reached"])
        self.note = f"{reached} of {len(results)} machine(s) answered"
        return results

    def store(self, data: list[dict[str, Any]]) -> None:
        """Kept already, in collect (inside the round's lock)."""
        return None


__all__ = ["LicenseTriesCollector"]

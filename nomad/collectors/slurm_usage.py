# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Slurm's monthly usage totals, kept month by month.

Slurm deletes job records after a while (PurgeJobAfter), but its monthly
usage rollups are often kept for good (PurgeUsageAfter=NONE): how many
core-hours (and GPU-hours, where GPUs are accounted) were allocated, idle,
planned for waiting jobs, down, and available, every month since the cluster
began. That is the only record of load from before nomad, and the long view a
capacity plan needs.

    sreport -P -t hours -T cpu,gres/gpu cluster utilization start=M end=M+1

One run asks about at most ``months_per_run`` months (newest first): this
month and last month again every ``refresh_hours`` until a month is over and
settled, and further back until the cluster's start (Slurm has nothing before
it: three empty months in a row, confirmed by one question about everything
before them) or ``since``. Which months were asked, with which TRES, when, and
whether Slurm had anything, is kept in cluster_usage_asked; cluster_usage holds
only what Slurm reported. Shares of time follow Slurm's own definitions:
allocated, idle and planned are parts of the time nodes were up (reported -
down); down is part of all reported time. Rows are per cluster, month and
TRES: cpu rows and gres/gpu rows are never to be added together.
"""
from __future__ import annotations

import logging
import subprocess
from datetime import date, datetime, timedelta
from typing import Any

from .base import BaseCollector, CollectionError, MissingToolError, find_tool, registry

logger = logging.getLogger(__name__)

TRES = "cpu,gres/gpu"
REFRESH_HOURS = 6.0
MONTHS_PER_RUN = 24
# Months in a row with nothing reported that may mark the cluster's start.
EMPTY_MONTHS_TO_STOP = 3
# A month is settled once this long after its end (Slurm rolls usage up hourly
# and daily; a late rollup is redone within a day).
SETTLED_AFTER = timedelta(days=2)
EARLIEST = date(2000, 1, 1)
_NOTHING_BEFORE = "slurm_usage.nothing_before"
_CPU_ONLY = "slurm_usage.cpu_only"

# sreport's column names (lower case) -> ours. "Reserved" was renamed
# "Planned" in newer Slurm; "Over Comm" (old versions) is not kept.
_COLUMNS = {
    "cluster": "cluster", "tres name": "tres", "allocated": "allocated_h",
    "down": "down_h", "plnd down": "planned_down_h", "idle": "idle_h",
    "planned": "planned_h", "reserved": "planned_h", "reported": "reported_h",
}
_HOURS = ("allocated_h", "down_h", "planned_down_h", "idle_h", "planned_h", "reported_h")


def month_start(d: date) -> date:
    return date(d.year, d.month, 1)


def next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def previous_month(d: date) -> date:
    return date(d.year - (d.month == 1), (d.month - 2) % 12 + 1, 1)


def parse_sreport(text: str) -> list[dict[str, Any]]:
    """Rows of `sreport -P ... cluster utilization` output (with its header)."""
    rows, header = [], None
    for line in text.splitlines():
        if "|" not in line:
            continue
        fields = [f.strip() for f in line.split("|")]
        if header is None:
            if fields[0].lower() == "cluster":
                header = [_COLUMNS.get(f.lower()) for f in fields]
            continue
        rec: dict[str, Any] = {}
        for name, value in zip(header, fields):
            if name is None:
                continue
            if name in _HOURS:
                try:
                    rec[name] = float(value.replace(",", "")) if value else 0.0
                except ValueError:
                    rec[name] = None
            else:
                rec[name] = value
        if rec.get("cluster") and rec.get("tres"):
            for h in _HOURS:
                rec.setdefault(h, 0.0)
            rows.append(rec)
    return rows


def _tres_error(message: str) -> bool:
    """Whether an sreport error is about the TRES asked for (as opposed to,
    say, slurmdbd not answering)."""
    m = message.lower()
    return "tres" in m or "gres" in m


@registry.register
class SlurmUsageCollector(BaseCollector):
    """Monthly usage totals from sreport, in table cluster_usage.

    Configuration ([collectors.slurm_usage]; it runs where the slurm collector
    does unless enabled is set here):
        refresh_hours:  how often this and last month are asked again (6)
        months_per_run: at most this many months asked per run (24)
        since:          "YYYY-MM", the first month wanted (default: back to
                        the cluster's start)
        tres:           TRES asked for ("cpu,gres/gpu")
    A setting that can't be read is warned about and its default used.
    """

    name = "slurm_usage"
    description = "Slurm monthly usage totals (sreport)"
    default_interval = 3600

    def __init__(self, config: dict[str, Any], db_path: str):
        super().__init__(config, db_path)
        self.refresh_hours = self._setting(config, "refresh_hours", REFRESH_HOURS, float)
        self.months_per_run = max(1, self._setting(config, "months_per_run", MONTHS_PER_RUN, int))
        self.since = self._setting(
            config, "since", None,
            lambda v: month_start(datetime.strptime(str(v)[:7], "%Y-%m").date()))
        self.tres = str(config.get("tres") or TRES)

    @staticmethod
    def _setting(config, key, default, convert):
        value = config.get(key)
        if value is None or value == "":
            return default
        try:
            return convert(value)
        except (TypeError, ValueError):
            logger.warning(f"slurm_usage: {key} = {value!r} can't be read; using {default!r}")
            return default

    # -- schema ---------------------------------------------------------------------

    @staticmethod
    def ensure_schema(conn) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cluster_usage (
                cluster        TEXT NOT NULL,
                month          TEXT NOT NULL,   -- YYYY-MM
                tres           TEXT NOT NULL,   -- cpu, gres/gpu, ...
                allocated_h    REAL,
                down_h         REAL,
                planned_down_h REAL,
                idle_h         REAL,
                planned_h      REAL,
                reported_h     REAL,
                settled        INTEGER NOT NULL DEFAULT 0,
                collected_at   TEXT NOT NULL,
                PRIMARY KEY (cluster, month, tres)
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cluster_usage_asked (
                month        TEXT PRIMARY KEY,  -- YYYY-MM
                tres         TEXT NOT NULL,     -- what it was asked for
                settled      INTEGER NOT NULL,  -- asked after the month was over and settled
                reported_h   REAL NOT NULL,     -- cpu hours reported, all clusters (0: nothing)
                asked_at     TEXT NOT NULL
            )""")
        conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, "
                     "value TEXT NOT NULL, updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)")

    @staticmethod
    def _get(conn, key):
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _put(conn, key, value):
        conn.execute("INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, ?)",
                     (key, value, datetime.now().isoformat(timespec="seconds")))

    def _effective_tres(self, conn) -> str:
        return "cpu" if self._get(conn, _CPU_ONLY) == self.tres else self.tres

    # -- which months -------------------------------------------------------------------

    def _months_to_ask(self, conn, today: date, tres: str) -> list[date]:
        """Newest first: this and last month when due again, then the rest."""
        asked = {m: (t, s, at) for m, t, s, at in conn.execute(
            "SELECT month, tres, settled, asked_at FROM cluster_usage_asked")}
        floor = self._get(conn, _NOTHING_BEFORE)
        floor = datetime.strptime(floor, "%Y-%m").date() if floor else EARLIEST
        if self.since and self.since > floor:
            floor = self.since
        due_at = (datetime.now() - timedelta(hours=self.refresh_hours)).isoformat()

        def due(m: date) -> bool:
            a = asked.get(m.strftime("%Y-%m"))
            if a is None or a[0] != tres:          # never, or not for these TRES
                return True
            return not a[1] and a[2] < due_at     # not settled, and asked long enough ago

        out: list[date] = []
        m = month_start(today)
        while m >= floor and len(out) < self.months_per_run:
            if due(m):
                out.append(m)
            m = previous_month(m)
        return out

    # -- sreport ------------------------------------------------------------------------

    def _sreport(self, start: date, end: date, tres: str) -> list[dict[str, Any]]:
        cmd = ["sreport", "-P", "-t", "hours", "-T", tres, "cluster", "utilization",
               f"start={start.isoformat()}", f"end={end.isoformat()}"]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise CollectionError(f"sreport failed: {(result.stderr or '').strip()[:200]}")
        return parse_sreport(result.stdout)

    def collect(self) -> list[dict[str, Any]]:
        if not find_tool("sreport"):
            raise MissingToolError("sreport not installed")
        today = date.today()
        with self.get_db_connection() as conn:
            self.ensure_schema(conn)
            conn.commit()
            tres = self._effective_tres(conn)
            months = self._months_to_ask(conn, today, tres)
        if not months:
            self.note = "up to date"
            return []

        data: list[dict[str, Any]] = []
        now = datetime.now()
        asked = 0
        for m in months:
            try:
                rows = self._sreport(m, next_month(m), tres)
            except CollectionError as e:
                if asked == 0 and "," in tres and _tres_error(str(e)):
                    # A Slurm that doesn't account one of these TRES: cpu alone,
                    # remembered so the months asked this way stay done.
                    logger.warning(f"slurm_usage: {e}; asking for cpu only")
                    tres = "cpu"
                    data.append({"type": "cpu_only", "tres": self.tres})
                    rows = self._sreport(m, next_month(m), tres)
                elif asked == 0:
                    raise
                else:
                    self.note = f"stopped after {asked} months: {e}"
                    break
            except subprocess.TimeoutExpired:
                # Not retried (each try would wait another minute): next run.
                self.note = (f"stopped after {asked} months: sreport timed out" if asked
                             else "sreport timed out; next run tries again")
                break
            asked += 1
            settled = int(now >= datetime.combine(next_month(m), datetime.min.time())
                          + SETTLED_AFTER)
            key = m.strftime("%Y-%m")
            cpu = sum(r["reported_h"] or 0 for r in rows if r["tres"] == "cpu")
            data.append({"type": "asked", "month": key, "tres": tres, "settled": settled,
                         "reported_h": cpu, "asked_at": now.isoformat(timespec="seconds")})
            for r in rows:
                data.append({"type": "cluster_usage", "month": key, "settled": settled,
                             "collected_at": now.isoformat(timespec="seconds"), **r})
        return data

    def count_records(self, data: list[dict[str, Any]]) -> int:
        return sum(1 for r in data if r.get("type") == "cluster_usage")

    def store(self, data: list[dict[str, Any]]) -> None:
        with self.get_db_connection() as conn:
            self.ensure_schema(conn)
            for r in data:
                kind = r.get("type")
                if kind == "cpu_only":
                    self._put(conn, _CPU_ONLY, r["tres"])
                elif kind == "asked":
                    conn.execute(
                        "INSERT OR REPLACE INTO cluster_usage_asked (month, tres, settled, "
                        "reported_h, asked_at) VALUES (?, ?, ?, ?, ?)",
                        (r["month"], r["tres"], r["settled"], r["reported_h"], r["asked_at"]))
                elif kind == "cluster_usage":
                    conn.execute(
                        "INSERT OR REPLACE INTO cluster_usage (cluster, month, tres, "
                        "allocated_h, down_h, planned_down_h, idle_h, planned_h, reported_h, "
                        "settled, collected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (r["cluster"], r["month"], r["tres"], r["allocated_h"], r["down_h"],
                         r["planned_down_h"], r["idle_h"], r["planned_h"], r["reported_h"],
                         r["settled"], r["collected_at"]))
            conn.commit()
            try:
                self._find_the_start(conn)
            except (CollectionError, subprocess.TimeoutExpired, OSError) as e:
                logger.warning(f"slurm_usage: could not check where the cluster starts: {e}")
            conn.commit()

    def _find_the_start(self, conn) -> None:
        """Where the cluster starts: below the earliest month with reported hours,
        EMPTY_MONTHS_TO_STOP months in a row with nothing, and nothing at all
        reported before them (one sreport question about everything earlier:
        a long outage in a cluster's life is not its start)."""
        if self._get(conn, _NOTHING_BEFORE):
            return
        asked = {m: rep for m, rep in conn.execute(
            "SELECT month, reported_h FROM cluster_usage_asked")}
        with_data = sorted(m for m, rep in asked.items() if rep > 0)
        if not with_data:
            return
        m = previous_month(datetime.strptime(with_data[0], "%Y-%m").date())
        empty = 0
        while asked.get(m.strftime("%Y-%m")) == 0:
            empty += 1
            if empty >= EMPTY_MONTHS_TO_STOP:
                earlier = self._sreport(EARLIEST, m, "cpu")
                if not any((r["reported_h"] or 0) > 0 for r in earlier):
                    self._put(conn, _NOTHING_BEFORE, m.strftime("%Y-%m"))
                return
            m = previous_month(m)

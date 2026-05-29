# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
EnergyEngine — orchestrator for the NØMAD energy module.

Loads job-energy rows and DCGM GPU energy once, then serves the cluster
summary, per-group/per-user breakdowns, and carbon view from that single
pass. Provides unified CLI and JSON output, mirroring DynamicsEngine.

Usage:
    engine = EnergyEngine(db_path, hours=168, cluster_name="spydur",
                          config=cfg, mode="physical")
    print(engine.full_summary())
    print(engine.report(group_by="partition"))
    print(engine.user_profile("jdoe"))
    data = engine.to_json()
"""
from __future__ import annotations

import sqlite3

from . import formatters as fmt
from .power import (
    EnergySnapshot, aggregate, iter_job_energy, gpu_energy_from_dcgm, _window,
    compute_energy, _parse_dt,
)
import sqlite3 as _sqlite3
from datetime import datetime as _datetime
from .waste import JobEnergyRow, MODE_PHYSICAL


class EnergyEngine:
    """Main entry point for the NØMAD energy module."""

    def __init__(
        self,
        db_path: str,
        hours: int = 168,
        cluster_name: str = "cluster",
        config: dict | None = None,
        region: str | None = None,
        mode: str = MODE_PHYSICAL,
    ):
        self.db_path = str(db_path)
        self.hours = hours
        self.cluster_name = cluster_name
        self.config = config or {}
        self.region = region
        self.mode = mode

        # Loaded lazily, once.
        self._loaded = False
        self._rows: list[JobEnergyRow] = []
        self._cpu_wh = 0.0
        self._gpu_total_wh = 0.0
        self._gpu_idle_wh = 0.0
        self._dcgm = False
        self._window: tuple = ()
        self._snapshot: EnergySnapshot | None = None

    # ── data loading ────────────────────────────────────────────────────
    def _load(self) -> None:
        if self._loaded:
            return
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            self._window = _window(conn, self.hours)
            self._rows, self._cpu_wh = iter_job_energy(
                conn, *self._window, self.config, self.cluster_name
            )
            self._gpu_total_wh, self._gpu_idle_wh, self._dcgm = \
                gpu_energy_from_dcgm(conn, *self._window)
        finally:
            conn.close()
        self._loaded = True

    def _aggregate(self, rows, cpu_wh, gpu_total, gpu_idle) -> EnergySnapshot:
        return aggregate(
            rows, cpu_wh, gpu_total, gpu_idle, self._dcgm,
            self.config, self._window, mode=self.mode, region_override=self.region,
        )

    # ── cluster snapshot ──────────────────────────────────────────────────
    def snapshot(self) -> EnergySnapshot:
        self._load()
        if self._snapshot is None:
            self._snapshot = self._aggregate(
                self._rows, self._cpu_wh, self._gpu_total_wh, self._gpu_idle_wh
            )
        return self._snapshot

    # ── breakdown by partition / group / user ─────────────────────────────
    def breakdown(self, group_by: str = "partition") -> dict[str, EnergySnapshot]:
        """Per-group snapshots. group_by in {partition, group, user}.

        GPU energy is reported at the cluster level only (per-job GPU
        attribution is not yet wired), so subset snapshots carry CPU energy
        and all waste sources; the per-group GPU column is zero until then.
        """
        self._load()
        keyfn = {
            "partition": lambda r: r.partition,
            "group": lambda r: r.account,
            "user": lambda r: r.user,
        }.get(group_by, lambda r: r.partition)

        buckets: dict[str, list[JobEnergyRow]] = {}
        for r in self._rows:
            buckets.setdefault(keyfn(r), []).append(r)

        out: dict[str, EnergySnapshot] = {}
        for key, rows in buckets.items():
            cpu_wh = sum(r.cpu_actual_wh for r in rows)
            out[key] = self._aggregate(rows, cpu_wh, 0.0, 0.0)
        return out

    # ── per-user profile + recommendations ────────────────────────────────
    def user_snapshot(self, username: str) -> EnergySnapshot:
        self._load()
        rows = [r for r in self._rows if r.user == username]
        cpu_wh = sum(r.cpu_actual_wh for r in rows)
        return self._aggregate(rows, cpu_wh, 0.0, 0.0)

    def recommendations(self, snap: EnergySnapshot) -> list[str]:
        """Non-punitive, opportunity-framed suggestions ranked by impact."""
        w = snap.waste
        recs: list[tuple[float, str]] = []
        over_h = w.over_request_seconds / 3600.0
        if over_h > 1:
            recs.append((w.over_request_seconds,
                f"Requesting wall time closer to actual runtime would free "
                f"~{over_h:,.0f} reserved hours and shorten everyone's queue."))
        if w.gpu_idle_wh > 0:
            recs.append((w.gpu_idle_wh,
                f"Some GPU time shows low real utilization "
                f"(~{w.gpu_idle_wh/1000:,.1f} kWh). CPU-only work runs more "
                f"efficiently on CPU partitions."))
        if w.cpu_underutil_wh > 0:
            recs.append((w.cpu_underutil_wh,
                f"Allocating cores nearer to what jobs use would recover "
                f"~{w.cpu_underutil_wh/1000:,.1f} kWh."))
        recs.sort(key=lambda t: t[0], reverse=True)
        return [msg for _, msg in recs]


    # ── before / after comparison ─────────────────────────────────────────
    def compare_periods(self, split=None, start=None, end=None):
        """Compute energy for two periods of one timeline (before vs after).

        The full window defaults to the data span (min start .. max end of
        jobs); `split` defaults to its midpoint. Returns (pre, post, split).
        For an intervention dataset, the midpoint split lands on the
        intervention by construction.
        """
        clause = "WHERE cluster = ?" if self.cluster_name else ""
        params = [self.cluster_name] if self.cluster_name else []
        conn = _sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                f"SELECT MIN(start_time), MAX(end_time) FROM jobs {clause}", params
            ).fetchone()
        finally:
            conn.close()
        full_start = start or _parse_dt(row[0]) if row else start
        full_end = end or _parse_dt(row[1]) if row else end
        if full_start is None or full_end is None:
            return None, None, None
        if split is None:
            split = full_start + (full_end - full_start) / 2

        def _snap(win):
            return compute_energy(self.db_path, self.config, cluster_name=self.cluster_name,
                                  mode=self.mode, region_override=self.region, window=win)
        return _snap((full_start, split)), _snap((split, full_end)), split

    def compare(self, split=None) -> str:
        pre, post, split = self.compare_periods(split=split)
        if pre is None:
            return "No jobs found to compare."
        return fmt.format_comparison_cli(pre, post, split, self.cluster_name, self.mode)

    # ── output ────────────────────────────────────────────────────────────
    def full_summary(self, explain: bool = False) -> str:
        return fmt.format_summary_cli(
            self.snapshot(), self.cluster_name, self.mode, explain=explain)

    def report(self, group_by: str = "partition") -> str:
        return fmt.format_report_cli(self.breakdown(group_by), group_by, self.mode)

    def user_profile(self, username: str, explain: bool = False) -> str:
        snap = self.user_snapshot(username)
        return fmt.format_user_cli(
            username, snap, self.recommendations(snap), self.mode, explain=explain)

    def carbon_report(self, explain: bool = False) -> str:
        # Carbon is the same snapshot viewed through its emissions; the
        # summary already leads with consumed/recoverable CO2.
        return self.full_summary(explain=explain)

    def to_json(self) -> str:
        return fmt.to_json_str(
            fmt.format_summary_json(self.snapshot(), self.cluster_name, self.mode))

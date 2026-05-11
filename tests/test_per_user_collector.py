# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
End-to-end tests for PerUserCollector.

We override iter_processes() to inject synthetic snapshots, then run multiple
ticks against a real SQLite DB (in tmp_path) to verify:
  - whitelisted processes get sample rows but no rule evaluation
  - sustained CPU misuse fires the configured rule
  - dedup: re-firing within cooldown bumps occurrences instead of inserting
  - severity tier flows through to the alert row
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nomad.collectors.per_user import (
    PerUserCollector,
    ProcessSnapshot,
)
from nomad.collectors.per_user.ancestry import ProcessInfo, WhitelistConfig
from nomad.collectors.per_user.rules import DEFAULT_RULES


GB = 1024 ** 3


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------




def make_snapshot(
    pid: int = 1234,
    username: str = "testuser",
    uid: int = 10001,
    command: str = "testcmd",
    exe_path: str = "/home/testuser/bin/testcmd",
    cpu_percent: float = 0.0,
    memory_rss: int = 100_000_000,
    started_at: float = 1000.0,
    ppid: int = 1,
) -> ProcessSnapshot:
    return ProcessSnapshot(
        info=ProcessInfo(
            pid=pid, ppid=ppid, uid=uid, username=username,
            command=command, exe_path=exe_path,
        ),
        cpu_percent=cpu_percent,
        memory_rss_bytes=memory_rss,
        memory_vms_bytes=memory_rss * 2,
        num_threads=1,
        num_fds=10,
        started_at=started_at,
        cmdline=command,
    )


class FakeCollector(PerUserCollector):
    """Test subclass that injects a fixed sequence of process lists."""

    def __init__(self, snapshots_per_tick: list[list[ProcessSnapshot]], **kwargs):
        super().__init__(**kwargs)
        self._snapshots_per_tick = snapshots_per_tick
        self._tick_idx = 0

    def iter_processes(self):
        snaps = self._snapshots_per_tick[min(self._tick_idx, len(self._snapshots_per_tick) - 1)]
        self._tick_idx += 1
        return iter(snaps)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_collector_persists_samples_to_db(db_path):
    snaps = [make_snapshot(cpu_percent=5.0, memory_rss=100_000_000)]
    config = {"role": "headnode"}
    collector = FakeCollector(
        snapshots_per_tick=[snaps],
        config=config,
        db_path=db_path,
    )
    collector.run()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT username, command, cpu_percent FROM per_user_sample").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "testuser"


def test_whitelisted_process_records_sample_but_does_not_fire(db_path):
    """arobbins's clusterbackup case: parent path matches /usr/local/sw/."""
    snap = make_snapshot(
        username="arobbins",
        uid=10500,
        command="clusterbackup.py",
        exe_path="/usr/local/sw/clusterbackup/clusterbackup.py",
        cpu_percent=95.0,                  # would normally fire
        memory_rss=100_000_000,
    )
    config = {
        "role": "headnode",
        "whitelist": {
            "parent_paths": ["/usr/local/sw/"],
            "min_uid": 1000,
        },
    }
    # 6 ticks of high CPU — would definitely fire if not whitelisted
    snaps = [[snap]] * 6
    collector = FakeCollector(
        snapshots_per_tick=snaps,
        config=config,
        db_path=db_path,
    )
    for _ in range(6):
        collector.run()

    with sqlite3.connect(db_path) as conn:
        sample_count = conn.execute("SELECT COUNT(*) FROM per_user_sample").fetchone()[0]
        alert_count = conn.execute("SELECT COUNT(*) FROM per_user_alert").fetchone()[0]
        whitelisted = conn.execute(
            "SELECT whitelist_match FROM per_user_sample LIMIT 1"
        ).fetchone()[0]
    assert sample_count == 6
    assert alert_count == 0                    # whitelist suppressed all firings
    assert whitelisted == "parent_path:/usr/local/sw/"


def test_sustained_high_cpu_fires_alert(db_path):
    """Synthetic ia3nk: 95% CPU sustained across enough samples to fire 5min rule."""
    # 7 ticks of 95% CPU — the rule needs 5 minutes (5+ samples)
    snaps = [[make_snapshot(cpu_percent=95.0)] for _ in range(7)]
    config = {"role": "headnode"}
    collector = FakeCollector(
        snapshots_per_tick=snaps,
        config=config,
        db_path=db_path,
    )
    # We need to advance time between ticks. Override the time source by
    # directly manipulating sample timestamps via the engine. The simplest
    # path: run all ticks back-to-back and rely on real wall-clock — but
    # that's slow. Instead we monkey-patch time.time inside the test scope.
    import time as time_module
    real_time = time_module.time
    fake_now = [real_time()]

    def fake_time():
        return fake_now[0]

    # Patch time in the collector's modules
    from nomad.collectors.per_user import collector as collector_mod
    from nomad.collectors.per_user import rules as rules_mod
    collector_mod.time.time = fake_time
    rules_mod.time.time = fake_time

    try:
        for _ in range(7):
            collector.run()
            fake_now[0] += 60                   # advance 60s
    finally:
        collector_mod.time.time = real_time
        rules_mod.time.time = real_time

    with sqlite3.connect(db_path) as conn:
        alerts = conn.execute(
            "SELECT rule_id, severity, occurrences FROM per_user_alert"
        ).fetchall()

    fired_rules = {a[0] for a in alerts}
    assert "cpu_10pct_5min" in fired_rules
    assert "cpu_50pct_2min" in fired_rules


def test_alert_dedup_increments_occurrences(db_path):
    """Re-firing the same rule on the same session should bump occurrences."""
    snaps = [[make_snapshot(cpu_percent=95.0)] for _ in range(20)]
    config = {"role": "headnode"}
    collector = FakeCollector(
        snapshots_per_tick=snaps,
        config=config,
        db_path=db_path,
    )
    # Override cooldown to 5 minutes (default is 1 hour)
    collector.engine.cooldown_seconds = 300

    import time as time_module
    real_time = time_module.time
    fake_now = [real_time()]

    def fake_time():
        return fake_now[0]

    from nomad.collectors.per_user import collector as collector_mod
    from nomad.collectors.per_user import rules as rules_mod
    collector_mod.time.time = fake_time
    rules_mod.time.time = fake_time

    try:
        for _ in range(20):                      # 20 minutes of sustained 95%
            collector.run()
            fake_now[0] += 60
    finally:
        collector_mod.time.time = real_time
        rules_mod.time.time = real_time

    with sqlite3.connect(db_path) as conn:
        # Should have 2 alert rows (cpu_10pct_5min, cpu_50pct_2min) with
        # occurrences > 1 each (because cooldown expired during the 20 min run)
        rows = conn.execute(
            "SELECT rule_id, occurrences FROM per_user_alert ORDER BY rule_id"
        ).fetchall()
    by_rule = dict(rows)
    assert "cpu_10pct_5min" in by_rule
    assert by_rule["cpu_10pct_5min"] >= 2        # at least one re-firing


def test_severity_tier_recorded_in_alert(db_path):
    """Memory_4gb_10min is 'informational' — verify it lands in the alert as such."""
    snap = make_snapshot(cpu_percent=5.0, memory_rss=5 * GB)
    snaps = [[snap]] * 12                        # 12 ticks * 60s = 12 min sustained
    config = {"role": "headnode"}
    collector = FakeCollector(
        snapshots_per_tick=snaps,
        config=config,
        db_path=db_path,
    )

    import time as time_module
    real_time = time_module.time
    fake_now = [real_time()]

    def fake_time():
        return fake_now[0]

    from nomad.collectors.per_user import collector as collector_mod
    from nomad.collectors.per_user import rules as rules_mod
    collector_mod.time.time = fake_time
    rules_mod.time.time = fake_time

    try:
        for _ in range(12):
            collector.run()
            fake_now[0] += 60
    finally:
        collector_mod.time.time = real_time
        rules_mod.time.time = real_time

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT rule_id, severity FROM per_user_alert ORDER BY rule_id"
        ).fetchall()
    by_rule = dict(rows)
    assert by_rule.get("memory_4gb_10min") == "informational"


def test_collector_disabled_returns_early(db_path):
    snap = make_snapshot(cpu_percent=99.0, memory_rss=100 * GB)
    config = {"enabled": False}
    collector = FakeCollector(
        snapshots_per_tick=[[snap]],
        config=config,
        db_path=db_path,
    )
    collector.run()
    with sqlite3.connect(db_path) as conn:
        n = conn.execute("SELECT COUNT(*) FROM per_user_sample").fetchone()[0]
    assert n == 0


def test_track_store_evicts_when_process_exits(db_path):
    """Tick 1 has process A; tick 2 doesn't. A's track should be evicted."""
    snap_a = make_snapshot(pid=100, command="A", started_at=1000.0)
    snap_b = make_snapshot(pid=200, command="B", started_at=1010.0)
    config = {"role": "headnode"}
    collector = FakeCollector(
        snapshots_per_tick=[[snap_a, snap_b], [snap_b]],
        config=config,
        db_path=db_path,
    )
    collector.run()
    assert len(collector.tracks) == 2
    collector.run()
    assert len(collector.tracks) == 1

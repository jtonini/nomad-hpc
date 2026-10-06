# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The workstation diagnostic reads the table the collector writes.

It used to read load_1m, memory_percent and disk_percent, which no table has:
every workstation showed 0% load, memory and disk, and nothing was flagged.
"""
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta

import pytest

from nomad.collectors import workstation as ws
from nomad.collectors.workstation import WorkstationCollector, WorkstationStats
from nomad.diag.workstation import diagnose_workstation, reading


def _store(db, **fields):
    """One reading, written by the collector itself (its own schema)."""
    stats = WorkstationStats(hostname=fields.pop("hostname", "labws1"), department="pi1$")
    for k, v in fields.items():
        setattr(stats, k, v)
    record = stats.to_dict()
    record["status"] = "online"
    WorkstationCollector({}, str(db)).store([record])


def _healthy(**over):
    base = dict(uptime_seconds=86400, load_avg_1m=1.5, cpu_count=8,
                memory_total_mb=32000, memory_used_mb=8000,
                disk_total_gb=500.0, disk_used_gb=200.0, disk_usage_pct=40.0,
                users_logged_in=2, process_count=300, zombie_count=0)
    base.update(over)
    return base


def _causes(diag):
    return [c["cause"] for c in diag.potential_causes]


def test_the_figures_are_the_collectors(tmp_path):
    db = tmp_path / "nomad.db"
    _store(db, **_healthy())
    diag = diagnose_workstation(str(db), "labws1")
    assert diag.cpu_load == 1.5 and diag.cpu_count == 8
    assert diag.memory_used_pct == pytest.approx(25.0)
    assert diag.memory_total_mb == 32000
    assert diag.disk_used_pct == pytest.approx(40.0)
    assert diag.users_logged_in == 2 and diag.process_count == 300
    assert _causes(diag) == ["No obvious issues detected"]


@pytest.mark.parametrize("over, cause", [
    (dict(memory_used_mb=31000), "Critical Memory Pressure"),
    (dict(memory_used_mb=28000), "High Memory Usage"),
    (dict(disk_usage_pct=97.0, disk_used_gb=485.0), "Disk Almost Full"),
    (dict(disk_usage_pct=88.0, disk_used_gb=440.0), "High Disk Usage"),
    (dict(load_avg_1m=20.0), "CPU Overload"),
    (dict(load_avg_1m=10.0), "High CPU Load"),
    (dict(swap_used_mb=4096), "Heavy Swap Usage"),
])
def test_a_busy_or_full_workstation_is_flagged(tmp_path, over, cause):
    db = tmp_path / "nomad.db"
    _store(db, **_healthy(**over))
    assert cause in _causes(diagnose_workstation(str(db), "labws1"))


def test_figures_a_reading_lacks_are_said_to_be_missing_not_zero(tmp_path):
    # What the collector stores when /proc/meminfo, df and uptime all failed.
    db = tmp_path / "nomad.db"
    _store(db, **_healthy(uptime_seconds=0, load_avg_1m=0.0, memory_total_mb=0,
                          memory_used_mb=0, disk_total_gb=0.0, disk_used_gb=0.0,
                          disk_usage_pct=0.0))
    diag = diagnose_workstation(str(db), "labws1")
    causes = {c["cause"]: c["detail"] for c in diag.potential_causes}
    assert "No obvious issues detected" not in causes
    assert "load, memory or disk" in causes["Incomplete Reading"]
    assert not any("appears healthy" in r for r in diag.recommendations)
    assert "nomad diag workstation labws1 --prereqs" in " ".join(diag.recommendations)


def test_an_idle_machine_with_its_uptime_is_a_load_of_zero(tmp_path):
    db = tmp_path / "nomad.db"
    _store(db, **_healthy(load_avg_1m=0.0))
    diag = diagnose_workstation(str(db), "labws1")
    assert "Incomplete Reading" not in _causes(diag)


def test_an_old_reading_is_not_the_present(tmp_path):
    db = tmp_path / "nomad.db"
    _store(db, **_healthy())
    old = (datetime.now() - timedelta(hours=5)).isoformat()
    with sqlite3.connect(db) as c:
        c.execute("UPDATE workstation_state SET timestamp = ?", (old,))
    diag = diagnose_workstation(str(db), "labws1", hours=24)
    causes = {c["cause"]: c["detail"] for c in diag.potential_causes}
    assert "5.0 h ago" in causes["Workstation not reporting"]


def test_the_history_is_read_and_summed_up_without_trend_alarms(tmp_path):
    # Memory rising a little at each reading: as three five-minute points this
    # read as "accelerating" (a memory leak). The diagnostic doesn't judge
    # trends from that.
    db = tmp_path / "nomad.db"
    for used in (8000, 8100, 8400, 9000):
        _store(db, **_healthy(memory_used_mb=used))
    now = datetime.now()
    with sqlite3.connect(db) as c:
        ids = [r[0] for r in c.execute("SELECT id FROM workstation_state ORDER BY id")]
        for n, i in enumerate(ids):
            when = now - timedelta(minutes=5 * (len(ids) - 1 - n))
            c.execute("UPDATE workstation_state SET timestamp = ? WHERE id = ?",
                      (when.isoformat(), i))
    diag = diagnose_workstation(str(db), "labws1")
    assert diag.resource_history["samples"] == 4
    assert diag.resource_history["avg_mem_pct"] == pytest.approx(
        sum(u / 32000 * 100 for u in (8000, 8100, 8400, 9000)) / 4)
    assert diag.memory_used_pct == pytest.approx(9000 / 32000 * 100)
    assert diag.trends == {}
    assert "Memory Usage Accelerating" not in _causes(diag)


def test_reading_keeps_older_column_names():
    r = reading({"load_1m": 2.0, "memory_percent": 50.0, "disk_percent": 70.0,
                 "cpu_count": 4})
    assert (r["load"], r["mem_pct"], r["disk_pct"]) == (2.0, 50.0, 70.0)


def test_the_zombie_command_counts_zombies_only():
    # A child that has exited and not been waited for is a zombie.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        for _ in range(100):
            with open(f"/proc/{child.pid}/stat") as f:
                if f.read().split(") ", 1)[1].startswith("Z"):
                    break
            time.sleep(0.02)
        cmd = "ps -A -o stat= | grep -c Z"
        n = int(subprocess.run(["bash", "-c", cmd], capture_output=True, text=True).stdout)
        real = sum(1 for pid in os.listdir("/proc") if pid.isdigit()
                   and _state(pid) == "Z")
        assert n == real >= 1
    finally:
        child.wait()


def _state(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ", 1)[1][0]
    except OSError:
        return None


def test_a_quoted_remote_command_arrives_as_written(tmp_path, monkeypatch):
    # A stand-in for ssh: like ssh, it joins the words after the host and has
    # a shell run them.
    fake = tmp_path / "ssh"
    fake.write_text('#!/bin/sh\nwhile [ "$1" = -o ]; do shift 2; done\nshift\n'
                    'exec bash -c "$*"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    assert ws.run_command("printf '%s|' 'a  b' c", "labws1") == "a  b|c|"

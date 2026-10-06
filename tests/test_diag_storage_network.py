# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Storage and network diagnostics say only what their readings show.

A storage server read for an hour was called "filling rapidly" (HIGH, full in
122.9 days) from three readings minutes apart; a path that is only pinged
showed throughput 0 Mbps as if measured.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest

from nomad.diag.network import diagnose_network
from nomad.diag.storage import diagnose_storage

TB = 1024 ** 4
GB = 1024 ** 3


def _storage_db(tmp_path, readings):
    """readings: (timestamp, used_bytes) for one server of 32 TB."""
    db = tmp_path / "nomad.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE storage_state (id INTEGER PRIMARY KEY, timestamp TEXT, "
                  "hostname TEXT, storage_type TEXT, status TEXT, total_bytes INTEGER, "
                  "used_bytes INTEGER, free_bytes INTEGER, usage_pct REAL, pools_json TEXT)")
        for when, used in readings:
            c.execute("INSERT INTO storage_state (timestamp, hostname, storage_type, status, "
                      "total_bytes, used_bytes, free_bytes, usage_pct) VALUES (?,?,?,?,?,?,?,?)",
                      (when.isoformat(), "nas1", "zfs", "online", 32 * TB, int(used),
                       int(32 * TB - used), used / (32 * TB) * 100))
    return str(db)


def _causes(diag):
    return {c["cause"]: c for c in diag.potential_causes}


def test_an_hour_of_readings_is_no_fill_rate(tmp_path):
    now = datetime.now()
    # Rising a little at each reading, five minutes apart.
    rows = [(now - timedelta(minutes=5 * (12 - i)), 0.3 * TB + i * 2 * GB) for i in range(13)]
    diag = diagnose_storage(_storage_db(tmp_path, rows), "nas1")
    assert list(_causes(diag)) == ["No obvious issues detected"]
    assert diag.trends["usage"] == {}
    assert diag.recommendations == ["Storage appears healthy - no action required"]


@pytest.mark.parametrize("per_day, cause, confidence", [
    (55 * GB, None, None),                              # full in ~3 months
    (240 * GB, "Storage Filling", "low"),               # ~ 3 weeks
    (700 * GB, "Storage Filling", "medium"),            # ~ a week
    (2560 * GB, "Storage Filling Fast", "high"),        # ~ 2 days
])
def test_a_week_or_more_of_growth_is_judged_by_days_to_full(tmp_path, per_day, cause,
                                                             confidence):
    now = datetime.now()
    start = 32 * TB - 5 * TB - 10 * per_day            # 5 TB free at the end (84%)
    rows = [(now - timedelta(days=10 - d), start + d * per_day) for d in range(11)]
    diag = diagnose_storage(_storage_db(tmp_path, rows), "nas1")
    causes = _causes(diag)
    usage = diag.trends["usage"]
    assert usage["trend_days"] == 11
    assert usage["first_derivative"] == pytest.approx(per_day, rel=1e-6)
    assert usage["days_until_full"] == pytest.approx(5 * TB / per_day, rel=1e-6)
    if cause is None:
        assert list(causes) == ["No obvious issues detected"]
    else:
        assert causes[cause]["confidence"] == confidence
        assert "full in" in causes[cause]["detail"]
        assert any("zfs list" in r for r in diag.recommendations)
        assert "Storage appears healthy - no action required" not in diag.recommendations


def test_shrinking_storage_is_not_filling(tmp_path):
    now = datetime.now()
    rows = [(now - timedelta(days=10 - d), 2 * TB - d * 50 * GB) for d in range(11)]
    diag = diagnose_storage(_storage_db(tmp_path, rows), "nas1")
    assert diag.trends["usage"]["trend"] == "decreasing"
    assert diag.trends["usage"]["days_until_full"] is None
    assert list(_causes(diag)) == ["No obvious issues detected"]


def _net_db(tmp_path, rows):
    """rows: (timestamp, ping_avg_ms, throughput_mbps, tcp_retrans)."""
    db = tmp_path / "nomad.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE network_perf (id INTEGER PRIMARY KEY, timestamp TEXT, "
                  "source_host TEXT, dest_host TEXT, path_type TEXT, status TEXT, "
                  "ping_min_ms REAL, ping_avg_ms REAL, ping_max_ms REAL, ping_mdev_ms REAL, "
                  "ping_loss_pct REAL, throughput_mbps REAL, bytes_transferred INTEGER, "
                  "tcp_retrans INTEGER)")
        for when, ping, mbps, retrans in rows:
            c.execute("INSERT INTO network_perf (timestamp, source_host, dest_host, path_type, "
                      "status, ping_avg_ms, ping_mdev_ms, ping_loss_pct, throughput_mbps, "
                      "tcp_retrans) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (when.isoformat(), "labws1", "nas1", "nfs", "healthy", ping, 0.05, 0.0,
                       mbps, retrans))
    return str(db)


def test_a_path_only_pinged_says_throughput_is_not_measured(tmp_path):
    now = datetime.now()
    db = _net_db(tmp_path, [(now - timedelta(minutes=5 * i), 0.3, None, None) for i in range(6)])
    diag = diagnose_network(db, "labws1", "nas1")
    causes = _causes(diag)
    assert causes["No obvious issues detected"]["detail"] == "Latency and packet loss are normal"
    assert "not measured" in causes["Throughput Not Measured"]["detail"]
    assert diag.recommendations[0] == "Network appears healthy - no action required"
    assert "iperf3 -s running on nas1" in diag.recommendations[1]


def test_the_last_measured_throughput_is_shown_after_ping_only_rows(tmp_path):
    now = datetime.now()
    db = _net_db(tmp_path, [(now - timedelta(minutes=50), 0.3, 940.0, 3),
                            (now - timedelta(minutes=5), 0.3, None, None)])
    diag = diagnose_network(db, "labws1", "nas1")
    assert diag.throughput_mbps == 940.0 and diag.tcp_retrans == 3
    assert "Throughput Not Measured" not in _causes(diag)
    assert diag.recommendations == ["Network appears healthy - no action required"]

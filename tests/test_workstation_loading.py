# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Tests for workstation session loading and scoring in nomad.edu.progress."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

import pytest

from nomad.edu.progress import (
    _load_user_sessions,
    _score_sessions,
    _split_session_fields,
)
from nomad.edu.scoring import SessionFingerprint


# ── Fixtures ─────────────────────────────────────────────────────────

def _make_db(tmp_path):
    """Build a minimal combined.db schema for loader tests."""
    db = tmp_path / "combined.db"
    conn = sqlite3.connect(str(db))
    conn.executescript("""
        CREATE TABLE workstation_user_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME, hostname TEXT, username TEXT,
            uid INTEGER, session_epoch INTEGER,
            cpu_usage_usec INTEGER, cpu_user_usec INTEGER,
            cpu_system_usec INTEGER,
            memory_current_bytes INTEGER, memory_peak_bytes INTEGER,
            io_read_bytes INTEGER, io_write_bytes INTEGER,
            pids_current INTEGER, collector_version TEXT, source TEXT
        );
        CREATE TABLE workstation_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp DATETIME, hostname TEXT, department TEXT,
            status TEXT, memory_total_mb INTEGER, cpu_count INTEGER
        );
    """)
    conn.commit()
    return db, conn


def _insert_session(conn, *, hostname, username, uid, session_epoch,
                    peak_gb, span_hours, samples=None,
                    end_offset_hours=1.0):
    """Insert a synthetic session: first sample + final sample (with peak)."""
    end = datetime.now() - timedelta(hours=end_offset_hours)
    start = end - timedelta(hours=span_hours)
    peak_bytes = int(peak_gb * 1024 * 1024 * 1024)
    if samples is None:
        samples = max(2, int(span_hours * 60))
    for i in range(samples):
        frac = i / max(1, samples - 1)
        ts = (start + (end - start) * frac).isoformat()
        cur_peak = int(peak_bytes * (0.1 + 0.9 * frac))
        conn.execute("""
            INSERT INTO workstation_user_snapshot
              (timestamp, hostname, username, uid, session_epoch,
               cpu_usage_usec, memory_peak_bytes, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'cgroup_v2')
        """, (ts, hostname, username, uid, session_epoch,
              i * 1_000_000, cur_peak))


def _insert_host(conn, *, hostname, memory_total_mb, cpu_count):
    ts = datetime.now().isoformat()
    conn.execute("""
        INSERT INTO workstation_state
          (timestamp, hostname, department, status,
           memory_total_mb, cpu_count)
        VALUES (?, ?, 'parish-lab', 'online', ?, ?)
    """, (ts, hostname, memory_total_mb, cpu_count))


# ── Loader: shape & filtering ────────────────────────────────────────

def test_load_returns_session_dicts(tmp_path):
    """Loader returns one dict per (host, user, session_epoch) with
    aggregated fields."""
    db, conn = _make_db(tmp_path)
    _insert_host(conn, hostname="boyi", memory_total_mb=256_904,
                 cpu_count=64)
    _insert_session(conn, hostname="boyi", username="kbui", uid=310395,
                    session_epoch=1, peak_gb=253.0, span_hours=34.3)
    conn.commit()
    conn.close()

    rows = _load_user_sessions(str(db), "kbui", days=7)
    assert len(rows) == 1
    row = rows[0]
    assert row["hostname"] == "boyi"
    assert row["username"] == "kbui"
    assert row["session_epoch"] == 1
    assert row["peak_memory_bytes"] > 250 * 1024**3
    assert 33 <= row["span_hours"] <= 35
    assert row["host_memory_total_mb"] == 256_904
    assert row["host_cpu_count"] == 64
    assert row["samples"] >= 2


def test_load_filters_uid_under_1000(tmp_path):
    """System accounts (uid < 1000) must not surface as sessions."""
    db, conn = _make_db(tmp_path)
    _insert_host(conn, hostname="aamy", memory_total_mb=30_873, cpu_count=32)
    _insert_session(conn, hostname="aamy", username="root", uid=0,
                    session_epoch=1, peak_gb=8.0, span_hours=2.0)
    conn.commit()
    conn.close()

    rows = _load_user_sessions(str(db), "root", days=7)
    assert rows == []


def test_load_filters_by_username(tmp_path):
    """Only the requested user's sessions come back."""
    db, conn = _make_db(tmp_path)
    _insert_host(conn, hostname="boyi", memory_total_mb=256_904,
                 cpu_count=64)
    _insert_session(conn, hostname="boyi", username="kbui", uid=310395,
                    session_epoch=1, peak_gb=200.0, span_hours=20.0)
    _insert_session(conn, hostname="boyi", username="someone_else",
                    uid=99999, session_epoch=2, peak_gb=50.0,
                    span_hours=4.0)
    conn.commit()
    conn.close()

    rows = _load_user_sessions(str(db), "kbui", days=7)
    assert len(rows) == 1
    assert rows[0]["username"] == "kbui"


def test_load_filters_by_window(tmp_path):
    """Sessions older than the window are not returned."""
    db, conn = _make_db(tmp_path)
    _insert_host(conn, hostname="boyi", memory_total_mb=256_904,
                 cpu_count=64)
    _insert_session(conn, hostname="boyi", username="kbui", uid=310395,
                    session_epoch=1, peak_gb=100.0, span_hours=2.0,
                    end_offset_hours=30 * 24)
    _insert_session(conn, hostname="boyi", username="kbui", uid=310395,
                    session_epoch=2, peak_gb=50.0, span_hours=1.0,
                    end_offset_hours=1)
    conn.commit()
    conn.close()

    rows = _load_user_sessions(str(db), "kbui", days=7)
    assert len(rows) == 1
    assert rows[0]["session_epoch"] == 2


def test_load_missing_host_state_yields_null_capacity(tmp_path):
    """No workstation_state row → host capacity fields are None,
    not a SQL error. Scoring will mark dimensions inapplicable."""
    db, conn = _make_db(tmp_path)
    _insert_session(conn, hostname="ghost_host", username="user1",
                    uid=1001, session_epoch=1, peak_gb=4.0,
                    span_hours=1.0)
    conn.commit()
    conn.close()

    rows = _load_user_sessions(str(db), "user1", days=7)
    assert len(rows) == 1
    assert rows[0]["host_memory_total_mb"] is None


def test_load_no_db_returns_empty(tmp_path):
    """Missing DB doesn't raise; returns empty list (parallel to
    _load_user_jobs)."""
    rows = _load_user_sessions(
        str(tmp_path / "does_not_exist.db"), "anyone", days=7
    )
    assert rows == []


# ── Split helper ─────────────────────────────────────────────────────

def test_split_session_fields():
    """_split_session_fields separates session and host_state cleanly."""
    row = {
        "hostname": "boyi", "username": "kbui", "uid": 310395,
        "session_epoch": 42, "peak_memory_bytes": 200 * 1024**3,
        "cpu_usage_usec": 999, "samples": 1200, "span_hours": 12.0,
        "first_seen": "2026-05-01T00:00:00",
        "last_seen": "2026-05-01T12:00:00",
        "host_memory_total_mb": 256_904, "host_cpu_count": 64,
    }
    session, host_state = _split_session_fields(row)

    assert session["username"] == "kbui"
    assert session["peak_memory_bytes"] == 200 * 1024**3
    assert session["span_hours"] == 12.0
    assert "host_memory_total_mb" not in session

    assert host_state["memory_total_mb"] == 256_904
    assert host_state["cpu_count"] == 64
    assert "peak_memory_bytes" not in host_state


# ── End-to-end: load + score ─────────────────────────────────────────

def test_load_and_score_kbui_end_to_end(tmp_path):
    """Full pipeline: kbui-on-boyi data → SessionFingerprint with both
    dimensions failing."""
    db, conn = _make_db(tmp_path)
    _insert_host(conn, hostname="boyi", memory_total_mb=256_904,
                 cpu_count=64)
    _insert_session(conn, hostname="boyi", username="kbui", uid=310395,
                    session_epoch=1, peak_gb=253.0, span_hours=34.3)
    conn.commit()
    conn.close()

    rows = _load_user_sessions(str(db), "kbui", days=7)
    fps = _score_sessions(rows)

    assert len(fps) == 1
    fp = fps[0]
    assert isinstance(fp, SessionFingerprint)
    assert fp.username == "kbui"
    assert fp.hostname == "boyi"
    assert fp.dimensions["memory_pressure"].score < 5
    assert fp.dimensions["duration_fit"].score == 10
    assert hasattr(fp, "_last_seen")
    assert fp._last_seen


def test_score_sessions_survives_bad_row():
    """One unprocessable row doesn't kill the batch."""
    good_row = {
        "hostname": "boyi", "username": "kbui", "uid": 310395,
        "session_epoch": 1, "peak_memory_bytes": 200 * 1024**3,
        "cpu_usage_usec": 1, "samples": 60, "span_hours": 4.0,
        "first_seen": "2026-05-01T00:00:00",
        "last_seen": "2026-05-01T04:00:00",
        "host_memory_total_mb": 256_904, "host_cpu_count": 64,
    }
    bad_row = dict(good_row)
    bad_row["session_epoch"] = 2
    bad_row["span_hours"] = "not-a-number"

    fps = _score_sessions([good_row, bad_row])
    # Good row scores; bad row is logged and skipped
    assert len(fps) == 1
    assert fps[0].session_epoch == 1

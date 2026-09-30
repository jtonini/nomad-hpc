# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Tests for the NØMAÐ Insight Engine."""
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# Adjust path for direct execution
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nomad.insights.signals import (
    Signal, SignalType, Severity,
    read_job_signals, read_disk_signals, read_gpu_signals,
    read_queue_signals, read_network_signals, read_alert_signals,
    read_cloud_signals, read_all_signals,
)
from nomad.insights.templates import narrate
from nomad.insights.correlator import correlate, Insight
from nomad.insights.engine import InsightEngine
from nomad.insights.formatters import (
    format_cli_brief, format_cli_detail, format_json, format_slack,
)


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def demo_db(tmp_path):
    """Create a minimal demo database for testing."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    c = conn.cursor()
    now = datetime.now()

    # Jobs table — matches real nomad demo schema
    c.execute("""CREATE TABLE jobs (
        job_id TEXT NOT NULL,
        cluster TEXT NOT NULL,
        user_name TEXT,
        partition TEXT,
        node_list TEXT,
        job_name TEXT,
        state TEXT,
        exit_code INTEGER,
        exit_signal INTEGER,
        failure_reason INTEGER,
        submit_time DATETIME,
        start_time DATETIME,
        end_time DATETIME,
        req_cpus INTEGER,
        req_mem_mb INTEGER,
        req_gpus INTEGER,
        req_time_seconds INTEGER,
        runtime_seconds INTEGER,
        wait_time_seconds INTEGER,
        PRIMARY KEY (job_id, cluster)
    )""")
    # Generate jobs with mixed outcomes
    states = (
        ['COMPLETED'] * 80 +
        ['FAILED'] * 10 +
        ['TIMEOUT'] * 5 +
        ['OUT_OF_MEMORY'] * 5
    )
    for i, state in enumerate(states):
        submit = (now - timedelta(hours=12, minutes=i * 5))
        start = submit + timedelta(minutes=30)
        end = start + timedelta(hours=1)
        partition = 'gpu' if i % 5 == 0 else 'compute'
        gpus = 1 if partition == 'gpu' else 0
        user = f"user{i % 5}"
        c.execute(
            "INSERT INTO jobs (job_id, cluster, user_name, partition, state, submit_time, start_time, end_time, req_gpus, wait_time_seconds, runtime_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(i), "demo", user, partition, state, submit.isoformat(), start.isoformat(), end.isoformat(), gpus, 1800 + i * 60, 3600),
        )
    # Storage table
    c.execute("""CREATE TABLE storage_state (
        id INTEGER PRIMARY KEY,
        timestamp TEXT,
        hostname TEXT,
        storage_type TEXT,
        total_bytes INTEGER,
        used_bytes INTEGER,
        free_bytes INTEGER,
        usage_percent REAL
    )""")
    for h in range(6):
        ts = (now - timedelta(hours=h)).isoformat()
        usage = 85 + h * 0.5  # Filling up
        c.execute(
            "INSERT INTO storage_state VALUES (NULL,?,?,?,?,?,?,?)",
            (ts, "storage01", "scratch", int(1000*1073741824), int(usage*10*1073741824), int((100-usage)*10*1073741824), usage),
        )

    # Network table
    c.execute("""CREATE TABLE network_perf (
        id INTEGER PRIMARY KEY,
        timestamp TEXT,
        source_host TEXT,
        dest_host TEXT,
        ping_avg_ms REAL,
        ping_loss_pct REAL
    )""")
    for h in range(6):
        ts = (now - timedelta(hours=h)).isoformat()
        c.execute("INSERT INTO network_perf VALUES (NULL,?,?,?,?,?)",
                  (ts, "head", "compute", 8.5 + h, 0.05))

    # Queue state
    c.execute("""CREATE TABLE queue_state (
        id INTEGER PRIMARY KEY,
        partition TEXT,
        pending_jobs INTEGER,
        running_jobs INTEGER,
        total_jobs INTEGER,
        timestamp TEXT
    )""")
    ts = now.isoformat()
    c.execute("INSERT INTO queue_state VALUES (NULL,?,?,?,?,?)", ("compute", 50, 10, 60, ts))
    c.execute("INSERT INTO queue_state VALUES (NULL,?,?,?,?,?)", ("gpu", 20, 5, 25, ts))

    # Alerts
    c.execute("""CREATE TABLE alerts (
        id INTEGER PRIMARY KEY,
        timestamp TEXT,
        severity TEXT,
        source TEXT,
        host TEXT,
        message TEXT,
        details TEXT,
        resolved INTEGER DEFAULT 0
    )""")
    c.execute("INSERT INTO alerts VALUES (NULL,?,?,?,?,?,?,?)",
              (now.isoformat(), "warning", "disk_usage", "storage01", "Disk high", "92%", 0))
    c.execute("INSERT INTO alerts VALUES (NULL,?,?,?,?,?,?,?)",
              ((now - timedelta(hours=2)).isoformat(), "warning", "disk_usage", "storage01", "Disk high", "91%", 1))
    c.execute("INSERT INTO alerts VALUES (NULL,?,?,?,?,?,?,?)",
              ((now - timedelta(hours=4)).isoformat(), "warning", "disk_usage", "storage01", "Disk high", "93%", 1))

    # Cloud metrics
    c.execute("""CREATE TABLE cloud_metrics (
        id INTEGER PRIMARY KEY,
        timestamp TEXT,
        node_name TEXT,
        cluster TEXT,
        metric_name TEXT,
        value REAL,
        unit TEXT,
        source TEXT,
        instance_type TEXT,
        availability_zone TEXT,
        tags TEXT,
        cost_usd REAL
    )""")
    for h in range(24):
        ts = (now - timedelta(hours=h)).isoformat()
        c.execute("INSERT INTO cloud_metrics VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?)",
                  (ts, "ml-train-01", "aws-east", "cpu_utilization", 10.5, "percent",
                   "cloudwatch", "p3.2xlarge", "us-east-1a", None, None))
        c.execute("INSERT INTO cloud_metrics VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?)",
                  (ts, "ml-train-01", "aws-east", "cost_usd_per_day", 3.50, "usd",
                   "cloudwatch", "p3.2xlarge", "us-east-1a", None, None))

    # Workstation table
    c.execute("""CREATE TABLE workstation_state (
        id INTEGER PRIMARY KEY,
        timestamp TEXT,
        hostname TEXT,
        cpu_percent REAL,
        memory_percent REAL
    )""")
    for h in range(6):
        ts = (now - timedelta(hours=h)).isoformat()
        c.execute("INSERT INTO workstation_state VALUES (NULL,?,?,?,?)", (ts, "ws01", 85, 90))
        c.execute("INSERT INTO workstation_state VALUES (NULL,?,?,?,?)", (ts, "ws02", 92, 95))

    conn.commit()
    conn.close()
    return db_path


# ── Signal reader tests ──────────────────────────────────────────────────

def test_read_job_signals(demo_db):
    signals = read_job_signals(demo_db, hours=24)
    assert len(signals) > 0
    rate_sig = [s for s in signals if s.title == "job_success_rate"]
    assert len(rate_sig) == 1
    assert rate_sig[0].metrics["total"] == 100
    assert rate_sig[0].metrics["success_rate"] == 80.0


def test_read_disk_signals(demo_db):
    signals = read_disk_signals(demo_db, hours=12)
    assert len(signals) > 0
    usage_sig = [s for s in signals if s.title == "filesystem_usage"]
    assert len(usage_sig) >= 1


def test_read_gpu_signals(demo_db):
    signals = read_gpu_signals(demo_db, hours=24)
    # Should detect GPU failures
    assert isinstance(signals, list)


def test_read_queue_signals(demo_db):
    signals = read_queue_signals(demo_db, hours=12)
    pressure = [s for s in signals if s.title == "queue_pressure"]
    assert len(pressure) >= 1


def test_read_network_signals(demo_db):
    signals = read_network_signals(demo_db, hours=12)
    latency = [s for s in signals if s.title == "high_network_latency"]
    assert len(latency) >= 1


def test_read_alert_signals(demo_db):
    """Alerts are reported as raised, grouped by condition.

    nomad never marks an alert resolved, so none is called "active"; and a
    condition raised again after each cooldown is not "flapping".
    """
    signals = read_alert_signals(demo_db, hours=24)
    raised = [s for s in signals if s.title == "alerts_raised"]
    assert len(raised) == 1
    assert raised[0].metrics["total"] == 3
    assert raised[0].metrics["conditions"] == 1   # disk_usage on storage01, 3 times
    assert not [s for s in signals if s.title in ("active_alerts", "flapping_alert")]


def test_read_cloud_signals(demo_db):
    signals = read_cloud_signals(demo_db, hours=24)
    underused = [s for s in signals if s.title == "underutilized_cloud_instance"]
    assert len(underused) >= 1  # ml-train-01 at 10.5% avg


def test_read_all_signals(demo_db):
    signals = read_all_signals(demo_db, hours=24)
    assert len(signals) >= 5
    types_found = {s.signal_type for s in signals}
    assert SignalType.JOBS in types_found


# ── Template tests ───────────────────────────────────────────────────────

def test_narrate_produces_text(demo_db):
    signals = read_all_signals(demo_db, hours=24)
    for sig in signals:
        text = narrate(sig)
        assert isinstance(text, str)
        assert len(text) > 5


# ── Correlator tests ─────────────────────────────────────────────────────

def test_correlate_produces_insights(demo_db):
    signals = read_all_signals(demo_db, hours=24)
    insights = correlate(signals)
    assert isinstance(insights, list)
    # Our demo DB should trigger at least the workstation correlation
    # (two overloaded workstations + active alerts)


def test_correlate_disk_and_jobs():
    """Test disk+job correlation with synthetic signals."""
    signals = [
        Signal(
            signal_type=SignalType.DISK,
            severity=Severity.CRITICAL,
            title="disk_fill_projection",
            detail="storage01 filling fast",
            metrics={"server": "storage01", "fill_rate_gb_hr": 5.0, "hours_to_full": 8},
            affected_entities=["storage01"],
        ),
        Signal(
            signal_type=SignalType.JOBS,
            severity=Severity.WARNING,
            title="job_success_rate",
            detail="Low success rate",
            metrics={"total": 100, "success_rate": 75, "failed": 20, "oom": 3, "timed_out": 2, "hours": 24},
        ),
    ]
    insights = correlate(signals)
    disk_insights = [i for i in insights if i.title == "disk_filling_and_failures"]
    assert len(disk_insights) == 1
    assert disk_insights[0].severity == Severity.CRITICAL
    assert "storage01" in disk_insights[0].narrative


# ── Formatter tests ──────────────────────────────────────────────────────

def test_format_cli_brief(demo_db):
    signals = read_all_signals(demo_db)
    narratives = [(s, narrate(s)) for s in signals]
    insights = correlate(signals)
    output = format_cli_brief(narratives, insights)
    assert "NØMAÐ Insight Brief" in output
    assert "Cluster health:" in output


def test_format_cli_detail(demo_db):
    signals = read_all_signals(demo_db)
    narratives = [(s, narrate(s)) for s in signals]
    insights = correlate(signals)
    output = format_cli_detail(narratives, insights)
    assert "NØMAÐ Insight Report" in output
    assert "ALL SIGNALS" in output


def test_format_json(demo_db):
    signals = read_all_signals(demo_db)
    narratives = [(s, narrate(s)) for s in signals]
    insights = correlate(signals)
    output = format_json(narratives, insights)
    data = json.loads(output)
    assert "overall_health" in data
    assert "signals" in data
    assert "insights" in data
    assert data["signal_count"] == len(signals)


def test_format_slack(demo_db):
    signals = read_all_signals(demo_db)
    narratives = [(s, narrate(s)) for s in signals]
    insights = correlate(signals)
    output = format_slack(narratives, insights)
    assert "NØMAÐ" in output


# ── Engine integration test ──────────────────────────────────────────────

def test_engine_full_pipeline(demo_db):
    engine = InsightEngine(demo_db, hours=24, cluster_name="test-cluster")

    assert engine.signal_count > 0
    assert engine.overall_health in ("good", "nominal", "degraded", "impaired")

    brief = engine.brief()
    assert "test-cluster" in brief

    detail = engine.detail()
    assert "test-cluster" in detail

    json_out = engine.to_json()
    data = json.loads(json_out)
    assert data["cluster"] == "test-cluster"

    slack = engine.to_slack()
    assert isinstance(slack, str)

    subject, body = engine.to_email()
    assert "test-cluster" in subject
    assert "NØMAÐ" in body


def test_engine_empty_db(tmp_path):
    """Engine should handle an empty database gracefully."""
    db_path = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db_path))
    conn.close()

    engine = InsightEngine(db_path, hours=24)
    assert engine.signal_count == 0
    # Nothing was measured, so nothing can be called good.
    assert engine.overall_health == "unknown"
    assert not engine.measured
    assert all(c["status"] == "no_data" for c in engine.coverage)
    brief = engine.brief()
    assert "nothing measured" in brief.lower()
    assert engine.to_dict()["overall_health"] == "unknown"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.22: behaviour signals -- the agent (or careless script) the /proc watch
can't see. An agent driving the cluster over the user's own ssh looks like the
user typing, so the watch reads what was done: heavy CPU on a login node (the
probe), and job-submission patterns from the jobs table -- bursts (listed,
since a sweep bursts too), fail-fast repeats, and cancel/resubmit churn."""
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest

from nomad.collectors import watch_probe as probe
from nomad.collectors.policy_watch import (PolicyWatchCollector, SEVERITY, LABELS, JOB_DEFAULTS,
                                           config_problems, findings_of, job_findings, heavy_note)


# ── the probe: heavy use of a login node ────────────────────────────────────

def _mkproc(uptime=100000.0):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "uptime"), "w") as f:
        f.write(f"{uptime} 50000.0\n")
    return d


def _mkproc_pid(proc, pid, uid, comm, cpu_seconds, age_seconds, rss_pages=100000, argv=None):
    hz = probe.HZ or 100
    uptime = float(open(os.path.join(proc, "uptime")).read().split()[0])
    dd = os.path.join(proc, pid)
    os.makedirs(dd, exist_ok=True)
    with open(os.path.join(dd, "comm"), "w") as f:
        f.write(comm + "\n")
    argv = argv or [comm]
    with open(os.path.join(dd, "cmdline"), "wb") as f:
        f.write(b"\0".join(a.encode() for a in argv) + b"\0")
    with open(os.path.join(dd, "status"), "w") as f:
        f.write(f"Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nPPid:\t1\n")
    utime = int(cpu_seconds * hz)
    start = int((uptime - age_seconds) * hz)
    fields = ["S", "1", pid, pid, "0", "-1", "0", "0", "0", "0", "0", str(utime), "0",
              "0", "0", "20", "0", "1", "0", str(start), "0", str(rss_pages)]
    with open(os.path.join(dd, "stat"), "w") as f:
        f.write(f"{pid} ({comm}) " + " ".join(fields) + "\n")


def test_heavy_flags_sustained_cpu_only():
    proc = _mkproc()
    _mkproc_pid(proc, "100", 1000, "python3", cpu_seconds=20000, age_seconds=5000,
                rss_pages=2_000_000, argv=["python3", "big.py"])   # ~4 cores
    _mkproc_pid(proc, "101", 1000, "vim", cpu_seconds=100, age_seconds=5000)          # ~0.02 cores
    _mkproc_pid(proc, "102", 1001, "gcc", cpu_seconds=50, age_seconds=10)             # intense but brief
    _mkproc_pid(proc, "103", 0, "kworker", cpu_seconds=99999, age_seconds=5000)       # system account
    rows = probe.heavy(proc, 1000, cores=2.0, min_seconds=600)
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "heavy" and r["program"] == "python3" and r["uid"] == 1000
    assert r["heavy"]["cores"] >= 3.9 and r["heavy"]["rss_mb"] > 1000


def test_heavy_min_cpu_floor_is_configurable():
    proc = _mkproc()
    _mkproc_pid(proc, "200", 1000, "python3", cpu_seconds=700, age_seconds=300)       # 2.3 cores, 700 CPU-s
    assert probe.heavy(proc, 1000, cores=2.0, min_seconds=600) != []
    assert probe.heavy(proc, 1000, cores=2.0, min_seconds=1000) == []                 # under the floor


def test_probe_args_carry_heavy():
    c = PolicyWatchCollector({"enabled": True, "heavy_cpu": 3.0, "heavy_min_seconds": 300}, ":memory:")
    a = c._args()
    assert "--heavy" in a and a[a.index("--heavy-cpu") + 1] == "3.0" and a[a.index("--heavy-min") + 1] == "300"
    off = PolicyWatchCollector({"enabled": True, "watch_heavy": False}, ":memory:")
    assert "--heavy" not in off._args()


# ── the collector: heavy is kept only on a login host ───────────────────────

def _heavy_probe():
    return {"findings": [{"uid": 1000, "user": "alice", "kind": "heavy", "program": "python3",
                          "detail": "", "aka": ["python3"], "processes": 1,
                          "heavy": {"cores": 4.0, "rss_mb": 7812}}], "listeners": [], "root": []}


def test_heavy_is_a_warning_on_a_login_host():
    rows = findings_of(_heavy_probe(), "spydur", "login")
    assert rows and rows[0]["kind"] == "heavy" and rows[0]["severity"] == "warning"
    assert "cores" in rows[0]["note"]


def test_heavy_is_dropped_off_a_login_host():
    assert findings_of(_heavy_probe(), "node07", "node") == []
    assert findings_of(_heavy_probe(), "ml-ws01", "workstation") == []


def test_heavy_note_reads_the_facts():
    assert heavy_note({"cores": 4.0, "rss_mb": 7812}) == "~4 cores, sustained; 7.6 GB"
    assert heavy_note({"cores": 2.5}) == "~2.5 cores, sustained"
    assert heavy_note({}) == "" and heavy_note(None) == ""


# ── the collector: job-behaviour from the jobs table ────────────────────────

@pytest.fixture
def jobs_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, user_name TEXT, job_name TEXT, "
                 "state TEXT, submit_time TEXT, runtime_seconds INTEGER)")
    return conn


def _add(conn, rid, user, name, state, submit_dt, runtime):
    conn.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)",
                 (str(rid), user, name, state, submit_dt.isoformat(sep=" "), runtime))


NOW = datetime(2026, 10, 30, 12, 0, 0)


def test_a_burst_of_submissions_is_listed(jobs_db):
    for i in range(65):
        _add(jobs_db, i, "alice", f"j{i}", "COMPLETED", NOW - timedelta(minutes=5) + timedelta(seconds=i * 4), 100)
    jobs_db.commit()
    out = job_findings(jobs_db, "spydur", "login", NOW, dict(JOB_DEFAULTS))
    burst = [r for r in out if r["kind"] == "job_burst"]
    assert burst and burst[0]["username"] == "alice" and burst[0]["severity"] == "info"


def test_an_array_is_one_submission_not_a_burst(jobs_db):
    submit = NOW - timedelta(hours=1)
    for i in range(500):                                  # 500 tasks, one submit time
        _add(jobs_db, i, "bob", "arr", "COMPLETED", submit, 200)
    jobs_db.commit()
    assert job_findings(jobs_db, "spydur", "login", NOW, dict(JOB_DEFAULTS)) == []


def test_fail_fast_is_a_warning(jobs_db):
    for i in range(20):
        _add(jobs_db, i, "carol", f"f{i}", "FAILED", NOW - timedelta(minutes=30) + timedelta(seconds=i * 10), 3)
    jobs_db.commit()
    out = job_findings(jobs_db, "spydur", "login", NOW, dict(JOB_DEFAULTS))
    ff = [r for r in out if r["kind"] == "fail_fast"]
    assert ff and ff[0]["username"] == "carol" and ff[0]["severity"] == "warning"


def test_a_node_failure_is_not_the_users_fault(jobs_db):
    for i in range(20):
        _add(jobs_db, i, "dave", f"n{i}", "NODE_FAIL", NOW - timedelta(minutes=30) + timedelta(seconds=i * 10), 2)
    jobs_db.commit()
    assert [r for r in job_findings(jobs_db, "spydur", "login", NOW, dict(JOB_DEFAULTS))
            if r["kind"] == "fail_fast"] == []


def test_cancel_resubmit_churn_is_a_warning(jobs_db):
    for i in range(10):
        state = "CANCELLED by 1" if i < 6 else "COMPLETED"
        _add(jobs_db, i, "erin", "model", state, NOW - timedelta(minutes=40) + timedelta(minutes=i), 50)
    jobs_db.commit()
    out = job_findings(jobs_db, "spydur", "login", NOW, dict(JOB_DEFAULTS))
    churn = [r for r in out if r["kind"] == "job_churn"]
    assert churn and churn[0]["username"] == "erin" and "cancelled" in churn[0]["note"]


def test_no_jobs_table_is_no_op():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    assert job_findings(conn, "jonimitchell", "login", NOW, dict(JOB_DEFAULTS)) == []


# ── config ──────────────────────────────────────────────────────────────────

def test_behaviour_kinds_have_severities_and_labels():
    assert SEVERITY["heavy"] == "warning" and SEVERITY["job_burst"] == "info"
    assert SEVERITY["fail_fast"] == "warning" and SEVERITY["job_churn"] == "warning"
    assert all(k in LABELS for k in ("heavy", "job_burst", "fail_fast", "job_churn"))


def test_config_validates_behaviour_keys():
    ok, info = config_problems({"enabled": True, "watch_heavy": True, "heavy_cpu": 2.5,
                                "burst_jobs": 40, "churn_repeats": 6})
    assert ok == []
    assert any("behaviour" in line for line in info)
    bad, _ = config_problems({"burst_jobs": "lots", "heavy_cpu": -1, "watch_jobs": "maybe",
                              "fail_fast_seconds": 0})
    assert any("burst_jobs" in p for p in bad) and any("heavy_cpu" in p for p in bad)
    assert any("watch_jobs" in p for p in bad) and any("fail_fast_seconds" in p for p in bad)

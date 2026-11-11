# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.39.1: the weekly email as people read it: what is alike is one note
(the times of one kind of login-node use, one kind of asking for too much
over a person's partitions), with why and the better way said once; sizes
and times in words; no contractions; newer OpenSSH's sshd-session is what a
login node is for."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from nomad import notices as N
from nomad.edu import login_node as L
from nomad.edu.insights import Issue, UsageStats, UserInsights
from tests.test_release_1380 import row

NOW = datetime.now().replace(microsecond=0)
CONFIG = {"console": {"courses": {}, "labs": {"group_pattern": "{netid}$"}},
          "notices": {"review_to": [], "mail_domain": "example.edu", "send_from": "rc@example.edu",
                      "link": "https://nomad.example.edu", "signature": ["Research Computing", "Example University"],
                      "contact": "Questions: rc@example.edu", "skip_users": ["svc"]}}
# n't, 're, 'll, 've, 'd, 'm, and the 's of "it's" (a possessive's is fine: "the cluster's")
CONTRACTION = re.compile(r"\b[A-Za-z]+'(t|re|ll|ve|d|m)\b|\b(it|that|there|what|here|he|she|who)'s\b", re.I)


def issue(key, directive, asked, used, unit, partition, suggested, **kw):
    base = {"dimension": key, "dimension_key": key, "affected_jobs": 20, "total_applicable": 22, "avg_score": 10.0,
            "severity": "critical", "trajectory": "stable", "directive": directive, "direction": "lower",
            "current_value_typical": asked, "current_display": "x", "suggested_display": suggested,
            "cluster": "spydur", "partition": partition, "usage_stats": UsageStats(used, 0, 0, 0, 0, unit),
            "rationale": "Memory you don't use is unavailable to other jobs on the node."}
    base.update(kw)
    return Issue(**base)


@pytest.fixture
def world(tmp_path, monkeypatch):
    from nomad.db.migrations import ensure_database
    db = tmp_path / "combined.db"
    ensure_database(db)
    now = datetime.now(timezone.utc)
    rows = []
    for k in range(7):          # seven long computations through the week
        rows.append(row(user="tia", session=f"t{k}", end=now - timedelta(hours=10 + 12 * k), held=40 * 60,
                        command="python", cmdline=f"python fit.py --part {k}"))
    rows.append(row("user_cpu_200pct_10min", user="tia", session="all", end=now - timedelta(days=5, hours=3),
                    held=30 * 60, command="python", cpu=770.0))
    # newer OpenSSH: each login's and transfer's process is sshd-session
    rows.append(row("user_cpu_200pct_10min", user="zed", session="z", end=now - timedelta(hours=5), held=30 * 60,
                    command="sshd-session", cmdline="sshd-session: zed [priv]", cpu=590.0))
    rows.append(row(user="svc", session="v", end=now - timedelta(hours=5), held=40 * 60, command="cc1"))
    with sqlite3.connect(db) as c:
        c.execute("ALTER TABLE per_user_alert ADD COLUMN source_site TEXT")
        cols = list(rows[0])
        c.executemany(f"INSERT INTO per_user_alert ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                      [tuple(r[x] for x in cols) for r in rows])
    issues = {"tia": [issue("time", "time", 86400, 5886.5, "seconds", "cpunodes", "04:00:00"),
                      issue("time", "time", 7 * 86400, 1775, "seconds", "gpunodes", "09:00:00"),
                      issue("memory", "mem", 32768, 2142.72, "MB", "gpunodes", "8G"),
                      issue("cpu", "cpus-per-task", 8, 1.0, "cores", "gpunodes", "1")]}

    def fake(db_path, username, days=90, config=None, **k):
        return UserInsights(username=username, job_count=22, window_days=days, issues=issues.get(username, []))
    import nomad.edu.insights as I
    monkeypatch.setattr(I, "user_insights", fake)
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO jobs (job_id, user_name, end_time) VALUES ('1', 'tia', ?)",
                  ((NOW - timedelta(days=1)).isoformat(),))
    return db


def plan(db):
    s = N.settings(CONFIG)
    conn = sqlite3.connect(db)
    try:
        return N.send(conn, db, CONFIG, s, None, NOW, dry=True), s
    finally:
        conn.close()


def test_sizes_and_times_in_words():
    assert [N._mem(x) for x in (105.375, 72.9375, 0.2, 4903.61, 31723.6, 128000, 7372.8, 29491.2)] == [
        "105 MB", "73 MB", "1 MB", "4.8 GB", "31 GB", "125 GB", "7.2 GB", "28.8 GB"]
    assert [N._dur(x) for x in (20, 2988.5, 5886.5, 59858, 57600, 86400, 12 * 86400, 165600)] == [
        "under a minute", "50 min", "1 h 38 min", "16 h 38 min", "16 h", "1 day", "12 days", "1 day 22 h"]
    assert [N._many(x, "core") for x in (1, 1.0, 7.7, 8, 18)] == ["1 core", "1 core", "7.7 cores", "8 cores",
                                                                    "18 cores"]


def test_what_is_alike_is_one_note(world):
    p, s = plan(world)
    (m,) = p["mail"]                        # zed's sshd-session is not told; svc is skipped
    assert m["user"] == "tia" and len(m["entries"]) == 12
    assert p["left"]["not a person's account"] == 1 and p["left"]["nothing new"] == 1
    text = m["text"]
    # login nodes: seven long computations are one note, five shown, the why and better way once
    assert "* A long computation on the login node (7 times)" in text
    assert text.count("  - python on spydur, ") == 6         # five of the seven, and the one together
    assert "  ... and 2 more like these on My activity." in text
    why = L._TEXT["cpu"][1]
    assert text.count(why) == 1 and text.count("srun --pty -c 4") == 1
    assert "\n    srun --pty -c 4 --mem=16G --time=4:00:00 bash\n" in text
    assert "    python fit.py --part 0" in text                 # the newest first
    assert text.index("--part 0") < text.index("--part 1")
    assert "* Many programs busy at once on the login node\n" in text and "(7.7 cores)" in text
    # jobs: one note a kind, a line and its #SBATCH a partition
    assert text.count("* Your jobs ask for more time than they use") == 1
    assert ("  - On spydur (cpunodes), 20 of your last 22 jobs asked for more time than they used "
            "(typically 1 day; they used about 1 h 38 min). Ask for this instead:\n"
            "      #SBATCH --time=04:00:00\n") in text
    assert "(typically 7 days; they used about 30 min)" in text and "#SBATCH --time=09:00:00" in text
    assert "(typically 32 GB; they used about 2.1 GB)" in text
    assert "(typically 8 cores; they used about 1)" in text and "#SBATCH --cpus-per-task=1" in text
    assert text.count("Jobs that ask for less time start sooner") == 1
    # the email's own words: no contractions anywhere, the job advice's included
    assert "don't" not in text
    assert not CONTRACTION.search(text), CONTRACTION.search(text)
    # five notes: two on the login nodes, three about jobs; each entry is still told once
    assert N.subject_of(m) == "Research computing: 5 notes on your recent work"
    assert len({e.key for e in m["entries"]}) == 12
    assert text.index("On the login nodes") < text.index("Your jobs\n---------")


def test_sshd_session_is_what_a_login_node_is_for():
    assert {"sshd-session", "sshd-auth"} <= set(L.SKIP_PROGRAMS)
    now = datetime.now(timezone.utc)
    rows = [row("user_cpu_200pct_10min", command="sshd-session", end=now - timedelta(hours=1), cpu=590.0),
            row("cpu_50pct_2min", command="sshd-auth", session="a", end=now - timedelta(hours=1))]
    assert L.from_rows(rows, now=now) == []
    assert len(L.from_rows([row(command="python3", end=now - timedelta(hours=1))], now=now)) == 1

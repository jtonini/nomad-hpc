# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.64.1: people's sessions on lab machines, thinned past 13 months, stay
the sessions nomad.workstation_sessions tells apart: the same number, each
with its first and last reading, its CPU time and its most memory. 1.64.0
kept one reading every 15 minutes, which could hide a session ending and
the next beginning in the same 15 minutes."""
from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from nomad import keep_rules as K
from nomad.workstation_sessions import split

TODAY = date.today()
OLD = TODAY - timedelta(days=420)


def table(c):
    c.execute("CREATE TABLE workstation_user_snapshot (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME "
              "NOT NULL, hostname TEXT NOT NULL, username TEXT NOT NULL, uid INTEGER NOT NULL, session_epoch INTEGER, "
              "cpu_usage_usec INTEGER, memory_peak_bytes INTEGER, memory_anon_bytes INTEGER)")


def readings(seed, days=3):
    """Readings every 5 minutes (sometimes not), slices that end and begin
    again (in the same 15 minutes too), counters now and then missing,
    the epoch changing within a slice, a few hours with no reading."""
    rnd = random.Random(seed)
    rows = []
    for host in ("ws1", "ws2"):
        for uid in (1001, 1002):
            t = datetime(OLD.year, OLD.month, OLD.day) + timedelta(minutes=rnd.randrange(0, 60))
            end = t + timedelta(days=days)
            cpu, peak, epoch = 0, 2 ** 20, 1
            while t < end:
                r = rnd.random()
                if r < 0.03:                                   # the slice ends; a new one
                    cpu, peak, epoch = 0, 2 ** 20, epoch + 1
                elif r < 0.05:
                    t += timedelta(hours=rnd.choice((1, 2, 3)))  # no readings for a while
                elif r < 0.10:
                    epoch += 1                                 # a new terminal: the epoch, not the slice
                cpu += rnd.randrange(0, 600_000_000)
                peak += rnd.randrange(0, 2 ** 22)
                anon = rnd.randrange(2 ** 20, 2 ** 32)
                c = None if rnd.random() < 0.01 else cpu
                rows.append((t.isoformat(), host, f"u{uid}", uid, epoch, c, peak, anon))
                t += timedelta(minutes=20 if rnd.random() < 0.01 else rnd.choice((5, 5, 5, 5, 4, 6)))
    return sorted(rows)                                   # written in time order, as a collector does


def sessions(c):
    snaps = c.execute("SELECT hostname || char(0) || uid, timestamp, session_epoch, cpu_usage_usec, "
                      "memory_peak_bytes, memory_anon_bytes FROM workstation_user_snapshot "
                      "ORDER BY hostname, uid, timestamp").fetchall()
    out = []
    for rows in split(snaps):
        cpus = [r[3] for r in rows if r[3] is not None]
        out.append((rows[0][0], rows[0][1], rows[-1][1], (max(cpus) - min(cpus)) if cpus else None,
                    max(r[5] for r in rows)))
    return out


@pytest.mark.parametrize("seed", range(12))
def test_the_sessions_stay_as_they_were(tmp_path, seed):
    c = sqlite3.connect(tmp_path / "site.db")
    table(c)
    if seed % 2:                                          # found by an index on the time, as on a hub
        c.execute("CREATE INDEX idx_wus_timestamp ON workstation_user_snapshot(timestamp)")
    rows = readings(seed)
    c.executemany("INSERT INTO workstation_user_snapshot (timestamp, hostname, username, uid, session_epoch, "
                  "cpu_usage_usec, memory_peak_bytes, memory_anon_bytes) VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    before = sessions(c)
    K.run(c, K.settings({}), "by hand", TODAY)
    after = sessions(c)
    left = c.execute("SELECT COUNT(*) FROM workstation_user_snapshot").fetchone()[0]
    assert after == before
    assert left < len(rows) * 0.6                         # and fewer readings kept


def test_two_slices_in_the_same_15_minutes(tmp_path):
    """One slice ends at 10:05 having used a lot; the next begins at 10:10
    and has used more by 10:14: the 15 minutes' last reading alone hides it."""
    c = sqlite3.connect(tmp_path / "site.db")
    table(c)
    day = datetime(OLD.year, OLD.month, OLD.day)
    rows = [((day + timedelta(hours=9, minutes=m)).isoformat(), "ws1", "kim", 1001, None, m * 10 ** 7, 2 ** 30, 0)
            for m in range(0, 66, 5)]                       # 9:00 to 10:05, CPU up to 6.5e8
    rows += [((day + timedelta(hours=10, minutes=m)).isoformat(), "ws1", "kim", 1001, None, (m - 9) * 10 ** 9,
              2 ** 20, 0) for m in (10, 14, 20, 25)]       # 10:10 on: from nothing, faster
    c.executemany("INSERT INTO workstation_user_snapshot (timestamp, hostname, username, uid, session_epoch, "
                  "cpu_usage_usec, memory_peak_bytes, memory_anon_bytes) VALUES (?,?,?,?,?,?,?,?)", rows)
    c.commit()
    before = sessions(c)
    assert len(before) == 2
    K.run(c, K.settings({}), "by hand", TODAY)
    assert sessions(c) == before

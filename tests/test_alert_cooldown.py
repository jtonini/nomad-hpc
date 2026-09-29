# SPDX-License-Identifier: AGPL-3.0-or-later
"""The alert cooldown holds across cron runs.

Under cron every `nomad collect --once` is a new process, so a cooldown kept
only in memory started empty each time: a disk that stayed 85% full was
stored -- and emailed, where email was on -- every five minutes.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from nomad.alerts.dispatcher import AlertDispatcher


def _db(tmp_path):
    path = tmp_path / "site.db"
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, severity TEXT NOT NULL,
        category TEXT NOT NULL, source TEXT, message TEXT NOT NULL, details TEXT,
        resolved BOOLEAN DEFAULT FALSE, dedup_key TEXT)""")
    c.commit(); c.close()
    return path


class _Backend:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)
        return True


def _run(path, backend, cooldown=15, **alert):
    """One cron run: a fresh dispatcher, as `nomad collect --once` makes."""
    d = AlertDispatcher({"database": {"path": path.name},
                         "general": {"data_dir": str(path.parent)},
                         "alerts": {"cooldown_minutes": cooldown}})
    d.backends = [backend]
    a = {"severity": "warning", "source": "disk", "host": "spydur",
         "message": "Disk /scratch at 85.0% (threshold: 80%)"}
    a.update(alert)
    return d.dispatch(a)


def _stored(path):
    c = sqlite3.connect(path)
    n = c.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
    c.close()
    return n


def test_second_run_within_cooldown_sends_nothing(tmp_path):
    path, backend = _db(tmp_path), _Backend()
    assert _run(path, backend) == {"_Backend": True}
    assert _run(path, backend) == {}          # the next cron run, 5 minutes on
    assert len(backend.sent) == 1 and _stored(path) == 1


def test_sent_again_once_the_window_has_passed(tmp_path):
    path, backend = _db(tmp_path), _Backend()
    _run(path, backend)
    c = sqlite3.connect(path)
    c.execute("UPDATE alerts SET timestamp = ?",
              ((datetime.now() - timedelta(minutes=20)).isoformat(),))
    c.commit(); c.close()
    assert _run(path, backend) == {"_Backend": True}
    assert len(backend.sent) == 2


def test_different_host_severity_or_source_is_a_different_alert(tmp_path):
    path, backend = _db(tmp_path), _Backend()
    _run(path, backend)
    _run(path, backend, host="arachne")
    _run(path, backend, severity="critical")
    _run(path, backend, source="gpu")
    assert len(backend.sent) == 4


def test_alert_without_a_host(tmp_path):
    path, backend = _db(tmp_path), _Backend()
    _run(path, backend, host=None)
    _run(path, backend, host=None)
    assert len(backend.sent) == 1


def test_no_cooldown_and_no_database(tmp_path):
    path, backend = _db(tmp_path), _Backend()
    _run(path, backend, cooldown=0)
    _run(path, backend, cooldown=0)
    assert len(backend.sent) == 2
    d = AlertDispatcher({"alerts": {}})             # no database configured
    d.backends = [backend]
    assert d.dispatch({"severity": "warning", "source": "x", "host": "h", "message": "m"})

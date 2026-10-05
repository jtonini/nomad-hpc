# SPDX-License-Identifier: AGPL-3.0-or-later
"""An alert is raised when its condition appears or gets worse, then reminded
of once a day while it lasts -- across cron runs, one condition at a time.

Before 1.7.16 an alert was "the same" when its source, host and severity
matched: /home and /scratch on one head node were one alert, so a /home
warning waited behind the /scratch warning that had stood for weeks, and
that one was stored (and sent) every 15 minutes -- 2,405 times in two weeks
at spydur while /home filled.
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from nomad.alerts import dispatcher as dispatcher_mod
from nomad.alerts.dispatcher import AlertDispatcher, alert_key

T0 = datetime(2026, 10, 2, 9, 0)


class Clock:
    def __init__(self):
        self.now = T0

    def advance(self, **kw):
        self.now += timedelta(**kw)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return c.now

    monkeypatch.setattr(dispatcher_mod, "datetime", FakeDateTime)
    return c


def _db(tmp_path, with_state=True):
    path = tmp_path / "site.db"
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, severity TEXT NOT NULL,
        category TEXT NOT NULL, source TEXT, message TEXT NOT NULL, details TEXT,
        resolved BOOLEAN DEFAULT FALSE, dedup_key TEXT)""")
    if with_state:
        c.execute(dispatcher_mod.ALERT_STATE_SQL)
    c.commit()
    c.close()
    return path


class _Backend:
    def __init__(self):
        self.sent = []

    def send(self, alert):
        self.sent.append(alert)
        return True


def _run(path, backend, alerts_cfg=None, **alert):
    """One cron run: a fresh dispatcher, as `nomad collect --once` makes."""
    d = AlertDispatcher({"database": {"path": path.name},
                         "general": {"data_dir": str(path.parent)},
                         "alerts": alerts_cfg or {}})
    d.backends = [backend]
    a = {"severity": "warning", "source": "disk", "host": "spydur", "subject": "/scratch",
         "message": "Disk /scratch at 87.0% (threshold: 80%)"}
    a.update(alert)
    return d.dispatch(a)


def _stored(path):
    c = sqlite3.connect(path)
    rows = c.execute("SELECT severity, message, dedup_key FROM alerts ORDER BY id").fetchall()
    c.close()
    return rows


def test_a_standing_condition_is_raised_once_not_every_run(tmp_path, clock):
    path, backend = _db(tmp_path), _Backend()
    for _ in range(12 * 6):                                    # six hours of cron runs
        _run(path, backend)
        clock.advance(minutes=5)
    assert len(backend.sent) == 1 and len(_stored(path)) == 1


def test_each_disk_on_a_host_is_its_own_condition(tmp_path, clock):
    path, backend = _db(tmp_path), _Backend()
    _run(path, backend)                                        # /scratch, standing
    clock.advance(minutes=5)
    _run(path, backend)
    _run(path, backend, subject="/home", message="Disk /home at 80.4% (threshold: 80%)")
    assert [a["subject"] for a in backend.sent] == ["/scratch", "/home"]
    assert [r[2] for r in _stored(path)] == ["disk|spydur|/scratch", "disk|spydur|/home"]


def test_getting_worse_is_raised_and_a_dip_back_is_not(tmp_path, clock):
    path, backend = _db(tmp_path), _Backend()
    for sev in ("warning", "warning", "critical", "critical", "warning", "critical"):
        _run(path, backend, subject="/home", severity=sev)
        clock.advance(minutes=5)
    assert [a["severity"] for a in backend.sent] == ["warning", "critical"]


def test_reminded_once_a_day_while_it_lasts(tmp_path, clock):
    path, backend = _db(tmp_path), _Backend()
    for _ in range(12 * 50):                                   # 50 hours
        _run(path, backend)
        clock.advance(minutes=5)
    assert len(backend.sent) == 3                              # 0 h, 24 h, 48 h
    c = sqlite3.connect(path)
    first, count, sev = c.execute(
        "SELECT first_seen, raised_count, severity FROM alert_state").fetchone()
    assert first == T0.isoformat() and count == 3 and sev == "warning"


def test_reminder_interval_is_configurable(tmp_path, clock):
    path, backend = _db(tmp_path), _Backend()
    for _ in range(12 * 7):
        _run(path, backend, {"reminder_hours": 6})
        clock.advance(minutes=5)
    assert len(backend.sent) == 2


def test_a_condition_that_ended_and_came_back_is_raised_again(tmp_path, clock):
    path, backend = _db(tmp_path), _Backend()
    _run(path, backend)
    clock.advance(minutes=30)
    _run(path, backend)                                        # still the same episode
    clock.advance(minutes=61)                                  # not seen for an hour
    _run(path, backend)
    assert len(backend.sent) == 2


def test_a_new_episode_starts_over_at_its_own_severity(tmp_path, clock):
    path, backend = _db(tmp_path), _Backend()
    _run(path, backend, severity="critical")
    clock.advance(hours=2)
    _run(path, backend, severity="warning")
    clock.advance(minutes=5)
    _run(path, backend, severity="critical")                   # worse than this episode
    assert [a["severity"] for a in backend.sent] == ["critical", "warning", "critical"]


def test_works_on_a_database_without_the_state_table(tmp_path, clock):
    path, backend = _db(tmp_path, with_state=False), _Backend()
    _run(path, backend)
    clock.advance(minutes=5)
    _run(path, backend)
    assert len(backend.sent) == 1


def test_without_a_database_a_cooldown_in_this_process(tmp_path, clock):
    backend = _Backend()
    d = AlertDispatcher({"alerts": {"cooldown_minutes": 15}})
    d.backends = [backend]
    a = {"severity": "warning", "source": "disk", "host": "h", "subject": "/a", "message": "m"}
    d.dispatch(dict(a))
    d.dispatch(dict(a, subject="/b"))
    clock.advance(minutes=5)
    d.dispatch(dict(a))
    clock.advance(minutes=11)
    d.dispatch(dict(a))
    assert [x["subject"] for x in backend.sent] == ["/a", "/b", "/a"]


def test_a_database_that_cannot_be_written_still_sends_once(tmp_path, clock, monkeypatch):
    # spydur, 4 Oct: nomad's database is on the /home that filled. Each cron
    # run is a new process; a local file remembers what was sent.
    monkeypatch.setattr(dispatcher_mod.tempfile, "gettempdir", lambda: str(tmp_path))
    backend = _Backend()

    def run(**alert):
        d = AlertDispatcher({"database": {"path": "missing/dir/x.db"},
                             "general": {"data_dir": str(tmp_path)}, "alerts": {}})
        d.backends = [backend]
        a = {"severity": "warning", "source": "disk", "host": "h", "subject": "/home",
             "message": "m"}
        a.update(alert)
        return d.dispatch(a)

    assert run() == {"_Backend": True}
    for _ in range(12):
        clock.advance(minutes=5)
        assert run() == {}
    assert run(severity="critical") == {"_Backend": True}      # worse
    clock.advance(hours=24)
    assert run(severity="critical") == {"_Backend": True}      # the daily reminder
    assert run(subject="/scratch") == {"_Backend": True}       # another condition
    assert len(backend.sent) == 4


def test_nomad_test_alerts_is_always_sent(tmp_path, clock):
    path, backend = _db(tmp_path), _Backend()
    for _ in range(3):
        _run(path, backend, source="test", host="cli-test", subject=None)
    assert len(backend.sent) == 3


def test_alerts_without_a_subject_keep_one_condition_per_source_and_host(tmp_path, clock):
    # node_state names the node as the host; it needs no subject.
    path, backend = _db(tmp_path), _Backend()
    _run(path, backend, source="node_state", host="spdr03", subject=None)
    _run(path, backend, source="node_state", host="spdr04", subject=None)
    clock.advance(minutes=5)
    _run(path, backend, source="node_state", host="spdr03", subject=None)
    assert [a["host"] for a in backend.sent] == ["spdr03", "spdr04"]
    assert alert_key({"source": "node_state", "host": "spdr03"}) == "node_state|spdr03|"
    assert alert_key({"source": "a|b", "host": "h", "subject": "/x"}) == "a/b|h|/x"


def test_the_subject_is_kept_in_the_stored_details(tmp_path, clock):
    import json
    path, backend = _db(tmp_path), _Backend()
    _run(path, backend, subject="/home")
    c = sqlite3.connect(path)
    details = json.loads(c.execute("SELECT details FROM alerts").fetchone()[0])
    assert details["subject"] == "/home" and details["host"] == "spydur"


def test_two_metrics_of_one_mount_are_two_conditions(tmp_path, clock):
    path, backend = _db(tmp_path), _Backend()
    for metric in ("avg_rtt_ms", "retrans_percent"):
        _run(path, backend, source="nfs", subject="/home", message=metric,
             details={"metric": metric})
    assert [a["message"] for a in backend.sent] == ["avg_rtt_ms", "retrans_percent"]
    assert _stored(path)[1][2] == "nfs|spydur|/home|retrans_percent"


def test_critical_again_after_a_while_at_warning_is_raised(tmp_path, clock):
    path, backend = _db(tmp_path), _Backend()
    _run(path, backend, severity="critical")
    for _ in range(14):                                        # 70 minutes at warning
        clock.advance(minutes=5)
        _run(path, backend, severity="warning")
    clock.advance(minutes=5)
    _run(path, backend, severity="critical")
    assert [a["severity"] for a in backend.sent] == ["critical", "critical"]


def test_critical_again_after_a_reminder_at_warning_is_raised(tmp_path, clock):
    path, backend = _db(tmp_path), _Backend()
    _run(path, backend, severity="critical")
    clock.advance(minutes=5)
    _run(path, backend, severity="warning")
    c = sqlite3.connect(path)                     # still critical at its worst a moment ago,
    c.execute("UPDATE alert_state SET last_raised = ?, worst_seen = ?",   # raised a day ago
              ((clock.now - timedelta(hours=24)).isoformat(), clock.now.isoformat()))
    c.commit()
    c.close()
    clock.advance(minutes=5)
    _run(path, backend, severity="warning")                    # the reminder, at warning
    clock.advance(minutes=5)
    _run(path, backend, severity="critical")
    assert [a["severity"] for a in backend.sent] == ["critical", "warning", "critical"]


def test_a_send_that_failed_is_tried_again_sooner_and_sooner_less(tmp_path, clock):
    path = _db(tmp_path)

    class Down:
        def __init__(self):
            self.tries = 0

        def send(self, alert):
            self.tries += 1
            return False

    down = Down()
    for _ in range(12 * 3):                                    # three hours
        _run(path, down)
        clock.advance(minutes=5)
    assert down.tries == 4                                     # 0, 15, 45, 105 minutes
    assert len(_stored(path)) == 1                             # one alert, sent again

    class Up(_Backend):
        pass

    up = Up()
    clock.advance(hours=2)                                     # the next retry is due
    _run(path, up, message="Disk /scratch at 88.0% (threshold: 80%)")
    clock.advance(minutes=5)
    _run(path, up)
    assert len(up.sent) == 1                                   # delivered, then quiet
    c = sqlite3.connect(path)
    assert c.execute("SELECT send_failures, retry_at FROM alert_state").fetchone() == (0, None)


def test_the_state_says_how_it_is_now(tmp_path, clock):
    path, backend = _db(tmp_path), _Backend()
    _run(path, backend, severity="critical")
    clock.advance(minutes=5)
    _run(path, backend, severity="warning")
    c = sqlite3.connect(path)
    assert c.execute("SELECT severity, last_severity FROM alert_state").fetchone() == \
        ("critical", "warning")


def test_a_backend_broken_for_months_keeps_its_backoff(tmp_path, clock):
    path = _db(tmp_path)

    class Down:
        tries = 0

        def send(self, alert):
            Down.tries += 1
            return False

    c = sqlite3.connect(path)
    _run(path, Down())
    c.execute("UPDATE alert_state SET send_failures = 60")       # two months of failures
    c.commit()
    for _ in range(12 * 48):                                      # two more days
        clock.advance(minutes=5)
        _run(path, Down())
    assert Down.tries <= 4                                        # daily, not every run
    assert c.execute("SELECT send_failures FROM alert_state").fetchone()[0] > 60

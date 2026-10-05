# SPDX-License-Identifier: AGPL-3.0-or-later
"""Disk fill forecasts, and the thresholds a site sets.

spydur's /home (16.5 TB) grew about 0.01 TB a day for weeks, then about
2 TB a day from Friday 2 October: 80% Friday 15:35, 90% Saturday 11:55, 95%
Saturday 22:35, 99% Sunday 06:15. nomad's one alert came at 95%, eight hours
before 99%. A forecast from the last hours of readings says on Friday that
it will be full within days, and on Saturday that it will be full within one.
(The replay below has those four times; when the fast growth began is
assumed -- 06:00 Friday. forecast_replay.py replays the hub's real rows.)
"""

import sqlite3
from datetime import datetime, timedelta

import pytest

from nomad.alerts import thresholds as th
from nomad.collectors.disk import DiskCollector, fill_forecast

TB = 1e12
TOTAL = 16.49 * TB


def series(points, step_minutes=5):
    """5-minute readings (unix_s, used_bytes) through (datetime, used_fraction) points."""
    out = []
    for (t0, f0), (t1, f1) in zip(points, points[1:]):
        t = t0
        while t < t1:
            f = f0 + (f1 - f0) * (t - t0) / (t1 - t0)
            out.append((t.timestamp(), f * TOTAL))
            t += timedelta(minutes=step_minutes)
    out.append((points[-1][0].timestamp(), points[-1][1] * TOTAL))
    return out


def test_needs_enough_readings_over_an_hour():
    t = 1_790_000_000
    assert fill_forecast([(t, 1), (t + 4000, 2), (t + 8000, 3)], 100, 50) == (None, None)
    few = [(t + i * 300, i) for i in range(10)]                # 45 minutes
    assert fill_forecast(few, 100, 50) == (None, None)


def test_a_steady_rise_is_projected_to_full():
    t = 1_790_000_000
    pts = [(t + i * 300, 1000 + i * 300 * 2.0) for i in range(25)]   # 2 bytes/s, 2 h
    rate, hours = fill_forecast(pts, 1e6, 7200)
    assert rate == pytest.approx(2.0) and hours == pytest.approx(1.0)


def test_flat_falling_tiny_or_full_is_no_forecast():
    t = 1_790_000_000
    flat = [(t + i * 300, 5e12 + (1e9 if i % 2 else -1e9)) for i in range(72)]  # ±1 GB jitter
    assert fill_forecast(flat, TOTAL, 3e12)[1] is None
    falling = [(t + i * 300, 5e12 - i * 1e9) for i in range(72)]
    assert fill_forecast(falling, TOTAL, 3e12)[1] is None
    tiny = [(t + i * 300, 5e12 + i * 1e8) for i in range(72)]          # 7 GB in 6 h
    assert fill_forecast(tiny, TOTAL, 3e12)[1] is None
    full = [(t + i * 300, 5e12 + i * 1e10) for i in range(72)]
    assert fill_forecast(full, TOTAL, 0)[1] is None


def _classify(hours, t=None):
    t = t or th.thresholds_from({})["disk"]
    if hours is None or hours <= 0:
        return None
    if hours <= t["full_within_hours_critical"]:
        return "critical"
    if hours <= t["full_within_hours_warning"]:
        return "warning"
    return None


def test_spydur_home_weekend_replayed():
    fri = datetime(2026, 10, 2)
    pts = [(fri - timedelta(days=2), 0.74), (fri + timedelta(hours=6), 0.7415),
           (fri + timedelta(hours=15, minutes=35), 0.80),
           (fri + timedelta(days=1, hours=11, minutes=55), 0.90),
           (fri + timedelta(days=1, hours=22, minutes=35), 0.95),
           (fri + timedelta(days=2, hours=6, minutes=15), 0.99),
           (fri + timedelta(days=2, hours=8), 1.0)]
    readings = series(pts)
    first = {}
    for i, (t, used) in enumerate(readings):
        window = [r for r in readings[:i + 1] if r[0] >= t - 6 * 3600]
        _, hours = fill_forecast(window, TOTAL, TOTAL - used)
        level = _classify(hours)
        if level and level not in first:
            first[level] = datetime.fromtimestamp(t)
        if used / TOTAL >= 0.80 and "threshold_80" not in first:
            first["threshold_80"] = datetime.fromtimestamp(t)
    at_99 = fri + timedelta(days=2, hours=6, minutes=15)
    assert first["warning"] < first["threshold_80"]           # before the 80% warning
    assert first["warning"] < fri + timedelta(hours=12)       # Friday morning
    assert at_99 - first["critical"] >= timedelta(hours=20)   # a day's notice, not 8 hours
    assert first["critical"].date() == datetime(2026, 10, 3).date()


def test_quiet_weeks_raise_nothing():
    start = datetime(2026, 9, 21)
    readings = series([(start, 0.73), (start + timedelta(days=10), 0.75)])
    for i in range(72, len(readings), 37):
        window = readings[i - 72:i + 1]
        _, hours = fill_forecast(window, TOTAL, TOTAL - window[-1][1])
        assert _classify(hours) is None


# ---------------------------------------------------------------------------
# The threshold checker
# ---------------------------------------------------------------------------

@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(th, "send_alert", lambda **kw: out.append(kw))
    monkeypatch.setattr(th, "get_dispatcher", lambda: object())
    return out


def fs(path="/home", pct=50.0, **extra):
    item = {"type": "filesystem", "path": path, "used_percent": pct, "total_bytes": TOTAL,
            "used_bytes": pct / 100 * TOTAL, "available_bytes": (100 - pct) / 100 * TOTAL}
    item.update(extra)
    return item


def test_each_alert_names_its_disk(sent):
    th.ThresholdChecker({}).check("disk", [fs("/home", 85), fs("/scratch", 87)], host="spydur")
    assert [(a["subject"], a["severity"]) for a in sent] == [("/home", "warning"),
                                                             ("/scratch", "warning")]


def test_the_sites_thresholds_are_read_and_not_leaked(sent):
    th.ThresholdChecker({"alerts": {"thresholds": {"disk": {"used_percent_warning": 90}}}}
                        ).check("disk", [fs(pct=85)])
    assert sent == []
    th.ThresholdChecker({}).check("disk", [fs(pct=85)])
    assert len(sent) == 1                                     # the default is still 80
    assert th.DEFAULT_THRESHOLDS["disk"]["used_percent_warning"] == 80


def test_the_example_configs_flat_keys_but_not_the_old_forecast_days(sent):
    # disk_fill_days_warning and [alerts.predictive]'s days were written for
    # a forecast that never ran (7 days, from a copied default.toml): not read.
    cfg = {"alerts": {"thresholds": {"disk_warning_percent": 85, "disk_fill_days_warning": 7,
                                     "queue_depth_warning": 100},
                      "predictive": {"days_until_full_warning": 7}}}
    t = th.thresholds_from(cfg)["disk"]
    assert t["used_percent_warning"] == 85 and t["full_within_hours_warning"] == 72
    nested = {"alerts": {"thresholds": {"disk": {"full_within_hours_critical": 6,
                                                 "used_percent_warning": "85",
                                                 "used_percent_critical": "high"}}}}
    t = th.thresholds_from(nested)["disk"]
    assert t["full_within_hours_critical"] == 6 and t["used_percent_warning"] == 85
    assert t["used_percent_critical"] == 95                   # not a number: left out


def test_quota_records_are_not_disks(sent):
    quota = {"type": "quota", "entity_type": "user", "entity_name": "someone",
             "filesystem_path": "/home", "used_percent": 99.0}
    th.ThresholdChecker({}).check("disk", [quota])
    assert sent == []


def test_a_disk_filling_fast_is_forecast(sent):
    rate = 2 * TB
    th.ThresholdChecker({}).check("disk", [
        fs("/home", 84, hours_until_full=40, fill_rate_bytes_per_day=rate, forecast_window_hours=6),
        fs("/data", 50, hours_until_full=10, fill_rate_bytes_per_day=rate, forecast_window_hours=6),
        fs("/scratch", 60, hours_until_full=500, fill_rate_bytes_per_day=1e9,
           forecast_window_hours=6)], host="spydur")
    got = [(a["source"], a["subject"], a["severity"]) for a in sent]
    assert ("disk_forecast", "/home", "warning") in got
    assert ("disk_forecast", "/data", "critical") in got
    assert not any(s == "/scratch" and src == "disk_forecast" for src, s, _ in got)
    msg = next(a["message"] for a in sent if a["subject"] == "/data")
    assert msg == ("Disk /data will be full in about 10 hours "
                   "(filling 2.0 TB/day over the last 6 hours; 50% used)")
    assert th._duration(1.2) == "1 hour" and th._duration(0.02) == "1 minute"


def test_past_its_critical_threshold_a_disk_is_forecast_only_when_filling_fast(sent):
    # spydur /home at 97%, 529 GB left: slowly, the 95% alert says it all...
    th.ThresholdChecker({}).check("disk", [fs("/home", 97.0, hours_until_full=30,
                                              fill_rate_bytes_per_day=0.4 * TB,
                                              forecast_window_hours=6)])
    assert [(a["source"], a["severity"]) for a in sent] == [("disk", "critical")]
    assert sent[0]["message"] == "Disk /home at 97.0% (494.7 GB free; threshold: 95%)"
    sent.clear()
    # ...at the weekend's rate the rest goes in hours: that is news.
    th.ThresholdChecker({}).check("disk", [fs("/home", 97.0, hours_until_full=5,
                                              fill_rate_bytes_per_day=2 * TB,
                                              forecast_window_hours=6)])
    assert [(a["source"], a["severity"]) for a in sent] == [("disk", "critical"),
                                                            ("disk_forecast", "critical")]
    sent.clear()
    cfg = {"alerts": {"thresholds": {"disk": {"full_within_hours_past_critical": 2}}}}
    th.ThresholdChecker(cfg).check("disk", [fs("/home", 97.0, hours_until_full=5)])
    assert [a["source"] for a in sent] == ["disk"]


def test_forecasts_can_be_turned_off(sent):
    th.ThresholdChecker({"alerts": {"predictive": {"enabled": False}}}).check(
        "disk", [fs(pct=50, hours_until_full=5)])
    assert sent == []


# ---------------------------------------------------------------------------
# The disk collector stores the forecast
# ---------------------------------------------------------------------------

def test_store_writes_the_forecast_into_the_row_and_the_record(tmp_path):
    from nomad.db.migrations import ensure_database
    db = tmp_path / "s.db"
    ensure_database(db)
    now = datetime.now()
    with sqlite3.connect(db) as c:
        for i in range(36, 0, -1):                           # 3 h, +10 GB per 5 min
            t = now - timedelta(minutes=5 * i)
            used = 10 * TB - i * 10e9
            c.execute("INSERT INTO filesystems (path, total_bytes, used_bytes, available_bytes, "
                      "used_percent, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                      ("/home", 12 * TB, used, 12 * TB - used, used / 12e12 * 100,
                       t.isoformat()))
    rec = {"type": "filesystem", "path": "/home", "total_bytes": 12 * TB, "used_bytes": 10 * TB,
           "available_bytes": 2 * TB, "used_percent": 83.3}
    DiskCollector({"filesystems": ["/home"]}, db).store([rec])
    assert rec["fill_rate_bytes_per_day"] == pytest.approx(10e9 * 12 * 24, rel=0.02)
    assert rec["hours_until_full"] == pytest.approx(2e12 / (10e9 * 12), rel=0.02)
    with sqlite3.connect(db) as c:
        rate, days = c.execute("SELECT fill_rate_bytes_per_day, days_until_full FROM filesystems "
                               "ORDER BY id DESC LIMIT 1").fetchone()
    assert rate == pytest.approx(rec["fill_rate_bytes_per_day"])
    assert days == pytest.approx(rec["hours_until_full"] / 24)


def _history(db, path, rows):
    with sqlite3.connect(db) as c:
        for t, total, avail in rows:
            c.execute("INSERT INTO filesystems (path, total_bytes, used_bytes, available_bytes, "
                      "used_percent, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                      (path, total, total - avail, avail, (total - avail) / total * 100,
                       t.isoformat()))


def test_readings_of_another_filesystem_are_not_history(tmp_path):
    # /home unmounted for half an hour: df read the root filesystem beneath.
    from nomad.db.migrations import ensure_database
    db = tmp_path / "s.db"
    ensure_database(db)
    now = datetime.now()
    rows = []
    for i in range(72, 0, -1):
        t = now - timedelta(minutes=5 * i)
        root = 50 <= i < 56
        rows.append((t, 0.1 * TB if root else 16.49 * TB, 0.05 * TB if root else 4.3 * TB))
    _history(db, "/home", rows)
    rec = {"type": "filesystem", "path": "/home", "total_bytes": 16.49 * TB,
           "used_bytes": 12.19 * TB, "available_bytes": 4.3 * TB, "used_percent": 74.0}
    DiskCollector({}, db).store([rec])
    assert rec["hours_until_full"] is None
    assert abs(rec["fill_rate_bytes_per_day"]) < 1e9


def test_a_dataset_on_a_filling_pool_is_forecast(tmp_path):
    # Its own usage flat; the pool's free space (and so its size) shrinking.
    from nomad.db.migrations import ensure_database
    db = tmp_path / "s.db"
    ensure_database(db)
    now = datetime.now()
    used = 2 * TB
    rows = [(now - timedelta(minutes=5 * i), used + 3 * TB + i * 5e9, 3 * TB + i * 5e9)
            for i in range(72, 0, -1)]
    _history(db, "/data", [(t, total, avail) for t, total, avail in rows])
    rec = {"type": "filesystem", "path": "/data", "total_bytes": used + 3 * TB,
           "used_bytes": used, "available_bytes": 3 * TB, "used_percent": 40.0}
    DiskCollector({}, db).store([rec])
    assert rec["hours_until_full"] == pytest.approx(3e12 / 60e9, rel=0.05)    # 50 hours


def test_a_dataset_on_a_pool_filling_fast_is_still_forecast(tmp_path):
    # Its size (own 0.2 TB + the pool's free space) shrinks 0.4 TB an hour.
    from nomad.db.migrations import ensure_database
    db = tmp_path / "s.db"
    ensure_database(db)
    now = datetime.now()
    own = 0.2 * TB
    rows = [(now - timedelta(minutes=5 * i), own + 3 * TB + i * 0.4 * TB / 12,
             3 * TB + i * 0.4 * TB / 12) for i in range(36, 0, -1)]
    _history(db, "/pool/lab", rows)
    rec = {"type": "filesystem", "path": "/pool/lab", "total_bytes": own + 3 * TB,
           "used_bytes": own, "available_bytes": 3 * TB, "used_percent": 6.3}
    DiskCollector({}, db).store([rec])
    assert rec["hours_until_full"] == pytest.approx(7.5, rel=0.05)
    assert rec["forecast_window_hours"] == pytest.approx(3.0, abs=0.1)


def test_store_without_history_leaves_the_forecast_empty(tmp_path):
    from nomad.db.migrations import ensure_database
    db = tmp_path / "s.db"
    ensure_database(db)
    rec = {"type": "filesystem", "path": "/x", "total_bytes": 100, "used_bytes": 50,
           "available_bytes": 50, "used_percent": 50.0}
    DiskCollector({}, db).store([rec])
    assert rec["hours_until_full"] is None and rec["fill_rate_bytes_per_day"] is None


# ---------------------------------------------------------------------------
# nomad collect reads the site's thresholds; nomad alerts shows what is active
# ---------------------------------------------------------------------------

def _config(tmp_path, warning):
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f"""
[general]
data_dir = "{tmp_path}"

[collectors.disk]
filesystems = ["/home"]

[alerts.thresholds.disk]
used_percent_warning = {warning}
""")
    return cfg


@pytest.mark.parametrize("warning, raised", [(80, 1), (90, 0)])
def test_collect_uses_the_sites_thresholds_and_its_database(tmp_path, monkeypatch,
                                                            warning, raised):
    from click.testing import CliRunner

    import nomad.alerts.dispatcher as dispatcher_mod
    from nomad.cli import cli
    from nomad.collectors import base
    monkeypatch.setattr(dispatcher_mod, "_dispatcher", None)
    monkeypatch.setattr(base.registry, "_config", {}, raising=False)
    monkeypatch.setattr(DiskCollector, "collect", lambda self: [fs("/home", 85)])
    db = tmp_path / "elsewhere.db"
    result = CliRunner().invoke(cli, ["-c", str(_config(tmp_path, warning)), "collect", "--once",
                                      "-C", "disk", "--db", str(db)])
    assert result.exit_code == 0, result.output
    with sqlite3.connect(db) as c:
        rows = c.execute("SELECT category, severity, dedup_key FROM alerts").fetchall()
    assert rows == [("disk", "warning", "disk|" + _host() + "|/home|used_percent")] * raised

    out = CliRunner().invoke(cli, ["-c", str(_config(tmp_path, warning)), "alerts",
                                   "--db", str(db)]).output
    assert f"Active now: {raised}" in out
    if raised:
        assert "Disk /home at 85.0%" in out and "raised 1x" in out


def _host():
    import socket
    return socket.gethostname()



def test_a_disk_too_full_to_store_into_is_still_alerted_about(tmp_path, monkeypatch):
    # nomad's database on the filesystem that filled: storing fails, the
    # readings are still checked, and the run is still a failure.
    from nomad.collectors import base
    checked = []
    monkeypatch.setattr(base, "check_and_alert",
                        lambda name, data, cfg, host=None: checked.append((name, data)))
    monkeypatch.setattr(DiskCollector, "collect", lambda self: [fs("/home", 100.0)])

    def full(self, data):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(DiskCollector, "store", full)
    r = DiskCollector({}, tmp_path / "s.db").run()
    assert not r.success and "disk is full" in r.error_message
    assert checked and checked[0][0] == "disk" and checked[0][1][0]["used_percent"] == 100.0

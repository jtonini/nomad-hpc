# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.63.0: keep rules, part 1 (nomad.keep_rules): CPU per core and disk per
device rolled up to hours past 90 days and days past a year, lab machines'
mounts to what each day was, collector runs to a count a day; `nomad db
keep`; the databases collector once a day; and `nomad db status --sites`
listing only what each site found at its latest reading."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad import db_health as H
from nomad import keep_rules as K

TODAY = date(2026, 11, 23)


def ts(d: date, h=0, m=0, s=0) -> str:
    return datetime(d.year, d.month, d.day, h, m, s, 123456).isoformat()


def mpstat(conn, first: date, last: date, cores=2, every=30):
    conn.execute("CREATE TABLE IF NOT EXISTS mpstat_core (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "timestamp DATETIME NOT NULL, core_id INTEGER NOT NULL, user_percent REAL, nice_percent REAL, "
                 "system_percent REAL, iowait_percent REAL, irq_percent REAL, soft_percent REAL, "
                 "steal_percent REAL, idle_percent REAL, busy_percent REAL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_mpstat_core_ts ON mpstat_core(timestamp)")
    d = first
    rows = []
    while d <= last:
        for minute in range(0, 24 * 60, every):
            for core in range(cores):
                busy = float(minute % 60)                          # 0, 30 in each hour: averages 15
                rows.append((ts(d, minute // 60, minute % 60), core, busy, 0, 0, 0, 0, 0, 0, 100 - busy, busy))
        d += timedelta(days=1)
    conn.executemany("INSERT INTO mpstat_core (timestamp, core_id, user_percent, nice_percent, system_percent, "
                     "iowait_percent, irq_percent, soft_percent, steal_percent, idle_percent, busy_percent) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


def n(conn, table, where="1", args=()):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]


# -- settings ----------------------------------------------------------------------------------

def test_the_settings():
    s = K.settings(None)
    assert (s.enabled, s.detail_days, s.hourly_days, s.max_rows, s.tables, s.problems) == \
        (True, 90, 365, 1_000_000, {}, ())
    s = K.settings({"detail_days": 30, "hourly_days": 10, "keep_rules": "yes",
                    "keep": {"mpstat_core": {"detail_days": 180}, "jobs": {"detail_days": 7}}})
    assert s.enabled is True and s.detail_days == 30 and s.hourly_days == 30
    assert s.days("mpstat_core") == (180, 180) and s.days("iostat_device") == (30, 30)
    assert len(s.problems) == 3                            # keep_rules not a bool, hourly < detail, jobs not kept


# -- CPU per core: hours, then days ------------------------------------------------------------

def test_cpu_per_core_to_hours_past_90_days(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    mpstat(conn, TODAY - timedelta(days=95), TODAY - timedelta(days=1))
    s = K.settings({})
    (td,) = [t for t in K.todo(conn, s, TODAY) if t.table == "mpstat_core"]
    cut = TODAY - timedelta(days=90)
    assert td.levels == [("hourly", TODAY - timedelta(days=95), cut)] and td.rows == 5 * 48 * 2
    (r,) = [x for x in K.run(conn, s, "by hand", TODAY) if x["table"] == "mpstat_core"]
    assert (r["before"], r["after"], r["days"]) == (480, 5 * 24 * 2, 5)
    old = conn.execute("SELECT timestamp, core_id, busy_percent, idle_percent FROM mpstat_core WHERE timestamp < ? "
                       "ORDER BY timestamp, core_id", (cut.isoformat(),)).fetchall()
    assert old[0] == (f"{TODAY - timedelta(days=95)}T00:00:00", 0, 15.0, 85.0)
    assert len({(t, c) for t, c, *_ in old}) == len(old) == 240               # one a core and hour
    assert n(conn, "mpstat_core", "timestamp >= ?", (cut.isoformat(),)) == 90 * 48 * 2   # recent: untouched
    # done once: the same day again finds nothing due
    assert not [x for x in K.run(conn, s, "daily", TODAY) if x["table"] == "mpstat_core"]
    # the next day: that day only
    (r2,) = [x for x in K.run(conn, s, "daily", TODAY + timedelta(days=1)) if x["table"] == "mpstat_core"]
    assert (r2["days"], r2["before"], r2["after"]) == (1, 96, 48)


def test_then_days_past_a_year(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    mpstat(conn, TODAY - timedelta(days=368), TODAY - timedelta(days=366))
    s = K.settings({})
    K.run(conn, s, "by hand", TODAY)                       # to hours first
    assert n(conn, "mpstat_core") == 3 * 24 * 2
    K.run(conn, s, "daily", TODAY + timedelta(days=1))     # days are made of the hours
    rows = conn.execute("SELECT timestamp, core_id, busy_percent FROM mpstat_core ORDER BY timestamp, core_id").fetchall()
    assert rows[:2] == [(f"{TODAY - timedelta(days=368)}T00:00:00", 0, 15.0),
                        (f"{TODAY - timedelta(days=368)}T00:00:00", 1, 15.0)]
    assert len(rows) == 3 * 2


def test_disk_per_device_too(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    conn.execute("CREATE TABLE iostat_device (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL, "
                 "device TEXT NOT NULL, reads_per_sec REAL, read_kb_per_sec REAL, read_await_ms REAL, "
                 "writes_per_sec REAL, write_kb_per_sec REAL, write_await_ms REAL, util_percent REAL, "
                 "queue_length REAL)")
    d = TODAY - timedelta(days=100)
    for minute in range(0, 120, 10):
        for dev, util in (("sda", minute), ("nvme0n1", 50.0)):
            conn.execute("INSERT INTO iostat_device (timestamp, device, util_percent) VALUES (?, ?, ?)",
                         (ts(d, minute // 60, minute % 60), dev, float(util)))
    conn.commit()
    K.run(conn, K.settings({}), "by hand", TODAY)
    got = conn.execute("SELECT timestamp, device, util_percent FROM iostat_device ORDER BY timestamp, device").fetchall()
    assert got == [(f"{d}T00:00:00", "nvme0n1", 50.0), (f"{d}T00:00:00", "sda", 25.0),
                   (f"{d}T01:00:00", "nvme0n1", 50.0), (f"{d}T01:00:00", "sda", 85.0)]


# -- lab machines' mounts: what each day was ---------------------------------------------------

def test_mounts_keep_what_each_day_was(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    conn.execute("CREATE TABLE workstation_mount_state (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME "
                 "NOT NULL, hostname TEXT NOT NULL, mountpoint TEXT NOT NULL, fstype TEXT, source TEXT, "
                 "is_mounted INTEGER NOT NULL, is_responsive INTEGER NOT NULL, response_ms REAL)")
    d = TODAY - timedelta(days=120)
    for h in range(24):                                    # a run an hour, two machines, two mounts
        for host in ("aamy", "evan"):
            if host == "evan" and h == 23:
                continue                                   # evan missed the day's last run
            for mp in ("/home", "/scratch"):
                ok = 0 if (host == "aamy" and mp == "/scratch" and h in (5, 6)) else 1
                conn.execute("INSERT INTO workstation_mount_state (timestamp, hostname, mountpoint, is_mounted, "
                             "is_responsive) VALUES (?, ?, ?, 1, ?)", (ts(d, h), host, mp, ok))
    conn.commit()
    K.run(conn, K.settings({}), "by hand", TODAY)
    kept = conn.execute("SELECT substr(timestamp, 12, 2), hostname, mountpoint, is_responsive "
                        "FROM workstation_mount_state ORDER BY 1, 2, 3").fetchall()
    assert kept == [("06", "aamy", "/scratch", 0),                    # the day's last trouble
                    ("22", "evan", "/home", 1), ("22", "evan", "/scratch", 1),     # evan's last reading
                    ("23", "aamy", "/home", 1), ("23", "aamy", "/scratch", 1)]     # the day's last run


# -- collector runs: a count a day ---------------------------------------------------------------

def test_collector_runs_counted_a_day(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    conn.execute("CREATE TABLE collection_log (id INTEGER PRIMARY KEY AUTOINCREMENT, collector TEXT NOT NULL, "
                 "started_at DATETIME NOT NULL, completed_at DATETIME, success BOOLEAN, records_collected INTEGER, "
                 "error_message TEXT)")
    conn.execute("CREATE INDEX idx_collection_log_collector ON collection_log(collector, started_at)")
    for day in (200, 199, 10):
        d = TODAY - timedelta(days=day)
        for h in range(4):
            fail = day == 199 and h == 2
            conn.execute("INSERT INTO collection_log (collector, started_at, success, records_collected, "
                         "error_message) VALUES ('disk', ?, ?, ?, ?)",
                         (ts(d, h), 0 if fail else 1, 0 if fail else 5, "df timed out" if fail else None))
    conn.commit()
    K.run(conn, K.settings({}), "by hand", TODAY)
    days = conn.execute(f"SELECT collector, day, runs, failures, records, last_error FROM {K.DAILY_LOG} "
                        "ORDER BY day").fetchall()
    assert days == [("disk", str(TODAY - timedelta(days=200)), 4, 0, 20, None),
                    ("disk", str(TODAY - timedelta(days=199)), 4, 1, 15, "df timed out")]
    left = [r[0][:10] for r in conn.execute("SELECT started_at FROM collection_log ORDER BY started_at")]
    assert left == [str(TODAY - timedelta(days=200))] + [str(TODAY - timedelta(days=10))] * 4   # its first run


# -- going through it ----------------------------------------------------------------------------

def test_a_table_past_the_limit_waits(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    mpstat(conn, TODAY - timedelta(days=95), TODAY - timedelta(days=91))
    (r,) = K.run(conn, K.settings({}), "daily", TODAY, limit=100)
    assert r["table"] == "mpstat_core" and "nomad db keep --apply" in r["note"] and r["days"] == 0
    assert n(conn, "mpstat_core") == 5 * 48 * 2


def test_out_of_the_change_log_first(tmp_path):
    from nomad.db import delta_site as D
    conn = sqlite3.connect(tmp_path / "s.db", isolation_level=None)
    mpstat(conn, TODAY - timedelta(days=92), TODAY - timedelta(days=91))
    conn.execute("CREATE TABLE jobs (job_id TEXT PRIMARY KEY, user_name TEXT)")
    D.install(conn)
    on = set(D._our_triggers(conn).values())
    assert {"mpstat_core", "jobs"} <= on
    conn.isolation_level = ""
    assert K.untrack(conn, ["mpstat_core"]) == ["mpstat_core"]
    on = set(D._our_triggers(conn).values())
    assert "mpstat_core" not in on and "jobs" in on                       # the hub sends that table whole
    K.run(conn, K.settings({}), "by hand", TODAY)
    assert n(conn, D.LOG) == 0                                            # nothing logged one by one
    plain = sqlite3.connect(tmp_path / "p.db")
    assert K.untrack(plain, ["mpstat_core"]) == []                         # no log: nothing to take out


def test_rows_found_without_an_index_on_their_time(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    conn.execute("CREATE TABLE workstation_mount_state (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, "
                 "hostname TEXT, mountpoint TEXT, is_mounted INTEGER, is_responsive INTEGER)")
    for k in range(300):
        d = TODAY - timedelta(days=150 - k // 2)
        conn.execute("INSERT INTO workstation_mount_state (timestamp, hostname, mountpoint, is_mounted, "
                     "is_responsive) VALUES (?, 'aamy', '/home', 1, 1)", (ts(d, k % 2),))
    conn.execute("DELETE FROM workstation_mount_state WHERE id BETWEEN 40 AND 60")     # a gap in row numbers
    conn.commit()
    rule = K.BY_TABLE["workstation_mount_state"]
    d = TODAY - timedelta(days=120)
    w = K.Window(conn, rule, d, d + timedelta(days=1))
    got = conn.execute(f"SELECT timestamp FROM workstation_mount_state WHERE {w.where(rule)}", w.args).fetchall()
    assert [g[0][:10] for g in got] == [str(d)] * 2
    K.run(conn, K.settings({}), "by hand", TODAY)
    # 60 days past 90, ten of them wholly in the gap: a row a day for the other 50
    assert n(conn, "workstation_mount_state", "timestamp < ?", (str(TODAY - timedelta(days=90)),)) == 50


# -- the collector, once a day after a run by hand ---------------------------------------------

def test_the_collector_once_a_day_after_a_run_by_hand(tmp_path):
    from nomad.collectors.databases import DatabasesCollector
    db = tmp_path / "site.db"
    today = date.today()
    conn = sqlite3.connect(db)
    mpstat(conn, today - timedelta(days=93), today - timedelta(days=92))
    conn.close()
    col = DatabasesCollector({"every_hours": 0}, db)
    now = datetime.now() + timedelta(hours=21)
    c = sqlite3.connect(db)
    assert col._keep(c, now) == ""                         # never by hand here: left alone
    assert n(c, "mpstat_core") == 2 * 48 * 2
    K.run(c, K.settings({}), "by hand", today - timedelta(days=5))       # by hand, nothing due then
    assert col._keep(c, datetime.now() + timedelta(hours=2)) == ""        # not a day since
    assert "older readings rolled up" in col._keep(c, now)
    assert n(c, "mpstat_core") == 2 * 24 * 2
    assert col._keep(c, now + timedelta(hours=6)) == ""     # once a day
    c.close()


# -- nomad db keep ---------------------------------------------------------------------------------

def run_cli(args, db, config=None):
    from nomad.db_cli import db_group
    return CliRunner().invoke(db_group, args, obj={"config": config or {}, "db_path": str(db)})


def test_db_keep(tmp_path):
    db = tmp_path / "site.db"
    conn = sqlite3.connect(db)
    mpstat(conn, TODAY - timedelta(days=95), TODAY - timedelta(days=1), every=60)
    conn.close()
    out = run_cli(["keep"], db)
    assert out.exit_code == 0, out.output
    assert "mpstat_core (CPU busy per core): 240 rows due (to hours" in out.output and "--apply" in out.output
    out = run_cli(["keep", "--apply"], db)
    assert out.exit_code == 0, out.output
    assert "mpstat_core: 240 -> 240 rows (5 days" in out.output                # one a core and hour already
    assert "Vacuumed (full)" in out.output
    c = sqlite3.connect(db)
    assert K.ran_by_hand(c) and c.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    c.close()
    out = run_cli(["keep", "--apply", "--no-vacuum"], db)
    assert "Nothing due." in out.output


def test_db_keep_is_never_for_the_combined_database(tmp_path, monkeypatch):
    combined = tmp_path / "combined.db"
    sqlite3.connect(combined).close()
    monkeypatch.setattr(H, "hub_db", lambda config: combined)
    out = run_cli(["keep"], combined)
    assert out.exit_code != 0 and "follows the sites" in out.output


# -- --sites: each site's latest reading ---------------------------------------------------------

def test_sites_lists_each_sites_latest_reading(tmp_path, monkeypatch):
    hub = tmp_path / "combined.db"
    c = sqlite3.connect(hub)
    H.ensure(c)
    c.execute("ALTER TABLE db_files ADD COLUMN source_site TEXT")
    c.execute("ALTER TABLE db_tables ADD COLUMN source_site TEXT")
    for t, path, role, b, site in (("2026-11-23T13:10:00", "/d/arachne.db", "site", 6_260_000_000, "arachne"),
                                   ("2026-11-23T13:10:00", "/d/nomad.db", "empty", 0, "arachne"),
                                   ("2026-11-23T19:10:00", "/d/arachne.db", "site", 2_590_000_000, "arachne"),
                                   ("2026-11-23T13:10:00", "/d/vadur.db", "site", 110_800_000, "vadur")):
        c.execute("INSERT INTO db_files (timestamp, path, role, bytes, free_bytes, disk_free, source_site) "
                  "VALUES (?,?,?,?,0,1,?)", (t, path, role, b, site))
    c.commit()
    c.close()
    monkeypatch.setattr(H, "hub_db", lambda config: hub)
    out = run_cli(["status", "--sites"], tmp_path / "site.db")
    assert out.exit_code == 0, out.output
    lines = out.output.splitlines()
    assert len(lines) == 2 and "2.59 GB" in lines[0] and "nomad.db" not in out.output and "vadur.db" in lines[1]


@pytest.mark.parametrize("width, prefix", [(13, "2026-08-01T05"), (10, "2026-08-01")])
def test_stamps(tmp_path, width, prefix):
    conn = sqlite3.connect(tmp_path / "s.db")
    mpstat(conn, date(2026, 8, 1), date(2026, 8, 1), cores=1, every=720)
    rule = K.BY_TABLE["mpstat_core"]
    w = K.Window(conn, rule, date(2026, 8, 1), date(2026, 8, 2))
    with conn:
        K._averages(conn, rule, K._cols(conn, "mpstat_core"), w, width)
    stamps = [r[0] for r in conn.execute("SELECT timestamp FROM mpstat_core ORDER BY timestamp")]
    assert stamps == (["2026-08-01T00:00:00", "2026-08-01T12:00:00"] if width == 13 else ["2026-08-01T00:00:00"])
    assert prefix[:10] == stamps[0][:10]

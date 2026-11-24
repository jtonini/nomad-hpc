# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.64.0: keep rules, part 2. The tables reports read over terms and years
keep 13 months in full, then one reading every 15 minutes, as it was;
people's sessions on lab machines keep what makes their totals exact;
sessions on interactive servers (where their daily totals are kept) and
programs run on lab machines are then removed. A rule new to a database
waits for nomad db keep --apply there."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from click.testing import CliRunner

from nomad import keep_rules as K

TODAY = date.today()
OLD = TODAY - timedelta(days=400)       # past report_detail_days (395)
NEW = TODAY - timedelta(days=100)       # past detail_days (90), not past 395


def at(d: date, minute: int) -> str:
    return (datetime(d.year, d.month, d.day) + timedelta(minutes=minute)).isoformat()


def node_state(c, days=(OLD, NEW), nodes=("n1", "n2", "n3"), every=5, site=None):
    c.execute("CREATE TABLE IF NOT EXISTS node_state (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME "
              "NOT NULL, node_name TEXT NOT NULL, state TEXT, cpus_total INTEGER, cpus_alloc INTEGER, cpu_load REAL, "
              "cluster TEXT" + (", source_site TEXT" if site else "") + ")")
    c.execute("CREATE INDEX IF NOT EXISTS idx_node_state_ts ON node_state(timestamp)")
    rows = []
    for d in days:
        for m in range(0, 24 * 60, every):
            for i, n in enumerate(nodes):
                state = "down" if 600 <= m < 640 else ("mixed" if (m // every + i) % 3 else "idle")
                rows.append((at(d, m), n, state, 64, 32 if state == "mixed" else 0, 10.0, "c1")
                            + ((site,) if site else ()))
    c.executemany("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, cpu_load, cluster"
                  + (", source_site" if site else "") + ") VALUES (?,?,?,?,?,?,?" + (",?" if site else "") + ")",
                  rows)
    c.commit()


def count(c, table, d):
    return c.execute(f"SELECT COUNT(*) FROM {table} WHERE timestamp >= ? AND timestamp < ?",
                     (d.isoformat(), (d + timedelta(days=1)).isoformat())).fetchone()[0]


def test_the_settings():
    s = K.settings({})
    assert (s.report_detail_days, s.sample_minutes) == (395, 15)
    assert s.days("node_state")[0] == 395 and s.days("mpstat_core")[0] == 90
    s = K.settings({"report_detail_days": 500, "sample_minutes": 7,
                    "keep": {"gpu_stats": {"detail_days": 800}, "vmstat": {}}})
    assert s.days("node_state")[0] == 500 and s.days("gpu_stats")[0] == 800 and s.days("vmstat")[0] == 500
    assert s.sample_minutes == 15 and any("sample_minutes = 7" in p for p in s.problems)


def test_nodes_by_whole_checks_every_15_minutes(tmp_path):
    c = sqlite3.connect(tmp_path / "site.db")
    node_state(c)
    K.run(c, K.settings({}), "by hand", TODAY)
    assert count(c, "node_state", OLD) == 24 * 4 * 3 and count(c, "node_state", NEW) == 24 * 12 * 3
    # each moment kept has every node: a count of nodes down still means what it did
    per = {n for (n,) in c.execute("SELECT COUNT(*) FROM node_state GROUP BY timestamp")}
    assert per == {3}
    kept = [m for (m,) in c.execute("SELECT DISTINCT substr(timestamp, 15, 2) FROM node_state WHERE timestamp < ?",
                                    ((OLD + timedelta(days=1)).isoformat(),))]
    assert sorted(kept) == ["00", "15", "30", "45"]
    # the outage of 10:00-10:40 is still there, at 15 minutes
    down = c.execute("SELECT COUNT(DISTINCT timestamp) FROM node_state WHERE state = 'down' AND timestamp < ?",
                     ((OLD + timedelta(days=1)).isoformat(),)).fetchone()[0]
    assert down == 3


def test_a_share_read_from_the_samples_stays_about_the_same(tmp_path):
    c = sqlite3.connect(tmp_path / "site.db")
    node_state(c, days=(OLD,))
    q = "SELECT AVG(1.0 * cpus_alloc / cpus_total), AVG(state = 'idle') FROM node_state"
    before = c.execute(q).fetchone()
    K.run(c, K.settings({}), "by hand", TODAY)
    after = c.execute(q).fetchone()
    assert abs(before[0] - after[0]) < 0.02 and abs(before[1] - after[1]) < 0.02


def test_gpus_each_on_its_own(tmp_path):
    c = sqlite3.connect(tmp_path / "site.db")
    c.execute("CREATE TABLE gpu_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL, "
              "node_name TEXT, gpu_index INTEGER, gpu_util_percent REAL, power_draw_w REAL)")
    c.executemany("INSERT INTO gpu_stats (timestamp, node_name, gpu_index, gpu_util_percent, power_draw_w) "
                  "VALUES (?,?,?,?,?)", [(at(OLD, m), "g1", g, 50.0, 200.0) for m in range(24 * 60) for g in (0, 1)])
    c.commit()
    K.run(c, K.settings({}), "by hand", TODAY)
    assert c.execute("SELECT gpu_index, COUNT(*) FROM gpu_stats GROUP BY 1").fetchall() == [(0, 96), (1, 96)]


def test_sessions_on_lab_machines_keep_their_totals(tmp_path):
    c = sqlite3.connect(tmp_path / "site.db")
    c.execute("CREATE TABLE workstation_user_snapshot (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME "
              "NOT NULL, hostname TEXT NOT NULL, username TEXT NOT NULL, uid INTEGER NOT NULL, session_epoch INTEGER, "
              "cpu_usage_usec INTEGER, memory_anon_bytes INTEGER)")
    rows = [(at(OLD, m), "ws1", "kim", 1001, 7, (m - 482) * 60_000_000, 5 * 2 ** 30 if m == 607 else 2 ** 30)
            for m in range(482, 1200, 5)]
    c.executemany("INSERT INTO workstation_user_snapshot (timestamp, hostname, username, uid, session_epoch, "
                  "cpu_usage_usec, memory_anon_bytes) VALUES (?,?,?,?,?,?,?)", rows)
    c.commit()
    q = ("SELECT MIN(timestamp), MAX(timestamp), MAX(cpu_usage_usec) - MIN(cpu_usage_usec), "
         "MAX(memory_anon_bytes) FROM workstation_user_snapshot")
    before = c.execute(q).fetchone()
    K.run(c, K.settings({}), "by hand", TODAY)
    assert c.execute(q).fetchone() == before
    n = c.execute("SELECT COUNT(*) FROM workstation_user_snapshot").fetchone()[0]
    assert n < len(rows) / 2


def interactive(c, servers=("srv1", "srv2"), daily_from=None):
    c.execute("CREATE TABLE interactive_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME NOT NULL, "
              "server_id TEXT NOT NULL, user TEXT NOT NULL, session_type TEXT NOT NULL)")
    c.executemany("INSERT INTO interactive_sessions (timestamp, server_id, user, session_type) VALUES (?,?,?,?)",
                  [(at(OLD, m), s, "pat", "jupyter") for m in range(0, 600, 5) for s in servers])
    c.execute("CREATE TABLE interactive_daily (day TEXT NOT NULL, server_id TEXT NOT NULL, user TEXT NOT NULL, "
              "session_type TEXT NOT NULL, session_seconds REAL)")
    if daily_from:
        c.execute("INSERT INTO interactive_daily VALUES (?, 'srv1', 'pat', 'jupyter', 3600)", (daily_from.isoformat(),))
    c.commit()


def test_interactive_sessions_removed_where_daily_totals_are_kept(tmp_path):
    c = sqlite3.connect(tmp_path / "site.db")
    interactive(c, daily_from=OLD - timedelta(days=30))
    K.run(c, K.settings({}), "by hand", TODAY)
    assert c.execute("SELECT server_id, COUNT(*) FROM interactive_sessions GROUP BY 1").fetchall() == [("srv2", 120)]


def test_but_not_before_those_totals_began(tmp_path):
    c = sqlite3.connect(tmp_path / "site.db")
    interactive(c, daily_from=OLD + timedelta(days=1))
    K.run(c, K.settings({}), "by hand", TODAY)
    assert c.execute("SELECT COUNT(*) FROM interactive_sessions").fetchone()[0] == 240


def test_programs_run_on_lab_machines_removed(tmp_path):
    c = sqlite3.connect(tmp_path / "site.db")
    c.execute("CREATE TABLE workstation_process_record (id INTEGER PRIMARY KEY AUTOINCREMENT, hostname TEXT NOT NULL, "
              "username TEXT NOT NULL, pid INTEGER, command TEXT, start_time INTEGER, ingested_at DATETIME)")
    c.executemany("INSERT INTO workstation_process_record (hostname, username, pid, command, start_time, ingested_at) "
                  "VALUES ('ws1', 'lee', ?, 'python', 0, ?)",
                  [(i, f"{d.isoformat()} 12:00:{i:02d}") for d in (OLD, NEW) for i in range(10)])
    c.commit()
    K.run(c, K.settings({}), "by hand", TODAY)
    assert c.execute("SELECT COUNT(*) FROM workstation_process_record").fetchone()[0] == 10


def test_rows_out_of_order_found_by_their_time(tmp_path):
    """On a copy of the hub's database, a site's rows can come after newer
    ones (a whole copy): with an index on the time, they are found."""
    c = sqlite3.connect(tmp_path / "copy.db")
    node_state(c, days=(NEW,), site="hpc1")
    node_state(c, days=(OLD,), site="hpc2")
    K.run(c, K.settings({}), "by hand", TODAY)
    assert c.execute("SELECT source_site, COUNT(*) FROM node_state GROUP BY 1").fetchall() == [
        ("hpc1", 24 * 12 * 3), ("hpc2", 24 * 4 * 3)]


def test_a_new_rule_waits_for_a_run_by_hand(tmp_path):
    from nomad.collectors.databases import DatabasesCollector
    db = tmp_path / "site.db"
    c = sqlite3.connect(db)
    node_state(c, days=(OLD,))
    K.ensure(c)
    # as 1.63's nomad db keep --apply left it: part 1 gone through
    c.execute(f"INSERT INTO {K.RUNS} (timestamp, how, tbl, days, rows_before, rows_after, seconds) "
              "VALUES (?, 'by hand', '-', 0, 0, 0, 0)", ((datetime.now() - timedelta(days=2)).isoformat(),))
    c.commit()
    assert "node_state" not in K.covered(c) and "mpstat_core" in K.covered(c)
    col = DatabasesCollector.__new__(DatabasesCollector)
    col.k = K.settings({})
    col._keep(c, datetime.now())
    assert count(c, "node_state", OLD) == 24 * 12 * 3                # left as it was
    K.run(c, K.settings({}), "by hand", TODAY)
    assert "node_state" in K.covered(c) and count(c, "node_state", OLD) == 24 * 4 * 3


def cli(args, db):
    from nomad.db_cli import db_group
    return CliRunner().invoke(db_group, args, obj={"config": {}, "db_path": str(db)})


def test_db_keep_says_when_the_first_day_is_due(tmp_path):
    db = tmp_path / "site.db"
    c = sqlite3.connect(db)
    node_state(c, days=(NEW,))
    c.close()
    out = cli(["keep"], db)
    assert out.exit_code == 0, out.output
    due = NEW + timedelta(days=395)
    assert f"node_state (cluster nodes' state): nothing due (full detail 395 days; its first day, {NEW}, is due {due})" \
        in out.output
    assert "New here, left by the daily run until nomad db keep --apply goes through them once: node_state." \
        in out.output


def test_db_keep_says_what_it_would_do(tmp_path):
    db = tmp_path / "site.db"
    c = sqlite3.connect(db)
    node_state(c, days=(OLD,))
    c.close()
    out = cli(["keep"], db)
    end = TODAY - timedelta(days=396)
    assert f"node_state (cluster nodes' state): 864 rows due (to one reading every 15 minutes, {OLD} to {end})" \
        in out.output


def test_the_term_report_weighs_each_day_by_its_time(tmp_path):
    """A period with older days thinned and newer ones in full: each day's
    readings stand for the time they cover, so the period's figures stay."""
    from nomad.usage import sources
    from nomad.usage.config import ClusterConfig
    days = [TODAY - timedelta(days=k) for k in range(12, 0, -1)]
    c = sqlite3.connect(tmp_path / "site.db")
    node_state(c, days=days)
    # the second day: every node down all day
    c.execute("UPDATE node_state SET state = 'down', cpus_alloc = 0 WHERE timestamp >= ? AND timestamp < ?",
              (days[1].isoformat(), days[2].isoformat()))
    c.commit()
    t0 = datetime(days[0].year, days[0].month, days[0].day)
    t1 = t0 + timedelta(days=12)

    def figures():
        d = sources.Data(cfg=ClusterConfig(name="hpc1"), t0=t0, t1=t1, source="", db_label=None, site=None)
        sources._node_samples(c, d)
        hours = sum(v[3] for v in d.node_months.values())
        alloc = sum(v[1] for v in d.node_months.values()) / hours
        down = sum(cl.get("down", 0) for cl in d.node_state_classes.values())
        return alloc, down
    before = figures()
    K.run(c, K.Settings(report_detail_days=6), "check", TODAY)
    assert count(c, "node_state", days[0]) == 24 * 4 * 3 and count(c, "node_state", days[-1]) == 24 * 12 * 3
    after = figures()
    assert abs(after[0] - before[0]) / before[0] < 0.02
    # 3 node-days down on the second day, and 40 minutes each day (the helper's)
    assert abs(before[1] - (3 * 24 + 11 * 3 * 40 / 60)) < 2
    assert abs(after[1] - before[1]) / before[1] < 0.03

# SPDX-License-Identifier: AGPL-3.0-or-later
"""1.61.0: nomad's own databases -- the databases collector (sizes, growth,
free space, old copies; a growth alert; weekly incremental vacuum) and
`nomad db status | tidy | vacuum`."""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from click.testing import CliRunner

from nomad import db_health as H


def _db(path: Path, jobs=(), extra_rows: int = 0, incremental: bool = False):
    c = sqlite3.connect(path)
    if incremental:
        c.execute("PRAGMA auto_vacuum = INCREMENTAL")
    c.execute("CREATE TABLE jobs (job_id TEXT, cluster TEXT, submit_time TEXT, PRIMARY KEY (job_id, cluster))")
    c.execute("CREATE TABLE cluster_usage (cluster TEXT, month TEXT, tres TEXT, collected_at TEXT, "
              "PRIMARY KEY (cluster, month, tres))")
    c.execute("CREATE TABLE mpstat_core (timestamp TEXT, cpu INTEGER, busy REAL)")
    c.executemany("INSERT INTO jobs VALUES (?, 'c1', '2026-10-01T10:00:00')", [(j,) for j in jobs])
    c.execute("INSERT INTO cluster_usage VALUES ('c1', '2026-10', 'cpu', '2026-10-31T00:00:00')")
    c.executemany("INSERT INTO mpstat_core VALUES ('2026-10-01T10:00:00', ?, 0.5)", [(i,) for i in range(extra_rows)])
    c.commit()
    c.close()
    return path


def _old(path: Path, days: int = 30):
    t = time.time() - days * 86400
    os.utime(path, (t, t))


def _config(tmp_path, hub=False):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    cfg = {"general": {"data_dir": str(data)}, "database": {"path": "site.db"}}
    if hub:
        cfg["hub"] = {"output_db": str(data / "combined.db"), "sites": [{"name": "c1", "host": "c1"}]}
    return cfg, data


def test_settings():
    s = H.settings({"every_hours": 3, "incremental_vacuum": False})
    assert s.every_hours == 3 and not s.incremental_vacuum and not s.problems
    bad = H.settings({"every_hours": -1, "stray_days": 2.5, "incremental_vacuum": "yes"})
    assert len(bad.problems) == 3 and bad.every_hours == 6 and bad.stray_days == 14


def test_which_databases(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cfg, data = _config(tmp_path, hub=True)
    _db(data / "site.db", ["1", "2"])
    _db(data / "combined.db", ["1"])
    _db(data / "lab_check.db")
    _db(data / "site.before-x.db", ["1"])
    _old(data / "site.before-x.db")
    sqlite3.connect(data / "nomad.db").execute("CREATE TABLE jobs (job_id TEXT)").connection.commit()
    _old(data / "nomad.db")
    _db(data / "scratch.db")
    roles = {p.name: role for p, role in H.files(cfg)}
    assert roles == {"site.db": "site", "combined.db": "hub", "lab_check.db": "helper",
                     "site.before-x.db": "old copy", "nomad.db": "empty", "scratch.db": "other"}


def test_the_collector_reads_and_warns(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    from nomad.collectors.databases import DatabasesCollector
    cfg, data = _config(tmp_path)
    site = _db(data / "site.db", ["1"], extra_rows=200)
    sent = []
    import nomad.alerts.dispatcher as D
    monkeypatch.setattr(D, "send_alert", lambda **kw: sent.append(kw) or {})
    c = DatabasesCollector({**cfg, "alert_growth_mb": 1}, site)
    rows = c.collect()
    assert [r["role"] for r in rows] == ["site"] and "1 database(s)" in c.note
    assert c.collect() == [] and "read less than 6 h ago" in c.note
    conn = sqlite3.connect(site)
    got = conn.execute("SELECT COUNT(*), MAX(tables) FROM db_files").fetchone()
    assert got[0] == 1 and got[1] >= 3
    assert conn.execute("SELECT max_rowid FROM db_tables WHERE name = 'mpstat_core'").fetchone()[0] == 200
    # a week of readings, then a day that grew 5 MB against 0.1 MB usual
    path = str(site.resolve())
    now = datetime.now()
    conn.execute("DELETE FROM db_files")
    for d in range(10, 0, -1):
        conn.execute("INSERT INTO db_files (timestamp, path, role, bytes) VALUES (?, ?, 'site', ?)",
                     ((now - timedelta(days=d)).isoformat(timespec="seconds"), path, 10_000_000 + (10 - d) * 100_000))
    conn.execute("INSERT INTO db_files (timestamp, path, role, bytes) VALUES (?, ?, 'site', ?)",
                 (now.isoformat(timespec="seconds"), path, 10_000_000 + 900_000 + 5_000_000))
    conn.commit()
    s = H.settings({"alert_growth_mb": 1})
    last, usual = H.unusual_growth(conn, path, s)
    assert last == 5_000_000 and usual == 100_000
    assert H.growth(conn, path) is not None
    assert H.unusual_growth(conn, path, H.settings({"alert_growth_mb": 10})) is None      # under the floor
    n = c._growth_alerts(conn, [{"path": path, "role": "site", "bytes": 15_900_000, "disk_free": 10 ** 9}])
    assert n == 1 and sent[0]["source"] == "database_growth" and sent[0]["subject"] == path
    assert "grew 5.0 MB in a day, against 0.1 MB on a usual day" in sent[0]["message"]
    conn.close()


def test_weekly_incremental_vacuum(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    from nomad.collectors.databases import DatabasesCollector
    cfg, data = _config(tmp_path)
    site = _db(data / "site.db", incremental=True)
    c = sqlite3.connect(site)
    c.execute("CREATE TABLE big (x BLOB)")
    c.executemany("INSERT INTO big VALUES (?)", [(os.urandom(4096),) for _ in range(20000)])
    c.commit()
    c.execute("DELETE FROM big")
    c.commit()
    c.close()
    before = site.stat().st_size
    col = DatabasesCollector(cfg, site)
    col.collect()
    assert site.stat().st_size < before / 2 and "given back" in col.note
    v = sqlite3.connect(site).execute("SELECT kind, bytes_before > bytes_after FROM db_vacuums").fetchall()
    assert v == [("incremental", 1)]


def test_compare(tmp_path):
    live = _db(tmp_path / "live.db", ["1", "2", "3"], extra_rows=10)
    ok = _db(tmp_path / "ok.db", ["1", "2"], extra_rows=5)
    c = sqlite3.connect(ok)
    c.execute("INSERT INTO jobs VALUES ('7_[1-3]', 'c1', '2026-10-01T10:00:00')")
    c.execute("UPDATE cluster_usage SET collected_at = '2026-10-15T00:00:00'")      # updated in place since
    c.commit()
    c.close()
    got = H.compare(live, ok)
    assert got["ok"] and any("placeholder" in n for n in got["notes"])
    more = _db(tmp_path / "more.db", ["1", "9"], extra_rows=20)
    got = H.compare(live, more)
    assert not got["ok"] and "jobs: 1 only in the copy" in got["short"]
    assert any("mpstat_core: 10 fewer rows" in x for x in got["short"])


def test_tidy(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    from nomad.cli import cli
    cfg, data = _config(tmp_path)
    toml = tmp_path / "nomad.toml"
    toml.write_text(f'[general]\ndata_dir = "{data}"\n\n[database]\npath = "site.db"\n')
    _db(data / "site.db", ["1", "2"], extra_rows=10)
    _db(data / "site.before-a.db", ["1"], extra_rows=5)
    _db(data / "site.before-b.db", ["1", "8"], extra_rows=5)
    sqlite3.connect(data / "nomad.db").execute("CREATE TABLE jobs (job_id TEXT)").connection.commit()
    for n in ("site.before-a.db", "site.before-b.db", "nomad.db"):
        _old(data / n)
    run = CliRunner()
    out = run.invoke(cli, ["-c", str(toml), "db", "tidy"])
    assert out.exit_code == 0, out.output
    assert out.output.count("would be removed") == 2 and "KEPT: only in it: jobs: 1 only in the copy" in out.output
    assert (data / "site.before-a.db").exists()
    out = run.invoke(cli, ["-c", str(toml), "db", "tidy", "--apply"])
    assert out.exit_code == 0, out.output
    assert not (data / "site.before-a.db").exists() and not (data / "nomad.db").exists()
    assert (data / "site.before-b.db").exists() and (data / "site.db").exists()
    refused = run.invoke(cli, ["-c", str(toml), "db", "tidy", str(data / "site.db"), "--apply"])
    assert refused.exit_code != 0 and "one nomad uses" in refused.output


def test_status_and_vacuum(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    from nomad.cli import cli
    from nomad.db import delta_site
    cfg, data = _config(tmp_path)
    toml = tmp_path / "nomad.toml"
    toml.write_text(f'[general]\ndata_dir = "{data}"\n\n[database]\npath = "site.db"\n')
    site = _db(data / "site.db", ["1"], extra_rows=3000)
    c = sqlite3.connect(site)
    c.execute("DELETE FROM mpstat_core")
    c.commit()
    delta_site.install(c, hub="hub1")
    c.close()
    run = CliRunner()
    out = run.invoke(cli, ["-c", str(toml), "db", "status", "--tables"])
    assert out.exit_code == 0, out.output
    assert "site.db (site)" in out.output and "vacuuming: none" in out.output and "mpstat_core" in out.output
    out = run.invoke(cli, ["-c", str(toml), "db", "vacuum", "--apply"])
    assert "--full sets it" in out.output
    out = run.invoke(cli, ["-c", str(toml), "db", "vacuum", "--full"])
    assert "Would give back" in out.output
    out = run.invoke(cli, ["-c", str(toml), "db", "vacuum", "--full", "--apply"])
    assert out.exit_code == 0, out.output
    assert "the hub copies this database whole" in out.output
    c = sqlite3.connect(site)
    assert c.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (delta_site.LOG,)).fetchone()
    assert c.execute("SELECT kind FROM db_vacuums").fetchall() == [("full",)]
    c.close()
    out = run.invoke(cli, ["-c", str(toml), "db", "vacuum", "--apply"])
    assert out.exit_code == 0 and "given back" in out.output


def test_status_sites_on_the_hub(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    from nomad.cli import cli
    cfg, data = _config(tmp_path, hub=True)
    toml = tmp_path / "nomad.toml"
    toml.write_text(f'[general]\ndata_dir = "{data}"\n\n[hub]\noutput_db = "{data / "combined.db"}"\n\n'
                    '[[hub.sites]]\nname = "c1"\nhost = "c1"\n')
    hub = sqlite3.connect(data / "combined.db")
    H.ensure(hub)
    hub.execute("ALTER TABLE db_files ADD COLUMN source_site TEXT")
    now = datetime.now()
    for d, b in ((3, 1_000_000_000), (0, 1_300_000_000)):
        hub.execute("INSERT INTO db_files (timestamp, path, role, bytes, free_bytes, disk_free, source_site) "
                    "VALUES (?, '/data/c1.db', 'site', ?, 0, 50000000000, 'c1')",
                    ((now - timedelta(days=d)).isoformat(timespec="seconds"), b))
    hub.commit()
    hub.close()
    out = CliRunner().invoke(cli, ["-c", str(toml), "db", "status", "--sites"])
    assert out.exit_code == 0, out.output
    assert "c1" in out.output and "c1.db" in out.output and "+100.0 MB/day" in out.output

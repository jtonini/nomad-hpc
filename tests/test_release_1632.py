# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.63.2: a whole vacuum puts SQLite's temporary copy beside the database
(SQLITE_TMPDIR, in a Python of its own), checks for room there, and says in
plain words why it wasn't done."""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from nomad import db_health as H


def a_db(path: Path, rows=20000) -> Path:
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE t (a TEXT)")
    c.executemany("INSERT INTO t VALUES (?)", (("x" * 200,) for _ in range(rows)))
    c.commit()
    c.execute("DELETE FROM t WHERE rowid % 2 = 0")
    c.commit()
    c.close()
    return path


def test_a_whole_vacuum_gives_space_back(tmp_path):
    db = a_db(tmp_path / "site.db")
    before, after, had_log = H.full(db)
    assert after < before * 0.7 and had_log is False
    c = sqlite3.connect(db)
    assert c.execute("PRAGMA auto_vacuum").fetchone()[0] == 2 and c.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 10000
    c.close()


def test_and_takes_the_change_log_out(tmp_path):
    from nomad.db import delta_site as D
    db = a_db(tmp_path / "site.db", rows=200)
    c = sqlite3.connect(db, isolation_level=None)
    D.install(c)
    c.close()
    *_, had_log = H.full(db)
    c = sqlite3.connect(db)
    assert had_log and c.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (D.LOG,)).fetchone() is None
    c.close()


def test_its_copy_goes_beside_the_database(tmp_path, monkeypatch):
    db = a_db(tmp_path / "site.db", rows=10)
    seen = {}
    real = subprocess.run

    def run(args, env=None, **kw):
        seen.update(args=args, env=env)
        return real(args, env=env, **kw)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    H.full(db)
    assert seen["env"]["SQLITE_TMPDIR"] == str(tmp_path.resolve())
    assert seen["args"][1] == "-I" and seen["args"][-2] == str(db)


def full_disk(args, env=None, **kw):
    return subprocess.CompletedProcess(args, 1, "", "Traceback ...\nsqlite3.OperationalError: database or disk is full\n")


def test_a_full_disk_said_plainly(tmp_path, monkeypatch):
    db = a_db(tmp_path / "site.db", rows=10)
    monkeypatch.setattr(subprocess, "run", full_disk)
    with pytest.raises(H.VacuumError, match=r"database or disk is full \(the copy and the journal .* need about"):
        H.full(db)


def cli(args, db):
    from nomad.db_cli import db_group
    return CliRunner().invoke(db_group, args, obj={"config": {}, "db_path": str(db)})


def test_db_vacuum_says_why_and_changes_nothing(tmp_path, monkeypatch):
    db = a_db(tmp_path / "site.db", rows=10)
    monkeypatch.setattr(subprocess, "run", full_disk)
    out = cli(["vacuum", "--full", "--apply"], db)
    assert out.exit_code != 0 and "not vacuumed, nothing changed: database or disk is full" in out.output
    assert "Traceback" not in out.output


def test_db_vacuum_wants_room_beside_the_database(tmp_path, monkeypatch):
    db = a_db(tmp_path / "site.db", rows=10)
    real = H.measure
    monkeypatch.setattr(H, "measure", lambda p: {**real(p), "disk_free": 10})
    out = cli(["vacuum", "--full", "--apply"], db)
    assert out.exit_code != 0 and "free beside the database" in out.output


def test_db_keep_goes_on_without_the_vacuum(tmp_path, monkeypatch):
    from datetime import date, datetime, timedelta
    db = tmp_path / "site.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE collection_log (id INTEGER PRIMARY KEY AUTOINCREMENT, collector TEXT NOT NULL, "
              "started_at DATETIME NOT NULL, success BOOLEAN, records_collected INTEGER, error_message TEXT)")
    d = date.today() - timedelta(days=100)
    c.executemany("INSERT INTO collection_log (collector, started_at, success) VALUES ('disk', ?, 1)",
                  [(datetime(d.year, d.month, d.day, h).isoformat(),) for h in range(24)])
    c.commit()
    c.close()
    monkeypatch.setattr(subprocess, "run", full_disk)
    out = cli(["keep", "--apply"], db)
    assert out.exit_code == 0, out.output
    assert "collection_log: 24 -> 1 rows" in out.output
    assert "Not vacuumed, nothing changed by it: database or disk is full" in out.output
    assert "rolled up all the same" in out.output

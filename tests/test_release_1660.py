# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.66.0: no site's names in nomad; request numbers REQ-0012 by default;
nomad analyze reads the disk readings and fits every one; the demo has lab
machines' sessions and disk readings; the notices read a demo database."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

ROOT = Path(__file__).resolve().parents[1]
# the names of the site nomad was first run on, written backwards so this file doesn't hold them
NAMES = [w[::-1] for w in ("rudyps", "enhcara", "rudav", "bewredips", "llehctiminoj", "sugnim", "llewopnedab",
                           "nhguavharas", "dnomhcir", "hsirapc", "gnaym", "nilonk", "snibbora", "hcakubc",
                           "dlanodk")]


def test_no_site_names_in_the_code_tests_or_docs():
    found = []
    for top in ("nomad", "tests", "docs", "scripts"):
        for f in (ROOT / top).rglob("*"):
            if not f.is_file() or "__pycache__" in f.parts or f.suffix in (".pyc", ".png", ".pdf", ".woff2"):
                continue
            try:
                text = f.read_text(errors="strict").lower()
            except (UnicodeDecodeError, PermissionError):
                continue
            for w in NAMES:
                if w in text:
                    found.append(f"{f.relative_to(ROOT)}: {w[:2]}...")
    assert not found, found[:20]


def test_request_numbers_by_default_and_as_a_site_keeps_them():
    from nomad import tickets as T
    assert T.ref(12, T.settings({})) == "REQ-0012"
    kept = T.settings({"console": {"tickets": {"prefix": "ARC"}}})
    assert T.ref(12, kept) == "ARC-0012" and T.number_of("ARC-0012", kept) == 12


def test_the_hub_named_hub_is_the_hub():
    from nomad import hub_watch as W
    fields = {f: None for f in W.Hub.__dataclass_fields__}
    assert W.Hub(**{**fields, "name": "hub"}).label == "the hub"
    assert W.Hub(**{**fields, "name": "hub1"}).label == "the hub hub1"
    assert W.Hub(**{**fields, "name": None}).label == "its hub"


def disks(path: Path, per_day: float, bend: float = 0.0, noise: float = 0.0):
    import random
    rnd = random.Random(3)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE filesystems (id INTEGER PRIMARY KEY, path TEXT NOT NULL, total_bytes INTEGER NOT NULL, "
              "used_bytes INTEGER NOT NULL, available_bytes INTEGER NOT NULL, used_percent REAL NOT NULL, "
              "timestamp DATETIME)")
    tb = 10 ** 12
    start = datetime.now() - timedelta(days=7)
    rows = []
    for i in range(2016):
        d = i / 288
        used = int(50 * tb + per_day * tb * d + bend * tb * d * d + rnd.uniform(-noise, noise) * tb)
        rows.append(("/scratch", 100 * tb, used, 100 * tb - used, 100.0 * used / (100 * tb),
                     (start + timedelta(minutes=5 * i)).isoformat()))
    c.executemany("INSERT INTO filesystems (path, total_bytes, used_bytes, available_bytes, used_percent, timestamp) "
                  "VALUES (?,?,?,?,?,?)", rows)
    c.commit()
    c.close()
    return rows


def test_a_steady_fill_is_a_line_whatever_the_noise(tmp_path):
    from nomad.analysis.derivatives import Trend, analyze_disk_fit
    rows = disks(tmp_path / "d.db", per_day=1.6, noise=0.002)        # 1.6 TB a day, give or take 2 GB a reading
    hist = [{"timestamp": r[5], "used_bytes": r[2]} for r in rows]
    a = analyze_disk_fit(hist, limit_bytes=100 * 10 ** 12)
    assert a.trend == Trend.INCREASING_LINEAR and a.second_derivative is None
    assert abs(a.first_derivative / 1.6e12 - 1) < 0.02
    assert 22 < a.days_until_limit < 26                               # 61.2 TB of 100 used, 1.6 TB a day: ~24 days
    # the analyzer of the last three readings, for comparison: nonsense
    from nomad.analysis.derivatives import analyze_disk_trend
    b = analyze_disk_trend(hist, limit_bytes=100 * 10 ** 12)
    assert b.days_until_limit is None or b.days_until_limit < 5 or abs(b.first_derivative / 1.6e12 - 1) > 0.5


def test_a_fill_that_speeds_up_is_said_so(tmp_path):
    from nomad.analysis.derivatives import Trend, analyze_disk_fit
    rows = disks(tmp_path / "d.db", per_day=0.5, bend=0.3)
    a = analyze_disk_fit([{"timestamp": r[5], "used_bytes": r[2]} for r in rows], limit_bytes=100 * 10 ** 12)
    assert a.trend == Trend.ACCELERATING_GROWTH and a.second_derivative > 0


def cli(*args, home=None):
    from nomad.cli import cli as root
    return CliRunner().invoke(root, list(args), catch_exceptions=False)


def test_nomad_analyze_reads_the_disk_readings(tmp_path):
    disks(tmp_path / "d.db", per_day=1.6)
    r = cli("analyze", "--db", str(tmp_path / "d.db"), "--path", "/scratch", "--hours", "168")
    assert r.exit_code == 0, r.output
    assert "increasing_linear" in r.output and "Days until full" in r.output
    r = cli("analyze", "--db", str(tmp_path / "d.db"), "--path", "/home")
    assert r.exit_code != 0 and "(read: /scratch)" in r.output


def test_without_disk_readings_it_says_so(tmp_path):
    sqlite3.connect(tmp_path / "e.db").execute("CREATE TABLE storage_state (id INTEGER)")
    r = cli("analyze", "--db", str(tmp_path / "e.db"))
    assert r.exit_code != 0 and "no disk readings" in r.output


def test_the_demo_has_lab_sessions_and_disks(tmp_path):
    from nomad.demo import DemoDatabase
    from nomad.workstation_sessions import split
    db = DemoDatabase(str(tmp_path / "demo.db"))
    db.write_workstation_user_snapshot()
    db.write_filesystems()
    c = sqlite3.connect(tmp_path / "demo.db")
    assert c.execute("SELECT COUNT(DISTINCT username) FROM workstation_user_snapshot").fetchone()[0] == 4
    assert {p for (p,) in c.execute("SELECT DISTINCT path FROM filesystems")} == {"/home", "/scratch", "/localscratch"}
    snaps = c.execute("SELECT hostname || char(0) || uid, timestamp, session_epoch, cpu_usage_usec, memory_peak_bytes "
                      "FROM workstation_user_snapshot ORDER BY hostname, uid, timestamp").fetchall()
    long = [s for s in split(snaps) if (datetime.fromisoformat(s[-1][1]) - datetime.fromisoformat(s[0][1]))
            > timedelta(hours=30)]
    assert long                                                       # the run that belongs on a cluster


def test_notices_from_another_database_never_send(tmp_path):
    db = tmp_path / "demo.db"
    sqlite3.connect(db).execute("CREATE TABLE jobs (job_id TEXT)")
    r = cli("notices", "mail", "--db", str(db), "--send")
    assert r.exit_code != 0 and "not with --send" in r.output


def test_gpu_nodes_from_their_readings(tmp_path):
    from nomad.viz import dashboard
    c = sqlite3.connect(tmp_path / "g.db")
    c.execute("CREATE TABLE gpu_stats (node_name TEXT)")
    c.execute("INSERT INTO gpu_stats VALUES ('node05')")
    dashboard._GPU_NODES.clear()
    assert dashboard._gpu_nodes(c) == {"node05"}


@pytest.mark.parametrize("kit", ["gromacs-water", "cp2k-water"])
def test_a_kits_recorded_run_without_the_site(kit):
    from nomad.demos import kits as K
    assert K.recorded_text(K.find(None, kit)).startswith("A run of it (2026-11-09")

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad sync` tags every merged table with its site.

The merge used to add ``source_site`` only to a fixed list of tables, which
left out ``alerts``: a combined database could not say which site raised an
alert, so per-site Insights either showed every site's alerts or none.
"""
import sqlite3
import subprocess
from datetime import datetime

from click.testing import CliRunner

from tests.conftest import _bootstrap_db_with_migrations


def _site_db(path, host):
    _bootstrap_db_with_migrations(str(path))
    now = datetime.now().isoformat()
    with sqlite3.connect(path) as c:
        c.execute("INSERT INTO alerts (timestamp, severity, category, source, message, details)"
                  " VALUES (?, 'warning', 'disk', ?, 'Disk usage at 87% on /scratch', '{}')",
                  (now, host))
        c.execute("INSERT INTO node_state (timestamp, node_name, state, is_healthy)"
                  " VALUES (?, ?, 'IDLE', 1)", (now, f"{host}-n01"))
        c.execute("INSERT INTO per_user_state (hostname, key, kind, seen_at)"
                  " VALUES (?, 'run', 'run', 1.0)", (host,))
        c.execute("INSERT INTO per_user_daily (day, hostname, username, cpu_seconds)"
                  " VALUES ('2026-10-02', ?, 'ann', 60.0)", (host,))


def test_sync_tags_alerts_and_every_table_with_the_site(tmp_path, monkeypatch):
    import nomad.cli as cli_mod

    home = tmp_path / "home"
    cache = home / ".local" / "share" / "nomad" / "sync_cache"
    cache.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _site_db(cache / "alpha.db", "alpha-head")
    _site_db(cache / "beta.db", "beta-head")

    cfg = tmp_path / "sync.toml"
    cfg.write_text(
        '[[sites]]\nname = "alpha"\nhost = "alpha-head"\nssh_user = "x"\n\n'
        '[[sites]]\nname = "beta"\nhost = "beta-head"\nssh_user = "x"\n')

    # No network: the live pull fails and each site's cached copy is used.
    monkeypatch.setattr(cli_mod, "_pull_via_backup", lambda *a, **k: False)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "offline"))

    out = tmp_path / "combined.db"
    result = CliRunner().invoke(cli_mod.cli, ["sync", "-c", str(cfg), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert out.exists(), result.output

    c = sqlite3.connect(out)
    rows = c.execute("SELECT source_site, source FROM alerts ORDER BY source_site").fetchall()
    assert rows == [("alpha", "alpha-head"), ("beta", "beta-head")]
    untagged = []
    for (table,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                              "AND name NOT LIKE 'sqlite_%' AND name != 'sync_sites'"):
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if "source_site" not in cols:
            untagged.append(table)
    assert untagged == []
    # per_user's counters from run to run are the site's working state, not data.
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "per_user_state" not in tables
    assert c.execute("SELECT source_site FROM per_user_daily ORDER BY 1").fetchall() == [
        ("alpha",), ("beta",)]
    indexes = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_alerts_source_site_timestamp" in indexes
    assert "idx_node_state_source_site_timestamp" in indexes
    c.close()

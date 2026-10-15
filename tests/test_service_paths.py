# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""nomad run by a service account (1.12.6): settings in /etc/nomad, no login
shell, the hub's own collectors read without ssh."""
import os
import shutil
import sqlite3
import subprocess
import sys

from click.testing import CliRunner

from nomad.db import pull as P


def _wal_db(path, rows=20):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
    c.executemany("INSERT INTO t (v) VALUES (?)", [("x",)] * rows)
    c.commit()
    return c                         # kept open: the rows stay in the WAL


def _rows(path):
    with sqlite3.connect(path) as c:
        return c.execute("SELECT COUNT(*) FROM t").fetchone()[0]


def test_a_site_on_the_hub_itself_is_read_without_ssh(tmp_path, monkeypatch):
    src = tmp_path / "data" / "nomad.db"
    held = _wal_db(src, 20)
    held.executemany("INSERT INTO t (v) VALUES (?)", [("y",)] * 5)
    held.commit()

    def no_ssh(*a, **k):
        raise AssertionError(f"ran {a}")
    monkeypatch.setattr(P.sp, "run", no_ssh)
    copy = tmp_path / "cache" / "hub.db"
    copy.parent.mkdir()
    for host in ("localhost", "127.0.0.1", "LOCALHOST", "::1"):
        assert P.is_local(host)
        assert P.pull("anyone", host, str(src), copy)
        assert _rows(copy) == 25                      # what the WAL holds too
    assert not P.is_local("headnode") and not P.is_local("")
    assert not list(copy.parent.glob("*.part"))
    # Home-relative paths are this account's.
    monkeypatch.setenv("HOME", str(tmp_path))
    assert P.pull("x", "localhost", "~/data/nomad.db", copy)
    # Missing: said, and the last good copy kept.
    why = []
    assert not P.pull("x", "localhost", str(tmp_path / "none.db"), copy, why=why)
    assert why == [f"no database at {tmp_path / 'none.db'}"] and _rows(copy) == 25
    # Not a database: said, nothing left behind.
    (tmp_path / "junk.db").write_text("not a database")
    why = []
    assert not P.pull("x", "localhost", str(tmp_path / "junk.db"), copy, why=why)
    assert why and "could not copy" in why[0] and _rows(copy) == 25
    assert not list(copy.parent.glob("*.part*"))
    held.close()


def test_without_sqlite3_the_wal_is_checkpointed_before_the_copy(tmp_path, monkeypatch):
    from tests.test_sync_pull import SCP, SSH, _script
    fake = tmp_path / "bin"
    fake.mkdir()
    _script(fake / "ssh", SSH)
    _script(fake / "scp", SCP)
    home = tmp_path / "remote"
    db = home / ".local/share/nomad/site.db"
    held = _wal_db(db, 30)                  # 30 rows, all still in the WAL
    tools = tmp_path / "remote-bin"         # no sqlite3, but python3
    tools.mkdir()
    for tool in ("bash", "sh", "dirname", "find", "id", "stat", "wc", "df", "awk", "rm",
                 "cat", "timeout"):
        if shutil.which(tool):
            (tools / tool).symlink_to(shutil.which(tool))
    (tools / "python3").symlink_to(sys.executable)
    log = tmp_path / "log"
    log.write_text("")
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_REMOTE_PATH", str(tools))
    monkeypatch.setenv("FAKE_REMOTE_HOME", str(home))
    monkeypatch.setenv("FAKE_LOG", str(log))
    copy = tmp_path / "site.db"
    assert P.pull("u", "site", "~/.local/share/nomad/site.db", copy)
    assert any("wal_checkpoint" in line for line in log.read_text().splitlines())
    assert _rows(copy) == 30
    held.close()
    # A site without python3 either: copied as before.
    (tools / "python3").unlink()
    assert P.pull("u", "site", "~/.local/share/nomad/site.db", copy)


def test_sync_reads_the_hub_from_the_settings_in_use_and_merges_a_local_site(tmp_path, monkeypatch):
    import nomad.cli as cli_mod
    from tests.test_sync_pull import _hub_site
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))       # no ~/.config/nomad here
    db = tmp_path / "var" / "nomad.db"
    db.parent.mkdir()
    _hub_site(db, "hub")
    cfg = tmp_path / "etc" / "nomad.toml"
    cfg.parent.mkdir()
    out = tmp_path / "var" / "combined.db"
    cfg.write_text('[collectors.slurm]\npartitions = ["p1", "p2"]\n\n'
                   f'[hub]\noutput_db = "{out}"\n\n[[hub.sites]]\nname = "hub"\n'
                   f'host = "localhost"\ndb_path = "{db}"\n')
    hub = cli_mod._load_hub_config(config_path=str(cfg))
    assert hub["sites"][0]["host"] == "localhost" and hub["source"] == f"{cfg} [hub]"
    assert cli_mod._load_hub_config(config_path=str(tmp_path / "none.toml")) is None
    # --config-file takes a nomad.toml with [hub] as well as a sync.toml.
    assert cli_mod._load_hub_config(config_file_override=str(cfg))["sites"][0]["name"] == "hub"
    r = CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "sync", "--dry-run"])
    assert r.exit_code == 0, r.output
    assert f"copy {db} (on this host)" in r.output and "scp" not in r.output

    ran = []
    real_run = subprocess.run

    def record(*a, **k):
        argv = a[0] if a else k.get("args")
        if isinstance(argv, (list, tuple)) and argv and argv[0] in ("ssh", "scp"):
            ran.append(argv)
            return subprocess.CompletedProcess(argv, 255, "", "not here")
        return real_run(*a, **k)
    monkeypatch.setattr(subprocess, "run", record)
    r = CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "sync"])
    assert r.exit_code == 0, r.output
    assert ran == []                            # neither the database nor its settings over ssh
    with sqlite3.connect(out) as c:
        assert c.execute("SELECT COUNT(*) FROM node_state").fetchone()[0] == 1
        assert dict(c.execute("SELECT name, source FROM sync_sites")) == {"hub": "live"}
        # The site's partitions: from the settings in use, as a remote site's
        # come from its own nomad.toml.
        assert c.execute("SELECT partitions FROM sync_sites").fetchone()[0] == "p1,p2"


def test_a_relative_output_db_is_in_the_data_directory(tmp_path, monkeypatch):
    from nomad.actions.remote import hub_settings
    from nomad.config import combined_db_path
    monkeypatch.setenv("HOME", str(tmp_path))
    data = tmp_path / ".local" / "share" / "nomad"
    assert combined_db_path("hub.db") == data / "hub.db"
    assert combined_db_path(None) == data / "combined.db"
    assert combined_db_path("~/x.db") == tmp_path / "x.db"
    assert combined_db_path("/srv/c.db").as_posix() == "/srv/c.db"
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[hub]\noutput_db = "hub.db"\n\n[[hub.sites]]\nname = "a"\nhost = "localhost"\n\n'
                   '[[hub.sites]]\nname = "b"\nhost = "b-head"\n')
    h = hub_settings(cfg)
    assert h["combined_db"] == data / "hub.db"
    assert [(s["name"], s["local"]) for s in h["sites"]] == [("a", True), ("b", False)]


def test_an_action_on_the_hubs_own_site_runs_here(tmp_path, monkeypatch):
    from nomad import actions
    from nomad.actions import remote
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / ".config" / "nomad" / "nomad.toml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('[[hub.sites]]\nname = "hub"\nhost = "localhost"\n\n'
                   '[[hub.sites]]\nname = "far"\nhost = "far-head"\nssh_user = "svc"\n')
    monkeypatch.setattr("nomad.config.DEFAULT_CONFIG_PATHS", [cfg, tmp_path / "etc" / "nomad.toml"])
    asked = []
    monkeypatch.setattr(remote, "run_remote", lambda site, *a, **k: asked.append(site) or {"site": site})
    res = actions.run("version", site="hub")
    assert res["ok"] and res["site"] == "hub" and asked == []
    assert actions.run("version", site="far") == {"site": "far"} and asked == ["far"]


def test_report_settings_beside_the_settings_in_use(tmp_path, monkeypatch):
    from nomad.usage import config as U
    home_cfg = tmp_path / "home" / ".config" / "nomad" / "report.toml"
    system = tmp_path / "etc" / "report.toml"
    monkeypatch.setattr(U, "DEFAULT_PATH", home_cfg)
    monkeypatch.setattr(U, "SYSTEM_PATH", system)
    etc_toml = tmp_path / "etc" / "nomad.toml"
    # None exists: beside the settings in use (where `init` writes it).
    assert U.default_path(etc_toml) == system
    assert U.default_path() == home_cfg
    system.parent.mkdir(parents=True)
    system.write_text("[report]\n")
    assert U.default_path() == system                 # the system's, when it's the one there
    home_cfg.parent.mkdir(parents=True)
    home_cfg.write_text("[report]\n")
    assert U.default_path() == home_cfg               # the account's first
    assert U.default_path(etc_toml) == system         # beside the settings in use first
    assert U.load(None, "x", etc_toml).path == system


def test_the_first_sync_to_a_new_output_db_says_the_old_one_stops(tmp_path, monkeypatch):
    import nomad.cli as cli_mod
    from tests.test_sync_pull import _hub_site
    monkeypatch.setenv("HOME", str(tmp_path))
    old = tmp_path / ".local" / "share" / "nomad" / "combined.db"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"")
    db = tmp_path / "site.db"
    _hub_site(db, "hub")
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f'[hub]\noutput_db = "elsewhere.db"\n\n[[hub.sites]]\nname = "hub"\n'
                   f'host = "localhost"\ndb_path = "{db}"\n')
    r = CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "sync", "--dry-run"])
    assert "no longer updated" in r.output and str(old.parent / "elsewhere.db") in r.output
    (old.parent / "elsewhere.db").write_bytes(b"")
    r = CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "sync", "--dry-run"])
    assert "no longer updated" not in r.output


def test_a_local_sites_relative_path_is_in_the_data_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir("/")                       # where systemd runs it
    held = _wal_db(tmp_path / ".local" / "share" / "nomad" / "rel.db", 3)
    copy = tmp_path / "copy.db"
    assert P.pull("x", "localhost", "rel.db", copy) and _rows(copy) == 3
    held.close()

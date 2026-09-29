# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""One way to find and read nomad.toml, used by the CLI, the dashboards and
the Console alike, plus the [support] settings that replace built-in
addresses."""

import logging

import pytest
from click.testing import CliRunner

import nomad.config as nc


@pytest.fixture
def config_paths(tmp_path, monkeypatch):
    """Point nomad's standard config locations into a temporary directory."""
    user = tmp_path / "user" / "nomad.toml"
    system = tmp_path / "etc" / "nomad.toml"
    user.parent.mkdir()
    system.parent.mkdir()
    monkeypatch.setattr(nc, "DEFAULT_CONFIG_PATHS", [user, system])
    return user, system


def test_read_toml(tmp_path):
    good = tmp_path / "good.toml"
    good.write_text('[mail]\nhost = "x"\nport = 25\n')
    assert nc.read_toml(good) == {"mail": {"host": "x", "port": 25}}
    with pytest.raises(FileNotFoundError):
        nc.read_toml(tmp_path / "missing.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("a = 1\na = 2\n")
    with pytest.raises(Exception):
        nc.read_toml(bad)


def test_load_config_skips_an_unreadable_file_and_says_so(tmp_path, config_paths, caplog):
    user, system = config_paths
    user.write_text("this is = = not toml\n")
    system.write_text('[support]\nemail = "hpc@example.edu"\n')
    with caplog.at_level(logging.WARNING, logger="nomad.config"):
        cfg = nc.load_config()
    assert cfg == {"support": {"email": "hpc@example.edu"}}
    assert "Skipping config" in caplog.text


def test_user_config_wins_over_system(config_paths):
    user, system = config_paths
    user.write_text('who = "user"\n')
    system.write_text('who = "system"\n')
    assert nc.find_config() == user
    assert nc.load_config()["who"] == "user"


def test_dashboard_reads_the_same_file_as_the_cli(config_paths):
    """The dashboards used to look in /etc first; now they agree with nomad."""
    from nomad.viz import server
    user, system = config_paths
    user.write_text('[dashboard]\nport = 9999\n')
    system.write_text('[dashboard]\nport = 1111\n')
    assert server.find_config_file() == user
    cfg = server.load_config()
    assert cfg["dashboard"]["port"] == 9999
    assert cfg["dashboard"]["host"] == "localhost"          # default kept
    # Loading must not change the module's defaults for the next caller.
    assert server.DEFAULT_CONFIG["dashboard"]["port"] == 8050


def test_dashboard_command_passes_the_cli_config(tmp_path, monkeypatch):
    import nomad.viz.server as server
    from nomad.cli import cli
    seen = {}
    monkeypatch.setattr(server, "serve_dashboard",
                        lambda host, port, config_path=None, db_path=None: seen.update(
                            config_path=config_path, db_path=db_path))
    cfg = tmp_path / "site.toml"
    db = tmp_path / "x.db"
    db.write_bytes(b"x")
    cfg.write_text("[general]\n")
    result = CliRunner().invoke(cli, ["-c", str(cfg), "dashboard", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert seen["config_path"] == str(cfg)


def test_issue_institution_comes_from_support():
    from nomad.issue.collector import IssueCollector
    info = IssueCollector(config={"support": {"institution": "Example U"}}).collect()
    assert info.institution == "Example U"


def test_issue_email_without_a_support_address_says_so(capsys):
    from nomad.issue.cli_commands import _submit_email
    _submit_email(client=None, title="t", body="b", category="bug", support_email=None)
    out = capsys.readouterr().out
    assert "No support address is configured" in out
    assert "richmond" not in out

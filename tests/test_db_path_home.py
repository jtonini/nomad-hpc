# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A leading ~ in [general] data_dir or [database] path is the home directory,
not a directory named "~" under wherever nomad runs."""
from pathlib import Path

import pytest

from nomad.cli import get_db_path

HOME = Path.home()


@pytest.mark.parametrize("config, expected", [
    ({"general": {"data_dir": "~/.local/share/nomad"}, "database": {"path": "nomad.db"}},
     HOME / ".local/share/nomad/nomad.db"),
    ({"general": {"data_dir": "~/.local/share/nomad"}}, HOME / ".local/share/nomad/nomad.db"),
    ({"database": {"path": "~/elsewhere/site.db"}}, HOME / "elsewhere/site.db"),
    ({"general": {"data_dir": "/var/lib/nomad"}, "database": {"path": "nomad.db"}},
     Path("/var/lib/nomad/nomad.db")),
    ({}, HOME / ".local/share/nomad/nomad.db"),
])
def test_db_path(config, expected):
    got = get_db_path(config)
    assert got == expected and "~" not in str(got)


def test_the_alert_dispatcher_finds_the_same_database():
    from nomad.alerts.dispatcher import AlertDispatcher
    config = {"general": {"data_dir": "~/.local/share/nomad"}, "database": {"path": "nomad.db"}}
    d = AlertDispatcher(config)
    assert Path(d.db_path) == get_db_path(config)

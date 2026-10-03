# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""per_user state: what one cron run leaves for the next."""
from __future__ import annotations

import sqlite3

from nomad.collectors.per_user import state


def test_session_id_stable_for_same_inputs():
    assert state.make_session_id("h", 100, 1000.4) == state.make_session_id("h", 100, 1000.9)


def test_session_id_differs_when_pid_recycles_with_different_start_time():
    assert state.make_session_id("h", 100, 1000.0) != state.make_session_id("h", 100, 2000.0)


def test_session_id_differs_across_hosts():
    assert state.make_session_id("a", 100, 1000.0) != state.make_session_id("b", 100, 1000.0)


def test_alert_key_format_is_stable():
    assert state.alert_key("h", "abc", "cpu_10pct_5min") == "h|abc|cpu_10pct_5min"


def test_save_replaces_this_hosts_rows_and_load_reads_them_back(db_path):
    with sqlite3.connect(db_path) as c:
        state.save(c, "h", [
            state.StateRow("run", "run", 100.0),
            state.StateRow("p1", "process", 100.0, 12.5, 10, 20,
                           {"cpu_10pct_5min": state.Held(50.0, 30.0, 4096)}),
        ])
        state.save(c, "other", [state.StateRow("run", "run", 90.0)])
    with sqlite3.connect(db_path) as c:
        rows = state.load(c, "h")
    assert set(rows) == {"run", "p1"}
    p1 = rows["p1"]
    assert (p1.cpu_seconds, p1.io_read_bytes, p1.io_write_bytes) == (12.5, 10, 20)
    assert p1.rules == {"cpu_10pct_5min": state.Held(50.0, 30.0, 4096)}

    with sqlite3.connect(db_path) as c:
        state.save(c, "h", [state.StateRow("run", "run", 400.0)])
        assert set(state.load(c, "h")) == {"run"}           # p1 gone: its process ended
        assert set(state.load(c, "other")) == {"run"}       # another host untouched


def test_load_without_the_table_is_empty(tmp_path):
    with sqlite3.connect(tmp_path / "empty.db") as c:
        assert state.load(c, "h") == {}

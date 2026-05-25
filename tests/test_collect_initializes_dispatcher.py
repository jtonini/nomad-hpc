# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Joao Tonini
"""Regression test for the dispatcher-not-initialized bug.

`nomad collect` runs collectors that may call send_alert() (via the
node_state collector's _dispatch_state_alerts). send_alert() looks up
a global _dispatcher set by init_dispatcher(). If init_dispatcher()
is never called, send_alert() silently returns {} with only a
warning-level log.

Production symptom on 2026-05-20: arachne node03 was MIXED+DRAIN for
16+ hours. The node_state collector captured it every 5 minutes. But
the alerts table had exactly one row (a manual test alert from the
night before) and no email was ever sent. Root cause: nomad collect
never called init_dispatcher().

These tests guard the invariant: after `nomad collect` initializes,
the global dispatcher must exist so any collector's send_alert() call
reaches a real dispatcher.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner


@pytest.fixture
def minimal_config(tmp_path):
    """Minimal nomad.toml with one collector enabled and SMTP configured."""
    config = tmp_path / "nomad.toml"
    config.write_text(f"""
[general]
data_dir = "{tmp_path}"
log_level = "warning"

[database]
path = "test.db"

[collectors]
enabled = ["disk"]

[collectors.disk]
enabled = true
filesystems = ["/tmp"]

[alerts]
enabled = true
min_severity = "warning"

[alerts.email]
enabled = true
smtp_server = "127.0.0.1"
smtp_port = 25
use_tls = false
from_address = "test@example.invalid"
recipients = ["nobody@example.invalid"]
""")
    return config


def test_collect_calls_init_dispatcher(minimal_config):
    """REGRESSION: nomad collect must call init_dispatcher() so that
    collectors' send_alert() calls reach a real dispatcher.

    Before this fix, init_dispatcher was only called from inside
    ThresholdChecker.__init__(), and nomad collect never instantiated
    a ThresholdChecker. _dispatcher stayed None for the entire daemon
    lifetime. Production drains generated zero alerts.
    """
    from nomad.cli import cli
    import nomad.alerts as alerts_module

    real_init = alerts_module.init_dispatcher

    with patch.object(alerts_module, "init_dispatcher",
                      wraps=real_init) as spy:
        runner = CliRunner()
        result = runner.invoke(cli, [
            "-c", str(minimal_config),
            "collect", "--once",
        ])

    assert result.exit_code == 0, (
        f"collect --once exited with {result.exit_code}. "
        f"Output:\n{result.output}"
    )
    assert spy.called, (
        "init_dispatcher() was never called from nomad collect. "
        "This means send_alert() from any collector will silently no-op. "
        f"Output was:\n{result.output}"
    )


def test_global_dispatcher_exists_after_collect(minimal_config):
    """REGRESSION: after nomad collect runs setup, get_dispatcher()
    must return a real dispatcher instance (not None).
    """
    import nomad.alerts.dispatcher as dispatcher_module
    dispatcher_module._dispatcher = None

    from nomad.cli import cli
    from nomad.alerts import get_dispatcher

    assert get_dispatcher() is None, (
        "Test setup error: dispatcher was already initialized before collect"
    )

    runner = CliRunner()
    result = runner.invoke(cli, [
        "-c", str(minimal_config),
        "collect", "--once",
    ])

    assert result.exit_code == 0, (
        f"collect --once exited with {result.exit_code}. "
        f"Output:\n{result.output}"
    )

    dispatcher = get_dispatcher()
    assert dispatcher is not None, (
        "After nomad collect, get_dispatcher() still returns None. "
        "This means init_dispatcher() was never called and send_alert() "
        "from any collector will silently no-op.\n"
        f"Command output was:\n{result.output}"
    )

    assert dispatcher.backends, (
        "Dispatcher exists but has zero backends. "
        "Expected EmailBackend to be initialized from [alerts.email]."
    )
    backend_names = [b.__class__.__name__ for b in dispatcher.backends]
    assert "EmailBackend" in backend_names, (
        f"Expected EmailBackend in dispatcher, got: {backend_names}"
    )


def test_dispatcher_init_failure_does_not_crash_collect(tmp_path):
    """If [alerts] config is malformed, collect must continue.

    Alerts are notifications, not load-bearing for data collection.
    A broken SMTP config should not prevent metric collection.
    """
    config = tmp_path / "nomad.toml"
    config.write_text(f"""
[general]
data_dir = "{tmp_path}"

[database]
path = "test.db"

[collectors]
enabled = ["disk"]

[collectors.disk]
enabled = true
filesystems = ["/tmp"]
""")

    from nomad.cli import cli
    import nomad.alerts as alerts_module

    def boom(config):
        raise RuntimeError("simulated alerts misconfiguration")

    with patch.object(alerts_module, "init_dispatcher", side_effect=boom):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "-c", str(config),
            "collect", "--once",
        ])

    assert result.exit_code == 0, (
        f"collect crashed with exit code {result.exit_code} when dispatcher "
        f"init failed. Alerts must not be load-bearing for collection.\n"
        f"Output:\n{result.output}"
    )
    output_lower = result.output.lower()
    assert ("could not initialize alert dispatcher" in output_lower
            or "warning" in output_lower), (
        f"Expected warning about dispatcher init failure. "
        f"Output:\n{result.output}"
    )

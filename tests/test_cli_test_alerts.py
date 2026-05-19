# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Regression tests for the `nomad test-alerts` CLI subcommand.

Guards against three real bugs discovered during v1.6.4 SMTP wiring
on arachne:

  1. test-alerts shipped severity='info' against default
     min_severity='warning'. dispatch() silently filtered it,
     returned {}, the CLI iterated zero items and produced
     no Sent/Failed output. Operators couldn't tell the difference
     between success and silent drop.

  2. --email/--slack/--webhook flags were documented in --help
     but had no effect.

  3. Empty dispatch result produced no CLI output, leaving
     operators unable to distinguish success from silent drop.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from nomad.cli import cli


def _make_mock_backend(class_name, test_returns=True, send_returns=True):
    """Mock backend that mimics nomad.alerts.backends.NotificationBackend."""
    backend = MagicMock()
    backend.__class__.__name__ = class_name
    backend.test.return_value = test_returns
    backend.send.return_value = send_returns
    backend.enabled = True
    return backend


@pytest.fixture
def mock_email_backend():
    return _make_mock_backend("EmailBackend")


@pytest.fixture
def mock_slack_backend():
    return _make_mock_backend("SlackBackend")


@pytest.fixture
def make_dispatcher():
    """Returns a function that produces a mocked AlertDispatcher.

    The mock mimics the real dispatcher's behavior: test_backends() calls
    .test() on each backend, dispatch() respects severity filter and
    calls .send() on each backend whose alert passed the filter.
    """
    def _make(backends):
        dispatcher = MagicMock()
        dispatcher.backends = list(backends)

        def test_backends():
            return {b.__class__.__name__: b.test()
                    for b in dispatcher.backends}
        dispatcher.test_backends = test_backends

        def dispatch(alert):
            severity_order = {'info': 0, 'warning': 1, 'critical': 2}
            min_sev = severity_order.get('warning', 1)
            alert_sev = severity_order.get(alert.get('severity', 'info'), 0)
            if alert_sev < min_sev:
                return {}
            return {b.__class__.__name__: b.send(alert)
                    for b in dispatcher.backends}
        dispatcher.dispatch = dispatch

        return dispatcher
    return _make


# Core regression: test-alerts must call backend.send()

def test_test_alerts_calls_send_not_just_test(mock_email_backend, make_dispatcher):
    """Regression: test-alerts must exercise the dispatch+send path,
    not just the connection check via test_backends().

    Was failing because severity='info' got filtered by default
    min_severity='warning', dispatch returned {}, send was never called.
    """
    dispatcher = make_dispatcher([mock_email_backend])

    with patch("nomad.alerts.AlertDispatcher", return_value=dispatcher):
        runner = CliRunner()
        result = runner.invoke(cli, ["test-alerts"])

    assert result.exit_code == 0, result.output
    assert mock_email_backend.test.called, "backend.test() must run"
    assert mock_email_backend.send.called, (
        "backend.send() must run -- this is the whole point of test-alerts. "
        f"Output was:\n{result.output}"
    )


def test_test_alerts_uses_warning_severity(mock_email_backend, make_dispatcher):
    """The synthetic alert must use severity='warning' so it passes
    typical min_severity filters and the dispatch path actually runs.
    """
    dispatcher = make_dispatcher([mock_email_backend])

    with patch("nomad.alerts.AlertDispatcher", return_value=dispatcher):
        runner = CliRunner()
        result = runner.invoke(cli, ["test-alerts"])

    assert result.exit_code == 0, result.output

    call_args = mock_email_backend.send.call_args
    assert call_args is not None, "send() was not called"
    alert = call_args[0][0]
    assert alert["severity"] == "warning", (
        f"test-alerts must ship severity='warning', got {alert['severity']!r}. "
        "Lower severities get silently filtered by default min_severity."
    )


def test_test_alerts_reports_sent_in_output(mock_email_backend, make_dispatcher):
    """When send() succeeds, output must contain 'Sent' for that backend."""
    dispatcher = make_dispatcher([mock_email_backend])

    with patch("nomad.alerts.AlertDispatcher", return_value=dispatcher):
        runner = CliRunner()
        result = runner.invoke(cli, ["test-alerts"])

    assert result.exit_code == 0
    assert "Sent" in result.output, (
        f"Expected 'Sent' in output, got:\n{result.output}"
    )
    assert "EmailBackend" in result.output


# Flag filtering: --email, --slack, --webhook must actually filter

def test_email_flag_tests_only_email_backend(
    mock_email_backend, mock_slack_backend, make_dispatcher,
):
    """--email should test only the email backend, not slack."""
    dispatcher = make_dispatcher([mock_email_backend, mock_slack_backend])

    with patch("nomad.alerts.AlertDispatcher", return_value=dispatcher):
        runner = CliRunner()
        result = runner.invoke(cli, ["test-alerts", "--email"])

    assert result.exit_code == 0
    assert mock_email_backend.send.called, "Email backend should be tested"
    assert not mock_slack_backend.send.called, (
        "Slack backend should be skipped when --email is passed alone"
    )


def test_multiple_flags_tests_multiple_backends(
    mock_email_backend, mock_slack_backend, make_dispatcher,
):
    """--email --slack should test both backends."""
    dispatcher = make_dispatcher([mock_email_backend, mock_slack_backend])

    with patch("nomad.alerts.AlertDispatcher", return_value=dispatcher):
        runner = CliRunner()
        result = runner.invoke(cli, ["test-alerts", "--email", "--slack"])

    assert result.exit_code == 0
    assert mock_email_backend.send.called
    assert mock_slack_backend.send.called


def test_unselected_flag_reports_missing_backend(make_dispatcher):
    """--slack when slack isn't configured should report cleanly,
    not crash and not silently test nothing.
    """
    email_only = _make_mock_backend("EmailBackend")
    dispatcher = make_dispatcher([email_only])

    with patch("nomad.alerts.AlertDispatcher", return_value=dispatcher):
        runner = CliRunner()
        result = runner.invoke(cli, ["test-alerts", "--slack"])

    assert result.exit_code == 0
    assert "SlackBackend" in result.output
    assert "not configured" in result.output.lower()
    assert not email_only.send.called, (
        "Should not test email when --slack was explicitly requested"
    )


# Empty dispatch: never be silent

def test_empty_dispatch_explains_silence(mock_email_backend, make_dispatcher):
    """When dispatch() returns {} (filtered, cooldown, etc.), the CLI
    must say so explicitly rather than producing zero output.
    """
    dispatcher = make_dispatcher([mock_email_backend])
    dispatcher.dispatch = lambda alert: {}

    with patch("nomad.alerts.AlertDispatcher", return_value=dispatcher):
        runner = CliRunner()
        result = runner.invoke(cli, ["test-alerts"])

    assert result.exit_code == 0
    output_lower = result.output.lower()
    assert any(token in output_lower for token in (
        "filtered", "cooldown", "no backends dispatched", "min_severity",
    )), (
        f"Empty dispatch must produce visible explanation. Output was:\n"
        f"{result.output}"
    )


# No-backends help text: must use safe placeholders

def test_no_backends_uses_safe_placeholder_smtp(make_dispatcher):
    """When no backends are configured, the example shown must not
    use realistic-looking placeholders that admins might paste verbatim.
    Same v1.6.3 fix applied to the CLI help text.
    """
    dispatcher = make_dispatcher([])

    with patch("nomad.alerts.AlertDispatcher", return_value=dispatcher):
        runner = CliRunner()
        result = runner.invoke(cli, ["test-alerts"])

    assert result.exit_code == 0
    assert "smtp.example.com" not in result.output, (
        "Help text must not show smtp.example.com as a placeholder -- "
        "admins paste it verbatim and silent failures result."
    )
    assert "your-institution" in result.output or "127.0.0.1" in result.output

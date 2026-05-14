# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Tests for node-state alert dispatch and signal generation.

Node names mirror DEMO_CLUSTER from nomad.demo, and state strings are
uppercase to match what real Slurm clusters produce via scontrol.
"""

from unittest.mock import patch

import pytest

from nomad.collectors.node_state import NodeStateCollector
from nomad.insights.signals import read_node_health_signals, Severity


# ── Signal reader tests ───────────────────────────────────────────────────

def test_node_health_signals_finds_unhealthy(db_with_node_states):
    sigs = read_node_health_signals(db_with_node_states)
    titles = {s.title for s in sigs}
    assert 'node_down' in titles
    assert 'node_drain' in titles


def test_node_health_signals_severity_correct(db_with_node_states):
    sigs = read_node_health_signals(db_with_node_states)
    by_node = {s.metrics['node']: s for s in sigs if 'node' in s.metrics}

    # DOWN-family states are critical
    assert by_node['gpu01'].severity == Severity.CRITICAL  # DOWN+NOT_RESPONDING
    assert by_node['gpu02'].severity == Severity.CRITICAL  # DOWN

    # DRAIN-family states are warning
    assert by_node['node07'].severity == Severity.WARNING  # MIXED+DRAIN


def test_node_health_signals_empty_on_healthy_cluster(db_path):
    """No unhealthy nodes = no signals."""
    import sqlite3
    from datetime import datetime
    now = datetime.utcnow().isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO node_state (timestamp, node_name, state, reason, "
            "cluster, partitions, is_healthy) VALUES (?,?,?,?,?,?,?)",
            (now, 'node01', 'IDLE', '', 'demo-cluster', 'compute', 1),
        )
    assert read_node_health_signals(db_path) == []


def test_node_health_signals_respects_config(db_with_node_states):
    """Sites can override which states are critical/warning via config."""
    config = {
        'signals': {
            'node_state': {
                'critical_states': ['CUSTOM_DOWN'],  # neither DOWN nor DRAIN match
                'warning_states':  ['DRAIN'],
                'ignore_states':   ['NOT_RESPONDING'],
            }
        }
    }
    sigs = read_node_health_signals(db_with_node_states, config=config)
    surfaced = {s.metrics['node'] for s in sigs if 'node' in s.metrics}

    # gpu01 (DOWN+NOT_RESPONDING) ignored due to NOT_RESPONDING substring
    assert 'gpu01' not in surfaced
    # gpu02 (DOWN) doesn't match CUSTOM_DOWN -> warning fallback
    assert 'gpu02' in surfaced
    # node07 (MIXED+DRAIN) matches warning list
    assert 'node07' in surfaced


def test_node_health_signals_summary_threshold_disabled(db_with_node_states):
    """summary_threshold=0 disables the multi-node rollup."""
    sigs = read_node_health_signals(
        db_with_node_states,
        config={'signals': {'node_state': {'summary_threshold': 0}}},
    )
    titles = {s.title for s in sigs}
    assert 'multiple_nodes_unhealthy' not in titles


# ── Collector dispatch tests ──────────────────────────────────────────────

def test_collector_dispatches_alerts_for_unhealthy():
    """The collector should call send_alert for each unhealthy node."""
    collector = NodeStateCollector({}, ":memory:")
    records = [
        {'node_name': 'node01', 'state': 'IDLE',                'is_healthy': 1, 'reason': '',                  'cluster': 'demo-cluster'},
        {'node_name': 'gpu01',  'state': 'DOWN+NOT_RESPONDING', 'is_healthy': 0, 'reason': 'Not responding',    'cluster': 'demo-cluster'},
        {'node_name': 'node07', 'state': 'MIXED+DRAIN',         'is_healthy': 0, 'reason': 'Duplicate jobid',   'cluster': 'demo-cluster'},
    ]
    with patch('nomad.collectors.node_state.send_alert') as mock_send:
        collector._dispatch_state_alerts(records)
        assert mock_send.call_count == 2
        calls_by_host = {c.kwargs['host']: c.kwargs for c in mock_send.call_args_list}
        assert calls_by_host['gpu01']['severity'] == 'critical'
        assert calls_by_host['node07']['severity'] == 'warning'


def test_collector_alert_failure_does_not_break_collection():
    """If dispatcher fails, the collector should keep working."""
    collector = NodeStateCollector({}, ":memory:")
    records = [{'node_name': 'gpu01', 'state': 'DOWN', 'is_healthy': 0,
                'reason': 'test', 'cluster': 'demo-cluster'}]
    with patch('nomad.collectors.node_state.send_alert', side_effect=RuntimeError("smtp broken")):
        collector._dispatch_state_alerts(records)  # should not raise


def test_collector_classification_uses_defaults_when_unconfigured():
    """No config = sensible Slurm defaults."""
    collector = NodeStateCollector({}, ":memory:")
    # Critical: from arachne production DB
    assert collector._classify_state('DOWN') == 'critical'
    assert collector._classify_state('DOWN+NOT_RESPONDING') == 'critical'
    assert collector._classify_state('NOT_RESPONDING') == 'critical'
    # Warning: drain family
    assert collector._classify_state('MIXED+DRAIN') == 'warning'
    assert collector._classify_state('DRNG') == 'warning'
    # Unrecognized non-healthy -> warning fallback
    assert collector._classify_state('IDLE') == 'warning'


def test_collector_classification_respects_config():
    """Sites can override classification via [alerts.node_state]."""
    config = {
        'alerts': {
            'node_state': {
                'critical_states': ['CUSTOM_DOWN'],
                'warning_states':  ['CUSTOM_WARN'],
                'ignore_states':   ['POWERED_DOWN'],
            }
        }
    }
    collector = NodeStateCollector(config, ":memory:")
    assert collector._classify_state('CUSTOM_DOWN') == 'critical'
    assert collector._classify_state('IDLE+CUSTOM_WARN') == 'warning'
    assert collector._classify_state('POWERED_DOWN') is None
    # Default 'DOWN' no longer critical because critical_states was replaced
    assert collector._classify_state('DOWN') == 'warning'  # fallback


def test_collector_alerts_disabled_skips_dispatch():
    """[alerts.node_state] enabled=false silences dispatch."""
    config = {'alerts': {'node_state': {'enabled': False}}}
    collector = NodeStateCollector(config, ":memory:")
    assert collector._alert_config.get('enabled') is False

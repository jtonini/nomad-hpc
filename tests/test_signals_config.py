# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Tests for the config pass-through contract in the signals module."""

import inspect
import sqlite3

import pytest

from nomad.insights import signals as signals_module
from nomad.insights.signals import read_all_signals


READERS = [
    'read_job_signals',
    'read_disk_signals',
    'read_gpu_signals',
    'read_queue_signals',
    'read_network_signals',
    'read_alert_signals',
    'read_node_health_signals',
    'read_cloud_signals',
    'read_workstation_signals',
    'read_dynamics_signals',
]


@pytest.mark.parametrize("reader_name", READERS)
def test_reader_accepts_config_kwarg(reader_name):
    """Every signal reader must accept a config keyword argument.

    This contract enables read_all_signals to distribute site policy
    uniformly. Readers that don't yet consume config should still
    accept and ignore it -- bodies opt in to policy extraction one
    at a time.
    """
    reader = getattr(signals_module, reader_name)
    sig = inspect.signature(reader)
    assert 'config' in sig.parameters, (
        f"{reader_name} must accept a 'config' keyword argument"
    )
    assert sig.parameters['config'].default is None, (
        f"{reader_name}'s config parameter must default to None"
    )


def test_read_all_signals_loads_config_when_none(tmp_path, monkeypatch):
    """When called without config, read_all_signals should call load_config."""
    called = {'load_config': False}

    def fake_load_config(path=None):
        called['load_config'] = True
        return {'signals': {}}

    monkeypatch.setattr('nomad.config.load_config', fake_load_config)

    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    read_all_signals(db)
    assert called['load_config'], "read_all_signals must call load_config when config=None"


def test_read_all_signals_uses_passed_config(tmp_path, monkeypatch):
    """When config is passed explicitly, load_config should NOT be called."""
    called = {'load_config': False}

    def fake_load_config(path=None):
        called['load_config'] = True
        return {}

    monkeypatch.setattr('nomad.config.load_config', fake_load_config)

    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    read_all_signals(db, config={'signals': {}})
    assert not called['load_config'], "load_config must not be called when config is passed"

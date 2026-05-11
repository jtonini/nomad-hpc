# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Tests for nomad.edu.per_user_hooks and nomad.insights.readers.per_user"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nomad.edu.per_user_hooks import (
    TEMPLATES,
    insights_for_user,
    render_insight_text,
)
from nomad.insights.signals import (
    aggregate_cluster_culture_signal,
    read_per_user_signals,
)




# ---------------------------------------------------------------------------
# Edu hooks
# ---------------------------------------------------------------------------

def test_template_catalog_has_entries_for_all_default_rules():
    from nomad.collectors.per_user.rules import DEFAULT_RULES
    rule_template_ids = {r.edu_template_id for r in DEFAULT_RULES if r.edu_template_id}
    template_ids = set(TEMPLATES.keys())
    assert rule_template_ids.issubset(template_ids), (
        f"Rules reference templates that don't exist: {rule_template_ids - template_ids}"
    )


def test_insights_for_user_returns_only_that_users_alerts(db_with_alerts):
    insights = insights_for_user(db_with_alerts, username="ia3nk", lookback_days=30)
    assert len(insights) == 1
    assert insights[0].title == TEMPLATES["head_node_cpu_sustained"].title
    assert insights[0].severity == "actionable"


def test_insights_for_user_handles_unknown_user(db_with_alerts):
    insights = insights_for_user(db_with_alerts, username="nobody", lookback_days=30)
    assert insights == []


def test_insights_aggregate_occurrences(db_with_alerts):
    """abezerra has 4 occurrences in the test row."""
    insights = insights_for_user(db_with_alerts, username="abezerra", lookback_days=30)
    assert len(insights) == 1
    assert insights[0].occurrences == 4


def test_render_insight_text_includes_remediation(db_with_alerts):
    insights = insights_for_user(db_with_alerts, username="ia3nk", lookback_days=30)
    text = render_insight_text(insights[0])
    assert "srun" in text                            # remediation includes srun command
    assert "[!]" in text                             # actionable severity marker
    assert "gmx_mpi" in text                         # the triggering command


def test_render_insight_text_uses_info_marker_for_informational(db_with_alerts):
    insights = insights_for_user(db_with_alerts, username="abezerra", lookback_days=30)
    text = render_insight_text(insights[0])
    assert "[i]" in text                             # informational marker
    assert "[!]" not in text


# ---------------------------------------------------------------------------
# Insights reader
# ---------------------------------------------------------------------------

def test_read_per_user_signals_returns_all_recent(db_with_alerts):
    signals = read_per_user_signals(db_with_alerts, lookback_hours=168)
    assert len(signals) == 3


def test_read_per_user_signals_filters_by_hostname(db_with_alerts):
    signals = read_per_user_signals(db_with_alerts, hostname="spydur")
    assert len(signals) == 2
    assert {s.username for s in signals} == {"ia3nk", "perickso"}


def test_signals_carry_severity_in_category(db_with_alerts):
    signals = read_per_user_signals(db_with_alerts)
    by_sev = {s.username: s.severity for s in signals}
    assert by_sev["ia3nk"] == "actionable"
    assert by_sev["abezerra"] == "informational"


def test_aggregate_cluster_culture_for_spydur(db_with_alerts):
    signals = read_per_user_signals(db_with_alerts)
    agg = aggregate_cluster_culture_signal(signals, "spydur")
    assert agg is not None
    assert agg.context["distinct_users"] == 2
    assert agg.context["severity_breakdown"]["actionable"] == 2


def test_aggregate_cluster_culture_for_arachne(db_with_alerts):
    """Arachne's culture: 1 user, all informational. The validation report's
    'arachne is better disciplined' observation lands here as data."""
    signals = read_per_user_signals(db_with_alerts)
    agg = aggregate_cluster_culture_signal(signals, "arachne")
    assert agg is not None
    assert agg.context["distinct_users"] == 1
    assert agg.context["severity_breakdown"] == {"informational": 1}


def test_aggregate_returns_none_for_quiet_host(db_with_alerts):
    signals = read_per_user_signals(db_with_alerts)
    agg = aggregate_cluster_culture_signal(signals, "noiseless")
    assert agg is None

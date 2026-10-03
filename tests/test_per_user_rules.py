# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""per_user rules: since when a condition has held, from one reading to the next.

The collector runs from cron; each reading carries averages over the
interval since the previous one (CPU, I/O) or values at the reading
(memory), and the collector keeps each rule's ``since`` between runs.
"""
from __future__ import annotations

import logging

from nomad.collectors.per_user.rules import (
    DEFAULT_RULES,
    GB,
    MB,
    Reading,
    Rule,
    advance,
    fires,
    parse_rules,
    step,
    tolerance,
)

CPU50 = Rule("cpu_50pct_2min", "cpu", 50.0, "percent", 120)
CPU10 = Rule("cpu_10pct_5min", "cpu", 10.0, "percent", 300)
MEM16 = Rule("memory_16gb_2min", "memory", 16.0, "gb", 120)
MEM4 = Rule("memory_4gb_10min", "memory", 4.0, "gb", 600, "informational")
IO50 = Rule("io_50mbs_10min", "io", 50.0, "mb_s", 600, "informational")
UCPU = Rule("user_cpu_200pct_10min", "user_cpu", 200.0, "percent", 600)


def run(rule, readings):
    """Feed readings in order; return (since, fired) after each."""
    since, out = None, []
    for r in readings:
        since = step(rule, r, since)
        out.append((since, fires(rule, since, r.now)))
    return out


def cpu(now, start, pct):
    return Reading(now=now, window_start=start, cpu_percent=pct)


def test_a_five_minute_average_above_fires_the_two_minute_rule_at_once():
    # Cron every 300 s: the first interval average above 50% has held 300 s.
    out = run(CPU50, [cpu(1300, 1000, 95.0)])
    assert out == [(1000, True)]


def test_the_five_minute_rule_fires_on_a_299_second_interval():
    assert tolerance(CPU10) == 30
    assert run(CPU10, [cpu(1299, 1000, 40.0)]) == [(1000, True)]
    assert run(CPU10, [cpu(1200, 1000, 40.0)]) == [(1000, False)]


def test_since_carries_over_while_the_condition_holds_and_resets_below():
    out = run(CPU10, [cpu(1300, 1000, 40.0), cpu(1600, 1300, 35.0),
                      cpu(1900, 1600, 2.0), cpu(2200, 1900, 50.0)])
    assert [s for s, _ in out] == [1000, 1000, None, 1900]
    assert [f for _, f in out] == [True, True, False, True]


def test_a_lifetime_average_dates_the_condition_from_the_start():
    # First sight of a process two days old, 100% on average since it started.
    started = 1000.0
    now = started + 2 * 86400
    out = run(CPU50, [cpu(now, started, 100.0)])
    assert out == [(started, True)]


def test_memory_holds_only_from_the_first_reading_above():
    r = lambda now, gb: Reading(now=now, window_start=now - 300, memory_bytes=int(gb * GB))
    out = run(MEM16, [r(1000, 20), r(1300, 20)])
    assert out == [(1000, False), (1000, True)]
    out = run(MEM4, [r(1000, 5), r(1300, 5), r(1600, 5)])
    assert [f for _, f in out] == [False, False, True]
    assert run(MEM4, [r(1000, 5), r(1300, 3), r(1600, 5)])[-1] == (1600, False)


def test_io_in_megabytes_per_second():
    r = lambda now, mbs: Reading(now=now, window_start=now - 300, io_bytes_per_s=mbs * MB)
    out = run(IO50, [r(1300, 80), r(1600, 80)])
    assert out == [(1000, False), (1000, True)]


def test_an_unmeasured_value_is_no_news_and_never_fires():
    assert step(CPU50, Reading(now=1300, window_start=1000), None) is None
    # A run by hand seconds after cron's: too short for an average.
    assert step(CPU50, Reading(now=1305, window_start=1300), 1000) == 1000
    assert advance(CPU50, Reading(now=1305, window_start=1300), 1000) == (1000, False)
    assert advance(CPU50, cpu(1600, 1305, 90.0), 1000) == (1000, True)
    assert step(IO50, Reading(now=1300, window_start=1000, cpu_percent=99.0), None) is None


def test_user_rules_are_per_user_and_windowed():
    assert UCPU.per_user and UCPU.windowed
    assert not CPU50.per_user and not MEM16.windowed
    out = run(UCPU, [cpu(1300, 1000, 240.0), cpu(1600, 1300, 260.0)])
    assert out == [(1000, False), (1000, True)]


def test_defaults_cover_each_kind_and_have_unique_ids():
    kinds = {r.rule_type for r in DEFAULT_RULES}
    assert kinds == {"cpu", "memory", "io", "user_cpu", "user_memory"}
    ids = [r.rule_id for r in DEFAULT_RULES]
    assert len(ids) == len(set(ids))


def test_parse_rules_reads_tables_and_leaves_out_bad_ones(caplog):
    with caplog.at_level(logging.WARNING):
        rules = parse_rules([
            {"rule_id": "a", "rule_type": "cpu", "threshold": 80, "duration_seconds": 600},
            {"rule_id": "b", "rule_type": "user_memory", "threshold_value": 64,
             "duration_seconds": 300, "severity": "informational"},
            {"rule_id": "c", "rule_type": "disk", "threshold": 1, "duration_seconds": 1},
            {"rule_id": "d", "rule_type": "cpu", "duration_seconds": 1},
            {"rule_id": "a", "rule_type": "memory", "threshold": 8, "duration_seconds": 60},
            {"rule_id": "e", "rule_type": "cpu", "threshold": 50, "duration_seconds": 60,
             "severity": "urgent"},
        ])
    assert [r.rule_id for r in rules] == ["a", "b"]
    assert rules[0].threshold_unit == "percent" and rules[0].severity == "actionable"
    assert rules[1].threshold_unit == "gb" and rules[1].severity == "informational"
    assert "disk" in caplog.text and "used twice" in caplog.text and "urgent" in caplog.text

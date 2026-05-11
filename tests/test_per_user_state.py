# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Tests for nomad.collectors.per_user.state"""
from __future__ import annotations

from nomad.collectors.per_user.rules import Sample
from nomad.collectors.per_user.state import (
    FiringDedup,
    TrackStore,
    make_session_id,
)


def test_session_id_stable_for_same_inputs():
    a = make_session_id("host1", 1234, 1700000000.0)
    b = make_session_id("host1", 1234, 1700000000.0)
    assert a == b


def test_session_id_differs_when_pid_recycles_with_different_start_time():
    a = make_session_id("host1", 1234, 1700000000.0)
    b = make_session_id("host1", 1234, 1700001000.0)        # later start time
    assert a != b


def test_session_id_differs_across_hosts():
    a = make_session_id("host1", 1234, 1700000000.0)
    b = make_session_id("host2", 1234, 1700000000.0)
    assert a != b


def test_observe_creates_one_track_per_session():
    store = TrackStore(hostname="h", max_window_seconds=600)
    s1 = Sample(timestamp=0.0, cpu_percent=10.0, memory_rss_bytes=10**8)
    s2 = Sample(timestamp=60.0, cpu_percent=12.0, memory_rss_bytes=10**8)
    store.observe(pid=1234, start_time=1.0, username="u", uid=10001, command="x", sample=s1)
    store.observe(pid=1234, start_time=1.0, username="u", uid=10001, command="x", sample=s2)
    assert len(store) == 1
    track = store.all_tracks()[0]
    assert len(track.samples) == 2


def test_observe_creates_separate_tracks_after_pid_recycle():
    """Same pid, different start_time -> two distinct tracks."""
    store = TrackStore(hostname="h", max_window_seconds=600)
    s = Sample(timestamp=0.0, cpu_percent=10.0, memory_rss_bytes=10**8)
    store.observe(pid=1234, start_time=1.0, username="u", uid=10001, command="x", sample=s)
    store.observe(pid=1234, start_time=99.0, username="u", uid=10001, command="x", sample=s)
    assert len(store) == 2


def test_gc_evicts_processes_not_in_live_set():
    store = TrackStore(hostname="h", max_window_seconds=600)
    s = Sample(timestamp=0.0, cpu_percent=10.0, memory_rss_bytes=10**8)
    track1 = store.observe(pid=1, start_time=1.0, username="u", uid=10001, command="a", sample=s)
    track2 = store.observe(pid=2, start_time=1.0, username="u", uid=10001, command="b", sample=s)
    assert len(store) == 2
    # Only track1's session is still alive
    evicted = store.gc(now=10.0, live_session_ids={track1.process_session_id})
    assert evicted == 1
    assert len(store) == 1
    assert store.get(track1.process_session_id) is not None
    assert store.get(track2.process_session_id) is None


def test_gc_evicts_stale_tracks_when_no_live_set_provided():
    """Fallback path: caller didn't pass live_session_ids -> use last_observed timestamps."""
    store = TrackStore(hostname="h", max_window_seconds=300, stale_after_seconds=600)
    s = Sample(timestamp=0.0, cpu_percent=10.0, memory_rss_bytes=10**8)
    store.observe(pid=1, start_time=1.0, username="u", uid=10001, command="a", sample=s)
    # Time advances past stale_after threshold
    evicted = store.gc(now=1000.0, live_session_ids=None)
    assert evicted == 1
    assert len(store) == 0


def test_dedup_key_format_is_stable():
    key = FiringDedup.make_key("host1", "abc123", "cpu_10pct_5min")
    assert key == "host1|abc123|cpu_10pct_5min"


def test_dedup_distinguishes_new_vs_seen_keys():
    dedup = FiringDedup()
    k = FiringDedup.make_key("host", "sid", "rule")
    assert dedup.is_new(k)
    dedup.record(k, fired_at=100.0)
    assert not dedup.is_new(k)

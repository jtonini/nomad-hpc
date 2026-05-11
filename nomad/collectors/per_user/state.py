# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAD per-user collector — in-memory state.

Owns the live ProcessTrack objects between collection ticks. The collector
calls observe() once per (pid, sample) and gc() at the end of each tick.

Design
------
Tracks are keyed by process_session_id, not pid. process_session_id is
sha1(hostname | pid | start_time_unix), so a recycled pid produces a new
track. This is the same pattern used by the workstation_user_snapshot
table for session epochs.

Memory bound: O(distinct active processes), not O(time). Eviction happens
when a process disappears OR when no observations have arrived for it
within `stale_after_seconds` (defaults to twice the longest rule window).

This is in-memory only by design — keeping it crash-resilient via DB writes
on every observation would multiply the I/O cost we just confirmed at
0.12% CPU. If the collector restarts, tracks reset, sustain windows reset,
and we miss firings until enough samples accumulate again. That's an
acceptable price for the overhead floor. Crash-resilience belongs in
Component 1.5 (pacct), which records every process exit unconditionally.
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field

from .rules import ProcessTrack, Sample

logger = logging.getLogger(__name__)


def make_session_id(hostname: str, pid: int, start_time: float) -> str:
    """Deterministic session id for a process. 16 hex chars (64 bits) is
    plenty for collision avoidance within a host's history."""
    h = hashlib.sha1(f"{hostname}|{pid}|{int(start_time)}".encode()).hexdigest()
    return h[:16]


@dataclass
class TrackStore:
    """Holds live ProcessTrack objects, keyed by session_id.

    Single-threaded by assumption (the collector runs ticks sequentially).
    No locking.
    """
    hostname: str
    max_window_seconds: int                # passed in from RuleEngine
    stale_after_seconds: int = 0           # 0 -> auto: 2x max_window

    _tracks: dict[str, ProcessTrack] = field(default_factory=dict)
    # Track last-observed time for GC
    _last_observed: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.stale_after_seconds == 0:
            self.stale_after_seconds = max(self.max_window_seconds * 2, 600)

    # -----------------------------------------------------------------------
    # Observation
    # -----------------------------------------------------------------------

    def observe(
        self,
        *,
        pid: int,
        start_time: float,
        username: str,
        uid: int,
        command: str,
        sample: Sample,
    ) -> ProcessTrack:
        """Record one sample for a process. Creates a new track on first sight."""
        session_id = make_session_id(self.hostname, pid, start_time)
        track = self._tracks.get(session_id)
        if track is None:
            track = ProcessTrack(
                process_session_id=session_id,
                pid=pid,
                username=username,
                uid=uid,
                command=command,
            )
            self._tracks[session_id] = track
            logger.debug("new track: %s pid=%s user=%s cmd=%s", session_id, pid, username, command)
        track.add_sample(sample, self.max_window_seconds)
        self._last_observed[session_id] = sample.timestamp
        return track

    def get(self, session_id: str) -> ProcessTrack | None:
        return self._tracks.get(session_id)

    def all_tracks(self) -> list[ProcessTrack]:
        return list(self._tracks.values())

    # -----------------------------------------------------------------------
    # GC
    # -----------------------------------------------------------------------

    def gc(self, now: float | None = None, live_session_ids: set[str] | None = None) -> int:
        """Drop tracks for processes we haven't seen recently.

        Two signals trigger eviction:
          - The session_id is not in `live_session_ids` (caller knows the
            process exited). This is the primary signal, evicts immediately.
          - The track hasn't received a sample in `stale_after_seconds`
            (fallback for cases where the caller didn't pass live_session_ids,
            e.g. on a tick with errors).

        Returns the number of tracks evicted.
        """
        if now is None:
            now = time.time()
        cutoff = now - self.stale_after_seconds

        evicted = 0
        for session_id in list(self._tracks.keys()):
            should_evict = False
            if live_session_ids is not None and session_id not in live_session_ids:
                should_evict = True
            elif self._last_observed.get(session_id, 0) < cutoff:
                should_evict = True

            if should_evict:
                del self._tracks[session_id]
                self._last_observed.pop(session_id, None)
                evicted += 1
        if evicted:
            logger.debug("gc evicted %d tracks", evicted)
        return evicted

    def __len__(self) -> int:
        return len(self._tracks)


@dataclass
class FiringDedup:
    """Tracks alert firings for dedup against the per_user_alert table.

    The DB has a UNIQUE(dedup_key) constraint. We keep an in-memory mirror
    so we can decide cheaply whether a firing is new (insert) or a
    re-occurrence (UPDATE occurrences, last_seen).

    Reset on collector restart — that's fine, the DB constraint catches
    actual duplicates and the collector handles the IntegrityError.
    """
    _seen: dict[str, int] = field(default_factory=dict)   # dedup_key -> last_fired_unix

    @staticmethod
    def make_key(hostname: str, session_id: str, rule_id: str) -> str:
        return f"{hostname}|{session_id}|{rule_id}"

    def is_new(self, dedup_key: str) -> bool:
        return dedup_key not in self._seen

    def record(self, dedup_key: str, fired_at: float) -> None:
        self._seen[dedup_key] = int(fired_at)

    def __len__(self) -> int:
        return len(self._seen)

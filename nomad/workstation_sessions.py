# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A person's sessions on a lab workstation, from the cgroup readings (1.13.2).

A session is the person's user slice on a machine: from the first reading of
it to the last, for as long as systemd keeps it (while they have anything
running there). The probe's ``session_epoch`` is the slice directory's ctime,
and the kernel changes a directory's ctime whenever an entry in it is made or
removed: every login, terminal, ssh connection or cron job a person starts
there adds a session scope to the slice and changes it. Grouped by it, one
person's day at a machine came out as dozens of sessions, each short and each
carrying the slice's whole memory peak, which only grows.

A slice's counters only grow while it lives and start from nothing when it
is made again. So here a session runs while they keep growing: a new one
starts when the CPU time or the memory peak goes down, or after MAX_GAP
with no reading (the slice may have gone and come back with counters that
happen to be higher; or the collector was stopped, and nothing is known of
that time). Readings without counters fall back on the epoch.

Rows are tuples ``(key, timestamp, epoch, cpu_usage_usec, memory_peak_bytes,
*rest)`` ordered by key, then timestamp; ``key`` is whatever says whose slice
on which machine (the hostname and uid, with the site in a combined
database).
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta

MAX_GAP = timedelta(hours=2)        # as the interactive collector's


def _when(text) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text)[:19])
    except ValueError:
        return None


def _new_slice(prev: tuple, row: tuple) -> bool:
    a, b = _when(prev[1]), _when(row[1])
    if a is not None and b is not None and b - a > MAX_GAP:
        return True
    known = False
    for i in (3, 4):                     # CPU time, memory peak
        a, b = prev[i], row[i]
        if a is None or b is None:
            continue
        if b < a:
            return True
        known = True
    return not known and prev[2] != row[2]


def split(rows: Iterable[tuple]) -> Iterator[list[tuple]]:
    """The rows of each session, in order (rows ordered by key, then time)."""
    current: list[tuple] = []
    for row in rows:
        if current and (row[0] != current[-1][0] or _new_slice(current[-1], row)):
            yield current
            current = []
        current.append(row)
    if current:
        yield current

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


# A pause this long (reading or writing a file, waiting on the network) does
# not end a run; a longer one does.
RUN_GRACE = timedelta(minutes=30)
# An interval keeps a run going when it keeps this share of the cores needed
# busy; the run counts when its average reaches all of them.
RUN_DIP = 0.75


def busy_runs(readings: Iterable[tuple], cores_needed: float, min_hours: float,
              dip: float = RUN_DIP, grace: timedelta = RUN_GRACE) -> list[tuple]:
    """The stretches of one session that kept ``cores_needed`` cores busy on
    average for ``min_hours`` or more: [(hours, cores, ended)], in order
    (``ended``: the run's last busy reading, YYYY-MM-DDTHH:MM:SS).

    ``readings`` are (timestamp, cpu_usage_usec) in time order. A session can
    be weeks long (a slice lives while anything of the person's runs on the
    machine), so its own average says little about a two-day job inside it:
    a run is a stretch of intervals each keeping ``dip`` of the cores needed
    busy, with pauses up to ``grace`` inside it, and counts when its average
    over the whole stretch reaches ``cores_needed``. A stretch whose average
    falls short (a heavy job beside days of moderate load) is searched again
    for the stretches inside it that were fully busy."""
    points = [(t, cpu) for t, cpu in ((_when(w), c) for w, c in readings)
              if t is not None and cpu is not None]
    return _runs(points, cores_needed, min_hours, dip, grace)


def _runs(points: list, need: float, min_hours: float, dip: float, grace: timedelta) -> list:
    runs: list[tuple] = []
    first = last = None              # indices of the run's first and last busy reading

    def close():
        if first is None:
            return
        (t0, c0), (t1, c1) = points[first], points[last]
        hours = (t1 - t0).total_seconds() / 3600
        if hours < min_hours or hours <= 0:
            return
        cores = (c1 - c0) / 1e6 / (hours * 3600)
        if cores >= need:
            runs.append((hours, cores, t1.isoformat(timespec="seconds")))
        elif dip < 1.0:
            runs.extend(_runs(points[first:last + 1], need, min_hours, 1.0, grace))

    for i in range(1, len(points)):
        (ta, ca), (tb, cb) = points[i - 1], points[i]
        dt = (tb - ta).total_seconds()
        if dt > 0 and (cb - ca) / 1e6 / dt >= dip * need:
            if first is None:
                first = i - 1
            last = i
        elif first is not None and tb - points[last][0] > grace:
            close()
            first = last = None
    close()
    return runs

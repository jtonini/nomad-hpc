# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Slurm job records nomad did not collect: from an sacct export, or from sacct.

nomad's job history starts when it was installed, misses what it didn't
follow, and Slurm deletes its own job records after a while (PurgeJobAfter).
An export taken in time (``sacct -a -X -P -o ALL``, or any ``sacct -P`` with a
header line), or sacct itself for a period it still holds, fills that in:

    nomad import sacct EXPORT [--apply]
    nomad import sacct --from 2025-10-01 [--to 2026-10-01] [--apply]

Records are read by the export's header, so any field order and extra fields
do. A record spread over several lines (a field with newlines) is put back
together: lines join a record until it reads as one; the next line then starts
another (unless it is a lone rest of a last field with a newline), and a line
that reads as a whole record ends one that never read. A '|' inside a free-text field (submit line, working
directory, job name, comments) is put back where the other fields then read as
they should. Job steps and pending array ranges are skipped.

Writing never undoes what nomad knows. A stored job gets what it lacks
(account, allocation, working directory, partition from a list, failure
reason); its outcome changes only when nomad had none -- stored as running,
pending or UNKNOWN, or an outcome assumed before 1.7.19 -- and the records show
it ended. A job the records show running or pending, and nomad doesn't have,
goes in as UNKNOWN with no end: an export is a snapshot, and its "running" may
be months old. Job numbers Slurm gave out again are placed by
nomad.db.jobkeys. Without --apply nothing is written.
"""
from __future__ import annotations

import gzip
import io
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Iterable, Iterator

from nomad.db.jobkeys import ACTIVE_STATES, decide, ensure_job_columns, place, stored_job

from .slurm import _ASSUMED_END, SlurmCollector, _JOB_FIELDS

logger = logging.getLogger(__name__)

# Free-text fields that may hold a '|', in the order tried when a record has
# more fields than its header.
_FREE_TEXT = ("SubmitLine", "JobName", "WorkDir", "Comment", "AdminComment",
              "SystemComment", "Constraints", "Reason", "Extra", "Container",
              "StdOut", "StdErr", "StdIn")
_TIMES = ("Submit", "Start", "End")
_NO_TIME = ("", "Unknown", "None", "N/A")
# What well-formed fields look like: used to tell which free-text field a
# stray '|' belongs to (the fold that leaves the most fields well-formed).
_DURATION = r"(\d+-)?\d{1,2}:\d{2}(:\d{2}(\.\d+)?)?"
_TIME = r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d|Unknown|None"
_SHAPES = {
    "JobID": r"\d+([_+][\w\[\]\-,%]*)?(\.\w+)?",
    "JobIDRaw": r"\d+(\.\w+)?",
    "State": r"[A-Z_]+( by \d+)?\+?",
    "ExitCode": r"\d+:\d+", "DerivedExitCode": r"\d+:\d+",
    "AllocCPUS": r"\d+", "NCPUS": r"\d+", "NNodes": r"\d+", "UID": r"\d+", "GID": r"\d+",
    "Elapsed": _DURATION, "ElapsedRaw": r"\d+",
    "Timelimit": _DURATION + r"|UNLIMITED|Partition_Limit|INVALID",
    "CPUTime": _DURATION, "TotalCPU": _DURATION, "UserCPU": _DURATION, "SystemCPU": _DURATION,
    "WorkDir": r"/[^|]*", "User": r"[^\s|]+", "Group": r"[^\s|]+",
    "Submit": _TIME, "Start": _TIME, "End": _TIME, "Eligible": _TIME,
    "AllocTRES": r"[\w/:=.,\-]*", "ReqTRES": r"[\w/:=.,\-]*",
    "NodeList": r"[\w\[\],\-.]*|None assigned",
}
# Commit about this often while importing (seconds), and give way a moment
# after each commit, so the cron collectors writing the same database get in.
COMMIT_EVERY = 0.5
YIELD_FOR = 0.2
# A record with more stray '|' than this is not read (and doesn't swallow more).
MAX_EXTRA = 64

_ACTIVE = ", ".join(f"'{s}'" for s in ACTIVE_STATES)
# A stored job without a real outcome: running or pending, UNKNOWN, an outcome
# assumed before 1.7.19, or no state at all.
_NO_OUTCOME = (f"(jobs.state IS NULL OR jobs.state IN ({_ACTIVE}, 'UNKNOWN') "
               f"OR (jobs.state = 'COMPLETED' AND jobs.{_ASSUMED_END.strip()}))")
_ENDED = (f"({_NO_OUTCOME} AND COALESCE(excluded.state, '') NOT IN ({_ACTIVE}, 'UNKNOWN') "
          f"AND excluded.end_time IS NOT NULL)")
_OUTCOME = ("state", "end_time", "exit_code", "exit_signal", "runtime_seconds",
            "start_time", "wait_time_seconds", "node_list")
_SPECIAL = ("job_id", "partition", "failure_reason")


def _fill_sql(outcome: bool = True) -> str:
    """The import's upsert. outcome=False: never touch the outcome (a record
    older than what is stored for the same job)."""
    cols = ", ".join(_JOB_FIELDS)
    marks = ", ".join("?" * len(_JOB_FIELDS))
    agree = "(jobs.state IS NULL OR jobs.state = excluded.state)"
    sets = []
    for c in _JOB_FIELDS:
        if c in _SPECIAL:
            continue
        if c in _OUTCOME:
            if outcome:
                sets.append(f"{c} = CASE WHEN {_ENDED} THEN excluded.{c} "
                            f"WHEN {agree} THEN COALESCE(jobs.{c}, excluded.{c}) "
                            f"ELSE jobs.{c} END")
        else:
            sets.append(f"{c} = COALESCE(jobs.{c}, excluded.{c})")
    sets.append("partition = CASE WHEN COALESCE(jobs.partition, '') = '' THEN excluded.partition "
                "WHEN instr(jobs.partition, ',') > 0 AND COALESCE(excluded.partition, '') <> '' "
                "AND instr(excluded.partition, ',') = 0 THEN excluded.partition "
                "ELSE jobs.partition END")
    if outcome:
        sets.append(f"failure_reason = CASE WHEN {_ENDED} OR {agree} "
                    f"THEN excluded.failure_reason ELSE jobs.failure_reason END")
    return (f"INSERT INTO jobs ({cols}) VALUES ({marks}) ON CONFLICT(job_id) DO UPDATE SET "
            + ", ".join(sets))


_FILL = _fill_sql()
_FILL_KEEP_OUTCOME = _fill_sql(outcome=False)


def _is_time(value: str) -> bool:
    value = (value or "").strip()
    if value in _NO_TIME:
        return True
    try:
        datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")
        return True
    except ValueError:
        return False


def _open(path: str) -> Iterator[str]:
    with open(path, "rb") as raw:
        magic = raw.read(2)
    if magic == b"\x1f\x8b":
        stream = io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    else:
        stream = open(path, encoding="utf-8", errors="replace")
    with stream:
        for line in stream:
            yield line.rstrip("\n").rstrip("\r")


class ExportError(ValueError):
    """An export nomad can't read at all (no header line, say)."""


def read_export(lines: Iterable[str]) -> Iterator[dict | None]:
    """Records of a ``sacct -P`` export with a header line, as dicts by field
    name; None for a record that can't be read. ExportError when the first
    line isn't a header with JobID (an export made with -n, say)."""
    it = iter(lines)
    header = None
    for line in it:
        if line.strip():
            header = [h.strip() for h in line.split("|")]
            break
    if not header:
        return
    if "JobID" not in header:
        raise ExportError("the first line is not a header with JobID "
                          "(make the export without -n/--noheader)")
    n = len(header)
    pos = {name: i for i, name in enumerate(header)}
    times = [pos[t] for t in _TIMES if t in pos]
    folds = [pos[f] for f in _FREE_TEXT if f in pos]
    shapes = [(pos[name], re.compile(rx)) for name, rx in _SHAPES.items() if name in pos]

    def readable(parts: list[str]) -> bool:
        return all(_is_time(parts[i]) for i in times)

    def score(parts: list[str]) -> int:
        return sum(1 for i, rx in shapes if not parts[i] or rx.fullmatch(parts[i]))

    def fold(parts: list[str]) -> list[str] | None:
        """The record's fields, with stray '|' put back: one free-text field
        taking them all, or two sharing them; the most well-formed wins
        (ties: fewer fields folded, then the order of _FREE_TEXT)."""
        extra = len(parts) - n
        if extra < 0:
            return None
        if extra == 0:
            return parts if readable(parts) else None
        best, best_key = None, None

        def consider(trial, folded):
            nonlocal best, best_key
            if readable(trial):
                key = (score(trial), -folded)
                if best_key is None or key > best_key:
                    best, best_key = trial, key
        for k in folds:
            consider(parts[:k] + ["|".join(parts[k:k + extra + 1])] + parts[k + extra + 1:], 1)
        if 2 <= extra <= 4:
            for a in folds:
                for b in folds:
                    if b <= a:
                        continue
                    for ea in range(1, extra):
                        eb = extra - ea
                        t = parts[:a] + ["|".join(parts[a:a + ea + 1])] + parts[a + ea + 1:]
                        t = t[:b] + ["|".join(t[b:b + eb + 1])] + t[b + eb + 1:]
                        consider(t, 2)
        return best

    last_free = header[-1] in _FREE_TEXT

    # One record at a time: lines join it while it is short of its fields or
    # doesn't read yet; once it reads, the next line starts another -- unless
    # that line is a lone piece of a last field that held a newline.
    buf: list[str] | None = None
    done: list[str] | None = None        # buf, read as a record (None: not yet)

    def readable_whole(parts):
        return fold(parts) if len(parts) >= n else None

    for line in it:
        parts = line.split("|")
        if buf is None:
            if line.strip():
                buf, done = parts, readable_whole(parts)
            continue
        if done is not None:
            if not line.strip():
                continue
            if len(parts) == 1 and last_free:
                buf[-1] += "\n" + parts[0]
                done = readable_whole(buf)
                continue
            alone = readable_whole(parts)
            if alone is None:
                # The record read, but perhaps only by taking pieces of a free
                # text that goes on here ("a|b|c" before a newline): joined,
                # does it read better? A record that read well can't.
                joined = buf[:-1] + [buf[-1] + "\n" + parts[0]] + parts[1:]
                better = readable_whole(joined)
                if better is not None and score(better) > score(done):
                    buf, done = joined, better
                    continue
            yield _record(header, done)
            buf, done = parts, alone
            continue
        # The record so far doesn't read yet.
        whole = readable_whole(parts)
        if whole is not None:
            # A whole record on this line: the one before was cut short.
            yield None
            buf, done = parts, whole
            continue
        buf[-1] += "\n" + parts[0]
        buf.extend(parts[1:])
        if len(buf) > n + MAX_EXTRA:
            yield None                  # too much to be one record
            buf, done = None, None
            continue
        done = readable_whole(buf)
    if buf is not None:
        yield _record(header, done)


def _record(header, parts):
    return dict(zip(header, parts)) if parts is not None else None


@dataclass
class Counts:
    records: int = 0
    steps: int = 0
    ranges: int = 0           # pending array ranges ("123_[5-10]"): not jobs
    unreadable: int = 0
    new: int = 0              # jobs added (including older ones kept apart)
    unknown: int = 0          # of those: running or pending in the records, stored as UNKNOWN
    same: int = 0
    filled: int = 0           # stored jobs given something they lacked
    ended: int = 0            # stored without an outcome, ended in the records
    stale: int = 0
    older: int = 0
    moved: int = 0
    no_place: int = 0
    repeats: int = 0          # the same job again (sacct lists a job in every month it ran)
    first: str | None = None  # submit months of the jobs added
    last: str | None = None
    months: dict = field(default_factory=dict)

    def added(self, submit) -> None:
        m = submit[:7] if submit else None
        if not m:
            return
        self.months[m] = self.months.get(m, 0) + 1
        self.first = m if self.first is None or m < self.first else self.first
        self.last = m if self.last is None or m > self.last else self.last


class Importer:
    """Writes JobInfo records into a nomad database (or only counts them)."""

    def __init__(self, conn: sqlite3.Connection, apply: bool):
        self.conn = conn
        self.apply = apply
        self.counts = Counts()
        self._seen: set = set()
        self._since_commit = time.monotonic()
        if apply:
            ensure_job_columns(conn)
            conn.commit()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
        self._check = ", ".join(c if c in cols else "NULL" for c in
                                ("state", "work_root", "alloc_tres", "account", "partition",
                                 "failure_reason", "end_time"))

    def _stored(self, job_id: str):
        return self.conn.execute(f"SELECT {self._check} FROM jobs WHERE job_id = ?",
                                 (job_id,)).fetchone()

    @staticmethod
    def _lacks(row, rec: dict, outcome: bool = True) -> bool:
        """Whether the record gives the stored row something it lacks."""
        if row is None:
            return False
        state, work_root, alloc_tres, account, partition, failure, _ = row
        return ((work_root is None and rec["work_root"] is not None)
                or (alloc_tres is None and rec["alloc_tres"] is not None)
                or (account is None and rec["account"] is not None)
                or bool(partition and "," in partition and rec["partition"]
                        and "," not in rec["partition"])
                or (outcome and (state is None or state == rec["state"])
                    and (failure or 0) != (rec["failure_reason"] or 0)))

    @staticmethod
    def _no_outcome(row) -> bool:
        if row is None:
            return False
        state, end = row[0], row[6]
        assumed = (state == "COMPLETED" and end is not None and "T" not in str(end)
                   and len(str(end)) == 19)
        return state is None or state in ACTIVE_STATES or state == "UNKNOWN" or assumed

    def add(self, job) -> None:
        rec = job.to_dict()
        c = self.counts
        if "[" in str(rec["job_id"]):
            c.ranges += 1                   # a pending array range, not a job
            return
        key = (rec["job_id"], rec["submit_time"])
        if key in self._seen:
            c.repeats += 1
            return
        self._seen.add(key)
        active = (rec["state"] or "").split(" ")[0] in ACTIVE_STATES
        kind, where = decide(self.conn, rec["job_id"], rec["submit_time"],
                             rec["user_name"], rec["job_name"])
        if kind == "older" and where is not None and stored_job(self.conn, where) is not None:
            kind = "same"                   # kept apart already (an earlier import)
        if kind in ("older", "move") and where is None:
            c.no_place += 1
            return
        sql = _FILL
        if kind == "stale":
            # An earlier look at a job stored since: what it lacks, never its outcome.
            c.stale += 1
            sql = _FILL_KEEP_OUTCOME
            where = str(rec["job_id"])
            if self._lacks(self._stored(where), rec, outcome=False):
                c.filled += 1
        elif kind == "same":
            c.same += 1
            row = self._stored(where)
            if self._lacks(row, rec):
                c.filled += 1
            if not active and rec["end_time"] and self._no_outcome(row):
                c.ended += 1
        else:                               # new, older (kept apart), move
            if kind == "older":
                c.older += 1
            elif kind == "move":
                c.moved += 1
            c.new += 1
            c.added(rec["submit_time"])
            if active:
                # Running or pending in the records, maybe months ago: not an
                # outcome nomad can vouch for, and not one to settle.
                c.unknown += 1
                rec["state"] = "UNKNOWN"
                rec["end_time"] = None
                rec["exit_code"] = rec["exit_signal"] = rec["failure_reason"] = None
        if not self.apply:
            return
        if kind == "stale":
            job_id = where
        else:
            job_id = place(self.conn, rec["job_id"], rec["submit_time"], rec["user_name"],
                           rec["job_name"])
        if job_id is None:
            c.no_place += 1
            return
        rec["job_id"] = job_id
        self.conn.execute(sql, tuple(rec[f] for f in _JOB_FIELDS))
        if time.monotonic() - self._since_commit >= COMMIT_EVERY:
            self.commit(pause=True)

    def commit(self, pause: bool = False) -> None:
        if self.apply:
            self.conn.commit()
            if pause:
                time.sleep(YIELD_FOR)       # let a waiting collector write
        self._since_commit = time.monotonic()

    def finish(self) -> Counts:
        self.commit()
        return self.counts


def import_export(conn: sqlite3.Connection, path: str, apply: bool) -> Counts:
    """Read an sacct export into the database (or count what would change).
    What was read before an error stays written (with --apply)."""
    parser = SlurmCollector({}, ":memory:")
    imp = Importer(conn, apply)
    try:
        for rec in read_export(_open(path)):
            imp.counts.records += 1
            if rec is None:
                imp.counts.unreadable += 1
                continue
            job_id = (rec.get("JobID") or "").strip()
            if "." in job_id:
                imp.counts.steps += 1
                continue
            try:
                job = parser.job_from_fields(rec)
            except Exception as e:                   # one odd record, not the import
                logger.debug(f"unreadable record {job_id}: {e}")
                job = None
            if job is None:
                imp.counts.unreadable += 1
                continue
            imp.add(job)
    finally:
        imp.finish()
    return imp.counts


def month_windows(since: date, until: date) -> Iterator[tuple[date, date]]:
    """[start, end) windows of at most a calendar month covering [since, until)."""
    a = since
    while a < until:
        b = date(a.year + (a.month == 12), a.month % 12 + 1, 1)
        b = min(b, until)
        yield a, b
        a = b


class SacctStopped(Exception):
    """sacct failed part way: ``counts`` holds what was done (and committed)."""

    def __init__(self, month: date, error: Exception, counts: Counts):
        super().__init__(f"sacct failed for {month:%Y-%m}: {error}")
        self.month, self.error, self.counts = month, error, counts


def import_from_sacct(conn: sqlite3.Connection, since: date, until: date, apply: bool,
                      progress=None) -> Counts:
    """Ask sacct for every job of [since, until), a month at a time. Each
    month is committed before sacct is asked about the next; SacctStopped,
    with the counts so far, if sacct fails."""
    collector = SlurmCollector({}, ":memory:")
    imp = Importer(conn, apply)
    for a, b in month_windows(since, until):
        imp.commit()                        # nothing held while sacct answers
        try:
            jobs = collector._sacct("--allusers", f"--starttime={a.isoformat()}",
                                    f"--endtime={b.isoformat()}")
        except Exception as e:              # CollectionError, a timeout, no sacct
            imp.finish()
            raise SacctStopped(a, e, imp.counts)
        if progress:
            progress(a, len(jobs))
        for job in jobs:
            imp.counts.records += 1
            imp.add(job)
    return imp.finish()


__all__ = ["Counts", "ExportError", "Importer", "SacctStopped", "import_export",
           "import_from_sacct", "month_windows", "read_export"]

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Job numbers that come back, and the job columns nomad adds.

nomad keys a job on its Slurm number (``jobs.job_id``), and so do
job_summary, job_metrics and the other per-job tables. Slurm reuses numbers:
after its counter restarts (state lost in an outage) or wraps at MaxJobId,
job 4711 is a new job. Writing the new job over the stored row would merge
two jobs: the new one's state, nodes and times under the old one's user,
partition, name and submit time.

``place()`` decides, before a job record is written, where it goes. When the
stored row is a different job, that older job moves aside to
``NUMBER@SUBMIT-TIME`` in every table that has a job id, and the plain
number is free for the job Slurm means by it now. A requeued job (same user,
same name, same number) is the same job and keeps updating in place.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# States of a job that has not ended (squeue lists them; sacct shows them for
# jobs still running).
ACTIVE_STATES = ('RUNNING', 'PENDING', 'COMPLETING', 'CONFIGURING', 'SUSPENDED',
                 'REQUEUED', 'REQUEUE_HOLD', 'REQUEUE_FED', 'RESIZING', 'SIGNALING',
                 'STAGE_OUT', 'STOPPED', 'RESV_DEL_HOLD', 'SPECIAL_EXIT', 'EXPEDITING')

# A job that ended this long before another job with its number was
# submitted is another job, even under the same user and name.
REUSED_AFTER = timedelta(days=30)
# A stored job still marked active (a requeue keeps its number and gets a new
# submit time) is the same job only within its time limit plus REUSED_AFTER of
# the new submit time, or a year when it has no limit: rows left RUNNING by an
# outage that lost Slurm's state must not take a new job years later.
ACTIVE_FOR_AT_MOST = timedelta(days=365)

# Columns that hold a job number, in any table.
_ID_COLUMNS = ("job_id", "job_id_a", "job_id_b")

# Columns 1.7.42 adds to jobs (from sacct): what Slurm allocated, the account,
# and where the job ran from (first and last two parts of its working
# directory; never the whole path).
JOB_COLUMNS = (
    ("alloc_tres", "TEXT"),
    ("alloc_gpus", "INTEGER"),
    ("account", "TEXT"),
    ("work_root", "TEXT"),
    ("work_tail", "TEXT"),
)


def ensure_job_columns(conn: sqlite3.Connection) -> None:
    """Add JOB_COLUMNS to jobs where missing (a no-op once they exist)."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    if not have:
        return
    for col, kind in JOB_COLUMNS:
        if col not in have:
            try:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {kind}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise


def norm_time(value) -> str | None:
    """A stored or parsed time as 'YYYY-MM-DDTHH:MM:SS', comparable as text."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0, tzinfo=None).isoformat()
    text = str(value).strip().replace(" ", "T")[:19]
    if len(text) < 10:
        return None
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return None
    return text


def _state_word(state) -> str:
    parts = str(state or '').split()
    return parts[0].rstrip('+').upper() if parts else ''


def aside_id(job_id: str, submit) -> str:
    """The id an older job with this number is kept under: '4711@2026-03-01T10:00:00'."""
    return f"{job_id}@{norm_time(submit) or 'unknown'}"


def _gap(a: str, b: str) -> timedelta:
    return abs(datetime.fromisoformat(a) - datetime.fromisoformat(b))


def same_job(stored, submit, user_name=None, job_name=None) -> bool:
    """Whether the stored row (a mapping with submit_time, user_name,
    job_name, state, end_time and optionally req_time_seconds) is the job a
    record with this submit time, user and name describes -- the same job
    seen again, or requeued -- as opposed to another job that got the number."""
    submit = norm_time(submit)
    stored_submit = norm_time(stored["submit_time"])
    if submit is None or stored_submit is None or submit == stored_submit:
        return True
    s_user, s_name = stored["user_name"], stored["job_name"]
    if user_name and s_user and user_name != s_user:
        return False
    if job_name and s_name and job_name != s_name:
        return False
    if _state_word(stored["state"]) in ACTIVE_STATES:
        try:
            limit = int(stored["req_time_seconds"] or 0)
        except (KeyError, IndexError, TypeError, ValueError):
            limit = 0
        window = timedelta(seconds=limit) + REUSED_AFTER if limit > 0 else ACTIVE_FOR_AT_MOST
        # From the last sign of life: a job can wait long before it runs.
        try:
            started = norm_time(stored["start_time"])
        except (KeyError, IndexError):
            started = None
        last = max(t for t in (stored_submit, started) if t)
        return _gap(submit, last) <= window
    ended = norm_time(stored["end_time"]) or stored_submit
    return _gap(ended, submit) <= REUSED_AFTER


def id_tables(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(table, column) for every column holding a job number."""
    out = []
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"):
        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')}
        out.extend((name, c) for c in _ID_COLUMNS if c in cols)
    return out


def _scope(conn, table: str, col: str, job_id: str, before, old_submit):
    """WHERE clause and arguments for the rows of ``table`` that belong to
    the earlier job ``job_id``: a submit_time column must be its submit time
    (another cluster's job with the number has its own); a timestamp must be
    before the new job's submission (``before``); else every row."""
    cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    where, args = f'"{col}" = ?', [job_id]
    if "submit_time" in cols and old_submit:
        where += ' AND ("submit_time" IS NULL OR julianday("submit_time") = julianday(?))'
        args.append(old_submit)
    elif "timestamp" in cols and before:
        where += ' AND ("timestamp" IS NULL OR julianday("timestamp") < julianday(?))'
        args.append(before)
    return where, tuple(args)


def move_aside(conn: sqlite3.Connection, job_id: str, new_id: str, before=None,
               old_submit=None) -> int:
    """Rename job ``job_id`` to ``new_id`` in every table; rows changed in jobs.

    jobs first: when ``new_id`` is already taken there, nothing moves. Rows
    in other tables move by ``_scope``: those with another submit time than
    the earlier job's (``old_submit``), or a timestamp from the new job's
    submission (``before``) on, stay -- written for the new job, or another
    cluster's, by code that runs before the job collectors see it (the job
    monitor's samples, the groups collector's accounting). A job that
    moves aside while still marked active can't be running any more (its
    number is another job's), so it becomes UNKNOWN.
    """
    row = conn.execute("SELECT state FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    moved = conn.execute("UPDATE OR IGNORE jobs SET job_id = ? WHERE job_id = ?",
                         (new_id, job_id)).rowcount
    if not moved:
        return 0
    if row is not None and _state_word(row[0]) in ACTIVE_STATES:
        conn.execute("UPDATE jobs SET state = 'UNKNOWN' WHERE job_id = ?", (new_id,))
    before, old_submit = norm_time(before), norm_time(old_submit)
    for table, col in id_tables(conn):
        if table == "jobs":
            continue
        where, args = _scope(conn, table, col, job_id, before, old_submit)
        conn.execute(f'UPDATE OR IGNORE "{table}" SET "{col}" = ? WHERE {where}',
                     (new_id, *args))
    return moved


def decide(conn: sqlite3.Connection, job_id, submit_time, user_name=None,
           job_name=None) -> tuple[str, str | None]:
    """Where a job record goes, without writing anything: (kind, id).

    - ``("new", number)``: no stored row has the number.
    - ``("same", number)``: the stored row is this job (seen again, or
      requeued); the record updates it.
    - ``("stale", None)``: the stored row is this same job, seen later than
      this record; keep what is stored.
    - ``("older", aside)``: another, newer job holds the number (this is a
      record from the past, such as an imported history); it goes under its
      own ``NUMBER@SUBMIT``.
    - ``("move", aside)``: another, older job holds the number; that job moves
      to ``aside`` (or merges there, if it is already kept there) and the
      record takes the number. ``aside`` is None when no id is free.
    """
    if not job_id:
        return "new", job_id
    job_id = str(job_id)
    submit = norm_time(submit_time)
    stored = stored_job(conn, job_id)
    if stored is None:
        return "new", job_id
    stored_submit = norm_time(stored["submit_time"])
    if submit is None or stored_submit is None or stored_submit == submit:
        return "same", job_id
    if same_job(stored, submit, user_name, job_name):
        return ("same", job_id) if submit > stored_submit else ("stale", None)
    if submit < stored_submit:
        return "older", _free_id(conn, aside_id(job_id, submit), submit, user_name, job_name)
    return "move", _free_id(conn, aside_id(job_id, stored_submit), stored_submit,
                            stored["user_name"], stored["job_name"])


def place(conn: sqlite3.Connection, job_id, submit_time, user_name=None,
          job_name=None) -> str | None:
    """The job id to write this job record under (see ``decide``), after
    moving an older, different job out of the way. None: don't write it --
    the stored row is this same job seen later, or no id was free."""
    kind, where = decide(conn, job_id, submit_time, user_name, job_name)
    if kind in ("new", "same", "stale", "older"):
        return where
    job_id = str(job_id)
    stored_submit = norm_time(stored_job(conn, job_id)["submit_time"])
    submit = norm_time(submit_time)
    if where is not None and stored_job(conn, where) is not None:
        # The earlier job is already kept there (stored twice): one copy.
        _merge_into(conn, job_id, where, before=submit, old_submit=stored_submit)
        logger.info(f"job {job_id}: the earlier job was already kept as {where}")
        return job_id
    if where is None or not move_aside(conn, job_id, where, before=submit,
                                       old_submit=stored_submit):
        logger.warning(f"job {job_id}: could not move the earlier job aside; "
                       f"this record is not stored")
        return None
    logger.info(f"job {job_id}: the number came back for a new job; "
                f"the earlier job is kept as {where}")
    return job_id


def stored_job(conn: sqlite3.Connection, job_id) -> dict | None:
    """The stored row of ``job_id`` as same_job() wants it, or None."""
    try:
        row = conn.execute(
            "SELECT submit_time, user_name, job_name, state, end_time, req_time_seconds, "
            "start_time FROM jobs WHERE job_id = ?", (str(job_id),)).fetchone()
    except sqlite3.OperationalError:
        # A jobs table without those columns (an old or hand-made one).
        try:
            row = conn.execute(
                "SELECT submit_time, user_name, job_name, state, end_time, NULL, NULL "
                "FROM jobs WHERE job_id = ?", (str(job_id),)).fetchone()
        except sqlite3.OperationalError:
            return None
    if row is None:
        return None
    return dict(zip(("submit_time", "user_name", "job_name", "state", "end_time",
                     "req_time_seconds", "start_time"), row))


def _free_id(conn: sqlite3.Connection, wanted: str, submit=None, user_name=None,
             job_name=None) -> str | None:
    """Where the job (submit, user, name) is kept aside: ``wanted`` or
    ``wanted~2`` ... ``wanted~9`` -- the one already holding this same job,
    else the first free one; None if all hold other jobs."""
    free = None
    for candidate in [wanted] + [f"{wanted}~{n}" for n in range(2, 10)]:
        held = stored_job(conn, candidate)
        if held is None:
            if free is None:
                free = candidate
            continue
        if (norm_time(held["submit_time"]) == norm_time(submit)
                and (not user_name or not held["user_name"] or held["user_name"] == user_name)
                and (not job_name or not held["job_name"] or held["job_name"] == job_name)):
            return candidate
    return free


def _merge_into(conn: sqlite3.Connection, job_id: str, kept_id: str, before=None,
                old_submit=None) -> None:
    """``job_id`` is a second copy of the job kept as ``kept_id``: its rows go
    there where that has none, the rest of the copy is dropped, and
    ``job_id`` is free. Rows are the copy's by the same rule as move_aside."""
    before, old_submit = norm_time(before), norm_time(old_submit)
    for table, col in id_tables(conn):
        if table == "jobs":
            continue
        where, args = _scope(conn, table, col, job_id, before, old_submit)
        conn.execute(f'UPDATE OR IGNORE "{table}" SET "{col}" = ? WHERE {where}',
                     (kept_id, *args))
        conn.execute(f'DELETE FROM "{table}" WHERE {where}', args)
    conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))


__all__ = ["ACTIVE_STATES", "JOB_COLUMNS", "aside_id", "decide", "ensure_job_columns",
           "id_tables", "move_aside", "norm_time", "place", "same_job", "stored_job"]

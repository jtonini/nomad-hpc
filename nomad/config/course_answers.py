# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What faculty members said about the courses they teach, term by term.

Course groups are reused term after term, and a group's faculty members are
often last year's instructors too; the person teaching a course this term is
the one who knows. The Console asks the faculty members of a course group
(``nomad.facts.courses.questions``) and records the answer here; nomad.toml's
decisions (``nomad course add|exclude``) still win over it, and it wins over
nomad's guess (``nomad.config.courses.teaching``).

The answers live in their own small SQLite file beside the hub's combined
database (``course_answers.db``): the combined database is rebuilt from the
sites, and nomad.toml is the site's settings, not a place for what people
say. One row per term, course group and person: ``teaches`` 1 or 0, when.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

FILE = "course_answers.db"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS course_answers (
    term        TEXT NOT NULL,      -- the term's first day, YYYY-MM-DD
    group_name  TEXT NOT NULL,
    netid       TEXT NOT NULL,
    teaches     INTEGER NOT NULL,   -- 1: teaches it this term; 0: doesn't
    answered_at TEXT NOT NULL,
    PRIMARY KEY (term, group_name, netid)
)"""


class AnswersError(Exception):
    """The answers file is there but can't be read or written (another
    account's, say): what people said would silently not count."""


def path_beside(db_path) -> Path:
    """The answers file beside a database (the hub's combined.db)."""
    return Path(db_path).expanduser().parent / FILE


def _readonly(p: Path):
    return sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)


def _has_table(conn) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                        "AND name = 'course_answers'").fetchone() is not None


def load(path, term: str) -> dict:
    """{group: {netid: True/False}} for ``term``; {} when nothing was said
    (no file yet). AnswersError when the file can't be read."""
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    try:
        conn = _readonly(p)
        try:
            rows = conn.execute("SELECT group_name, netid, teaches FROM course_answers "
                                "WHERE term = ?", (term,)).fetchall() if _has_table(conn) else []
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise AnswersError(f"{p}: can't read it ({e})") from e
    out: dict = {}
    for group, netid, teaches in rows:
        out.setdefault(group, {})[str(netid).lower()] = bool(teaches)
    return out


def listing(path, term: str | None = None) -> list[dict]:
    """Every answer (``term``: that term's), newest first. AnswersError
    when the file can't be read."""
    p = Path(path).expanduser()
    if not p.exists():
        return []
    try:
        conn = _readonly(p)
        try:
            if not _has_table(conn):
                return []
            sql = ("SELECT term, group_name, netid, teaches, answered_at FROM course_answers"
                   + (" WHERE term = ?" if term else "") + " ORDER BY answered_at DESC")
            return [{"term": t, "group": g, "netid": n, "teaches": bool(x), "answered_at": a}
                    for t, g, n, x, a in conn.execute(sql, (term,) if term else ())]
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise AnswersError(f"{p}: can't read it ({e})") from e


def record(path, term: str, group: str, netid: str, teaches: bool | None,
           now: datetime | None = None) -> None:
    """Record (``teaches`` None: forget) what ``netid`` said about ``group``
    in ``term`` (its first day, YYYY-MM-DD). The file is readable by its
    owner only -- the owner of the directory it is in, also when root
    writes it, so the Console (that account) can go on reading it."""
    from nomad.config.courses import term_key
    try:
        term = term_key(term)
    except ValueError as e:
        raise AnswersError(f"{term!r}: not a term's first day (YYYY-MM-DD)") from e
    group, n = group.strip(), netid.strip().lower()
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        fd = os.open(p, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            owner = p.parent.stat()
            os.chown(p, owner.st_uid, owner.st_gid)
    elif p.stat().st_mode & 0o077:
        try:
            os.chmod(p, 0o600)
        except OSError as e:
            raise AnswersError(f"{p}: readable by others and not ours to change ({e})") from e
    try:
        conn = sqlite3.connect(p, timeout=30)
        try:
            conn.execute(_SCHEMA)
            if teaches is None:
                conn.execute("DELETE FROM course_answers WHERE term = ? AND group_name = ? "
                             "AND netid = ?", (term, group, n))
            else:
                conn.execute("INSERT OR REPLACE INTO course_answers VALUES (?, ?, ?, ?, ?)",
                             (term, group, n, 1 if teaches else 0,
                              (now or datetime.now()).isoformat(timespec="seconds")))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise AnswersError(f"{p}: can't write it ({e})") from e

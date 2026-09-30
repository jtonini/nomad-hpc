# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Read one site's rows out of a NØMAÐ database, without copying it.

A combined database (built by ``nomad sync``) holds every site's rows, each
tagged with ``source_site``. The analysis engines were written against a
single site's database and query plain table names (``FROM jobs``), so they
used to be handed a copy of the combined database filtered to one site --
several gigabytes written to /tmp on every request.

:func:`connect` does the same job with no copying: it opens the database
read-only and, when a site is given, creates a TEMP view named after every
table that carries ``source_site``, showing only that site's rows. SQLite
resolves an unqualified table name in the temp schema first, so every
existing query reads the view. Tables without ``source_site`` are read as
they are.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

SITE_COLUMN = "source_site"


def _ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def connect(db_path, site: str | None = None) -> sqlite3.Connection:
    """A read-only connection, limited to one site's rows when ``site`` is given.

    Raises ``sqlite3.OperationalError`` when the database does not exist
    (read-only mode never creates one).
    """
    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    if site:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM main.sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        for table in tables:
            cols = {r[1] for r in conn.execute(
                f"PRAGMA main.table_info({_ident(table)})")}
            if SITE_COLUMN in cols:
                conn.execute(
                    f"CREATE TEMP VIEW {_ident(table)} AS "
                    f"SELECT * FROM main.{_ident(table)} "
                    f"WHERE {SITE_COLUMN} = {_literal(site)}")
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """Column names of a table or view (empty when it does not exist)."""
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({_ident(table)})")}
    except sqlite3.Error:
        return set()


def is_combined(db_path) -> bool:
    """True when the database carries more than one site's rows."""
    return len(sites(db_path)) > 1


def sites(db_path) -> list[str]:
    """The sites whose rows a database holds, sorted.

    A combined database records them in ``sync_sites``; otherwise they are
    read from the ``source_site`` column of the tables most sites fill. A
    single-site database without ``source_site`` returns [].
    """
    try:
        conn = connect(db_path)
    except sqlite3.Error:
        return []
    try:
        found: set[str] = set()
        if table_columns(conn, "sync_sites") >= {"name"}:
            found.update(r[0] for r in conn.execute(
                "SELECT name FROM sync_sites") if r[0])
            if found:
                return sorted(found)
        for table in ("filesystems", "jobs", "node_state", "workstation_state"):
            if SITE_COLUMN in table_columns(conn, table):
                found.update(r[0] for r in conn.execute(
                    f"SELECT DISTINCT {SITE_COLUMN} FROM {_ident(table)}") if r[0])
        return sorted(found)
    finally:
        conn.close()


def require_site(db_path, site: str | None, pooled_ok: bool = False) -> str | None:
    """The site to read, checked against the database.

    Raises ValueError for a site the database doesn't hold, and -- unless
    ``pooled_ok`` -- when a combined database is given no site: analyses
    that pool sites mix one site's nodes and jobs with another's (two
    "node01"s become one node flapping between states).
    """
    known = sites(db_path)
    name = Path(db_path).name
    if site:
        if known and site not in known:
            raise ValueError(f"No site '{site}' in {name}; it holds: {', '.join(known)}.")
        return site
    if len(known) > 1 and not pooled_ok:
        raise ValueError(f"{name} holds {len(known)} sites ({', '.join(known)}); "
                         f"choose one (--site).")
    return None


def newest(conn: sqlite3.Connection, table: str,
           column: str = "timestamp") -> datetime | None:
    """The newest ``column`` value in ``table`` (through any site view)."""
    if column not in table_columns(conn, table):
        return None
    try:
        row = conn.execute(
            f"SELECT {_ident(column)} FROM {_ident(table)} "
            f"WHERE {_ident(column)} IS NOT NULL "
            f"ORDER BY {_ident(column)} DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    return parse_time(row[0]) if row else None


def parse_time(value) -> datetime | None:
    """A stored timestamp as a naive local datetime, or None."""
    if value is None:
        return None
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is not None:
        t = t.astimezone().replace(tzinfo=None)
    return t

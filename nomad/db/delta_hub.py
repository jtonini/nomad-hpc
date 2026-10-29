# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The hub's side of the incremental sync (see nomad.db.delta_site).

The combined database keeps, for every row a site sent, which site it came
from (``_sync_site``) and its rowid there (``_src_rowid``), with a unique
index on the two: a row sent again replaces itself, a row deleted there is
deleted here. Each site's place in its log is kept in ``_nomad_sync_state``
(the log's epoch and the last sequence number applied).

* ``apply_snapshot``: a whole copy of a site's database (the first time, or
  when its log started over), in place of what the hub had from that site.
  The full rebuild (``nomad sync --full``, and the first sync after an
  upgrade) is this, for every site, into a new database.
* ``apply_delta``: what changed since, as the site's exporter sent it.

Both run in one transaction per site: a site that fails keeps what it had,
and its place in its log, and is asked again next time.

What this keeps from the full rebuild: each table gets the columns its sites
have (added as they appear), a site's own ``id`` column is not copied (the
hub numbers its rows itself), ``source_site`` is the site's name unless the
site's table has its own, and a table a site no longer has loses that site's
rows. What INSERT OR REPLACE does on a site (deleting a row whose unique key
the new one shares, without telling the delete trigger) the site's log notes
itself (see nomad.db.delta_site), so such a row comes as deleted.

Each site's answer is spooled to a file and applied as it is read, so the hub
holds one record at a time however much a site sends (a migration that adds
a column to a large table sends that table whole).
"""
from __future__ import annotations

import hashlib
import io
import logging
import os
import shlex
import socket
import sqlite3
import subprocess as sp
import tempfile
from datetime import datetime
from pathlib import Path

from nomad.db import delta_site

logger = logging.getLogger("nomad.sync")

FORMAT = "2"
STATE = "_nomad_sync_state"
META = "_nomad_sync_meta"
SITE_COL, ROWID_COL = "_sync_site", "_src_rowid"
# Each host's own working state, never merged (see nomad sync).
SKIP_TABLES = frozenset({
    'schema_version', 'schema_migrations', 'sqlite_sequence', 'config',
    'per_user_state', 'interactive_state', 'oom_state', 'node_disks_sweeps', 'storage_reads',
    'policy_watch_sweeps', 'policy_watch_hosts',
    # A demo's marker (1.17.4): a hub's database is never a demo.
    'demo_info',
})
INIT_SECONDS = 120
EXPORT_SECONDS = 900
# Every table of a site sent whole (no snapshot possible there): millions of rows.
WHOLE_SECONDS = 3600
# The hub's write-ahead log is cut back to this once it has been folded into
# the database (a whole copy of a site leaves it that site's size otherwise).
WAL_LIMIT = 64 * 1024 * 1024
# How long the hub waits for readers (the Console) before cutting it back.
WAL_WAIT_SECONDS = 30.0
STATE_COLUMNS = (("site", "TEXT PRIMARY KEY"), ("epoch", "TEXT"), ("seq", "INTEGER"), ("mode", "TEXT"),
                 ("updated_at", "TEXT"), ("failures", "INTEGER"), ("host", "TEXT"),
                 ("ssh_user", "TEXT"), ("db_path", "TEXT"), ("ssh_key", "TEXT"), ("token", "TEXT"))
# What says the exporter itself can't run at the site (python3 without its
# sqlite3 module, or too old), not that this attempt failed: looked for in
# the error's last line only (a login shell may print anything before it).
_CANT_RUN = ("ModuleNotFoundError", "ImportError", "SyntaxError")


class DeltaError(Exception):
    """The site's exporter could not run or its answer was not whole.
    ``cant_run``: it can't run there at all (no point asking again before
    copying the site whole)."""

    def __init__(self, message, cant_run=False):
        super().__init__(message)
        self.cant_run = cant_run


def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=60, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 60000")
    conn.execute(f"PRAGMA journal_size_limit = {WAL_LIMIT}")
    return conn


def hub_id(combined_path) -> str:
    """This hub as the sites know it: its host and its combined database (a
    trial sync to another file is another hub, with its own place in each
    site's log)."""
    where = f"{socket.gethostname()}:{os.path.realpath(str(combined_path))}"
    return "h" + hashlib.sha1(where.encode("utf-8")).hexdigest()[:12]


def shrink_wal(conn, wait_seconds: float | None = None) -> bool:
    """Fold the write-ahead log into the database and cut it back to
    nothing, waiting a little for readers (the Console) to finish; else as
    much as can be folded now. True when it was emptied."""
    import time
    deadline = time.monotonic() + (WAL_WAIT_SECONDS if wait_seconds is None else wait_seconds)
    try:
        conn.execute("PRAGMA busy_timeout = 1000")
        while True:
            busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy and busy[0] == 0:
                return True
            if time.monotonic() >= deadline:
                conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
                return False
            time.sleep(0.5)
    except sqlite3.Error:
        return False
    finally:
        try:
            conn.execute("PRAGMA busy_timeout = 60000")
        except sqlite3.Error:
            pass


# -- the hub's bookkeeping --------------------------------------------------------------

def ensure_bookkeeping(conn) -> None:
    conn.execute(f"CREATE TABLE IF NOT EXISTS {STATE} ("
                 + ", ".join(f"{c} {t}" for c, t in STATE_COLUMNS) + ")")
    have = {r[1] for r in conn.execute(f"PRAGMA main.table_info({STATE})")}
    for c, t in STATE_COLUMNS:
        if c not in have:
            conn.execute(f"ALTER TABLE {STATE} ADD COLUMN {c} {t}")
    conn.execute(f"CREATE TABLE IF NOT EXISTS {META} (key TEXT PRIMARY KEY, value TEXT)")


def set_meta(conn, key: str, value: str) -> None:
    ensure_bookkeeping(conn)
    conn.execute(f"INSERT OR REPLACE INTO {META} (key, value) VALUES (?, ?)", (key, value))


def get_meta(conn, key: str):
    try:
        row = conn.execute(f"SELECT value FROM {META} WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def is_incremental(path) -> bool:
    """Whether ``path`` is a combined database this version updates in place."""
    path = Path(path)
    if not path.is_file():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        try:
            return get_meta(conn, "format") == FORMAT
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def get_state(conn, site: str):
    try:
        row = conn.execute(f"SELECT epoch, seq, mode, token FROM {STATE} WHERE site = ?",
                           (site,)).fetchone()
    except sqlite3.Error:
        return None
    return {"epoch": row[0], "seq": row[1], "mode": row[2], "token": row[3]} if row else None


def set_state(conn, site: str, epoch, seq, mode: str, token=None) -> None:
    """A site's place in its log (``mode`` "delta"), or "copy": copied
    whole, and again next time; ``token``, what the site called its last
    answer (sent back next time: a site restored from a backup won't know
    it). Clears its failures."""
    ensure_bookkeeping(conn)
    conn.execute(f"INSERT INTO {STATE} (site, epoch, seq, mode, updated_at, failures, token) "
                 "VALUES (?, ?, ?, ?, ?, 0, ?) ON CONFLICT (site) DO UPDATE SET epoch = excluded.epoch, "
                 "seq = excluded.seq, mode = excluded.mode, updated_at = excluded.updated_at, "
                 "failures = 0, token = excluded.token",
                 (site, epoch, seq, mode, datetime.now().isoformat(), token))


def note_failure(conn, site: str) -> int:
    """One more failed attempt in a row at ``site``; how many now."""
    ensure_bookkeeping(conn)
    conn.execute(f"INSERT INTO {STATE} (site, failures) VALUES (?, 1) "
                 "ON CONFLICT (site) DO UPDATE SET failures = COALESCE(failures, 0) + 1", (site,))
    return int(conn.execute(f"SELECT failures FROM {STATE} WHERE site = ?", (site,)).fetchone()[0])


def remember_site(conn, site: dict) -> None:
    """Where a site is, so it can be told when it is taken out of [hub] sites."""
    ensure_bookkeeping(conn)
    conn.execute(f"UPDATE {STATE} SET host = ?, ssh_user = ?, db_path = ?, ssh_key = ? WHERE site = ?",
                 (site.get("host"), site.get("ssh_user") or site.get("user"), site.get("db_path"),
                  str(site["ssh_key"]) if site.get("ssh_key") else None, site.get("name")))


def stored_site(conn, name: str):
    """A site as remember_site kept it, or None."""
    try:
        row = conn.execute(f"SELECT host, ssh_user, db_path, ssh_key FROM {STATE} WHERE site = ?",
                           (name,)).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    site = {"name": name, "host": row[0], "db_path": row[2]}
    if row[1]:
        site["ssh_user"] = row[1]
    if row[3]:
        site["ssh_key"] = row[3]
    return site


# -- tables ------------------------------------------------------------------------------

def _hub_columns(conn, table: str) -> list:
    return [r[1] for r in conn.execute(f"PRAGMA main.table_info({_q(table)})")]


def ensure_table(conn, table: str, cols: list) -> None:
    """``table`` with the site's columns (but ``id``), ``source_site`` and the
    two keys, and the unique index on them; columns added as sites gain them."""
    defs = [(c, t or "TEXT") for c, t in cols if c != "id"]
    names = [c for c, _ in defs]
    extra = ([] if "source_site" in names else [("source_site", "TEXT")]) + \
        [(SITE_COL, "TEXT"), (ROWID_COL, "INTEGER")]
    body = ", ".join(["id INTEGER PRIMARY KEY AUTOINCREMENT"]
                     + [f"{_q(c)} {t}" for c, t in defs + extra])
    conn.execute(f"CREATE TABLE IF NOT EXISTS main.{_q(table)} ({body})")
    have = set(_hub_columns(conn, table))
    for c, t in defs + extra:
        if c not in have:
            conn.execute(f"ALTER TABLE main.{_q(table)} ADD COLUMN {_q(c)} {t}")
    conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS main.{_q('_nomad_src_' + table)} "
                 f"ON {_q(table)} ({SITE_COL}, {ROWID_COL})")


def site_tables(conn) -> list:
    """The hub's tables that hold rows from sites."""
    out = []
    for (name,) in conn.execute("SELECT name FROM main.sqlite_master WHERE type = 'table'"):
        if name.startswith("sqlite_") or name.startswith("_nomad_"):
            continue
        if SITE_COL in _hub_columns(conn, name):
            out.append(name)
    return out


def forget_site(conn, site: str) -> int:
    """A site taken out of [hub] sites: its rows, its place in its log, its
    sync_sites row (as a full rebuild would have left it out)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        n = drop_gone(conn, site, ())
        conn.execute(f"DELETE FROM {STATE} WHERE site = ?", (site,))
        try:
            conn.execute("DELETE FROM sync_sites WHERE name = ?", (site,))
        except sqlite3.Error:
            pass
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return n


def drop_gone(conn, site: str, present) -> int:
    """A site's rows in tables it no longer has."""
    n = 0
    present = set(present)
    for t in site_tables(conn):
        if t not in present:
            n += conn.execute(f"DELETE FROM main.{_q(t)} WHERE {SITE_COL} = ?", (site,)).rowcount
    return n


# -- a whole copy ------------------------------------------------------------------------

def _source_tables(conn, schema: str, skip) -> list:
    out = []
    for name, sql in conn.execute(f"SELECT name, sql FROM {schema}.sqlite_master WHERE type = 'table'"):
        if name.startswith("sqlite_") or name.startswith("_nomad_") or name in skip:
            continue
        if sql and "WITHOUT ROWID" in sql.upper().replace("\n", " "):
            continue
        out.append(name)
    return sorted(out)


def _snapshot_place(conn, schema: str):
    """The site log's epoch and last sequence number as the copy has them."""
    try:
        row = conn.execute(f"SELECT value FROM {schema}._nomad_capture_meta WHERE key = 'epoch'").fetchone()
        epoch = row[0] if row else None
    except sqlite3.Error:
        return None, None
    if not epoch:
        return None, None
    seq = conn.execute(f"SELECT MAX(seq) FROM {schema}._nomad_changes").fetchone()[0]
    if seq is None:
        row = conn.execute(f"SELECT seq FROM {schema}.sqlite_sequence WHERE name = '_nomad_changes'"
                           ).fetchone()
        seq = row[0] if row else 0
    return epoch, int(seq)


def apply_snapshot(conn, site: str, copy_path, skip=SKIP_TABLES, record_state: bool = True,
                   trust_log: bool = True, token=None) -> dict:
    """A site's whole database (``copy_path``, a consistent copy) in place of
    what the hub had from it. Returns records, epoch, seq (None when the copy
    has no log: an older nomad there, copied whole every time). With
    ``trust_log`` False (the log could not be set up just before the copy,
    or the copy is of the live file, which can be torn), the copy's place in
    the log is not taken: the site is copied whole again next time.
    ``token``: what the site's --init called this copy (sent back next time)."""
    # ATTACH makes a database that isn't there: a copy that vanished would
    # take every row of the site out of the hub.
    if not Path(copy_path).is_file() or Path(copy_path).stat().st_size == 0:
        raise DeltaError(f"the copy {copy_path} is not there")
    conn.execute("ATTACH DATABASE ? AS src", (str(copy_path),))
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            records = 0
            present = _source_tables(conn, "src", skip)
            if not present:
                raise DeltaError("the copy has no tables")
            for t in present:
                cols = [(r[1], r[2] or "") for r in conn.execute(f"PRAGMA src.table_info({_q(t)})")]
                ensure_table(conn, t, cols)
                kept = [c for c, _ in cols if c != "id"]
                own_site = "source_site" in kept
                dest = kept + ([] if own_site else ["source_site"]) + [SITE_COL, ROWID_COL]
                sel = [f"src.{_q(t)}.{_q(c)}" for c in kept] + ([] if own_site else ["?"]) + ["?", "rowid"]
                conn.execute(f"DELETE FROM main.{_q(t)} WHERE {SITE_COL} = ?", (site,))
                args = ([] if own_site else [site]) + [site]
                cur = conn.execute(f"INSERT INTO main.{_q(t)} ({', '.join(_q(c) for c in dest)}) "
                                   f"SELECT {', '.join(sel)} FROM src.{_q(t)}", args)
                records += max(cur.rowcount, 0)
            drop_gone(conn, site, present)
            epoch, seq = _snapshot_place(conn, "src") if trust_log else (None, None)
            if record_state:
                set_state(conn, site, epoch, seq, "delta" if epoch else "copy",
                          token=token if epoch else None)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("DETACH DATABASE src")
    return {"records": records, "epoch": epoch, "seq": seq}


# -- what changed ------------------------------------------------------------------------

class _Table:
    def __init__(self, conn, site, name, cols, uniq, reset):
        self.conn, self.site, self.name = conn, site, name
        ensure_table(conn, name, cols)
        names = [c for c, _ in cols]
        self.kept = [i for i, c in enumerate(names) if c != "id"]
        kept = [names[i] for i in self.kept]
        self.own_site = "source_site" in kept
        dest = kept + ([] if self.own_site else ["source_site"]) + [SITE_COL, ROWID_COL]
        sets = ", ".join(f"{_q(c)} = excluded.{_q(c)}" for c in kept)
        self.upsert = (f"INSERT INTO main.{_q(name)} ({', '.join(_q(c) for c in dest)}) "
                       f"VALUES ({', '.join('?' * len(dest))}) "
                       f"ON CONFLICT ({SITE_COL}, {ROWID_COL}) DO "
                       + (f"UPDATE SET {sets}" if sets else "NOTHING"))
        if reset:
            conn.execute(f"DELETE FROM main.{_q(name)} WHERE {SITE_COL} = ?", (site,))

    def row(self, sent: list) -> None:
        rid = sent[0]
        values = [delta_site.decode(v) for v in sent[1:]]
        args = [values[i] for i in self.kept] + ([] if self.own_site else [self.site]) + [self.site, rid]
        self.conn.execute(self.upsert, args)

    def delete(self, rids: list) -> int:
        n = 0
        for rid in rids:
            n += self.conn.execute(f"DELETE FROM main.{_q(self.name)} WHERE {SITE_COL} = ? "
                                   f"AND {ROWID_COL} = ?", (self.site, rid)).rowcount
        return n


def apply_delta(conn, site: str, records) -> dict:
    """A site's changes (records from its exporter) applied in place, in one
    transaction, with its new place in its log. Raises DeltaError (and
    changes nothing) when the answer is not whole; returns
    {"need_copy": why} when the site says the hub must copy it instead."""
    it = iter(records)
    try:
        first = next(it)
    except StopIteration:
        raise DeltaError("the site sent nothing") from None
    if "need_copy" in first:
        return {"need_copy": first["need_copy"]}
    if first.get("h") != "nomad-delta":
        raise DeltaError("not a delta")
    conn.execute("BEGIN IMMEDIATE")
    try:
        table, rows, deleted, reset, end = None, 0, 0, [], None
        for rec in it:
            if "r" in rec:
                if table is None:
                    raise DeltaError("rows before a table")
                table.row(rec["r"])
                rows += 1
            elif "x" in rec:
                if table is None:
                    raise DeltaError("deletes before a table")
                deleted += table.delete(rec["x"])
            elif "t" in rec:
                table = _Table(conn, site, rec["t"], [tuple(c) for c in rec["cols"]],
                               rec.get("uniq"), bool(rec.get("reset")))
                if rec.get("reset"):
                    reset.append(rec["t"])
            elif rec.get("end"):
                end = rec
                break
        if end is None:
            raise DeltaError("the answer was cut short")
        deleted += drop_gone(conn, site, end.get("tables") or [])
        set_state(conn, site, first["epoch"], int(end["to"]), "delta", token=end.get("token"))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"rows": rows, "deleted": deleted, "reset": reset, "from": first["from"], "to": end["to"]}


# -- talking to a site -------------------------------------------------------------------

def _script() -> bytes:
    return Path(delta_site.__file__).read_bytes()


def local_db(db_path: str) -> str:
    """A site on the hub itself: its path as copy_local reads it."""
    p = Path(os.path.expanduser(str(db_path)))
    if not p.is_absolute():
        from nomad.config import default_data_dir
        p = default_data_dir() / p
    return str(p)


def run_site(site: dict, args: list, timeout: int, out=None):
    """Run the exporter at ``site`` with ``args`` (its database added). What
    it wrote goes to ``out`` (a binary file) when given, else is returned.
    Raises DeltaError when it can't run there."""
    from nomad.db.pull import error_text, is_local
    db = site.get("db_path") or "~/.local/share/nomad/nomad.db"
    if is_local(site.get("host")):
        sink = out if out is not None else io.BytesIO()
        try:
            rc = delta_site.main(["--db", local_db(db)] + args, out=sink)
        except (sqlite3.Error, OSError) as e:
            raise DeltaError(str(e)) from None
        if rc != 0:
            raise DeltaError(f"the exporter stopped (exit {rc})")
        return None if out is not None else sink.getvalue()
    user = site.get("ssh_user") or site.get("user") or ""
    target = f"{user}@{site['host']}" if user else site["host"]
    cmd = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
    if site.get("ssh_key"):
        cmd += ["-i", str(site["ssh_key"])]
    remote = ("env -u PYTHONHOME -u PYTHONPATH python3 - "
              + " ".join(shlex.quote(a) for a in ["--db", str(db)] + args))
    try:
        done = sp.run(cmd + [target, remote], input=_script(), timeout=timeout,
                      stdout=out if out is not None else sp.PIPE, stderr=sp.PIPE)
    except sp.TimeoutExpired:
        raise DeltaError(f"no answer within {timeout} s") from None
    except OSError as e:
        raise DeltaError(f"ssh could not run: {e}") from None
    err = done.stderr.decode("utf-8", "replace") if isinstance(done.stderr, bytes) else (done.stderr or "")
    if done.returncode != 0:
        why = error_text(err) or f"exit {done.returncode}"
        # 127: no python3 (or no env); 255 is ssh's own failure (the site
        # unreachable), not the exporter's.
        cant_run = done.returncode == 127 or (done.returncode != 255
                                              and any(w in error_text(err) for w in _CANT_RUN))
        raise DeltaError(why, cant_run=cant_run)
    if out is not None:
        return None
    got = done.stdout
    return got if isinstance(got, bytes) else (got or "").encode("utf-8", "replace")


def _first(site: dict, args: list, timeout: int, key: str) -> dict:
    data = run_site(site, args, timeout)
    try:
        recs = list(delta_site.read(data))
    except (ValueError, OSError, EOFError) as e:
        raise DeltaError(f"unreadable answer: {e}") from None
    if not recs or key not in recs[0]:
        raise DeltaError(f"the site did not answer as asked ({key})")
    return recs[0]


def init_site(site: dict, skip=SKIP_TABLES, hub: str = delta_site.DEFAULT_HUB) -> dict:
    """Put the log in place at ``site`` (before a whole copy is taken, so the
    copy says where in the log it stands)."""
    return _first(site, ["--skip", ",".join(sorted(skip)), "--hub", hub, "--init"], INIT_SECONDS, "init")


def remove_log(site: dict) -> int:
    """Take the log and its triggers out at ``site``; the number of triggers removed."""
    return int(_first(site, ["--remove"], INIT_SECONDS, "removed")["removed"])


def forget_at_site(site: dict, hub: str) -> int:
    """Tell ``site`` this hub no longer reads it (its log goes if no other
    hub does); the number of triggers taken out there."""
    return int(_first(site, ["--hub", hub, "--forget"], INIT_SECONDS, "forgot").get("removed") or 0)


def remove_old_spools(spool_dir, hours: float = 2) -> None:
    """Answers and copies left by a hub killed mid-sync (systemd's stop,
    the OOM killer, a reboot: no cleanup ran)."""
    import time
    cutoff = time.time() - hours * 3600
    for pattern in ("delta_*.part", "*.copy.db", "*.copy.db-*", "*.copy.db.*.part"):
        try:
            for p in Path(spool_dir).glob(pattern):
                try:
                    if p.is_file() and p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    pass
        except OSError:
            pass


class Answer:
    """A site's answer, spooled to a file and read back a record at a time."""

    def __init__(self, path: Path):
        self.path = path
        self.size = path.stat().st_size if path.exists() else 0

    def records(self):
        try:
            with open(self.path, "rb") as f:
                yield from delta_site.read_stream(f)
        except DeltaError:
            raise
        except Exception as e:                  # a broken gzip stream, bad JSON, ...
            raise DeltaError(f"unreadable answer: {e}") from None

    def close(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def fetch_delta(site: dict, state: dict, skip=SKIP_TABLES, hub: str = delta_site.DEFAULT_HUB,
                spool_dir=None) -> Answer:
    """The site's changes past ``state``, spooled to a file in ``spool_dir``
    (the system's temporary directory by default)."""
    args = ["--since", str(int(state["seq"] or 0)), "--epoch", str(state["epoch"])]
    if state.get("token"):
        args += ["--token", str(state["token"])]
    return _spooled(site, ["--skip", ",".join(sorted(skip)), "--hub", hub] + args, spool_dir,
                    EXPORT_SECONDS)


def fetch_whole(site: dict, skip=SKIP_TABLES, hub: str = delta_site.DEFAULT_HUB,
                spool_dir=None) -> Answer:
    """Every table of the site whole, from its exporter (a site that can't
    take a snapshot), spooled as fetch_delta's answer is."""
    return _spooled(site, ["--skip", ",".join(sorted(skip)), "--hub", hub, "--whole"], spool_dir,
                    WHOLE_SECONDS)


def _spooled(site: dict, args: list, spool_dir, timeout: int) -> Answer:
    if spool_dir is not None:
        Path(spool_dir).mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f"delta_{site.get('name', 'site')}_", suffix=".part",
                                dir=str(spool_dir) if spool_dir is not None else None)
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            run_site(site, args, timeout, out=f)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise
    return Answer(path)

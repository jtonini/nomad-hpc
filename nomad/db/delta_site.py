# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A site's side of the incremental sync: what changed in its database since
the hub last took it.

``nomad sync`` used to copy every site's whole database on every run and
rebuild the hub's combined database from scratch, about 24 GB of writes a run
at a four-site hub: it wore out the hub's SSD (and each site wrote a full
snapshot of its database for every pull). Instead, each site now keeps a log
of the rows added, changed and deleted (triggers on its tables, written by
its own collectors' transactions), and the hub asks for what is in the log
past the point it has; it applies that in place.

This file is sent to the site by the hub and run there with the site's own
python3 (``python3 - ARGS``, the program on standard input), so it needs
nothing installed but Python 3.6 or later; it uses the standard library only.
The hub also imports it for a site on the hub itself, and the site's own
``nomad collect`` imports it to take out a log no hub reads any more. What it
does:

* ``--init --hub H``: puts the log and the triggers in place (idempotent),
  holds the log for hub H from here (it is about to copy the database), and
  says the log's epoch (a random name, new whenever the log is put back) and
  its last sequence number.
* ``--since N --epoch E --hub H [--token T]``: records that hub H has the log
  up to N, drops the log up to where every hub heard from lately has it, then
  writes every row added or changed after N with its current content, and
  every row deleted after N. A table whose triggers were missing or out of
  date (a migration recreated it, renamed it, or changed its columns or
  unique keys) is sent whole. If the epoch is not E (the log was put back,
  or was never there), the log no longer reaches back to N, N is too far
  behind, or T is not one of the last two answers given to H (the site's
  database restored from a backup since), it says the hub must copy it.
* ``--whole --hub H``: every table sent whole, as of a place in the log the
  hub then continues from: the copy for a site with no way to take a
  snapshot (no sqlite3 command, and a python3 older than 3.7).
* ``--forget --hub H``: hub H no longer reads this site; with no other hub
  heard from lately, the log and its triggers are taken out.
* ``--remove``: the log and its triggers taken out.

Output: a marker line (whatever the site prints before it, a login message,
is not read), then JSON lines, gzip-compressed: a header, per table its
columns and unique keys then its rows (``r``: rowid and values) and deleted
rowids (``x``), and a trailer listing the tables there are now. Nothing is
written on the site but the log and its bookkeeping: no snapshot.

What is logged: an insert, a delete, an update that changes something (and
as a delete plus an insert when it moves the row to another rowid), and,
before an insert or an update, every row holding the unique key the new row
takes (``k``): INSERT OR REPLACE deletes that row without telling the delete
trigger (and UPDATE OR REPLACE too), so such a row is sent as deleted if it
is gone by the export; one still there was not replaced (an upsert, OR
IGNORE) and is not sent for that.

Reads are short (one statement at a time, each read in full before anything
is written out), so a collector is never kept waiting by the export, however
slowly the hub takes the answer: a row that changes while it is read is sent
as it is then, and sent again at the next sync (the log has it), which the
hub applies the same way.

A migration that drops a column (ALTER TABLE ... DROP COLUMN) must first take
that table's triggers out (``untrack``), since they name its columns: the
next sync puts them back and sends the table whole.
"""
import argparse
import base64
import gzip
import hashlib
import io
import json
import os
import secrets
import sqlite3
import sys
import time

MARKER = b"__NOMAD_DELTA_BEGINS__\n"
FORMAT = 2
PREFIX = "_nomad_"
LOG = "_nomad_changes"
CAPTURE = "_nomad_capture"
META = "_nomad_capture_meta"
# A hub more than this many logged changes behind (silent for a day or
# more: busy collectors log tens of thousands an hour) copies the database
# instead; the log is dropped past it once twice this is waiting.
MAX_LOG = 3000000
BATCH = 500
LOG_BATCH = 20000
# Log rows read into memory at a time by an export (a few tens of MB).
PART = 200000
PRUNE_BATCH = 50000
PRUNE_PAUSE = 0.1
DEFAULT_HUB = "hub"
# A hub heard from within this long keeps the log from being dropped past
# its place (a second hub, a trial sync to another file); one silent longer
# is not waited for (it copies the database if it comes back).
RECENT_DAYS = 2
# No hub heard from for this long: nothing reads the log, so the site's own
# collectors take it out (a site taken out of [hub] sites, a hub retired).
EXPIRE_DAYS = 7
KINDS = ("i", "u", "d", "bi", "bu")
# Part of each table's signature: a new form of the triggers puts them back
# (and sends the table whole once).
TRIGGERS = 2
# SQLite before 3.32 (RHEL 8 has 3.26) allows 999 values in one statement.
MAX_VARIABLES = 999


def _q(name):
    return '"' + str(name).replace('"', '""') + '"'


def _lit(value):
    return "'" + str(value).replace("'", "''") + "'"


def _connect(path):
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _now():
    return int(time.time())


def tables(conn, skip=()):
    """The site's data tables: not SQLite's, not this log's, not the hub's
    skip list (each host's own working state), and only rowid tables."""
    out = []
    for name, sql in conn.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'"):
        if name.startswith("sqlite_") or name.startswith(PREFIX) or name in skip:
            continue
        if sql and "WITHOUT ROWID" in sql.upper().replace("\n", " "):
            continue
        out.append(name)
    return sorted(out)


def columns(conn, table):
    """(name, declared type) of each column, in order."""
    return [(r[1], r[2] or "") for r in conn.execute("PRAGMA table_info(%s)" % _q(table))]


def key_specs(conn, table):
    """Each UNIQUE or PRIMARY KEY constraint other than the rowid itself, as
    [(column, collation)]: what INSERT OR REPLACE deletes on. Keys on an
    expression, and partial ones, are left out."""
    keys = []
    for row in conn.execute("PRAGMA index_list(%s)" % _q(table)):
        unique = row[2]
        partial = row[4] if len(row) > 4 else 0
        if not unique or partial:
            continue
        spec = [(r[2], r[4] or "BINARY") for r in conn.execute("PRAGMA index_xinfo(%s)" % _q(row[1]))
                if r[5]]
        if spec and all(c is not None for c, _ in spec):
            keys.append(spec)
    return sorted(keys)


def unique_keys(conn, table):
    """The unique keys' columns (as the hub is told them)."""
    return [[c for c, _ in k] for k in key_specs(conn, table)]


def signature(cols, keys=()):
    text = "%d\x1e" % TRIGGERS + "\x1f".join(c for c, _ in cols) + "\x1e" + "\x1d".join(
        "\x1f".join("%s %s" % (c, coll) for c, coll in k) for k in keys)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def trigger_names(table, keys=()):
    kinds = KINDS if keys else KINDS[:3]
    return [PREFIX + k + "_" + table for k in kinds]


def _differs(col):
    """The column's old and new values differ: byte for byte, and in type
    (a NOCASE or RTRIM column, or 1 and 1.0, compare equal otherwise)."""
    c = _q(col)
    return "OLD.%s IS NOT NEW.%s COLLATE BINARY OR typeof(OLD.%s) IS NOT typeof(NEW.%s)" % (c, c, c, c)


def trigger_sql(table, cols, keys=()):
    """Insert and delete are logged as they happen; an update only when it
    changes something (an upsert that rewrites a row as it was is not news),
    and as a delete plus an insert when it moves the row to another rowid.
    Before an insert, and an update that changes a unique key, the rows
    holding that key are noted (``k``): REPLACE would delete them unseen."""
    qt, lit = _q(table), _lit(table)
    names = [_q(n) for n in trigger_names(table, keys)]
    changed = " OR ".join(["OLD.rowid IS NOT NEW.rowid"] + [_differs(c) for c, _ in cols])
    out = [
        "CREATE TRIGGER %s AFTER INSERT ON %s BEGIN "
        "INSERT INTO %s (tbl, rid, op) VALUES (%s, NEW.rowid, 'u'); END" % (names[0], qt, LOG, lit),
        "CREATE TRIGGER %s AFTER UPDATE ON %s WHEN %s BEGIN "
        "INSERT INTO %s (tbl, rid, op) SELECT %s, OLD.rowid, 'd' WHERE OLD.rowid IS NOT NEW.rowid; "
        "INSERT INTO %s (tbl, rid, op) VALUES (%s, NEW.rowid, 'u'); END"
        % (names[1], qt, changed, LOG, lit, LOG, lit),
        "CREATE TRIGGER %s AFTER DELETE ON %s BEGIN "
        "INSERT INTO %s (tbl, rid, op) VALUES (%s, OLD.rowid, 'd'); END" % (names[2], qt, LOG, lit),
    ]
    if keys:
        def holders(extra):
            return " ".join(
                "INSERT INTO %s (tbl, rid, op) SELECT %s, rowid, 'k' FROM %s WHERE %s%s;"
                % (LOG, lit, qt, " AND ".join("%s = NEW.%s COLLATE %s" % (_q(c), _q(c), coll)
                                              for c, coll in k), extra)
                for k in keys)
        key_cols = sorted({c for k in keys for c, _ in k})
        moved = " OR ".join(_differs(c) for c in key_cols)
        out.append("CREATE TRIGGER %s BEFORE INSERT ON %s BEGIN %s END" % (names[3], qt, holders("")))
        out.append("CREATE TRIGGER %s BEFORE UPDATE ON %s WHEN %s BEGIN %s END"
                   % (names[4], qt, moved, holders(" AND rowid IS NOT OLD.rowid")))
    return out


def _meta(conn, key):
    row = conn.execute("SELECT value FROM %s WHERE key = ?" % META, (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO %s (key, value) VALUES (?, ?)" % META, (key, str(value)))


def _new_epoch():
    return "%s-%s" % (time.strftime("%Y%m%d%H%M%S"), secrets.token_hex(4))


def _our_triggers(conn):
    """{name: the table it is on} for this log's triggers."""
    return dict(conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger' AND substr(name, 1, ?) = ?",
        (len(PREFIX), PREFIX)).fetchall())


def _made_for(name):
    """The table a trigger of this log was made for, from its name."""
    kind, _, table = name[len(PREFIX):].partition("_")
    return table if kind in KINDS else None


def install(conn, skip=(), hub=None):
    """The log and its triggers, as the tables are now. Returns (epoch, the
    tables to be sent whole: tracked before but their triggers were missing
    or no longer matched their columns and keys, or new since the log
    began). With ``hub`` (``--init``, before the hub copies the database):
    that hub holds the log from here, and its earlier answers no longer
    count."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS %s (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "tbl TEXT NOT NULL, rid INTEGER NOT NULL, op TEXT NOT NULL)" % LOG)
        conn.execute("CREATE TABLE IF NOT EXISTS %s (tbl TEXT PRIMARY KEY, sig TEXT)" % CAPTURE)
        conn.execute("CREATE TABLE IF NOT EXISTS %s (key TEXT PRIMARY KEY, value TEXT)" % META)
        epoch = _meta(conn, "epoch")
        fresh = epoch is None
        if fresh:
            epoch = _new_epoch()
            _set_meta(conn, "epoch", epoch)
            _set_meta(conn, "created", _now())
            _set_meta(conn, "pruned", last_seq(conn))
        now = tables(conn, skip)
        here = set(now)
        triggers = _our_triggers(conn)
        for name, on in list(triggers.items()):
            # One left on a table renamed away (ALTER TABLE ... RENAME moves a
            # table's triggers with it), or on a table no longer tracked: it
            # would log that table's rows under another name.
            if _made_for(name) != on or on not in here:
                conn.execute("DROP TRIGGER IF EXISTS %s" % _q(name))
                del triggers[name]
        tracked = dict(conn.execute("SELECT tbl, sig FROM %s" % CAPTURE).fetchall())
        resend = []
        for t in now:
            cols = columns(conn, t)
            keys = key_specs(conn, t)
            sig = signature(cols, keys)
            mine = {n for n, on in triggers.items() if on == t}
            if tracked.get(t) == sig and mine == set(trigger_names(t, keys)):
                continue
            for n in mine:
                conn.execute("DROP TRIGGER IF EXISTS %s" % _q(n))
            for sql in trigger_sql(t, cols, keys):
                conn.execute(sql)
            conn.execute("INSERT OR REPLACE INTO %s (tbl, sig) VALUES (?, ?)" % CAPTURE, (t, sig))
            if not fresh:
                # What happened to it before now was not logged: all of it.
                conn.execute("INSERT INTO %s (tbl, rid, op) VALUES (?, 0, 'r')" % LOG, (t,))
                resend.append(t)
        for t in tracked:
            if t not in here:
                conn.execute("DELETE FROM %s WHERE tbl = ?" % CAPTURE, (t,))
        if hub:
            _set_meta(conn, "heard:" + hub, _now())
            _set_meta(conn, "ack:" + hub, last_seq(conn))
            conn.execute("DELETE FROM %s WHERE key = ?" % META, ("served:" + hub,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return epoch, resend


def remove(conn):
    """The log and its triggers taken out (going back to a nomad that copies
    whole databases, or nothing reads the log any more)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        names = list(_our_triggers(conn))
        for n in names:
            conn.execute("DROP TRIGGER IF EXISTS %s" % _q(n))
        for t in (LOG, CAPTURE, META):
            conn.execute("DROP TABLE IF EXISTS %s" % t)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return len(names)


def untrack(conn, table):
    """``table``'s triggers taken out, before a migration that drops one of
    its columns (the triggers name them, so SQLite refuses the drop). The
    next sync puts them back and sends the table whole."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        names = [n for n, on in _our_triggers(conn).items() if on == table]
        for n in names:
            conn.execute("DROP TRIGGER IF EXISTS %s" % _q(n))
        try:
            conn.execute("DELETE FROM %s WHERE tbl = ?" % CAPTURE, (table,))
        except sqlite3.OperationalError:
            pass                                  # no log here
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return len(names)


def last_seq(conn):
    row = conn.execute("SELECT MAX(seq) FROM %s" % LOG).fetchone()
    if row and row[0] is not None:
        return int(row[0])
    # An empty log: AUTOINCREMENT's high-water mark, so a number is never reused.
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?", (LOG,)).fetchone()
    return int(row[0]) if row else 0


def _hubs(conn):
    """{hub: [its place in the log, when it was last heard from]}."""
    hubs = {}
    for key, value in conn.execute("SELECT key, value FROM %s WHERE key LIKE 'ack:%%' "
                                   "OR key LIKE 'heard:%%'" % META):
        kind, _, hub = key.partition(":")
        try:
            hubs.setdefault(hub, [None, None])[0 if kind == "ack" else 1] = int(float(value))
        except (TypeError, ValueError):
            pass
    return hubs


def _drop_hub(conn, hub):
    """``hub`` no longer holds the log back (in a transaction)."""
    conn.execute("DELETE FROM %s WHERE key IN (?, ?, ?)" % META,
                 ("ack:" + hub, "heard:" + hub, "served:" + hub))


def _floor(conn, now):
    """How far the log may be dropped: to the oldest place among the hubs
    heard from lately (all of it when there are none)."""
    recent = [ack for ack, heard in _hubs(conn).values()
              if ack is not None and heard is not None and heard >= now - RECENT_DAYS * 86400]
    return min(recent) if recent else last_seq(conn)


def prune(conn, now=None):
    """The log dropped as far as every hub heard from lately allows, a batch
    per transaction (a collector waits for one at most). Where that is, is
    read again in each transaction: a hub may be answered meanwhile."""
    now = _now() if now is None else now
    while True:
        conn.execute("BEGIN IMMEDIATE")
        try:
            pruned = int(_meta(conn, "pruned") or 0)
            floor = _floor(conn, now)
            if floor <= pruned:
                conn.execute("COMMIT")
                return
            low = conn.execute("SELECT MIN(seq) FROM %s" % LOG).fetchone()[0]
            step = floor if low is None else min(floor, max(pruned, int(low) - 1) + PRUNE_BATCH)
            conn.execute("DELETE FROM %s WHERE seq <= ?" % LOG, (step,))
            _set_meta(conn, "pruned", max(pruned, step))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if step < floor:
            # A collector waiting to write gets its turn before the next
            # batch (SQLite's busy wait sleeps between tries, and would lose
            # every one to the next batch).
            time.sleep(PRUNE_PAUSE)


def _served(conn, hub):
    value = _meta(conn, "served:" + hub)
    return [t for t in (value or "").split(",") if t]


def forget(conn, hub, now=None):
    """Hub ``hub`` no longer reads this site. With no other hub heard from
    lately the log goes; the number of triggers taken out."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (META,)).fetchone() is None:
        return 0
    now = _now() if now is None else now
    conn.execute("BEGIN IMMEDIATE")
    try:
        _drop_hub(conn, hub)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if not [h for h, (_, heard) in _hubs(conn).items()
            if heard is not None and heard >= now - EXPIRE_DAYS * 86400]:
        return remove(conn)
    prune(conn, now)
    return 0


def expire(conn, now=None, days=EXPIRE_DAYS, max_log=MAX_LOG):
    """Run by the site's own collectors: a log no hub has read for ``days``
    is taken out (nothing would ever drop it), and the hubs more than
    ``max_log`` behind once twice that is waiting stop holding it (each
    copies the database if it comes back). What was done, or None."""
    try:
        meta = dict(conn.execute("SELECT key, value FROM %s" % META).fetchall())
    except sqlite3.OperationalError:
        return None                               # no log here
    if "epoch" not in meta:
        return None
    now = _now() if now is None else now
    times = []
    for key, value in meta.items():
        if key.startswith("heard:") or key == "created":
            try:
                times.append(float(value))
            except (TypeError, ValueError):
                pass
    if times and now - max(times) > days * 86400:
        remove(conn)
        return "removed"
    top = last_seq(conn)
    if top - int(meta.get("pruned") or 0) <= 2 * max_log:
        return None
    conn.execute("BEGIN IMMEDIATE")
    try:
        behind = [h for h, (ack, _) in _hubs(conn).items() if ack is None or ack < top - max_log]
        for hub in behind:
            _drop_hub(conn, hub)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    prune(conn, now)
    return "dropped for %d hub(s) far behind" % len(behind) if behind else None


def _value(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"$b": base64.b64encode(bytes(v)).decode("ascii")}
    return v


def _rows(conn, table, cols, rids):
    names = ", ".join(_q(c) for c, _ in cols)
    for i in range(0, len(rids), BATCH):
        part = rids[i:i + BATCH]
        marks = ", ".join("?" * len(part))
        # Read whole before any is written out: no statement stays open (and
        # no lock held) while the hub takes its time reading the answer.
        batch = conn.execute("SELECT rowid, %s FROM %s WHERE rowid IN (%s)"
                             % (names, _q(table), marks), part).fetchall()
        for row in batch:
            yield row


def _existing(conn, table, rids):
    for i in range(0, len(rids), BATCH):
        part = rids[i:i + BATCH]
        marks = ", ".join("?" * len(part))
        for (rid,) in conn.execute("SELECT rowid FROM %s WHERE rowid IN (%s)"
                                   % (_q(table), marks), part).fetchall():
            yield rid


def _all_rows(conn, table, cols):
    """Every row, a batch per statement (no long read: a collector is never
    kept waiting), in rowid order."""
    names = ", ".join(_q(c) for c, _ in cols)
    last = None
    while True:
        if last is None:
            batch = conn.execute("SELECT rowid, %s FROM %s ORDER BY rowid LIMIT ?"
                                 % (names, _q(table)), (BATCH * 4,)).fetchall()
        else:
            batch = conn.execute("SELECT rowid, %s FROM %s WHERE rowid > ? ORDER BY rowid LIMIT ?"
                                 % (names, _q(table)), (last, BATCH * 4)).fetchall()
        if not batch:
            return
        for row in batch:
            yield row
        last = batch[-1][0]


def _whole_tables(conn, since, top):
    """The tables to be sent whole: an ``r`` in the log past ``since``."""
    whole, at = set(), since
    while at < top:
        batch = conn.execute("SELECT seq, tbl, op FROM %s WHERE seq > ? AND seq <= ? "
                             "ORDER BY seq LIMIT ?" % LOG, (at, top, LOG_BATCH)).fetchall()
        if not batch:
            break
        whole.update(tbl for _seq, tbl, op in batch if op == "r")
        at = batch[-1][0]
    return whole


def _change_parts(conn, since, top, part=None):
    """The log past ``since``, a part at a time: in each, the last thing
    that happened to each row (a ``k``, a row that may have been replaced,
    only when nothing else did), read in sequence order a batch per
    statement."""
    part = PART if part is None else part
    at = since
    while at < top:
        latest, n = {}, 0
        while n < part and at < top:
            batch = conn.execute("SELECT seq, tbl, rid, op FROM %s WHERE seq > ? AND seq <= ? "
                                 "ORDER BY seq LIMIT ?" % LOG, (at, top, LOG_BATCH)).fetchall()
            if not batch:
                at = top
                break
            for _seq, tbl, rid, op in batch:
                if op == "r":
                    continue
                rows = latest.get(tbl)
                if rows is None:
                    rows = latest[tbl] = {}
                if op != "k" or rid not in rows:
                    rows[rid] = op
            n += len(batch)
            at = batch[-1][0]
        if latest:
            yield latest


def _why_copy(conn, since, epoch, hub, token, max_log):
    """Why the hub at (epoch, since, token) must copy the database instead,
    or None (in the transaction that then records its place)."""
    have = _meta(conn, "epoch")
    if epoch is None or have != epoch:
        return "the log is new" if epoch is None else "the log was put back since"
    top = last_seq(conn)
    if since > top:
        return "the hub is ahead of this log"
    if since < int(_meta(conn, "pruned") or 0):
        # Dropped already: the hub was not heard from for days, or fell back
        # to an older place (restored from a backup). What came between is gone.
        return "the log no longer reaches back to the hub's place"
    if token:
        served = _served(conn, hub)
        if not served:
            return "this hub is new to this log (another host, or its combined database moved?)"
        if token not in served:
            return "this is not the log the hub last read (the site's database restored from a backup?)"
    if top - since > max_log:
        return "the hub is more than %d changes behind" % max_log
    return None


def _answered(conn, hub, held=None):
    """A token for an answer just read whole, kept with the one the hub
    holds now (it may not apply this one), in a transaction. Given out only
    once the answer has been read: a backup of the site taken while it was
    read does not know it."""
    token = secrets.token_hex(8)
    served = _served(conn, hub)
    keep = [held] if held and held in served else served[-1:]
    _set_meta(conn, "served:" + hub, ",".join(keep + [token]))
    return token


def _token_for(conn, hub, held):
    conn.execute("BEGIN IMMEDIATE")
    try:
        token = _answered(conn, hub, held)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return token


def init(conn, skip=(), hub=DEFAULT_HUB):
    """``--init``: the log in place, held for ``hub`` from here (it is about
    to copy the database), and a token for that copy."""
    epoch, _ = install(conn, skip, hub)
    token = _token_for(conn, hub, None)
    return {"init": True, "epoch": epoch, "seq": last_seq(conn), "token": token}


def export(conn, since, epoch, skip=(), max_log=MAX_LOG, hub=DEFAULT_HUB, token=None, now=None):
    """The records a hub at (epoch, since) needs, as dicts (see the module's
    description). Records its place (and drops the log that no hub needs)
    first; the answer's token comes last, in the trailer."""
    have_epoch, _ = install(conn, skip)
    since = int(since)
    now = _now() if now is None else now
    # The checks and the record of the hub's place are one transaction, and
    # every prune works out how far it may go in its own: a hub is never
    # answered from a log dropped past its place.
    conn.execute("BEGIN IMMEDIATE")
    try:
        why = _why_copy(conn, since, epoch, hub, token, max_log)
        if why:
            _drop_hub(conn, hub)               # it copies, and holds the log from then
        else:
            _set_meta(conn, "ack:" + hub, since)
            _set_meta(conn, "heard:" + hub, now)
            for other, (_, heard) in _hubs(conn).items():
                if heard is None or heard < now - EXPIRE_DAYS * 86400:
                    _drop_hub(conn, other)
            top = last_seq(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    prune(conn, now)
    if why:
        yield {"need_copy": why, "epoch": have_epoch}
        return
    yield {"h": "nomad-delta", "v": FORMAT, "epoch": have_epoch, "from": since, "to": top}
    present = tables(conn, skip)
    here = set(present)
    whole = sorted(_whole_tables(conn, since, top) & here)
    for t in whole:
        for rec in _table_whole(conn, t):
            yield rec
    # The rest a part of the log at a time (what is held in memory stays
    # small however far behind the hub is): a row changed in two parts is
    # sent twice, as it is now both times, which the hub applies the same.
    for latest in _change_parts(conn, since, top):
        for t in sorted(latest):
            if t not in here or t in whole:
                continue
            for rec in _table_changes(conn, t, latest[t]):
                yield rec
    yield {"end": True, "tables": present, "to": top, "token": _token_for(conn, hub, token)}


def _table_changes(conn, t, changed):
    cols = columns(conn, t)
    yield {"t": t, "cols": cols, "uniq": unique_keys(conn, t), "reset": False}
    upserts = sorted(rid for rid, op in changed.items() if op == "u")
    seen = set()
    for row in _rows(conn, t, cols, upserts):
        seen.add(row[0])
        yield {"r": [_value(v) for v in row]}
    # Deleted, or taken out by INSERT OR REPLACE since (noted just before):
    # either way, not there now.
    hints = sorted(rid for rid, op in changed.items() if op == "k")
    there = set(_existing(conn, t, hints))
    gone = sorted([rid for rid, op in changed.items() if op == "d"]
                  + [rid for rid in upserts if rid not in seen]
                  + [rid for rid in hints if rid not in there])
    for i in range(0, len(gone), BATCH * 4):
        yield {"x": gone[i:i + BATCH * 4]}


def _table_whole(conn, t):
    cols = columns(conn, t)
    yield {"t": t, "cols": cols, "uniq": unique_keys(conn, t), "reset": True}
    for row in _all_rows(conn, t, cols):
        yield {"r": [_value(v) for v in row]}


def export_whole(conn, skip=(), hub=DEFAULT_HUB, now=None):
    """Every table whole, as of a place in the log the hub continues from:
    the triggers are in place before the first row is read, so whatever
    changes while the tables are read is logged after that place and sent
    again at the next sync. For a site that can't take a snapshot."""
    epoch, _ = install(conn, skip)
    now = _now() if now is None else now
    conn.execute("BEGIN IMMEDIATE")
    try:
        top = last_seq(conn)
        have = _meta(conn, "ack:" + hub)
        _set_meta(conn, "ack:" + hub, top if have is None else min(int(have), top))
        _set_meta(conn, "heard:" + hub, now)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    yield {"h": "nomad-delta", "v": FORMAT, "epoch": epoch, "from": None, "to": top, "whole": True}
    present = tables(conn, skip)
    for t in present:
        for rec in _table_whole(conn, t):
            yield rec
    yield {"end": True, "tables": present, "to": top, "token": _token_for(conn, hub, None)}


def write(records, out):
    """The marker, then the records as gzip-compressed JSON lines."""
    out.write(MARKER)
    out.flush()
    with gzip.GzipFile(fileobj=out, mode="wb", compresslevel=6) as z:
        for rec in records:
            z.write(json.dumps(rec, separators=(",", ":")).encode("utf-8") + b"\n")


def read_stream(f, banner_limit=16 * 1024 * 1024):
    """The records in the seekable binary file ``f`` (what a site wrote,
    banner and all), one at a time."""
    buf, start = b"", f.tell()
    while True:
        chunk = f.read(65536)
        if not chunk:
            raise ValueError("no delta in the answer")
        buf += chunk
        pos = buf.find(MARKER)
        if pos >= 0:
            f.seek(start + pos + len(MARKER))
            break
        if len(buf) > banner_limit:
            raise ValueError("no delta in the answer")
    with gzip.GzipFile(fileobj=f, mode="rb") as z:
        for line in z:
            if line.strip():
                yield json.loads(line.decode("utf-8"))


def read(data):
    """The records in ``data`` (bytes a site wrote, banner and all)."""
    return read_stream(io.BytesIO(data))


def decode(value):
    if isinstance(value, dict) and "$b" in value:
        return base64.b64decode(value["$b"])
    return value


def _db_path(path):
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(os.path.expanduser("~"), path)
    return path


def main(argv=None, out=None):
    ap = argparse.ArgumentParser(prog="nomad-delta", description="A site's changes for its hub.")
    ap.add_argument("--db", required=True)
    ap.add_argument("--skip", default="", help="tables not to track, comma-separated")
    ap.add_argument("--hub", default=DEFAULT_HUB, help="which hub is asking")
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--remove", action="store_true", help="take the log and its triggers out")
    ap.add_argument("--forget", action="store_true", help="this hub no longer reads the site")
    ap.add_argument("--whole", action="store_true", help="every table whole (no snapshot possible)")
    ap.add_argument("--since", type=int)
    ap.add_argument("--epoch")
    ap.add_argument("--token")
    ap.add_argument("--max-log", type=int, default=MAX_LOG)
    args = ap.parse_args(argv)
    out = out if out is not None else sys.stdout.buffer
    path = _db_path(args.db)
    if not os.path.isfile(path):
        sys.stderr.write("no database at %s\n" % path)
        return 2
    skip = tuple(s for s in args.skip.split(",") if s)
    conn = _connect(path)
    try:
        if args.remove:
            write([{"removed": remove(conn)}], out)
        elif args.forget:
            write([{"forgot": True, "removed": forget(conn, args.hub)}], out)
        elif args.init:
            write([init(conn, skip, args.hub)], out)
        elif args.whole:
            write(export_whole(conn, skip, args.hub), out)
        else:
            if args.since is None:
                sys.stderr.write("--since, --init or --whole\n")
                return 2
            write(export(conn, args.since, args.epoch, skip, args.max_log, args.hub, args.token), out)
    except sqlite3.Error as e:
        sys.stderr.write("sqlite: %s\n" % e)
        return 2
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Copying a file or a folder from one machine to another (1.58.0): the hub
does it, as the person on both, through each machine's nomad-gate.

A copy is asked from the Console's My files (a file or a folder of theirs,
and a folder of theirs on another machine to put it in) and kept here, in
the Console's own database: it goes on when their browser is closed, and
after the Console restarts (from the file it was at). Nothing of the files
is kept on the hub: each piece passes through.

How a copy goes:

* A folder: the gate lists what is under it (tree, as the person sees it;
  hidden files and folders, links and other kinds left out, and said). Too
  many files and folders (copy_files_max), or too much (copy_max_gb): not
  started, and they are told to use rsync from one machine to the other.
* Room: the machine it goes to must have room for all of it.
* Each folder made there (one there already is gone into); each file read
  in pieces from the one (each with its sha256, the file the same one all
  along), written in pieces on the other (each with its sha256), the whole
  file's sha256 checked there before it is put in its place.
* A file there already, by the name it would have: as they said when they
  asked (``if_there``): left as it is ("skip"), replaced ("replace"; only a
  file of theirs, as My files replaces), or both kept, the new one as
  "name (2).ext" ("keep"). A folder there already by a copied folder's
  name is gone into; with "keep", the copied folder itself gets a name of
  its own ("folder (2)").
* A machine busy moving other files: waited for. A machine that doesn't
  answer: asked again three times over a minute and a half; then the copy
  stops (failed, saying why) and can be started again from where it
  stopped.
* Stopped by them (Cancel): what was sent of the file it was at is
  removed; what was copied before stays.

The engine here is plain (``run`` takes the two machines as ``call_from``
and ``call_to``: ``call(op, data=None, **options)`` asks that machine's
gate as the person); the Console gives it remote.call bound to each site.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import sqlite3
import threading
import time

IF_THERE = ("skip", "replace", "keep")
STATES = ("queued", "copying", "cancelling", "done", "failed", "cancelled")
ENDED = ("done", "failed", "cancelled")
GET_PIECE = 8 * 1024 * 1024          # the gate's GET_PIECE: the most one get answers
PUT_PIECE = 16 * 1024 * 1024         # the gate's MAX_PIECE: the most one put takes
KEEP_DAYS = 30                       # ended copies kept to be seen this long
BUSY_WAIT = 4                        # seconds between asks of a machine busy moving files
BUSY_TRIES = 45                      # three minutes of it
AGAIN_WAITS = (5, 20, 60)            # a machine that doesn't answer: asked again after these
DOUBLE = (".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst")


class CopyError(Exception):
    """A copy that can't go on, said for the person."""


class Cancelled(Exception):
    """They stopped it."""


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _err(doc) -> str:
    return str((doc or {}).get("error") or "the machine didn't say") if isinstance(doc, dict) else \
        "an answer that can't be read"


# ── the store ───────────────────────────────────────────────────────────────

class Store:
    """The copies asked, and the files and folders of each, in an SQLite
    file of the Console's (copies.db beside its activity log)."""

    def __init__(self, path: str):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS copies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user TEXT NOT NULL, from_site TEXT NOT NULL, from_path TEXT NOT NULL,
                    kind TEXT NOT NULL, to_site TEXT NOT NULL, to_dir TEXT NOT NULL, to_name TEXT,
                    if_there TEXT NOT NULL, state TEXT NOT NULL,
                    files_total INTEGER DEFAULT 0, folders_total INTEGER DEFAULT 0,
                    files_done INTEGER DEFAULT 0, files_skipped INTEGER DEFAULT 0,
                    files_failed INTEGER DEFAULT 0, folders_failed INTEGER DEFAULT 0,
                    bytes_total INTEGER DEFAULT 0,
                    bytes_done INTEGER DEFAULT 0, current TEXT, error TEXT, left_out TEXT,
                    listed INTEGER DEFAULT 0, created TEXT NOT NULL, started TEXT, ended TEXT,
                    tries INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS copies_user ON copies (user, id);
                CREATE INDEX IF NOT EXISTS copies_state ON copies (state);
                CREATE TABLE IF NOT EXISTS copy_items (
                    copy_id INTEGER NOT NULL, n INTEGER NOT NULL, rel TEXT NOT NULL,
                    kind TEXT NOT NULL, size INTEGER DEFAULT 0, state TEXT NOT NULL DEFAULT 'pending',
                    dest TEXT, why TEXT, PRIMARY KEY (copy_id, n));
            """)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode = WAL")
        return db

    def _do(self, sql: str, args=()):
        with self._lock, self._db() as db:
            return db.execute(sql, args)

    def add(self, user: str, from_site: str, from_path: str, kind: str, to_site: str, to_dir: str,
            if_there: str = "skip") -> int:
        if kind not in ("file", "folder"):
            raise ValueError("kind: file or folder")
        if if_there not in IF_THERE:
            raise ValueError(f"if_there: one of {', '.join(IF_THERE)}")
        with self._lock, self._db() as db:
            cur = db.execute(
                "INSERT INTO copies (user, from_site, from_path, kind, to_site, to_dir, if_there, state, created)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                (user, from_site, from_path, kind, to_site, to_dir, if_there, _now()))
            return cur.lastrowid

    def get(self, cid: int) -> dict | None:
        with self._lock, self._db() as db:
            r = db.execute("SELECT * FROM copies WHERE id = ?", (cid,)).fetchone()
        return self._row(r)

    @staticmethod
    def _row(r) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        try:
            d["left_out"] = json.loads(d["left_out"]) if d.get("left_out") else {}
        except ValueError:
            d["left_out"] = {}
        return d

    def set(self, cid: int, **fields) -> None:
        if not fields:
            return
        if "left_out" in fields and not isinstance(fields["left_out"], (str, type(None))):
            fields["left_out"] = json.dumps(fields["left_out"])
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._do(f"UPDATE copies SET {cols} WHERE id = ?", (*fields.values(), cid))

    def mine(self, user: str, limit: int = 30) -> list:
        with self._lock, self._db() as db:
            rows = db.execute("SELECT * FROM copies WHERE user = ? ORDER BY id DESC LIMIT ?",
                              (user, limit)).fetchall()
        return [self._row(r) for r in rows]

    def running(self) -> list:
        with self._lock, self._db() as db:
            rows = db.execute("SELECT * FROM copies WHERE state IN ('copying', 'cancelling') ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def queued(self) -> list:
        with self._lock, self._db() as db:
            rows = db.execute("SELECT * FROM copies WHERE state = 'queued' ORDER BY id").fetchall()
        return [self._row(r) for r in rows]

    def going(self, user: str) -> int:
        """How many of theirs are queued or going (a limit on asking)."""
        with self._lock, self._db() as db:
            return db.execute("SELECT COUNT(*) FROM copies WHERE user = ? AND state IN "
                              "('queued', 'copying', 'cancelling')", (user,)).fetchone()[0]

    def cancel(self, cid: int, user: str) -> str | None:
        """Theirs stopped: one waiting at once, one going when it is between
        pieces. The state it is now in, or None: not theirs, or ended."""
        with self._lock, self._db() as db:
            r = db.execute("SELECT state FROM copies WHERE id = ? AND user = ?", (cid, user)).fetchone()
            if r is None or r["state"] in ENDED:
                return None
            if r["state"] == "queued":
                db.execute("UPDATE copies SET state = 'cancelled', ended = ?, current = NULL WHERE id = ?",
                           (_now(), cid))
                return "cancelled"
            db.execute("UPDATE copies SET state = 'cancelling' WHERE id = ?", (cid,))
            return "cancelling"

    def again(self, cid: int, user: str) -> bool:
        """A copy of theirs that failed or was stopped, or ended with files
        not copied, started again from where it stopped: what was copied
        stays copied, what failed is tried again."""
        with self._lock, self._db() as db:
            r = db.execute("SELECT state FROM copies WHERE id = ? AND user = ?", (cid, user)).fetchone()
            if r is None or r["state"] not in ENDED:
                return False
            failed = db.execute("SELECT COUNT(*) FROM copy_items WHERE copy_id = ? AND state = 'failed'",
                                (cid,)).fetchone()[0]
            if r["state"] == "done" and not failed:
                return False
            db.execute("UPDATE copy_items SET state = 'pending', why = NULL WHERE copy_id = ? AND state = 'failed'",
                       (cid,))
            db.execute("UPDATE copies SET state = 'queued', error = NULL, ended = NULL WHERE id = ?", (cid,))
            return True

    def claim(self, cid: int) -> bool:
        """A copy waiting, taken to run here (by one runner alone)."""
        with self._lock, self._db() as db:
            return db.execute("UPDATE copies SET state = 'copying' WHERE id = ? AND state = 'queued'",
                              (cid,)).rowcount == 1

    def wanted_stop(self, cid: int) -> bool:
        with self._lock, self._db() as db:
            r = db.execute("SELECT state FROM copies WHERE id = ?", (cid,)).fetchone()
        return r is None or r["state"] == "cancelling"

    def after_restart(self) -> int:
        """Copies that were going when the Console stopped: waiting again (they
        go on from the file they were at)."""
        with self._lock, self._db() as db:
            db.execute("UPDATE copies SET state = 'cancelled', ended = ? WHERE state = 'cancelling'", (_now(),))
            return db.execute("UPDATE copies SET state = 'queued' WHERE state = 'copying'").rowcount

    def tidy(self, days: int = KEEP_DAYS) -> int:
        cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * 86400))
        with self._lock, self._db() as db:
            old = [r[0] for r in db.execute("SELECT id FROM copies WHERE state IN ('done', 'failed', 'cancelled') "
                                            "AND COALESCE(ended, created) < ?", (cutoff,))]
            for cid in old:
                db.execute("DELETE FROM copy_items WHERE copy_id = ?", (cid,))
                db.execute("DELETE FROM copies WHERE id = ?", (cid,))
        return len(old)

    # the files and folders of one copy
    def add_items(self, cid: int, items: list) -> None:
        with self._lock, self._db() as db:
            db.execute("DELETE FROM copy_items WHERE copy_id = ?", (cid,))
            db.executemany("INSERT INTO copy_items (copy_id, n, rel, kind, size) VALUES (?, ?, ?, ?, ?)",
                           [(cid, n, it["rel"], it["kind"], int(it.get("size") or 0)) for n, it in enumerate(items)])

    def items(self, cid: int, state: str | None = None) -> list:
        with self._lock, self._db() as db:
            if state:
                rows = db.execute("SELECT * FROM copy_items WHERE copy_id = ? AND state = ? ORDER BY n",
                                  (cid, state)).fetchall()
            else:
                rows = db.execute("SELECT * FROM copy_items WHERE copy_id = ? ORDER BY n", (cid,)).fetchall()
        return [dict(r) for r in rows]

    def item(self, cid: int, n: int, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self._do(f"UPDATE copy_items SET {cols} WHERE copy_id = ? AND n = ?", (*fields.values(), cid, n))

    def problems(self, cid: int, limit: int = 50) -> list:
        """The files and folders not copied, and why (the first ``limit``)."""
        with self._lock, self._db() as db:
            rows = db.execute("SELECT rel, kind, state, why FROM copy_items WHERE copy_id = ? AND state IN "
                              "('failed', 'skipped') ORDER BY n LIMIT ?", (cid, limit)).fetchall()
        return [dict(r) for r in rows]

    def counts(self, cid: int) -> dict:
        with self._lock, self._db() as db:
            rows = db.execute("SELECT kind, state, COUNT(*), COALESCE(SUM(size), 0) FROM copy_items "
                              "WHERE copy_id = ? GROUP BY kind, state", (cid,)).fetchall()
        out = {"files_done": 0, "files_skipped": 0, "files_failed": 0, "folders_failed": 0, "bytes_done": 0}
        for kind, state, n, size in rows:
            if kind != "file":
                if state == "failed":
                    out["folders_failed"] += n
                continue
            if state == "done":
                out["files_done"] += n
                out["bytes_done"] += size
            elif state == "skipped":
                out["files_skipped"] += n
            elif state == "failed":
                out["files_failed"] += n
        return out


# ── names ───────────────────────────────────────────────────────────────────

def other_name(name: str, k: int) -> str:
    """``name`` as the k-th one kept beside it: "data (2).csv", "run (3)",
    "out (2).tar.gz"."""
    low = name.lower()
    for ext in DOUBLE:
        if low.endswith(ext) and len(name) > len(ext):
            return f"{name[:-len(ext)]} ({k}){name[-len(ext):]}"
    stem, ext = posixpath.splitext(name)
    if not stem or stem.startswith(".") and not ext:
        return f"{name} ({k})"
    return f"{stem} ({k}){ext}"


def upload_of(cid: int, n: int, dest: str) -> str:
    """The id the gate keeps a file being written by (its hidden part file)."""
    return hashlib.sha256(f"copy|{cid}|{n}|{dest}".encode("utf-8", errors="surrogateescape")).hexdigest()[:24]


# ── the engine ──────────────────────────────────────────────────────────────

class Copier:
    """One copy, run: ``call_from(op, **options)`` and ``call_to(op, data=None,
    **options)`` ask the gates of the machine it comes from and the one it
    goes to, as the person. ``max_bytes``, ``max_files``: the hub's limits.
    ``sleep``: time.sleep (tests give their own)."""

    def __init__(self, store: Store, cid: int, call_from, call_to, *, max_bytes: int, max_files: int,
                 sleep=time.sleep, piece: int = PUT_PIECE):
        self.store, self.cid = store, cid
        self.call_from, self.call_to = call_from, call_to
        self.max_bytes, self.max_files = max_bytes, max_files
        self.sleep = sleep
        self.piece = max(GET_PIECE, min(int(piece), PUT_PIECE))
        self.c = store.get(cid)
        self._last_said = 0.0

    # asking, with waits
    def _stop_wanted(self) -> None:
        if self.store.wanted_stop(self.cid):
            raise Cancelled()

    def ask(self, which: str, op: str, data=None, **options) -> dict:
        """The machine's answer; a busy one waited for, one that doesn't answer
        asked again (AGAIN_WAITS), then CopyError. A refusal is the answer."""
        call = self.call_from if which == "from" else self.call_to
        site = self.c["from_site"] if which == "from" else self.c["to_site"]
        busy, again = 0, 0
        while True:
            try:
                doc = call(op, data=data, **options) if data is not None else call(op, **options)
            except Exception as e:                  # noqa: BLE001 -- said, and asked again
                doc = {"ok": False, "error": f"the request could not be made ({type(e).__name__})", "where": "hub"}
            if not isinstance(doc, dict):
                doc = {"ok": False, "error": "an answer that can't be read", "where": "site"}
            if doc.get("ok"):
                return doc
            error = _err(doc)
            if doc.get("busy") or error.startswith("busy:"):
                busy += 1
                if busy > BUSY_TRIES:
                    raise CopyError(f"{site} stayed busy moving other files: start it again later")
                self.store.set(self.cid, current=f"waiting: {site} is busy moving other files")
                self._stop_wanted()
                self.sleep(BUSY_WAIT)
                continue
            if doc.get("where") in ("ssh", "site") and not doc.get("maybe"):
                if again >= len(AGAIN_WAITS):
                    raise CopyError(f"{site} doesn't answer ({error[:200]}): start it again later, "
                                    "it goes on from where it stopped")
                self.store.set(self.cid, current=f"waiting: {site} didn't answer")
                self._stop_wanted()
                self.sleep(AGAIN_WAITS[again])
                again += 1
                continue
            return doc

    def must(self, which: str, op: str, data=None, **options) -> dict:
        doc = self.ask(which, op, data=data, **options)
        if not doc.get("ok"):
            raise CopyError(_err(doc))
        return doc

    # the whole copy
    def run(self) -> dict:
        c = self.c
        if c is None or c["state"] in ENDED:
            return c or {}
        if c["state"] == "queued" and not self.store.claim(self.cid):
            return self.store.get(self.cid)              # another runner has it
        self.store.set(self.cid, state="copying", started=c.get("started") or _now(), tries=(c.get("tries") or 0) + 1,
                       error=None)
        try:
            if not c.get("listed"):
                self._list()
            self._copy()
        except Cancelled:
            self.store.set(self.cid, state="cancelled", ended=_now(), current=None, **self.store.counts(self.cid))
        except CopyError as e:
            self.store.set(self.cid, state="failed", ended=_now(), current=None, error=str(e)[:1000],
                           **self.store.counts(self.cid))
        except Exception as e:                      # noqa: BLE001 -- never a copy left "copying"
            self.store.set(self.cid, state="failed", ended=_now(), current=None,
                           error=f"it stopped on the hub ({type(e).__name__}): start it again",
                           **self.store.counts(self.cid))
        else:
            n = self.store.counts(self.cid)
            self.store.set(self.cid, state="done", ended=_now(), current=None, **n)
        return self.store.get(self.cid)

    def _list(self) -> None:
        """What the copy is: its files and folders, their size; checked
        against the limits and the room there; the name it gets there."""
        c = self.c
        src = c["from_path"].rstrip("/") or "/"
        base = posixpath.basename(src)
        if not base:
            raise CopyError("the whole of / can't be copied")
        if c["kind"] == "folder":
            doc = self.must("from", "tree", dir=src, most=self.max_files)
            if doc.get("more"):
                raise CopyError(f"{src} has more than {self.max_files:,} files and folders: copy its folders one "
                                "at a time, or use rsync from one machine to the other")
            entries = doc.get("entries") or []
            items = [{"rel": "", "kind": "folder", "size": 0}] + [
                {"rel": e["path"], "kind": e["kind"], "size": int(e.get("size") or 0)} for e in entries
                if isinstance(e, dict) and e.get("kind") in ("file", "folder") and e.get("path")]
            left = {k: v for k, v in (doc.get("skipped") or {}).items() if v}
        else:
            facts = self.must("from", "get", path=src, length=0)
            items = [{"rel": "", "kind": "file", "size": int(facts.get("size") or 0)}]
            left = {}
        total = sum(i["size"] for i in items if i["kind"] == "file")
        files = sum(1 for i in items if i["kind"] == "file")
        if total > self.max_bytes:
            raise CopyError(f"{_size(total)} is more than a copy from here moves ({_size(self.max_bytes)}): use rsync "
                            "from one machine to the other")
        # the name it gets there, and the room for it
        to_dir = c["to_dir"].rstrip("/") or "/"
        name = base
        st = self.must("to", "put-status", path=posixpath.join(to_dir, name), upload=upload_of(self.cid, -1, name))
        if c["kind"] == "folder" and st.get("there") not in (None, "folder"):
            if c["if_there"] != "keep":
                raise CopyError(f"{posixpath.join(to_dir, name)} is there already, and isn't a folder: copy it with "
                                "\"keep both\", or into another folder")
        if c["kind"] == "folder" and st.get("there") is not None and c["if_there"] == "keep":
            name = self._free_name(to_dir, base)
        free = st.get("free")
        if isinstance(free, int) and free < total:
            raise CopyError(f"not enough room on {c['to_site']} in {to_dir}: {_size(free)} free, the copy is "
                            f"{_size(total)}")
        self.store.add_items(self.cid, items)
        self.store.set(self.cid, listed=1, to_name=name, files_total=files,
                       folders_total=len(items) - files, bytes_total=total, left_out=left)
        self.c = self.store.get(self.cid)

    def _free_name(self, folder: str, name: str) -> str:
        for k in range(2, 100):
            other = other_name(name, k)
            st = self.must("to", "put-status", path=posixpath.join(folder, other), upload=upload_of(self.cid, -1, other))
            if st.get("there") is None:
                return other
        raise CopyError(f"{name} (2) to (99) are all there in {folder}: copy it into another folder")

    def _copy(self) -> None:
        c = self.c
        src_root = c["from_path"].rstrip("/") or "/"
        dst_root = posixpath.join(c["to_dir"].rstrip("/") or "/", c["to_name"] or posixpath.basename(src_root))
        bad = []                                         # folders not made: what is in them isn't copied
        for it in self.store.items(self.cid):
            if it["state"] in ("done", "skipped", "failed"):
                if it["kind"] == "folder" and it["state"] == "failed":
                    bad.append(it["rel"])
                continue
            self._stop_wanted()
            rel = it["rel"]
            src = posixpath.join(src_root, rel) if rel else src_root
            dest = it.get("dest") or (posixpath.join(dst_root, rel) if rel else dst_root)
            if any(rel == b or rel.startswith(b + "/") for b in bad):
                self.store.item(self.cid, it["n"], state="failed", why="its folder couldn't be made there")
                continue
            if it["kind"] == "folder":
                why = self._folder(dest)
                if why:
                    bad.append(rel)
                    self.store.item(self.cid, it["n"], state="failed", why=why[:300])
                else:
                    self.store.item(self.cid, it["n"], state="done", dest=dest)
                continue
            self.store.set(self.cid, current=rel or posixpath.basename(src_root), **self.store.counts(self.cid))
            self._file(it, src, dest)

    def _folder(self, dest: str) -> str | None:
        doc = self.ask("to", "mkdir", dir=dest)
        if doc.get("ok") or (doc.get("exists") and doc.get("folder")):
            return None
        if doc.get("exists"):
            return f"{dest} is there already, and isn't a folder"
        return _err(doc)

    def _file(self, it: dict, src: str, dest: str) -> None:
        n = it["n"]
        replace = self.c["if_there"] == "replace"
        if it["state"] == "copying" and it.get("dest"):
            # its first piece was taken there (the name was free then): what is there now is this copy's own
            replace = True
        self.store.item(self.cid, n, dest=dest)
        while True:
            got = self._send(n, src, dest, replace)
            if got == "sent":
                self.store.item(self.cid, n, state="done", why=None)
                return
            if isinstance(got, str) and got.startswith("failed:"):
                self.store.item(self.cid, n, state="failed", why=got[7:].strip()[:300])
                return
            # got == "there": a file is there by that name
            how = self.c["if_there"]
            if how == "skip":
                self.store.item(self.cid, n, state="skipped", why="there already: left as it is")
                return
            if how == "replace":
                self.store.item(self.cid, n, state="failed", why="there already, and it couldn't be replaced")
                return
            folder, name = posixpath.split(dest)
            dest = posixpath.join(folder, self._free_name(folder, name))
            self.store.item(self.cid, n, dest=dest)

    def _send(self, n: int, src: str, dest: str, replace: bool):
        """One file, from its start: "sent", "there" (a file is there by that
        name, not to be replaced), or "failed: why"."""
        up = upload_of(self.cid, n, dest)
        first = self.ask("from", "get", path=src, offset=0, length=GET_PIECE)
        if not first.get("ok"):
            return f"failed: {_err(first)}"
        size, version = int(first.get("size") or 0), first.get("version")
        whole, at, buf = hashlib.sha256(), 0, b""
        pending = [first]
        sent_any = False
        try:
            while True:
                while pending:
                    doc = pending.pop()
                    if doc.get("version") != version:
                        return "failed: it changed while it was being copied: copy it again"
                    data = base64.b64decode(doc.get("data") or "", validate=True)
                    if hashlib.sha256(data).hexdigest() != doc.get("sha256") or len(data) != doc.get("length"):
                        return "failed: a piece didn't arrive whole"
                    if not data and at + len(buf) < size:
                        return "failed: it is shorter than it was: copy it again"
                    whole.update(data)
                    buf += data
                read_to = at + len(buf)
                if len(buf) < self.piece and read_to < size:
                    self._stop_wanted()
                    pending.append(self.ask("from", "get", path=src, offset=read_to,
                                            length=min(GET_PIECE, size - read_to)))
                    if not pending[-1].get("ok"):
                        return f"failed: {_err(pending[-1])}"
                    continue
                last = read_to >= size
                options = {"path": dest, "upload": up, "offset": at, "total": size}
                if last:
                    options["sha256"] = whole.hexdigest()
                if replace:
                    options["replace"] = True
                doc = self.ask("to", "put", data=buf, **options)
                if not doc.get("ok"):
                    if doc.get("exists") and at == 0 and not replace:
                        return "there"
                    if doc.get("exists"):
                        self.ask("to", "put-cancel", path=dest, upload=up)
                        return "failed: a file came there by that name meanwhile"
                    return f"failed: {_err(doc)}"
                if not sent_any:
                    self.store.item(self.cid, n, state="copying")
                sent_any = True
                at += len(buf)
                buf = b""
                self._progress(at, size)
                if last:
                    return "sent"
                self._stop_wanted()
        except Cancelled:
            if sent_any:
                self.ask("to", "put-cancel", path=dest, upload=up)
            raise

    def _progress(self, at: int, size: int) -> None:
        if time.monotonic() - self._last_said < 2 and at < size:
            return
        self._last_said = time.monotonic()
        n = self.store.counts(self.cid)
        n["bytes_done"] += at if at < size else 0
        self.store.set(self.cid, **n)


def _size(n: int) -> str:
    for unit, k in (("TB", 1000 ** 4), ("GB", 1000 ** 3), ("MB", 1000 ** 2), ("KB", 1000)):
        if n >= k:
            return f"{n / k:.1f} {unit}"
    return f"{n} bytes"


# ── the runner, in the Console ──────────────────────────────────────────────

class Runner:
    """Runs the copies waiting, ``at_once`` at a time and one a person, each
    in a thread of its own; ``calls(copy)`` gives (call_from, call_to) for
    it, or raises CopyError (a machine no longer there). ``limits()`` gives
    (max_bytes, max_files). ``ended(copy)``: told when one ends (the audit
    log). Started once with the Console (start()); wake() when one is asked."""

    def __init__(self, store: Store, calls, limits, at_once: int = 2, ended=None, sleep=time.sleep):
        self.store, self.calls, self.limits = store, calls, limits
        self.at_once = max(1, int(at_once))
        self.ended, self.sleep = ended, sleep
        self._wake = threading.Event()
        self._going: dict = {}                       # id -> (user, thread)
        self._lock = threading.Lock()
        self._thread = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.store.after_restart()
        self.store.tidy()
        self._thread = threading.Thread(target=self._loop, name="nomad-copies", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while True:
            try:
                self.step()
            except Exception:                        # noqa: BLE001 -- the runner never stops
                pass
            self._wake.wait(10)
            self._wake.clear()

    def step(self) -> list:
        """Start what may start now; the ids started."""
        started = []
        with self._lock:
            self._going = {k: v for k, v in self._going.items() if v[1].is_alive()}
            busy_people = {u for u, _ in self._going.values()}
            for c in self.store.queued():
                if len(self._going) >= self.at_once:
                    break
                if c["id"] in self._going or c["user"] in busy_people:
                    continue
                t = threading.Thread(target=self._one, args=(c["id"],), name=f"nomad-copy-{c['id']}", daemon=True)
                self._going[c["id"]] = (c["user"], t)
                busy_people.add(c["user"])
                t.start()
                started.append(c["id"])
        return started

    def _one(self, cid: int) -> None:
        c = self.store.get(cid)
        try:
            call_from, call_to = self.calls(c)
            max_bytes, max_files = self.limits()
            Copier(self.store, cid, call_from, call_to, max_bytes=max_bytes, max_files=max_files,
                   sleep=self.sleep).run()
        except CopyError as e:
            self.store.set(cid, state="failed", ended=_now(), error=str(e)[:1000], current=None)
        except Exception as e:                       # noqa: BLE001
            self.store.set(cid, state="failed", ended=_now(), current=None,
                           error=f"it couldn't start on the hub ({type(e).__name__})")
        finally:
            if self.ended:
                try:
                    self.ended(self.store.get(cid))
                except Exception:                    # noqa: BLE001
                    pass
            self.wake()

    def wait(self, timeout: float = 30) -> None:
        """Until no copy is going (tests)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                alive = [t for _, t in self._going.values() if t.is_alive()]
            if not alive and not self.store.queued():
                return
            for t in alive:
                t.join(0.2)
            if not alive:
                self.step()
                time.sleep(0.05)


def store_path(data_dir: str | None = None) -> str:
    """copies.db in the Console's data folder (~/.nomad-console)."""
    d = data_dir or os.path.expanduser("~/.nomad-console")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return os.path.join(d, "copies.db")

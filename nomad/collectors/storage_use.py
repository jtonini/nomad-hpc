# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Who uses each storage server's space, and on what (1.16).

Reads the files ``nomad-storage.sh`` writes on each storage server from
root's cron (``nomad storage script``; nomad itself never runs as root
there): ``usage.txt``, each owner's space by the filesystem's own accounting,
and ``scan.txt``, the folders inside each top folder (``/home/USER/FOLDER``)
with their size, files and age. Over ssh with an ordinary account in the
group the script was told (-g), or from this machine when the files are
here.

    [collectors.storage_use]
    enabled = true
    # every_minutes = 60          # how often each server's files are read
    # keep_days = 30              # every reading kept this long; one a day after
    # timeout = 30                # seconds a server has to answer

    [[collectors.storage_use.sources]]
    server = "fs1"                # the name nomad shows
    host = "fs1"                  # ssh to it; leave out for files on this machine
    dir = "/var/lib/nomad-storage"
    # usage_dir = "..."           # usage.txt's own folder, when it isn't dir
    # scan_dir = "..."            # (usage run as root, the scan as another account)
    # ssh_user = "..."            # default: this account
    # copy_of = "fs0"             # it holds copies (replicas, backups) of fs0's
    #                             # files: shown, never added to anyone's totals
    # same_accounts = true        # it and this machine share their accounts (one
    #                             # directory): owners it gives only as a number
    #                             # are named as this machine knows them

A file is stored once (by when its run started); one a run didn't finish
(no #finished line) is left for the next read, and one that can't be stored
is noted without holding up the others. Tables: storage_reports (each
file read), storage_paths (each path's size), storage_owners (each owner's
space, quotas), storage_datasets (ZFS filesystems, quotas), storage_tops
(each scan of a top folder, kept) and storage_folders (the latest scan of
each top folder's folders). Owners in the scan are uids, named here as this
machine knows them.
"""
from __future__ import annotations

import grp
import logging
import pwd
import re
import shlex
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from nomad.collectors.base import BaseCollector, CollectionError, find_tool
from nomad.storage import ReportError, parse

logger = logging.getLogger(__name__)

HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
MARK = "__NOMAD_STORAGE_FILE__"
UNREADABLE = "__NOMAD_STORAGE_UNREADABLE__"

SCHEMA = """
CREATE TABLE IF NOT EXISTS storage_reports (
    server TEXT NOT NULL,
    mode TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    host TEXT,
    os TEXT,
    version INTEGER,
    read_at TEXT,
    paths INTEGER,
    owners INTEGER,
    tops INTEGER,
    not_scanned INTEGER,
    run_by TEXT,
    run_uid INTEGER,
    copy_of TEXT,
    odd_names INTEGER,
    unreadable TEXT
);
CREATE INDEX IF NOT EXISTS idx_storage_reports ON storage_reports(server, mode, started);
CREATE TABLE IF NOT EXISTS storage_reads (
    server TEXT NOT NULL,
    read_at TEXT NOT NULL,
    note TEXT,
    found INTEGER
);
CREATE TABLE IF NOT EXISTS storage_paths (
    timestamp TEXT NOT NULL,
    server TEXT NOT NULL,
    path TEXT NOT NULL,
    fs TEXT,
    dataset TEXT,
    mount TEXT,
    total_bytes INTEGER,
    used_bytes INTEGER,
    available_bytes INTEGER,
    accounting INTEGER,
    note TEXT,
    covers TEXT,
    errors TEXT
);
CREATE INDEX IF NOT EXISTS idx_storage_paths ON storage_paths(server, path, timestamp);
CREATE INDEX IF NOT EXISTS idx_storage_paths_time ON storage_paths(timestamp);
CREATE TABLE IF NOT EXISTS storage_owners (
    timestamp TEXT NOT NULL,
    server TEXT NOT NULL,
    path TEXT NOT NULL,
    dataset TEXT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    used_bytes INTEGER,
    soft_bytes INTEGER,
    hard_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_storage_owners ON storage_owners(server, path, kind, name, timestamp);
CREATE INDEX IF NOT EXISTS idx_storage_owners_time ON storage_owners(timestamp);
CREATE INDEX IF NOT EXISTS idx_storage_owners_read ON storage_owners(server, path, kind, timestamp);
CREATE TABLE IF NOT EXISTS storage_datasets (
    timestamp TEXT NOT NULL,
    server TEXT NOT NULL,
    path TEXT,
    dataset TEXT NOT NULL,
    mountpoint TEXT,
    used_bytes INTEGER,
    available_bytes INTEGER,
    quota_bytes INTEGER,
    refquota_bytes INTEGER,
    referenced_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_storage_datasets ON storage_datasets(server, path, timestamp);
CREATE TABLE IF NOT EXISTS storage_tops (
    scanned_at TEXT NOT NULL,
    server TEXT NOT NULL,
    path TEXT NOT NULL,
    top TEXT NOT NULL,
    top_uid INTEGER,
    top_owner TEXT,
    bytes INTEGER,
    files INTEGER,
    newest TEXT,
    older1_bytes INTEGER,
    older2_bytes INTEGER,
    age1_days INTEGER,
    age2_days INTEGER,
    seconds INTEGER,
    complete INTEGER,
    denied INTEGER
);
CREATE INDEX IF NOT EXISTS idx_storage_tops ON storage_tops(server, path, top, scanned_at);
CREATE TABLE IF NOT EXISTS storage_folders (
    scanned_at TEXT NOT NULL,
    server TEXT NOT NULL,
    path TEXT NOT NULL,
    top TEXT NOT NULL,
    top_uid INTEGER,
    top_owner TEXT,
    folder TEXT NOT NULL,
    owner_uid INTEGER,
    owner TEXT,
    bytes INTEGER,
    files INTEGER,
    newest TEXT,
    older1_bytes INTEGER,
    older2_bytes INTEGER,
    age1_days INTEGER,
    age2_days INTEGER,
    complete INTEGER
);
CREATE INDEX IF NOT EXISTS idx_storage_folders ON storage_folders(server, path, top);
"""


def _iso(epoch: int | None) -> str | None:
    if not epoch:
        return None
    try:
        return datetime.fromtimestamp(int(epoch)).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def sources_from(config: dict) -> tuple[list[dict], list[str]]:
    """The sources to read, and what was left out and why."""
    out, problems, names = [], [], set()
    for i, s in enumerate(config.get("sources") or []):
        if not isinstance(s, dict):
            problems.append(f"sources[{i}]: a table (server, host, dir)")
            continue
        server = str(s.get("server") or s.get("host") or "").strip()
        host = s.get("host")
        d = str(s.get("dir") or "/var/lib/nomad-storage")
        if not server or not HOST_RE.match(server):
            problems.append(f"sources[{i}]: server is a name like fs1")
            continue
        if host is not None and not HOST_RE.match(str(host)):
            problems.append(f"{server}: host {host!r} isn't a host name")
            continue
        dirs = {m: str(s.get(f"{m}_dir") or d) for m in ("usage", "scan")}
        bad = [x for x in (d, *dirs.values()) if not x.startswith("/") or any(c in x for c in "\n\0'\"$`\\!")]
        if bad:
            problems.append(f"{server}: dir {bad[0]!r} isn't an absolute path without quotes")
            continue
        if server in names:
            problems.append(f"{server}: listed twice")
            continue
        copy_of = s.get("copy_of")
        if copy_of is not None and not HOST_RE.match(str(copy_of)):
            problems.append(f"{server}: copy_of {copy_of!r} isn't a server name")
            continue
        same = s.get("same_accounts", False)
        if not isinstance(same, bool):
            problems.append(f"{server}: same_accounts is true or false")
            continue
        names.add(server)
        user = s.get("ssh_user")
        out.append({"server": server, "host": str(host) if host else None, "dir": d.rstrip("/") or "/",
                    "dirs": {m: x.rstrip("/") or "/" for m, x in dirs.items()},
                    "ssh_user": str(user) if user else None,
                    "copy_of": str(copy_of) if copy_of else None, "same_accounts": same})
    return out, problems


def split_files(output: str) -> dict[str, str]:
    """{usage|scan: text} from the fetch's output."""
    files, cur, buf = {}, None, []
    for line in output.split("\n"):
        line += "\n"
        if line.startswith(MARK + " "):
            if cur:
                files[cur] = "".join(buf)
            cur, buf = line.split(" ", 1)[1].strip(), []
        elif cur is not None:
            buf.append(line)
    if cur:
        files[cur] = "".join(buf)
    return files


def fetch_command(d: str | dict) -> str:
    """One command that prints both files, each after a mark line; a file
    that is there but can't be read (this account not in the script's -g
    group) says so, rather than looking like one not written yet."""
    dirs = d if isinstance(d, dict) else {"usage": d, "scan": d}
    parts = []
    for m in ("usage", "scan"):
        f = shlex.quote(dirs[m] + "/" + m + ".txt")
        parts.append(f"echo '{MARK} {m}'; if [ -e {f} ]; then cat {f} 2>/dev/null || echo '{UNREADABLE}'; fi")
    return "sh -c " + shlex.quote("; ".join(parts))


class StorageUseCollector(BaseCollector):
    name = "storage_use"
    description = "Who uses each storage server's space (files from nomad-storage.sh)"
    default_interval = 3600

    def __init__(self, config: dict[str, Any], db_path):
        super().__init__(config, db_path)
        self.sources, self.problems = sources_from(self.config)
        self.every = timedelta(minutes=float(self.config.get("every_minutes", 60) or 60))
        self.keep = timedelta(days=float(self.config.get("keep_days", 30) or 30))
        self.timeout = max(5, int(self.config.get("timeout", 30) or 30))
        self._max_retries = 1
        self._names: dict[int, str | None] = {}

    # -- reading ------------------------------------------------------------------

    def _last(self, conn) -> dict[tuple[str, str], str]:
        return {(s, m): t for s, m, t in conn.execute(
            "SELECT server, mode, MAX(started) FROM storage_reports GROUP BY server, mode")}

    @staticmethod
    def _working_state(conn) -> None:
        """storage_reads is this collector's own (not synced): columns added
        since 1.16.0 are added here."""
        conn.executescript(SCHEMA)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(storage_reads)")}
        if "found" not in cols:
            conn.execute("ALTER TABLE storage_reads ADD COLUMN found INTEGER")

    def _read_recently(self, conn, server: str) -> bool:
        # A read that found no file at all (the script not run there yet)
        # doesn't hold back the next one.
        row = conn.execute("SELECT MAX(read_at) FROM storage_reads WHERE server = ? "
                           "AND (found IS NULL OR found > 0)", (server,)).fetchone()
        if not row or not row[0]:
            return False
        try:
            t = datetime.fromisoformat(row[0])
        except ValueError:
            return False
        return timedelta(0) <= datetime.now() - t < self.every - timedelta(seconds=30)

    def _fetch(self, src: dict) -> dict[str, str]:
        if not src["host"]:
            out = {}
            for mode in ("usage", "scan"):
                p = Path(src["dirs"][mode]) / f"{mode}.txt"
                try:
                    out[mode] = p.read_text(encoding="utf-8", errors="replace")
                except FileNotFoundError:
                    continue
                except PermissionError:
                    out[mode] = UNREADABLE + "\n"
                except OSError as e:
                    raise CollectionError(f"{p}: {e.strerror or e}") from None
            return out
        ssh = find_tool("ssh")
        if not ssh:
            raise CollectionError("ssh not found")
        argv = [ssh, "-n", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.timeout}"]
        if src["ssh_user"]:
            argv += ["-l", src["ssh_user"]]
        argv += ["--", src["host"], fetch_command(src["dirs"])]
        try:
            r = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                               timeout=self.timeout + 60)
        except subprocess.TimeoutExpired:
            raise CollectionError(f"{src['host']} didn't answer in time") from None
        if r.returncode != 0 and MARK not in r.stdout:
            why = (r.stderr.strip().splitlines() or ["no answer"])[-1][:160]
            raise CollectionError(f"{src['host']}: {why}")
        return split_files(r.stdout)

    def _name_of(self, kind: str, name: str) -> str:
        """An owner the storage server gave only as a number, on a source
        with same_accounts: named as this machine knows it (it often has the
        directory the server lacks), else left a number (an account that no
        longer exists anywhere here)."""
        if not name.isdigit():
            return name
        if kind == "user":
            return self._owner(int(name)) or name
        if kind == "group":
            try:
                return grp.getgrgid(int(name)).gr_name
            except (KeyError, OverflowError, ValueError):
                return name
        return name

    def _owner(self, uid: int | None) -> str | None:
        if uid is None:
            return None
        if uid not in self._names:
            try:
                self._names[uid] = pwd.getpwuid(uid).pw_name
            except (KeyError, OverflowError, ValueError):
                self._names[uid] = None
        return self._names[uid]

    def collect(self) -> list[dict[str, Any]]:
        if not self.sources:
            self.note = "; ".join(self.problems) or "no sources ([collectors.storage_use] sources)"
            return []
        records, notes, failed = [], list(self.problems), 0
        with self.get_db_connection() as conn:
            self._working_state(conn)
            last = self._last(conn)
            due = [s for s in self.sources if not self._read_recently(conn, s["server"])]
        if not due:
            self.note = f"read within the last {self.every.total_seconds() / 60:.0f} minutes"
            return []
        now = datetime.now().isoformat(timespec="seconds")
        for src in due:
            server = src["server"]
            try:
                files = self._fetch(src)
            except CollectionError as e:
                failed += 1
                notes.append(f"{server}: {e}")
                records.append({"type": "storage_read", "server": server, "read_at": now, "note": str(e)})
                continue
            said = []
            found = 0
            for mode in ("usage", "scan"):
                text = files.get(mode)
                if not text or not text.strip():
                    said.append(f"no {mode}.txt yet")
                    continue
                found += 1
                if text.strip() == UNREADABLE:
                    said.append(f"{mode}.txt is there but this account can't read it "
                                "(is it in the group given to the script's -g?)")
                    continue
                try:
                    rep = parse(text)
                except ReportError as e:
                    said.append(f"{mode}.txt: {e}")
                    continue
                if rep.mode != mode:
                    said.append(f"{mode}.txt is a {rep.mode} file")
                    continue
                started = _iso(rep.started)
                if started is None or (last.get((server, mode)) and started <= last[(server, mode)]):
                    continue                     # stored already
                records.append({"type": "storage_report", "server": server, "read_at": now,
                                "report": rep, "copy_of": src.get("copy_of"),
                                "same_accounts": src.get("same_accounts", False)})
                said.append(f"{mode} of {started[:16].replace('T', ' ')}")
            records.append({"type": "storage_read", "server": server, "read_at": now,
                            "note": "; ".join(said) or None, "found": found})
            if said:
                notes.append(f"{server}: " + ", ".join(said))
        if failed and failed == len(due):
            raise CollectionError("; ".join(notes))
        self.note = "; ".join(notes) or None
        return records

    # -- storing ------------------------------------------------------------------

    def store(self, data: list[dict[str, Any]]) -> None:
        if not data:
            return
        self._names = {}             # a lookup that failed once (a directory hiccup) is tried again
        with self.get_db_connection() as conn:
            self._working_state(conn)
            failed: dict[str, list[str]] = {}
            for d in data:
                if d.get("type") != "storage_report":
                    continue
                # Each file on its own: one that can't be stored (figures no
                # filesystem has, a database error) is noted and skipped, and
                # the others go in.
                conn.execute("SAVEPOINT storage_report")
                try:
                    self._store_report(conn, d["server"], d["read_at"], d["report"], d.get("copy_of"),
                                       d.get("same_accounts", False))
                except (sqlite3.Error, ValueError, OverflowError, TypeError) as e:
                    conn.execute("ROLLBACK TO storage_report")
                    failed.setdefault(d["server"], []).append(f"{d['report'].mode}.txt not stored: {e}")
                    logger.warning("storage_use: %s %s.txt not stored: %s", d["server"], d["report"].mode, e)
                conn.execute("RELEASE storage_report")
            for d in data:
                if d.get("type") == "storage_read":
                    note = "; ".join(x for x in [d.get("note")] + failed.get(d["server"], []) if x) or None
                    conn.execute("INSERT INTO storage_reads (server, read_at, note, found) VALUES (?, ?, ?, ?)",
                                 (d["server"], d["read_at"], note, d.get("found")))
            conn.execute("DELETE FROM storage_reads WHERE read_at < ?",
                         ((datetime.now() - timedelta(days=7)).isoformat(),))
            self._thin(conn)
            conn.commit()

    def _store_report(self, conn, server: str, read_at: str, rep, copy_of: str | None = None,
                      same_accounts: bool = False) -> None:
        started = _iso(rep.started)
        if started is None:
            raise ValueError("no start time")
        conn.execute(
            "INSERT INTO storage_reports (server, mode, started, finished, host, os, version, read_at, "
            "paths, owners, tops, not_scanned, run_by, run_uid, copy_of, odd_names, unreadable) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (server, rep.mode, started, _iso(rep.finished), rep.host, rep.os, rep.version, read_at,
             len(rep.paths), len(rep.owners), len(rep.tops), rep.not_scanned, rep.run_by, rep.run_uid,
             copy_of, rep.odd_names, "\n".join(rep.unreadable) or None))
        if rep.mode == "usage":
            conn.executemany(
                "INSERT INTO storage_paths (timestamp, server, path, fs, dataset, mount, total_bytes, "
                "used_bytes, available_bytes, accounting, note, covers, errors) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(started, server, p.path, p.fs, p.dataset, p.mount, p.total_bytes, p.used_bytes,
                  p.available_bytes, int(p.accounting), p.note, p.covers,
                  "; ".join(p.errors) or None) for p in rep.paths])
            conn.executemany(
                "INSERT INTO storage_owners (timestamp, server, path, dataset, kind, name, used_bytes, "
                "soft_bytes, hard_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(started, server, o.path, o.dataset, o.kind,
                  self._name_of(o.kind, o.name) if same_accounts else o.name, o.used_bytes,
                  o.soft_bytes, o.hard_bytes) for o in rep.owners])
            conn.executemany(
                "INSERT INTO storage_datasets (timestamp, server, path, dataset, mountpoint, used_bytes, "
                "available_bytes, quota_bytes, refquota_bytes, referenced_bytes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(started, server, x.path, x.name, x.mountpoint, x.used_bytes, x.available_bytes,
                  x.quota_bytes, x.refquota_bytes, x.referenced_bytes) for x in rep.datasets])
            return
        # Top folders gone from a path (a departed account's home, removed):
        # their figures go too. Not when the path listed nothing at all (not
        # mounted that night, say) or couldn't be listed: that is no news of
        # any folder.
        for path, names in rep.present.items():
            if not names or path in rep.unreadable:
                continue
            keep = set(names)
            for table in ("storage_folders", "storage_tops"):
                gone = [t for (t,) in conn.execute(
                    f"SELECT DISTINCT top FROM {table} WHERE server = ? AND path = ?", (server, path))
                    if t not in keep]
                conn.executemany(f"DELETE FROM {table} WHERE server = ? AND path = ? AND top = ?",
                                 [(server, path, t) for t in gone])
        a1, a2 = rep.ages
        for t in rep.tops:
            when = _iso(t.scanned_at) or started
            owner = self._owner(t.owner_uid)
            fs = t.folders
            newest = max((f.newest or 0 for f in fs), default=0)
            conn.execute(
                "INSERT INTO storage_tops (scanned_at, server, path, top, top_uid, top_owner, bytes, files, "
                "newest, older1_bytes, older2_bytes, age1_days, age2_days, seconds, complete, denied) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (when, server, t.path, t.name, t.owner_uid, owner, sum(f.bytes for f in fs),
                 sum(f.files for f in fs), _iso(newest), sum(f.older1_bytes for f in fs),
                 sum(f.older2_bytes for f in fs), a1, a2, t.seconds, int(t.complete), t.denied))
            # The latest scan of a top folder stands for it: an incomplete one
            # replaces a complete one only when nothing better is there.
            if not t.complete:
                row = conn.execute("SELECT MIN(complete) FROM storage_folders WHERE server = ? AND path = ? "
                                   "AND top = ?", (server, t.path, t.name)).fetchone()
                if row and row[0] == 1:
                    continue
            conn.execute("DELETE FROM storage_folders WHERE server = ? AND path = ? AND top = ?",
                         (server, t.path, t.name))
            conn.executemany(
                "INSERT INTO storage_folders (scanned_at, server, path, top, top_uid, top_owner, folder, "
                "owner_uid, owner, bytes, files, newest, older1_bytes, older2_bytes, age1_days, age2_days, "
                "complete) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(when, server, t.path, t.name, t.owner_uid, owner, f.name, f.owner_uid,
                  self._owner(f.owner_uid), f.bytes, f.files, _iso(f.newest), f.older1_bytes,
                  f.older2_bytes, a1, a2, int(t.complete)) for f in fs])

    def _thin(self, conn) -> None:
        """Past keep_days, one reading a day: each path's first whole reading
        of the day (every owner and dataset in it, so a comparison with that
        day sees everyone), for the two weeks just past the line (those
        before were thinned already, if the collector hasn't been off for
        longer)."""
        cut = datetime.now() - self.keep
        lo = (cut - timedelta(days=14)).isoformat()
        hi = cut.isoformat()
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS storage_keep (server TEXT, path TEXT, ts TEXT)")
        conn.execute("CREATE INDEX IF NOT EXISTS temp.storage_keep_i ON storage_keep(server, path, ts)")
        for table in ("storage_owners", "storage_paths", "storage_datasets"):
            conn.execute("DELETE FROM storage_keep")
            conn.execute(
                f"INSERT INTO storage_keep SELECT server, path, MIN(timestamp) FROM {table} "
                f"WHERE timestamp >= ? AND timestamp < ? GROUP BY server, path, substr(timestamp, 1, 10)",
                (lo, hi))
            conn.execute(
                f"DELETE FROM {table} WHERE timestamp >= ? AND timestamp < ? AND NOT EXISTS ("
                f"SELECT 1 FROM storage_keep k WHERE k.server = {table}.server AND k.path IS {table}.path "
                f"AND k.ts = {table}.timestamp)", (lo, hi))

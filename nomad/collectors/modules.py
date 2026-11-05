# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The environment modules a cluster has (1.26.0): what ``module -t avail``
lists there, read once a day, so a job script nomad makes offers the
versions the cluster really has (nomad.submit.templates).

It runs ``module -t avail`` in a login shell of the account collecting
(Lmod, or Environment Modules: ``module`` is a shell function a login sets
up), after ``module use`` for each of ``module_paths`` -- a folder of modules
a login doesn't add on its own (spydur's /usr/local/sw/modulefiles)::

    [collectors.modules]
    enabled = true
    module_paths = ["/usr/local/sw/modulefiles"]
    # min_hours = 20            # read at most this often

Tables: ``modules_available`` (each module, whether the cluster marks it its
default, when nomad first saw it), rewritten only when the list changes;
``modules_status`` (when it was last read, how many).
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import sqlite3
import subprocess
from datetime import datetime, timedelta
from typing import Any

from .base import BaseCollector, CollectionError

logger = logging.getLogger(__name__)

MIN_HOURS = 20
_PATH = re.compile(r"^/[A-Za-z0-9._/+-]{1,255}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}$")
# What Lmod and Environment Modules put after a name: (default), (D), <L>, (L), <aL>,
# (default:stable), (g09) (a symbolic version), <nearly-forbidden> ...
_MARKS = re.compile(r"\s*(\([^()]*\)|<[^<>]*>)\s*$")
# Said before and after module's own output, so what a login prints isn't taken for modules.
_START, _END = "__NOMAD_MODULES_START__", "__NOMAD_MODULES_END__"


def parse(text: str) -> list[tuple[str, bool]]:
    """``module -t avail``'s lines: [(name, marked as the default)]. Folder
    headers ("/etc/modulefiles:") and family names ("gaussian/") are not modules."""
    return _scan(text)[0]


def _scan(text: str) -> tuple[list[tuple[str, bool]], set]:
    """([(name, marked as the default)], the folder headers that gave a module
    -- a name another folder gave first too)."""
    out, seen, folder, folders = [], set(), None, set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.endswith(":"):
            folder = line[:-1].rstrip("/") or "/"
            continue
        if line.endswith("/"):
            continue
        default = False
        while True:
            m = _MARKS.search(line)
            if not m:
                break
            tags = re.split(r"[:,\s]+", m.group(1)[1:-1].lower())
            default = default or "default" in tags or "d" in tags
            line = line[:m.start()].rstrip()
        if line.endswith("/") or not _NAME.fullmatch(line):
            continue
        if folder:
            folders.add(folder)
        if line in seen:
            continue
        seen.add(line)
        out.append((line, default))
    return out, folders


class ModulesCollector(BaseCollector):
    """The environment modules this cluster lists (module -t avail), daily."""

    name = "modules"
    description = "The environment modules a cluster has (module -t avail), once a day"
    default_interval = 86400

    def __init__(self, config: dict[str, Any], db_path):
        super().__init__(config, db_path)
        paths = self.config.get("module_paths") or []
        self.paths = [p for p in (paths if isinstance(paths, list) else [paths]) if isinstance(p, str)
                      and _PATH.fullmatch(p)]
        self.min_hours = float(self.config.get("min_hours", MIN_HOURS))
        self.timeout = int(self.config.get("timeout") or 120)

    def _recent(self) -> bool:
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            try:
                row = conn.execute("SELECT MAX(read_at) FROM modules_status").fetchone()
            finally:
                conn.close()
            last = datetime.fromisoformat(row[0]) if row and row[0] else None
        except (sqlite3.Error, ValueError):
            return False
        return bool(last and datetime.now() - last < timedelta(hours=self.min_hours))

    def collect(self) -> list[dict[str, Any]]:
        if self.min_hours > 0 and self._recent():
            self.note = f"read less than {self.min_hours:g} h ago"
            return []
        # a folder to add that isn't there now (NFS away) or that module won't use: said, and the
        # list kept as it was -- never read as "the cluster has none of those any more"
        uses = "".join(f"[ -d {shlex.quote(p)} ] || {{ echo __NOMAD_NO_FOLDER__ {shlex.quote(p)}; exit 0; }}; "
                       f"module use {shlex.quote(p)} >/dev/null 2>&1 || "
                       f"{{ echo __NOMAD_USE_FAILED__ {shlex.quote(p)}; exit 0; }}; " for p in self.paths)
        script = ("type module >/dev/null 2>&1 || { echo __NOMAD_NO_MODULES__; exit 0; }; "
                  f"{uses}echo {_START}; module -t avail 2>&1; rc=$?; echo {_END} $rc")
        try:
            r = subprocess.run(["bash", "-lc", script], capture_output=True, text=True, errors="replace",
                               timeout=self.timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise CollectionError(f"module -t avail: no answer in {self.timeout} s") from None
        except OSError as e:
            raise CollectionError(f"bash: {e}") from None
        said = r.stdout.splitlines()
        for word, why in (("__NOMAD_NO_MODULES__", "no module command in a login shell here"),):
            if any(x.strip() == word for x in said):
                self.note = why
                return []
        for x in said:
            for word, why in (("__NOMAD_NO_FOLDER__ ", "isn't there now"), ("__NOMAD_USE_FAILED__ ", "module use failed")):
                if x.startswith(word):
                    raise CollectionError(f"{x[len(word):].strip()}: {why}; the modules read before are kept")
        if _START not in said:
            raise CollectionError("module -t avail didn't run" + (f": {r.stdout.strip()[-200:]}"
                                                                  if r.stdout.strip() else ""))
        body = said[said.index(_START) + 1:]
        end = next((i for i, x in enumerate(body) if x.startswith(_END)), None)
        if end is None:
            raise CollectionError("module -t avail was cut short; the modules read before are kept")
        rc = body[end][len(_END):].strip()
        if rc != "0":
            raise CollectionError(f"module -t avail: exit {rc}; the modules read before are kept")
        entries, folders = _scan("\n".join(body[:end]))
        if not entries:
            raise CollectionError("module -t avail listed nothing")
        # each folder added must have given modules (a folder that came back empty is one that failed)
        def same(path):
            return {path.rstrip("/") or "/", os.path.realpath(path)}
        under = set().union(*(same(f) for f in folders))
        missing = [p for p in self.paths if not same(p) & under]
        if missing and under:
            raise CollectionError(f"no modules from {', '.join(missing)}; the modules read before are kept")
        return [{"module": n, "is_default": d} for n, d in entries]

    def count_records(self, data) -> int:
        return len(data)

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS modules_available (
            module TEXT PRIMARY KEY, is_default INTEGER, first_seen TEXT);
        CREATE TABLE IF NOT EXISTS modules_status (
            id INTEGER PRIMARY KEY CHECK (id = 1), read_at TEXT, count INTEGER, paths TEXT);
    """

    def store(self, data: list[dict[str, Any]]) -> None:
        now = datetime.now().isoformat(timespec="seconds")
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        try:
            conn.executescript(self.SCHEMA)
            have = dict(conn.execute("SELECT module, is_default FROM modules_available").fetchall())
            now_set = {d["module"]: 1 if d["is_default"] else 0 for d in data}
            for name in set(have) - set(now_set):
                conn.execute("DELETE FROM modules_available WHERE module = ?", (name,))
            for name, default in now_set.items():
                if name not in have:
                    conn.execute("INSERT INTO modules_available (module, is_default, first_seen) VALUES (?, ?, ?)",
                                 (name, default, now))
                elif have[name] != default:
                    conn.execute("UPDATE modules_available SET is_default = ? WHERE module = ?", (default, name))
            conn.execute("INSERT INTO modules_status (id, read_at, count, paths) VALUES (1, ?, ?, ?) "
                         "ON CONFLICT(id) DO UPDATE SET read_at = excluded.read_at, count = excluded.count, "
                         "paths = excluded.paths", (now, len(now_set), ":".join(self.paths) or None))
            conn.commit()
        finally:
            conn.close()


def available(conn: sqlite3.Connection, site: str | None = None) -> list[tuple[str, bool]]:
    """A cluster's modules as last read: [(name, is_default)]; on the hub,
    one site's (source_site)."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(modules_available)")}
        if not cols:
            return []
        if site and "source_site" in cols:
            rows = conn.execute("SELECT module, is_default FROM modules_available WHERE source_site = ? "
                                "ORDER BY module", (site,)).fetchall()
        else:
            rows = conn.execute("SELECT module, is_default FROM modules_available ORDER BY module").fetchall()
    except sqlite3.Error:
        return []
    return [(m, bool(d)) for m, d in rows]


__all__ = ["ModulesCollector", "parse", "available"]

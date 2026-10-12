# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Processes the kernel killed for want of memory: who, what, when.

On a shared host without per-user limits -- a teaching server running
RStudio and JupyterHub, a cluster's login node -- one person's work can use
up the memory, and the kernel then kills a process to free some: often not
the one that took it, and often a student's notebook in the middle of a lab.
Nothing else records it; the student sees a dead kernel. Each kill goes in
``oom_kills``: when, which process (its program name and pid), whose
(uid and account name), which control group it ran in (a user's slice says
whose session it was), and how much memory it held.

Read from the kernel's messages in the system journal, from where the
previous run stopped (``oom_state``: this host's working state, not synced),
across reboots -- kills just before one (memory ran out, the server was
restarted) are the ones that matter most, and ``journalctl -k`` reads only
the current boot. The first run looks back ``lookback_days`` (30). The
account needs to read the journal (on most systems, membership of
systemd-journal or adm); when it cannot, the run says so. An account given
access later reads from then on: to look back, delete ``oom_state``.

Kernels word a kill differently over the years; all of these are read:

    oom-kill:constraint=CONSTRAINT_NONE,...,task_memcg=/user.slice/user-1234.slice/...,task=python3,pid=4321,uid=1234
    Out of memory: Killed process 4321 (python3) total-vm:..kB, anon-rss:..kB, ..., UID:1234 ...
    Memory cgroup out of memory: Killed process 4321 (python3) total-vm:..kB, anon-rss:..kB, ...
    Out of memory: Kill process 4321 (python3) score 900 or sacrifice child
    Killed process 4321 (python3) total-vm:..kB, anon-rss:..kB, file-rss:..kB
"""
from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
import time
from datetime import datetime
from typing import Any

from nomad.collectors.base import BaseCollector, find_tool, registry, tool_env

logger = logging.getLogger(__name__)

KILLED = re.compile(r"(?P<cgroup>Memory cgroup out of memory: )?(?:Out of memory: )?"
                    r"Killed process (?P<pid>\d+) \((?P<comm>[^)]*)\)(?P<rest>.*)")
OOM_KILL = re.compile(r"oom-kill:(?P<fields>.*)")
SLICE_UID = re.compile(r"user-(\d+)\.slice")


def _kb(text: str, name: str) -> int | None:
    m = re.search(rf"\b{name}:(\d+)kB", text)
    return int(m.group(1)) if m else None


def _fields(text: str) -> dict:
    """oom-kill:a=b,c=d,global_oom,... as a dict (bare words map to True)."""
    out = {}
    for part in text.split(","):
        k, sep, v = part.partition("=")
        out[k.strip()] = v.strip() if sep else True
    return out


def parse(entries: list[tuple[str, str]], host: str) -> list[dict]:
    """Kills from (timestamp, message) pairs in order, joining each
    "Killed process" line with the "oom-kill:" line before it that names the
    same pid."""
    details: dict[str, dict] = {}
    kills = []
    for when, msg in entries:
        m = OOM_KILL.search(msg)
        if m:
            f = _fields(m.group("fields"))
            if f.get("pid"):
                details[str(f["pid"])] = f
            continue
        m = KILLED.search(msg)
        if not m:
            continue
        pid, rest = m.group("pid"), m.group("rest")
        f = details.pop(pid, {})
        uid = None
        u = re.search(r"\bUID:(\d+)", rest)
        if u:
            uid = int(u.group(1))
        elif str(f.get("uid", "")).isdigit():
            uid = int(f["uid"])
        memcg = f.get("task_memcg") if isinstance(f.get("task_memcg"), str) else None
        if uid is None and memcg:
            s = SLICE_UID.search(memcg)
            uid = int(s.group(1)) if s else None
        kills.append({
            "timestamp": when,
            "host": host,
            "pid": int(pid),
            "process": m.group("comm"),
            "uid": uid,
            "memcg": memcg,
            "constraint_type": f.get("constraint") if isinstance(f.get("constraint"), str)
            else ("CONSTRAINT_MEMCG" if m.group("cgroup") else None),
            "anon_rss_kb": _kb(rest, "anon-rss"),
            "total_vm_kb": _kb(rest, "total-vm"),
        })
    return kills


def _account(uid: int | None) -> str | None:
    if uid is None:
        return None
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError, OverflowError):
        return None


@registry.register
class OOMCollector(BaseCollector):
    """Out-of-memory kills from the kernel's messages."""
    name = "oom"
    description = "Processes the kernel killed for want of memory"
    default_interval = 300

    def __init__(self, config: dict[str, Any], db_path):
        super().__init__(config, db_path)
        try:
            self.lookback_days = max(0, int(config.get("lookback_days", 30)))
        except (TypeError, ValueError):
            self.lookback_days = 30
        self.host = socket.gethostname().split(".")[0]

    # -- reading the journal ------------------------------------------------
    def _state(self) -> dict:
        try:
            with self.get_db_connection() as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS oom_state (key TEXT PRIMARY KEY, value TEXT)")
                return dict(conn.execute("SELECT key, value FROM oom_state").fetchall())
        except Exception:
            return {}

    def _journal(self, args: list[str]):
        exe = find_tool("journalctl")
        # Kernel messages of every boot (_TRANSPORT=kernel; -k would be this
        # boot's only), and only the fields read.
        def run(fields):
            return subprocess.run([exe, "-o", "json"] + fields + ["--no-pager", "-q"]
                                  + args + ["_TRANSPORT=kernel"],
                                  capture_output=True, text=True, timeout=300, env=tool_env())
        res = run(["--output-fields=MESSAGE"])
        if res.returncode != 0 and "output-fields" in (res.stderr or ""):
            res = run([])                     # systemd before 236
        return res

    def collect(self) -> list[dict[str, Any]]:
        if not find_tool("journalctl"):
            self.note = "journalctl not found: the kernel's messages cannot be read here"
            return []
        state = self._state()
        if state.get("cursor"):
            res = self._journal([f"--after-cursor={state['cursor']}"])
            if res.returncode != 0 and state.get("last_ts"):
                # The journal was rotated or vacuumed past the cursor.
                res = self._journal([f"--since={state['last_ts']}"])
        elif state.get("last_ts"):
            res = self._journal([f"--since={state['last_ts']}"])
        elif self.lookback_days:
            res = self._journal([f"--since=-{self.lookback_days}d"])
        else:
            res = self._journal(["--since=now"])
        if res.returncode != 0:
            err = (res.stderr or "").strip()
            if "permission" in err.lower() or "no journal files" in err.lower():
                self.note = (f"the kernel's messages are not readable as this account "
                             f"(usually needs the systemd-journal or adm group): {err[-120:]}")
                return []
            raise RuntimeError(f"journalctl failed: {err[-200:]}")
        entries, cursor, last_ts, seen = [], state.get("cursor"), state.get("last_ts"), 0
        for line in res.stdout.splitlines():
            try:
                e = json.loads(line)
            except ValueError:
                continue
            cursor = e.get("__CURSOR") or cursor
            msg = e.get("MESSAGE")
            if isinstance(msg, list):          # non-UTF-8 messages come as byte lists
                msg = bytes(b for b in msg if isinstance(b, int)).decode("utf-8", "replace")
            if not isinstance(msg, str):
                continue
            try:
                usec = int(e.get("__REALTIME_TIMESTAMP"))
                when = datetime.fromtimestamp(usec / 1e6)
            except (TypeError, ValueError, OverflowError, OSError):
                continue
            # Seconds since 1970 (journalctl --since=@N): local time would be
            # ambiguous in the hour repeated when daylight time ends.
            last_ts = f"@{usec // 1_000_000}"
            seen += 1
            if "oom-kill:" in msg or "Killed process" in msg:
                entries.append((when.isoformat(timespec="seconds"), msg))
        if not seen and not state:
            # A first look that finds no kernel message: from now on the look
            # starts here. Looking back days and finding none at all, this
            # account most likely cannot read them (journalctl says nothing
            # then): said once.
            if self.lookback_days:
                self.note = ("no kernel messages readable: the account may need the "
                             "systemd-journal or adm group to read them")
            last_ts = f"@{int(time.time())}"
        kills = parse(entries, self.host)
        for k in kills:
            k["username"] = _account(k["uid"])
        return [{"kills": kills, "cursor": cursor, "last_ts": last_ts}]

    def count_records(self, data: list[dict[str, Any]]) -> int:
        return len(data[0].get("kills", [])) if data else 0

    def store(self, data: list[dict[str, Any]]) -> None:
        payload = data[0]
        with self.get_db_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS oom_kills (
                    timestamp       TEXT NOT NULL,
                    host            TEXT NOT NULL,
                    pid             INTEGER NOT NULL,
                    process         TEXT,
                    uid             INTEGER,
                    username        TEXT,
                    memcg           TEXT,
                    constraint_type TEXT,
                    anon_rss_kb     INTEGER,
                    total_vm_kb     INTEGER,
                    UNIQUE (host, timestamp, pid)
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_oom_kills_time ON oom_kills(timestamp)")
            conn.execute("CREATE TABLE IF NOT EXISTS oom_state (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany(
                "INSERT OR IGNORE INTO oom_kills (timestamp, host, pid, process, uid, username, "
                "memcg, constraint_type, anon_rss_kb, total_vm_kb) VALUES (?,?,?,?,?,?,?,?,?,?)",
                [(k["timestamp"], k["host"], k["pid"], k["process"], k["uid"], k.get("username"),
                  k["memcg"], k["constraint_type"], k["anon_rss_kb"], k["total_vm_kb"])
                 for k in payload.get("kills", [])])
            for key in ("cursor", "last_ts"):
                if payload.get(key):
                    conn.execute("INSERT OR REPLACE INTO oom_state (key, value) VALUES (?, ?)",
                                 (key, payload[key]))
            conn.commit()

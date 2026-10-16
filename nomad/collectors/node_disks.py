# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Local disks of a cluster's compute nodes, read from the head node (1.14).

Slurm knows a node's local disk only as configured (TmpDisk, often 0) and
never how full it is, and nothing else nomad collects reads the nodes'
filesystems: a job filling /localscratch went unseen, even where that folder
lives on the node's system disk and filling it can take the node down.

On the head node, every ``interval_minutes`` (30), this asks each node that
Slurm lists and that isn't down for ``df`` of each path in ``paths``, over
ssh (BatchMode: an account with key or host-based access to the nodes, as
the collector's account usually has inside a cluster), a few nodes at a
time, for at most ``max_seconds`` in all. Each reading says which filesystem
holds the path (``mount``): "/" means the path is a folder on the system
disk. Alerts at the disk thresholds (80 and 95% by default,
[alerts.thresholds.disk]), one per node and path.

The interval runs from when a sweep started, whatever came of it: a sweep
where no node answered (their logins hang on a dead home directory, say) is
not repeated at every cron run, nor retried within one.

    [collectors.node_disks]
    enabled = true
    paths = ["/localscratch"]      # each node's local work space
    # interval_minutes = 30
    # parallel = 8                 # nodes asked at once
    # timeout = 10                 # seconds a node has to answer
    # max_seconds = 120            # a sweep stops asking after this long
    # ssh_user = "..."             # default: this account
    # nodes = ["cn01", "cn02"]     # default: every node sinfo lists

Table ``node_disks``: timestamp, node, path, mount, device, total_bytes,
used_bytes, available_bytes, used_percent. Table ``node_disks_sweeps``: when
each sweep started, how many nodes it asked and how many answered (this
host's own working state; the hub doesn't copy it).
"""
from __future__ import annotations

import logging
import re
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timedelta
from typing import Any

from nomad.collectors.base import (BaseCollector, CollectionError, MissingToolError, find_tool,
                                   tool_env)

logger = logging.getLogger(__name__)

OUTPUT_MARK = "__NOMAD_OUTPUT_BEGINS__"
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,252}$")
# Slurm states of a node that won't answer (sinfo %t): down, powered down or
# powering, rebooting, configured for the future but not there. A node that
# is failing, drained or in an unknown state is usually up, and is asked.
# The marks after a state: * not responding, ~ powered off, # % ! powering
# up or down.
UNREACHABLE = ("down", "pow", "boot", "futr")
MARKS = "*~#%!"

SCHEMA = """
CREATE TABLE IF NOT EXISTS node_disks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    node TEXT NOT NULL,
    path TEXT NOT NULL,
    mount TEXT,
    device TEXT,
    total_bytes INTEGER,
    used_bytes INTEGER,
    available_bytes INTEGER,
    used_percent REAL
);
CREATE INDEX IF NOT EXISTS idx_node_disks_node_time ON node_disks(node, timestamp);
CREATE INDEX IF NOT EXISTS idx_node_disks_time ON node_disks(timestamp);
CREATE TABLE IF NOT EXISTS node_disks_sweeps (
    timestamp TEXT NOT NULL,
    asked INTEGER,
    answered INTEGER,
    readings INTEGER
);
"""


def paths_from(config: dict) -> list[str]:
    """The paths to read: absolute, one line each; the rest left out (logged)."""
    raw = config.get("paths", ["/localscratch"])
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for p in raw if isinstance(raw, list) else []:
        if isinstance(p, str) and p.startswith("/") and "\n" not in p and "\0" not in p:
            out.append(p)
        else:
            logger.warning(f"[collectors.node_disks] paths: {p!r} is not an absolute path; left out")
    return out


def remote_command(paths: list[str]) -> str:
    """One line per path: the path, a tab, then df's line for it (empty when
    the path isn't there). Printed after OUTPUT_MARK, so a login banner is
    never read as a reading."""
    quoted = " ".join(shlex.quote(p) for p in paths)
    script = (f"echo {OUTPUT_MARK}; for p in {quoted}; do "
              "r=$(df -P -B1 -- \"$p\" 2>/dev/null | tail -n 1); "
              "printf '%s\\t%s\\n' \"$p\" \"$r\"; done")
    # In sh whatever the account's login shell (csh has no $(...)).
    return "sh -c " + shlex.quote(script)


def parse(node: str, output: str, when: str) -> list[dict[str, Any]]:
    """Readings from one node's answer to remote_command."""
    head, mark, rest = output.partition(OUTPUT_MARK + "\n")
    text = rest if mark else output
    out = []
    for line in text.splitlines():
        path, _tab, df = line.partition("\t")
        f = df.split()
        if not path.startswith("/") or len(f) < 6:
            continue
        try:
            total, used, avail = int(f[1]), int(f[2]), int(f[3])
        except ValueError:
            continue
        # df's own Use%: used of what users can have (used + available).
        pct = round(100.0 * used / (used + avail), 2) if used + avail > 0 else None
        out.append({"type": "node_disk", "timestamp": when, "node": node, "path": path,
                    "mount": " ".join(f[5:]), "device": f[0], "total_bytes": total,
                    "used_bytes": used, "available_bytes": avail, "used_percent": pct})
    return out


def nodes_from_sinfo(output: str) -> list[tuple[str, str]]:
    """(node, host) from ``sinfo -a -N -h -o '%N %n %t'``, once each, without
    those that won't answer (down, powered off, not responding). The node is
    Slurm's NodeName, as node_state has it; the host is its NodeHostname,
    which ssh reaches (most often the same)."""
    seen, out = set(), []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        node, host, state = parts[0], parts[1], parts[2].lower()
        if node in seen or not HOST_RE.match(node):
            continue
        seen.add(node)
        if state[-1:] in MARKS or any(state.startswith(s) for s in UNREACHABLE):
            continue
        out.append((node, host if HOST_RE.match(host) else node))
    return out


class NodeDisksCollector(BaseCollector):
    name = "node_disks"
    description = "Compute nodes' local disks (df over ssh from the head node)"
    default_interval = 1800

    def __init__(self, config: dict[str, Any], db_path):
        super().__init__(config, db_path)
        self.paths = paths_from(self.config)
        self.every = timedelta(minutes=float(self.config.get("interval_minutes", 30) or 30))
        self.parallel = max(1, int(self.config.get("parallel", 8) or 8))
        self.timeout = max(2, int(self.config.get("timeout", 10) or 10))
        self.max_seconds = max(1.0, float(self.config.get("max_seconds", 120) or 120))
        # A sweep is many attempts already; one that failed is not run again
        # straight away (BaseCollector retries three times by default).
        self._max_retries = max(1, int(self.config.get("max_retries", 1) or 1))
        self.ssh_user = self.config.get("ssh_user") or None
        self.listed = [n for n in (self.config.get("nodes") or []) if isinstance(n, str)]

    # -- when ---------------------------------------------------------------------

    def _read_recently(self) -> datetime | None:
        """When the last sweep started, if newer than interval_minutes
        (collect --once runs every collector at every cron run). A sweep
        that stored nothing counts: it is not tried again until then."""
        try:
            with self.get_db_connection() as conn:
                conn.executescript(SCHEMA)
                last = conn.execute(
                    "SELECT MAX(t) FROM (SELECT MAX(timestamp) AS t FROM node_disks_sweeps "
                    "UNION ALL SELECT MAX(timestamp) FROM node_disks)").fetchone()[0]
        except Exception:
            return None
        if not last:
            return None
        try:
            t = datetime.fromisoformat(str(last)[:19])
        except ValueError:
            return None
        return t if timedelta(0) <= datetime.now() - t < self.every - timedelta(seconds=30) \
            else None

    def _sweep(self, when: str, asked: int, answered: int | None = None,
               readings: int | None = None) -> None:
        """Note a sweep: at its start (so a run that hangs or fails still
        counts), then what came of it. The last few dozen are kept."""
        try:
            with self.get_db_connection() as conn:
                conn.executescript(SCHEMA)
                if answered is None:
                    conn.execute("INSERT INTO node_disks_sweeps (timestamp, asked) VALUES (?, ?)",
                                 (when, asked))
                    conn.execute("DELETE FROM node_disks_sweeps WHERE timestamp < ?",
                                 ((datetime.now() - timedelta(days=7)).isoformat(),))
                else:
                    conn.execute("UPDATE node_disks_sweeps SET answered = ?, readings = ? "
                                 "WHERE timestamp = ?", (answered, readings, when))
                conn.commit()
        except Exception as e:
            logger.warning(f"node_disks: could not note the sweep: {e}")

    # -- which nodes ----------------------------------------------------------------

    def _nodes(self) -> list[tuple[str, str]]:
        if self.listed:
            return [(n, n) for n in self.listed if HOST_RE.match(n)]
        sinfo = find_tool("sinfo")
        if not sinfo:
            raise CollectionError("sinfo not found: list the nodes in "
                                  "[collectors.node_disks] nodes, or run this on a head node")
        # -a: nodes in hidden partitions, or partitions this account can't use, too.
        r = subprocess.run([sinfo, "-a", "-N", "-h", "-o", "%N %n %t"], capture_output=True,
                           text=True, errors="replace", timeout=60, env=tool_env())
        if r.returncode != 0:
            raise CollectionError(f"sinfo failed: {r.stderr.strip()[:200]}")
        return nodes_from_sinfo(r.stdout)

    # -- asking a node ----------------------------------------------------------------

    def _ask(self, ssh: str, node: str, host: str, when: str
             ) -> tuple[str, list[dict] | None, str]:
        argv = [ssh, "-n", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.timeout}"]
        if self.ssh_user:
            argv += ["-l", str(self.ssh_user)]
        argv += ["--", host, remote_command(self.paths)]
        try:
            # errors="replace": a banner in another encoding is one node's
            # oddity, not a reason to lose every node's readings.
            r = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                               timeout=self.timeout + 10)
        except subprocess.TimeoutExpired:
            return node, None, "timed out"
        except Exception as e:
            return node, None, str(e)[:120]
        if r.returncode != 0 and OUTPUT_MARK not in r.stdout:
            return node, None, (r.stderr.strip().splitlines() or ["no answer"])[-1][:120]
        return node, parse(node, r.stdout, when), ""

    def collect(self) -> list[dict[str, Any]]:
        if not self.paths:
            self.note = "no paths to read ([collectors.node_disks] paths)"
            return []
        recent = self._read_recently()
        if recent is not None:
            self.note = (f"last asked at {recent:%H:%M}; every "
                         f"{self.every.total_seconds() / 60:.0f} minutes")
            return []
        ssh = find_tool("ssh")
        if not ssh:
            raise MissingToolError("ssh not found")
        nodes = self._nodes()
        if not nodes:
            self.note = "no node to read (none listed, or all down)"
            return []
        when = datetime.now().isoformat(timespec="seconds")
        self._sweep(when, len(nodes))
        pool = ThreadPoolExecutor(max_workers=min(self.parallel, len(nodes)))
        futures = [pool.submit(self._ask, ssh, n, h, when) for n, h in nodes]
        unasked = 0
        try:
            for _ in as_completed(futures, timeout=self.max_seconds):
                pass
        except FuturesTimeout:
            unasked = sum(1 for f in futures if f.cancel())
        finally:
            # Those already asking end within their own timeout, and count.
            pool.shutdown(wait=True, cancel_futures=True)
        records, failed, missing, answered = [], [], 0, 0
        for (n, _h), f in zip(nodes, futures):
            if f.cancelled():
                continue
            try:
                node, got, why = f.result()
            except Exception as e:                  # _ask catches its own; to be sure
                node, got, why = n, None, str(e)[:120]
            if got is None:
                failed.append(f"{node} ({why})")
                continue
            answered += 1
            missing += len(self.paths) - len(got)
            records.extend(got)
        self._sweep(when, len(nodes), answered, len(records))
        notes = []
        if failed:
            notes.append(f"{len(failed)} of {len(nodes)} nodes didn't answer: "
                         + ", ".join(failed[:5]) + (" ..." if len(failed) > 5 else ""))
        if unasked:
            notes.append(f"{unasked} node(s) not asked: the sweep stopped after "
                         f"{self.max_seconds:g} s (max_seconds)")
        if missing:
            notes.append(f"{missing} path reading(s) missing (not on that node)")
        if not answered:
            raise CollectionError("; ".join(notes) or "no node answered")
        self.note = "; ".join(notes) or None
        every = self.every.total_seconds() / 60
        for r in records:
            # For the alerts: how often a full disk is seen (see _check_node_disk).
            r["every_minutes"] = every
        return records

    def store(self, data: list[dict[str, Any]]) -> None:
        rows = [d for d in data if d.get("type") == "node_disk"]
        if not rows:
            return
        with self.get_db_connection() as conn:
            conn.executescript(SCHEMA)
            conn.executemany(
                "INSERT INTO node_disks (timestamp, node, path, mount, device, total_bytes, "
                "used_bytes, available_bytes, used_percent) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(d["timestamp"], d["node"], d["path"], d["mount"], d["device"],
                  d["total_bytes"], d["used_bytes"], d["available_bytes"], d["used_percent"])
                 for d in rows])
            conn.commit()

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

Who has the data (1.14.3): after the df readings, a few nodes are asked to
total each path by owner, on the node itself (find's own block counts, a
hard-linked file once, summed there, so only one line per owner comes
back): chosen from the readings just taken, full disks first (at
``owners_above`` percent, 80, or more: counted at every sweep, the one
counted longest ago first), then the others every ``owners_every_hours``
(6), at most ``owners_per_sweep`` nodes (``parallel``) at once. A link is
followed to its folder; the count stays on that filesystem, runs at the
lowest priority and stops after ``owners_seconds`` (60) per path. Folders the
account can't read are named by owner, not counted. The largest owners are
kept (``owners_top``, 5) and named in the disk's alert (``owners_in_alerts =
false`` keeps names out of alerts, which Slack and webhooks also receive).
``owners = false`` turns the counts off.

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
    # owners = true                # also total each path by owner
    # owners_every_hours = 6
    # owners_above = 80            # at this % or more: counted at every sweep
    # owners_seconds = 60          # a node's time to count one path
    # owners_top = 5               # owners kept per node and path
    # owners_per_sweep = 8         # nodes counted per sweep (at most parallel)
    # owners_in_alerts = true      # name the largest owners in alerts

Table ``node_disks``: timestamp, node, path, mount, device, total_bytes,
used_bytes, available_bytes, used_percent. Table ``node_disks_sweeps``: when
each sweep started, how many nodes it asked and how many answered (this
host's own working state; the hub doesn't copy it). Table
``node_disk_owner_counts``: each count (node, path, how many owners, bytes
counted, whether it finished and if not why); ``node_disk_owners``: its
largest owners and anyone with folders it couldn't read (username, uid,
bytes, files, unreadable folders).
"""
from __future__ import annotations

import logging
import pwd
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
CREATE TABLE IF NOT EXISTS node_disk_owner_counts (
    timestamp TEXT NOT NULL,
    node TEXT NOT NULL,
    path TEXT NOT NULL,
    owners INTEGER,
    bytes INTEGER,
    complete INTEGER,
    stopped TEXT
);
CREATE INDEX IF NOT EXISTS idx_node_disk_owner_counts ON node_disk_owner_counts(node, path, timestamp);
CREATE TABLE IF NOT EXISTS node_disk_owners (
    timestamp TEXT NOT NULL,
    node TEXT NOT NULL,
    path TEXT NOT NULL,
    username TEXT,
    uid INTEGER,
    bytes INTEGER,
    files INTEGER,
    unreadable INTEGER
);
CREATE INDEX IF NOT EXISTS idx_node_disk_owners ON node_disk_owners(node, path, timestamp);
"""

# Totals a path by owner on the node, after a C line naming the path: an O
# line per owner (uid, bytes as find's block counts, as du's, a file with
# several hard links once; files), a U line per owner of folders the account
# can't read (uid, how many; not counted), then an S line: find's exit
# status (124: stopped at the time limit; nopath, noaccess), permission
# errors and other errors. Files that vanish during the count are not errors.
# One line of sh with no ! (csh reads both as a login shell).
_OWNERS_AWK = (
    "/^__rc / {rc=$2; next} "
    "/^find: / {if ($0 ~ /ermission denied/) d++; else e++; next} "
    "$1 == \"D\" && NF == 2 {h[$2]++; next} "
    "NF == 4 && $1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ "
    "{if ($3 > 1 && l[$4]++) next; s[$1] += $2; n[$1]++; next} "
    "END {for (u in s) printf \"O\\t%s\\t%.0f\\t%.0f\\n\", u, s[u] * 512, n[u]; "
    "for (u in h) printf \"U\\t%s\\t%.0f\\n\", u, h[u]; "
    "printf \"S\\t%s\\t%.0f\\t%.0f\\n\", rc, d, e}")


def _owners_script(seconds: int) -> str:
    """The sh that counts "$p" by owner (see _OWNERS_AWK)."""
    return ("printf 'C\\t%s\\n' \"$p\"; "
            "q=$(cd -P -- \"$p\" 2>/dev/null && pwd -P); "
            "if [ -n \"$q\" ]; then io=; command -v ionice >/dev/null 2>&1 && io='ionice -c3 -t'; "
            f"{{ LC_ALL=C timeout -k 5 {int(seconds)} nice -n 19 $io find \"$q\" "
            "-ignore_readdir_race -xdev '(' -type d '(' -not -readable -o -not -executable ')' "
            "-printf 'D %U\\n' -prune ')' -o -printf '%U %b %n %i\\n' 2>&1; "
            "printf '\\n__rc %s\\n' \"$?\"; } | nice -n 19 awk " + shlex.quote(_OWNERS_AWK) + "; "
            "elif [ -e \"$p\" ]; then printf 'S\\tnoaccess\\t0\\t0\\n'; "
            "else printf 'S\\tnopath\\t0\\t0\\n'; fi")


def paths_from(config: dict) -> list[str]:
    """The paths to read: absolute, one line each; the rest left out (logged)."""
    raw = config.get("paths", ["/localscratch"])
    if isinstance(raw, str):
        raw = [raw]
    out = []
    for p in raw if isinstance(raw, list) else []:
        # ! is csh's history character, even in quotes: a node whose account
        # logs in to csh would answer nothing at all.
        if (isinstance(p, str) and p.startswith("/") and "\n" not in p and "\0" not in p
                and "!" not in p):
            if p not in out:
                out.append(p)
        else:
            logger.warning(f"[collectors.node_disks] paths: {p!r} is not an absolute path "
                           "(without !); left out")
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


def owners_command(paths: list[str], seconds: int = 60) -> str:
    """Each path's totals by owner (see _OWNERS_AWK), after OUTPUT_MARK."""
    parts = [f"echo {OUTPUT_MARK}"]
    for p in paths:
        parts.append(f"p={shlex.quote(p)}")
        parts.append(_owners_script(seconds))
    return "sh -c " + shlex.quote("; ".join(parts))


def parse_owners(output: str) -> dict[str, dict]:
    """{path: {"owners": [(uid, bytes, files), ...], "unreadable": {uid:
    folders}, "status": find's exit status, "nopath" or "noaccess", "denied":
    n, "errors": n}} from a node's answer."""
    _head, mark, rest = output.partition(OUTPUT_MARK + "\n")
    out: dict[str, dict] = {}
    cur = None
    for line in (rest if mark else "").splitlines():
        f = line.split("\t")
        if f[0] == "C" and len(f) >= 2:
            cur = out.setdefault("\t".join(f[1:]), {"owners": [], "unreadable": {},
                                                    "status": None, "denied": 0, "errors": 0})
        elif f[0] == "O" and cur is not None and len(f) == 4:
            try:
                cur["owners"].append((int(f[1]), int(float(f[2])), int(float(f[3]))))
            except ValueError:
                continue
        elif f[0] == "U" and cur is not None and len(f) == 3:
            try:
                cur["unreadable"][int(f[1])] = int(float(f[2]))
            except ValueError:
                continue
        elif f[0] == "S" and cur is not None and len(f) == 4:
            cur["status"] = f[1]
            try:
                cur["denied"], cur["errors"] = int(float(f[2])), int(float(f[3]))
            except ValueError:
                pass
            cur = None
    return out


def _name(uid: int, cache: dict) -> str:
    """The account's name, as the head node knows it (the nodes share its
    directory); the number when it has none."""
    if uid not in cache:
        try:
            cache[uid] = pwd.getpwuid(uid).pw_name
        except (KeyError, OverflowError, ValueError):
            cache[uid] = str(uid)
    return cache[uid]


def owners_summary(got: dict, top: int, names: dict) -> dict:
    """What a count found, for the record: the largest owners, named, those
    with folders the account couldn't read, and whether the count finished."""
    owners = sorted(got.get("owners") or [], key=lambda o: (-o[1], o[0]))
    hidden = got.get("unreadable") or {}
    status = got.get("status")
    if status == "0" and not got.get("denied") and not hidden:
        stopped = None
    elif status == "124":
        stopped = "time limit"
    elif status == "nopath":
        stopped = "no such folder"
    elif status == "noaccess":
        stopped = "no access"
    elif hidden or got.get("denied"):
        stopped = "unreadable folders"
    elif status is None:
        stopped = "no answer"
    else:
        stopped = "count failed"
    sizes = {u: (b, n) for u, b, n in owners}
    kept = [u for u, _b, _n in owners[:max(0, top)]]
    kept += sorted(u for u in hidden if u not in kept)[:5]       # whose folders weren't read
    return {"owners": len(set(sizes) | set(hidden)), "bytes": sum(o[1] for o in owners),
            "complete": stopped is None, "stopped": stopped,
            "unreadable_by": [_name(u, names) for u in sorted(hidden)][:5],
            "top": [{"user": _name(u, names), "uid": u, "bytes": sizes.get(u, (0, 0))[0],
                     "files": sizes.get(u, (0, 0))[1], "unreadable": hidden.get(u, 0)}
                    for u in kept]}


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
        c = self.config
        self.owners = c.get("owners", True) is not False
        self.owners_every = timedelta(hours=float(c.get("owners_every_hours", 6) or 6))
        self.owners_above = float(c.get("owners_above", 80) or 80)
        self.owner_seconds = max(5, int(c.get("owners_seconds", 60) or 60))
        self.owners_top = max(1, int(c.get("owners_top", 5) or 5))
        # One round of counts per sweep: no more nodes than are asked at once.
        self.owners_per_sweep = max(1, min(self.parallel, int(
            c.get("owners_per_sweep", self.parallel) or self.parallel)))
        self.owners_in_alerts = c.get("owners_in_alerts", True) is not False

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

    def _owners_due(self, records: list[dict]) -> dict[str, list[str]]:
        """{node: [path, ...]} to count by owner, from this sweep's readings,
        owners_per_sweep nodes, in this order:

        1. a disk full now (owners_above or more) that wasn't when last
           counted, or never was: its alert names who has the data;
        2. counts due: never counted, or not for owners_every_hours;
        3. full disks counted again at every sweep, the longest since first.

        Within each, never counted first, then full disks, then the longest
        since. A node that didn't answer, or doesn't have the path, has no
        reading and isn't counted (it would take a turn at every sweep)."""
        if not self.owners or not records:
            return {}
        last, then = {}, {}
        try:
            with self.get_db_connection() as conn:
                conn.executescript(SCHEMA)
                for node, path, ts, pct in conn.execute(
                        "SELECT l.node, l.path, l.ts, d.used_percent FROM "
                        "(SELECT node, path, MAX(timestamp) AS ts FROM node_disk_owner_counts "
                        " GROUP BY node, path) l LEFT JOIN node_disks d "
                        "ON d.node = l.node AND d.path = l.path AND d.timestamp = l.ts"):
                    last[(node, path)], then[(node, path)] = ts, pct
        except Exception as e:
            logger.warning(f"node_disks: could not read the last owner counts: {e}")
            return {}
        now = datetime.now()
        # A sweep starts a little later each time (cron, the sweep before):
        # a count due after six hours is not put off to the next sweep.
        every = self.owners_every - min(self.every / 2, timedelta(minutes=10))
        best: dict[str, tuple] = {}
        due: dict[str, list[str]] = {}
        for r in records:
            node, path, pct = r.get("node"), r.get("path"), r.get("used_percent")
            full = isinstance(pct, (int, float)) and pct >= self.owners_above
            try:
                age = now - datetime.fromisoformat(str(last[(node, path)])[:19])
            except (KeyError, ValueError):
                age = None
            if not (full or age is None or age >= every):
                continue
            was = then.get((node, path))
            newly = full and (age is None or not (isinstance(was, (int, float))
                                                  and was >= self.owners_above))
            tier = 0 if newly else 1 if (age is None or age >= every) else 2
            rank = (tier, 0 if age is None else 1, 0 if full else 1,
                    0 if age is None else -age.total_seconds())
            due.setdefault(node, []).append(path)
            best[node] = min(best.get(node, rank), rank)
        chosen = sorted(due, key=lambda n: (best[n], n))[:self.owners_per_sweep]
        return {n: due[n] for n in chosen}

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

    def _argv(self, ssh: str, host: str, command: str) -> list[str]:
        argv = [ssh, "-n", "-T", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.timeout}"]
        if self.ssh_user:
            argv += ["-l", str(self.ssh_user)]
        return argv + ["--", host, command]

    def _ask(self, ssh: str, node: str, host: str, when: str
             ) -> tuple[str, list[dict] | None, str]:
        try:
            # errors="replace": a banner in another encoding is one node's
            # oddity, not a reason to lose every node's readings.
            r = subprocess.run(self._argv(ssh, host, remote_command(self.paths)),
                               capture_output=True, text=True, errors="replace",
                               timeout=self.timeout + 10)
        except subprocess.TimeoutExpired:
            return node, None, "timed out"
        except Exception as e:
            return node, None, str(e)[:120]
        if r.returncode != 0 and OUTPUT_MARK not in r.stdout:
            return node, None, (r.stderr.strip().splitlines() or ["no answer"])[-1][:120]
        return node, parse(node, r.stdout, when), ""

    def _count(self, ssh: str, node: str, host: str, paths: list[str]) -> dict[str, dict]:
        """{path: what the count found} for one node ({} when it didn't answer)."""
        try:
            r = subprocess.run(self._argv(ssh, host, owners_command(paths, self.owner_seconds)),
                               capture_output=True, text=True, errors="replace",
                               timeout=self.timeout + 15 + (self.owner_seconds + 10) * len(paths))
        except Exception as e:
            logger.info(f"node_disks: {node}: count by owner not finished: {str(e)[:120]}")
            return {}
        return parse_owners(r.stdout)

    def _counts(self, ssh: str, records: list[dict], hosts: dict[str, str]) -> None:
        """Count the chosen nodes by owner (see _owners_due), into their
        readings: ``owners`` on each counted reading."""
        due = self._owners_due(records)
        if not due:
            return
        longest = max(len(v) for v in due.values())
        budget = self.timeout + 20 + (self.owner_seconds + 10) * longest
        pool = ThreadPoolExecutor(max_workers=len(due))
        futures = {pool.submit(self._count, ssh, n, hosts.get(n, n), paths): n
                   for n, paths in due.items()}
        found: dict[str, dict] = {}
        try:
            for f in as_completed(futures, timeout=budget):
                try:
                    found[futures[f]] = f.result()
                except Exception:
                    found[futures[f]] = {}
        except FuturesTimeout:
            pass
        finally:
            # Each count ends within its own time limit, inside the budget.
            pool.shutdown(wait=True, cancel_futures=True)
        names: dict = {}
        for rec in records:
            node, path = rec["node"], rec["path"]
            if node in due and path in due[node]:
                rec["owners"] = owners_summary((found.get(node) or {}).get(path, {}),
                                               self.owners_top, names)
                rec["owners_in_alerts"] = self.owners_in_alerts

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
        # Then who has the data, on a few nodes chosen from these readings.
        self._counts(ssh, records, dict(nodes))
        counted = [r["owners"] for r in records if r.get("owners")]
        if counted:
            short = sum(1 for c in counted if not c["complete"])
            notes.append(f"{len(counted)} path(s) counted by owner"
                         + (f", {short} not finished" if short else ""))
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
            counted = [d for d in rows if d.get("owners")]
            conn.executemany(
                "INSERT INTO node_disk_owner_counts (timestamp, node, path, owners, bytes, "
                "complete, stopped) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(d["timestamp"], d["node"], d["path"], d["owners"]["owners"],
                  d["owners"]["bytes"], int(d["owners"]["complete"]), d["owners"]["stopped"])
                 for d in counted])
            conn.executemany(
                "INSERT INTO node_disk_owners (timestamp, node, path, username, uid, bytes, "
                "files, unreadable) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(d["timestamp"], d["node"], d["path"], o["user"], o["uid"], o["bytes"],
                  o["files"], o.get("unreadable", 0)) for d in counted for o in d["owners"]["top"]])
            conn.commit()

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Each Slurm partition's settings, as ``scontrol show partition -o`` prints them.

Read at every node_state collection (one more scontrol call), stored only
when something changed:

- ``partition_settings``: one row per partition and cluster, as it is now
  (``gone_at`` once Slurm no longer lists it);
- ``partition_changes``: when a partition appeared, changed or went, and
  what changed (old and new values).

What the settings are for: which partitions a job fits and who may use them
(``nomad size``), which are preemptible guest partitions (PreemptMode,
PriorityTier, whatever the site calls them), the limits a site's jobs run
under (no default time limit, no maximum), and a record of when those
policies changed. It names partitions, and the accounts and groups allowed
in them, as Slurm lists them; no job or person.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta

SETTINGS = "partition_settings"
CHANGES = "partition_changes"
# A reading that changed nothing still says when it was last checked, at most
# this often (every write is a row the hub's sync sends).
CHECK_EVERY = timedelta(hours=1)

# Typed columns, from scontrol's keys. Everything scontrol printed is also
# kept whole in ``settings`` (JSON).
_TEXT = {"State": "state", "Nodes": "nodes", "AllowAccounts": "allow_accounts",
         "DenyAccounts": "deny_accounts", "AllowGroups": "allow_groups", "AllowQos": "allow_qos",
         "DenyQos": "deny_qos", "QoS": "qos", "PreemptMode": "preempt_mode",
         "OverSubscribe": "oversubscribe", "TRES": "tres", "JobDefaults": "job_defaults",
         "TRESBillingWeights": "billing_weights"}
_INT = {"TotalNodes": "total_nodes", "TotalCPUs": "total_cpus", "MaxNodes": "max_nodes",
        "MaxCPUsPerNode": "max_cpus_per_node", "PriorityTier": "priority_tier",
        "PriorityJobFactor": "priority_job_factor", "GraceTime": "grace_time_s"}
_MEM = {"DefMemPerCPU": "def_mem_per_cpu_mb", "DefMemPerNode": "def_mem_per_node_mb",
        "MaxMemPerCPU": "max_mem_per_cpu_mb", "MaxMemPerNode": "max_mem_per_node_mb"}
_FLAG = {"Default": "is_default", "Hidden": "hidden", "RootOnly": "root_only",
         "ExclusiveUser": "exclusive_user"}
_TYPES = {**dict.fromkeys(_TEXT.values(), "TEXT"),
          **dict.fromkeys((*_INT.values(), *_MEM.values(), *_FLAG.values(), "max_time_min", "max_time_unlimited", "default_time_min"), "INTEGER")}
COLUMNS = ["cluster", "partition"] + sorted(_TYPES)
# The bookkeeping columns, beside the typed ones.
_KEPT = (("settings", "TEXT"), ("fingerprint", "TEXT"), ("first_seen", "DATETIME"),
         ("changed_at", "DATETIME"), ("checked_at", "DATETIME"), ("gone_at", "DATETIME"))
_NOT_SET = {"", "NONE", "N/A", "(NULL)", "NULL"}
_UNLIMITED = {"UNLIMITED", "INFINITE"}
_TIME = re.compile(r"^(?:(\d+)-)?(\d+)(?::(\d+))?(?::(\d+))?$")


def slurm_minutes(value) -> tuple[int | None, bool]:
    """(minutes, unlimited) of a Slurm time limit: 'UNLIMITED' -> (None, True),
    'NONE' -> (None, False), '7-00:00:00' -> (10080, False). Slurm's forms:
    minutes, minutes:seconds, hours:minutes:seconds, days-hours,
    days-hours:minutes, days-hours:minutes:seconds. Seconds round up."""
    s = str(value or "").strip().upper()
    if s in _UNLIMITED:
        return None, True
    if s in _NOT_SET:
        return None, False
    m = _TIME.match(s)
    if not m:
        return None, False
    days, a, b, c = m.groups()
    if days is not None:
        hours, minutes, seconds = int(a), int(b or 0), int(c or 0)
        total = int(days) * 86400 + hours * 3600 + minutes * 60 + seconds
    elif c is not None:
        total = int(a) * 3600 + int(b) * 60 + int(c)
    elif b is not None:
        total = int(a) * 60 + int(b)
    else:
        total = int(a) * 60
    return -(-total // 60), False


def _int(value) -> int | None:
    s = str(value or "").strip()
    return int(s) if s.isdigit() else None


def _mem_mb(value) -> int | None:
    """DefMemPerCPU and the like: megabytes, or None (UNLIMITED, unset)."""
    s = str(value or "").strip().upper()
    m = re.match(r"^(\d+)([KMGT]?)$", s)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2) or "M"
    return {"K": n // 1024, "M": n, "G": n * 1024, "T": n * 1024 * 1024}[unit]


def parse(text: str) -> list[dict]:
    """``scontrol show partition -o`` (one partition per line) as dicts: the
    typed columns, plus ``settings`` (every key=value as printed)."""
    out = []
    for line in (text or "").splitlines():
        pairs = {}
        for tok in line.split():
            key, eq, value = tok.partition("=")
            if eq:
                pairs[key] = value
        name = pairs.get("PartitionName")
        if not name:
            continue
        row = {"partition": name, "settings": pairs}
        for k, col in _TEXT.items():
            v = pairs.get(k)
            row[col] = None if v is None or v.upper() in _NOT_SET else v
        for k, col in _INT.items():
            row[col] = _int(pairs.get(k))
        for k, col in _MEM.items():
            row[col] = _mem_mb(pairs.get(k))
        for k, col in _FLAG.items():
            v = pairs.get(k)
            row[col] = None if v is None else int(v.upper().startswith("YES"))
        row["max_time_min"], unlimited = slurm_minutes(pairs.get("MaxTime"))
        row["max_time_unlimited"] = int(unlimited) if "MaxTime" in pairs else None
        row["default_time_min"], _ = slurm_minutes(pairs.get("DefaultTime"))
        out.append(row)
    return out


def fingerprint(row: dict) -> str:
    return hashlib.sha1(json.dumps(row.get("settings") or {}, sort_keys=True).encode()).hexdigest()


def ensure(conn: sqlite3.Connection) -> None:
    typed = ", ".join(f"{c} {_TYPES[c]}" for c in sorted(_TYPES))
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {SETTINGS} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cluster TEXT NOT NULL,
        partition TEXT NOT NULL,
        {typed},
        settings TEXT,
        fingerprint TEXT,
        first_seen DATETIME,
        changed_at DATETIME,
        checked_at DATETIME,
        gone_at DATETIME,
        UNIQUE(cluster, partition))""")
    conn.execute(f"""CREATE TABLE IF NOT EXISTS {CHANGES} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cluster TEXT NOT NULL,
        partition TEXT NOT NULL,
        at DATETIME NOT NULL,
        kind TEXT NOT NULL,
        changes TEXT)""")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{CHANGES}_at ON {CHANGES}(at)")
    have = _has(conn, SETTINGS)
    for c, kind in [*sorted(_TYPES.items()), *_KEPT]:
        if c not in have:                 # a column added in a later nomad
            conn.execute(f"ALTER TABLE {SETTINGS} ADD COLUMN {c} {kind}")


def store(conn: sqlite3.Connection, cluster: str, rows: list[dict], now: datetime | None = None) -> dict:
    """Write what changed since the last reading. Returns counts:
    {"new", "changed", "gone", "same"}."""
    now = (now or datetime.now()).replace(microsecond=0)
    stamp = now.isoformat()
    ensure(conn)
    have = {r[0]: r[1:] for r in conn.execute(
        f"SELECT partition, fingerprint, settings, checked_at, gone_at FROM {SETTINGS} WHERE cluster = ?",
        (cluster,))}
    counts = {"new": 0, "changed": 0, "gone": 0, "same": 0}
    seen = set()
    cols = [c for c in COLUMNS if c not in ("cluster", "partition")]
    for row in rows:
        name = row["partition"]
        seen.add(name)
        fp = fingerprint(row)
        values = [row.get(c) for c in cols]
        settings = json.dumps(row.get("settings") or {}, sort_keys=True)
        old = have.get(name)
        if old is None:
            conn.execute(
                f"INSERT INTO {SETTINGS} (cluster, partition, {', '.join(cols)}, settings, fingerprint, "
                f"first_seen, changed_at, checked_at) VALUES ({', '.join('?' * (len(cols) + 7))})",
                [cluster, name] + values + [settings, fp, stamp, stamp, stamp])
            conn.execute(f"INSERT INTO {CHANGES} (cluster, partition, at, kind, changes) VALUES (?, ?, ?, 'new', ?)",
                         (cluster, name, stamp, settings))
            counts["new"] += 1
            continue
        old_fp, old_settings, checked, gone = old
        if old_fp == fp and gone is None:
            last = _time(checked)
            if last is None or now - last >= CHECK_EVERY:
                conn.execute(f"UPDATE {SETTINGS} SET checked_at = ? WHERE cluster = ? AND partition = ?",
                             (stamp, cluster, name))
            counts["same"] += 1
            continue
        try:
            before = json.loads(old_settings or "{}")
        except ValueError:
            before = {}
        after = row.get("settings") or {}
        diff = {k: [before.get(k), after.get(k)] for k in sorted(set(before) | set(after))
                if before.get(k) != after.get(k)}
        kind = "back" if gone is not None else "changed"
        conn.execute(
            f"UPDATE {SETTINGS} SET {', '.join(c + ' = ?' for c in cols)}, settings = ?, fingerprint = ?, "
            f"changed_at = ?, checked_at = ?, gone_at = NULL WHERE cluster = ? AND partition = ?",
            values + [settings, fp, stamp, stamp, cluster, name])
        conn.execute(f"INSERT INTO {CHANGES} (cluster, partition, at, kind, changes) VALUES (?, ?, ?, ?, ?)",
                     (cluster, name, stamp, kind, json.dumps(diff, sort_keys=True)))
        counts["changed"] += 1
    for name, (_, _, _, gone) in have.items():
        if name not in seen and gone is None:
            conn.execute(f"UPDATE {SETTINGS} SET gone_at = ?, checked_at = ? WHERE cluster = ? AND partition = ?",
                         (stamp, stamp, cluster, name))
            conn.execute(f"INSERT INTO {CHANGES} (cluster, partition, at, kind, changes) VALUES (?, ?, ?, 'gone', NULL)",
                         (cluster, name, stamp))
            counts["gone"] += 1
    # A site's collector reads one cluster: rows under another name are from
    # before the cluster was renamed, and are no longer listed.
    for other, name in conn.execute(f"SELECT cluster, partition FROM {SETTINGS} WHERE cluster <> ? "
                                    "AND gone_at IS NULL", (cluster,)).fetchall():
        conn.execute(f"UPDATE {SETTINGS} SET gone_at = ? WHERE cluster = ? AND partition = ?",
                     (stamp, other, name))
        conn.execute(f"INSERT INTO {CHANGES} (cluster, partition, at, kind, changes) VALUES (?, ?, ?, 'gone', NULL)",
                     (other, name, stamp))
        counts["gone"] += 1
    return counts


def _time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T")[:19])
    except (TypeError, ValueError):
        return None


def _has(conn, table) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def current(conn: sqlite3.Connection, site: str | None = None, *, gone: bool = False) -> list[dict]:
    """The partitions as last read: dicts of the typed columns, plus
    ``site`` (on a hub: the site the row came from), ``settings`` (dict) and
    the times. ``gone``: also partitions Slurm no longer lists."""
    cols = _has(conn, SETTINGS)
    if not cols:
        return []
    site_col = "source_site" if "source_site" in cols else "NULL"
    sql = f"SELECT *, {site_col} AS _site FROM {SETTINGS}"
    where, args = [], []
    if not gone:
        where.append("gone_at IS NULL")
    if site is not None and "source_site" in cols:
        where.append("source_site = ?")
        args.append(site)
    if where:
        sql += " WHERE " + " AND ".join(where)
    cur = conn.execute(sql + " ORDER BY _site, cluster, partition", args)
    names = [d[0] for d in cur.description]
    out = []
    for r in cur:
        d = dict(zip(names, r))
        d["site"] = d.pop("_site")
        try:
            d["settings"] = json.loads(d.get("settings") or "{}")
        except ValueError:
            d["settings"] = {}
        out.append(d)
    return out


def changes(conn: sqlite3.Connection, site: str | None = None, since: datetime | None = None) -> list[dict]:
    """When partitions appeared, changed, came back or went, newest first."""
    cols = _has(conn, CHANGES)
    if not cols:
        return []
    site_col = "source_site" if "source_site" in cols else "NULL"
    sql = f"SELECT {site_col}, cluster, partition, at, kind, changes FROM {CHANGES}"
    where, args = [], []
    if site is not None and "source_site" in cols:
        where.append("source_site = ?")
        args.append(site)
    if since is not None:
        where.append("at >= ?")
        args.append(since.isoformat())
    if where:
        sql += " WHERE " + " AND ".join(where)
    out = []
    for s, cluster, part, at, kind, ch in conn.execute(sql + " ORDER BY at DESC, partition", args):
        try:
            what = json.loads(ch) if ch else None
        except ValueError:
            what = None
        out.append({"site": s, "cluster": cluster, "partition": part, "at": at, "kind": kind,
                    "changes": what})
    return out


_NAMED = ("allow_accounts", "deny_accounts", "allow_groups", "allow_qos", "deny_qos", "qos")
_NAMED_KEYS = {"AllowAccounts", "DenyAccounts", "AllowGroups", "AllowQos", "DenyQos", "QoS",
               "PartitionName", "Alternate"}


def masked(rows: list[dict], hist: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """``nomad partitions --mask`` (1.17.3): partitions as "partition N" (by
    site, in name order), and who may use them as how many are listed --
    partition names often name a PI, and accounts and groups name people.
    Limits, tiers, preemption and nodes stay."""
    names: dict = {}
    for r in sorted(rows or [], key=lambda r: (str(r.get("site")), str(r.get("partition")))):
        site = r.get("site") or r.get("cluster")
        n = sum(1 for k in names if k[0] == site) + 1
        names.setdefault((site, r.get("partition")), f"partition {n}")

    def name_of(r):
        site = r.get("site") or r.get("cluster")
        key = (site, r.get("partition"))
        if key not in names:
            names[key] = f"partition {sum(1 for k in names if k[0] == site) + 1}"
        return names[key]

    def count(v):
        v = str(v or "")
        if not v or v.upper() == "ALL":
            return v or None
        return f"{len([x for x in v.split(',') if x])} listed"

    out = []
    for r in rows or []:
        m = dict(r)
        m["partition"] = name_of(r)
        for k in _NAMED:
            if k in m:
                m[k] = count(m[k])
        m.pop("settings", None)
        out.append(m)
    changed = []
    for c in hist or []:
        m = dict(c)
        m["partition"] = name_of(c)
        if isinstance(m.get("changes"), dict):
            m["changes"] = {k: (["(listed)", "(listed)"] if k in _NAMED_KEYS else v)
                            for k, v in m["changes"].items()}
        changed.append(m)
    return out, changed


def is_guest(p: dict, partitions: list[dict]) -> bool:
    """A partition whose jobs can be pushed off by another partition's: its
    PreemptMode isn't OFF, and some other partition sharing a node with it
    has a higher PriorityTier. (With preempt/partition_prio that is how Slurm
    decides; a site preempting by QOS shows PreemptMode too.)"""
    mode = str(p.get("preempt_mode") or "").upper()
    if not mode or mode == "OFF":
        return False
    from nomad.hostlist import expand_hostlist
    mine = set(expand_hostlist(p.get("nodes")))
    tier = p.get("priority_tier") or 0
    for q in partitions:
        if q is p or q.get("partition") == p.get("partition") or q.get("site") != p.get("site"):
            continue
        if (q.get("priority_tier") or 0) > tier and mine & set(expand_hostlist(q.get("nodes"))):
            return True
    return False


def time_text(minutes, unlimited) -> str:
    if unlimited:
        return "none"
    if minutes is None:
        return "not set"
    d, rest = divmod(int(minutes), 1440)
    h, m = divmod(rest, 60)
    if d:
        return f"{d} d" + (f" {h} h" if h else "")
    if h:
        return f"{h} h" + (f" {m} min" if m else "")
    return f"{m} min"


def mem_text(mb) -> str:
    """In one unit (1.17.3): 4096 -> '4 GB', 7350 -> '7.2 GB', 29500 ->
    '28.8 GB'; under 1 GB in MB (512 -> '512 MB')."""
    mb = int(mb)
    if mb < 1024:
        return f"{mb} MB"
    gb = mb / 1024
    return f"{gb:.0f} GB" if abs(gb - round(gb)) < 0.005 else f"{gb:.1f} GB"

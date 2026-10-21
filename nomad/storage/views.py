# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What the storage_use tables say, for `nomad storage` and the Console:
each storage server's paths, who holds their space and how it changed, what
hasn't been touched, and when each owner last ran a job.

Works on a site's database and on the hub's combined one, where every table
carries source_site (the site whose collector read the server). A server read
by more than one site is taken from the one that read it last, so its figures
are never added up twice; `reads()` says when that happens.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

LINE_BREAKS = "/"        # the scan's folder for files whose names hold a line break


def _cols(conn, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.DatabaseError:
        return set()


def _has_rows(conn, table: str) -> bool:
    if not _cols(conn, table):
        return False
    return conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone() is not None


def available(conn) -> bool:
    """Whether anything has been stored (the tables exist from migration 25 on)."""
    return any(_has_rows(conn, t) for t in ("storage_paths", "storage_owners", "storage_tops"))


def _sites(conn) -> dict[str, str]:
    """server -> the site whose collector read it last (the hub); {} on a site."""
    if "source_site" not in _cols(conn, "storage_reports"):
        return {}
    out: dict[str, tuple[str, str]] = {}
    for server, site, started in conn.execute(
            "SELECT server, source_site, MAX(started) FROM storage_reports GROUP BY server, source_site"):
        if server not in out or (started or "") > out[server][1]:
            out[server] = (site, started or "")
    return {k: v[0] for k, v in out.items()}


class _Scope:
    """The site filter for one server, as SQL."""

    def __init__(self, conn):
        self.sites = _sites(conn)

    def of(self, server: str, alias: str = "") -> tuple[str, list]:
        if not self.sites:
            return "", []
        col = f"{alias}.source_site" if alias else "source_site"
        return f" AND {col} = ?", [self.sites.get(server, "")]


def _copies(conn, scope: _Scope) -> dict[str, str | None]:
    """server -> what it holds copies of (its latest report says), or None."""
    if "copy_of" not in _cols(conn, "storage_reports"):
        return {}
    out = {}
    if scope.sites:
        rows = conn.execute(
            "SELECT r.server, r.copy_of, r.source_site FROM storage_reports r JOIN (SELECT server, source_site, "
            "MAX(started) AS s FROM storage_reports GROUP BY server, source_site) l ON r.server = l.server "
            "AND r.source_site = l.source_site AND r.started = l.s")
    else:
        rows = conn.execute(
            "SELECT r.server, r.copy_of, NULL FROM storage_reports r JOIN (SELECT server, MAX(started) AS s "
            "FROM storage_reports GROUP BY server) l ON r.server = l.server AND r.started = l.s")
    for server, copy_of, site in rows:
        if scope.sites and site != scope.sites.get(server):
            continue
        out[server] = copy_of
    return out


def paths(conn) -> list[dict]:
    """Each server's paths: as their latest usage file said, and those only
    the scan has read (no usage file yet)."""
    scope = _Scope(conn)
    copies = _copies(conn, scope)
    out: dict[tuple[str, str], dict] = {}
    pc = _cols(conn, "storage_paths")
    if {"server", "path"} <= pc:
        site = ", p.source_site" if scope.sites else ", NULL"
        extra = ", p.covers, p.errors" if "covers" in pc else ", NULL, NULL"
        # The latest reading of each path from each site (a site's own, on the hub).
        by = ", source_site" if scope.sites else ""
        on = " AND p.source_site = l.source_site" if scope.sites else ""
        for r in conn.execute(
                "SELECT p.server, p.path, p.fs, p.dataset, p.total_bytes, p.used_bytes, p.available_bytes, "
                f"p.accounting, p.note, p.timestamp{extra}{site} FROM storage_paths p JOIN (SELECT server, path{by}, "
                f"MAX(timestamp) AS t FROM storage_paths GROUP BY server, path{by}) l "
                f"ON p.server = l.server AND p.path = l.path AND p.timestamp = l.t{on} ORDER BY p.server, p.path"):
            if scope.sites and r[12] != scope.sites.get(r[0]):
                continue
            out[(r[0], r[1])] = {
                "server": r[0], "path": r[1], "fs": r[2], "dataset": r[3], "total_bytes": r[4],
                "used_bytes": r[5], "available_bytes": r[6], "accounting": bool(r[7]), "note": r[8],
                "read_at": r[9], "covers": r[10], "errors": r[11], "copy_of": copies.get(r[0])}
    if {"server", "path"} <= _cols(conn, "storage_tops"):
        site = ", source_site" if scope.sites else ", NULL"
        for server, path, when, s in conn.execute(
                f"SELECT server, path, MAX(scanned_at){site} FROM storage_tops GROUP BY server, path"
                + (", source_site" if scope.sites else "")):
            if (server, path) in out or (scope.sites and s != scope.sites.get(server)):
                continue
            out[(server, path)] = {
                "server": server, "path": path, "fs": None, "dataset": None, "total_bytes": None,
                "used_bytes": None, "available_bytes": None, "accounting": False,
                "note": "no usage file read yet: the scan's figures only", "read_at": when,
                "covers": None, "errors": None, "copy_of": copies.get(server)}
    return [out[k] for k in sorted(out)]


def last_jobs(conn) -> dict[str, str]:
    """username -> the latest submit of their jobs anywhere."""
    cols = _cols(conn, "jobs")
    if "user_name" not in cols:
        return {}
    col = "submit_time" if "submit_time" in cols else "start_time"
    return dict(conn.execute(
        f"SELECT user_name, MAX({col}) FROM jobs WHERE user_name IS NOT NULL GROUP BY user_name"))


def owners(conn, server: str, path: str, kind: str = "user", days: int = 30,
           jobs: dict | None = None) -> list[dict]:
    """Each owner's space on a path: now, ``days`` before the latest reading
    (the reading nearest before then), quotas, and from the scans what is
    older than the ages. An owner with space in several ZFS datasets has it
    added up; their quotas are per dataset (``quotas``), and ``soft_bytes`` /
    ``hard_bytes`` are set only when there is one."""
    if not {"server", "path", "name"} <= _cols(conn, "storage_owners"):
        return []
    scope = _Scope(conn)
    sql, args = scope.of(server)
    latest = conn.execute("SELECT MAX(timestamp) FROM storage_owners WHERE server = ? AND path = ? "
                          f"AND kind = ?{sql}", (server, path, kind, *args)).fetchone()[0]
    if not latest:
        return []
    then = (datetime.fromisoformat(latest) - timedelta(days=days)).isoformat()
    before = conn.execute("SELECT MAX(timestamp) FROM storage_owners WHERE server = ? AND path = ? "
                          f"AND kind = ? AND timestamp <= ?{sql}",
                          (server, path, kind, then, *args)).fetchone()[0]
    rows: dict[str, dict] = {}
    for name, dataset, used, soft, hard in conn.execute(
            "SELECT name, dataset, used_bytes, soft_bytes, hard_bytes FROM storage_owners WHERE server = ? "
            f"AND path = ? AND kind = ? AND timestamp = ?{sql}", (server, path, kind, latest, *args)):
        r = rows.setdefault(name, {"name": name, "used_bytes": 0, "datasets": [], "quotas": [],
                                   "was_bytes": None, "_n": 0})
        r["used_bytes"] += used or 0
        r["_n"] += 1
        if dataset and dataset not in r["datasets"]:
            r["datasets"].append(dataset)
        if soft or hard:
            r["quotas"].append({"dataset": dataset, "used_bytes": used, "soft_bytes": soft, "hard_bytes": hard})
    if before:
        for name, used in conn.execute(
                "SELECT name, SUM(used_bytes) FROM storage_owners WHERE server = ? AND path = ? AND kind = ? "
                f"AND timestamp = ?{sql} GROUP BY name", (server, path, kind, before, *args)):
            if name in rows:
                rows[name]["was_bytes"] = used or 0
    scans = folders_by_owner(conn, server, path) if kind == "user" else {}
    if jobs is None:
        jobs = last_jobs(conn) if kind == "user" else {}
    for r in rows.values():
        one = r.pop("_n") == 1
        q = r["quotas"][0] if one and r["quotas"] else {}
        r["soft_bytes"], r["hard_bytes"] = q.get("soft_bytes"), q.get("hard_bytes")
        r["change_bytes"] = None if r["was_bytes"] is None else r["used_bytes"] - r["was_bytes"]
        # The scans key an owner no machine can name as "uid N".
        r["scan"] = scans.get(r["name"]) or (scans.get(f"uid {r['name']}") if r["name"].isdigit() else None)
        r["last_job"] = jobs.get(r["name"])
        r["read_at"] = latest
        r["compared_with"] = before
    return sorted(rows.values(), key=lambda r: -r["used_bytes"])


def _latest_tops(conn, server: str, path: str) -> list[tuple]:
    """Each top folder's scan that stands for it: the latest finished one,
    else the latest (a scan stopped at its budget, until one finishes)."""
    tc = _cols(conn, "storage_tops")
    if not {"server", "top"} <= tc:
        return []
    sql, args = _Scope(conn).of(server)
    denied = "denied" if "denied" in tc else "0"
    best: dict[str, tuple] = {}
    for row in conn.execute(
            "SELECT top, top_owner, bytes, files, newest, older1_bytes, older2_bytes, age1_days, age2_days, "
            f"scanned_at, complete, {denied}, top_uid FROM storage_tops WHERE server = ? AND path = ?{sql}",
            (server, path, *args)):
        cur = best.get(row[0])
        if cur is None or (row[10] or 0, row[9] or "") > (cur[10] or 0, cur[9] or ""):
            best[row[0]] = row
    return list(best.values())


def folders_by_owner(conn, server: str, path: str) -> dict[str, dict]:
    """owner -> what the scans say of the top folders they own: bytes,
    files, how much is older than each age, the newest change, and whether
    every one of them has a finished scan."""
    out: dict[str, dict] = {}
    for (top, owner, size, files, newest, o1, o2, a1, a2, when, complete,
         denied, uid) in _latest_tops(conn, server, path):
        # An owner the collecting machine can't name goes by number, never by
        # the folder's name: a folder named after someone isn't theirs.
        key = owner or (f"uid {uid}" if uid is not None else "owner unknown")
        r = out.setdefault(key, {"name": key, "tops": [], "bytes": 0, "files": 0, "older1_bytes": 0,
                                 "older2_bytes": 0, "newest": None, "ages": (a1, a2), "scanned_at": None,
                                 "complete": True, "denied": 0})
        r["tops"].append(top)
        r["bytes"] += size or 0
        r["files"] += files or 0
        r["older1_bytes"] += o1 or 0
        r["older2_bytes"] += o2 or 0
        r["denied"] += denied or 0
        if newest and (r["newest"] is None or newest > r["newest"]):
            r["newest"] = newest
        if when and (r["scanned_at"] is None or when < r["scanned_at"]):
            r["scanned_at"] = when            # the oldest scan behind these figures
        if when and (r.get("scanned_to") is None or when > r["scanned_to"]):
            r["scanned_to"] = when            # and the newest
        r["complete"] = r["complete"] and bool(complete)
    return out


def scan_owners(conn, server: str, path: str, jobs: dict | None = None) -> list[dict]:
    """Owners by what the scans found, largest first: for a path without
    per-owner accounting."""
    rows = list(folders_by_owner(conn, server, path).values())
    if jobs is None:
        jobs = last_jobs(conn)
    for r in rows:
        r["last_job"] = jobs.get(r["name"])
    return sorted(rows, key=lambda r: -r["bytes"])


def folders(conn, server: str, path: str, top: str) -> list[dict]:
    """One top folder's folders, largest first, from the scan that stands for it."""
    if not {"server", "top"} <= _cols(conn, "storage_folders"):
        return []
    sql, args = _Scope(conn).of(server)
    out = []
    for f, o, b, n, w, x1, x2, a1, a2, s, c in conn.execute(
            "SELECT folder, owner, bytes, files, newest, older1_bytes, older2_bytes, age1_days, "
            "age2_days, scanned_at, complete FROM storage_folders WHERE server = ? AND path = ? "
            f"AND top = ?{sql} ORDER BY bytes DESC", (server, path, top, *args)):
        out.append({"folder": f, "line_breaks": f == LINE_BREAKS, "owner": o, "bytes": b, "files": n,
                    "newest": w, "older1_bytes": x1, "older2_bytes": x2, "ages": (a1, a2),
                    "scanned_at": s, "complete": bool(c)})
    return out


def datasets(conn, server: str, path: str) -> list[dict]:
    """The ZFS filesystems under a path at its latest reading, with their quotas."""
    dc = _cols(conn, "storage_datasets")
    if "path" not in dc:
        return []
    sql, args = _Scope(conn).of(server)
    latest = conn.execute(f"SELECT MAX(timestamp) FROM storage_datasets WHERE server = ? AND path = ?{sql}",
                          (server, path, *args)).fetchone()[0]
    if not latest:
        return []
    return [{"dataset": d, "mountpoint": m, "used_bytes": u, "available_bytes": a, "quota_bytes": q,
             "refquota_bytes": rq, "referenced_bytes": ref}
            for d, m, u, a, q, rq, ref in conn.execute(
                "SELECT dataset, mountpoint, used_bytes, available_bytes, quota_bytes, refquota_bytes, "
                f"referenced_bytes FROM storage_datasets WHERE server = ? AND path = ? AND timestamp = ?{sql} "
                "ORDER BY dataset", (server, path, latest, *args))]


def coverage(conn, server: str, path: str, days: int = 7, now: datetime | None = None) -> dict:
    """How much of a path the scans cover: top folders known, finished within
    ``days``, the oldest finished scan, those never finished (too big for the
    time the scan has), and files or folders the scan couldn't read."""
    now = now or datetime.now()
    since = (now - timedelta(days=days)).isoformat()
    tops = _latest_tops(conn, server, path)
    done = [t for t in tops if t[10]]
    return {"tops": len(tops), "recent": sum(1 for t in done if (t[9] or "") >= since),
            "oldest": min((t[9] for t in done if t[9]), default=None),
            "newest": max((t[9] for t in done if t[9]), default=None),
            "unfinished": len(tops) - len(done), "denied": sum(t[11] or 0 for t in tops), "days": days}


def person(conn, name: str, jobs: dict | None = None) -> list[dict]:
    """One person's space on every path, copies apart (``copy_of``): what
    the filesystem counts for them, the change, quotas, and their top
    folders' scans. For their own page."""
    out = []
    jobs = {} if jobs is None else jobs
    for p in paths(conn):
        row = next((r for r in owners(conn, p["server"], p["path"], jobs=jobs) if r["name"] == name), None)
        scan = folders_by_owner(conn, p["server"], p["path"]).get(name)
        if row is None and scan is None:
            continue
        out.append({"server": p["server"], "path": p["path"], "copy_of": p["copy_of"],
                    "used_bytes": row["used_bytes"] if row else None,
                    "change_bytes": row["change_bytes"] if row else None,
                    "quotas": row["quotas"] if row else [],
                    "soft_bytes": row["soft_bytes"] if row else None,
                    "hard_bytes": row["hard_bytes"] if row else None,
                    "scan": scan, "read_at": row["read_at"] if row else p["read_at"]})
    return out


def reads(conn) -> list[dict]:
    """Each server: its latest read here (a site's database), its latest
    usage and scan files (who ran them, what they left out), what it holds
    copies of, and the sites that read it (more than one: only the latest
    is shown)."""
    out: dict[str, dict] = {}
    if "server" in _cols(conn, "storage_reads"):
        for server, when, note in conn.execute(
                "SELECT r.server, r.read_at, r.note FROM storage_reads r JOIN (SELECT server, MAX(read_at) "
                "AS t FROM storage_reads GROUP BY server) l ON r.server = l.server AND r.read_at = l.t"):
            out[server] = {"server": server, "read_at": when, "note": note}
    rc = _cols(conn, "storage_reports")
    if "server" in rc:
        scope = _Scope(conn)
        more = {c: f"r.{c}" if c in rc else "NULL" for c in ("run_by", "copy_of", "odd_names", "unreadable")}
        for server, mode, started, finished, host, ns, run_by, copy_of, odd, unread in conn.execute(
                "SELECT r.server, r.mode, r.started, r.finished, r.host, r.not_scanned, "
                f"{more['run_by']}, {more['copy_of']}, {more['odd_names']}, {more['unreadable']} "
                "FROM storage_reports r "
                "JOIN (SELECT server, mode, MAX(started) AS s FROM storage_reports GROUP BY server, mode) l "
                "ON r.server = l.server AND r.mode = l.mode AND r.started = l.s"):
            r = out.setdefault(server, {"server": server, "read_at": None, "note": None})
            r[mode] = {"started": started, "finished": finished, "host": host, "not_scanned": ns,
                       "run_by": run_by, "odd_names": odd, "unreadable": unread}
            r["copy_of"] = copy_of
        if scope.sites:
            for server, sites in conn.execute(
                    "SELECT server, GROUP_CONCAT(DISTINCT source_site) FROM storage_reports GROUP BY server"):
                if server in out:
                    out[server]["sites"] = sorted((sites or "").split(","))
                    out[server]["site"] = scope.sites.get(server)
    return sorted(out.values(), key=lambda r: r["server"])

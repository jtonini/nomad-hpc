# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""nomad's own databases (1.61.0): how big each is and how fast it grows,
old copies left beside them, and the space a vacuum gives back.

Which databases: the site's (``[database] path``, else ``nomad.db`` in
``[general] data_dir``), the hub's combined database where ``[hub]`` lists
sites, the helpers nomad keeps beside them (lab_check.db, notices.db, ...),
and any other ``*.db`` in those folders. Another one not written for
``stray_days`` (14) is an *old copy* (a copy made before an upgrade or a
restore); one that holds no rows at all is *empty*.

The ``databases`` collector reads them every ``every_hours`` (6): each
file's size, its write-ahead log, the free space inside it (what a vacuum
gives back), the disk's free space, and each table's highest row number
(rows written since the last reading: cheap, unlike measuring each table's
bytes). Kept in ``db_files`` and ``db_tables`` (synced to the hub, so the
Console shows every site's), for ``keep_days`` (400). It warns (an alert,
``database_growth``) when a database grew more in a day than
``alert_growth_mb`` (1024) and, once it has a week of days, than
``growth_factor`` (4) times its usual day. Where a database is set for
incremental vacuuming, it gives free space back once a week
(``incremental_vacuum``, true): that moves pages, never rows.

    [collectors.databases]
    # every_hours = 6
    # stray_days = 14            # a copy not written this long is an old copy
    # alert_growth_mb = 1024     # a day's growth that warns (and growth_factor times the usual)
    # growth_factor = 4
    # incremental_vacuum = true  # weekly, where the database is set for it
    # keep_days = 400            # these readings

``nomad db status`` says it all for this host (``--tables``: each table's
size, slower), ``--sites`` on the hub for every site; ``nomad db tidy``
removes old copies and empty databases, an old copy only when the live
database holds everything in it (``compare``); ``nomad db vacuum`` gives the
free space back (``--full``: once, setting the database for incremental
vacuuming; the hub then copies that site's database whole at its next sync,
since a full vacuum may renumber rows).
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# The other databases nomad keeps beside a site's or the hub's: never an old copy.
HELPERS = frozenset({"lab_check.db", "notices.db", "course_answers.db", "ml.db", "licenses.db",
                     "copies.db", "timeline.db", "predict.db", "activity.db", "nomad_demo.db",
                     "nomad_intervention.db", "cluster_monitor.db"})
ROLES = ("site", "hub", "helper", "other", "old copy", "empty")

SCHEMA = """
CREATE TABLE IF NOT EXISTS db_files (
    timestamp    TEXT NOT NULL,
    path         TEXT NOT NULL,
    role         TEXT NOT NULL,
    bytes        INTEGER,
    wal_bytes    INTEGER,
    free_bytes   INTEGER,
    auto_vacuum  TEXT,
    disk_free    INTEGER,
    disk_total   INTEGER,
    modified_at  TEXT,
    tables       INTEGER,
    note         TEXT
);
CREATE INDEX IF NOT EXISTS idx_db_files_path ON db_files(path, timestamp);
CREATE INDEX IF NOT EXISTS idx_db_files_time ON db_files(timestamp);
CREATE TABLE IF NOT EXISTS db_tables (
    timestamp  TEXT NOT NULL,
    path       TEXT NOT NULL,
    name       TEXT NOT NULL,
    max_rowid  INTEGER,
    min_rowid  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_db_tables_path ON db_tables(path, name, timestamp);
CREATE INDEX IF NOT EXISTS idx_db_tables_time ON db_tables(timestamp);
CREATE TABLE IF NOT EXISTS db_vacuums (
    timestamp     TEXT NOT NULL,
    path          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    bytes_before  INTEGER,
    bytes_after   INTEGER,
    seconds       REAL
);
"""


@dataclass
class Settings:
    every_hours: float = 6.0
    stray_days: int = 14
    alert_growth_mb: float = 1024.0
    growth_factor: float = 4.0
    incremental_vacuum: bool = True
    keep_days: int = 400
    problems: tuple = ()


def _num(raw, key, default, low, high, problems, whole=False):
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not low <= v <= high \
            or (whole and int(v) != v):
        problems.append(f"{key}: a number from {low} to {high}")
        return default
    return int(v) if whole else float(v)


def settings(raw: dict | None) -> Settings:
    raw = raw if isinstance(raw, dict) else {}
    problems: list = []
    s = Settings(
        every_hours=_num(raw, "every_hours", 6.0, 0, 168, problems),
        stray_days=_num(raw, "stray_days", 14, 1, 3650, problems, whole=True),
        alert_growth_mb=_num(raw, "alert_growth_mb", 1024.0, 1, 10 ** 7, problems),
        growth_factor=_num(raw, "growth_factor", 4.0, 1, 1000, problems),
        keep_days=_num(raw, "keep_days", 400, 7, 36500, problems, whole=True),
    )
    iv = raw.get("incremental_vacuum", True)
    if not isinstance(iv, bool):
        problems.append("incremental_vacuum: true or false")
        iv = True
    s.incremental_vacuum = iv
    s.problems = tuple(problems)
    return s


# ---- which databases

def site_db(config: dict) -> Path:
    """The site's database, as nomad collect finds it (nomad.cli.get_db_path)."""
    default = Path.home() / ".local" / "share" / "nomad"
    data_dir = Path(str((config.get("general") or {}).get("data_dir") or default)).expanduser()
    db = str((config.get("database") or {}).get("path") or "")
    if db:
        p = Path(db).expanduser()
        return p if p.is_absolute() else data_dir / p
    return data_dir / "nomad.db"


def hub_db(config: dict) -> Path | None:
    """The hub's combined database, where [hub] lists sites."""
    hub = config.get("hub") if isinstance(config.get("hub"), dict) else {}
    if not hub.get("sites"):
        return None
    from nomad.config import combined_db_path
    return combined_db_path(hub.get("output_db"))


def _rows_in(path: Path) -> int | None:
    """Whether a database holds any row (1) or none (0); None: not readable."""
    try:
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        try:
            names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                             "AND name NOT LIKE 'sqlite_%'")]
            for n in names:
                if c.execute(f"SELECT 1 FROM {_q(n)} LIMIT 1").fetchone():
                    return 1
            return 0
        finally:
            c.close()
    except sqlite3.Error:
        return None


def files(config: dict, s: Settings | None = None, now: float | None = None,
          site: Path | None = None) -> list[tuple[Path, str]]:
    """[(path, role)] of the databases here (see the module's notes)."""
    s = s or settings(None)
    now = now or time.time()
    mine = Path(site) if site else site_db(config)
    hub = hub_db(config)
    known = [(mine, "site")] + ([(hub, "hub")] if hub else [])
    dirs = {mine.parent, Path.home() / ".local" / "share" / "nomad"} | ({hub.parent} if hub else set())
    out, seen = [], set()

    def add(p: Path, role: str):
        try:
            r = p.resolve()
        except OSError:
            return
        if r in seen or not r.is_file():
            return
        seen.add(r)
        out.append((r, role))

    for p, role in known:
        add(p, role)
    for d in sorted(dirs):
        try:
            names = sorted(os.listdir(d))
        except OSError:
            continue
        for n in names:
            if not n.endswith(".db"):
                continue
            p = d / n
            if n in HELPERS:
                add(p, "helper")
                continue
            try:
                old = now - p.stat().st_mtime >= s.stray_days * 86400
            except OSError:
                continue
            rows = _rows_in(p) if old else 1
            add(p, "empty" if rows == 0 else ("old copy" if old else "other"))
    return out


# ---- a reading

def _ro(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    c.isolation_level = None
    return c


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def measure(path: Path) -> dict:
    """One database's reading, cheap: sizes, free space inside and on the
    disk, and each table's highest and lowest row numbers."""
    path = Path(path)
    st = path.stat()
    wal = Path(str(path) + "-wal")
    disk = shutil.disk_usage(path.parent)
    c = _ro(path)
    try:
        page = c.execute("PRAGMA page_size").fetchone()[0]
        free = c.execute("PRAGMA freelist_count").fetchone()[0] * page
        av = {0: "none", 1: "full", 2: "incremental"}.get(c.execute("PRAGMA auto_vacuum").fetchone()[0], "?")
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                         "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        tables = []
        for n in names:
            try:
                # Two subqueries, each a jump to one end of the table. As one
                # SELECT MAX(rowid), MIN(rowid) SQLite reads the whole table
                # (its shortcut takes a lone MIN or MAX only): 1.61.0 read every
                # database in full at each reading.
                hi, lo = c.execute(f"SELECT (SELECT MAX(rowid) FROM {_q(n)}), "
                                   f"(SELECT MIN(rowid) FROM {_q(n)})").fetchone()
            except sqlite3.Error:                     # WITHOUT ROWID: no row numbers
                continue
            tables.append({"name": n, "max_rowid": hi, "min_rowid": lo})
    finally:
        c.close()
    return {"path": str(path), "bytes": st.st_size, "wal_bytes": wal.stat().st_size if wal.exists() else 0,
            "free_bytes": free, "auto_vacuum": av, "disk_free": disk.free, "disk_total": disk.total,
            "modified_at": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            "tables": tables}


def table_sizes(path: Path) -> dict | None:
    """Each table's bytes with its indexes (SQLite's dbstat: reads every
    page, minutes on a database of gigabytes); None where SQLite has no dbstat."""
    c = _ro(path)
    try:
        owner = dict(c.execute("SELECT name, tbl_name FROM sqlite_master WHERE type IN ('table','index')"))
        out: dict = {}
        try:
            for name, n in c.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"):
                t = owner.get(name, name)
                out[t] = out.get(t, 0) + (n or 0)
        except sqlite3.Error:
            return None
        return out
    finally:
        c.close()


def ensure(conn) -> None:
    conn.executescript(SCHEMA)


def record(conn, reading: dict, role: str, when: datetime, note: str = "") -> None:
    ts = when.isoformat(timespec="seconds")
    conn.execute("INSERT INTO db_files (timestamp, path, role, bytes, wal_bytes, free_bytes, auto_vacuum, "
                 "disk_free, disk_total, modified_at, tables, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (ts, reading["path"], role, reading["bytes"], reading["wal_bytes"], reading["free_bytes"],
                  reading["auto_vacuum"], reading["disk_free"], reading["disk_total"], reading["modified_at"],
                  len(reading["tables"]), note or None))
    conn.executemany("INSERT INTO db_tables (timestamp, path, name, max_rowid, min_rowid) VALUES (?,?,?,?,?)",
                     [(ts, reading["path"], t["name"], t["max_rowid"], t["min_rowid"]) for t in reading["tables"]])


def last_reading(conn) -> datetime | None:
    try:
        t = conn.execute("SELECT MAX(timestamp) FROM db_files").fetchone()[0]
    except sqlite3.Error:
        return None
    try:
        return datetime.fromisoformat(t) if t else None
    except ValueError:
        return None


def prune(conn, keep_days: int, now: datetime) -> None:
    cut = (now - timedelta(days=keep_days)).isoformat(timespec="seconds")
    for t in ("db_files", "db_tables", "db_vacuums"):
        conn.execute(f"DELETE FROM {t} WHERE timestamp < ?", (cut,))


# ---- growth, from the readings kept

def daily(conn, path: str, days: int = 35, site: str | None = None) -> list[tuple[str, int]]:
    """[(day, bytes)]: each day's first reading of a database, the last ``days`` days."""
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    where, args = "path = ? AND timestamp >= ?", [path, since]
    if site is not None:
        where += " AND source_site = ?"
        args.append(site)
    out: dict = {}
    for ts, b in conn.execute(f"SELECT timestamp, bytes FROM db_files WHERE {where} ORDER BY timestamp", args):
        out.setdefault(ts[:10], b)
    return sorted(out.items())


def growth(conn, path: str, days: int = 7, site: str | None = None) -> float | None:
    """Bytes a day over the last ``days`` days (the file's own size), or None
    without readings that far apart."""
    pts = daily(conn, path, days + 1, site)
    if len(pts) < 2:
        return None
    (d0, b0), (d1, b1) = pts[0], pts[-1]
    span = (datetime.fromisoformat(d1) - datetime.fromisoformat(d0)).days
    return (b1 - b0) / span if span > 0 and b0 is not None and b1 is not None else None


def unusual_growth(conn, path: str, s: Settings, site: str | None = None) -> tuple[float, float | None] | None:
    """(the last day's growth, the usual day's) when the last day's warns, else None."""
    pts = daily(conn, path, 30, site)
    deltas = [b1 - b0 for (_, b0), (_, b1) in zip(pts, pts[1:]) if b0 is not None and b1 is not None]
    if not deltas:
        return None
    last, before = deltas[-1], deltas[:-1][-28:]
    if last < s.alert_growth_mb * 1024 * 1024:
        return None
    usual = statistics.median(before) if len(before) >= 7 else None
    if usual is not None and usual > 0 and last < s.growth_factor * usual:
        return None
    return last, usual


def table_growth(conn, path: str, days: int = 7, site: str | None = None) -> dict:
    """{table: rows written a day} over the last ``days`` days, from the highest row numbers."""
    since = (datetime.now() - timedelta(days=days + 1)).isoformat(timespec="seconds")
    where, args = "path = ? AND timestamp >= ?", [path, since]
    if site is not None:
        where += " AND source_site = ?"
        args.append(site)
    first: dict = {}
    last: dict = {}
    for ts, name, hi in conn.execute(f"SELECT timestamp, name, max_rowid FROM db_tables WHERE {where} "
                                     "ORDER BY timestamp", args):
        first.setdefault(name, (ts, hi))
        last[name] = (ts, hi)
    out = {}
    for name, (t0, h0) in first.items():
        t1, h1 = last[name]
        span = (datetime.fromisoformat(t1) - datetime.fromisoformat(t0)).total_seconds() / 86400
        if span >= 0.5 and h0 is not None and h1 is not None:
            out[name] = max(0.0, (h1 - h0) / span)
    return out


# ---- old copies: does the live database hold everything in it?

TIME_COLS = ("timestamp", "collected_at", "ts", "time", "day", "date", "sampled_at", "recorded_at",
             "created_at", "submit_time", "start_time", "end_time", "tried_at", "seen_at", "at", "first_seen")
# nomad trims these itself (raw readings kept a while, summaries kept): fewer rows in the live one is meant
TRIMMED = frozenset({"hw_temps", "cpu_clock", "per_user_sample", "per_user_state", "storage_owners",
                     "storage_tops", "storage_folders", "storage_paths", "storage_datasets", "storage_reports",
                     "storage_reads", "node_disks_sweeps", "policy_watch_hosts", "policy_watch_sweeps",
                     "job_early", "interactive_state", "interactive_idle", "collection_log", "per_user_alert",
                     "alert_state", "db_files", "db_tables", "db_vacuums"})
ENDS = frozenset({"group_membership"})          # rows of what has ended are dropped by design
# what each row is about, where a table (the hub's, made by the sync) declares no key of its own
NATURAL = {"workstation_process_record": ("hostname", "pid", "start_time"),
           "cluster_usage": ("cluster", "month", "tres"),
           "job_accounting": ("job_id", "cluster"),
           "group_membership": ("username", "group_name", "cluster")}


def _tables(c, schema="main"):
    return {r[0] for r in c.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='table' "
                                    "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '\\_nomad\\_%' ESCAPE '\\'")}


def _info(c, t, schema="main"):
    return list(c.execute(f"PRAGMA {schema}.table_info({_q(t)})"))


def compare(live: Path, copy: Path) -> dict:
    """Whether ``live`` holds everything nomad keeps that ``copy`` holds:
    {"ok", "notes": [what differs as meant], "short": [what only the copy has]}.

    Jobs and measured jobs by their keys (a pending array's placeholder, an
    id with [..], is replaced by the array's tasks: counted, not held against
    the copy); a table with its own key, or one whose rows are about
    something (NATURAL), by those keys (memberships that ended since:
    counted); any other table with a time column by its rows up to the copy's
    last day; tables nomad trims on purpose left out."""
    a = _ro(copy)
    b = _ro(live)
    b.execute("ATTACH DATABASE ? AS c", (f"file:{copy}?mode=ro",))
    notes, short = [], []
    try:
        ta, tb = _tables(a), _tables(b)
        for t in sorted(ta & tb - TRIMMED):
            ca, cb = _info(a, t), _info(b, t)
            na, nb = [r[1] for r in ca], [r[1] for r in cb]
            if not a.execute(f"SELECT 1 FROM {_q(t)} LIMIT 1").fetchone():
                continue
            pk_path = False
            if t in ("jobs", "job_summary"):
                key = [k for k in ("source_site", "cluster", "job_id") if k in na and k in nb]
                key = key if "job_id" in key else []
            elif t in NATURAL:
                key = [k for k in NATURAL[t] if k in na and k in nb]
                key = key if len(key) == len(NATURAL[t]) else []
                if key and "source_site" in na and "source_site" in nb:
                    key = ["source_site"] + key
            else:
                pk = [r[1] for r in sorted(ca, key=lambda r: r[5]) if r[5]]
                whole_id = len(pk) == 1 and (next(r[2] for r in ca if r[1] == pk[0]) or "").upper() == "INTEGER"
                key = [k for k in pk if k in nb] if pk and not whole_id else []
                if key and len(key) != len(pk):
                    key = []
                if key and "source_site" in na and "source_site" in nb and "source_site" not in key:
                    key = ["source_site"] + key
                pk_path = bool(key)
            if key and pk_path:
                cond = " AND ".join(f"y.{_q(k)} IS x.{_q(k)}" for k in key)
                gone = f"NOT EXISTS (SELECT 1 FROM main.{_q(t)} y WHERE {cond})"
                if "job_id" in key:
                    held = b.execute(f"SELECT COUNT(*) FROM c.{_q(t)} x WHERE {gone} AND x.job_id LIKE ?",
                                     ("%[%",)).fetchone()[0]
                    if held:
                        notes.append(f"{t}: {held:,} pending-array placeholder(s), replaced by their tasks")
                    gone += " AND x.job_id NOT LIKE '%[%'"
                missing = b.execute(f"SELECT COUNT(*) FROM c.{_q(t)} x WHERE {gone}").fetchone()[0]
                if missing and t in ENDS:
                    notes.append(f"{t}: {missing:,} membership(s) in the copy have ended since")
                elif missing:
                    short.append(f"{t}: {missing:,} only in the copy")
                continue
            if key:
                sel = ", ".join(_q(k) for k in key)
                have = set(b.execute(f"SELECT {sel} FROM main.{_q(t)}"))
                missing = placeholders = 0
                months: Counter = Counter()
                for row in set(a.execute(f"SELECT {sel} FROM {_q(t)}")):
                    if row in have:
                        continue
                    if "job_id" in key and "[" in str(row[key.index("job_id")]):
                        placeholders += 1
                        continue
                    missing += 1
                    if t == "workstation_process_record":
                        h, st = row[key.index("hostname")], row[key.index("start_time")]
                        try:
                            months[(h, datetime.fromtimestamp(float(st)).strftime("%Y-%m"))] += 1
                        except (TypeError, ValueError, OSError, OverflowError):
                            months[(h, "?")] += 1
                if placeholders:
                    notes.append(f"{t}: {placeholders:,} pending-array placeholder(s), replaced by their tasks")
                if missing and t in ENDS:
                    notes.append(f"{t}: {missing:,} membership(s) in the copy have ended since")
                elif missing:
                    where = ", ".join(f"{h} {m}: {n}" for (h, m), n in sorted(months.items())[:8])
                    short.append(f"{t}: {missing:,} only in the copy" + (f" ({where})" if where else ""))
                continue
            tc = next((x for x in TIME_COLS if x in na and x in nb), None)
            if not tc:
                continue
            n_copy = a.execute(f"SELECT COUNT(*) FROM {_q(t)}").fetchone()[0]
            last = a.execute(f"SELECT {_q(tc)} FROM {_q(t)} ORDER BY rowid DESC LIMIT 1").fetchone()[0]
            if isinstance(last, (int, float)):
                n_live = b.execute(f"SELECT COUNT(*) FROM main.{_q(t)} WHERE {_q(tc)} <= ?", (last,)).fetchone()[0]
            else:
                try:
                    nxt = (datetime.fromisoformat(str(last)[:10]) + timedelta(days=1)).strftime("%Y-%m-%d")
                except ValueError:
                    continue
                n_live = b.execute(f"SELECT COUNT(*) FROM main.{_q(t)} WHERE {_q(tc)} < ?", (nxt,)).fetchone()[0]
            if n_live < n_copy:
                short.append(f"{t}: {n_copy - n_live:,} fewer rows up to the copy's last day")
        for t in sorted(ta - tb - TRIMMED):
            if a.execute(f"SELECT 1 FROM {_q(t)} LIMIT 1").fetchone():
                short.append(f"{t}: only in the copy")
    finally:
        a.close()
        b.close()
    return {"ok": not short, "notes": notes, "short": short}


def is_hub_copy(path: Path) -> bool:
    """Whether a database is a hub's (its jobs carry the site they came from)."""
    try:
        c = _ro(path)
        try:
            return "source_site" in [r[1] for r in c.execute("PRAGMA table_info(jobs)")]
        finally:
            c.close()
    except sqlite3.Error:
        return False


def remove(path: Path) -> int:
    """A database removed with its -wal and -shm; the bytes given back."""
    n = 0
    for s in ("", "-wal", "-shm"):
        p = Path(str(path) + s)
        if p.exists():
            n += p.stat().st_size
            p.unlink()
    return n


# ---- vacuum

def incremental(path: Path, conn_timeout: float = 60) -> tuple[int, int]:
    """Free pages given back where the database is set for it (moves pages,
    never rows: safe with the hub's incremental sync). (before, after) bytes."""
    before = Path(path).stat().st_size
    c = sqlite3.connect(str(path), timeout=conn_timeout)
    try:
        if c.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
            return before, before
        c.execute("PRAGMA incremental_vacuum").fetchall()      # stepped to the end: a page a step
        c.commit()
    finally:
        c.close()
    return before, Path(path).stat().st_size


def full(path: Path, conn_timeout: float = 300) -> tuple[int, int, bool]:
    """Once: set for incremental vacuuming and vacuum whole. A whole vacuum
    may give rows new numbers, which the hub's incremental sync goes by: the
    site's change log is taken out (its notes from hubs kept), so the hub
    copies this database whole at its next sync. (before, after, log taken out)."""
    from nomad.db import delta_site
    before = Path(path).stat().st_size
    c = sqlite3.connect(str(path), timeout=conn_timeout, isolation_level=None)
    try:
        c.execute("PRAGMA auto_vacuum = INCREMENTAL")
        c.execute("VACUUM")
        had_log = c.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (delta_site.LOG,)).fetchone() is not None
        if had_log:
            delta_site.remove(c, notes=False)
    finally:
        c.close()
    return before, Path(path).stat().st_size, had_log


def vacuum_record(conn, path, kind: str, before: int, after: int, seconds: float, when: datetime) -> None:
    conn.execute("INSERT INTO db_vacuums (timestamp, path, kind, bytes_before, bytes_after, seconds) "
                 "VALUES (?,?,?,?,?,?)", (when.isoformat(timespec="seconds"), str(path), kind, before, after,
                                         round(seconds, 1)))


def last_vacuum(conn, path) -> datetime | None:
    try:
        t = conn.execute("SELECT MAX(timestamp) FROM db_vacuums WHERE path = ?", (str(path),)).fetchone()[0]
        return datetime.fromisoformat(t) if t else None
    except (sqlite3.Error, ValueError):
        return None


def size(n: float | None) -> str:
    if n is None:
        return "-"
    a = abs(n)
    s = (f"{a / 1e12:.2f} TB" if a >= 1e12 else f"{a / 1e9:.2f} GB" if a >= 1e9
         else f"{a / 1e6:.1f} MB" if a >= 1e5 else f"{a / 1e3:.0f} kB")
    return ("-" if n < 0 else "") + s

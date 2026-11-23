# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Keep rules (1.63.0): how long nomad keeps each kind of reading in full,
and what it keeps after that. Nothing is lost outright: older readings are
rolled up, so history stays, at less detail.

Part 1, the tables nothing in nomad reads in full detail beyond a few days:

- ``mpstat_core`` (CPU busy per core) and ``iostat_device`` (disk activity
  per device): full detail ``detail_days`` (90); then, in the same table, one
  row per core (device) an hour holding that hour's averages, for
  ``hourly_days`` (365); then one a day holding the day's averages, forever.
  (A day's average is the average of its hours.)
- ``workstation_mount_state`` (lab machines' mounts): full detail
  ``detail_days``; then for each day the readings of the day's last run, each
  machine and mount's last reading of the day, and its last reading of the
  day that was not mounted or not answering, so a day with trouble still
  says so.
- ``collection_log`` (each collector's runs): full detail ``detail_days``;
  then each collector's day in ``collection_log_daily`` (runs, failures,
  records, first and last run, the last error), and each collector's very
  first run kept as it was (when nomad started watching what it watches).

Readers keep reading the same tables: they find hours or days where the
readings are older. Jobs, their summaries, accounting, groups, alerts and
monthly usage are never trimmed.

It runs where the readings are written (each site, and the hub's own
database), never on the hub's combined database: the hub's copy follows the
sites. The first run removes many rows at once, so ``nomad db keep --apply``
does it: each table it trims is taken out of the change log first (the hub
copies that table whole at its next sync, as a new table is), then the
database is vacuumed. After that the databases collector runs it once a day
(a day's worth, through the change log), and never more than ``max_rows``
rows at once: past that it waits for ``nomad db keep --apply`` again.

Settings, in [collectors.databases]::

    keep_rules = true        # the collector's daily run (after nomad db keep --apply once here)
    detail_days = 90         # full detail this long
    hourly_days = 365        # then hours this long, then days (CPU per core, disk per device)
    keep_max_rows = 1000000  # a daily run that would go through more waits for nomad db keep --apply
    [collectors.databases.keep]          # a table's own, to differ
    mpstat_core = { detail_days = 180 }
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# -- the rules ---------------------------------------------------------------------------------

AVERAGES = "averages"        # hours, then days, of averages, in the table itself
DAILY_READING = "daily"      # the readings that say what a day was
DAILY_COUNTS = "counts"      # a day's runs counted in a table of their own


@dataclass(frozen=True)
class Rule:
    table: str
    kind: str
    time: str
    keys: tuple                  # what a row is about (with source_site and hostname, where the table has them)
    columns: tuple = ()          # averaged (AVERAGES)
    what: str = ""


RULES = (
    Rule("mpstat_core", AVERAGES, "timestamp", ("core_id",),
         ("user_percent", "nice_percent", "system_percent", "iowait_percent", "irq_percent", "soft_percent",
          "steal_percent", "idle_percent", "busy_percent"), "CPU busy per core"),
    Rule("iostat_device", AVERAGES, "timestamp", ("device",),
         ("reads_per_sec", "read_kb_per_sec", "read_await_ms", "writes_per_sec", "write_kb_per_sec",
          "write_await_ms", "util_percent", "queue_length"), "disk activity per device"),
    Rule("workstation_mount_state", DAILY_READING, "timestamp", ("hostname", "mountpoint"), (),
         "lab machines' mounts"),
    Rule("collection_log", DAILY_COUNTS, "started_at", ("collector",), (), "collector runs"),
)
BY_TABLE = {r.table: r for r in RULES}
WHERE_KEYS = ("source_site", "hostname")       # added to a rule's keys where the table has them

MARKS = "keep_marks"
RUNS = "keep_runs"
DAILY_LOG = "collection_log_daily"
SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {MARKS} (
    tbl      TEXT NOT NULL,
    level    TEXT NOT NULL,
    through  TEXT NOT NULL,
    PRIMARY KEY (tbl, level)
);
CREATE TABLE IF NOT EXISTS {RUNS} (
    timestamp    TEXT NOT NULL,
    how          TEXT NOT NULL,
    tbl          TEXT NOT NULL,
    days         INTEGER,
    rows_before  INTEGER,
    rows_after   INTEGER,
    seconds      REAL,
    note         TEXT
);
CREATE TABLE IF NOT EXISTS {DAILY_LOG} (
    collector     TEXT NOT NULL,
    day           TEXT NOT NULL,
    runs          INTEGER,
    failures      INTEGER,
    records       INTEGER,
    first_at      TEXT,
    last_at       TEXT,
    last_error    TEXT,
    PRIMARY KEY (collector, day)
);
"""


# -- settings ----------------------------------------------------------------------------------

@dataclass
class Settings:
    enabled: bool = True
    detail_days: int = 90
    hourly_days: int = 365
    max_rows: int = 1_000_000
    tables: dict = field(default_factory=dict)     # table: (detail_days, hourly_days)
    problems: tuple = ()

    def days(self, table: str) -> tuple[int, int]:
        return self.tables.get(table, (self.detail_days, self.hourly_days))


def _whole(raw, key, default, lo, hi, problems, where="[collectors.databases]"):
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        problems.append(f"{where} {key} = {v!r}: a whole number from {lo} to {hi}")
        return default
    return v


def settings(raw: dict | None) -> Settings:
    """From the databases collector's table ([collectors.databases])."""
    raw = raw if isinstance(raw, dict) else {}
    problems: list = []
    on = raw.get("keep_rules", True)
    if not isinstance(on, bool):
        problems.append("[collectors.databases] keep_rules: true or false, without quotes")
        on = True
    detail = _whole(raw, "detail_days", 90, 7, 36500, problems)
    hourly = _whole(raw, "hourly_days", 365, 7, 36500, problems)
    if hourly < detail:
        problems.append(f"[collectors.databases] hourly_days ({hourly}) is less than detail_days ({detail}): "
                        f"taken as {detail}")
        hourly = detail
    most = _whole(raw, "keep_max_rows", 1_000_000, 1000, 10 ** 9, problems)
    tables: dict = {}
    own = raw.get("keep", {})
    if not isinstance(own, dict):
        problems.append("[collectors.databases.keep]: a table of tables")
        own = {}
    for name, v in own.items():
        where = f"[collectors.databases.keep] {name}"
        if name not in BY_TABLE:
            problems.append(f"{where}: not a table nomad keeps rules for ({', '.join(BY_TABLE)}; left out)")
            continue
        if not isinstance(v, dict):
            problems.append(f"{where}: {{ detail_days = N, hourly_days = N }}")
            continue
        d = _whole(v, "detail_days", detail, 7, 36500, problems, where)
        h = _whole(v, "hourly_days", max(hourly, d), 7, 36500, problems, where)
        tables[name] = (d, max(h, d))
    return Settings(enabled=on, detail_days=detail, hourly_days=hourly, max_rows=most, tables=tables,
                    problems=tuple(problems))


# -- the work ----------------------------------------------------------------------------------

def _schema_parts() -> list:
    return [x.strip() for x in SCHEMA.split(";") if x.strip()]


def ensure(conn) -> None:
    """The keep rules' own tables (each statement on its own: executescript
    commits whatever was open first)."""
    for x in _schema_parts():
        conn.execute(x)
    conn.commit()


def _cols(conn, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


def _keys(rule: Rule, cols: set) -> list:
    return [k for k in WHERE_KEYS if k in cols and k not in rule.keys] + list(rule.keys)


def _day(d: date) -> str:
    return d.isoformat()


def _mark(conn, table: str, level: str) -> date | None:
    try:
        row = conn.execute(f"SELECT through FROM {MARKS} WHERE tbl = ? AND level = ?", (table, level)).fetchone()
    except sqlite3.Error:
        return None
    return date.fromisoformat(row[0]) if row else None


def _first_day(conn, rule: Rule) -> date | None:
    """The day of the table's oldest reading: by its time where an index
    starts with it, else its first row (rows are written in time order)."""
    try:
        if _indexed(conn, rule):
            v = conn.execute(f'SELECT MIN("{rule.time}") FROM "{rule.table}"').fetchone()[0]
        else:
            row = conn.execute(f'SELECT "{rule.time}" FROM "{rule.table}" ORDER BY rowid LIMIT 1').fetchone()
            v = row[0] if row else None
    except sqlite3.Error:
        return None
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def _indexed(conn, rule: Rule) -> bool:
    """Whether an index starts with the table's time column (MIN is then a lookup)."""
    try:
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?",
                                    (rule.table,)).fetchall():
            first = conn.execute(f'PRAGMA index_info("{name}")').fetchone()
            if first and first[2] == rule.time:
                return True
    except sqlite3.Error:
        pass
    return False


def _bound(conn, rule: Rule, day: str, lo: int, hi: int) -> int:
    """The first row number whose time is ``day`` or later, by halving the
    row numbers (rows are written in time order): a day's rows found without
    an index on the time column."""
    q = f'SELECT rowid, "{rule.time}" FROM "{rule.table}" WHERE rowid >= ? ORDER BY rowid LIMIT 1'
    while lo < hi:
        mid = (lo + hi) // 2
        row = conn.execute(q, (mid,)).fetchone()
        if row is None:
            hi = mid
        elif row[1] is None or str(row[1]) < day:
            lo = row[0] + 1
        else:
            hi = mid
    return lo


class Window:
    """The rows of [d0, d1): by their time, within the row numbers where
    those days are (rows a little out of time order elsewhere are left as
    they are, never touched by mistake)."""

    def __init__(self, conn, rule: Rule, d0: date, d1: date):
        self.args = (0, 0, _day(d0), _day(d1))
        try:
            lo, hi = conn.execute(f'SELECT MIN(rowid), MAX(rowid) FROM "{rule.table}"').fetchone()
        except sqlite3.Error:
            return
        if lo is None:
            return
        a = _bound(conn, rule, _day(d0), lo, hi + 1)
        b = _bound(conn, rule, _day(d1), a, hi + 1)
        self.args = (a, b, _day(d0), _day(d1))

    def where(self, rule: Rule, alias: str = "") -> str:
        a = f"{alias}." if alias else ""
        return f'{a}rowid >= ? AND {a}rowid < ? AND {a}"{rule.time}" >= ? AND {a}"{rule.time}" < ?'


def _rows_between(conn, rule: Rule, d0: date, d1: date) -> int:
    if d1 <= d0:
        return 0
    w = Window(conn, rule, d0, d1)
    return conn.execute(f'SELECT COUNT(*) FROM "{rule.table}" WHERE {w.where(rule)}', w.args).fetchone()[0]


@dataclass
class Todo:
    """What one table has to go through: by level, the days [start, end)."""
    table: str
    rule: Rule
    levels: list            # [(level, start, end)]
    rows: int               # rows in those days now
    present: bool = True


def _levels(rule: Rule) -> tuple:
    return ("hourly", "daily") if rule.kind == AVERAGES else ("detail",)


def todo(conn, s: Settings, today: date | None = None) -> list[Todo]:
    """Each rule's table here and what is due: hours from the days past
    detail_days, days from those past hourly_days (AVERAGES), or the days
    past detail_days (the others), from where the last run stopped."""
    today = today or date.today()
    out = []
    for rule in RULES:
        cols = _cols(conn, rule.table)
        if not cols or rule.time not in cols:
            out.append(Todo(rule.table, rule, [], 0, present=False))
            continue
        detail, hourly = s.days(rule.table)
        first = _first_day(conn, rule)
        levels = []
        if first is not None:
            ends = (today - timedelta(days=detail), today - timedelta(days=hourly))
            for level, end in zip(_levels(rule), ends):
                start = max(_mark(conn, rule.table, level) or first, first)
                if level == "daily":
                    # days are made of hours: never past where the hours have got to
                    end = min(end, _mark(conn, rule.table, "hourly") or start)
                if start < end:
                    levels.append((level, start, end))
        rows = sum(_rows_between(conn, rule, a, b) for _, a, b in levels) if levels else 0
        out.append(Todo(rule.table, rule, levels, rows))
    return out


def _keep_ids(conn) -> None:
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _keep_ids (id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM _keep_ids")


def _averages(conn, rule: Rule, cols: set, w: Window, width: int) -> None:
    """One row a key and hour (width 13) or day (width 10): the first row of
    each, holding the averages and the hour's (day's) start; the rest go."""
    keys = _keys(rule, cols)
    avg = [c for c in rule.columns if c in cols]
    t = f'"{rule.time}"'
    group = [f"substr({t}, 1, {width})"] + [f'"{k}"' for k in keys]
    rows = conn.execute(
        f"SELECT MIN(rowid), substr(MIN({t}), 1, {width}), substr(MIN({t}), 11, 1)"
        + "".join(f', AVG("{c}")' for c in avg)
        + f' FROM "{rule.table}" WHERE {w.where(rule)} GROUP BY ' + ", ".join(group), w.args).fetchall()
    _keep_ids(conn)
    conn.executemany("INSERT INTO _keep_ids (id) VALUES (?)", [(r[0],) for r in rows])

    def stamp(prefix, sep):
        return prefix + ":00:00" if width == 13 else prefix + (sep or "T") + "00:00:00"
    sets = ", ".join([f"{t} = ?"] + [f'"{c}" = ?' for c in avg])
    conn.executemany(f'UPDATE "{rule.table}" SET {sets} WHERE rowid = ?',
                     [(stamp(r[1], r[2]), *r[3:], r[0]) for r in rows])
    conn.execute(f'DELETE FROM "{rule.table}" WHERE {w.where(rule)} AND rowid NOT IN (SELECT id FROM _keep_ids)',
                 w.args)


def _daily_reading(conn, rule: Rule, cols: set, w: Window) -> None:
    """A day's last run's readings, each key's last reading, and each key's
    last reading that was not mounted or not answering; the rest go."""
    keys = _keys(rule, cols)
    t = f'"{rule.time}"'
    k = ", ".join(f'"{x}"' for x in keys)
    site = [x for x in WHERE_KEYS[:1] if x in cols]
    bad = " OR ".join(f'"{c}" = 0' for c in ("is_mounted", "is_responsive") if c in cols) or "0"
    _keep_ids(conn)
    conn.execute(f'INSERT OR IGNORE INTO _keep_ids SELECT MAX(rowid) FROM "{rule.table}" WHERE {w.where(rule)} '
                 f"GROUP BY {k}", w.args)
    conn.execute(f'INSERT OR IGNORE INTO _keep_ids SELECT MAX(rowid) FROM "{rule.table}" WHERE {w.where(rule)} '
                 f"AND ({bad}) GROUP BY {k}", w.args)
    sel = ", ".join([f"MAX({t}) AS t"] + [f'"{x}"' for x in site])
    by = f" GROUP BY {', '.join(chr(34) + x + chr(34) for x in site)}" if site else ""
    on = " AND ".join([f"r.{t} = l.t"] + [f'r."{x}" IS l."{x}"' for x in site])
    conn.execute(f'INSERT OR IGNORE INTO _keep_ids SELECT r.rowid FROM "{rule.table}" r JOIN '
                 f'(SELECT {sel} FROM "{rule.table}" WHERE {w.where(rule)}{by}) l ON {on} '
                 f"WHERE {w.where(rule, 'r')}", w.args + w.args)
    conn.execute(f'DELETE FROM "{rule.table}" WHERE {w.where(rule)} AND rowid NOT IN (SELECT id FROM _keep_ids)',
                 w.args)


def _first_runs(conn, rule: Rule) -> None:
    """Each collector's very first run, kept as it was (temp _keep_first)."""
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _keep_first (id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM _keep_first")
    keys = ", ".join(f'"{x}"' for x in _keys(rule, _cols(conn, rule.table)))
    conn.execute(f'INSERT OR IGNORE INTO _keep_first SELECT MIN(rowid) FROM "{rule.table}" GROUP BY {keys}')


def _daily_counts(conn, rule: Rule, cols: set, w: Window, day: str) -> None:
    """A day's runs of each collector counted in collection_log_daily; the
    rows then go, but for each collector's very first run."""
    t = f'"{rule.time}"'
    ok = '"success"' if "success" in cols else None
    fails = f"SUM(CASE WHEN {ok} = 0 OR {ok} = 'False' THEN 1 ELSE 0 END)" if ok else "NULL"
    recs = 'SUM("records_collected")' if "records_collected" in cols else "NULL"
    rows = conn.execute(f'SELECT collector, COUNT(*), {fails}, {recs}, MIN({t}), MAX({t}) FROM "{rule.table}" '
                        f"WHERE {w.where(rule)} GROUP BY collector", w.args).fetchall()
    errors = {}
    if "error_message" in cols:
        for c, e in conn.execute(f'SELECT collector, error_message FROM "{rule.table}" WHERE {w.where(rule)} '
                                 f"AND error_message IS NOT NULL ORDER BY {t}", w.args):
            errors[c] = e                                   # the day's last
    for c, runs, failed, records, first, last in rows:
        old = conn.execute(f"SELECT runs, failures, records, first_at, last_at FROM {DAILY_LOG} "
                           "WHERE collector = ? AND day = ?", (c, day)).fetchone()
        if old:                                             # a day gone through in two goes: added up
            runs += old[0] or 0
            failed = (failed or 0) + (old[1] or 0)
            records = (records or 0) + (old[2] or 0)
            first, last = min(first, old[3] or first), max(last, old[4] or last)
            conn.execute(f"UPDATE {DAILY_LOG} SET runs = ?, failures = ?, records = ?, first_at = ?, last_at = ?, "
                         "last_error = COALESCE(?, last_error) WHERE collector = ? AND day = ?",
                         (runs, failed, records, first, last, errors.get(c), c, day))
        else:
            conn.execute(f"INSERT INTO {DAILY_LOG} (collector, day, runs, failures, records, first_at, last_at, "
                         "last_error) VALUES (?,?,?,?,?,?,?,?)", (c, day, runs, failed, records, first, last,
                                                                  errors.get(c)))
    conn.execute(f'DELETE FROM "{rule.table}" WHERE {w.where(rule)} AND rowid NOT IN (SELECT id FROM _keep_first)',
                 w.args)


def _one_day(conn, rule: Rule, cols: set, level: str, d: date) -> None:
    w = Window(conn, rule, d, d + timedelta(days=1))
    if rule.kind == AVERAGES:
        _averages(conn, rule, cols, w, 13 if level == "hourly" else 10)
    elif rule.kind == DAILY_READING:
        _daily_reading(conn, rule, cols, w)
    else:
        _daily_counts(conn, rule, cols, w, _day(d))


def _set_mark(conn, table: str, level: str, through: date) -> None:
    conn.execute(f"DELETE FROM {MARKS} WHERE tbl = ? AND level = ?", (table, level))
    conn.execute(f"INSERT INTO {MARKS} (tbl, level, through) VALUES (?, ?, ?)", (table, level, _day(through)))


def untrack(conn, tables) -> list:
    """The tables taken out of the change log, where it is kept (the hub
    sends each whole at its next sync): before a run that removes many rows,
    which the log would otherwise carry one by one."""
    from nomad.db import delta_site
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (delta_site.LOG,)).fetchone() is None:
            return []
    except sqlite3.Error:
        return []
    out = []
    for t in tables:
        conn.commit()
        delta_site.untrack(conn, t)
        out.append(t)
    return out


TRIES = 5                   # a day whose write lock others keep taking is tried this many times


def _write(conn, work) -> None:
    """``work(conn)`` as one write transaction, the lock taken first (1.63.1).
    A transaction that reads, then writes, may find another connection
    writing (a collector): SQLite then refuses at once rather than wait (it
    would wait forever on each other), "database is locked". BEGIN IMMEDIATE
    takes the write lock before reading, waiting up to the connection's
    timeout; a day still refused is tried again, a little later."""
    if conn.in_transaction:
        conn.commit()
    for attempt in range(TRIES):
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) and "busy" not in str(e) or attempt == TRIES - 1:
                raise
            time.sleep(2 * (attempt + 1))
            continue
        try:
            work(conn)
            conn.execute("COMMIT")
            return
        except BaseException:
            conn.execute("ROLLBACK")
            raise


def run(conn, s: Settings, how: str, today: date | None = None, limit: int | None = None,
        only: tuple = ()) -> list[dict]:
    """Go through what todo() says is due, a day at a time (each its own
    transaction, its mark with it: a run stopped part way goes on from there).
    ``limit``: a table with more rows due than this is left for ``nomad db
    keep --apply`` (its note says so). [{table, days, before, after, seconds,
    note}] for each table with something due."""
    today = today or date.today()
    if conn.in_transaction:
        conn.commit()
    _write(conn, lambda c: [c.execute(x) for x in _schema_parts()])
    out = []
    for td in todo(conn, s, today):
        if not td.present or not td.levels or (only and td.table not in only):
            continue
        if limit is not None and td.rows > limit:
            out.append({"table": td.table, "days": 0, "before": td.rows, "after": td.rows, "seconds": 0.0,
                        "note": f"{td.rows:,} rows due, more than keep_max_rows ({limit:,}): nomad db keep --apply"})
            continue
        cols = _cols(conn, td.table)
        t0 = time.time()
        days = 0
        if td.rule.kind == DAILY_COUNTS:
            _first_runs(conn, td.rule)
            conn.commit()
        for level, start, end in td.levels:
            d = start
            while d < end:
                def day(c, rule=td.rule, cols=cols, level=level, d=d):
                    _one_day(c, rule, cols, level, d)
                    _set_mark(c, rule.table, level, d + timedelta(days=1))
                _write(conn, day)
                days += 1
                d += timedelta(days=1)
        after = sum(_rows_between(conn, td.rule, a, b) for _, a, b in td.levels)
        took = time.time() - t0
        _write(conn, lambda c, td=td, days=days, after=after, took=took: c.execute(
            f"INSERT INTO {RUNS} (timestamp, how, tbl, days, rows_before, rows_after, seconds) VALUES (?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), how, td.table, days, td.rows, after, round(took, 1))))
        out.append({"table": td.table, "days": days, "before": td.rows, "after": after, "seconds": took,
                    "note": ""})
    if how == "by hand" and not any(not d["note"] for d in out):
        # gone through by hand, nothing due: the collector may go on daily
        _write(conn, lambda c: c.execute(
            f"INSERT INTO {RUNS} (timestamp, how, tbl, days, rows_before, rows_after, seconds) "
            "VALUES (?, ?, '-', 0, 0, 0, 0)", (datetime.now().isoformat(timespec="seconds"), how)))
    return out


def ran_by_hand(conn) -> bool:
    """Whether nomad db keep --apply has run on this database."""
    try:
        return conn.execute(f"SELECT 1 FROM {RUNS} WHERE how = 'by hand' LIMIT 1").fetchone() is not None
    except sqlite3.Error:
        return False


def last_run(conn) -> datetime | None:
    try:
        t = conn.execute(f"SELECT MAX(timestamp) FROM {RUNS}").fetchone()[0]
        return datetime.fromisoformat(t) if t else None
    except (sqlite3.Error, ValueError):
        return None


__all__ = ["RULES", "Rule", "Settings", "Todo", "ensure", "last_run", "ran_by_hand", "run", "settings", "todo",
           "untrack"]

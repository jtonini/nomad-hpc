"""Small helpers the facts share: reading a database whose tables differ from
one site and one nomad version to the next, times, and arithmetic that says
"not measured" instead of making up a number."""
from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timedelta

API = "nomad.facts/1"

# A site whose newest reading is older than this is "stale": what it last
# said is not the state now.
STALE_AFTER_HOURS = 2
# A workstation or storage server with no report for this long is not
# counted as online.
MACHINE_STALE_MINUTES = 60
MAX_DAYS = 365


def columns(conn: sqlite3.Connection, table: str) -> set:
    """The table's column names; empty when there is no such table."""
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def site_expr(cols: set, alias: str = "") -> str | None:
    """SQL naming the site of a row: the hub's source_site, a cluster's own
    name, or None for a table that says neither (one site's database)."""
    a = f"{alias}." if alias else ""
    if "source_site" in cols and "cluster" in cols:
        return f"COALESCE({a}source_site, {a}cluster)"
    if "source_site" in cols:
        return f"{a}source_site"
    if "cluster" in cols:
        return f"{a}cluster"
    return None


def site_label(site) -> str:
    """A site as answers name it; "this site" in one site's own database."""
    return "this site" if site is None else str(site)


def iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def parse_time(value) -> datetime | None:
    """A stored time as local naive datetime; None when it isn't one (Slurm
    writes "Unknown" as the end of a job still running)."""
    if value is None:
        return None
    text = str(value).strip().replace(" ", "T", 1)
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is not None:                  # collectors write local time; allow UTC too
        t = t.astimezone().replace(tzinfo=None)
    return t


def age_minutes(value, now: datetime) -> int | None:
    t = parse_time(value)
    return None if t is None else max(0, int((now - t).total_seconds() // 60))


def window(days: int, now: datetime) -> tuple[str, str, dict]:
    """(since, until, description) for jobs that ended in the last `days`.
    `until` is tomorrow: a string comparison puts "Unknown" (a running job's
    end) after any date, so the window must be closed."""
    days = max(1, min(int(days), MAX_DAYS))
    since = iso(now - timedelta(days=days))
    until = iso(now + timedelta(days=1))
    return since, until, {"days": days, "since": since, "until": iso(now)}


def pct(part, whole, digits: int = 1):
    return round(100 * part / whole, digits) if whole else None


def median(values):
    return statistics.median(values) if values else None


def quantile(values, q: float):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else None


def envelope(kind: str, now: datetime, win: dict | None = None) -> dict:
    out = {"api": API, "kind": kind, "generated_at": iso(now)}
    if win is not None:
        out["window"] = win
    return out


def open_readonly(path) -> sqlite3.Connection:
    """The database, read-only: the facts never write."""
    from pathlib import Path
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"no database at {p}")
    conn = sqlite3.connect(f"{p.as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def fetch(conn: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    """Rows as dicts, whatever the connection's row factory."""
    cur = conn.execute(sql, tuple(args))
    names = [d[0] for d in cur.description]
    return [dict(zip(names, tuple(r))) for r in cur.fetchall()]


def chunks(items: list, size: int = 500):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def num(value) -> float | None:
    """A stored number, or None when the column holds something else (a site
    that wrote 'N/A' must not take the answer down)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None

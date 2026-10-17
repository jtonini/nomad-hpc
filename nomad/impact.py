# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What changed after NØMAÐ: ``[impact]`` in nomad.toml, and the jobs before
and after it.

    [impact]
    launch = 2026-10-19            # when people started getting NØMAÐ's feedback
    baseline_from = 2025-08-24     # where "before" starts (default: a year before launch)
    events = [
      { date = 2026-10-06, what = "Console open to staff" },
      { date = 2027-01-11, what = "four nodes added to the large partition" },
    ]

``launch`` splits the jobs into before and after. ``events`` records
everything else that changes how people use the machines -- nodes added, a
Slurm limit, a workshop, a new part of NØMAÐ itself -- so a comparison names
them instead of crediting NØMAÐ with what they did.

Both periods are worked out the same way: the figures are nomad.facts.jobs'
(the Console's), month by month, from the jobs table and the measurements in
job_summary. Measurements stored before 1.13 took CPU use from Slurm's
AveCPU, which undercounts jobs running several tasks; set against 1.13's,
they would show an improvement that isn't there. ``nomad impact backfill``
reads the before period again from Slurm's accounting, the 1.13 way, as far
back as slurmdbd keeps it (``nomad import sacct --from`` brings the jobs
themselves). ``nomad impact status`` says what each period has.

Beyond the clusters (1.13.1): interactive servers, lab workstations and
shared login hosts (nomad.impact_sources), and the advice the Console showed
people and whether their next jobs followed it (nomad.impact_advice).

What the figures can say: after launch, requests came closer to use, fewer
jobs failed or waited, by this much. Not that NØMAÐ did it: the people and
the courses change from one term to the next, and the events change things
too. The report says so, and compares the same months a year apart where it
can.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

KEYS = ("launch", "baseline_from", "events", "advice_db", "exclude_users")
MIN_RUNTIME = 10            # seconds: shorter jobs are never measured (job_metrics)
BACKFILL_DAYS = 7           # one sacct call per week of the before period
DEFAULT_BEFORE = 365        # days before launch, when baseline_from isn't set


@dataclass(frozen=True)
class Event:
    when: date
    what: str


@dataclass(frozen=True)
class Impact:
    launch: date | None = None
    baseline_from: date | None = None
    events: tuple = ()
    problems: tuple = ()
    advice_db: str | None = None       # the Console's record of advice shown (1.13.1)
    # Accounts that aren't people, left out of every figure (1.13.2).
    exclude_users: frozenset = frozenset()

    @property
    def set(self) -> bool:
        return self.launch is not None


def _as_date(value) -> date | None:
    """A TOML date (2026-10-19), a datetime's day, or "2026-10-19"."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def impact_from(config: dict | None) -> Impact:
    """[impact] as nomad.toml has it; what is wrong goes in ``problems`` (for
    ``nomad config check``) and is left out, never guessed at."""
    section = (config or {}).get("impact")
    if section is None:
        return Impact()
    problems: list[str] = []
    if not isinstance(section, dict):
        return Impact(problems=("[impact] is not a table; ignored",))
    for key in section:
        if key not in KEYS:
            problems.append(f"[impact] {key}: not a setting; the settings are {', '.join(KEYS)}")

    launch = _as_date(section.get("launch"))
    if "launch" not in section:
        problems.append("[impact] launch is not set: the day people started getting "
                        "NØMAÐ's feedback (launch = 2026-10-19)")
    elif launch is None:
        problems.append("[impact] launch is not a date (launch = 2026-10-19); ignored")

    baseline = None
    if "baseline_from" in section:
        baseline = _as_date(section["baseline_from"])
        if baseline is None:
            problems.append("[impact] baseline_from is not a date; a year before launch is used")
        elif launch is not None and baseline >= launch:
            problems.append("[impact] baseline_from is not before launch; a year before "
                            "launch is used")
            baseline = None
    if baseline is None and launch is not None:
        baseline = launch - timedelta(days=DEFAULT_BEFORE)

    events = []
    raw = section.get("events", [])
    if not isinstance(raw, list):
        problems.append("[impact] events should be a list of { date = ..., what = \"...\" }; "
                        "ignored")
        raw = []
    for i, e in enumerate(raw, 1):
        when = _as_date(e.get("date")) if isinstance(e, dict) else None
        what = e.get("what") if isinstance(e, dict) else None
        if when is None or not isinstance(what, str) or not what.strip():
            problems.append(f"[impact] events: entry {i} needs a date and a what; ignored")
            continue
        events.append(Event(when, what.strip()))
    events.sort(key=lambda e: e.when)
    advice_db = section.get("advice_db")
    if advice_db is not None and not isinstance(advice_db, str):
        problems.append("[impact] advice_db should be a path; ignored")
        advice_db = None
    exclude = section.get("exclude_users", [])
    if not isinstance(exclude, list) or not all(isinstance(u, str) for u in exclude):
        problems.append("[impact] exclude_users should be a list of account names "
                        "(exclude_users = [\"svc-backup\"]); ignored")
        exclude = []
    return Impact(launch, baseline, tuple(events), tuple(problems), advice_db,
                  frozenset(u.strip() for u in exclude if u.strip()))


# -- reading the database ------------------------------------------------------------

def _cols(conn, table) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _site_col(cols: set) -> str | None:
    return "source_site" if "source_site" in cols else "cluster" if "cluster" in cols else None


def sites(conn) -> list:
    """The sites a database holds jobs for: [None] for one site's own."""
    col = _site_col(_cols(conn, "jobs"))
    if col is None:
        return [None]
    found = [r[0] for r in conn.execute(f"SELECT DISTINCT {col} FROM jobs "
                                        f"WHERE {col} IS NOT NULL ORDER BY 1")]
    return found or [None]


def _day(d: date) -> str:
    return d.isoformat() + "T00:00:00"


def coverage(conn, imp: Impact, site=None, today: date | None = None) -> dict:
    """What each period has: jobs that ended (running at least MIN_RUNTIME),
    of them measured, measured the 1.13 way, and with an eligible time; and
    the first job the database holds."""
    today = today or date.today()
    jcols, scols = _cols(conn, "jobs"), _cols(conn, "job_summary")
    col = _site_col(jcols)
    where, args = "", []
    if site is not None and col:
        where, args = f" AND j.{col} = ?", [site]
    first = conn.execute(f"SELECT MIN(j.end_time) FROM jobs j WHERE j.end_time LIKE '____-%'"
                         f"{where}", args).fetchone()[0]
    join, measured, new_way = "", "0", "0"
    if {"job_id", "avg_cpu_percent"} <= scols:
        from nomad.edu.progress import summary_join
        join = f" LEFT JOIN job_summary js ON {summary_join(conn)}"
        peak = "COALESCE(js.peak_memory_gb, 0) > 0" if "peak_memory_gb" in scols else "0"
        measured = f"(js.avg_cpu_percent IS NOT NULL OR {peak})"
        if "max_tasks" in scols:
            new_way = f"({measured} AND js.max_tasks IS NOT NULL)"
    eligible = "j.eligible_time IS NOT NULL" if "eligible_time" in jcols else "0"

    ran = "COALESCE(j.runtime_seconds, 0)" if "runtime_seconds" in jcols else str(MIN_RUNTIME)

    def period(start: date, end: date) -> dict:
        r = conn.execute(
            f"SELECT COUNT(*), SUM({measured}), SUM({new_way}), SUM({eligible}) "
            f"FROM jobs j{join} WHERE j.end_time >= ? AND j.end_time < ? "
            f"AND {ran} >= ?{where}",
            [_day(start), _day(end), MIN_RUNTIME, *args]).fetchone()
        return {"from": start.isoformat(), "to": (end - timedelta(days=1)).isoformat(),
                "jobs": r[0], "measured": r[1] or 0, "measured_113": r[2] or 0,
                "eligible": r[3] or 0}

    out = {"site": site, "first_job_end": str(first)[:10] if first else None}
    if imp.set:
        out["before"] = period(imp.baseline_from, min(imp.launch, today + timedelta(days=1)))
        if imp.launch <= today:
            out["after"] = period(imp.launch, today + timedelta(days=1))
    return out


def advice(cov: dict, imp: Impact, hub: bool) -> list[str]:
    """What to run so the before period can be compared, in words."""
    if not imp.set:
        return ["Set [impact] launch in nomad.toml: the day people started getting "
                "NØMAÐ's feedback."]
    on = f"On {cov['site']}: " if hub and cov["site"] else ""
    out = []
    first = cov.get("first_job_end")
    if first is None or first > (imp.baseline_from + timedelta(days=7)).isoformat():
        out.append(f"The jobs here start {first or 'nowhere'}, after {imp.baseline_from}; "
                   f"Slurm's accounting may still hold earlier ones. {on}"
                   f"`nomad import sacct --from {imp.baseline_from}` (a dry run; then --apply). "
                   f"If it adds none, Slurm has nothing earlier.")
    b = cov.get("before") or {}
    if (b.get("jobs") and b["eligible"] < 0.9 * b["jobs"]
            and not (first is None or first > (imp.baseline_from + timedelta(days=7)).isoformat())):
        out.append(f"{b['jobs'] - b['eligible']:,} of the {b['jobs']:,} jobs before launch have "
                   f"no eligible time, so their waits count from submission. {on}"
                   f"`nomad import sacct --from {imp.baseline_from}` (a dry run; then --apply) "
                   f"fills it in.")
    if b.get("jobs") and b["measured_113"] < 0.9 * b["jobs"]:
        out.append(f"{b['jobs'] - b['measured_113']:,} of the {b['jobs']:,} jobs before launch "
                   f"have no measurement read the 1.13 way. {on}`nomad impact backfill` "
                   f"(a dry run; then --apply) reads again what Slurm still keeps; jobs whose "
                   f"usage it has purged keep what is stored. Once it has run, what is left "
                   f"is what Slurm no longer has: running it again changes nothing.")
    return out


# -- backfill ---------------------------------------------------------------------------

def week_windows(start: date, end: date, days: int = BACKFILL_DAYS):
    a = start
    while a < end:
        b = min(a + timedelta(days=days), end)
        yield a, b
        a = b


def backfill(db_path, config: dict, start: date, end: date, apply: bool,
             progress=None) -> dict:
    """Measure the jobs that ended between start and end again from Slurm,
    the 1.13 way: those measured before 1.13 (job_summary.max_tasks unset)
    and those never measured. A dry run counts them from the database and
    asks Slurm nothing. On a site's own database only: the hub gets the
    results with the next sync.

    Where Slurm no longer keeps a job's step usage (slurmdbd purges steps
    sooner than jobs), the reading is empty and what was stored stays
    (``kept``; the collector never writes an empty reading over a stored
    one, 1.13.2: 1.13.0 and 1.13.1 did, and ``restore`` brings such values
    back from an older copy)."""
    conn = sqlite3.connect(db_path)
    try:
        if "source_site" in _cols(conn, "jobs"):
            raise ValueError("this is a hub's combined database; run the backfill on each "
                             "site, and the next sync brings the measurements here")
        imp = Impact(launch=end, baseline_from=start)
        cov = coverage(conn, imp, today=end - timedelta(days=1))["before"]
    finally:
        conn.close()
    windows = list(week_windows(start, end))
    counts = {"from": start.isoformat(), "to": (end - timedelta(days=1)).isoformat(),
              "jobs": cov["jobs"], "to_read": cov["jobs"] - cov["measured_113"],
              "windows": len(windows), "read": 0, "kept": 0, "apply": apply}
    if not apply:
        return counts
    from nomad.collectors.job_metrics import JobMetricsCollector
    from nomad.db.jobkeys import norm_time
    section = dict((config or {}).get("collectors", {}).get("job_metrics", {}) or {})
    section.setdefault("sacct_timeout", 600)        # a week of steps, all users
    col = JobMetricsCollector(section, str(db_path))
    # Only what 1.13 measured counts as done: the rest is read again.
    with col.get_db_connection() as c:
        scols = _cols(c, "job_summary")
        if "max_tasks" in scols:
            col._processed_jobs = {
                (r[0], norm_time(r[1])) for r in c.execute(
                    "SELECT js.job_id, j.submit_time FROM job_summary js "
                    "LEFT JOIN jobs j ON j.job_id = js.job_id WHERE js.max_tasks IS NOT NULL")}
    for a, b in windows:
        records = col.collect_window(datetime.combine(a, datetime.min.time()),
                                     datetime.combine(b, datetime.min.time()))
        col.store(records)
        empty = sum(1 for r in records if r.get("avg_cpu_percent") is None
                    and not (r.get("max_rss_mb") or 0) > 0)
        counts["read"] += len(records) - empty
        counts["kept"] += empty
        if progress:
            progress(a, len(records) - empty, empty)
    return counts


# -- bringing back measurements from an older copy -------------------------------------

# What the 1.13.0 and 1.13.1 backfill could write over, and its type.
MEASURE_COLUMNS = {"peak_cpu_percent": "REAL", "peak_memory_gb": "REAL",
                   "avg_cpu_percent": "REAL", "avg_memory_gb": "REAL", "avg_gpu_util": "REAL",
                   "health_score": "REAL", "feature_vector": "TEXT"}
_MEASURED = "(js.avg_cpu_percent IS NOT NULL OR COALESCE(js.peak_memory_gb, 0) > 0)"


def _measurements(src, site=None):
    """(job_id, submit_time, {column: value}) for the measured jobs of an
    older copy: an extract (table ``measurements``) or a database (a
    combined one needs ``site``)."""
    from nomad.db.jobkeys import norm_time
    tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "measurements" in tables:
        cols = [c for c in MEASURE_COLUMNS if c in _cols(src, "measurements")]
        for row in src.execute(f"SELECT job_id, submit_time, {', '.join(cols)} FROM measurements"):
            yield str(row[0]), norm_time(row[1]), dict(zip(cols, row[2:]))
        return
    scols, jcols = _cols(src, "job_summary"), _cols(src, "jobs")
    if not {"job_id", "avg_cpu_percent", "peak_memory_gb"} <= scols:
        raise ValueError("no job measurements in it")
    hub = "source_site" in scols
    if hub and not site:
        raise ValueError("it is a combined database: say which site's jobs (--site)")
    cols = [c for c in MEASURE_COLUMNS if c in scols]
    # The submit times in one pass, matched here: a hub's jobs table has an
    # index on source_site and none on job_id, and with it SQLite answered a
    # join by scanning the site's jobs for every measured job (hours for a
    # big site; 1.13.2).
    submit: dict = {}
    if {"job_id", "submit_time"} <= jcols:
        if hub and "source_site" in jcols:
            found = src.execute("SELECT job_id, submit_time FROM jobs WHERE source_site = ?",
                                (site,))
        else:
            found = src.execute("SELECT job_id, submit_time FROM jobs")
        submit = {str(j): t for j, t in found}
    where, args = ("", []) if not hub else (" AND js.source_site = ?", [site])
    for row in src.execute(f"SELECT js.job_id, {', '.join('js.' + c for c in cols)} "
                           f"FROM job_summary js WHERE {_MEASURED}{where}", args):
        job_id = str(row[0])
        yield job_id, norm_time(submit.get(job_id)), dict(zip(cols, row[1:]))


def extract(src_path, out_path, site=None) -> int:
    """The measured jobs of an older copy (one site's, from a combined
    database) into a small file for ``restore`` on that site: job number,
    submit time and the measurements, nothing else."""
    out_path = Path(out_path)
    if out_path.exists():
        raise ValueError(f"{out_path} exists")
    src = sqlite3.connect(f"file:{Path(src_path).expanduser()}?mode=ro", uri=True)
    try:
        rows = list(_measurements(src, site))
    finally:
        src.close()
    out = sqlite3.connect(out_path)
    try:
        out.execute("CREATE TABLE extract_info (site TEXT, made_at TEXT)")
        out.execute("INSERT INTO extract_info VALUES (?, ?)",
                    (site, datetime.now().isoformat(timespec="seconds")))
        out.execute("CREATE TABLE measurements (job_id TEXT PRIMARY KEY, submit_time TEXT, "
                    + ", ".join(f"{c} {t}" for c, t in MEASURE_COLUMNS.items()) + ")")
        out.executemany(
            f"INSERT OR REPLACE INTO measurements VALUES ({', '.join('?' * (2 + len(MEASURE_COLUMNS)))})",
            [(j, t, *[m.get(c) for c in MEASURE_COLUMNS]) for j, t, m in rows])
        out.commit()
    finally:
        out.close()
    return len(rows)


def restore(db_path, src_path, site=None, apply: bool = False) -> dict:
    """Bring back measurements an older copy has for jobs this site's
    database now holds unmeasured (1.13.0's and 1.13.1's backfill wrote
    "not measured" over them where Slurm had purged the steps). Only jobs
    with no measurement now; only the same job: the submit times are known
    on both sides and agree (Slurm gives numbers out again); jobs where
    either is unknown are counted as ``unsure`` and left alone. A value the
    copy doesn't have never replaces one stored. Restored rows keep
    max_tasks unset: they were measured the old way."""
    from nomad.db.jobkeys import aside_id, norm_time
    conn = sqlite3.connect(db_path, timeout=120)     # the collector may be writing
    counts = {"in_copy": 0, "restored": 0, "measured_now": 0, "other_job": 0, "unsure": 0,
              "not_here": 0, "apply": apply, "copy_site": None}
    try:
        if "source_site" in _cols(conn, "jobs"):
            raise ValueError("this is a hub's combined database: restore on the site, and the "
                             "next sync brings the measurements here")
        src = sqlite3.connect(f"file:{Path(src_path).expanduser()}?mode=ro", uri=True)
        try:
            rows = list(_measurements(src, site))
            if "site" in _cols(src, "extract_info"):
                counts["copy_site"] = src.execute("SELECT site FROM extract_info").fetchone()[0]
        finally:
            src.close()
        counts["in_copy"] = len(rows)
        here = {str(r[0]): (r[1], r[2]) for r in conn.execute(
            f"SELECT js.job_id, {_MEASURED}, j.submit_time FROM job_summary js "
            f"LEFT JOIN jobs j ON j.job_id = js.job_id")}
        todo = []
        for job_id, submit, values in rows:
            # The job, here under its number, or under NUMBER@SUBMIT when a
            # later job took the number (nomad.db.jobkeys).
            ids = [job_id] + ([aside_id(job_id, submit)] if submit and "@" not in job_id else [])
            outcome = "not_here"
            for jid in ids:
                if jid not in here:
                    continue
                measured, submit_here = here[jid]
                submit_here = norm_time(submit_here)
                if submit and submit_here and submit != submit_here:
                    outcome = "other_job"       # a later job took the number: try NUMBER@SUBMIT
                    continue
                if measured:
                    outcome = "measured_now"
                    break
                if not (submit and submit_here):
                    outcome = "unsure"          # can't tell it's the same job: left alone
                    continue
                outcome = "restored"
                todo.append((jid, values))
                break
            if outcome != "restored":
                counts[outcome] += 1
        counts["restored"] = len(todo)
        if apply and todo:
            have = _cols(conn, "job_summary")
            for job_id, values in todo:
                cols = [c for c in values if c in have and values[c] is not None]
                if not cols:
                    continue
                # A value the copy lacks never replaces one stored, and a job
                # measured since the count above is left as it is.
                conn.execute(f"UPDATE job_summary SET "
                             f"{', '.join(f'{c} = COALESCE(?, {c})' for c in cols)} "
                             f"WHERE job_id = ? AND NOT {_MEASURED.replace('js.', '')}",
                             [values[c] for c in cols] + [job_id])
            conn.commit()
    finally:
        conn.close()
    return counts


# -- the report ---------------------------------------------------------------------------

def _months(start: date, end: date):
    m = date(start.year, start.month, 1)
    while m <= end:
        n = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
        yield m, n
        m = n


def month_phase(m0: date, m1: date, launch: date) -> str:
    return "before" if m1 <= launch else "after" if m0 >= launch else "launch"


def spans(imp: Impact, today: date, data_from: str | None) -> list:
    """The periods compared, as (key, first day, day after the last): before
    launch; after it (none while launch is still to come); the after
    period's days a year earlier, where the data reaches back to them."""
    after_end = today + timedelta(days=1)
    a0, a1 = imp.launch - timedelta(days=365), after_end - timedelta(days=365)
    out = [("before", imp.baseline_from, min(imp.launch, after_end))]
    if imp.launch <= today:
        out.append(("after", imp.launch, after_end))
        if data_from and data_from <= a0.isoformat():
            out.append(("year_earlier", a0, a1))
    return out


def _figures(conn, since: date, until: date, site, ceilings, exclude=()) -> dict:
    """nomad.facts.jobs' figures for the jobs that ended in [since, until),
    and how much of their time limits finished jobs used. Not the jobs of
    the accounts in ``exclude``."""
    from nomad.facts.jobs import finished_rows, summarize
    from nomad.facts._common import num, pct
    rows = finished_rows(conn, _day(since), (datetime.combine(until, datetime.min.time())
                                             - timedelta(seconds=1)).isoformat(), site=site)
    if exclude:
        rows = [r for r in rows if r.get("user_name") not in exclude]
    s = summarize(rows, ceilings)
    if s is None:
        return {"finished": 0}
    done = [r for r in rows if str(r.get("state") or "").upper() == "COMPLETED"
            and (num(r.get("req_time")) or 0) > 0 and (num(r.get("runtime")) or 0) > 0]
    s["time_used_pct"] = pct(sum(num(r["runtime"]) for r in done),
                             sum(num(r["req_time"]) for r in done))
    # Waits count from the eligible time where there is one, from submission
    # otherwise: periods are comparable when both have it for most jobs.
    started = [r for r in rows if r.get("start_time")]
    s["eligible_pct"] = pct(sum(1 for r in started if r.get("eligible_time")), len(started))
    s["outcomes_out_of_memory"] = s["outcomes"]["out_of_memory"]
    # How much the figures rest on: the share of jobs that ran long enough to
    # be measured that were, and of those, the share measured before 1.13
    # (CPU from AveCPU, which reads jobs running several tasks low).
    could = [r for r in rows if (num(r.get("runtime")) or 0) >= MIN_RUNTIME]
    measured = [r for r in could if (num(r.get("avg_cpu")) or 0) > 0
                or (num(r.get("peak_gb")) or 0) > 0]
    s["measured_pct"] = pct(len(measured), len(could))
    s["old_way_pct"] = pct(sum(1 for r in measured if r.get("max_tasks") is None), len(measured))
    return s


def report(conn, imp: Impact, site=None, today: date | None = None) -> dict:
    """Month by month from baseline_from, and the periods side by side:
    before launch, after it, and the after period's months a year earlier.
    Counts and shares only: no names."""
    from nomad.edu.progress import memory_ceilings
    today = today or date.today()
    if not imp.set:
        return {"site": site, "set": False}
    ceilings = memory_ceilings(conn)
    jcols = _cols(conn, "jobs")
    col = _site_col(jcols)
    where, args = "", []
    if site is not None and col:
        where, args = f" AND {col} = ?", [site]
    first_seen: dict = {}
    for user, first in conn.execute(
            f"SELECT user_name, MIN(end_time) FROM jobs WHERE end_time LIKE '____-%'"
            f" AND user_name IS NOT NULL{where} GROUP BY user_name", args):
        if user not in imp.exclude_users:
            first_seen[user] = str(first)[:10]
    data_from = min(first_seen.values()) if first_seen else None

    months = []
    for m0, m1 in _months(imp.baseline_from, today):
        f = _figures(conn, m0, m1, site, ceilings, imp.exclude_users)
        phase = month_phase(m0, m1, imp.launch)
        # People new that month: not for months the data doesn't reach back
        # before (everyone would look new).
        new = None
        if data_from and data_from < m0.isoformat():
            new = sum(1 for d in first_seen.values() if m0.isoformat() <= d < m1.isoformat())
        months.append({"month": m0.strftime("%Y-%m"), "phase": phase, "new_people": new,
                       "events": [e.what for e in imp.events if m0 <= e.when < m1], **f})

    # "to" is the last day in the period.
    periods = {key: {"from": a.isoformat(), "to": (b - timedelta(days=1)).isoformat(),
                     **_figures(conn, a, b, site, ceilings, imp.exclude_users)}
               for key, a, b in spans(imp, today, data_from)}
    return {"site": site, "set": True, "launch": imp.launch.isoformat(),
            "baseline_from": imp.baseline_from.isoformat(), "data_from": data_from,
            "events": [{"date": e.when.isoformat(), "what": e.what} for e in imp.events],
            "months": months, "periods": periods}


def _p(v) -> str:
    return "—" if v is None else f"{v:.0f}%"


def _m(v) -> str:
    if v is None:
        return "—"
    return f"{v:.0f} min" if v < 120 else f"{v / 60:.1f} h"


def render(rep: dict) -> list[str]:
    """The report as text: a table of months, then the periods."""
    name = rep["site"] or "this site"
    if not rep.get("set"):
        return [f"{name}: [impact] launch is not set in nomad.toml."]
    out = [f"NØMAÐ impact, {name}: launch {rep['launch']}, before from {rep['baseline_from']}"
           + (f" (jobs from {rep['data_from']})" if rep.get("data_from") else " (no jobs)"), ""]
    head = (f"  {'month':<8} {'':<6} {'jobs':>7} {'people':>6} {'new':>4} {'measured':>8} "
            f"{'CPU used':>8} {'memory':>6} {'time':>5} {'failed':>6} {'OOM':>5} {'wait':>8}")
    out.append(head)
    for m in rep["months"]:
        cpu = (m.get("core_hours") or {}).get("used_pct")
        mem = (m.get("memory") or {}).get("used_pct")
        wait = (m.get("wait_minutes") or {}).get("median")
        out.append(f"  {m['month']:<8} {m['phase']:<6} {m.get('finished', 0):>7,} "
                   f"{m.get('people', 0) or 0:>6} "
                   f"{'—' if m['new_people'] is None else m['new_people']:>4} "
                   f"{_p(m.get('measured_pct')):>8} "
                   f"{_p(cpu):>8} {_p(mem):>6} {_p(m.get('time_used_pct')):>5} "
                   f"{_p(m.get('failed_pct')):>6} {m.get('outcomes_out_of_memory', 0) or 0:>5} "
                   f"{_m(wait):>8}")
        for what in m["events"]:
            out.append(f"  {'':<8} ↳ {what}")
    out += ["", "  measured: of the jobs that ran 10 s or more, those with a CPU or memory reading "
            "(the shares rest on them);",
            "  CPU used: of the core-hours measured jobs held; memory: peak of the memory "
            "asked for, by memory-hours;",
            "  time: of the time limits of jobs that completed; failed: failed, timed out or "
            "out of memory, of the jobs",
            "  that ran to an end; wait: median, from when a job could start (its eligible "
            "time, 1.13) or else from submission.", ""]
    if any((m.get("memory") or {}).get("allocated") is False for m in rep["months"]):
        out += [f"  memory at {name}: Slurm there doesn't allocate memory to jobs (their "
                "allocations list none), so a",
                "  job's --mem isn't held for it, and jobs can use more than they ask for: the "
                "share can pass 100%.", ""]
    labels = {"before": "Before launch", "after": "After launch",
              "year_earlier": "Same days a year earlier"}
    if "after" not in rep["periods"]:
        out.append(f"  Launch is {rep['launch']}: there is no after yet.")
    for key, p in rep["periods"].items():
        out.append(f"  {labels[key]:<26} {p['from']} to {p['to']}: "
                   f"{p.get('finished', 0):,} jobs ({_p(p.get('measured_pct'))} measured), "
                   f"CPU used {_p((p.get('core_hours') or {}).get('used_pct'))}, "
                   f"memory {_p((p.get('memory') or {}).get('used_pct'))}, "
                   f"time {_p(p.get('time_used_pct'))}, failed {_p(p.get('failed_pct'))}, "
                   f"wait {_m((p.get('wait_minutes') or {}).get('median'))}")
    b, a = rep["periods"].get("before", {}), rep["periods"].get("after", {})
    if (b.get("old_way_pct") or 0) > 20:
        out += ["", f"  {b['old_way_pct']:.0f}% of the measured jobs before launch were measured "
                "before 1.13, with CPU from AveCPU,",
                "  which reads jobs running several tasks low: set against after launch, their "
                "CPU can look like an",
                "  improvement that isn't one. Memory, time, failures and waits don't have this "
                "problem."]
    if (b.get("eligible_pct") is not None and a.get("eligible_pct") is not None
            and abs(b["eligible_pct"] - a["eligible_pct"]) > 20):
        out += ["", f"  The waits aren't comparable yet: {b['eligible_pct']:.0f}% of the jobs "
                f"before launch have an eligible time, {a['eligible_pct']:.0f}% after.",
                "  `nomad import sacct --from` on the site brings it for the earlier jobs "
                "(nomad impact status)."]
    out += ["", "  These say what changed after launch, not why: the people and courses change "
            "from term to term,",
            "  and the events listed changed things too. Compare the same months a year apart "
            "where the data allows."]
    return out

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

KEYS = ("launch", "baseline_from", "events")
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
    return Impact(launch, baseline, tuple(events), tuple(problems))


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

    def period(start: date, end: date) -> dict:
        r = conn.execute(
            f"SELECT COUNT(*), SUM({measured}), SUM({new_way}), SUM({eligible}) "
            f"FROM jobs j{join} WHERE j.end_time >= ? AND j.end_time < ? "
            f"AND COALESCE(j.runtime_seconds, 0) >= ?{where}",
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
                   f"`nomad import sacct --from {imp.baseline_from}` (a dry run; then --apply).")
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
                   f"(a dry run; then --apply) reads them again from Slurm, as far back as it "
                   f"keeps them.")
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
    results with the next sync."""
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
              "windows": len(windows), "read": 0, "apply": apply}
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
        counts["read"] += len(records)
        if progress:
            progress(a, len(records))
    return counts


# -- the report ---------------------------------------------------------------------------

def _months(start: date, end: date):
    m = date(start.year, start.month, 1)
    while m <= end:
        n = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
        yield m, n
        m = n


def _figures(conn, since: date, until: date, site, ceilings) -> dict:
    """nomad.facts.jobs' figures for the jobs that ended in [since, until),
    and how much of their time limits finished jobs used."""
    from nomad.facts.jobs import finished_rows, summarize
    from nomad.facts._common import num, pct
    rows = finished_rows(conn, _day(since), (datetime.combine(until, datetime.min.time())
                                             - timedelta(seconds=1)).isoformat(), site=site)
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
        first_seen[user] = str(first)[:10]
    data_from = min(first_seen.values()) if first_seen else None

    months = []
    for m0, m1 in _months(imp.baseline_from, today):
        f = _figures(conn, m0, m1, site, ceilings)
        phase = "before" if m1 <= imp.launch else "after" if m0 >= imp.launch else "launch"
        # People new that month: not for months the data doesn't reach back
        # before (everyone would look new).
        new = None
        if data_from and data_from < m0.isoformat():
            new = sum(1 for d in first_seen.values() if m0.isoformat() <= d < m1.isoformat())
        months.append({"month": m0.strftime("%Y-%m"), "phase": phase, "new_people": new,
                       "events": [e.what for e in imp.events if m0 <= e.when < m1], **f})

    after_end = today + timedelta(days=1)
    a0, a1 = imp.launch - timedelta(days=365), after_end - timedelta(days=365)
    spans = [("before", imp.baseline_from, min(imp.launch, after_end))]
    if imp.launch <= today:             # a launch still to come has no after yet
        spans.append(("after", imp.launch, after_end))
        if data_from and data_from <= a0.isoformat():
            spans.append(("year_earlier", a0, a1))
    # "to" is the last day in the period.
    periods = {key: {"from": a.isoformat(), "to": (b - timedelta(days=1)).isoformat(),
                     **_figures(conn, a, b, site, ceilings)} for key, a, b in spans}
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
    head = (f"  {'month':<8} {'':<6} {'jobs':>7} {'people':>6} {'new':>4} {'CPU used':>8} "
            f"{'memory':>6} {'time':>5} {'failed':>6} {'OOM':>5} {'wait':>8}")
    out.append(head)
    for m in rep["months"]:
        cpu = (m.get("core_hours") or {}).get("used_pct")
        mem = (m.get("memory") or {}).get("used_pct")
        wait = (m.get("wait_minutes") or {}).get("median")
        out.append(f"  {m['month']:<8} {m['phase']:<6} {m.get('finished', 0):>7,} "
                   f"{m.get('people', 0) or 0:>6} "
                   f"{'—' if m['new_people'] is None else m['new_people']:>4} "
                   f"{_p(cpu):>8} {_p(mem):>6} {_p(m.get('time_used_pct')):>5} "
                   f"{_p(m.get('failed_pct')):>6} {m.get('outcomes_out_of_memory', 0) or 0:>5} "
                   f"{_m(wait):>8}")
        for what in m["events"]:
            out.append(f"  {'':<8} ↳ {what}")
    out += ["", "  CPU used: of the core-hours measured jobs held; memory: peak of the memory "
            "asked for, by memory-hours;",
            "  time: of the time limits of jobs that completed; failed: failed, timed out or "
            "out of memory, of the jobs",
            "  that ran to an end; wait: median, from when a job could start (its eligible "
            "time, 1.13) or else from submission.", ""]
    labels = {"before": "Before launch", "after": "After launch",
              "year_earlier": "Same days a year earlier"}
    if "after" not in rep["periods"]:
        out.append(f"  Launch is {rep['launch']}: there is no after yet.")
    for key, p in rep["periods"].items():
        out.append(f"  {labels[key]:<26} {p['from']} to {p['to']}: "
                   f"{p.get('finished', 0):,} jobs, CPU used {_p((p.get('core_hours') or {}).get('used_pct'))}, "
                   f"memory {_p((p.get('memory') or {}).get('used_pct'))}, "
                   f"time {_p(p.get('time_used_pct'))}, failed {_p(p.get('failed_pct'))}, "
                   f"wait {_m((p.get('wait_minutes') or {}).get('median'))}")
    b, a = rep["periods"].get("before", {}), rep["periods"].get("after", {})
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

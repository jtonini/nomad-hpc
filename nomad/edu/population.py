# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Edu — everyone at once.

The lists behind the Console's Trajectory and Group Reports pages and
`nomad edu report`: every person with finished jobs in a window, and any
group of them, from one pass over the jobs.

Every job is scored by the same score_job() that explains a single job, and
periods are cut into the same weekly windows as user_trajectory(), so a
person's line in a list and their own page cannot disagree.

What counts:
  * a job counts when it finished (COMPLETED, FAILED, TIMEOUT) in the window;
  * it is *scored* only when NØMAÐ measured it (it has a job_summary row).
    Requested versus used walltime alone is not a picture of how someone
    uses the cluster, so unmeasured jobs are counted but not scored, and
    every figure says how many jobs and people it rests on.

Averages over people are medians with a quartile spread: a mean over five
people is one outlier away from describing none of them.
"""

from __future__ import annotations

import os
import re
import sqlite3
import statistics
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from nomad.edu.progress import _split_job_fields, summary_columns, summary_join
from nomad.edu.scoring import JobFingerprint, score_job

FINISHED = ("COMPLETED", "FAILED", "TIMEOUT")
DIMENSIONS = ("cpu", "memory", "time", "io", "gpu")
NEEDS_WORK = 65          # the same line JobFingerprint.needs_work draws
MIN_TREND_WINDOWS = 2    # a change needs a first and a last window with jobs
TREND_POINTS = 5         # more than this either way is a direction; less is noise


# ── Results ──────────────────────────────────────────────────────────

@dataclass
class PersonSummary:
    """One person over the window."""
    username: str
    jobs: int                        # finished jobs in the window
    scored_jobs: int                 # of those, measured and scored
    sites: list[str]
    overall: float | None            # mean of the dimension averages
    dimensions: dict[str, float]     # dimension -> average over scored jobs
    change: float | None             # overall, last weekly window minus first
    first_job: str | None
    last_job: str | None

    @property
    def weakest(self) -> str | None:
        return min(self.dimensions, key=self.dimensions.get) if self.dimensions else None

    @property
    def needs_work(self) -> list[str]:
        """Dimensions averaging below the needs-work line, worst first."""
        low = [d for d, s in self.dimensions.items() if s < NEEDS_WORK]
        return sorted(low, key=self.dimensions.get)


@dataclass
class Population:
    """Everyone with finished jobs in the window."""
    days: int
    since: str
    until: str
    people: dict[str, PersonSummary]
    sites: list[str]

    @property
    def jobs(self) -> int:
        return sum(p.jobs for p in self.people.values())

    @property
    def scored_jobs(self) -> int:
        return sum(p.scored_jobs for p in self.people.values())


@dataclass
class Spread:
    median: float
    p25: float
    p75: float
    n: int


@dataclass
class GroupReport:
    """A group's members over the window."""
    group: str
    members: int                         # everyone in the group
    people: list[PersonSummary]          # members with finished jobs, lowest overall first
    without_jobs: list[str]              # members with none in the window
    overall: Spread | None               # over members with a score
    dimensions: dict[str, Spread]        # dimension -> spread over members
    issues: list[tuple[str, int]]        # (dimension, members below the line), most first
    since: str
    until: str
    trend: dict[str, int] = field(default_factory=dict)
    # improving / steady / declining: members whose overall moved more than
    # TREND_POINTS between their first and last week with measured jobs;
    # too_few_weeks: scored members without two such weeks.

    @property
    def members_with_jobs(self) -> int:
        return len(self.people)

    @property
    def members_scored(self) -> int:
        return sum(1 for p in self.people if p.overall is not None)

    @property
    def jobs(self) -> int:
        return sum(p.jobs for p in self.people)

    @property
    def scored_jobs(self) -> int:
        return sum(p.scored_jobs for p in self.people)

    @property
    def sites(self) -> list[str]:
        return sorted({s for p in self.people for s in p.sites})


@dataclass
class GroupCard:
    """One line in a list of groups."""
    group: str
    members: int
    members_with_jobs: int
    members_scored: int
    jobs: int
    scored_jobs: int
    median_overall: float | None


# ── Scoring everyone ─────────────────────────────────────────────────

@dataclass
class _Acc:
    """Running totals for one person while the jobs stream past."""
    jobs: int = 0
    scored: int = 0
    sites: set = field(default_factory=set)
    dim_sum: dict = field(default_factory=lambda: defaultdict(float))
    dim_n: dict = field(default_factory=lambda: defaultdict(int))
    win_sum: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(float)))
    win_n: dict = field(default_factory=lambda: defaultdict(lambda: defaultdict(int)))
    first: str | None = None
    last: str | None = None


def _mean_of_dims(sums: dict, counts: dict) -> tuple[dict, float | None]:
    dims = {d: round(sums[d] / counts[d], 1) for d in sums if counts.get(d)}
    overall = round(sum(dims.values()) / len(dims), 1) if dims else None
    return dims, overall


def _compute(db_path: str, days: int, window_size: int) -> Population:
    now = datetime.now()
    start = now - timedelta(days=days)
    since = start.isoformat(timespec="seconds")
    acc: dict[str, _Acc] = defaultdict(_Acc)

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        join = summary_join(conn)
        rows = conn.execute(f"""
            SELECT j.*, js.job_id AS _summary_id, {summary_columns(conn)}
            FROM jobs j
            LEFT JOIN job_summary js ON {join}
            WHERE j.state IN ({','.join('?' * len(FINISHED))})
              AND j.end_time >= ?
        """, (*FINISHED, since))
        for r in rows:
            row = dict(r)
            user = row.get("user_name")
            if not user:
                continue
            _add(acc[user], row, start, window_size)
    finally:
        conn.close()

    people = {user: _summary(user, a) for user, a in acc.items()}
    sites = {s for p in people.values() for s in p.sites}
    return Population(days=days, since=since, until=now.isoformat(timespec="seconds"),
                      people=people, sites=sorted(sites))


def _add(a: _Acc, row: dict, start: datetime, window_size: int) -> JobFingerprint | None:
    """Count one finished job for its person; score it if it was measured.

    Returns the job's fingerprint when it was scored, else None.
    """
    a.jobs += 1
    end = row.get("end_time") or ""
    site = row.get("source_site") or row.get("cluster")
    if site:
        a.sites.add(site)
    if end:
        a.first = end if a.first is None or end < a.first else a.first
        a.last = end if a.last is None or end > a.last else a.last
    if row.get("_summary_id") is None:
        return None                              # counted, not measured
    job, summary = _split_job_fields(row)
    try:
        fp = score_job(job, summary)
    except Exception:
        return None
    fp._end_time = end
    a.scored += 1
    window = _window_index(end, start, window_size)
    for name, dim in fp.dimensions.items():
        if dim.applicable:
            a.dim_sum[name] += dim.score
            a.dim_n[name] += 1
            if window is not None:
                a.win_sum[window][name] += dim.score
                a.win_n[window][name] += 1
    return fp


def _summary(user: str, a: _Acc) -> PersonSummary:
    dims, overall = _mean_of_dims(a.dim_sum, a.dim_n)
    change = None
    windows = sorted(a.win_sum)
    if len(windows) >= MIN_TREND_WINDOWS:
        _, first = _mean_of_dims(a.win_sum[windows[0]], a.win_n[windows[0]])
        _, last = _mean_of_dims(a.win_sum[windows[-1]], a.win_n[windows[-1]])
        if first is not None and last is not None:
            change = round(last - first, 1)
    return PersonSummary(
        username=user, jobs=a.jobs, scored_jobs=a.scored,
        sites=sorted(a.sites), overall=overall, dimensions=dims,
        change=change, first_job=a.first, last_job=a.last,
    )


def score_person(username: str, rows: list[dict], days: int = 90,
                 window_size: int = 7) -> tuple[PersonSummary, list[JobFingerprint]]:
    """One person's finished jobs, by the same rules as population().

    `rows` are the person's finished jobs in the window, each joined to its
    job_summary with `_summary_id` (as progress._load_user_jobs returns them).
    Every row is counted; only measured ones are scored. Returns the summary
    and the fingerprints of the scored jobs, in row order -- so a caller
    looking at one person (nomad edu me, My Activity) gets the same overall,
    dimensions and change as that person's line in a list, without scoring
    everyone else.
    """
    start = datetime.now() - timedelta(days=days)
    a = _Acc()
    scored = []
    for row in rows:
        fp = _add(a, row, start, window_size)
        if fp is not None:
            scored.append(fp)
    return _summary(username, a), scored


def trend(change: float | None) -> str:
    """'improving', 'steady' or 'declining' -- 'too_few_weeks' with no change to read.

    A change is the overall score in the last week with measured jobs minus
    the first; more than TREND_POINTS either way is a direction.
    """
    if change is None:
        return "too_few_weeks"
    if change > TREND_POINTS:
        return "improving"
    if change < -TREND_POINTS:
        return "declining"
    return "steady"


def _window_index(end_time: str, start: datetime, window_size: int) -> int | None:
    """Which weekly window a job falls in -- the windows user_trajectory() uses."""
    try:
        end = datetime.fromisoformat(end_time.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None
    delta = (end - start).total_seconds()
    if delta < 0:
        return None
    return int(delta // (window_size * 86400))


_CACHE: dict[tuple, Population] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_SIZE = 4


def population(db_path: str, days: int = 90, window_size: int = 7) -> Population:
    """Everyone with finished jobs in the last `days` days.

    Kept until the database file changes (the hub replaces combined.db on
    every sync), so the first request after a sync pays for scoring
    (about a second for fifty thousand jobs) and the rest are free.
    """
    st = os.stat(db_path)
    key = (os.path.realpath(db_path), st.st_ino, st.st_mtime_ns, st.st_size, days, window_size)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    result = _compute(db_path, days, window_size)
    with _CACHE_LOCK:
        _CACHE[key] = result
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.pop(next(iter(_CACHE)))
    return result


# ── Groups ───────────────────────────────────────────────────────────

def research_group(name: str, pattern: str | None = None, exclude=()) -> bool:
    """Whether a group describes a research unit rather than a role.

    `exclude` drops groups everyone belongs to (people, users, student ...);
    `pattern`, when set, keeps only matching names (e.g. r"\\$$" where a
    trailing $ marks a lab).
    """
    if not name or name in set(exclude or ()):
        return False
    return bool(re.search(pattern, name)) if pattern else True


def group_members(db_path: str) -> dict[str, set[str]]:
    """group name -> usernames, across every site in the database."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        out: dict[str, set[str]] = defaultdict(set)
        for group, user in conn.execute(
                "SELECT DISTINCT group_name, username FROM group_membership"):
            if group and user:
                out[group].add(user)
        return out
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def _spread(values: list[float]) -> Spread | None:
    if not values:
        return None
    values = sorted(values)
    if len(values) >= 4:
        q = statistics.quantiles(values, n=4, method="inclusive")
        p25, p75 = q[0], q[2]
    else:
        p25, p75 = values[0], values[-1]
    return Spread(median=round(statistics.median(values), 1),
                  p25=round(p25, 1), p75=round(p75, 1), n=len(values))


def group_report(db_path: str, group_name: str, days: int = 90,
                 members: set[str] | None = None) -> GroupReport | None:
    """A group's members: who ran jobs, how they score, what most need.

    None when the group has no members in the database.
    """
    if members is None:
        members = group_members(db_path).get(group_name, set())
    if not members:
        return None
    pop = population(db_path, days)
    people = [pop.people[u] for u in members if u in pop.people]
    without = sorted(u for u in members if u not in pop.people)
    scored = [p for p in people if p.overall is not None]

    dims = {}
    for d in DIMENSIONS:
        s = _spread([p.dimensions[d] for p in scored if d in p.dimensions])
        if s:
            dims[d] = s
    issues = defaultdict(int)
    for p in scored:
        for d in p.needs_work:
            issues[d] += 1

    directions = {"improving": 0, "steady": 0, "declining": 0, "too_few_weeks": 0}
    for p in scored:
        directions[trend(p.change)] += 1

    people.sort(key=lambda p: (p.overall is None, p.overall if p.overall is not None else 0, p.username))
    return GroupReport(
        trend=directions,
        group=group_name, members=len(members), people=people,
        without_jobs=without, overall=_spread([p.overall for p in scored]),
        dimensions=dims,
        issues=sorted(issues.items(), key=lambda kv: (-kv[1], kv[0])),
        since=pop.since, until=pop.until,
    )


def group_cards(db_path: str, days: int = 90, pattern: str | None = None,
                exclude=()) -> list[GroupCard]:
    """Every research group, busiest first (by members who ran jobs)."""
    pop = population(db_path, days)
    cards = []
    for group, members in group_members(db_path).items():
        if not research_group(group, pattern, exclude):
            continue
        people = [pop.people[u] for u in members if u in pop.people]
        scored = [p.overall for p in people if p.overall is not None]
        cards.append(GroupCard(
            group=group, members=len(members), members_with_jobs=len(people),
            members_scored=len(scored), jobs=sum(p.jobs for p in people),
            scored_jobs=sum(p.scored_jobs for p in people),
            median_overall=round(statistics.median(scored), 1) if scored else None,
        ))
    cards.sort(key=lambda c: (-c.members_with_jobs, -c.jobs, c.group))
    return cards


# ── Terminal output ──────────────────────────────────────────────────

_NAMES = {"cpu": "CPU", "memory": "Memory", "time": "Time", "io": "I/O", "gpu": "GPU"}


def format_group_report(gr: GroupReport, days: int) -> str:
    """`nomad edu report <group>`: the same figures the Console shows."""
    from nomad.edu.explain import C
    lines = ["", f"  {C.BOLD}NØMAÐ Group Report{C.RESET} — {C.CYAN}{gr.group}{C.RESET}",
             f"  Last {days} days · {gr.members} members · "
             f"{gr.members_with_jobs} ran jobs · {gr.members_scored} scored · "
             f"{gr.scored_jobs} of {gr.jobs} jobs measured"
             + (f" · {', '.join(gr.sites)}" if gr.sites else ""), ""]
    if gr.overall is None:
        lines.append("  No member has a measured job in this period, so there is nothing to score.")
    else:
        o = gr.overall
        spread = f" (middle half {o.p25:.0f}–{o.p75:.0f})" if o.n >= 4 else ""
        lines.append(f"  Median overall: {o.median:.0f}/100 across {o.n} "
                     f"{'person' if o.n == 1 else 'people'}{spread}")
        t = gr.trend
        compared = t.get("improving", 0) + t.get("steady", 0) + t.get("declining", 0)
        if compared:
            lines.append(f"  Over the period: {t['improving']} of {compared} improved, "
                         f"{t['steady']} steady, {t['declining']} declined"
                         + (f" ({t['too_few_weeks']} without two measured weeks)"
                            if t.get("too_few_weeks") else ""))
        lines.append("")
        lines.append("  By dimension (median, people):")
        for d, s in gr.dimensions.items():
            lines.append(f"    {_NAMES.get(d, d):<7} {s.median:5.0f}   ({s.n})")
        if gr.issues:
            lines.append("")
            lines.append("  Most common to work on:")
            for d, n in gr.issues[:3]:
                lines.append(f"    {_NAMES.get(d, d)}: {n} of {gr.members_scored}")
        lines.append("")
        lines.append(f"  {'Member':<16}{'Jobs':>7}{'Measured':>10}{'Overall':>9}{'Change':>8}  Weakest")
        for p in gr.people:
            overall = f"{p.overall:.0f}" if p.overall is not None else "—"
            change = f"{p.change:+.0f}" if p.change is not None else "—"
            weakest = _NAMES.get(p.weakest, p.weakest or "—")
            lines.append(f"  {p.username:<16}{p.jobs:>7}{p.scored_jobs:>10}{overall:>9}{change:>8}  {weakest}")
    if gr.without_jobs:
        lines.append("")
        lines.append(f"  No jobs in this period: {len(gr.without_jobs)} "
                     f"member{'s' if len(gr.without_jobs) != 1 else ''}")
    lines.append("")
    return "\n".join(lines)

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A course's students this term, for its instructors: the class taken
together, each student as a row, and when the class works.

Courses, instructors and this term's students follow nomad.toml's
[console.courses] (``nomad.config.courses``): a course is a Unix group; its
students are its members who are not faculty and who joined this term or
used a server or a cluster since it began.

For each course:

* ``students``: listed, this term's, how many used an interactive server,
  how many have not yet, how many had a session open but never computed;
* ``interactive``: the class's hours open and computing, by week;
* ``each_student``: one row per student, busiest first, with their typical
  week (median hours per week) and its range (the 10th to 90th percentile of
  their weeks) -- what a caterpillar plot draws -- for hours computing once
  two whole weeks are measured (before that, an average week from the days
  measured, three at least), and for hours with a session open. Ranked by
  computing when there is any (a session open is often one nobody closed),
  else by time open (``ranked_by``). Rows carry no name unless ``names``
  (the institution's decision: [console.courses] names);
* ``when``: students on, on average, at each hour of each weekday;
* ``oom``: notebooks the kernel killed for lack of memory, and how many
  students that hit;
* ``jobs``: the students' cluster jobs, taken together.

Hours open come from the interactive collector's daily totals (nomad 1.11;
earlier weeks from ``nomad interactive backfill``), hours computing from
when 1.11 measured them, the hours of the week from 1.11.1. A week before
that is left out of a figure, never counted as zero. Busy is computing (2% of
a core between readings): reading or typing in a notebook is open, not busy.

With ``min_students`` (default 0), a course with fewer students this term
shows counts only. Unnamed rows are not anonymous to an instructor who knows
the class; whoever sets ``names`` and ``min_students`` should know that.
"""
from __future__ import annotations

import random
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta

from nomad.config.courses import ACTIVE
from nomad.facts._common import chunks, columns, envelope, iso, median, quantile

ANY = "(any)"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MIN_WEEKS = 2          # whole weeks of computing measured to show each student's range
MIN_DAYS = 3           # whole days measured for an average week before then


def resolve_name(raw: str, known: set) -> str | None:
    """A username as nomad stored it before 1.11 -- ps cut names longer than
    8 characters to 7 and a "+" -- matched to the one known name it can be."""
    if not raw:
        return None
    if raw in known:
        return raw
    if raw.endswith("+") and len(raw) > 1:
        hits = [k for k in known if k.startswith(raw[:-1])]
        if len(hits) == 1:
            return hits[0]
    return None


def _in(users) -> tuple[str, list]:
    users = list(users)
    return ",".join("?" * len(users)), users


def _site_clause(conn, table: str, sites) -> tuple[str, list] | None:
    """``sites`` None: every row; else rows of those sites only (a row whose
    site is not known matches none). None: skip the table -- asked for sites,
    it has no column to say (rare: `nomad sync` adds one to every table)."""
    if sites is None:
        return "", []
    if "source_site" not in columns(conn, table):
        return None
    marks, args = _in(sorted(sites))
    return f" AND source_site IN ({marks})", args


SITE_SHARE = 0.5      # a site counts for a course with at least half as many of its
                      # active students as its busiest site


class Activity:
    """Who used an interactive server or a cluster since a time: read once
    per answer, for every course in it. ``members_of`` and ``courses`` (the
    [console.courses] settings) let it say where a course's students work,
    for ``faculty``."""

    def __init__(self, conn, since: datetime, members_of=None, courses=None):
        self.conn, self.since = conn, since
        self.members_of, self.courses = members_of, courses
        self._raw: set | None = None
        self._known: set | None = None
        self._faculty: dict = {}
        self.faculty_sites: dict = {}      # group -> sites its students worked on (a hub)

    def combined(self) -> bool:
        """A hub's combined database: rows carry the site they came from."""
        return "source_site" in columns(self.conn, "group_membership")

    def known(self) -> set:
        """Every full name nomad has: the groups data and the daily totals."""
        if self._known is None:
            names: set = set()
            for sql in ("SELECT DISTINCT username FROM group_membership",
                        "SELECT DISTINCT user FROM interactive_daily"):
                try:
                    names |= {r[0] for r in self.conn.execute(sql) if r[0]}
                except sqlite3.Error:
                    pass
            self._known = names
        return self._known

    def raw(self) -> set:
        """(site, name) in the raw snapshots since ``since``, on each server
        before its daily totals began (names ps cut short matched against
        every known name, not only a course's); site None in one site's own
        database."""
        if self._raw is None:
            found: set = set()
            conn = self.conn
            cols = columns(conn, "interactive_sessions")
            if {"user", "timestamp", "server_id"} <= cols:
                site = "source_site" if "source_site" in cols else "NULL"
                daily = columns(conn, "interactive_daily")
                first: dict = {}
                if {"server_id", "day"} <= daily:
                    dsite = "source_site" if "source_site" in daily else "NULL"
                    first = {(w, srv): day for w, srv, day in conn.execute(
                        f"SELECT {dsite}, server_id, MIN(day) FROM interactive_daily "
                        "GROUP BY 1, 2")}
                known = self.known()
                for where, server in conn.execute(
                        f"SELECT DISTINCT {site}, server_id FROM interactive_sessions").fetchall():
                    upto = first.get((where, server)) or first.get((None, server)) or "9999"
                    if self.since.isoformat() >= upto:
                        continue
                    clause = "source_site IS ?" if site != "NULL" else "? IS NULL"
                    for (raw,) in conn.execute(
                            f"SELECT DISTINCT user FROM interactive_sessions WHERE {clause} "
                            "AND server_id = ? AND timestamp >= ? AND timestamp < ?",
                            (where, server, self.since.isoformat(), upto)):
                        name = resolve_name(str(raw or ""), known)
                        if name:
                            found.add((where, name))
            self._raw = found
        return self._raw

    def of(self, users: set, sites: set | None = None) -> set:
        """Of ``users``, who was active (``sites``: on those sites only, in a
        hub's combined database; an empty set matches nobody)."""
        if not users or (sites is not None and not sites):
            return set()
        conn, found = self.conn, set()
        start = self.since.date().isoformat()
        clause = _site_clause(conn, "interactive_daily", sites)
        if {"user", "day"} <= columns(conn, "interactive_daily") and clause is not None:
            extra, extra_args = clause
            for part in chunks(sorted(users)):
                marks, args = _in(part)
                found |= {r[0] for r in conn.execute(
                    f"SELECT DISTINCT user FROM interactive_daily WHERE day >= ? "
                    f"AND session_seconds > 0 AND user IN ({marks}){extra}",
                    [start] + args + extra_args)}
        found |= {n for where, n in self.raw() if sites is None or where in sites} & users
        clause = _site_clause(conn, "jobs", sites)
        if {"user_name", "submit_time"} <= columns(conn, "jobs") and clause is not None:
            extra, extra_args = clause
            for part in chunks(sorted(users)):
                marks, args = _in(part)
                found |= {r[0] for r in conn.execute(
                    f"SELECT DISTINCT user_name FROM jobs WHERE submit_time >= ? "
                    f"AND user_name IN ({marks}){extra}",
                    [self.since.isoformat()] + args + extra_args)}
        return found & users

    def sites_of(self, users: set) -> set:
        """Where ``users`` (a course's students) worked: the sites with at
        least SITE_SHARE as many of them active as the busiest site -- one
        earlier term's student's research job elsewhere doesn't make that
        cluster the course's. A hub's combined database; rows whose site is
        not known say nothing."""
        conn, on = self.conn, defaultdict(set)
        if not users:
            return set()
        start = self.since.date().isoformat()
        for table, user, when, extra, first in (
                ("interactive_daily", "user", "day", " AND session_seconds > 0", start),
                ("jobs", "user_name", "submit_time", "", self.since.isoformat())):
            if not {user, when, "source_site"} <= columns(conn, table):
                continue
            for part in chunks(sorted(users)):
                marks, args = _in(part)
                for where, who in conn.execute(
                        f"SELECT DISTINCT source_site, {user} FROM {table} WHERE {when} >= ?{extra} "
                        f"AND {user} IN ({marks})", [first] + args):
                    if where:
                        on[where].add(who)
        for where, n in self.raw():
            if where and n in users:
                on[where].add(n)
        if not on:
            return set()
        busiest = max(len(v) for v in on.values())
        return {w for w, v in on.items() if len(v) >= SITE_SHARE * busiest}

    def _students(self, group: str) -> set:
        """The group's members who could be its students (not faculty, not
        staff, not added or excluded): where they work is where the course is."""
        from nomad.config.courses import Course
        if self.members_of is None or self.courses is None:
            return set()
        c = self.courses
        listed = c.listed.get(group, Course())
        lower = lambda names: {n.lower() for n in names if n}           # noqa: E731
        return (lower(self.members_of(group)) - lower(self.members_of(c.faculty_group))
                - set(c.not_teaching) - set(listed.instructors) - set(listed.exclude))

    def faculty(self, group: str, candidates: set) -> dict:
        """Of a course group's faculty members, who teaches it this term by
        what they did: {netid: JOINED} for those who joined the group this
        term, when any did; else {netid: ACTIVE} for those active this term
        where the course's students work (in a hub's combined database, the
        sites they used: research on another cluster says nothing about a
        course). For ``nomad.config.courses.teaching``."""
        from nomad.config.courses import ACTIVE, JOINED, first_seen, joined_this_term
        key = (group, frozenset(candidates))
        if key in self._faculty:
            return dict(self._faculty[key])
        joined = first_seen(self.conn, group)
        out = {u: JOINED for u in candidates if joined_this_term(joined.get(u), self.since)}
        if not out:
            sites = None
            if self.combined():
                sites = self.sites_of(self._students(group))
                self.faculty_sites[group] = sorted(sites)
            out = dict.fromkeys(self.of(set(candidates), sites), ACTIVE)
        self._faculty[key] = out
        return dict(out)


def active_users(conn, users: set, since: datetime) -> set:
    """Of ``users``, who used an interactive server or a cluster since
    ``since``: the daily totals, the raw snapshots before them, and jobs."""
    return Activity(conn, since).of(users)


def _weeks(since: date, today: date) -> list[date]:
    """The Mondays of the weeks from ``since``'s to today's."""
    first = since - timedelta(days=since.weekday())
    out, w = [], first
    while w <= today:
        out.append(w)
        w += timedelta(days=7)
    return out


def _spread(values: list) -> dict | None:
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"typical": round(median(values), 2), "low": round(quantile(values, 0.1), 2),
            "high": round(quantile(values, 0.9), 2), "weeks": len(values)}


def _daily(conn, students: set, since: date) -> tuple[list, str | None, str | None]:
    """Each student's day rows (open, busy, measured, cpu seconds, peak MB),
    when the daily totals start, and when computing started being measured."""
    cols = columns(conn, "interactive_daily")
    if not {"day", "user", "session_type", "session_seconds"} <= cols:
        return [], None, None
    rows = []
    for part in chunks(sorted(students)):
        marks, args = _in(part)
        rows += conn.execute(
            "SELECT user, day, SUM(session_seconds), SUM(busy_seconds), SUM(cpu_measured_seconds), "
            "SUM(cpu_seconds), MAX(peak_mem_mb) FROM interactive_daily WHERE session_type = ? "
            f"AND day >= ? AND user IN ({marks}) GROUP BY user, day",
            [ANY, since.isoformat()] + args).fetchall()
    row = conn.execute("SELECT MIN(day) FROM interactive_daily").fetchone()
    covered = row[0] if row else None
    row = conn.execute("SELECT MIN(day) FROM interactive_daily WHERE cpu_measured_seconds > 0"
                       ).fetchone()
    measured = row[0] if row else None
    return rows, covered, measured


def _hourly(conn, students: set, since: datetime, now: datetime) -> dict | None:
    """Students on, and computing, on average at each hour of each weekday,
    over the days the hours were recorded."""
    cols = columns(conn, "interactive_person_hourly")
    if not {"hour", "user", "open_seconds"} <= cols:
        return None
    row = conn.execute("SELECT MIN(hour) FROM interactive_person_hourly").fetchone()
    if not row or not row[0]:
        return None
    try:
        first = datetime.strptime(row[0], "%Y-%m-%dT%H")
    except ValueError:
        return None
    start = max(first, since)
    row = conn.execute("SELECT MIN(hour) FROM interactive_person_hourly WHERE measured_seconds > 0"
                       ).fetchone()
    measured_from = None
    if row and row[0]:
        try:
            measured_from = max(datetime.strptime(row[0], "%Y-%m-%dT%H"), since)
        except ValueError:
            pass
    on = [[0.0] * 24 for _ in range(7)]
    busy = [[0.0] * 24 for _ in range(7)]
    for part in chunks(sorted(students)):
        marks, args = _in(part)
        for hour, open_s, busy_s in conn.execute(
                f"SELECT hour, SUM(open_seconds), SUM(busy_seconds) FROM interactive_person_hourly "
                f"WHERE hour >= ? AND hour < ? AND user IN ({marks}) GROUP BY hour",
                [start.strftime("%Y-%m-%dT%H"), now.strftime("%Y-%m-%dT%H")] + args):
            try:
                t = datetime.strptime(hour, "%Y-%m-%dT%H")
            except (TypeError, ValueError):
                continue
            on[t.weekday()][t.hour] += (open_s or 0) / 3600
            if measured_from is not None and t >= measured_from:
                busy[t.weekday()][t.hour] += (busy_s or 0) / 3600

    def times(frm):
        # How many times each hour of each weekday came round, whole, since
        # frm: today's later hours, and the first day's earlier, haven't.
        n = [[0] * 24 for _ in range(7)]
        t = frm.replace(minute=0, second=0, microsecond=0)
        if t < frm:
            t += timedelta(hours=1)
        while t + timedelta(hours=1) <= now:
            n[t.weekday()][t.hour] += 1
            t += timedelta(hours=1)
        return n

    n_on = times(start)
    n_busy = times(measured_from) if measured_from else None
    return {
        "since": iso(start), "busy_since": iso(measured_from) if measured_from else None,
        "weekdays": list(WEEKDAYS),
        "students_on": [[round(on[d][h] / n_on[d][h], 2) if n_on[d][h] else None
                         for h in range(24)] for d in range(7)],
        "students_computing": ([[round(busy[d][h] / n_busy[d][h], 2) if n_busy[d][h] else None
                                 for h in range(24)] for d in range(7)] if n_busy else None),
    }


def _oom(conn, students: set, since: datetime) -> dict | None:
    cols = columns(conn, "oom_kills")
    if not {"timestamp", "username"} <= cols:
        return None
    per: dict = defaultdict(int)
    for part in chunks(sorted(students)):
        marks, args = _in(part)
        for user, n in conn.execute(
                f"SELECT username, COUNT(*) FROM oom_kills WHERE timestamp >= ? "
                f"AND username IN ({marks}) GROUP BY username", [since.isoformat()] + args):
            per[user] += n
    return {"kills": sum(per.values()), "students": len(per), "per_student": dict(per)}


def _interactive(conn, students: set, since: date, today: date, oom_per: dict) -> dict:
    rows, covered, measured = _daily(conn, students, since)
    weeks = _weeks(since, today)
    complete = [w for w in weeks if w + timedelta(days=7) <= today]
    cov = date.fromisoformat(covered) if covered else None
    meas = date.fromisoformat(measured) if measured else None
    # A week counts for a figure when it was recorded from its first day
    # (the term's first week: from the term's first day).
    def recorded(w, frm):
        return frm is not None and max(w, since) >= frm
    open_weeks = [w for w in complete if recorded(w, cov)]
    busy_weeks = [w for w in complete if recorded(w, meas)]
    per: dict = defaultdict(lambda: {"open": defaultdict(float), "busy": defaultdict(float),
                                     "busy_days": 0.0, "cpu": 0.0, "peak": None, "measured": 0.0})
    # Whole days with computing measured (today is not over): an average
    # week from them, until there are whole weeks to spread.
    first_day = max(meas, since) if meas is not None else None
    days_measured = (today - first_day).days if first_day is not None else 0
    class_week = {w: {"open": 0.0, "busy": 0.0, "students": set()} for w in weeks}
    for user, day, open_s, busy_s, measured_s, cpu_s, peak in rows:
        d = date.fromisoformat(day)
        w = d - timedelta(days=d.weekday())
        p = per[user]
        p["open"][w] += (open_s or 0) / 3600
        p["busy"][w] += (busy_s or 0) / 3600
        if first_day is not None and first_day <= d < today:
            p["busy_days"] += (busy_s or 0) / 3600
        p["cpu"] += cpu_s or 0
        p["measured"] += measured_s or 0
        if peak is not None:
            p["peak"] = peak if p["peak"] is None else max(p["peak"], peak)
        if w in class_week:
            class_week[w]["open"] += (open_s or 0) / 3600
            class_week[w]["busy"] += (busy_s or 0) / 3600
            if (open_s or 0) > 0:
                class_week[w]["students"].add(user)
    each = []
    for user in students:
        p = per.get(user)
        open_vals = [round(p["open"].get(w, 0.0), 2) if p else 0.0 for w in open_weeks]
        busy_vals = [round(p["busy"].get(w, 0.0), 2) if p else 0.0 for w in busy_weeks]
        computing = _spread(busy_vals) if len(busy_weeks) >= MIN_WEEKS else None
        if computing is None and days_measured >= MIN_DAYS:
            # Not yet two whole weeks: the average week over the days measured.
            week = round((p["busy_days"] if p else 0.0) / days_measured * 7, 2)
            computing = {"typical": week, "low": None, "high": None, "weeks": None,
                         "days": days_measured, "estimated": True}
        each.append({
            "user": user,
            "hours_open": round(sum(p["open"].values()), 1) if p else 0.0,
            "hours_computing": round(sum(p["busy"].values()), 1) if p else 0.0,
            "open_per_week": _spread(open_vals),
            "computing_per_week": computing,
            "cpu_hours": round(p["cpu"] / 3600, 1) if p else 0.0,
            "peak_memory_gb": round(p["peak"] / 1024, 1) if p and p["peak"] is not None else None,
            "oom_kills": oom_per.get(user, 0),
            "weeks_active": sum(1 for w in open_weeks if p and p["open"].get(w, 0) > 0),
        })
    # Ranked by computing once it has been measured long enough (a session
    # open is often one nobody closed); else by time open. Busiest first;
    # ties in no order a name could give away.
    by = "computing" if any(r["computing_per_week"] for r in each) else "open"
    key = "computing_per_week" if by == "computing" else "open_per_week"
    random.shuffle(each)
    each.sort(key=lambda r: (-(r[key] or {}).get("typical", 0), -r["hours_computing"],
                             -r["hours_open"]))
    used = {r["user"] for r in each if r["hours_open"] > 0}
    computed = {u for u in students if per.get(u) and per[u]["busy"] and sum(per[u]["busy"].values()) > 0}
    measured_open = {u for u in used if per[u]["measured"] > 0}
    return {
        "recorded_since": covered, "computing_measured_since": measured,
        "used": len(used), "not_yet": len(students - used),
        # open while it was measured, never computing: possibly stuck
        "open_never_computing": len(measured_open - computed),
        "hours_open": round(sum(r["hours_open"] for r in each), 1),
        "hours_computing": round(sum(r["hours_computing"] for r in each), 1)
        if meas is not None else None,
        "weeks": [{"week": w.isoformat(), "complete": w in complete,
                   "students_on": len(class_week[w]["students"]) if recorded(w, cov) else None,
                   "hours_open": round(class_week[w]["open"], 1) if recorded(w, cov) else None,
                   "hours_computing": round(class_week[w]["busy"], 1) if recorded(w, meas) else None}
                  for w in weeks],
        "weeks_counted": {"open": len(open_weeks), "computing": len(busy_weeks)},
        "days_computing_measured": days_measured,
        "ranked_by": by,
        "each_student": each,
    }


def _no_instructor(who: dict) -> str:
    if who["undecided"]:
        return (f"{len(who['faculty'])} faculty members in the group, none of them active this "
                "term: nobody teaches it until one is added (nomad course add)")
    return "no faculty member in the group and nobody added (nomad course add)"


def courses(conn: sqlite3.Connection | None, config: dict | None, netid: str | None = None,
            groups: list | None = None, now: datetime | None = None,
            names: bool | None = None) -> dict:
    """The courses ``netid`` teaches (or, for staff, the ``groups`` named;
    neither: every course somebody teaches, the rest in ``not_shown``), this
    term. ``names``: rows by name (None: as [console.courses] names says)."""
    from nomad.config.access import members_lookup
    from nomad.config.courses import course_groups, courses_from, courses_of, first_seen, group_names
    from nomad.facts import jobs as jobfacts
    from nomad.edu.progress import memory_ceilings
    now = now or datetime.now()
    c = courses_from(config or {})
    start = c.start(now.date())
    since = datetime.combine(start, datetime.min.time())
    win = {"since": iso(since), "until": iso(now), "term_start": start.isoformat(),
           "term_start_from": "nomad.toml" if c.term_start else "the calendar"}
    out = envelope("course", now, win)
    out["courses"] = []
    show_names = c.names if names is None else names
    if conn is None or not c.enabled:
        out["notes"] = ["Courses are off: nomad.toml has no [console.courses]."
                        if not c.enabled else "No database."]
        return out
    exists, members_of = members_lookup(conn)
    known = group_names(conn)
    activity = Activity(conn, since, members_of, c)
    every = groups is None and not netid
    if groups is not None:
        # Only courses: any group's members are not a class to show.
        chosen = [g for g in groups if c.is_course(g)]
        others = [g for g in groups if not c.is_course(g)]
        if others:
            out.setdefault("not_courses", others)
    elif netid:
        chosen = courses_of(c, netid, known, members_of, activity.faculty)
    else:
        chosen = course_groups(c, known)
    ceilings = memory_ceilings(conn)
    for g in chosen:
        from nomad.config.courses import roster
        people = roster(c, g, members_of, activity.of, first_seen(conn, g), since,
                        activity.faculty)
        who = people["teaching"]
        shown = c.listed[g].name if g in c.listed and c.listed[g].name else g
        if every and not people["instructors"]:
            # Every course: one nobody teaches this term is shown to no one.
            out.setdefault("not_shown", []).append(
                {"group": g, "name": shown, "why": _no_instructor(who),
                 "students_this_term": len(people["this_term"])})
            continue
        students = people["this_term"]
        entry = {"group": g, "name": shown,
                 "instructors": sorted(people["instructors"]),
                 # added, only (faculty member), joined, active (see WHY)
                 "instructors_why": {n: who["why"][n] for n in sorted(who["why"])},
                 "faculty_not_active": len(who["not_active"]),
                 "students": {"listed": len(people["listed"]), "this_term": len(students),
                              "left_out": len(people["left_out"]),
                              # joined in an earlier term, as nomad saw it
                              "left_out_earlier": len(people["left_out_earlier"]),
                              # joined before nomad recorded join dates, not active
                              # this term: earlier students, or some not started yet
                              "left_out_unknown": len(people["left_out_unknown"]),
                              "new_this_term": len(people["new"])}}
        if not people["instructors"]:
            entry["no_instructor"] = _no_instructor(who)
        if ACTIVE in who["why"].values() and g in activity.faculty_sites:
            entry["active_on"] = activity.faculty_sites[g]       # where its students work
        if not exists(g) and g not in c.listed:
            entry["students"]["note"] = "no members in the groups data"
        if len(students) < c.min_students:
            entry["suppressed"] = (f"fewer than {c.min_students} students this term "
                                   "([console.courses] min_students)")
            out["courses"].append(entry)
            continue
        oom = _oom(conn, students, since)
        inter = _interactive(conn, students, start, now.date(), (oom or {}).get("per_student", {}))
        entry["students"].update({"used_a_server": inter.pop("used"),
                                  "not_yet": inter.pop("not_yet"),
                                  "open_never_computing": inter.pop("open_never_computing")})
        each = inter.pop("each_student")
        for i, r in enumerate(each, 1):
            if not show_names:
                r["user"] = f"student {i}"
        entry["interactive"] = inter
        entry["each_student"] = each
        entry["when"] = _hourly(conn, students, since, now)
        if oom is not None:
            entry["oom"] = {"kills": oom["kills"], "students": oom["students"]}
        rows = jobfacts.finished_rows(conn, iso(since), iso(now + timedelta(days=1)),
                                      users=sorted(students))
        ran = {r.get("user_name") for r in rows or () if r.get("user_name")}
        entry["jobs"] = {"students_with_jobs": len(ran), **(jobfacts.summarize(rows, ceilings) or {})}
        out["courses"].append(entry)
    out["names"] = bool(show_names)
    out["notes"] = [
        "This term's students: members who are not faculty and joined this term or used a "
        "server or a cluster since it began.",
        "Hours open: a session open, whether or not anyone was at it; computing: at least 2% "
        "of a core between readings five minutes apart.",
        "A week before a figure was recorded is left out of it, not counted as zero.",
        "Rows are unnamed unless the site allows names; with few students they are not "
        "anonymous to someone who knows the class.",
    ]
    return out

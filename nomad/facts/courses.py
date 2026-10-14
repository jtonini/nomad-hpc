# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A course's students this term, for its instructors: the class taken
together, each student as a row, and when the class works.

Courses, instructors and this term's students follow nomad.toml's
[console.courses] (``nomad.config.courses``): a course is a Unix group, an
environment (a conda environment, a Jupyter kernel) or both; its students
are its people who are not faculty and who joined its group this term or
used a server or a cluster since it began -- or, when students used its
environment this term, who joined or used the environment (``CourseIndex``).

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

    def __init__(self, conn, since: datetime, members_of=None, courses=None, env_users=None):
        self.conn, self.since = conn, since
        self.members_of, self.courses = members_of, courses
        self.env_users = env_users          # course -> who used its environments this term
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
        """The course's people who could be its students (not faculty, not
        staff, not added or excluded): where they work is where the course is."""
        if self.members_of is None or self.courses is None:
            return set()
        c = self.courses
        added, out = c.decisions(group, self.since.date().isoformat())
        lower = lambda names: {n.lower() for n in names if n}           # noqa: E731
        return (lower(self.members_of(group)) - lower(self.members_of(c.faculty_group))
                - set(c.not_teaching) - added - out)

    def faculty(self, group: str, candidates: set) -> dict:
        """Of a course's faculty members, who teaches it this term by what
        they did: {netid: JOINED} for those who joined its group this term,
        when any did; else {netid: ENVIRONMENT} for those who used its
        environment this term (``env_users``), when any did; else {netid:
        ACTIVE} for those active this term where the course's students work
        (in a hub's combined database, the sites they used: research on
        another cluster says nothing about a course). For
        ``nomad.config.courses.teaching``."""
        from nomad.config.courses import (ACTIVE, ENVIRONMENT, JOINED, first_seen,
                                          joined_this_term)
        key = (group, frozenset(candidates))
        if key in self._faculty:
            return dict(self._faculty[key])
        joined = first_seen(self.conn, group)
        out = {u: JOINED for u in candidates if joined_this_term(joined.get(u), self.since)}
        if not out and self.env_users is not None:
            used = self.env_users(group)
            out = {u: ENVIRONMENT for u in candidates if u in used}
        if not out:
            sites = None
            if self.combined():
                sites = self.sites_of(self._students(group))
                self.faculty_sites[group] = sorted(sites)
            out = dict.fromkeys(self.of(set(candidates), sites), ACTIVE)
        self._faculty[key] = out
        return dict(out)


MIN_ENV_DAYS = 2      # days with a session in an environment, this term, to be one of its
                      # users: a kernel every launcher lists is one wrong click away


def environment_people(conn, since: date, min_days: int = MIN_ENV_DAYS,
                       owners: set | None = None) -> dict:
    """{environment: people who had a session in it on at least ``min_days``
    days since ``since``} (the interactive collector's per-environment days,
    nomad 1.12.5), names in lower case. A session from a person's own
    environment (``own``: they own it, or keep it in their home) counts only
    for ``owners`` (the faculty group), and only when others used that same
    environment (``path``, on that site: servers may share a file system)
    this term: an instructor who built their course's. Anyone can name one
    of their own like a course."""
    cols = columns(conn, "interactive_env_daily")
    if not {"environment", "user", "day"} <= cols:
        return {}
    own = "own" if "own" in cols else "0"
    path = "path" if "path" in cols else "''"
    site = "source_site" if "source_site" in cols else "''"
    owners = owners or set()
    rows = [(env, str(user).lower(), day, mine, (where, place))
            for env, user, day, mine, where, server, place in conn.execute(
                f"SELECT DISTINCT environment, user, day, {own}, {site}, server_id, {path} "
                "FROM interactive_env_daily WHERE day >= ? AND (sessions > 0 OR "
                "session_seconds > 0)", (since.isoformat(),))
            if env and user]
    others: dict = defaultdict(set)          # where -> who used it, not as their own
    for env, user, day, mine, where in rows:
        if not mine:
            others[where].add(user)
    days: dict = defaultdict(set)
    for env, user, day, mine, where in rows:
        if mine and (user not in owners or not where[1] or not others[where] - {user}):
            continue
        days[(env, user)].add(day)
    out: dict = defaultdict(set)
    for (env, user), seen in days.items():
        if len(seen) >= min_days:
            out[env].add(user)
    return dict(out)


class CourseIndex:
    """Every course this term and its people, read once per answer: the
    [console.courses] settings, the groups data, who used which environment,
    and what faculty members said (``answers``, {course: {netid: bool}}).

    A course's ``members`` -- whom it is asked about and chooses its
    instructors among -- are its group's members when its group is a
    course, and whoever used the environment named for it this term; its
    ``people`` -- its students' pool -- also whoever used the other
    environments listed under it (``nomad.config.courses.
    course_environments``): an environment two courses share says who is in
    a class, not who teaches it. Names in lower case."""

    def __init__(self, conn, config: dict | None, now: datetime | None = None,
                 answers: dict | None = None):
        from nomad.config.access import members_lookup
        from nomad.config.courses import course_keys, courses_from, group_names
        self.conn = conn
        self.c = courses_from(config or {})
        self.now = now or datetime.now()
        self.start = self.c.start(self.now.date())
        self.term = self.start.isoformat()
        self.since = datetime.combine(self.start, datetime.min.time())
        self.answers = answers or {}
        self.exists, self._members_of = members_lookup(conn)
        self.groups = group_names(conn)
        self._groupset = set(self.groups)
        self._faculty: set | None = None
        self.env_people = environment_people(conn, self.start, owners=self.faculty())
        self.keys = course_keys(self.c, self.groups, self.env_people, self.term)
        self._keyset = set(self.keys)
        self._envs: dict = {}
        self._rosters: dict = {}
        self._by_env: dict = {}
        self._joined: dict = {}
        self.activity = Activity(conn, self.since, self.people, self.c, self.env_users)

    def envs_of(self, key: str) -> set:
        """The environments whose users this term are among ``key``'s people."""
        if key not in self._envs:
            from nomad.config.courses import course_environments
            self._envs[key] = course_environments(self.c, key, self.env_people, self.term,
                                                  self._groupset)
        return self._envs[key]

    def env_users(self, key: str) -> set:
        """Who used ``key``'s environments this term."""
        people: set = set()
        for e in self.envs_of(key):
            people |= self.env_people.get(e, set())
        return people

    def group_members(self, key: str) -> set:
        """``key``'s group's members, when that group is a course."""
        if not self.c.is_course(key, term=self.term):
            return set()
        return {n.lower() for n in self._members_of(key) if n}

    def members(self, name: str) -> set:
        """A course's members (see the class); any other group's members.
        When its class is by its environment (``by_environment``: a reused
        group), its group's faculty members count only when they joined it
        this term or used the environment; otherwise (its group is this
        term's class) faculty members outside its group who used its
        environment don't count: a colleague who borrowed it teaches nothing."""
        if name not in self._keyset:
            return {n.lower() for n in self._members_of(name) if n}
        own = self.env_people.get(name, set()) if name in self.envs_of(name) else set()
        group = self.group_members(name)
        if self.by_environment(name):
            used = self.env_users(name)
            group = {u for u in group if u not in self.faculty() or u in used
                     or u in self.joined(name)}
        if not self.outside_students(name):
            own = own - (self.faculty() - group)
        return group | own

    def outside_students(self, key: str) -> set:
        """Who used the environment named for ``key`` this term outside its
        group, besides faculty, staff and those excluded: without them, a
        faculty member outside its group who used that environment borrowed
        it. (Not its other environments: another course may share those.)"""
        c = self.c
        if key not in self.envs_of(key):
            return set()
        excluded = set(c.not_teaching) | c.decisions(key, self.term)[1]
        return (self.env_people.get(key, set()) - self.faculty() - excluded
                - self.group_members(key))

    def people(self, name: str) -> set:
        """A course's people (see the class); any other group's members."""
        if name not in self._keyset:
            return self.members(name)
        return self.members(name) | self.env_users(name)

    def label(self, key: str) -> str:
        c = self.c.listed.get(key)
        return c.name if c and c.name else key

    def joined(self, key: str) -> set:
        """Who joined ``key``'s group this term (nomad saw them join)."""
        if key not in self._joined:
            from nomad.config.courses import first_seen, joined_this_term
            self._joined[key] = {u for u, when in first_seen(self.conn, key).items()
                                 if joined_this_term(when, self.since)}
        return self._joined[key]

    def by_environment(self, key: str) -> set:
        """When ``key``'s environment says who is in the class this term (a
        reused group, this term's class elsewhere): its users besides
        faculty, staff and those excluded -- when more of them are outside
        its group than its group has students who joined or were active this
        term; or, early in a term, when some are outside it, a faculty member
        outside it used the environment, none of its own did (or joined it),
        and fewer than half its active students used it. Else nothing: the
        group says who is in the class, and its students who never use the
        environment count too (one outsider in its kernel changes nothing)."""
        if key not in self._by_env:
            c = self.c
            excluded = set(c.not_teaching) | c.decisions(key, self.term)[1]
            fac = self.faculty() - excluded
            users = self.env_users(key)
            used = users - self.faculty() - excluded
            out: set = set()
            if used:
                group = self.group_members(key)
                students = group - self.faculty() - excluded
                present = self.activity.of(students) | (self.joined(key) & students)
                outside = used - group
                if len(outside) > len(present):
                    out = used
                elif (outside and (users & fac) - group
                      and not (group & fac & (users | self.joined(key)))
                      and len(present & users) * 2 < len(present)):
                    out = used
            self._by_env[key] = out
        return self._by_env[key]

    def roster(self, key: str) -> dict:
        """``nomad.config.courses.roster`` for ``key``. A course whose class
        is by its environment (``by_environment``) counts as this term's
        students those who joined its group this term or used the
        environment: a reused group keeps earlier terms' students, and their
        work this term elsewhere is another course's."""
        from nomad.config.courses import ENVIRONMENT, ONLY, first_seen, roster
        if key not in self._rosters:
            used = self.by_environment(key)
            active = self.activity.of
            if used:
                def active(users, used=used):
                    return set(users) & used
            r = roster(self.c, key, self.members, active, first_seen(self.conn, key), self.since,
                       self.activity.faculty, self.answers.get(key), people_of=self.people)
            if used:
                # Its group's faculty members who neither joined it nor used
                # the environment were left out (members): say so, and say
                # why the one left teaches it.
                t = r["teaching"]
                gone = (self.c.decisions(key, self.term)[1] | set(self.c.not_teaching)
                        | t["said_no"])
                stale = (self.group_members(key) & self.faculty()) - self.members(key) - gone
                t["left_out"] = t["left_out"] | (stale - r["instructors"])
                users = self.env_users(key)
                for n, why in list(t["why"].items()):
                    if why == ONLY and n in users:
                        t["why"][n] = ENVIRONMENT
            self._rosters[key] = r
        return self._rosters[key]

    def faculty(self) -> set:
        """The faculty group's members (lower case)."""
        if self._faculty is None:
            self._faculty = {n.lower() for n in self._members_of(self.c.faculty_group) if n}
        return self._faculty

    def courses_of(self, netid: str) -> list[str]:
        """The courses ``netid`` teaches this term."""
        from nomad.config.courses import instructors
        n = (netid or "").strip().lower()
        if not n:
            return []
        out = []
        for k in self.keys:
            # Only one of its people, or someone added, can teach it.
            if n not in self.c.decisions(k, self.term)[0] and n not in self.members(k):
                continue
            if n in instructors(self.c, k, self.members, self.activity.faculty,
                                self.answers.get(k), self.term):
                out.append(k)
        return out


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


def questions(conn: sqlite3.Connection, config: dict | None, netid: str,
              answers: dict | None = None, now: datetime | None = None) -> list[dict]:
    """What to ask ``netid`` this term: the courses they are a faculty
    member of -- in its group, or a user of its environment -- (not
    excluded, not staff, not added by name: those are decided) that have
    students this term or that they already answered for. Each: group (the
    course), name, students this term, their ``answer`` (True/False/None),
    and whether nomad takes them as teaching it now (``teaches``, ``why``)."""
    from nomad.config.courses import candidates
    n = (netid or "").strip().lower()
    if conn is None or not n:
        return []
    idx = CourseIndex(conn, config, now, answers)
    if not idx.c.enabled:
        return []
    out = []
    for k in idx.keys:
        if n not in candidates(idx.c, k, idx.members, idx.term) or \
                n in idx.c.decisions(k, idx.term)[0]:
            continue
        said = idx.answers.get(k, {})
        people = idx.roster(k)
        if not people["this_term"] and n not in said:
            continue
        out.append({"group": k, "name": idx.label(k), "term": idx.term,
                    "students_this_term": len(people["this_term"]),
                    "answer": said.get(n), "teaches": n in people["instructors"],
                    "why": people["teaching"]["why"].get(n)})
    return out


def teaches(conn: sqlite3.Connection | None, config: dict | None, netid: str,
            answers: dict | None = None, now: datetime | None = None) -> list[str]:
    """The courses ``netid`` teaches this term, as ``courses`` decides, without
    their figures (a menu, a sidebar). ``answers``: as ``courses``'."""
    from nomad.config.courses import courses_from
    if conn is None or not courses_from(config or {}).enabled:
        return []
    return CourseIndex(conn, config, now, answers).courses_of(netid)


def every_course(conn: sqlite3.Connection | None, config: dict | None,
                 now: datetime | None = None) -> list[dict]:
    """Every course this term, whoever teaches it: [{"group", "name"}] (the
    course's key -- a group, an environment -- and how it is shown), for
    staff to choose from. Course names can say who teaches them: for staff."""
    from nomad.config.courses import courses_from
    if conn is None or not courses_from(config or {}).enabled:
        return []
    idx = CourseIndex(conn, config, now)
    return [{"group": k, "name": idx.c.label(k)} for k in idx.keys]


def may_answer(conn: sqlite3.Connection, config: dict | None, netid: str, group: str,
               now: datetime | None = None) -> tuple[bool, str]:
    """Whether ``netid`` may say whether they teach ``group`` this term: a
    course they are a faculty member of (in its group, or a user of its
    environment), not excluded, not staff, not already added by name.
    (ok, why not)."""
    from nomad.config.courses import candidates, courses_from
    n = (netid or "").strip().lower()
    if conn is None or not courses_from(config or {}).enabled:
        return False, "courses are off"
    idx = CourseIndex(conn, config, now)
    if group not in idx.keys:
        return False, "not a course"
    if n in idx.c.decisions(group, idx.term)[0]:
        return False, "already listed as teaching it"
    if n not in candidates(idx.c, group, idx.members, idx.term):
        return False, "not one of the course's faculty members"
    return True, ""


def _no_instructor(who: dict) -> str:
    if who["undecided"]:
        return (f"{len(who['faculty'])} faculty member(s) among its people"
                + (" besides those who said no" if who["said_no"] else "")
                + ", none of them active this term: nobody teaches it until one says so or is "
                "added (nomad course add)")
    if who["said_no"] and not who["faculty"]:
        return ("its faculty members said they don't teach it this term "
                "(nomad course add if someone else does)")
    return "no faculty member among its people and nobody added (nomad course add)"


def courses(conn: sqlite3.Connection | None, config: dict | None, netid: str | None = None,
            groups: list | None = None, now: datetime | None = None,
            names: bool | None = None, answers: dict | None = None) -> dict:
    """The courses ``netid`` teaches (or, for staff, the ``groups`` named;
    neither: every course somebody teaches, the rest in ``not_shown``), this
    term. ``names``: rows by name (None: as [console.courses] names says).
    ``answers``: what faculty members said this term, {group: {netid:
    True/False}} (``nomad.config.course_answers.load``)."""
    from nomad.config.courses import courses_from
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
    idx = CourseIndex(conn, config, now, answers)
    activity = idx.activity
    every = groups is None and not netid
    if groups is not None:
        # Only courses: any group's members are not a class to show.
        chosen = [g for g in groups if g in idx.keys]
        others = [g for g in groups if g not in idx.keys]
        if others:
            out.setdefault("not_courses", others)
    elif netid:
        chosen = idx.courses_of(netid)
    else:
        chosen = list(idx.keys)
    ceilings = memory_ceilings(conn)
    for g in chosen:
        people = idx.roster(g)
        who = people["teaching"]
        shown = idx.label(g)
        if every and not people["instructors"]:
            # Every course: one nobody teaches this term is shown to no one.
            out.setdefault("not_shown", []).append(
                {"group": g, "name": shown, "why": _no_instructor(who),
                 "students_this_term": len(people["this_term"])})
            continue
        students = people["this_term"]
        entry = {"group": g, "name": shown,
                 "instructors": sorted(people["instructors"]),
                 # added, confirmed, only, joined, environment, active (see WHY)
                 "instructors_why": {n: who["why"][n] for n in sorted(who["why"])},
                 "faculty_left_out": len(who["left_out"]),
                 "faculty_said_no": len(who["said_no"]),
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
        if idx.envs_of(g):
            entry["environments"] = sorted(idx.envs_of(g))     # whose users are its people
        if idx.by_environment(g):
            # This term's students: who joined or used its environment.
            entry["students"]["by_environment"] = True
        if not idx.people(g) and g not in c.listed:
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
        "This term's students: its people who are not faculty and joined its group this term "
        "or used a server or a cluster since it began (for a course whose class is in its "
        "environment rather than its group: who joined its group or used the environment).",
        "Hours open: a session open, whether or not anyone was at it; computing: at least 2% "
        "of a core between readings five minutes apart.",
        "A week before a figure was recorded is left out of it, not counted as zero.",
        "Rows are unnamed unless the site allows names; with few students they are not "
        "anonymous to someone who knows the class.",
    ]
    return out


# ── What a site has that may be courses ──

MAX_GROUP_MEMBERS = 300     # bigger groups are everyone of a kind (all users, a school)


def _own_group_regex(config: dict | None):
    """A faculty member's own group, by [console.labs] group_pattern (the
    usual "{netid}$" when unset), as a regular expression with the NetID."""
    import re
    try:
        from nomad.config.access import access_from
        pattern = access_from(config, log=False).group_pattern or "{netid}$"
    except Exception:
        pattern = "{netid}$"
    if "{netid}" not in pattern:
        return None
    left, right = pattern.split("{netid}", 1)
    return re.compile("^" + re.escape(left) + r"(?P<netid>[a-z0-9_][a-z0-9._-]*)"
                      + re.escape(right) + "$")


def discover(conn: sqlite3.Connection, config: dict | None, now: datetime | None = None,
             answers: dict | None = None, min_active: int = 3,
             max_members: int = MAX_GROUP_MEMBERS) -> dict:
    """What on this site may be courses that nomad.toml doesn't yet say
    are, for whoever sets courses up (``nomad course discover``, ``nomad
    course setup``). Read-only.

    * ``environments``: each environment people used this term (on
      MIN_ENV_DAYS days; the interactive collector, nomad 1.12.5): how many
      people, the faculty members among them, the courses it counts for,
      whether it is named like a course (``pattern``; not one set as
      ``course = false``) and whether a group has its name, and the group
      most of its other users are in (``group``: its name, how many, whether
      it is a course, whose own group it is);
    * ``groups``: groups that are not courses this term, with a faculty
      member in them or a faculty member's own group, at least
      ``min_active`` members active this term besides faculty, and no more
      than ``max_members`` members (bigger ones are everyone of a kind): the
      faculty members in them, whose own group it is (``own_of``, by
      [console.labs] group_pattern, "{netid}$" when unset), and the
      environment most of those active used;
    * ``courses``: how many courses there are this term, and how many
      nobody teaches.

    Names are NetIDs, groups and environments: for the people who set nomad
    up. Courses off: nothing but the counts."""
    idx = CourseIndex(conn, config, now, answers)
    c = idx.c
    out = {"term_start": idx.term, "sources": list(c.sources), "environments": [], "groups": [],
           "courses": {"count": len(idx.keys), "without_instructor": 0},
           "environments_recorded": bool(columns(conn, "interactive_env_daily"))}
    if not c.enabled:
        return out
    faculty = idx.faculty() - set(c.not_teaching)
    own = _own_group_regex(config)

    def owner(g: str):
        m = own.match(g) if own else None
        return m.group("netid") if m and m.group("netid") in faculty else None
    claimed = c.claimed()
    counts_for: dict = defaultdict(list)          # environment -> the courses it counts for
    for k in idx.keys:
        for e in idx.envs_of(k):
            counts_for[e].append(k)

    # Groups people are in, for overlaps: neither the faculty group nor
    # everyone-of-a-kind groups.
    sized = {g: {n.lower() for n in idx._members_of(g) if n} for g in idx.groups}
    usable = {g: m for g, m in sized.items()
              if g != c.faculty_group and 2 <= len(m) <= max_members}

    def best_group(people: set):
        hits = [(len(people & m), g) for g, m in usable.items() if people & m]
        if not hits:
            return None
        n, g = max(hits, key=lambda x: (x[0], -len(usable[x[1]]), x[1]))
        return {"group": g, "people": n, "is_course": g in idx._keyset, "own_of": owner(g)}

    def best_env(people: set):
        hits = [(len(people & users), e) for e, users in idx.env_people.items() if people & users]
        if not hits:
            return None
        n, e = max(hits, key=lambda x: (x[0], x[1]))
        return {"environment": e, "people": n, "courses": env_courses(e)}

    def env_courses(e: str) -> list:
        return sorted(counts_for.get(e, ()))

    for e, people in sorted(idx.env_people.items(), key=lambda x: (-len(x[1]), x[0])):
        ignored = e in c.listed and not c.listed[e].course
        out["environments"].append({
            "environment": e, "people": len(people),
            "faculty": sorted(people & faculty), "courses": env_courses(e),
            "named_like_a_course": (not ignored and e != c.faculty_group and e not in claimed
                                    and bool(c.pattern) and _matches(c.pattern, e)),
            "group_of_its_name": e in idx._groupset,
            "group": best_group(people - faculty)})

    looked = {g: m - faculty for g, m in usable.items()
              if g not in idx._keyset and not (g in c.listed and not c.listed[g].course)
              and (m & faculty or owner(g)) and len(m - faculty) >= min_active}
    # Who was active, asked once for everyone (a query per group is slow on
    # a hub's jobs).
    everyone: set = set()
    for students in looked.values():
        everyone |= students
    active_all = idx.activity.of(everyone)
    for g, students in looked.items():
        members = usable[g]
        active = students & active_all
        if len(active) < min_active:
            continue
        out["groups"].append({
            "group": g, "members": len(members), "active_this_term": len(active),
            "faculty": sorted(members & faculty), "own_of": owner(g),
            "environment": best_env(active)})
    out["groups"].sort(key=lambda x: (-x["active_this_term"], x["group"]))
    for k in idx.keys:
        if not idx.roster(k)["instructors"]:
            out["courses"]["without_instructor"] += 1
    return out


def _matches(pattern: str, name: str) -> bool:
    import re
    try:
        return re.fullmatch(pattern, name) is not None
    except re.error:
        return False

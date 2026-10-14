# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Courses: a Unix group per course, its instructors and its students.

    [console.courses]
    pattern = "^[a-z]{2,6}[0-9]{3}[a-z]?$"   # which groups are courses, by name
    faculty_group = "faculty"                # whose members can teach one
    term_start = "2026-08-24"                # this term's first day
    min_students = 0                         # fewer students this term: no figures
    names = false                            # instructors see students by name
    not_teaching = ["jdoe"]                  # in the faculty group, teaches no course

    [console.courses.groups."demo101"]
    name = "Econometrics"                    # how the course is shown
    instructors = ["jdoe"]                   # teaches it, in the group or not
    exclude = ["asmith"]                     # in the group and faculty, not teaching

    [console.courses.groups."lab201"]
    course = false                           # named like a course, but isn't one

Nothing is a course until [console.courses] is there: a site that has not
said how its groups work gets no course view rather than a wrong one (a
group named like a course that isn't one would show its members to every
faculty member in it). With the table, a group is a course when its name
matches ``pattern`` or it is listed under ``groups``.

Its instructors this term (``teaching``):

* anyone listed as one (``nomad course add``);
* the group's one member in ``faculty_group``, when it has one;
* with several, those who joined the group this term; when none did, those
  active this term where the course's students work (the servers and
  clusters they used): groups are reused term after term, and nobody takes
  last term's instructor out. When none of them was, nomad can't tell who
  teaches it, and nobody does until one is added;

less anyone excluded from the group, and anyone in ``not_teaching`` (staff
in the faculty group) unless added to the course by name. A course with no
instructor is shown to no one. Who joined is a stronger sign than who was
active: when one of several joined this term, the ones who did teach it.

Its students are its other members (faculty are never students), and of
those, this term's are the ones who joined since ``term_start`` or used a
server or a cluster since then: groups nobody prunes keep earlier terms'
students listed.

``term_start`` defaults to the start of the current term by a US academic
calendar (15 August, 1 January, 1 June). ``min_students`` defaults to 0: a
small institution's 300-level course may have three students. With so few,
even figures for the class taken together are close to each student's; the
institution decides, and ``names`` (default false) whether instructors see
each student by name.

``courses_from`` reads the settings (what is wrong is listed in
``problems`` and left out); ``teaching``, ``instructors``, ``courses_of``
and ``roster`` answer from the groups data (who was active, from the
caller: ``nomad.facts.courses.Activity``); ``set_course``,
``set_settings`` and ``set_not_teaching`` edit nomad.toml for `nomad
course`.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from nomad.config.edit import TomlEdit

DEFAULT_PATTERN = r"^[a-z]{2,6}[0-9]{3}[a-z]?$"
DEFAULT_FACULTY = "faculty"
SECTION = ("console", "courses")
GROUPS = SECTION + ("groups",)
_KEYS = ("pattern", "faculty_group", "term_start", "min_students", "names",
         "not_teaching", "groups")
_COURSE_KEYS = ("name", "instructors", "exclude", "course")
_GROUP = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._$-]*$")
_NETID = re.compile(r"^[a-z0-9_][a-z0-9._-]*$")


class CourseError(Exception):
    pass


def term_start_for(today: date) -> date:
    """The current term's first day by a US academic calendar: fall from
    15 August, spring from 1 January, summer from 1 June -- each moved to the
    Monday on or after it, so the first week is a week of classes."""
    def monday(d: date) -> date:
        return d + timedelta(days=(7 - d.weekday()) % 7)
    for month, day in ((8, 15), (6, 1), (1, 1)):
        start = monday(date(today.year, month, day))
        if today >= start:
            return start
    return monday(date(today.year - 1, 8, 15))


@dataclass(frozen=True)
class Course:
    name: str | None = None
    instructors: tuple = ()
    exclude: tuple = ()
    course: bool = True               # false: named like a course, but isn't one


@dataclass(frozen=True)
class Courses:
    enabled: bool = False                           # [console.courses] is there
    pattern: str = DEFAULT_PATTERN
    faculty_group: str = DEFAULT_FACULTY
    term_start: date | None = None
    min_students: int = 0
    names: bool = False
    not_teaching: tuple = ()                        # faculty-group members who teach nothing
    listed: dict = field(default_factory=dict)      # group -> Course
    problems: tuple = ()

    def is_course(self, group: str) -> bool:
        if not self.enabled or not group or group == self.faculty_group:
            return False
        if group in self.listed:
            return self.listed[group].course
        try:
            return bool(self.pattern) and re.fullmatch(self.pattern, group) is not None
        except re.error:
            return False

    def start(self, today: date | None = None) -> date:
        return self.term_start or term_start_for(today or date.today())

    def label(self, group: str) -> str:
        c = self.listed.get(group)
        return f"{group} ({c.name})" if c and c.name else group


def _names(value, where: str, problems: list, netids: bool = True) -> tuple:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        problems.append(f"{where} should be a list of NetIDs; ignored")
        return ()
    out = []
    for v in value:
        n = v.strip().lower() if isinstance(v, str) else None
        if n and (not netids or _NETID.match(n)):
            out.append(n)
        else:
            problems.append(f"{where}: {v!r} is not a NetID; ignored")
    return tuple(dict.fromkeys(out))


def courses_from(config: dict | None) -> Courses:
    console = (config or {}).get("console")
    raw = console.get("courses") if isinstance(console, dict) else None
    if raw is None:
        return Courses()
    problems: list[str] = []
    if not isinstance(raw, dict):
        return Courses(problems=("[console.courses] is not a table; ignored",))
    for key in raw:
        if key not in _KEYS:
            problems.append(f"[console.courses] {key}: unknown setting; the settings are "
                            f"{', '.join(_KEYS)}")
    pattern = raw.get("pattern", DEFAULT_PATTERN)
    if not isinstance(pattern, str):
        problems.append("[console.courses] pattern should be text; the default is used")
        pattern = DEFAULT_PATTERN
    else:
        try:
            re.compile(pattern)
        except re.error as e:
            problems.append(f"[console.courses] pattern is not a regular expression ({e}); "
                            "no group is a course by its name")
            pattern = ""
    faculty = raw.get("faculty_group", DEFAULT_FACULTY)
    if not isinstance(faculty, str) or not _GROUP.match(faculty.strip()):
        problems.append("[console.courses] faculty_group should be a group's name; "
                        f"{DEFAULT_FACULTY!r} is used")
        faculty = DEFAULT_FACULTY
    start = raw.get("term_start")
    if isinstance(start, datetime):
        start = start.date()
    elif isinstance(start, str):
        try:
            start = date.fromisoformat(start.strip())
        except ValueError:
            problems.append(f"[console.courses] term_start {start!r} is not a date "
                            "(YYYY-MM-DD); the calendar's is used")
            start = None
    elif start is not None and not isinstance(start, date):
        problems.append("[console.courses] term_start should be a date; the calendar's is used")
        start = None
    minimum = raw.get("min_students", 0)
    if isinstance(minimum, bool) or not isinstance(minimum, int) or minimum < 0:
        problems.append("[console.courses] min_students should be a whole number, 0 or more; "
                        "0 is used")
        minimum = 0
    names = raw.get("names", False)
    if not isinstance(names, bool):
        problems.append("[console.courses] names should be true or false; false is used")
        names = False
    not_teaching = _names(raw.get("not_teaching"), "[console.courses] not_teaching", problems)
    listed: dict = {}
    groups = raw.get("groups") or {}
    if not isinstance(groups, dict):
        problems.append('[console.courses.groups] should hold one table per course, as '
                        '[console.courses.groups."GROUP"]; ignored')
        groups = {}
    for group, table in groups.items():
        where = f'[console.courses.groups."{group}"]'
        g = group.strip()
        if not _GROUP.match(g):
            problems.append(f"{where}: not a group's name; ignored")
            continue
        if not isinstance(table, dict):
            problems.append(f"{where} should be a table; ignored")
            continue
        for key in table:
            if key not in _COURSE_KEYS:
                problems.append(f"{where} {key}: unknown; the settings are {', '.join(_COURSE_KEYS)}")
        name = table.get("name")
        if name is not None and not (isinstance(name, str) and name.strip()):
            problems.append(f"{where} name should be text; ignored")
            name = None
        is_course = table.get("course", True)
        if not isinstance(is_course, bool):
            # Whoever wrote it meant to say something about it: the safe reading.
            problems.append(f"{where} course should be true or false; taken as false "
                            "(not a course)")
            is_course = False
        listed[g] = Course(course=is_course, name=" ".join(name.split()) if name else None,
                           instructors=_names(table.get("instructors"), f"{where} instructors",
                                              problems),
                           exclude=_names(table.get("exclude"), f"{where} exclude", problems))
    return Courses(enabled=True, pattern=pattern, faculty_group=faculty.strip(), term_start=start,
                   min_students=minimum, names=names, not_teaching=not_teaching, listed=listed,
                   problems=tuple(problems))


def group_names(conn) -> list[str]:
    """Every group the groups data lists (any site), sorted."""
    try:
        return sorted(r[0] for r in conn.execute(
            "SELECT DISTINCT group_name FROM group_membership") if r[0])
    except Exception:
        return []


# Why someone teaches a course this term (``teaching``'s ``why``).
ADDED, ONLY, JOINED, ACTIVE = "added", "only", "joined", "active"
WHY = {ADDED: "added by name",
       ONLY: "the group's only faculty member",
       JOINED: "joined the group this term",
       ACTIVE: "active this term where its students work"}


def joined_this_term(when, since) -> bool:
    """A first_seen stamp on or after the term's first day ('2026-08-24',
    '2026-08-24T09:00:00' and '2026-08-24 09:00:00' alike)."""
    day = since.date() if isinstance(since, datetime) else since
    return bool(when) and str(when)[:10] >= day.isoformat()


def teaching(c: Courses, group: str, members_of: Callable[[str], set],
             active: Callable[[str, set], dict] | None = None) -> dict:
    """Who teaches ``group`` this term, and why (see the module's notes).

    ``active(group, candidates)``: of the group's faculty members, who
    joined the group this term or was active this term, as {netid: JOINED or
    ACTIVE} (``nomad.facts.courses.Activity.faculty``); without it nobody is
    taken as active. Returns ``instructors``; ``why`` {netid: ADDED, ONLY,
    JOINED or ACTIVE}; ``faculty``, the group's faculty members nomad chose
    among (less those excluded and those not teaching); ``not_active``,
    those of them left out for not being active this term; ``undecided``,
    true when several are and none was active (nobody teaches it until one
    is added)."""
    listed = c.listed.get(group, Course())
    added = set(listed.instructors) - set(listed.exclude)
    # The faculty members nomad chooses among: staff in the faculty group
    # (not_teaching) never count, even when added to this course by name -- a
    # TA added doesn't make its one faculty member one of several.
    faculty = (_lower(members_of(group)) & _lower(members_of(c.faculty_group))) \
        - set(listed.exclude) - set(c.not_teaching)
    why = dict.fromkeys(added, ADDED)
    found: dict = {}
    if len(faculty) == 1:
        found = dict.fromkeys(faculty, ONLY)
    elif faculty and active is not None:
        found = {n.lower(): r for n, r in (active(group, set(faculty)) or {}).items()
                 if n.lower() in faculty}
    for n, r in found.items():
        why.setdefault(n, r)
    not_active = faculty - set(found) - added
    return {"instructors": set(why), "why": why, "faculty": faculty, "not_active": not_active,
            "undecided": len(faculty) > 1 and not found and not added}


def _lower(names) -> set:
    return {n.lower() for n in names if n}


def instructors(c: Courses, group: str, members_of: Callable[[str], set],
                active: Callable[[str, set], dict] | None = None) -> set:
    """Who teaches ``group`` this term (``teaching``'s instructors)."""
    return teaching(c, group, members_of, active)["instructors"]


def course_groups(c: Courses, known: Iterable[str]) -> list[str]:
    """The groups that are courses: listed ones and those whose name matches."""
    if not c.enabled:
        return []
    return sorted({g for g in set(known) | set(c.listed) if c.is_course(g)})


def courses_of(c: Courses, netid: str, known: Iterable[str],
               members_of: Callable[[str], set],
               active: Callable[[str, set], dict] | None = None) -> list[str]:
    """The courses ``netid`` teaches this term."""
    n = (netid or "").strip().lower()
    if not n:
        return []
    out = []
    for g in course_groups(c, known):
        listed = c.listed.get(g, Course())
        # Only a member of the group, or someone added, can teach it.
        if n not in listed.instructors and n not in _lower(members_of(g)):
            continue
        if n in instructors(c, g, members_of, active):
            out.append(g)
    return out


def first_seen(conn, group: str) -> dict:
    """{username (lower case): earliest first_seen} for a group's rows (any
    site). A membership from before nomad 1.11 has none, and neither does
    anyone with such a row anywhere: a row from a site added since (or a
    renamed one) says when nomad first looked there, not when they joined."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(group_membership)")}
        if "first_seen" not in cols:
            return {}
        out: dict = {}
        unknown: set = set()
        for user, when in conn.execute(
                "SELECT username, first_seen FROM group_membership WHERE group_name = ?", (group,)):
            if not user:
                continue
            u = user.lower()
            if not when:
                unknown.add(u)
            elif u not in out or str(when) < out[u]:
                out[u] = str(when)
        return {u: w for u, w in out.items() if u not in unknown}
    except Exception:
        return {}


def roster(c: Courses, group: str, members_of: Callable[[str], set],
           active: Callable[[set], set], joined: dict, since: datetime,
           faculty_active: Callable[[str, set], dict] | None = None) -> dict:
    """The course's people: ``instructors`` (and ``teaching``, why);
    ``listed``, the group's other members; ``this_term``, those of them who
    joined since ``since`` or were active since then (``active(candidates)``
    says who); ``left_out``, the rest (listed, from before, not active).
    ``faculty_active``: as ``teaching``'s ``active``."""
    who = teaching(c, group, members_of, faculty_active)
    teach = who["instructors"]
    listed = _lower(members_of(group)) - teach - _lower(members_of(c.faculty_group))
    listed -= set(c.listed.get(group, Course()).exclude) | set(c.not_teaching)
    were_active = active(listed) & listed
    new = {u for u in listed if joined_this_term(joined.get(u), since)}
    this_term = were_active | new
    left = listed - this_term
    # Left out: joined in an earlier term (nomad saw them join), or not
    # known -- memberships older than nomad 1.11 have no join date, so a
    # student of this term who hasn't started looks like an earlier one.
    earlier = {u for u in left if joined.get(u)}
    return {"instructors": teach, "teaching": who, "listed": listed, "this_term": this_term,
            "active": were_active, "new": new, "left_out": left,
            "left_out_earlier": earlier, "left_out_unknown": left - earlier}


# -- editing nomad.toml ---------------------------------------------------------------

def _get(data: dict, path: tuple):
    for k in path:
        if not isinstance(data, dict):
            return None
        data = data.get(k)
    return data


def _check_group(group: str) -> str:
    g = group.strip()
    if not _GROUP.match(g):
        raise CourseError(f"{group!r}: not a group's name")
    return g


def set_course(edit: TomlEdit, group: str, *, add: str | None = None,
               exclude: str | None = None, remove: str | None = None,
               name: str | None = None, ignore: bool | None = None) -> list:
    """Change [console.courses.groups."GROUP"]: ``add`` an instructor,
    ``exclude`` a member who isn't one, ``remove`` someone from either list,
    set the course's ``name``, or ``ignore`` it (True: not a course; False:
    a course again). Returns what changed, in words."""
    g = _check_group(group)
    path = GROUPS + (g,)
    # A course table written as a value (groups = {...}, courses = {...})
    # can't take a table of its own; [console.courses.groups.X] headers can.
    clash = edit.assigned(path)
    if clash:
        raise CourseError(f"the course {g} is set as values in {edit.path.name} "
                          f"(line {clash[0] + 1}); change it by hand")
    table = _get(edit.data, path) or {}
    if not isinstance(table, dict):
        raise CourseError(f"{g} in [console.courses.groups] is not a table; change it by hand")
    body = {k: table[k] for k in table if k not in _COURSE_KEYS}
    if body:
        raise CourseError(f'[console.courses.groups."{g}"] has settings nomad does not know '
                          f"({', '.join(body)}); change it by hand")
    not_course = table.get("course") is False
    teach = [n for n in (table.get("instructors") or []) if isinstance(n, str)]
    out = [n for n in (table.get("exclude") or []) if isinstance(n, str)]
    shown = table.get("name") if isinstance(table.get("name"), str) else None
    changes = []
    for who in (add, exclude, remove):
        if who is not None and not _NETID.match(who.strip().lower()):
            raise CourseError(f"{who}: not a NetID (no domain, no @)")
    if add:
        n = add.strip().lower()
        if n not in teach:
            teach.append(n)
            changes.append(f"{n} teaches {g}")
        if n in out:
            out.remove(n)
            changes.append(f"{n} no longer excluded from {g}")
    if exclude:
        n = exclude.strip().lower()
        if n not in out:
            out.append(n)
            changes.append(f"{n} is not one of {g}'s instructors")
        if n in teach:
            teach.remove(n)
    if remove:
        n = remove.strip().lower()
        if n in teach:
            teach.remove(n)
            changes.append(f"{n} no longer listed as teaching {g}")
        if n in out:
            out.remove(n)
            changes.append(f"{n} no longer excluded from {g}")
    if ignore is not None and ignore != not_course:
        not_course = ignore
        changes.append(f"{g} is not a course" if ignore else f"{g} is a course again")
    if name is not None:
        new = " ".join(name.split())
        if new != (shown or ""):
            changes.append(f'{g} is shown as "{new}"' if new else f"{g}'s name removed")
            shown = new or None
    if not changes:
        return []
    new_body: dict = {}
    if not_course:
        new_body["course"] = False
    if shown:
        new_body["name"] = shown
    if teach:
        new_body["instructors"] = teach
    if out:
        new_body["exclude"] = out
    if new_body:
        if _get(edit.data, SECTION) is None:
            edit.set_table(SECTION, {"pattern": DEFAULT_PATTERN, "faculty_group": DEFAULT_FACULTY})
            changes.insert(0, "[console.courses] with the default pattern and faculty group: "
                              "courses are on")
        edit.set_table(path, new_body, after=SECTION)
    else:
        edit.set_table(path, {})
    return changes


def set_settings(edit: TomlEdit, **values) -> list:
    """[console.courses] with these settings (term_start as YYYY-MM-DD,
    pattern, faculty_group, min_students, names); a new table also gets the
    default pattern and faculty group, which turns courses on."""
    clash = edit.assigned(SECTION)
    if clash:
        raise CourseError(f"courses is set as values in {edit.path.name} (line {clash[0] + 1}); "
                          "change it by hand")
    values = {k: v for k, v in values.items() if v is not None}
    for k in values:
        if k not in ("term_start", "pattern", "faculty_group", "min_students", "names",
                     "not_teaching"):
            raise CourseError(f"{k}: not a course setting")
    if "term_start" in values:
        try:
            values["term_start"] = date.fromisoformat(str(values["term_start"])).isoformat()
        except ValueError as e:
            raise CourseError(f"{values['term_start']!r}: not a date (YYYY-MM-DD)") from e
    if "pattern" in values:
        try:
            re.compile(values["pattern"])
        except re.error as e:
            raise CourseError(f"{values['pattern']!r}: not a regular expression ({e})") from e
    if "faculty_group" in values and not _GROUP.match(str(values["faculty_group"])):
        raise CourseError(f"{values['faculty_group']!r}: not a group's name")
    current = _get(edit.data, SECTION)
    changes = []
    if current is None:
        body = {"pattern": DEFAULT_PATTERN, "faculty_group": DEFAULT_FACULTY, **values}
        edit.set_table(SECTION, body)
        return ["[console.courses]: courses are on"] + [f"{k} = {v!r}" for k, v in body.items()]
    if not isinstance(current, dict):
        raise CourseError(f"courses in {edit.path.name} is not a table; change it by hand")
    for key, val in values.items():
        now = current.get(key)
        if isinstance(now, (date, datetime)):          # a TOML date
            now = (now.date() if isinstance(now, datetime) else now).isoformat()
        if now == val:
            continue
        for i, table, k in reversed(edit.key_lines()):
            if table == SECTION and k == (key,):
                try:
                    edit.remove_line(i)
                except ValueError as e:
                    raise CourseError(f"{e}; change {key} by hand") from e
        edit.insert_key(SECTION, key, val)
        changes.append(f"{key} = {val!r}")
    return changes


def set_not_teaching(edit: TomlEdit, netid: str, undo: bool = False) -> list:
    """[console.courses] not_teaching with ``netid`` (``undo``: without it):
    someone in the faculty group who teaches no course (staff), left out of
    every course's instructors unless added to one by name."""
    n = (netid or "").strip().lower()
    if not _NETID.match(n):
        raise CourseError(f"{netid}: not a NetID (no domain, no @)")
    clash = edit.assigned(SECTION)
    if clash:
        raise CourseError(f"courses is set as values in {edit.path.name} (line {clash[0] + 1}); "
                          "change it by hand")
    current = _get(edit.data, SECTION)
    if not isinstance(current, dict):
        raise CourseError("courses are off (no [console.courses] in "
                          f"{edit.path.name}): `nomad course setup` turns them on")
    now = current.get("not_teaching", [])
    if isinstance(now, str):
        now = [now]
    if not isinstance(now, list):
        raise CourseError(f"[console.courses] not_teaching in {edit.path.name} is not a list; "
                          "change it by hand")
    names = [x.strip().lower() for x in now if isinstance(x, str) and x.strip()]
    if undo:
        if n not in names:
            return []
        names = [x for x in names if x != n]
        change = f"{n} may teach again (off [console.courses] not_teaching)"
    else:
        if n in names:
            return []
        names.append(n)
        change = f"{n} teaches no course unless added to one ([console.courses] not_teaching)"
    for i, table, k in reversed(edit.key_lines()):
        if table == SECTION and k == ("not_teaching",):
            try:
                edit.remove_line(i)
            except ValueError as e:
                raise CourseError(f"{e}; change not_teaching by hand") from e
    if names:
        edit.insert_key(SECTION, "not_teaching", names)
    return [change]

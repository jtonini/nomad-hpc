# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad course`: courses in nomad.toml ([console.courses]) and who is in them."""
from __future__ import annotations

import click


@click.group("course")
def course_group():
    """Courses: a Unix group per course, its instructors and this term's
    students ([console.courses] in nomad.toml).

    \b
    nomad course setup                           turn courses on: asks how classes get access
    nomad course setup --term-start 2026-08-24   change a setting
    nomad course discover                        what here may be a course, and what to do
    nomad course list                            each course: instructors (and why), students
    nomad course env GROUP ENV [--remove]        whoever used environment ENV is in GROUP's class
    nomad course add GROUP NETID                 NETID teaches GROUP this term
    nomad course exclude GROUP NETID             in GROUP and faculty, not teaching it this term
                                                 (both: --every-term for good)
    nomad course remove GROUP NETID              off either list
    nomad course name GROUP "Econometrics"       how the course is shown
    nomad course ignore GROUP [--undo]           named like a course, but isn't one
    nomad course not-teaching NETID [--undo]     faculty group, but teaches nothing (staff)
    nomad course answers                         what faculty said in the Console this term
    nomad course answer GROUP NETID --yes|--no   record it for them
    Changes are shown first; --apply writes them (with a backup).
    `nomad facts course` gives a course's figures.
    """


def _edit(ctx):
    from nomad.cli import _lab_edit
    return _lab_edit(ctx)


def _q(name: str) -> str:
    """A name as typed in a shell (a group like jdoe$ in quotes)."""
    import shlex
    return shlex.quote(name)


def _outside_courses(d: dict) -> dict:
    import copy
    d = copy.deepcopy(d)
    console = d.get("console")
    if isinstance(console, dict):
        console.pop("courses", None)
        if not console:
            d.pop("console")
    return d


def _finish(edit, changes, apply, check, notes=()):
    from nomad.cli import _lab_finish
    before = _outside_courses(_original(edit))
    _lab_finish(edit, changes, apply,
                lambda d: check(d) and _outside_courses(d) == before, notes)


def _save(edit, check) -> None:
    """Write the edit (as `--apply` does), with a backup."""
    before = _outside_courses(_original(edit))
    try:
        backup = edit.save(lambda d: check(d) and _outside_courses(d) == before)
    except ValueError as e:
        raise click.ClickException(f"{e}; nothing changed")
    except OSError as e:
        raise click.ClickException(f"can't write {edit.path} ({e.strerror or e}); "
                                   "nothing changed")
    click.echo("Written." + (f" Backup: {backup}" if backup else ""))


def _original(edit) -> dict:
    from nomad.config.edit import loads
    return loads(edit.original) if edit.original.strip() else {}


@course_group.command("setup")
@click.option("--sources", metavar="LIST",
              help="How classes get access, comma-separated: groups (a group per course, "
                   "named for it), own_groups (each instructor's own group), environments "
                   "(an environment per course). Default: groups.")
@click.option("--term-start", metavar="YYYY-MM-DD", help="This term's first day.")
@click.option("--pattern", help="Which groups (and environments) are courses, by name "
                                "(a regular expression).")
@click.option("--faculty-group", help="The group whose members can teach a course.")
@click.option("--min-students", type=click.IntRange(0, None),
              help="Fewer students this term: counts only (default 0).")
@click.option("--names/--no-names", default=None,
              help="Whether instructors see each student by name (default: no).")
@click.option("--ask", is_flag=True,
              help="Ask each question (the default in a terminal when no setting is given).")
@click.option("--db", type=click.Path(), help="Database (default: the hub's, else this host's).")
@click.option("--apply", is_flag=True, help="Write the change (default: only show it; "
                                            "with questions, asks first).")
@click.pass_context
def setup_cmd(ctx, sources, term_start, pattern, faculty_group, min_students, names, ask, db,
              apply):
    """Turn courses on, or change their settings. In a terminal with no
    setting given, it asks how classes get access here (a group per course,
    instructors' own groups, an environment per course), which names are
    courses', who can teach one, when the term began and whether instructors
    see names, with what it finds here for each; then shows what would
    change, and what nomad would take as courses."""
    import sys

    from nomad.config.courses import CourseError, courses_from, set_settings
    edit = _edit(ctx)
    given = {"sources": _sources(sources), "term_start": term_start, "pattern": pattern,
             "faculty_group": faculty_group, "min_students": min_students, "names": names}
    asking = ask or (all(v is None for v in given.values())
                     and sys.stdin.isatty() and sys.stdout.isatty())
    if asking:
        given = _ask_setup(ctx, edit, given, db)
    try:
        changes = set_settings(edit, **given)
    except CourseError as e:
        raise click.ClickException(str(e))
    term_start, sources = given["term_start"], given["sources"]
    min_students, names = given["min_students"], given["names"]

    def check(d):
        c = courses_from(d)
        return (c.enabled
                and (term_start is None or c.term_start == _date(term_start))
                and (min_students is None or c.min_students == min_students)
                and (names is None or c.names == names)
                and (sources is None or list(c.sources) == list(dict.fromkeys(sources))))
    notes = []
    if names:
        notes.append("Instructors will see their students by name: a decision for the "
                     "institution (FERPA).")
    before = courses_from(_original(edit))
    if term_start and _date(term_start) and before.enabled:
        old = before.start().isoformat()                # set, or the calendar's
        if old != _date(term_start).isoformat():
            held = [g for g, x in before.listed.items() if old in x.terms]
            notes.append(f"A new term: decisions for the term from {old} "
                         f"({len(held)} course(s) in nomad.toml) and what faculty members said "
                         "for it stop applying; they are kept as a record.")
    if asking and changes and not apply:
        _setup_preview(ctx, edit, db)
        click.echo(f"{edit.path}:")
        for change in changes:
            click.echo(f"  + {change}")
        for n in notes:
            click.echo(f"  {n}")
        if not click.confirm("\nWrite this to nomad.toml?", default=False):
            click.echo("Nothing changed.")
            return
        _save(edit, check)
        return
    if asking and changes:
        _setup_preview(ctx, edit, db)
    _finish(edit, changes, apply, check, notes)


def _open_masked(ctx, db, mask: bool):
    """The database (nomad.facts.cli._open), its path left out of the error
    under --mask."""
    from nomad.facts.cli import _open
    try:
        return _open(ctx, db)
    except click.ClickException as e:
        if mask:
            raise click.ClickException("no database here (give one with --db)") from e
        raise


def _sources(text):
    """--sources "groups,environments" as a list (None when not given)."""
    if text is None:
        return None
    return [s for s in text.replace(",", " ").split() if s]


def _try_open(ctx, db):
    """(path, connection) to the database, or (None, None) when there is none."""
    try:
        from nomad.facts.cli import _open
        return _open(ctx, db)
    except (click.ClickException, OSError, Exception):
        return None, None


def _with_courses(config: dict, table: dict) -> dict:
    """``config`` with [console.courses] replaced by ``table``."""
    import copy
    out = copy.deepcopy(config or {})
    console = out.get("console")
    if not isinstance(console, dict):
        console = out["console"] = {}
    console["courses"] = copy.deepcopy(table)
    return out


def _courses_table(data: dict) -> dict:
    console = (data or {}).get("console")
    table = console.get("courses") if isinstance(console, dict) else None
    return dict(table) if isinstance(table, dict) else {}


def _matching(pattern: str, names) -> list:
    import re
    try:
        rx = re.compile(pattern)
    except re.error:
        return []
    return sorted(n for n in names if rx.fullmatch(n))


def _examples(names: list, n: int = 3) -> str:
    return ", ".join(names[:n]) + (", ..." if len(names) > n else "")


_SOURCE_CHOICES = (("groups", "a Unix group per course, named for it (demo101)"),
                   ("own_groups", "each instructor's own group (jdoe$)"),
                   ("environments", "a software environment per course: a conda environment "
                                    "or Jupyter kernel named for it"))


def _ask_setup(ctx, edit, given: dict, db) -> dict:
    """Ask how courses work here, with what the database shows for each
    answer. Returns the settings for set_settings."""
    path, conn = _try_open(ctx, db)
    try:
        return _questions(ctx, edit, given, conn)
    finally:
        if conn is not None:
            conn.close()


def _questions(ctx, edit, given: dict, conn) -> dict:
    from datetime import date, datetime

    from nomad.config.access import members_lookup
    from nomad.config.courses import (DEFAULT_FACULTY, DEFAULT_PATTERN, SOURCES, courses_from,
                                      group_names, term_start_for)
    from nomad.facts.courses import discover
    config = ctx.obj.get("config", {}) or {}
    table = _courses_table(edit.data)
    now = courses_from(_with_courses(config, table)) if table else None
    pattern_now = given["pattern"] or (now.pattern if now else DEFAULT_PATTERN)
    groups, envs, own, members_of, recorded = [], [], [], None, False
    have_db = conn is not None
    if have_db:
        click.echo("Looking at the groups and environments here...", err=True)
        probe = _with_courses(config, {**table, "pattern": pattern_now,
                                       "sources": list(SOURCES)})
        found = discover(conn, probe, datetime.now())
        groups = group_names(conn)
        envs = [e["environment"] for e in found["environments"]]
        own = [g for g in found["groups"] if g["own_of"]]
        _, members_of = members_lookup(conn)
        recorded = found["environments_recorded"]
    gset = set(groups)
    click.echo("How courses work here (Enter keeps the answer in brackets).\n")

    # 1. Sources
    hints = {}
    if have_db:
        named = _matching(pattern_now, groups)
        hints["groups"] = (f"{_n(len(named), 'group')} here named like a course"
                           + (f": {_examples(named)}" if named else ""))
        hints["own_groups"] = f"{_own_groups(len(own))} people active this term"
        paired = [e for e in _matching(pattern_now, envs) if e in gset]
        hints["environments"] = (
            (f"{_n(len(envs), 'environment')} used this term, {len(paired)} named like a "
             "course group" + (f": {_examples(paired)}" if paired else ""))
            if recorded else "not recorded here yet: the interactive servers record them "
                             "from nomad 1.12.5 on")
    click.echo("1. How do classes get access to the servers here? More than one may apply.")
    for i, (key, text) in enumerate(_SOURCE_CHOICES, 1):
        click.echo(f"   {i}  {text}")
        if key in hints:
            click.echo(f"      ({hints[key]})")
    current = given["sources"] or (list(now.sources) if now else ["groups"])
    keys = [k for k, _ in _SOURCE_CHOICES]
    default = ",".join(str(keys.index(s) + 1) for s in current if s in keys) or "1"
    while True:
        answer = click.prompt("   Which? (e.g. 1,3)", default=default)
        picked = [p for p in answer.replace(",", " ").split() if p]
        if picked and all(p in ("1", "2", "3") for p in picked):
            sources = [keys[int(p) - 1] for p in dict.fromkeys(picked)]
            break
        click.echo("   1, 2 or 3, or several (1,3)")

    # 2. Pattern
    pattern = pattern_now
    if "groups" in sources or "environments" in sources:
        click.echo("\n2. Which names are a course's (a regular expression)?")
        while True:
            pattern = click.prompt("  ", default=pattern_now)
            if not _matching_ok(pattern):
                click.echo("   not a regular expression; again")
                continue
            if have_db:
                g = _matching(pattern, groups)
                e = [x for x in _matching(pattern, envs) if x in gset]
                parts = [f"{_n(len(g), 'group')}" + (f" ({_examples(g)})" if g else "")]
                if "environments" in sources:
                    parts.append(f"{_n(len(e), 'environment')} with a group of its name"
                                 + (f" ({_examples(e)})" if e else ""))
                click.echo(f"   matches {' and '.join(parts)}")
            break
    else:
        click.echo("\n2. (No names make a course: an own group becomes one when someone is "
                   "added to it.)")

    # 3. Faculty group
    click.echo("\n3. Which group's members can teach a course?")
    fac_now = given["faculty_group"] or (now.faculty_group if now else DEFAULT_FACULTY)
    while True:
        faculty = click.prompt("  ", default=fac_now).strip()
        if members_of is None:
            break
        n = len(members_of(faculty))
        if n:
            click.echo(f"   {faculty}: {_n(n, 'member')}")
            break
        if click.confirm(f"   {faculty}: no members in the groups data. Keep it anyway?",
                         default=False):
            break

    # 4. Term start: kept unset (the calendar's) unless one is given.
    calendar = term_start_for(date.today()).isoformat()
    set_now = given["term_start"] or (now.term_start.isoformat() if now and now.term_start
                                      else None)
    if set_now:
        click.echo("\n4. This term's first day (YYYY-MM-DD)? A new term begins when it is "
                   "changed.")
    else:
        click.echo(f"\n4. This term's first day (YYYY-MM-DD)? Enter: by the calendar ({calendar} "
                   "now; it moves on by itself). The first day of classes is better: weeks "
                   "before it count as weeks of nothing.")
    while True:
        term_start = click.prompt("  " if set_now else "   (Enter: by the calendar)",
                                  default=set_now or "", show_default=bool(set_now)).strip()
        if not term_start or _date(term_start):
            break
        click.echo("   as YYYY-MM-DD, e.g. 2026-08-24")
    term_start = term_start or None

    # 5. Small courses
    click.echo("\n5. Below how many students this term does a course show counts only? "
               "(0: figures for every course)")
    min_now = given["min_students"] if given["min_students"] is not None else \
        (now.min_students if now else 0)
    min_students = click.prompt("  ", default=min_now, type=click.IntRange(0, None))

    # 6. Names
    click.echo("\n6. Should instructors see each student by name? The institution decides "
               "(FERPA); without names, rows are numbered.")
    names_now = given["names"] if given["names"] is not None else (now.names if now else False)
    names = click.confirm("  ", default=names_now)
    click.echo("")
    return {"sources": sources, "term_start": term_start, "pattern": pattern,
            "faculty_group": faculty, "min_students": min_students, "names": names}


def _matching_ok(pattern: str) -> bool:
    import re
    try:
        re.compile(pattern)
        return True
    except re.error:
        return False


def _setup_preview(ctx, edit, db) -> None:
    """With the settings about to be written: how many courses, and what
    `nomad course discover` would suggest."""
    from datetime import datetime

    from nomad.facts.courses import discover
    path, conn = _try_open(ctx, db)
    if conn is None:
        return
    try:
        config = _with_courses(ctx.obj.get("config", {}) or {}, _courses_table(edit.data))
        d = discover(conn, config, datetime.now())
    finally:
        conn.close()
    click.echo(_courses_line(d))
    # Not another change of sources: they were just answered.
    todo = [t for t in _todo(d, 3) if t[0][:3] != ["nomad", "course", "setup"]]
    if todo:
        click.echo("Then (nomad course discover says more):")
        _show_todo(todo, _Names(False))
    click.echo("")


def _group_known(ctx, group: str) -> bool | None:
    """Does the groups data list GROUP, or is it an environment the
    interactive servers recorded (a course may be only an environment)? The
    hub's combined database, else this host's; None when none could be read."""
    return _db_has(ctx, ("SELECT 1 FROM group_membership WHERE group_name = ? LIMIT 1",
                         "SELECT 1 FROM interactive_env_daily WHERE environment = ? LIMIT 1"),
                   group)


def _env_known(ctx, env: str) -> bool | None:
    """Has a session run from ENV (as the interactive servers record it,
    nomad 1.12.5)? None when no database could be read."""
    return _db_has(ctx, ("SELECT 1 FROM interactive_env_daily WHERE environment = ? LIMIT 1",),
                   env)


def _db_has(ctx, queries, value) -> bool | None:
    """True when one of ``queries`` finds ``value`` in the hub's combined
    database or this host's; False when one could be read and none did;
    None when none could be (a table missing: that query can't say)."""
    import sqlite3
    import urllib.parse
    from pathlib import Path
    paths = []
    try:
        from nomad.actions.remote import hub_settings
        paths.append(hub_settings()["combined_db"])
    except Exception:
        pass
    try:
        from nomad.cli import get_db_path
        paths.append(get_db_path(ctx.obj.get("config", {}) or {}))
    except Exception:
        pass
    answered = False
    for path in paths:
        try:
            path = Path(path).expanduser()
            if not path.exists():
                continue
            conn = sqlite3.connect("file:" + urllib.parse.quote(str(path)) + "?mode=ro", uri=True)
            try:
                for sql in queries:
                    try:
                        row = conn.execute(sql, (value,)).fetchone()
                    except sqlite3.OperationalError as e:
                        if "no such table" in str(e):
                            continue
                        raise
                    answered = True
                    if row:
                        return True
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    return False if answered else None


def _date(text):
    from datetime import date
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _course_change(ctx, group, apply, new_group=False, **kw):
    from nomad.config.courses import CourseError, courses_from, set_course
    current = courses_from(ctx.obj.get("config", {}) or {})
    if group.strip() == current.faculty_group and not (kw.get("remove") or kw.get("env_remove")
                                                        or kw.get("ignore") is not None):
        raise click.ClickException(f"{group} is the faculty group ([console.courses] "
                                   "faculty_group): never a course")
    if not kw.get("remove") and not kw.get("env_remove") and not new_group:
        known = _group_known(ctx, group.strip())
        if known is False:
            raise click.ClickException(
                f"{group}: no such group in the groups data, and no environment of that name "
                "recorded (nomad course list shows the courses). Check the spelling, or give "
                "--new-group for a group not collected yet.")
    edit = _edit(ctx)
    # Decisions about who teaches hold for this term unless --every-term.
    before = courses_from(_original(edit))
    term = before.start().isoformat()
    if kw.get("add") or kw.get("exclude"):
        from datetime import date
        note = _term_note(before, date.today())
        if note:
            click.echo(click.style(note, fg="yellow"))
    try:
        changes = set_course(edit, group, term=term, **kw)
    except CourseError as e:
        raise click.ClickException(str(e))
    g = group.strip()

    def check(d):
        cs = courses_from(d)
        c = cs.listed.get(g)
        added, out = cs.decisions(g, term)
        if kw.get("add"):
            return kw["add"].strip().lower() in added
        if kw.get("exclude"):
            return kw["exclude"].strip().lower() in out
        if kw.get("remove"):
            n = kw["remove"].strip().lower()
            return n not in added and n not in out
        if kw.get("name") is not None:
            return ((c.name or "") if c else "") == " ".join(kw["name"].split())
        if kw.get("ignore") is not None:
            return (c is not None and c.course is False) if kw["ignore"] else \
                (c is None or c.course)
        if kw.get("env_add"):
            return c is not None and kw["env_add"].strip() in c.environments
        if kw.get("env_remove"):
            return c is None or kw["env_remove"].strip() not in c.environments
        return True
    notes = ["The Console reads nomad.toml on every request: no restart."] if changes else []
    if changes and kw.get("env_add"):
        notes = _env_notes(ctx, before, g, kw["env_add"].strip()) + notes
    _finish(edit, changes, apply, check, notes)


def _env_notes(ctx, before, group: str, env: str) -> list:
    """What to know before an environment counts for a course."""
    notes = []
    others = sorted(k for k, x in before.listed.items() if env in x.environments and k != group)
    if others:
        notes.append(f"{env} also counts for {', '.join(others)}: whoever used it is one of "
                     "the people of each.")
    elif env != group and before.is_course(env, "environment"):
        notes.append(f"{env} is named like a course; listed under {group}, it counts for "
                     f"{group} (and no course of its own).")
    if not others and env != group:
        from nomad.facts.courses import MIN_ENV_DAYS
        notes.append(f"Whoever used {env} on {MIN_ENV_DAYS} days or more this term is among "
                     f"{group}'s students; its instructors still come from its group.")
    if group in before.listed and not before.listed[group].course:
        notes.append(f"{group} is set as not a course (course = false): {env} counts for "
                     f"nothing until `nomad course ignore {_q(group)} --undo`.")
    elif not (group in before.listed and before.listed[group].every_term()) and \
            not before.is_course(group):
        notes.append(f"{group} becomes a course every term (a course's environments are listed "
                     "for good); for one term only, `nomad course add` its instructor instead.")
    if _env_known(ctx, env) is False:
        notes.append(f"No session has run from {env} yet, as far as the interactive servers "
                     "record (nomad 1.12.5 and later): check its name with `nomad course "
                     "discover`.")
    return notes


@course_group.command("env")
@click.argument("group")
@click.argument("environment")
@click.option("--remove", is_flag=True, help="ENVIRONMENT no longer counts for GROUP.")
@click.option("--new-group", is_flag=True, help="GROUP is not in the groups data yet.")
@click.option("--apply", is_flag=True, help="Write the change (default: only show it).")
@click.pass_context
def env_cmd(ctx, group, environment, remove, new_group, apply):
    """Whoever used ENVIRONMENT this term (a conda environment, a Jupyter
    kernel) is one of the course GROUP's people, for a class that uses an
    environment not named like its course; it makes GROUP a course. Every
    term, until --remove. The interactive servers record which environment
    each session runs from (nomad 1.12.5 and later); `nomad course discover`
    lists them."""
    if remove:
        _course_change(ctx, group, apply, new_group, env_remove=environment)
    else:
        _course_change(ctx, group, apply, new_group, env_add=environment)


@course_group.command("add")
@click.argument("group")
@click.argument("netid")
@click.option("--every-term", is_flag=True,
              help="Teaches it every term (default: this term only).")
@click.option("--new-group", is_flag=True, help="GROUP is not in the groups data yet.")
@click.option("--apply", is_flag=True, help="Write the change (default: only show it).")
@click.pass_context
def add_cmd(ctx, group, netid, every_term, apply, new_group):
    """NETID teaches GROUP this term: when no faculty member is in the group,
    when several are and nomad can't tell which teaches it, or for a
    co-instructor or a TA. Decisions for a term stop applying when the term
    moves on (--every-term: they don't)."""
    _course_change(ctx, group, apply, new_group, add=netid, every_term=every_term)


@course_group.command("exclude")
@click.argument("group")
@click.argument("netid")
@click.option("--every-term", is_flag=True,
              help="Never teaches it (default: not this term).")
@click.option("--new-group", is_flag=True, help="GROUP is not in the groups data yet.")
@click.option("--apply", is_flag=True, help="Write the change (default: only show it).")
@click.pass_context
def exclude_cmd(ctx, group, netid, every_term, apply, new_group):
    """NETID is in GROUP and in the faculty group but doesn't teach it this
    term (overrides what they said and nomad's guess). For someone who
    teaches no course at all, `nomad course not-teaching`."""
    _course_change(ctx, group, apply, new_group, exclude=netid, every_term=every_term)


@course_group.command("remove")
@click.argument("group")
@click.argument("netid")
@click.option("--apply", is_flag=True, help="Write the change (default: only show it).")
@click.pass_context
def remove_cmd(ctx, group, netid, apply):
    """NETID off GROUP's instructors and excluded lists (this term's and
    every term's)."""
    _course_change(ctx, group, apply, remove=netid)


@course_group.command("name")
@click.argument("group")
@click.argument("shown")
@click.option("--new-group", is_flag=True, help="GROUP is not in the groups data yet.")
@click.option("--apply", is_flag=True, help="Write the change (default: only show it).")
@click.pass_context
def name_cmd(ctx, group, shown, apply, new_group):
    """How GROUP is shown ("" to remove the name)."""
    _course_change(ctx, group, apply, new_group, name=shown)


@course_group.command("ignore")
@click.argument("group")
@click.option("--undo", is_flag=True, help="GROUP is a course again.")
@click.option("--new-group", is_flag=True, help="GROUP is not in the groups data yet.")
@click.option("--apply", is_flag=True, help="Write the change (default: only show it).")
@click.pass_context
def ignore_cmd(ctx, group, undo, apply, new_group):
    """GROUP is named like a course but isn't one (a project's or a lab's
    group): no course view, no instructors."""
    _course_change(ctx, group, apply, new_group, ignore=not undo)


@course_group.command("not-teaching")
@click.argument("netid")
@click.option("--undo", is_flag=True, help="NETID may teach again.")
@click.option("--apply", is_flag=True, help="Write the change (default: only show it).")
@click.pass_context
def not_teaching_cmd(ctx, netid, undo, apply):
    """NETID is in the faculty group but teaches no course (staff): left out
    of every course's instructors, unless added to one by name."""
    from nomad.config.courses import CourseError, courses_from, set_not_teaching
    edit = _edit(ctx)
    try:
        changes = set_not_teaching(edit, netid, undo=undo)
    except CourseError as e:
        raise click.ClickException(str(e))
    n = netid.strip().lower()

    def check(d):
        c = courses_from(d)
        return c.enabled and ((n not in c.not_teaching) if undo else (n in c.not_teaching))
    notes = ["The Console reads nomad.toml on every request: no restart."] if changes else []
    _finish(edit, changes, apply, check, notes)


@course_group.command("answers")
@click.option("--db", type=click.Path(), help="Database (default: the hub's, else this host's).")
@click.option("--all-terms", is_flag=True, help="Every term's answers, not only this term's.")
@click.option("--mask", is_flag=True, help="Groups and NetIDs numbered.")
@click.pass_context
def answers_cmd(ctx, db, all_terms, mask):
    """What faculty members said (in the Console) about teaching their
    course groups' courses this term."""
    from datetime import datetime

    from nomad.config import course_answers
    from nomad.config.courses import courses_from
    from nomad.facts.cli import _db
    c = courses_from(ctx.obj.get("config", {}) or {})
    term = c.start(datetime.now().date()).isoformat()
    path = course_answers.path_beside(_db(ctx, db))
    try:
        rows = course_answers.listing(path, None if all_terms else term)
    except course_answers.AnswersError as e:
        raise click.ClickException(str(e))
    if not rows:
        click.echo(f"No answers {'yet' if all_terms else 'for the term from ' + term} "
                   f"({path if not mask else 'course_answers.db'}).")
        return
    groups: dict = {}
    for i, r in enumerate(rows, 1):
        g = groups.setdefault(r["group"], f"course #{len(groups) + 1}") if mask else r["group"]
        who = f"person {i}" if mask else r["netid"]
        click.echo(f"  {r['term']}  {g}: {who} {'teaches it' if r['teaches'] else 'does not'} "
                   f"(said {r['answered_at']})")


@course_group.command("answer")
@click.argument("group")
@click.argument("netid")
@click.option("--yes", "teaches", flag_value="yes", help="NETID teaches GROUP this term.")
@click.option("--no", "teaches", flag_value="no", help="NETID doesn't teach it this term.")
@click.option("--clear", "teaches", flag_value="clear", help="Forget what NETID said.")
@click.option("--db", type=click.Path(), help="Database (default: the hub's, else this host's).")
@click.pass_context
def answer_cmd(ctx, group, netid, teaches, db):
    """Record what NETID said about teaching GROUP this term, as the Console
    does when they answer there (say, they told you by email). Only one of
    the course's faculty members can answer (in its group, or a user of the
    environment named for it); nomad.toml's decisions still win."""
    from datetime import datetime

    from nomad.config import course_answers
    from nomad.config.courses import courses_from
    from nomad.facts.cli import _open
    from nomad.facts.courses import may_answer
    if not teaches:
        raise click.UsageError("give --yes, --no or --clear")
    c = courses_from(ctx.obj.get("config", {}) or {})
    term = c.start(datetime.now().date()).isoformat()
    path, conn = _open(ctx, db)
    try:
        ok, why = may_answer(conn, ctx.obj.get("config", {}) or {}, netid, group.strip())
    finally:
        conn.close()
    if teaches != "clear" and not ok:
        raise click.ClickException(f"{netid} can't answer for {group}: {why}")
    value = None if teaches == "clear" else teaches == "yes"
    try:
        course_answers.record(course_answers.path_beside(path), term, group.strip(), netid, value)
    except course_answers.AnswersError as e:
        raise click.ClickException(str(e))
    said = {"yes": "teaches it", "no": "doesn't teach it", "clear": "answer forgotten"}[teaches]
    click.echo(f"{netid.strip().lower()}, {group.strip()}, term from {term}: {said}.")


def _short(why: str, sites=None, every_term: bool = False) -> str:
    from nomad.config.courses import ACTIVE, ADDED, CONFIRMED, ENVIRONMENT, ONLY
    if why == ONLY:
        return "only faculty member"
    if why == ACTIVE and sites:
        return f"active on {', '.join(sites)}"
    if why == ADDED:
        return "added, every term" if every_term else "added this term"
    if why == CONFIRMED:
        return "said so"
    if why == ENVIRONMENT:
        return "used its environment"
    return why


def _term_note(c, today) -> str | None:
    """When the calendar's term has moved past nomad.toml's term_start."""
    from nomad.config.courses import term_start_for
    cal = term_start_for(today)
    if c.term_start and cal > c.term_start:
        return (f"nomad.toml's term_start is {c.term_start}, and by the calendar a new term "
                f"began on {cal}: once it has, `nomad course setup --term-start YYYY-MM-DD` "
                f"(decisions for the term from {c.term_start} then stop applying)")
    return None


@course_group.command("list")
@click.option("--db", type=click.Path(), help="Database (default: the hub's, else this host's).")
@click.option("--mask", is_flag=True, help="Course groups, names and instructors numbered.")
@click.pass_context
def list_cmd(ctx, db, mask):
    """Each course: its instructors this term and why, and its students
    this term (counts). Courses nobody teaches this term come last."""
    from datetime import datetime

    from nomad.config.courses import courses_from
    from nomad.facts.cli import _open
    from nomad.facts.courses import CourseIndex
    c = courses_from(ctx.obj.get("config", {}) or {})
    if mask and c.problems:
        click.echo(click.style(f"nomad.toml: {len(c.problems)} problem(s) in [console.courses] "
                               "(nomad config check)", fg="yellow"))
    else:
        for p in c.problems:
            click.echo(click.style(f"nomad.toml: {p}", fg="yellow"))
    if not c.enabled:
        click.echo("Courses are off: nomad.toml has no [console.courses]. "
                   "`nomad course setup --term-start YYYY-MM-DD` turns them on.")
        return
    path, conn = _open_masked(ctx, db, mask)
    try:
        now = datetime.now()
        from nomad.config import course_answers
        start = c.start(now.date())
        try:
            said = course_answers.load(course_answers.path_beside(path), start.isoformat())
        except course_answers.AnswersError as e:
            said = {}
            click.echo(click.style(f"{course_answers.FILE if mask else e}: can't read it; what "
                                   "faculty members said is not counted below", fg="yellow"))
        idx = CourseIndex(conn, ctx.obj.get("config", {}) or {}, now, said)
        note = _term_note(c, now.date())
        if note:
            click.echo(click.style(note, fg="yellow"))
        click.echo(f"Courses this term (from {start}, "
                   f"{'nomad.toml' if c.term_start else 'by the calendar'}); "
                   f"from {', '.join(c.sources) or 'listed courses only'}")
        fg = "the faculty group" if mask else repr(c.faculty_group)
        click.echo(f"Instructors: those added by name; of its members in {fg}, "
                   "those who said they teach it this term, else its only one, else those who "
                   "joined its group this term, else those who used its environment this term, "
                   "else those active this term where its students work; less those excluded "
                   "and those who said no.")
        if c.not_teaching:
            who = (f"{len(c.not_teaching)} person(s)" if mask
                   else ", ".join(c.not_teaching))
            click.echo(f"Teach no course unless added to one (not_teaching): {who}")
        if not idx.keys:
            click.echo("  no course: no group or environment matches the pattern"
                       + ("" if mask else f" {c.pattern!r}")
                       + " and none is listed (nomad course discover)")
        nobody = []
        shown = 0
        for g in idx.keys:
            r = idx.roster(g)
            t = r["teaching"]
            envs = sorted(idx.envs_of(g))
            by_env = bool(idx.env_users(g) - idx.faculty())
            students = (f"{len(r['this_term'])} student(s) this term"
                        + (f", {len(r['left_out_earlier'])} from earlier terms"
                           if r["left_out_earlier"] else "")
                        + (f", {len(r['left_out_unknown'])} listed but "
                           + ("not using its environment" if by_env else "not active")
                           + " (joined before nomad recorded join dates)"
                           if r["left_out_unknown"] else ""))
            if envs:
                students += (f"; {len(envs)} environment(s)" if mask
                             else f"; environment {', '.join(envs)}")
            if not idx.members(g):
                students += "; no members in the groups data"
            if not r["instructors"]:
                nobody.append((g, t, students))
                continue
            shown += 1
            label = f"course #{shown}" if mask else c.label(g)
            if mask:
                who = f"{len(r['instructors'])} instructor(s)"
            else:
                sites = idx.activity.faculty_sites.get(g)
                always = set(c.listed[g].instructors) if g in c.listed else set()
                who = ", ".join(f"{n} ({_short(t['why'][n], sites, n in always)})"
                                for n in sorted(r["instructors"]))
            if t["left_out"]:
                who += (f"; {len(t['left_out'])} other faculty member(s) left out this term"
                        + ("" if mask else f" ({', '.join(sorted(t['left_out']))})"))
            if t["said_no"]:
                who += (f"; {len(t['said_no'])} said they don't teach it"
                        + ("" if mask else f" ({', '.join(sorted(t['said_no']))})"))
            click.echo(f"  {label}: {who}; {students}")
        if nobody:
            click.echo("Shown to nobody this term (no instructor; `nomad course add GROUP NETID` "
                       "if someone teaches it):")
            for i, (g, t, students) in enumerate(nobody, 1):
                label = f"group #{i}" if mask else c.label(g)
                if t["undecided"]:
                    why = (f"{len(t['faculty'])} faculty member(s)"
                           + (" besides those who said no" if t["said_no"] else "")
                           + ", none active this term"
                           + ("" if mask else f" ({', '.join(sorted(t['faculty']))})"))
                elif t["said_no"]:
                    why = ("its faculty member(s) said they don't teach it"
                           + ("" if mask else f" ({', '.join(sorted(t['said_no']))})"))
                else:
                    why = "no faculty member among its people"
                click.echo(f"  {label}: {why}; {students}")
    finally:
        conn.close()


class _Names:
    """Names as they are, or numbered (--mask): "group #2", "environment #1",
    "person #3"."""

    def __init__(self, mask: bool):
        self.mask, self.seen = mask, {}

    def __call__(self, kind: str, name: str) -> str:
        if not self.mask:
            return name
        key = (kind, name)
        if key not in self.seen:
            n = sum(1 for k in self.seen if k[0] == kind) + 1
            self.seen[key] = f"{kind} #{n}"
        return self.seen[key]

    def people(self, netids: list) -> str:
        """Faculty members: their NetIDs, or how many."""
        if not netids:
            return "no faculty member"
        if self.mask:
            return _n(len(netids), "faculty member")
        return f"faculty: {', '.join(netids)}"


def _courses_line(d: dict) -> str:
    srcs = ", ".join(d["sources"]) or "listed courses only"
    n, none = d["courses"]["count"], d["courses"]["without_instructor"]
    return (f"The term from {d['term_start']}; courses from {srcs}: {n} course(s) this term"
            + (f", {none} with no instructor yet (nomad course list)" if none else ""))


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


def _own_groups(n: int) -> str:
    return ("1 faculty member's own group has" if n == 1
            else f"{n} faculty members' own groups have")


def _of(part: int, whole: int) -> str:
    if part == whole:
        return f"all {whole}" if whole > 1 else "the one"
    return f"{part} of the {whole}"


def _todo(d: dict, min_active: int) -> list:
    """What to do about what `discover` found: [(command words, why)], a word
    being text or (kind, name); why a function of the namer. A change of
    sources comes first, as one command: --sources sets the whole list.
    Nothing is proposed for an environment several faculty members use
    (shared software, not a class's)."""
    todo: list = []
    sources = list(d["sources"])
    more: list = []                 # (source, why)
    envs = d["environments"]
    # Environments named like a course group become its with the source.
    named = [e for e in envs if not e["courses"] and e["named_like_a_course"]
             and e["group_of_its_name"]]
    coming: set = set()
    if named and "environments" not in sources:
        coming = {e["environment"] for e in named}
        more.append(("environments", lambda nm, named=named: (
            f"{_n(len(named), 'environment')} named like a course group ("
            + ", ".join(nm("environment", e["environment"]) for e in named)
            + "): whoever used it this term one of that course's people")))
    for e in envs:
        if e["courses"] or e["environment"] in coming:
            continue
        others = e["people"] - len(e["faculty"])
        if others < min_active:
            continue
        shared = len(e["faculty"]) > 1
        g = e["group"]
        if e["named_like_a_course"] and not e["group_of_its_name"]:
            if not shared:
                todo.append((["nomad", "course", "env", ("environment", e["environment"]),
                              ("environment", e["environment"])],
                             lambda nm, e=e, others=others: (
                                 f"named like a course, with no group of its name: if one class "
                                 f"uses it, a course of its own, every term ({others} people "
                                 f"besides faculty, {_n(len(e['faculty']), 'faculty member')})")))
            continue
        if g and g["is_course"] and g["people"] * 5 >= others * 4:
            todo.append((["nomad", "course", "env", ("group", g["group"]),
                          ("environment", e["environment"])],
                         lambda nm, g=g, others=others: (
                             f"{_of(g['people'], others)} who used it besides faculty "
                             f"{'is' if g['people'] == 1 else 'are'} in "
                             f"{nm('group', g['group'])}, a course"
                             + (f"; the other {others - g['people']} would be its students too"
                                if others > g["people"] else ""))))
            continue
        if g and g["own_of"] and g["people"] * 2 >= others:
            continue                # its own group is below
        if not shared:
            todo.append((["nomad", "course", "env", "GROUP", ("environment", e["environment"])],
                         lambda nm, others=others: (
                             f"{others} people besides faculty used it this term, most of them "
                             "not in one course's group: if a class uses it, GROUP is that "
                             "course")))
    # Faculty members' own groups with people active this term.
    own_left = 0
    for g in d["groups"]:
        if not g["own_of"]:
            continue
        be = g["environment"]
        if be and be["people"] * 2 >= g["active_this_term"] and (
                be["courses"] or be["environment"] in coming):
            continue        # its people are in a course by its environment
        if "own_groups" not in sources:
            own_left += 1
            continue
        todo.append((["nomad", "course", "add", ("group", g["group"]), ("person", g["own_of"])],
                     lambda nm, g=g: (
                         f"{nm('person', g['own_of'])}'s own group, {g['active_this_term']} "
                         "active this term besides faculty: if it holds their class this term "
                         "(an own group is often a lab too: a course this term only)")))
    if own_left:
        more.append(("own_groups", lambda nm, n=own_left: (
            f"{_own_groups(n)} people active this term: if classes are held in instructors' "
            "own groups (`nomad course discover` then lists which)")))
    if more:
        new = sources + [m for m, _ in more]
        todo.insert(0, (["nomad", "course", "setup", "--sources", ",".join(new)],
                        lambda nm, more=more: "; ".join(why(nm) for _, why in more)
                        + (" (leave out what doesn't apply here)" if len(more) > 1 else "")))
    if any(not g["own_of"] for g in d["groups"]):
        todo.append((["nomad", "course", "add", "GROUP", "NETID"],
                     lambda nm: ("for a group above that holds a class this term (`nomad course "
                                 "setup --pattern` if course groups are named some other way)")))
    return todo


def _show_todo(todo: list, nm) -> None:
    for words, why in todo:
        text = " ".join(w if isinstance(w, str)
                        else (f"<{nm(*w)}>" if nm.mask else _q(w[1])) for w in words)
        click.echo(f"  {text}")
        click.echo(f"      {why(nm)}")


@course_group.command("discover")
@click.option("--db", type=click.Path(), help="Database (default: the hub's, else this host's).")
@click.option("--min-active", type=click.IntRange(1, None), default=3, show_default=True,
              help="Groups with at least this many people active this term besides faculty.")
@click.option("--mask", is_flag=True, help="Groups, environments and NetIDs numbered.")
@click.pass_context
def discover_cmd(ctx, db, min_active, mask):
    """What here may be a course that nomad.toml doesn't say is one yet:
    the environments people had a session in this term (a conda environment,
    a Jupyter kernel), and groups with a faculty member and people active
    this term; then what to do about them. Read-only: each command it
    suggests shows its change first. Best a week or two into a term."""
    from datetime import datetime

    from nomad.config import course_answers
    from nomad.config.courses import courses_from
    from nomad.facts.cli import _open
    from nomad.facts.courses import discover
    config = ctx.obj.get("config", {}) or {}
    c = courses_from(config)
    if not c.enabled:
        click.echo("Courses are off: nomad.toml has no [console.courses]. `nomad course setup` "
                   "turns them on, asking how classes get access here.")
        return
    path, conn = _open_masked(ctx, db, mask)
    try:
        now = datetime.now()
        try:
            said = course_answers.load(course_answers.path_beside(path),
                                       c.start(now.date()).isoformat())
        except course_answers.AnswersError:
            said = {}
        click.echo("Looking at the groups and environments here...", err=True)
        d = discover(conn, config, now, said, min_active=min_active)
    finally:
        conn.close()
    nm = _Names(mask)
    click.echo(_courses_line(d))
    click.echo("\nEnvironments people had a session in this term:")
    if not d["environments_recorded"]:
        click.echo("  not recorded here yet: the interactive servers record them from nomad "
                   "1.12.5 on (upgrade them, then look again after some days of classes)")
    elif not d["environments"]:
        click.echo("  none this term")
    for e in d["environments"]:
        others = e["people"] - len(e["faculty"])
        text = f"  {nm('environment', e['environment'])}: {_n(e['people'], 'person', 'people')}"
        if e["faculty"]:
            text += f" ({nm.people(e['faculty'])})"
        if e["courses"]:
            text += "; counts for " + ", ".join(nm("course", k) for k in e["courses"])
        elif e["named_like_a_course"]:
            text += ("; named like a course, counts for none" if e["group_of_its_name"]
                     else "; named like a course, no group of its name")
        else:
            text += "; counts for no course"
        g = e["group"]
        if g and others:
            text += (f"; {_of(g['people'], others)} besides faculty "
                     f"{'is' if g['people'] == 1 else 'are'} in "
                     f"{nm('group', g['group'])}" + (" (a course)" if g["is_course"] else "")
                     + (f" ({nm('person', g['own_of'])}'s own group)" if g["own_of"] else ""))
        click.echo(text)
    click.echo(f"\nGroups that are not courses, with a faculty member and at least {min_active} "
               "people active this term besides faculty:")
    if not d["groups"]:
        click.echo("  none")
    for g in d["groups"]:
        text = (f"  {nm('group', g['group'])}: {_n(g['members'], 'member')}, "
                f"{g['active_this_term']} active this term besides faculty")
        if g["own_of"]:
            text += f"; {nm('person', g['own_of'])}'s own group"
        text += f"; {nm.people(g['faculty'])}"
        be = g["environment"]
        if be:
            text += (f"; {_of(be['people'], g['active_this_term'])} used "
                     f"{nm('environment', be['environment'])}")
            if be["courses"]:
                text += " (" + ", ".join(nm("course", k) for k in be["courses"]) + "'s)"
        click.echo(text)
    todo = _todo(d, min_active)
    click.echo("\nTo do" + (" (each shows its change first; --apply writes it):" if todo else
                            ": nothing found."))
    _show_todo(todo, nm)


@click.group("interactive")
def interactive_group():
    """Interactive servers (RStudio, JupyterHub): what the interactive
    collector keeps.

    \b
    nomad interactive backfill      rebuild the totals for the weeks before
                                    nomad 1.11 from the raw snapshots
    """


@interactive_group.command("backfill")
@click.option("--db", type=click.Path(), help="This host's database (default: from nomad.toml).")
@click.option("--redo", is_flag=True, help="Replace rows rebuilt earlier.")
@click.option("--apply", is_flag=True, help="Write them (default: only say what would be).")
@click.pass_context
def backfill_cmd(ctx, db, redo, apply):
    """Rebuild the daily and hourly totals for the days before nomad kept
    them (1.11; hours 1.11.1), from the raw snapshots: who was on and for how
    long, and the memory held. Not what was computing: the old snapshots
    can't say. Run it on the interactive server itself (the hub rebuilds its
    copy from the site's at the next sync). Rows written are marked rebuilt."""
    import sqlite3
    from pathlib import Path

    from nomad.collectors.interactive_backfill import rebuild
    if db:
        path = Path(db).expanduser()
    else:
        from nomad.cli import get_db_path
        path = get_db_path(ctx.obj.get("config", {}) or {})
    if not path.exists():
        raise click.ClickException(f"no database at {path}")
    conn = sqlite3.connect(path, timeout=30)
    click.echo(f"Reading the snapshots in {path} (one pass; a few minutes for millions of rows)...",
               err=True)

    def progress(rows, ts):
        click.echo(f"  {rows:,} rows read (now at {str(ts)[:10]})", err=True)
    try:
        result = rebuild(conn, apply=apply, redo=redo, progress=progress)
    finally:
        conn.close()
    if result.get("note"):
        click.echo(result["note"])
    for s in result["servers"]:
        if not s["days"] and not s["hours"]:
            click.echo(f"{s['server']}: nothing before the totals to rebuild")
            continue
        click.echo(f"{s['server']}: {s['days']} day(s) ({s['first_day']} to {s['last_day']}, "
                   f"before {s['before_day'] or 'now'}) and {s['hours']} hour(s) "
                   f"(before {s['before_hour'] or 'now'}); {s['people']} people; readings from "
                   f"{s['readings_from']}")
        if s["names_not_matched"]:
            click.echo(f"  {s['names_not_matched']} name(s) cut short by ps (\"abcdefg+\") "
                       "matched no known name: kept as they are")
    if result["servers"] and not apply:
        click.echo("\nNothing written. Run again with --apply to write them.")
    elif result["servers"]:
        click.echo("\nWritten (marked rebuilt). The hub has them after its next sync.")

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
    nomad course setup --term-start 2026-08-24   turn courses on (or change settings)
    nomad course list                            each course, its instructors and students
    nomad course add GROUP NETID                 NETID teaches GROUP (not in it, or a TA)
    nomad course exclude GROUP NETID             in GROUP and faculty, but not teaching it
    nomad course remove GROUP NETID              off either list
    nomad course name GROUP "Econometrics"       how the course is shown
    nomad course ignore GROUP [--undo]           named like a course, but isn't one
    Changes are shown first; --apply writes them (with a backup).
    `nomad facts course` gives a course's figures.
    """


def _edit(ctx):
    from nomad.cli import _lab_edit
    return _lab_edit(ctx)


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


def _original(edit) -> dict:
    from nomad.config.edit import loads
    return loads(edit.original) if edit.original.strip() else {}


@course_group.command("setup")
@click.option("--term-start", metavar="YYYY-MM-DD", help="This term's first day.")
@click.option("--pattern", help="Which groups are courses, by name (a regular expression).")
@click.option("--faculty-group", help="The group whose members can teach a course.")
@click.option("--min-students", type=click.IntRange(0, None),
              help="Fewer students this term: counts only (default 0).")
@click.option("--names/--no-names", default=None,
              help="Whether instructors see each student by name (default: no).")
@click.option("--apply", is_flag=True, help="Write the change (default: only show it).")
@click.pass_context
def setup_cmd(ctx, term_start, pattern, faculty_group, min_students, names, apply):
    """Turn courses on, or change their settings."""
    from nomad.config.courses import CourseError, courses_from, set_settings
    edit = _edit(ctx)
    try:
        changes = set_settings(edit, term_start=term_start, pattern=pattern,
                               faculty_group=faculty_group, min_students=min_students,
                               names=names)
    except CourseError as e:
        raise click.ClickException(str(e))

    def check(d):
        c = courses_from(d)
        return (c.enabled
                and (term_start is None or c.term_start == _date(term_start))
                and (min_students is None or c.min_students == min_students)
                and (names is None or c.names == names))
    notes = []
    if names:
        notes.append("Instructors will see their students by name: a decision for the "
                     "institution (FERPA).")
    _finish(edit, changes, apply, check, notes)


def _date(text):
    from datetime import date
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _course_change(ctx, group, apply, **kw):
    from nomad.config.courses import CourseError, courses_from, set_course
    edit = _edit(ctx)
    try:
        changes = set_course(edit, group, **kw)
    except CourseError as e:
        raise click.ClickException(str(e))
    g = group.strip()

    def check(d):
        c = courses_from(d).listed.get(g)
        if kw.get("add"):
            return c is not None and kw["add"].strip().lower() in c.instructors
        if kw.get("exclude"):
            return c is not None and kw["exclude"].strip().lower() in c.exclude
        if kw.get("remove"):
            n = kw["remove"].strip().lower()
            return c is None or (n not in c.instructors and n not in c.exclude)
        if kw.get("name") is not None:
            return ((c.name or "") if c else "") == " ".join(kw["name"].split())
        if kw.get("ignore") is not None:
            return (c is not None and c.course is False) if kw["ignore"] else \
                (c is None or c.course)
        return True
    notes = ["The Console reads nomad.toml on every request: no restart."] if changes else []
    _finish(edit, changes, apply, check, notes)


@course_group.command("add")
@click.argument("group")
@click.argument("netid")
@click.option("--apply", is_flag=True, help="Write the change (default: only show it).")
@click.pass_context
def add_cmd(ctx, group, netid, apply):
    """NETID teaches GROUP: when no faculty member is in the group, or for
    a co-instructor or a TA."""
    _course_change(ctx, group, apply, add=netid)


@course_group.command("exclude")
@click.argument("group")
@click.argument("netid")
@click.option("--apply", is_flag=True, help="Write the change (default: only show it).")
@click.pass_context
def exclude_cmd(ctx, group, netid, apply):
    """NETID is in GROUP and in the faculty group but does not teach it."""
    _course_change(ctx, group, apply, exclude=netid)


@course_group.command("remove")
@click.argument("group")
@click.argument("netid")
@click.option("--apply", is_flag=True, help="Write the change (default: only show it).")
@click.pass_context
def remove_cmd(ctx, group, netid, apply):
    """NETID off GROUP's instructors and excluded lists."""
    _course_change(ctx, group, apply, remove=netid)


@course_group.command("name")
@click.argument("group")
@click.argument("shown")
@click.option("--apply", is_flag=True, help="Write the change (default: only show it).")
@click.pass_context
def name_cmd(ctx, group, shown, apply):
    """How GROUP is shown ("" to remove the name)."""
    _course_change(ctx, group, apply, name=shown)


@course_group.command("ignore")
@click.argument("group")
@click.option("--undo", is_flag=True, help="GROUP is a course again.")
@click.option("--apply", is_flag=True, help="Write the change (default: only show it).")
@click.pass_context
def ignore_cmd(ctx, group, undo, apply):
    """GROUP is named like a course but isn't one (a project's or a lab's
    group): no course view, no instructors."""
    _course_change(ctx, group, apply, ignore=not undo)


@course_group.command("list")
@click.option("--db", type=click.Path(), help="Database (default: the hub's, else this host's).")
@click.option("--mask", is_flag=True, help="Course groups, names and instructors numbered.")
@click.pass_context
def list_cmd(ctx, db, mask):
    """Each course: its instructors, and its students this term (counts)."""
    from datetime import datetime

    from nomad.config.access import members_lookup
    from nomad.config.courses import (course_groups, courses_from, first_seen, group_names,
                                      roster)
    from nomad.facts.cli import _open
    from nomad.facts.courses import Activity
    c = courses_from(ctx.obj.get("config", {}) or {})
    for p in c.problems:
        click.echo(click.style(f"nomad.toml: {p}", fg="yellow"))
    if not c.enabled:
        click.echo("Courses are off: nomad.toml has no [console.courses]. "
                   "`nomad course setup --term-start YYYY-MM-DD` turns them on.")
        return
    path, conn = _open(ctx, db)
    try:
        exists, members_of = members_lookup(conn)
        now = datetime.now()
        start = c.start(now.date())
        since = datetime.combine(start, datetime.min.time())
        found = course_groups(c, group_names(conn))
        activity = Activity(conn, since)
        click.echo(f"Courses this term (from {start}, "
                   f"{'nomad.toml' if c.term_start else 'by the calendar'}); "
                   f"instructors: members of {c.faculty_group!r} and those listed")
        if not found:
            click.echo("  no group is a course: none matches the pattern "
                       f"{c.pattern!r} and none is listed")
        for i, g in enumerate(found, 1):
            r = roster(c, g, members_of, activity.of, first_seen(conn, g), since)
            label = f"course #{i}" if mask else c.label(g)
            teach = r["instructors"]
            who = (f"{len(teach)} instructor(s)" if mask else ", ".join(sorted(teach))) \
                if teach else click.style("no instructor (nomad course add GROUP NETID)", fg="yellow")
            line = (f"  {label}: {who}; {len(r['this_term'])} student(s) this term"
                    + (f", {len(r['left_out_earlier'])} from earlier terms"
                       if r["left_out_earlier"] else "")
                    + (f", {len(r['left_out_unknown'])} listed but not active (joined before "
                       "nomad recorded join dates)" if r["left_out_unknown"] else ""))
            if not exists(g):
                line += "; no members in the groups data"
            click.echo(line)
    finally:
        conn.close()


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
    try:
        result = rebuild(conn, apply=apply, redo=redo)
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

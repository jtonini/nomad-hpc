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
    nomad course list                            each course: instructors (and why), students
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
    if term_start and _date(term_start):
        before = courses_from(_original(edit))
        old = before.start().isoformat()                # set, or the calendar's
        if old != _date(term_start).isoformat():
            held = [g for g, x in before.listed.items() if old in x.terms]
            notes.append(f"A new term: decisions for the term from {old} "
                         f"({len(held)} course(s) in nomad.toml) and what faculty members said "
                         "for it stop applying; they are kept as a record.")
    _finish(edit, changes, apply, check, notes)


def _group_known(ctx, group: str) -> bool | None:
    """Does the groups data list GROUP (the hub's combined database, else
    this host's)? None when no database could be read."""
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
                row = conn.execute("SELECT 1 FROM group_membership WHERE group_name = ? LIMIT 1",
                                   (group,)).fetchone()
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
    if not kw.get("remove") and not new_group:
        known = _group_known(ctx, group.strip())
        if known is False:
            raise click.ClickException(
                f"{group}: no such group in the groups data (nomad course list shows the "
                "courses). Check the spelling, or give --new-group for a group not collected yet.")
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
        return True
    notes = ["The Console reads nomad.toml on every request: no restart."] if changes else []
    _finish(edit, changes, apply, check, notes)


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
    the course group's faculty members can answer; nomad.toml's decisions
    still win."""
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
    from nomad.config.courses import ACTIVE, ADDED, CONFIRMED, ONLY
    if why == ONLY:
        return "only faculty member"
    if why == ACTIVE and sites:
        return f"active on {', '.join(sites)}"
    if why == ADDED:
        return "added, every term" if every_term else "added this term"
    if why == CONFIRMED:
        return "said so"
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

    from nomad.config.access import members_lookup
    from nomad.config.courses import (course_groups, courses_from, first_seen, group_names,
                                      roster)
    from nomad.facts.cli import _open
    from nomad.facts.courses import Activity
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
    path, conn = _open(ctx, db)
    try:
        exists, members_of = members_lookup(conn)
        now = datetime.now()
        start = c.start(now.date())
        since = datetime.combine(start, datetime.min.time())
        found = course_groups(c, group_names(conn))
        activity = Activity(conn, since, members_of, c)
        from nomad.config import course_answers
        try:
            said = course_answers.load(course_answers.path_beside(path), start.isoformat())
        except course_answers.AnswersError as e:
            said = {}
            click.echo(click.style(f"{course_answers.FILE if mask else e}: can't read it; what "
                                   "faculty members said is not counted below", fg="yellow"))
        note = _term_note(c, now.date())
        if note:
            click.echo(click.style(note, fg="yellow"))
        click.echo(f"Courses this term (from {start}, "
                   f"{'nomad.toml' if c.term_start else 'by the calendar'})")
        click.echo(f"Instructors: those added by name; of the group's members in "
                   f"{c.faculty_group!r}, those who said they teach it this term, else the one "
                   "member, else those who joined the group this term, else those active this "
                   "term where its students work; less those excluded and those who said no.")
        if c.not_teaching:
            who = (f"{len(c.not_teaching)} person(s)" if mask
                   else ", ".join(c.not_teaching))
            click.echo(f"Teach no course unless added to one (not_teaching): {who}")
        if not found:
            click.echo("  no group is a course: none matches the pattern "
                       f"{c.pattern!r} and none is listed")
        nobody = []
        shown = 0
        for g in found:
            r = roster(c, g, members_of, activity.of, first_seen(conn, g), since,
                       activity.faculty, said.get(g))
            t = r["teaching"]
            students = (f"{len(r['this_term'])} student(s) this term"
                        + (f", {len(r['left_out_earlier'])} from earlier terms"
                           if r["left_out_earlier"] else "")
                        + (f", {len(r['left_out_unknown'])} listed but not active (joined "
                           "before nomad recorded join dates)" if r["left_out_unknown"] else ""))
            if not exists(g):
                students += "; no members in the groups data"
            if not r["instructors"]:
                nobody.append((g, t, students))
                continue
            shown += 1
            label = f"course #{shown}" if mask else c.label(g)
            if mask:
                who = f"{len(r['instructors'])} instructor(s)"
            else:
                sites = activity.faculty_sites.get(g)
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
                    why = "no faculty member in the group"
                click.echo(f"  {label}: {why}; {students}")
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

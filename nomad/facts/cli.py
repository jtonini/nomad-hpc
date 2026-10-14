# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad facts`: the answers the Console's /api/v1 gives, on the command line."""
from __future__ import annotations

import json
from pathlib import Path

import click

TB = 1e12


def _db(ctx, db) -> Path:
    """--db, else the hub's combined database when there is one, else this
    host's own."""
    if db:
        return Path(db).expanduser()
    from nomad.actions.remote import hub_settings
    combined = hub_settings()["combined_db"]
    if combined.exists():
        return combined
    from nomad.cli import get_db_path
    return get_db_path(ctx.obj.get("config", {}) or {})


def _open(ctx, db):
    from nomad.facts import open_readonly
    path = _db(ctx, db)
    try:
        return path, open_readonly(path)
    except FileNotFoundError:
        raise click.ClickException(f"no database at {path} (give one with --db)")


def _size(n) -> str:
    if n is None:
        return "?"
    if n >= TB:
        return f"{n / TB:.1f} TB"
    return f"{n / 1e9:.1f} GB"


def _p(v) -> str:
    return "not measured" if v is None else f"{v:g}%"


def _jobs_line(j: dict | None) -> str:
    if not j:
        return "no job records"
    if j.get("suppressed"):
        return j["why"]
    if not j["finished"]:
        return "no jobs finished"
    o = j["outcomes"]
    parts = [f"{j['finished']} finished"]
    if j["failed_pct"] is not None:
        parts.append(f"{j['failed_pct']:g}% of the {j['ran_to_an_end']} that ran to an end failed, "
                     "timed out or ran out of memory")
    if o.get("node_fail"):
        parts.append(f"{o['node_fail']} lost to node failures")
    c, m, w = j["core_hours"], j["memory"], j["wait_minutes"]
    parts.append(f"core-hours used {_p(c['used_pct'])} ({c['jobs_measured']} measured)")
    mem = f"memory used at peak {_p(m['used_pct'])} ({m['jobs_measured']} measured"
    aside = m["set_aside"]
    parts.append(mem + (f", {aside} impossible peak{'s' if aside != 1 else ''} set aside)"
                        if aside else ")"))
    if w["median"] is not None:
        parts.append(f"median wait {w['median']:g} min (p90 {w['p90']:g})")
    return "; ".join(parts)


def _where(path, mask: bool) -> str:
    """The database, unless masked: its path names whoever ran this."""
    return "" if mask else f" ({path})"


def _mask_sites(data: dict) -> None:
    """Filesystem paths can name a lab or a project (/data/smithlab): numbered."""
    names: dict = {}
    for s in data["sites"]:
        for f in s.get("storage", ()):
            names.setdefault((s["name"], f["path"]), f"filesystem #{len(names) + 1}")
    for s in data["sites"]:
        for f in s.get("storage", ()):
            f["path"] = names[(s["name"], f["path"])]
            for x in f.get("same_as", ()):
                x["path"] = names.get((x["site"], x["path"]), "filesystem")


def _mask_labs(data: dict) -> None:
    """Lab names and groups name a PI; workstations are often named for the
    person at them; storage entries, exports and server names for a lab."""
    for i, lab in enumerate(data["labs"], 1):
        lab["group"] = lab["name"] = f"lab #{i}"
        for j, w in enumerate(lab.get("workstations", ()), 1):
            w["name"] = f"workstation #{j}"
        for j, st in enumerate(lab.get("storage", ()), 1):
            for key in ("entry", "server", "name", "export"):
                if key in st:
                    st[key] = f"storage #{j}"
            if st.get("shares_free_with"):
                st["shares_free_with"] = [f"storage ({len(st['shares_free_with'])} more)"]


def _when(t) -> str:
    return str(t or "?")[:16].replace("T", " ")


def _age(minutes) -> str:
    if minutes is None:
        return "no data"
    if minutes < 120:
        return f"{minutes} min old"
    if minutes < 48 * 60:
        return f"{minutes // 60} h old"
    return f"{minutes // 1440} days old"


@click.group("facts")
def facts_group():
    """nomad's answers about sites, a person and a lab, as the Console's
    /api/v1 gives them.

    \b
    nomad facts sites               every site now, and its jobs (last 30 days)
    nomad facts me NETID            one person's work
    nomad facts lab NETID           the labs that person leads, taken together
    Add --json for the data itself, --mask before sharing the output.
    Reads the hub's combined.db when there is one, else this host's database.
    """


@facts_group.command("sites")
@click.option("--days", default=30, show_default=True, type=click.IntRange(1, 365))
@click.option("--db", type=click.Path(), help="Database (default: the hub's, else this host's).")
@click.option("--json", "as_json", is_flag=True, help="The answer as JSON.")
@click.option("--mask", is_flag=True,
              help="Leave out node reasons, filesystem paths and the database's path.")
@click.pass_context
def sites_cmd(ctx, days, db, as_json, mask):
    """Every site now: freshness, nodes, CPU, memory, GPUs, queue, storage,
    alerts, workstations; and its jobs over --days."""
    from nomad.facts import sites
    path, conn = _open(ctx, db)
    try:
        # The reasons admins gave for unavailable nodes are free text.
        data = sites(conn, days=days, config=ctx.obj.get("config", {}) or {}, reasons=not mask)
    finally:
        conn.close()
    if mask:
        _mask_sites(data)
    if as_json:
        click.echo(json.dumps(data, indent=2))
        return
    click.echo(f"Sites, {_when(data['generated_at'])}{_where(path, mask)}; "
               f"jobs: last {data['window']['days']} days")
    for s in data["sites"]:
        state = {"ok": click.style("ok", fg="green"), "stale": click.style("stale", fg="yellow"),
                 "no_data": click.style("no data", fg="red")}[s["status"]]
        line = f"\n  {s['name']}  {state}, data {_age(s['age_minutes'])}"
        sync = s.get("sync")
        if sync and sync["failing"]:
            line += click.style(f"; sync failing (last success {_when(sync['last_success'])})", fg="red")
        click.echo(line)
        n = s.get("nodes")
        if n:
            text = (f"    nodes {n['total']}: {n['up']} up, {n['drained']} drained, {n['down']} down"
                    + (f", {n['not_reporting']} not reporting" if n["not_reporting"] else ""))
            cpu, mem, gpu = s["cpu"], s["memory"], s["gpus"]
            if cpu["allocated_pct"] is None:
                text += f"; {cpu['cores']} cores, {mem['total_gb']:g} GB (allocation not current)"
            else:
                text += (f"; CPU {cpu['allocated_pct']:g}% allocated of {cpu['cores']} cores"
                         f"; memory {mem['allocated_pct']:g}% allocated of {mem['total_gb']:g} GB")
            if gpu["count"]:
                text += (f"; GPUs {gpu['count']}, {_p(gpu['busy_pct'])} busy"
                         + (f" ({gpu['measured']} measured by DCGM)" if gpu["measured"] else ""))
            click.echo(text)
            q = s["queue"]
            click.echo(f"    now: {q['running']} running, {q['pending']} waiting"
                       + (" (as last reported)" if q["stale"] else ""))
            click.echo(f"    jobs: {_jobs_line(s.get('jobs'))}")
            for u in n["unavailable"]:
                if u.get("reason"):
                    click.echo(f"      {u['node']} {u['condition']}: {u['reason']}")
        w = s.get("workstations")
        if w:
            click.echo(f"    workstations: {w['online']} of {w['total']} online")
        for f in s.get("storage", ()):
            text = f"    {f['path']}: {_p(f['used_pct'])} of {_size(f['total_bytes'])}, {_size(f['free_bytes'])} free"
            if f["days_until_full"] is not None:
                text += f", full in about {f['days_until_full']} days at its 30-day rate"
            if f.get("same_as"):
                text += " (probably the same as " + ", ".join(
                    f"{x['site']} {x['path']}" for x in f["same_as"]) + ")"
            click.echo(text + (" [stale]" if f["stale"] else ""))
        a = s.get("alerts")
        if a and any(a.values()):
            click.echo("    alerts now: " + ", ".join(f"{v} {k}" for k, v in a.items() if v))


@facts_group.command("me")
@click.argument("netid")
@click.option("--days", default=30, show_default=True, type=click.IntRange(1, 365))
@click.option("--db", type=click.Path(), help="Database (default: the hub's, else this host's).")
@click.option("--json", "as_json", is_flag=True, help="The answer as JSON.")
@click.option("--mask", is_flag=True, help="Leave out the NetID, partitions and the database's path.")
@click.pass_context
def me_cmd(ctx, netid, days, db, as_json, mask):
    """One person's work: jobs over --days, running and waiting now, score
    and advice (as the Console's My Activity)."""
    from nomad.facts import person
    path, conn = _open(ctx, db)
    try:
        data = person(conn, netid.strip().lower(), days=days,
                      config=ctx.obj.get("config", {}) or {}, db_path=path)
    finally:
        conn.close()
    if mask:
        data["person"]["username"] = "(this person)"
        for a in data["advice"]:
            a["partition"] = None if a.get("partition") is None else "(partition)"
    if as_json:
        click.echo(json.dumps(data, indent=2))
        return
    who = data["person"]["username"]
    click.echo(f"{who}, last {data['window']['days']} days{_where(path, mask)}")
    click.echo(f"  jobs: {_jobs_line(data['jobs'])}")
    for site, j in (data.get("jobs_by_site") or {}).items():
        click.echo(f"    {site}: {j['finished']} finished")
    now = data["now"]
    click.echo(f"  now: {now['running']} running, {now['pending']} waiting")
    sc = data["score"]
    click.echo("  score: " + ("nothing measured to score" if sc is None else
                              f"{sc['overall']:g}/100 over {sc['jobs_scored']} measured jobs, {sc['trend']}"))
    for a in data["advice"]:
        click.echo(f"  advice ({a['severity']}, {a['dimension']}): {a['advice']}")


@facts_group.command("lab")
@click.argument("netid")
@click.option("--days", default=30, show_default=True, type=click.IntRange(1, 365))
@click.option("--db", type=click.Path(), help="Database (default: the hub's, else this host's).")
@click.option("--json", "as_json", is_flag=True, help="The answer as JSON.")
@click.option("--mask", is_flag=True,
              help="Leave out the NetID, lab names, groups, machine and storage names, and the "
                   "database's path.")
@click.pass_context
def lab_cmd(ctx, netid, days, db, as_json, mask):
    """The labs NETID leads, by nomad.toml's rules: members, their jobs
    taken together, workstations and storage."""
    from nomad.facts import labs
    path, conn = _open(ctx, db)
    try:
        data = labs(conn, ctx.obj.get("config", {}) or {}, netid.strip().lower(), days=days)
    finally:
        conn.close()
    if mask:
        _mask_labs(data)
    if as_json:
        click.echo(json.dumps(data, indent=2))
        return
    who = "this person" if mask else netid
    if not data["labs"]:
        click.echo(f"{who} leads no lab by nomad.toml's rules")
        return
    for lab in data["labs"]:
        click.echo(f"{lab['name']}, last {data['window']['days']} days{_where(path, mask)}")
        m = lab.get("members")
        if m:
            click.echo(f"  members: {m['current']}" + ("" if m["ran_jobs"] is None
                                                     else f", {m['ran_jobs']} ran jobs"))
            j, now = lab.get("jobs"), lab["now"]
            click.echo("  jobs: " + (j["why"] if j and j.get("suppressed") else _jobs_line(j)))
            click.echo("  now: " + (now["why"] if now.get("suppressed") else
                                   f"{now['running']} running, {now['pending']} waiting"))
        ws = lab.get("workstations", [])
        if ws:
            online = sum(1 for w in ws if w.get("online"))
            click.echo(f"  workstations: {online} of {len(ws)} online")
        for s in lab.get("storage", []):
            label = s["entry"] if s["entry"].startswith("storage #") else f"storage {s['entry']}"
            if not s.get("measured"):
                click.echo(f"  {label}: not measured")
            else:
                click.echo(f"  {label}: {_p(s['used_pct'])} used, "
                           f"{_size(s['free_bytes'])} free" + (" [stale]" if s["stale"] else ""))


_BARS = " ▁▂▃▄▅▆▇█"


def _caterpillar(spread: dict | None, top: float, width: int = 30) -> str:
    """One student's weeks as a line: the range (10th to 90th percentile)
    and the typical week (median) on a shared scale."""
    if not spread:
        return " " * (width + 2)
    scale = (width - 1) / top if top > 0 else 0

    def at(v):
        return min(width - 1, max(0, round(v * scale)))
    lo, mid, hi = at(spread["low"]), at(spread["typical"]), at(spread["high"])
    cells = [" "] * width
    for i in range(lo, hi + 1):
        cells[i] = "─"
    if hi > lo:
        cells[lo], cells[hi] = "├", "┤"
    cells[mid] = "●"
    return "|" + "".join(cells) + "|"


def _strip(values: list, top: float) -> str:
    out = []
    for v in values:
        if v is None:
            out.append("·")
        elif v <= 0 or top <= 0:
            out.append(" ")
        else:
            out.append(_BARS[max(1, min(8, round(8 * v / top)))])
    return "".join(out)


def _rows(rows: list, key: str, head: str, other: str | None) -> None:
    """Each student's line: their weeks on a shared scale, busiest first."""
    top = max(((r[key] or {}).get("high") or (r[key] or {}).get("typical") or 0) for r in rows) or 1
    click.echo(f"  each student, {head}, busiest first:")
    width = max(len(r["user"]) for r in rows)
    for r in rows:
        sp = r[key]
        shown = None
        if sp and sp.get("low") is None:
            shown = {"low": sp["typical"], "typical": sp["typical"], "high": sp["typical"]}
        text = f"    {r['user']:<{width}} {_caterpillar(shown or sp, top)}"
        if sp:
            text += f" {sp['typical']:5.1f} h"
            if sp.get("low") is not None:
                text += f" ({sp['low']:.1f}-{sp['high']:.1f})"
        if other and r.get("open_per_week"):
            text += f"; {other} {r['open_per_week']['typical']:.0f} h/week"
        if r["oom_kills"]:
            text += f"; {r['oom_kills']} killed for memory"
        click.echo(text)


def _left_out(s: dict) -> str:
    """Who the group lists but the term's figures leave out, and why."""
    parts = []
    if s.get("left_out_earlier"):
        parts.append(f"{s['left_out_earlier']} from earlier terms")
    if s.get("left_out_unknown"):
        parts.append(f"{s['left_out_unknown']} listed but not active this term: earlier "
                     "students, or some not started yet")
    return f" (not counted: {'; '.join(parts)})" if parts else ""


def _teachers(course: dict) -> str:
    """Instructors with why each one teaches it; or why nobody does."""
    from nomad.config.courses import ACTIVE, WHY
    why = course.get("instructors_why") or {}

    def words(code):
        text = WHY.get(code, code)
        if code == ACTIVE and course.get("active_on"):
            text = f"active this term on {', '.join(course['active_on'])}, where its students work"
        return text
    teach = [f"{n} ({words(why[n])})" if n in why else n for n in course["instructors"]]
    text = ", ".join(teach) if teach else course.get("no_instructor",
                                                    "none found (nomad course add)")
    if course.get("faculty_not_active"):
        text += (f"; {course['faculty_not_active']} other faculty member(s) in the group not "
                 "active this term")
    return text


def _not_shown(data: dict) -> None:
    gone = data.get("not_shown") or ()
    if not gone:
        return
    click.echo(f"\nNot shown to anyone this term ({len(gone)}): nobody teaches them by "
               "nomad.toml's rules (nomad course list)")
    for g in gone:
        click.echo(f"  {g['group']}: {g['students_this_term']} student(s) this term; {g['why']}")


def _mask_courses(data: dict) -> None:
    """Course groups and names can say who teaches them; instructors are NetIDs."""
    if data.get("not_courses"):
        data["not_courses"] = [f"group #{i}" for i in range(1, len(data["not_courses"]) + 1)]
    for i, gone in enumerate(data.get("not_shown", ()), 1):
        gone["group"] = gone["name"] = f"not shown #{i}"
    for i, course in enumerate(data["courses"], 1):
        course["group"] = course["name"] = f"course #{i}"
        course["instructors"] = [f"instructor {j}" for j in range(1, len(course["instructors"]) + 1)]
        if "instructors_why" in course:
            course["instructors_why"] = {f"instructor {j}": why for j, why in
                                         enumerate(course["instructors_why"].values(), 1)}
        if course.get("active_on"):
            course["active_on"] = [f"site {j}" for j in range(1, len(course["active_on"]) + 1)]
        for j, r in enumerate(course.get("each_student", ()), 1):
            r["user"] = f"student {j}"


@facts_group.command("course")
@click.argument("groups", nargs=-1)
@click.option("--instructor", metavar="NETID", help="The courses NETID teaches.")
@click.option("--names", is_flag=True,
              help="Students by name (default: as [console.courses] names says).")
@click.option("--db", type=click.Path(), help="Database (default: the hub's, else this host's).")
@click.option("--json", "as_json", is_flag=True, help="The answer as JSON.")
@click.option("--mask", is_flag=True,
              help="Leave out course groups and names, instructors, students' names and the "
                   "database's path.")
@click.pass_context
def course_cmd(ctx, groups, instructor, names, db, as_json, mask):
    """A course's students this term: the class together, each student as
    a row (their typical week and its range: a caterpillar plot), and when
    the class works. GROUPS: the course groups (default: every course, or
    with --instructor the ones NETID teaches). Needs [console.courses] in
    nomad.toml."""
    from nomad.facts.courses import courses
    if groups and instructor:
        raise click.UsageError("give course groups or --instructor, not both")
    path, conn = _open(ctx, db)
    try:
        data = courses(conn, ctx.obj.get("config", {}) or {},
                       netid=instructor.strip().lower() if instructor else None,
                       groups=list(groups) or None, names=True if names else None)
    finally:
        conn.close()
    if mask:
        _mask_courses(data)
    if as_json:
        click.echo(json.dumps(data, indent=2))
        return
    if not data["courses"]:
        note = (data.get("notes") or [""])[0]
        if note.startswith(("Courses are off", "No database")):
            click.echo(note)
        elif instructor:
            who = "this person" if mask else instructor
            click.echo(f"{who} teaches no course this term by nomad.toml's rules "
                       "(nomad course list shows each course's instructors and why)")
        elif data.get("not_shown"):
            click.echo("no course has an instructor this term")
            _not_shown(data)
        else:
            click.echo("no course found (nomad course list)")
        return
    w = data["window"]
    for course in data["courses"]:
        label = course["group"] if course["name"] == course["group"] else \
            f"{course['group']} ({course['name']})"
        click.echo(f"\n{label}: this term, from {w['term_start']} ({w['term_start_from']})"
                   f"{_where(path, mask)}")
        click.echo(f"  instructors: {_teachers(course)}")
        s = course["students"]
        line = f"  students: {s['this_term']} this term" + _left_out(s)
        if "used_a_server" in s:
            line += f"; {s['used_a_server']} used a server, {s['not_yet']} not yet"
            if s["open_never_computing"]:
                line += f"; {s['open_never_computing']} had sessions open but never computed"
        click.echo(line)
        if course.get("suppressed"):
            click.echo(f"  {course['suppressed']}")
            continue
        it = course["interactive"]
        if it["recorded_since"] is None:
            click.echo("  interactive servers: nothing recorded")
        else:
            text = f"  hours open: {it['hours_open']:g} (recorded since {it['recorded_since']})"
            if it["hours_computing"] is not None:
                text += (f"; computing: {it['hours_computing']:g} "
                         f"(measured since {it['computing_measured_since']})")
            click.echo(text)
        rows = course.get("each_student", [])
        by = it.get("ranked_by", "open")
        if rows and by == "computing":
            sample = next((r["computing_per_week"] for r in rows if r["computing_per_week"]), {})
            if sample.get("estimated"):
                head = (f"hours computing in an average week, from the {sample['days']} whole "
                        "day(s) measured so far (each student's range once two whole weeks are)")
            else:
                head = (f"hours computing per week over {it['weeks_counted']['computing']} "
                        "week(s): typical ● and range ├─┤ (10th-90th percentile)")
            _rows(rows, "computing_per_week", head, "session open")
        elif rows and it["weeks_counted"]["open"]:
            _rows(rows, "open_per_week",
                  f"hours with a session open per week over {it['weeks_counted']['open']} week(s) "
                  "(often a session nobody closed: computing is measured from nomad 1.11 on)",
                  None)
        when = course.get("when")
        if when:
            comp = when.get("students_computing")
            grid, label, since = when["students_on"], "with a session open", when["since"]
            if comp and any(v for row in comp for v in row if v):
                grid, label, since = comp, "computing", when["busy_since"]
            top = max((v for row in grid for v in row if v is not None), default=0)
            if top > 0:
                click.echo(f"  when the class works: students {label}, on average (since "
                           f"{since[:10]}; scale {_BARS[1]} to {_BARS[8]} = up to {top:g})")
                click.echo("        0     6     12    18    ")
                for day, row in zip(when["weekdays"], grid):
                    click.echo(f"    {day} {_strip(row, top)}")
        o = course.get("oom")
        if o and o["kills"]:
            click.echo(f"  memory: {o['kills']} session(s) killed for lack of memory "
                       f"({o['students']} student(s))")
        j = course.get("jobs")
        if j and j["students_with_jobs"]:
            click.echo(f"  clusters: {j['students_with_jobs']} student(s) ran jobs; {_jobs_line(j)}")
    _not_shown(data)

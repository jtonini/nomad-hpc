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

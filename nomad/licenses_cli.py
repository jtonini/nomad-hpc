# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad licenses`: when the licenses of the center's software end, and the
emails before (nomad.licenses, 1.56.0)."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import click

from nomad import licenses as L

ITEMS = ("collectors", "licenses", "items")


def _config(ctx) -> dict:
    return (ctx.obj or {}).get("config") or {}


def _site_db(ctx) -> Path:
    from nomad.cli import get_db_path
    return get_db_path(_config(ctx))


def _combined(ctx) -> Path | None:
    from nomad.actions.remote import hub_settings
    try:
        hs = hub_settings((ctx.obj or {}).get("config_target"))
    except Exception:            # noqa: BLE001 -- no hub here
        return None
    return hs["combined_db"] if hs["sites"] and hs["combined_db"].exists() else None


def _open_ro(path: Path):
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
        conn.execute("SELECT 1")
        return conn
    except sqlite3.Error:
        return None


def _section(ctx) -> dict:
    c = _config(ctx).get("collectors")
    s = c.get("licenses") if isinstance(c, dict) else None
    return s if isinstance(s, dict) else {}


def _print_rows(rows: list, hub: bool) -> None:
    if not rows:
        click.echo("No licenses read yet." + ("" if hub else " List them in [[collectors.licenses.items]] "
                                              "(nomad licenses find shows what this machine has)."))
        return
    wide = max(len(r["name"]) for r in rows)
    site = max(len(r["site"]) for r in rows) if hub else 0
    for r in rows:
        head = (f"{r['site']:<{site}}  " if hub else "") + f"{r['name']:<{wide}}  "
        words = L.ends_words(r)
        if r["state"] == "not renewing":
            words += " (not renewing: no reminders)"
        click.echo(head + words + (f"  [{r['detail']}]" if r["detail"] and r["kind"] != "date" else ""))
        pad = " " * len(head)
        if r["problem"]:
            since = f" since {r['problem_since'][:16].replace('T', ' ')}" if r.get("problem_since") else ""
            click.echo(f"{pad}! can't read it{since}: {r['problem']}")
        elif r["stale"]:
            click.echo(f"{pad}! not read since {str(r['read_at'])[:16].replace('T', ' ')}")
        if r["about"]:
            click.echo(f"{pad}{r['about']}")


@click.group("licenses", invoke_without_command=True)
@click.option("--here", is_flag=True, help="This machine's own (on the hub: not every site's).")
@click.option("--json", "as_json", is_flag=True, help="As JSON.")
@click.pass_context
def licenses_group(ctx, here=False, as_json=False):
    """When the licenses of the center's software end -- read where the
    software keeps the date -- and an email to the center before each does.
    nomad never changes a license.

    \b
    nomad licenses                    every license and when it ends (on the hub: every site's)
    nomad licenses read [NAME]        read this machine's now (and keep it)
    nomad licenses find               what this machine seems to have, as `add` lines
    nomad licenses add NAME KIND ...  list one here ([[collectors.licenses.items]]); --apply
    nomad licenses renew NAME no      not being renewed: listed, no reminders; --apply
    nomad licenses remind [--send]    on the hub: what is due, and the email
    nomad licenses timer [--apply]    on the hub: remind every morning, from systemd
    nomad licenses tries              whether licensed programs run on the lab machines or nodes (1.59.0)

    KIND: posit, flexlm, gurobi, cert, command, date. Settings: docs/licenses.md."""
    if ctx.invoked_subcommand is not None:
        return
    combined = None if here else _combined(ctx)
    path = combined or _site_db(ctx)
    conn = _open_ro(path) if path.exists() else None
    rows = L.listing(conn) if conn is not None else []
    if as_json:
        click.echo(json.dumps(rows, indent=1, default=str))
        return
    if combined is None:
        items, problems = L.items_of(_section(ctx))
        for p in problems:
            click.echo(f"note: {p}", err=True)
        read = {r["name"] for r in rows}
        for it in items:
            if it.name not in read:
                click.echo(f"note: {it.name} isn't read yet (nomad licenses read)", err=True)
    else:
        for p in L.settings(_config(ctx)).problems:
            click.echo(f"note: {p}", err=True)
    _print_rows(rows, hub=combined is not None)
    if rows:
        c = L.counts(rows)
        click.echo(f"{c['all']} license{'s' if c['all'] != 1 else ''}: {c['ended']} ended, {c['soon']} "
                   f"end{'s' if c['soon'] == 1 else ''} within {L.SOON_DAYS} days, {c['problems']} can't be read.")


@licenses_group.command("read")
@click.argument("name", required=False)
@click.pass_context
def licenses_read(ctx, name=None):
    """Read this machine's licenses now (or NAME's), keep what was read, and
    say it. The collector does it a few times a day on its own."""
    items, problems = L.items_of(_section(ctx))
    for p in problems:
        click.echo(f"note: {p}", err=True)
    if not items:
        raise click.ClickException("no [[collectors.licenses.items]] in this machine's nomad.toml "
                                   "(nomad licenses find shows what it has)")
    if name is not None:
        items = [it for it in items if it.name.lower() == name.strip().lower()]
        if not items:
            raise click.ClickException(f"no license named {name!r} here")
    readings = {it.name: L.read(it) for it in items}
    rows = _keep(ctx, L.items_of(_section(ctx))[0], readings, only=name is not None)
    _print_rows(rows, hub=False)
    if any(r["problem"] for r in rows):
        sys.exit(1)


@licenses_group.command("find")
@click.option("--root", "roots", multiple=True, metavar="FOLDER",
              help="Look under this folder (again for more); default: the usual software folders.")
@click.pass_context
def licenses_find(ctx, roots):
    """What licenses this machine seems to have (Posit's programs, FlexLM and
    Gurobi license files, web certificates here), each as the `nomad
    licenses add` line that lists it. Changes nothing; shows no keys."""
    found = L.find(tuple(roots) or L.FIND_ROOTS)
    have = {it.name.lower() for it in L.items_of(_section(ctx))[0]}
    have_src = {it.source() for it in L.items_of(_section(ctx))[0]}
    if not found:
        click.echo("Nothing found here.")
        return
    target = (ctx.obj or {}).get("config_target")
    for e in found:
        it = L.item_from(dict(e), "found", [])
        listed = e["name"].lower() in have or (it is not None and it.source() in have_src)
        click.echo(f"{e['name']} ({L.SAID_KIND[e['kind']]})" + (": listed already" if listed else ""))
        if not listed:
            click.echo("  " + L.add_line(e, target))
    click.echo("Name each as people know it (\"Ansys and Lumerical\"); add --about \"...\" to say what it is "
               "and who renews it. Each `add` shows what it would write; --apply writes it.")


def _options_body(kind, program, file, host, port, command, pattern, ends, about, renew, sudo, remind_days, timeout):
    body: dict = {"kind": kind}
    for key, val in (("program", program), ("file", file), ("host", host), ("pattern", pattern), ("ends", ends)):
        if val:
            body[key] = val
    if port is not None:
        body["port"] = port
    if command:
        body["command"] = command
    if about:
        body["about"] = about
    if not renew:
        body["renew"] = False
    if sudo is False:
        body["sudo"] = False
    if remind_days:
        try:
            body["remind_days"] = [int(x) for x in remind_days.replace(" ", "").split(",") if x]
        except ValueError:
            raise click.UsageError("--remind-days: whole numbers of days, such as 60,30,14") from None
    if timeout is not None:
        body["timeout"] = timeout
    return body


def _listed(data: dict) -> list:
    c = data.get("collectors") if isinstance(data.get("collectors"), dict) else {}
    s = c.get("licenses") if isinstance(c.get("licenses"), dict) else {}
    return s.get("items") if isinstance(s.get("items"), list) else []


def _same_name(entry, name: str) -> bool:
    return isinstance(entry, dict) and isinstance(entry.get("name"), str) and \
        " ".join(entry["name"].split()).lower() == name.lower()


def _keep(ctx, all_items: list, readings: dict, only: bool) -> list:
    """Keep these readings in this machine's database; ``only``: the others
    stay as they were (those not read now). The rows read, as listed."""
    db = _site_db(ctx)
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db), timeout=30)
        try:
            if only:
                conn.executescript(L.SCHEMA)
                keep = {r[0] for r in conn.execute(f"SELECT name FROM {L.TABLE}")}
                kept = [it for it in all_items if it.name in keep or it.name in readings]
                L.store(conn, kept, readings)
            else:
                L.store(conn, all_items, readings)
            return [r for r in L.listing(conn) if r["name"] in readings]
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise click.ClickException(f"{db}: {e}") from None


def _write(ctx, name: str, item: L.Item, replace: bool, apply: bool, tag: str) -> None:
    from nomad.cli import _lab_edit
    from nomad.config.edit import loads
    edit = _lab_edit(ctx)
    data = edit.data
    body = item.body()
    old = [e for e in _listed(data) if _same_name(e, name)]
    if old:
        now = L.item_from(old[0], "nomad.toml", [])
        if now is not None and now.body() == body:
            click.echo(f"{name} is listed already, as said. Nothing to change.")
            return
        if not replace:
            raise click.ClickException(f"{name} is listed already, differently: --replace to replace it")
        edit.remove_entries(ITEMS, name=old[0]["name"])
    section = _section(ctx)
    if "enabled" not in section:
        edit.insert_key(ITEMS[:2], "enabled", True)
    elif section.get("enabled") is not True:
        click.echo("note: [collectors.licenses] says enabled = false: nothing is read on its own until it is true",
                   err=True)
    edit.add_entry(ITEMS, body)

    def check(d):
        got = [L.item_from(e, "nomad.toml", []) for e in _listed(d) if _same_name(e, name)]
        return len(got) == 1 and got[0] is not None and got[0].body() == body

    if not check(loads(edit.text)):
        raise click.ClickException("the edit didn't read back as meant: nothing written")
    from nomad.config.edit import table_text
    block = table_text(ITEMS, body, array=True)
    if not apply:
        click.echo(f"Would {'replace' if old else 'add'}, in {edit.path}:\n")
        click.echo(block)
        click.echo("Run again with --apply to write it.")
        return
    try:
        keep = edit.save(check, tag=tag)
    except ValueError as e:
        raise click.ClickException(f"nothing written: {e}") from None
    click.echo(f"{'Replaced' if old else 'Added'} {name} in {edit.path}" + (f" (the file before: {keep.name})"
                                                                            if keep else "") + ".")
    # Read and kept at once (1.59.2): the Licenses page has it after the hub's next sync,
    # not after the collector's next reading hours later.
    now_items = L.items_of((loads(edit.text).get("collectors") or {}).get("licenses") or {})[0]
    mine = next((it for it in now_items if _same_name({"name": it.name}, name)), item)
    rd = L.read(mine)
    _keep(ctx, now_items, {mine.name: rd}, only=True)
    said = L.ends_words({"ends": rd.ends, "never": rd.never}) if not rd.problem else f"can't: {rd.problem}"
    click.echo(f"Read now and kept: {said}" + (f" [{rd.detail}]" if rd.detail and mine.kind != "date" else "")
               + ("" if mine.renew else " (not being renewed: no reminders)"))
    click.echo("The hub has it after its next sync.")


@licenses_group.command("add")
@click.argument("name")
@click.argument("kind", type=click.Choice(L.KINDS))
@click.option("--program", help="posit: rstudio-server's or license-manager's full path.")
@click.option("--file", help="flexlm, gurobi: the license file (a pattern too, such as /opt/x/*.lic).")
@click.option("--host", help="cert: the host name on the certificate.")
@click.option("--port", type=int, help="cert: the port (default 443).")
@click.option("--command", "command", multiple=True, help="command: the program, then each word (again for each).")
@click.option("--pattern", help="command: where the date is in what it prints (a regular expression).")
@click.option("--ends", help="date: when it ends (2027-06-30), or never.")
@click.option("--about", help="What it is, who renews it: a few words.")
# Plain flags: click 8.3 reads a flag whose value is False as False when it isn't given (1.57.1).
@click.option("--no-renew", "no_renew", is_flag=True, help="Not being renewed: listed, no reminders.")
@click.option("--no-sudo", "no_sudo", is_flag=True, help="posit: run license-manager without sudo.")
@click.option("--remind-days", help="Its own reminders, such as 90,30,7 (default: the hub's).")
@click.option("--timeout", type=int, help="posit, command: seconds to wait (default 60).")
@click.option("--replace", is_flag=True, help="Replace the entry of that name.")
@click.option("--apply", is_flag=True, help="Write it (otherwise only show it).")
@click.pass_context
def licenses_add(ctx, name, kind, program, file, host, port, command, pattern, ends, about, no_renew, no_sudo,
                 remind_days, timeout, replace, apply):
    """List a license in this machine's nomad.toml: a
    [[collectors.licenses.items]] entry (and [collectors.licenses] enabled
    = true when it isn't set). Shows it first; --apply writes it, with a copy
    of the file before, and reads it once."""
    body = _options_body(kind, program, file, host, port, list(command), pattern, ends, about, not no_renew,
                         False if no_sudo else None, remind_days, timeout)
    problems: list = []
    item = L.item_from({"name": name, **body}, "the entry", problems)
    if item is None or problems:
        raise click.UsageError("; ".join(problems) or "not an entry nomad reads")
    gone = L.missing(item)
    if gone:
        raise click.ClickException(f"{gone}: nothing written (list it on the machine that has it)")
    _write(ctx, item.name, item, replace, apply, "licenses-add")


@licenses_group.command("renew")
@click.argument("name")
@click.argument("answer", type=click.Choice(["yes", "no"]))
@click.option("--apply", is_flag=True, help="Write it (otherwise only show it).")
@click.pass_context
def licenses_renew(ctx, name, answer, apply):
    """Whether NAME is being renewed: no keeps it listed without reminders
    (renew = false in its entry); yes reminds again."""
    found = [e for e in _listed(_config(ctx)) if _same_name(e, name)]
    if not found:
        raise click.ClickException(f"no license named {name!r} in this machine's nomad.toml "
                                   "(it is listed where it is read: nomad licenses shows the machine)")
    item = L.item_from(found[0], "nomad.toml", [])
    if item is None:
        raise click.ClickException(f"{name}'s entry doesn't read (nomad config check)")
    want = answer == "yes"
    if item.renew == want:
        click.echo(f"{item.name}: renew is {str(want).lower()} already. Nothing to change.")
        return
    from dataclasses import replace as changed
    _write(ctx, item.name, changed(item, renew=want), True, apply, "licenses-renew")


def _hub(ctx):
    combined = _combined(ctx)
    if combined is None:
        raise click.ClickException("no combined database here: run it on the hub")
    s = L.settings(_config(ctx))
    for p in s.problems:
        click.echo(f"note: {p}", err=True)
    return combined, s, L.db_path(s, combined)


@licenses_group.command("remind")
@click.option("--send", is_flag=True, help="Send it (otherwise only say what is due).")
@click.option("--text", "show_text", is_flag=True, help="Print the email as it would go.")
@click.pass_context
def licenses_remind(ctx, send, show_text):
    """On the hub: what is due now (a license ending within a reminder's
    days, one that ended lately, one nomad can't read), in one email to the
    center; --send sends it and keeps what went, so nothing goes twice."""
    combined, s, rec_path = _hub(ctx)
    conn = _open_ro(combined)
    if conn is None:
        raise click.ClickException(f"{combined} can't be read")
    rec = L.open_record(rec_path, write=send)
    out = L.remind(conn, s, rec, datetime.now(), dry=not send, config=_config(ctx))
    if not out["notes"] and not out.get("tries"):
        click.echo("Nothing due now.")
        return
    from nomad import license_tries as T
    for n in out.get("tries") or []:
        r = n["row"]
        said = f"doesn't run: {r.get('reason') or r['result']}" if n["why"] == "notrun" else "runs again"
        click.echo(f"  {r.get('site') or ''}: {T.label(r)} on {r['machine']} ({r['install']}): {said}")
    for n in out["notes"]:
        r = n.row
        what = {"ends": L.ends_words(r), "ended": L.ends_words(r), "unread": f"can't be read: {r['problem']}",
                "stale": f"not read since {str(r['read_at'])[:16].replace('T', ' ')}"}[n.why]
        click.echo(f"  {r['site']}: {r['name']}: {what}")
    click.echo(f"{out['status']}: {out['subject']!r} to {', '.join(out['to']) or '(nobody)'}"
               + (f": {out['error']}" if out["error"] else ""))
    if show_text:
        click.echo(f"\n--- Subject: {out['subject']}\n\n{out['text']}")
    if out["status"] in ("failed", "can't send"):
        sys.exit(1)


@licenses_group.command("timer")
@click.option("--user", "account", help="The account it runs as (default: nomad, if there is one).")
@click.option("--at", "when", default="*-*-* 08:20", show_default=True,
              help="When, as systemd writes it (OnCalendar).")
@click.option("--apply", is_flag=True, help="Install and start it (as root).")
@click.option("--force", is_flag=True, help="Replace a timer installed with other settings.")
@click.pass_context
def licenses_timer(ctx, account, when, apply, force):
    """On the hub: a systemd timer that sends what is due every morning
    (`nomad licenses remind --send`) as the nomad account (or --user).
    Without --apply it prints the two files."""
    from nomad.timers import install
    install(ctx, "nomad-licenses", "NOMAD license reminders", "licenses remind --send", when, account, apply,
            force)


# -- whether licensed programs run on the lab machines (1.59.0) ---------------------------------

TRIES = ("collectors", "license_tries", "programs")


def _tries_settings(ctx):
    """The site's [collectors.license_tries], with the workstations it would try."""
    from nomad import license_tries as T
    from nomad.collectors.plan import plan
    for p in plan(_config(ctx)):
        if p.name == "license_tries":
            return T.settings(p.config), p.enabled
    return T.settings({}), False


def _when(at) -> str:
    try:
        d = datetime.fromisoformat(str(at))
    except (TypeError, ValueError):
        return str(at or "")
    return f"{d.day} {d.strftime('%b')} {d.strftime('%H:%M')}"


def _print_tries(data: dict, hub: bool) -> None:
    from nomad import license_tries as T
    groups = T.summary(data)
    reached = [h for h in data.get("hosts", []) if h.get("reached")]
    if not groups and reached:
        click.echo(f"No install of the programs listed (as new as min_version) on the {len(reached)} "
                   "machine(s) reached.")
    elif not groups:
        click.echo("No tries kept yet." + ("" if hub else " Set [collectors.license_tries] "
                                           "(nomad licenses tries setup), then nomad licenses tries now."))
    for g in groups:
        where = f"{g['site']}: " if hub and g["site"] else ""
        n = len(g["runs"]) + len(g["not"])
        click.echo(f"{g['label']} ({where}{g['install']}): runs on {len(g['runs'])} of {n}")
        by: dict = {}
        for m, res, why, since in g["not"]:
            by.setdefault((res, why), []).append((m, since))
        for (res, why), ms in sorted(by.items(), key=lambda kv: -len(kv[1])):
            verb = "doesn't start on" if res == "nostart" else "doesn't run on"
            first = min((s for _m, s in ms if s), default=None)
            click.echo(f"   {verb} {len(ms)}: {', '.join(m for m, _s in ms)}: {why}"
                       + (f" (since {_when(first)})" if first else ""))
    gone = [h for h in data.get("hosts", []) if not h.get("reached")]
    if gone:
        click.echo("Not reached at the last try: " + ", ".join(
            f"{h['machine']}" + (f" (since {_when(h['reached_at'])})" if h.get("reached_at") else "")
            for h in sorted(gone, key=lambda h: h["machine"])))
    last = max((h.get("tried_at") or "" for h in data.get("hosts", [])), default="")
    if last:
        click.echo(f"Tried last: {_when(last)}.")


@licenses_group.group("tries", invoke_without_command=True)
@click.option("--site", help="On the hub: only this site's.")
@click.option("--json", "as_json", is_flag=True, help="As JSON.")
@click.pass_context
def licenses_tries(ctx, site, as_json):
    """Whether licensed programs run on the lab machines (or a cluster's
    nodes): a tiny job with each install of each program listed, on each
    machine, twice a day ([collectors.license_tries], docs/licenses.md).

    \b
    nomad licenses tries              what ran where, at the last try (on the hub: every site's)
    nomad licenses tries now          on the site: try now (and keep it)
    nomad licenses tries setup NAME RECIPE --min-version 6.4 [--nodes] [--apply]
                                      on the site: list a program to try (RECIPE: qchem);
                                      --nodes: on every node of this cluster's Slurm"""
    if ctx.invoked_subcommand is not None:
        return
    from nomad import license_tries as T
    combined = _combined(ctx)
    path = combined or _site_db(ctx)
    conn = _open_ro(path) if path.exists() else None
    data = T.listing(conn, site)
    if as_json:
        click.echo(json.dumps(data, indent=1, default=str))
        return
    if combined is None:
        s, on = _tries_settings(ctx)
        for p in s.problems:
            click.echo(f"note: {p}", err=True)
        if s.programs and not on:
            click.echo("note: [collectors.license_tries] isn't enabled: nothing is tried on its own", err=True)
    _print_tries(data, hub=combined is not None)


@licenses_tries.command("now")
@click.option("--machine", "machines", multiple=True, metavar="HOST", help="Only this machine (again for more).")
@click.pass_context
def licenses_tries_now(ctx, machines):
    """On the site: try each program on each machine now (not waiting for the
    collector's turn), keep what came, and say it."""
    from nomad import license_tries as T
    s, _on = _tries_settings(ctx)
    for p in s.problems:
        click.echo(f"note: {p}", err=True)
    if not s.programs:
        raise click.ClickException("no [[collectors.license_tries.programs]] here (nomad licenses tries setup)")
    why = T.resolve(s)
    if why:
        raise click.ClickException(why)
    if machines:
        unknown = [m for m in machines if m not in s.machines]
        if unknown:
            raise click.ClickException(f"not among this site's machines: {', '.join(unknown)}")
        s.machines = list(machines)
    if not s.machines:
        raise click.ClickException('no machines: [collectors.workstation] workstations, machines = [...] '
                                   'or "slurm"')
    click.echo(f"Trying {', '.join(p.name for p in s.programs)} on {len(s.machines)} machine(s), "
               f"{min(s.at_once, len(s.machines))} at a time...")
    db = _site_db(ctx)
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        with T.one_round(db):
            results = T.try_all(s)
            conn = sqlite3.connect(str(db), timeout=30)
            try:
                T.store(conn, results, s.programs, keep_others=bool(machines))
                data = T.listing(conn, machines=None if not machines else {m.split(".")[0].lower() for m in machines})
            finally:
                conn.close()
    except T.Busy:
        raise click.ClickException("another round of tries is going on (the collector's): "
                                   "nomad licenses tries shows it once it ends") from None
    except sqlite3.Error as e:
        raise click.ClickException(f"{db}: {e}") from None
    _print_tries(data, hub=False)


@licenses_tries.command("setup")
@click.argument("name")
@click.argument("recipe")
@click.option("--min-version", help="Leave out installs older than this (6.4: Q-Chem 6.4 and newer).")
@click.option("--nodes", is_flag=True, help='Try on every node of this cluster\'s Slurm (machines = "slurm").')
@click.option("--replace", is_flag=True, help="Replace the entry of that name.")
@click.option("--apply", is_flag=True, help="Write it (otherwise only show it).")
@click.pass_context
def licenses_tries_setup(ctx, name, recipe, min_version, nodes, replace, apply):
    """List a program to try on this site's lab machines (or, with --nodes,
    on every node of its cluster): a [[collectors.license_tries.programs]]
    entry (and [collectors.license_tries] enabled = true when it isn't set;
    machines = "slurm" with --nodes). Shows it first; --apply writes it, with
    a copy of the file before."""
    from nomad import license_tries as T
    from nomad.cli import _lab_edit
    from nomad.config.edit import loads, table_text
    body: dict = {"name": name, "recipe": recipe.lower()}
    if min_version:
        body["min_version"] = str(min_version)
    s = T.settings({"programs": [body]})
    if s.problems or not s.programs:
        raise click.UsageError("; ".join(s.problems) or "not an entry nomad reads")
    edit = _lab_edit(ctx)

    def listed(d) -> list:
        c = d.get("collectors") if isinstance(d.get("collectors"), dict) else {}
        sec = c.get("license_tries") if isinstance(c.get("license_tries"), dict) else {}
        return [e for e in (sec.get("programs") or []) if isinstance(e, dict)
                and str(e.get("name", "")).strip().lower() == name.strip().lower()]

    old = listed(edit.data)
    sec0 = ((edit.data.get("collectors") or {}).get("license_tries") or {})
    wants_nodes = nodes and str(sec0.get("machines", "")).strip().lower() != "slurm"
    if old:
        if {k: old[0].get(k) for k in body} == body and set(old[0]) <= set(body) and not wants_nodes:
            click.echo(f"{name} is listed already, as said. Nothing to change.")
            return
        if not replace and not ({k: old[0].get(k) for k in body} == body and set(old[0]) <= set(body)):
            raise click.ClickException(f"{name} is listed already, differently: --replace to replace it")
        edit.remove_entries(TRIES, name=old[0]["name"])
    if wants_nodes:
        for i, table, k in edit.key_lines():
            if table == TRIES[:2] and k == ("machines",):
                try:
                    edit.remove_line(i)
                except ValueError:
                    raise click.ClickException("[collectors.license_tries] machines goes on over several lines: "
                                               'set machines = "slurm" by hand') from None
                break
        edit.insert_key(TRIES[:2], "machines", "slurm")
    sec = ((edit.data.get("collectors") or {}).get("license_tries") or {})
    if "enabled" not in sec:
        edit.insert_key(TRIES[:2], "enabled", True)
    elif sec.get("enabled") is not True:
        click.echo("note: [collectors.license_tries] says enabled = false: nothing is tried on its own "
                   "until it is true", err=True)
    edit.add_entry(TRIES, body)

    def check(d):
        got = listed(d)
        ok = len(got) == 1 and {k: got[0].get(k) for k in body} == body
        if nodes:
            ok = ok and str(((d.get("collectors") or {}).get("license_tries") or {}).get("machines")) == "slurm"
        return ok

    if not check(loads(edit.text)):
        raise click.ClickException("the edit didn't read back as meant: nothing written")
    if not apply:
        click.echo(f"Would {'replace' if old else 'add'}, in {edit.path}:\n")
        if wants_nodes:
            click.echo('[collectors.license_tries]\nmachines = "slurm"\n')
        click.echo(table_text(TRIES, body, array=True))
        click.echo("Run again with --apply to write it.")
        return
    try:
        keep = edit.save(check, tag="license-tries")
    except ValueError as e:
        raise click.ClickException(f"nothing written: {e}") from None
    click.echo(f"{'Replaced' if old else 'Added'} {name} in {edit.path}"
               + (f" (the file before: {keep.name})" if keep else "") + ".")
    from nomad.collectors.plan import plan
    st = next((T.settings(p.config) for p in plan(loads(edit.text)) if p.name == "license_tries"), T.settings({}))
    T.resolve(st)
    where = "node(s) (Slurm's, asked again each time)" if st.slurm else "machine(s)"
    click.echo(f"It will be tried on {len(st.machines)} {where} twice a day; now: nomad licenses tries now")

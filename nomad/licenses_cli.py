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
    db = _site_db(ctx)
    try:
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db), timeout=30)
        try:
            all_items = L.items_of(_section(ctx))[0]
            if name is not None:
                # only these read: the others stay as they were
                conn.executescript(L.SCHEMA)
                keep = {r[0] for r in conn.execute(f"SELECT name FROM {L.TABLE}")}
                kept = [it for it in all_items if it.name in keep or it.name in readings]
                L.store(conn, kept, readings)
            else:
                L.store(conn, all_items, readings)
            rows = [r for r in L.listing(conn) if r["name"] in readings]
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise click.ClickException(f"{db}: {e}") from None
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
    if tag == "licenses-add":
        rd = L.read(item)
        said = L.ends_words({"ends": rd.ends, "never": rd.never}) if not rd.problem else f"can't: {rd.problem}"
        click.echo(f"Read now: {said}" + (f" [{rd.detail}]" if rd.detail and item.kind != "date" else ""))
        click.echo("The collector keeps it from its next run (or now: nomad licenses read).")


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
@click.option("--no-renew", "renew", flag_value=False, default=True, help="Not being renewed: listed, no reminders.")
@click.option("--no-sudo", "sudo", flag_value=False, default=None, help="posit: run license-manager without sudo.")
@click.option("--remind-days", help="Its own reminders, such as 90,30,7 (default: the hub's).")
@click.option("--timeout", type=int, help="posit, command: seconds to wait (default 60).")
@click.option("--replace", is_flag=True, help="Replace the entry of that name.")
@click.option("--apply", is_flag=True, help="Write it (otherwise only show it).")
@click.pass_context
def licenses_add(ctx, name, kind, program, file, host, port, command, pattern, ends, about, renew, sudo, remind_days,
                 timeout, replace, apply):
    """List a license in this machine's nomad.toml: a
    [[collectors.licenses.items]] entry (and [collectors.licenses] enabled
    = true when it isn't set). Shows it first; --apply writes it, with a copy
    of the file before, and reads it once."""
    body = _options_body(kind, program, file, host, port, list(command), pattern, ends, about, renew, sudo,
                         remind_days, timeout)
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
    if not out["notes"]:
        click.echo("Nothing due now.")
        return
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

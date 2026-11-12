# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad storage cold`: data unchanged for a year, and the cases about it (1.44.0).

On the hub, as the account the Console runs as (the cases are kept in its
lab_check.db): nomad.storage.cold says how it works.
"""
from __future__ import annotations

import json

import click

from nomad import lab_check as L
from nomad import tickets as T
from nomad.storage import cold as C


def _ctx(ctx):
    from nomad.lab_check_cli import _ctx as lab_ctx
    config, report, cs, conn, rec_path = lab_ctx(ctx)
    s = C.settings(config)
    for p in s.problems:
        click.echo(f"note: {p}", err=True)
    return config, cs, conn, rec_path, s


def _number(s: C.Settings, text: str) -> int:
    n = T.number_of(text, s.tickets)
    if n is None:
        raise click.ClickException(f"{text!r} is not a case's number (like {C.ref_of(13, s)}, or 13)")
    return n


def _where(text: str) -> tuple[str, str]:
    server, sep, path = str(text).partition(":")
    if not sep or not server or not path.startswith("/"):
        raise click.BadParameter(f"{text!r}: SERVER:/PATH, as the list says it (fs1:/home)")
    return server, path


def _print(got: dict, s: C.Settings) -> None:
    for p in got["paths"]:
        if p["nearly_full"]:
            click.echo(f"NEARLY FULL: {p['server']}:{p['path']} is {round(100 * p['full_share'])}% full; "
                       f"{C.tb(p['listed_bytes'])} of it is on the list.")
    click.echo(f"{got['people']} people hold {C.tb(got['total_bytes'])} unchanged for {s.days} days or more "
               f"(at least {s.min_tb:g} TB each on a path).")
    for r in got["rows"]:
        case = r.get("case")
        labs = ", ".join(x["lab"] + (" (PI)" if x["pi"] else "") for x in r["labs"]) or "in no lab group now"
        click.echo(f"\n{r['person']:<12} {r['server']}:{r['path']}  {C.tb(r['unchanged_bytes'])} of "
                   f"{C.tb(r['bytes'])} unchanged {r['unchanged_days']} days; last change "
                   f"{(r['last_change'] or '–')[:10]}; last job {(r['last_job'] or 'none')[:10]}")
        click.echo(f"    {labs}" + (f"; lab check: {r['lab_check']['said']} ({r['lab_check']['lab']}, "
                                    f"{r['lab_check']['at'][:10]})" if r.get("lab_check") else ""))
        if case:
            click.echo(f"    {case['ref']}: {case['said']} ({(case.get('updated_at') or case['opened_at'])[:10]})"
                       + (f": {case['reason']}" if case.get("reason") else ""))
    for m in got["not_measured"]:
        click.echo(f"\nnot measured: {m}")


@click.group("cold", invoke_without_command=True)
@click.pass_context
def cold_group(ctx):
    """Data unchanged for a year: who holds at least [console.cold_data]
    min_tb of it on a path, on every scanned storage server, with their last
    job, labs and what the lab check said of them; and the cases Research
    Computing opens to talk with them about it (numbered like account
    requests: ARC-0013).

    \b
    nomad storage cold [list] [--server S] [--json | --csv]     the list
    nomad storage cold open PERSON SERVER:/PATH [--note "..."] a case, to contact them
    nomad storage cold case ARC-0013 [STATE] [--note "..."]    it moves on, or a note
    nomad storage cold show ARC-0013                           one case and its notes
    nomad storage cold digest [--send]                         the weekly email to Research Computing
    nomad storage cold timer [--apply]                         send it weekly (a systemd timer)

    STATE: to_contact, contacted, moving, keep (a note says why), moved.
    Run as the account the Console runs as (sudo -u nomad nomad storage cold ...).
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(cold_list)


@cold_group.command("list")
@click.option("--server", help="One server's.")
@click.option("--json", "as_json", is_flag=True, help="As JSON.")
@click.option("--csv", "as_csv", is_flag=True, help="As a spreadsheet, a row each (it names people).")
@click.pass_context
def cold_list(ctx, server=None, as_json=False, as_csv=False):
    """The list, largest first."""
    if as_json and as_csv:
        raise click.UsageError("--json or --csv, not both")
    config, cs, conn, rec_path, s = _ctx(ctx)
    got = C.listing(conn, C.open_records(rec_path), config, s)
    if server:
        got["rows"] = [r for r in got["rows"] if r["server"] == server]
        got["paths"] = [p for p in got["paths"] if p["server"] == server]
        got["total_bytes"] = sum(r["unchanged_bytes"] for r in got["rows"])
        got["people"] = len({r["person"] for r in got["rows"]})
    if as_csv:
        click.echo(C.csv_text(got), nl=False)
    elif as_json:
        click.echo(json.dumps(got, indent=2, default=str))
    else:
        _print(got, s)


def _write(rec_path):
    try:
        return C.open_records(rec_path, write=True)
    except L.CheckError as exc:
        raise click.ClickException(str(exc)) from None


@cold_group.command("open")
@click.argument("person")
@click.argument("where", metavar="SERVER:/PATH")
@click.option("--note", help="A note: what was said, to whom.")
@click.option("--by", help="Who opens it (default: who runs this).")
@click.pass_context
def cold_open(ctx, person, where, note, by):
    """Open a case about PERSON's data on SERVER:/PATH, to contact them."""
    config, cs, conn, rec_path, s = _ctx(ctx)
    server, path = _where(where)
    rec = _write(rec_path)
    row = next((r for r in C.listing(conn, rec, config, s)["rows"]
                if (r["person"], r["server"], r["path"]) == (person, server, path)), None)
    if row is None:
        click.echo(f"note: {person} on {server}:{path} isn't on the list now; the case is opened all the same",
                   err=True)
    try:
        c = C.open_case(rec, person, server, path, by=by or "", note=note, row=row, s=s)
    except C.CaseError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"{c['ref']}: {person} on {server}:{path}, {c['said']}.")


@cold_group.command("case")
@click.argument("number")
@click.argument("state", required=False, type=click.Choice(C.STATES))
@click.option("--note", help="What happened; for keep, why it is kept (needed).")
@click.option("--by", help="Who (default: who runs this).")
@click.pass_context
def cold_case(ctx, number, state, note, by):
    """A case moves on to STATE, or gets a note (or both)."""
    config, cs, conn, rec_path, s = _ctx(ctx)
    n = _number(s, number)
    try:
        c = C.update_case(_write(rec_path), n, by=by or "", state=state, note=note, s=s)
    except C.CaseError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"{c['ref']}: {c['said']}" + (f" ({c['reason']})" if c.get("reason") else "") + ".")


@cold_group.command("show")
@click.argument("number")
@click.option("--json", "as_json", is_flag=True, help="As JSON.")
@click.pass_context
def cold_show(ctx, number, as_json):
    """One case, and its notes."""
    config, cs, conn, rec_path, s = _ctx(ctx)
    c = C.case_of(C.open_records(rec_path), _number(s, number), s)
    if c is None:
        raise click.ClickException(f"no case {C.ref_of(_number(s, number), s)}")
    if as_json:
        click.echo(json.dumps(c, indent=2))
        return
    click.echo(f"{c['ref']} {c['owner']} {c['server']}:{c['path']}: {c['said']}"
               + (f" ({c['reason']})" if c.get("reason") else ""))
    click.echo(f"  opened {c['opened_at'][:16].replace('T', ' ')} by {c['opened_by']}"
               + (f", then {C.tb(c['unchanged_at_open'])} unchanged of {C.tb(c['bytes_at_open'])}"
                  if c.get("unchanged_at_open") else ""))
    for x in c["notes"]:
        click.echo(f"  {x['at'][:16].replace('T', ' ')} {x['by']}: " + (C.SAID[x["state"]] if x["state"] else "note")
                   + (f": {x['note']}" if x["note"] else ""))


@cold_group.command("digest")
@click.option("--send", is_flag=True, help="Email it to [console.cold_data] notify (else print it).")
@click.pass_context
def cold_digest(ctx, send):
    """The weekly email to Research Computing: any server nearly full, what
    is new on the list, the open cases and those not updated lately, and
    the largest not yet a case. Sending marks what was new."""
    config, cs, conn, rec_path, s = _ctx(ctx)
    rec = _write(rec_path) if send else C.open_records(rec_path)
    got = C.listing(conn, rec, config, s)
    if send:
        new = C.note_seen(rec, got)
    else:
        new = [r for r in got["rows"] if not r["on_list_since"]]
    subject, text = C.digest(got, new, C.cases(rec, s, open_only=True), s)
    if not send:
        click.echo(f"Subject: {subject}\nTo: {', '.join(s.notify) or '(no one: [console.cold_data] notify)'}\n")
        click.echo(text, nl=False)
        return
    said = C.send_digest(config, cs, s, subject, text)
    click.echo(f"{subject}: {said}.")
    if said != "sent":
        raise SystemExit(1)


@cold_group.command("timer")
@click.option("--at", "when", default="Mon *-*-* 07:40:00", show_default=True,
              help="When, as systemd's OnCalendar says it.")
@click.option("--user", "account", help="The account it runs as (default: nomad).")
@click.option("--apply", is_flag=True, help="Install and start it (as root); else print the units.")
@click.option("--force", is_flag=True, help="Replace units that differ.")
@click.pass_context
def cold_timer(ctx, when, account, apply, force):
    """Send the weekly email from a systemd timer (nomad-cold-data.timer)."""
    from nomad import timers
    timers.install(ctx, "nomad-cold-data", "NOMAD weekly email about data unchanged for a year",
                   "storage cold digest --send", when, account, apply, force, every_word="weekly")

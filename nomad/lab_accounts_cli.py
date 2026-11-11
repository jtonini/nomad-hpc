# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad lab accounts`: a lab's PI asks ARC for accounts (1.42.0).

On the hub, as the account the Console runs as (the requests are kept in its
lab_check.db, and the directory's password file is its own):
nomad.account_requests says how it works.
"""
from __future__ import annotations

import json
from pathlib import Path

import click

from nomad import account_requests as A
from nomad import directory as D
from nomad import lab_check as L
from nomad.lab_check_cli import _ctx


def _settings(config) -> A.Settings:
    s = A.settings(config)
    for p in s.problems:
        click.echo(f"note: {p}", err=True)
    return s


def _directory(config) -> D.Directory:
    try:
        return D.Directory(D.settings(config))
    except D.DirectoryError as exc:
        raise click.ClickException(str(exc)) from None


def _sites(s: A.Settings, conn) -> list:
    return list(s.sites) or A.sites_with_nodes(conn)


@click.group("accounts")
def accounts_group():
    """A lab's PI asks for accounts for their students, on My lab in the
    Console: nomad says who each one is, from the campus directory, and ARC
    is emailed. ARC makes the accounts as it does now, and closes each one
    here (the PI is emailed). nomad makes no account itself.

    \b
    nomad lab accounts check             can nomad ask the directory? (as the nomad account)
    nomad lab accounts lookup TEXT...    who each NetID or name is, as a PI would see it [--file F]
    nomad lab accounts list [--open] [--lab LAB]   the requests, and what became of each account
    nomad lab accounts close ID NETID SITE made|not_made [--note "..."]   ARC made it, or not (why)

    Settings: [directory] and [console.labs.accounts] in the hub's nomad.toml
    (docs/lab-accounts.md). Run as the account the Console runs as
    (sudo -u nomad nomad lab accounts ...).
    """


@accounts_group.command("check")
@click.pass_context
def accounts_check(ctx):
    """Can nomad ask the directory? A search for no one, as the service
    account, with nothing printed of the password."""
    config = (ctx.obj or {}).get("config") or {}
    ds = D.settings(config)
    for p in ds.problems:
        click.echo(f"note: {p}", err=True)
    try:
        got = D.Directory(ds).check()
    except D.DirectoryError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"The directory answers: {got['uri']}, people under {got['base']}, as {got['as']}.")
    s = _settings(config)
    click.echo("Account requests are " + ("on" if s.enabled else "off ([console.labs.accounts] enabled = false)")
               + "; new requests are emailed to " + (", ".join(s.notify) or "no one (set notify)") + ".")


@accounts_group.command("lookup")
@click.argument("text", nargs=-1)
@click.option("--file", "file_", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              help="A table (CSV, tab-separated, .xlsx) or a list, one per line.")
@click.option("--json", "as_json", is_flag=True, help="As JSON.")
@click.pass_context
def accounts_lookup(ctx, text, file_, as_json):
    """Who each NetID, address or name is, as a PI checking a request
    would see it: found (and their name), several of that name, not found;
    and where nomad sees them with an account already. Each TEXT is a line
    ("Jane Doe", "Doe, Jane", jdoe)."""
    config, report, cs, conn, rec_path = _ctx(ctx)
    s = _settings(config)
    lines = "\n".join(text)
    if file_:
        try:
            lines += "\n" + A.table_text(file_.name, file_.read_bytes())
        except A.RequestError as exc:
            raise click.ClickException(str(exc)) from None
    entries = A.parse_text(lines, cs.mail_domain)
    if not entries:
        raise click.UsageError("give NetIDs or names, or --file")
    try:
        rows = A.resolve(entries, _directory(config), conn, A.open_records(rec_path), _sites(s, conn))
    except D.DirectoryError as exc:
        raise click.ClickException(str(exc)) from None
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    for r in rows:
        if r["status"] == "found":
            extra = (f"; has an account on {', '.join(r['has'])}" if r.get("has") else "") + \
                    (f"; already asked for on {', '.join(r['asked'])}" if r.get("asked") else "")
            click.echo(f"  {r['text'][:30]:<30} {r['netid']:<12} {r['name']}{extra}")
        elif r["status"] == "choose":
            click.echo(f"  {r['text'][:30]:<30} several: "
                       + "; ".join(f"{c['netid']} {c['name']}" for c in r["choices"]))
        else:
            said = {"not_found": "not found", "too_many": "too many of that name", "need_first": "a surname alone",
                    "not_understood": "not understood"}[r["status"]]
            click.echo(f"  {r['text'][:30]:<30} {said}" + (f": {r['why']}" if r.get("why") else ""))


@accounts_group.command("list")
@click.option("--open", "open_only", is_flag=True, help="Only requests with accounts still to make.")
@click.option("--lab", help="One lab's (its group).")
@click.option("--days", default=365, show_default=True, help="Requests of the last this many days.")
@click.option("--json", "as_json", is_flag=True, help="As JSON.")
@click.pass_context
def accounts_list(ctx, open_only, lab, days, as_json):
    """The requests, newest first, and what became of each account."""
    _, _, _, _, rec_path = _ctx(ctx)
    got = A.requests(A.open_records(rec_path), lab=lab, open_only=open_only, days=days)
    if as_json:
        click.echo(json.dumps(got, indent=2))
        return
    if not got:
        click.echo("No requests" + (" with accounts still to make." if open_only else "."))
        return
    for r in got:
        click.echo(f"#{r['id']} {r['at'][:16].replace('T', ' ')} {r['lab']} by {r['by']}: "
                   f"{r['open']} of {len(r['people'])} still to make" + (f"; note: {r['note']}" if r["note"] else ""))
        for p in r["people"]:
            done = "" if p["state"] == "asked" else \
                f" ({p['closed_by']}, {p['closed_at'][:10]}" + (f": {p['closed_note']}" if p["closed_note"] else "") + ")"
            click.echo(f"    {p['netid']:<12} {(p['name'] or '')[:30]:<30} {p['site']:<12} {A.SAID[p['state']]}{done}")


@accounts_group.command("close")
@click.argument("request", type=int)
@click.argument("netid")
@click.argument("site")
@click.argument("state", type=click.Choice(["made", "not_made"]))
@click.option("--note", help="Why it was not made (needed then); or a word for the PI.")
@click.option("--no-email", is_flag=True, help="Don't email the PI.")
@click.option("--by", help="Who made it (default: who runs this).")
@click.pass_context
def accounts_close(ctx, request, netid, site, state, note, no_email, by):
    """ARC made the account, or not (and why): the request keeps who
    closed it and when, and the PI is emailed (review_to while set)."""
    config, report, cs, conn, rec_path = _ctx(ctx)
    s = _settings(config)
    try:
        rec = A.open_records(rec_path, write=True)
    except L.CheckError as exc:
        raise click.ClickException(str(exc)) from None
    req = A.request_of(rec, request)
    if req is None:
        raise click.ClickException(f"no request #{request}")
    try:
        done = A.close(rec, request, netid, site, state, by=by or "", note=note)
    except A.RequestError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"#{request} {done['netid']} on {site}: {A.SAID[state]}.")
    if not no_email:
        click.echo(f"Email to {req['by']}: {A.tell_pi(config, cs, s, req, [done])}.")

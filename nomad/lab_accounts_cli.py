# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad lab accounts`: a lab's PI asks ARC for accounts (1.42.0).

On the hub, as the account the Console runs as (the requests are kept in its
lab_check.db, and the directory's password file is its own):
nomad.account_requests says how it works.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
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

    Each request has a number, as in a ticket system: ARC-0012 ([console.tickets] prefix).

    \b
    nomad lab accounts check             can nomad ask the directory? (as the nomad account)
    nomad lab accounts lookup TEXT...    who each NetID or name is, as a PI would see it [--file F]
    nomad lab accounts list [--open] [--lab LAB] [--find TEXT]   the requests, and what became of each account
    nomad lab accounts show ARC-0012     one request
    nomad lab accounts close ARC-0012 NETID SITE made|not_made [--note "..."]   ARC made it, or not (why)
    nomad lab accounts report [--since DATE] [--until DATE] [--cluster C] [--json | --csv]

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


def _print_requests(got: list) -> None:
    for r in got:
        click.echo(f"{r['ref']} {r['at'][:16].replace('T', ' ')} {r['lab']} by {r['by']}: "
                   f"{r['open']} of {len(r['people'])} still to make" + (f"; note: {r['note']}" if r["note"] else ""))
        for p in r["people"]:
            done = "" if p["state"] == "asked" else \
                f" ({p['closed_by']}, {p['closed_at'][:10]}" + (f": {p['closed_note']}" if p["closed_note"] else "") + ")"
            click.echo(f"    {p['netid']:<12} {(p['name'] or '')[:30]:<30} {p['site']:<12} {A.SAID[p['state']]}{done}")


def _number(s: A.Settings, text: str) -> int:
    from nomad import tickets as T
    n = T.number_of(text, s.tickets)
    if n is None:
        raise click.ClickException(f"{text!r} is not a request's number (like {A.ref_of(12, s)}, or 12)")
    return n


@accounts_group.command("list")
@click.option("--open", "open_only", is_flag=True, help="Only requests with accounts still to make.")
@click.option("--lab", help="One lab's (its group).")
@click.option("--days", default=365, show_default=True, help="Requests of the last this many days.")
@click.option("--find", "find", metavar="TEXT",
              help="Those of any age with this number (ARC-0012), NetID, name, lab or PI (any part of one).")
@click.option("--json", "as_json", is_flag=True, help="As JSON.")
@click.pass_context
def accounts_list(ctx, open_only, lab, days, find, as_json):
    """The requests, newest first, and what became of each account."""
    config, _, _, _, rec_path = _ctx(ctx)
    s = _settings(config)
    rec = A.open_records(rec_path)
    if find:
        got = A.find(rec, find, s, lab=lab)
        if open_only:
            got = [r for r in got if r["open"]]
    else:
        got = A.requests(rec, lab=lab, open_only=open_only, days=days, s=s)
    if as_json:
        click.echo(json.dumps(got, indent=2))
        return
    if not got:
        click.echo("No requests" + (f" found by {find!r}." if find else
                                    " with accounts still to make." if open_only else "."))
        return
    _print_requests(got)
    if find and len(got) >= A.FIND_MOST:
        click.echo(f"(the newest {A.FIND_MOST}: say more to find fewer)")


@accounts_group.command("show")
@click.argument("request")
@click.option("--json", "as_json", is_flag=True, help="As JSON.")
@click.pass_context
def accounts_show(ctx, request, as_json):
    """One request, by its number (ARC-0012, or 12), and what became of each account."""
    config, _, _, _, rec_path = _ctx(ctx)
    s = _settings(config)
    got = A.find(A.open_records(rec_path), A.ref_of(_number(s, request), s), s)
    if not got:
        raise click.ClickException(f"no request {A.ref_of(_number(s, request), s)}")
    if as_json:
        click.echo(json.dumps(got[0], indent=2))
    else:
        _print_requests(got)


@accounts_group.command("close")
@click.argument("request")
@click.argument("netid")
@click.argument("site")
@click.argument("state", type=click.Choice(["made", "not_made"]))
@click.option("--note", help="Why it was not made (needed then); or a word for the PI.")
@click.option("--no-email", is_flag=True, help="Don't email the PI.")
@click.option("--by", help="Who made it (default: who runs this).")
@click.pass_context
def accounts_close(ctx, request, netid, site, state, note, no_email, by):
    """ARC made the account, or not (and why): the request keeps who
    closed it and when, and the PI is emailed (review_to while set).
    REQUEST: its number, ARC-0012 (or 12)."""
    config, report, cs, conn, rec_path = _ctx(ctx)
    s = _settings(config)
    number = _number(s, request)
    try:
        rec = A.open_records(rec_path, write=True)
    except L.CheckError as exc:
        raise click.ClickException(str(exc)) from None
    req = A.request_of(rec, number, s)
    if req is None:
        raise click.ClickException(f"no request {A.ref_of(number, s)}")
    try:
        done = A.close(rec, number, netid, site, state, by=by or "", note=note, s=s)
    except A.RequestError as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"{req['ref']} {done['netid']} on {site}: {A.SAID[state]}.")
    if not no_email:
        click.echo(f"Email to {req['by']}: {A.tell_pi(config, cs, s, req, [done])}.")


def _day(text: str | None, default: date) -> date:
    if not text:
        return default
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise click.BadParameter(f"{text!r}: a date, like 2026-07-01") from None


def _figures(t: dict) -> str:
    return (f"{t['accounts']} accounts for {t['people']} {'person' if t['people'] == 1 else 'people'}: "
            f"made {t['made']}, not made {t['not_made']}, withdrawn {t['withdrawn']}, still to make {t['open']}")


def _speed(d: dict | None) -> str:
    if not d:
        return "none made yet"
    return f"median {d['median']:g} days, 9 in 10 within {d['p90']:g}, longest {d['longest']:g}"


@accounts_group.command("report")
@click.option("--since", metavar="DATE", help="From this day (default: a year before --until).")
@click.option("--until", metavar="DATE", help="To this day, included (default: today).")
@click.option("--cluster", "site", metavar="SITE", help="One cluster's accounts.")
@click.option("--json", "as_json", is_flag=True, help="The figures, as JSON.")
@click.option("--csv", "as_csv", is_flag=True,
              help="One row per account instead, for a spreadsheet (it names people: Research Computing's own).")
@click.pass_context
def accounts_report(ctx, since, until, site, as_json, as_csv):
    """The requests asked in a period, and what became of each account as
    of now: in all, by cluster, lab and month, and how long making them took
    (from asked to marked made)."""
    if as_json and as_csv:
        raise click.UsageError("--json or --csv, not both")
    config, _, _, _, rec_path = _ctx(ctx)
    s = _settings(config)
    last = _day(until, date.today())
    first = _day(since, last - timedelta(days=365))
    if first > last:
        raise click.BadParameter("--since is after --until")
    t0 = datetime.combine(first, datetime.min.time())
    t1 = datetime.combine(last + timedelta(days=1), datetime.min.time())
    rec = A.open_records(rec_path)
    if as_csv:
        click.echo(A.csv_text(A.records(rec, t0, t1, site, s)), nl=False)
        return
    r = A.report(rec, t0, t1, site, s)
    if as_json:
        click.echo(json.dumps(r, indent=2))
        return
    tot = r["totals"]
    click.echo(f"Account requests asked {first} to {last}" + (f", on {site}" if site else "")
               + f"; what became of each, as of {r['as_of'][:16].replace('T', ' ')}:")
    if not tot["requests"]:
        click.echo("  none.")
    else:
        click.echo(f"  {tot['requests']} requests from {tot['labs']} labs ({tot['pis']} PIs): {_figures(tot)}")
        click.echo(f"  Days from asked to made: {_speed(tot['days_to_make'])}"
                   + (f" ({r['slowest']['ref']}, {r['slowest']['lab']} on {r['slowest']['site']})"
                      if r["slowest"] else ""))
        for title, key, rows in (("By cluster", "site", r["by_cluster"]), ("By lab", "lab", r["by_lab"]),
                                 ("By month", "month", r["by_month"])):
            click.echo(f"\n{title}:")
            click.echo(f"  {'':<16} {'requests':>8} {'accounts':>8} {'made':>5} {'not made':>8} {'withdrawn':>9} "
                       f"{'open':>5} {'median days':>11}")
            for x in rows:
                med = x["days_to_make"]["median"] if x["days_to_make"] else None
                click.echo(f"  {str(x[key])[:16]:<16} {x['requests']:>8} {x['accounts']:>8} {x['made']:>5} "
                           f"{x['not_made']:>8} {x['withdrawn']:>9} {x['open']:>5} "
                           f"{'' if med is None else format(med, 'g'):>11}")
    o = r["open_now"]
    click.echo("\nStill to make now" + (f" on {site}" if site else "") + ": "
               + (f"{o['accounts']} accounts in {o['requests']} requests; the oldest {o['oldest']['ref']}, "
                  f"asked {o['oldest']['days']:g} days ago." if o["accounts"] else "none."))

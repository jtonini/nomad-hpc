# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad notices`: the weekly email to each person about their own use
(nomad.notices, 1.39.0). Run on the hub."""
from __future__ import annotations

import getpass
import os
import sys

import click

from nomad import notices as N

_REPORT_PROBLEM: dict = {}


def _ctx(ctx):
    """nomad.toml, report.toml (None when it can't be read), the settings,
    the combined database (read-only; None without one) and its path, and
    the notices' record path."""
    config = (ctx.obj or {}).get("config") or {}
    target = (ctx.obj or {}).get("config_target")
    report = None
    _REPORT_PROBLEM.clear()
    try:
        from nomad.condo.config import CondoConfigError, load
        try:
            report = load(None, target)
        except CondoConfigError as e:
            _REPORT_PROBLEM["why"] = str(e)
    except ImportError:
        pass
    s = N.settings(config, report, _REPORT_PROBLEM.get("why"))
    from nomad.actions.remote import hub_settings
    combined = hub_settings(target)["combined_db"]
    conn = None
    if combined.exists():
        from nomad.facts import open_readonly
        conn = open_readonly(combined)
    return config, s, conn, combined, N.db_path(s, combined)


def _problems(s):
    for p in s.problems:
        click.echo(f"note: {p}", err=True)


def _who() -> str:
    return os.environ.get("SUDO_USER") or getpass.getuser()


@click.group("notices", invoke_without_command=True)
@click.pass_context
def notices_group(ctx):
    """A weekly email to each person about their own use: what of theirs a
    login node's monitoring caught, and a pattern of jobs asking for far
    more than they use, with the better way and a link to My activity.

    \b
    nomad notices                          how it is set, and what went lately
    nomad notices mail [--user NETID] [--text]   what is due now (nothing is sent)
    nomad notices mail --send              send it (while review_to is set, to review_to)
    nomad notices set NETID on|off [--apply]     a person's choice, for them
    nomad notices timer [--apply]          send it every week, from systemd

    On for everyone by default; a person turns it off on My activity.
    Accounts only in a course are left out ([notices] courses). Settings:
    [notices] in nomad.toml (docs/notices.md)."""
    if ctx.invoked_subcommand is not None:
        return
    _, s, _, _, rec_path = _ctx(ctx)
    _problems(s)
    st = N.status(s, N.open_db(rec_path))
    sending = {"held": "nothing is sent (see the notes above)",
               "review": f"for review, to {', '.join(st['review_to'])}",
               "people": "to people"}[st["sending"]]
    click.echo(f"What it tells: {', '.join(st['kinds']) or 'nothing (kinds = [])'}; "
               f"at most every {st['every_days']} days a person.")
    click.echo(f"Emails go {sending}.")
    click.echo(f"Last 30 days: {st['sent_30_days']} sent to people, {st['review_30_days']} for review"
               + (f"; the last {st['last'][:16].replace('T', ' ')}" if st["last"] else "") + ".")
    click.echo(f"People who turned it off: {st['stopped']}.")


@notices_group.command("mail")
@click.option("--send", is_flag=True, help="Send them (otherwise only say what is due).")
@click.option("--user", "only", metavar="NETID", help="Only this person's (the others stay due).")
@click.option("--text", "show_text", is_flag=True, help="Without --send: print each email as it would go.")
@click.option("--counts", is_flag=True, help="Counts only, no names (to show someone else).")
@click.pass_context
def notices_mail(ctx, send, only, show_text, counts):
    """The emails due now: each person with something new since the last one,
    at most every every_days. While review_to is set, they go there."""
    config, s, conn, combined, rec_path = _ctx(ctx)
    _problems(s)
    if conn is None:
        raise click.ClickException(f"no combined database at {combined}: run it on the hub")
    if only is not None and N.clean_netid(only) is None:
        raise click.UsageError(f"--user {only!r}: a NetID")
    if send:
        if s.held:
            raise click.ClickException("nothing is sent: [notices] review_to is not decided "
                                       "(see the notes above)")
        rec = N.open_db(rec_path, write=True)
    else:
        rec = N.open_db(rec_path)
    p = N.send(conn, combined, config, s, rec, dry=not send, only=N.clean_netid(only) if only else None)
    if counts:
        by: dict = {}
        notes = {"login_node": 0, "jobs": 0}
        for m in p["mail"]:
            by[m["status"]] = by.get(m["status"], 0) + 1
            for e in m["entries"]:
                notes[e.kind] += 1
        click.echo(f"Emails: {', '.join(f'{v} {k}' for k, v in by.items()) or 'none due'}"
                   + (f" (to {', '.join(s.review_to)}, for review)" if s.reviewing and p["mail"] else "") + ".")
        click.echo(f"Notes in them: {notes['login_node']} on the login nodes, {notes['jobs']} about jobs.")
        left = {k: v for k, v in p["left"].items() if v}
        click.echo("Not emailed: " + (", ".join(f"{v} {k}" for k, v in left.items()) or "none") + ".")
        if any(m["status"] in ("can't send", "failed") for m in p["mail"]):
            sys.exit(1)
        return
    if not p["mail"]:
        click.echo("Nothing due now" + (f" for {only}" if only else "") + ".")
    for m in p["mail"]:
        to = ", ".join(m["to"]) or "(no address)"
        why = f": {m.get('problem') or m.get('error')}" if m["status"] in ("can't send", "failed", "held") else ""
        kinds = {}
        for e in m["entries"]:
            kinds[e.kind] = kinds.get(e.kind, 0) + 1
        what = ", ".join(f"{n} {'on the login nodes' if k == 'login_node' else 'about jobs'}"
                         for k, n in kinds.items())
        click.echo(f"  {m['status']}: {m['user']} ({what}) to {to}" + why)
    left = {k: v for k, v in p["left"].items() if v}
    if left and not only:
        click.echo("Not emailed: " + ", ".join(f"{v} {k}" for k, v in left.items()) + ".")
    if show_text and not send:
        for m in p["mail"]:
            click.echo(f"\n--- To: {', '.join(m['to']) or '(no address)'}\nSubject: {m['subject']}\n\n{m['text']}")
    if not send and p["mail"]:
        click.echo("Nothing was sent: --send sends them" + (" (to review_to)" if s.reviewing else "") + ".")
    if any(m["status"] in ("can't send", "failed") for m in p["mail"]):
        sys.exit(1)


@notices_group.command("set")
@click.argument("netid")
@click.argument("choice", type=click.Choice(["on", "off"]))
@click.option("--apply", is_flag=True, help="Record it (otherwise only say what would change).")
@click.pass_context
def notices_set(ctx, netid, choice, apply):
    """NETID is emailed (on, the default) or not (off): for someone who asked
    by email. People choose for themselves on My activity in the Console."""
    _, s, _, _, rec_path = _ctx(ctx)
    n = N.clean_netid(netid)
    if not n:
        raise click.UsageError(f"{netid!r} is not a NetID")
    now = N.wants_email(N.open_db(rec_path), n)
    want = choice == "on"
    if now == want:
        click.echo(f"{n} is {'emailed' if now else 'not emailed'} already: nothing to change.")
        return
    if not apply:
        click.echo(f"{n} would be {'emailed' if want else 'no longer emailed'}. Run again with --apply to do it.")
        return
    try:
        N.set_email(N.open_db(rec_path, write=True), n, want, by=_who())
    except (N.NoticesError, OSError) as e:
        raise click.ClickException(str(e)) from None
    click.echo(f"{n} is {'emailed again' if want else 'no longer emailed'}.")


@notices_group.command("timer")
@click.option("--user", "account", help="The account it runs as (default: nomad, if there is one).")
@click.option("--at", "when", default="Mon *-*-* 09:10", show_default=True,
              help="When, as systemd writes it (OnCalendar).")
@click.option("--apply", is_flag=True, help="Install and start it (as root).")
@click.option("--force", is_flag=True, help="Replace a timer installed with other settings.")
@click.pass_context
def notices_timer(ctx, account, when, apply, force):
    """A systemd timer that sends the week's emails (`nomad notices mail
    --send`) as the nomad account (or --user). Without --apply it prints the
    two files. While review_to is set, they go there."""
    from nomad.timers import install
    install(ctx, "nomad-notices", "NOMAD weekly notices to people about their own use", "notices mail --send",
            when, account, apply, force, every_word="weekly")

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad lab check`: each term, a lab's PI says who is still in it (1.33.0).

On the hub, as the account the Console runs as (the answers are kept in its
lab_check.db): nomad.lab_check says how it works.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import click

from nomad import lab_check as L


_REPORT_PROBLEM: dict = {}


def _ctx(ctx):
    """nomad.toml, report.toml (the condos; None when it can't be read:
    then its condos are left out and, unless review_to is set in
    [console.labs.check], no email is sent), the combined database
    (read-only; None without one) and the settings."""
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
    s = L.settings(config, report, _REPORT_PROBLEM.get("why"))
    from nomad.actions.remote import hub_settings
    combined = hub_settings(target)["combined_db"]
    conn = None
    if combined.exists():
        from nomad.facts import open_readonly
        conn = open_readonly(combined)
    return config, report, s, conn, L.db_path(s, target)


def _problems(s):
    for p in s.problems:
        click.echo(f"note: {p}", err=True)


def _round(s):
    rnd = L.round_of(s, datetime.now().date())
    if rnd is None:
        raise click.ClickException("the check is off: set opens in [console.labs.check] (e.g. "
                                   "opens = [\"09-15\", \"02-01\", \"06-15\"])")
    return rnd


def _gb(n) -> str:
    if n is None:
        return "?"
    return f"{n / 1e9:.1f} GB" if n < 1e12 else f"{n / 1e12:.2f} TB"


@click.group("check")
def check_group():
    """Each term, a lab's PI says who is still in it, on My lab in the
    Console; what their answers call for is ARC's to-do. nomad changes no
    group or account itself.

    \b
    nomad lab check status          this term: each lab, answered or not, emails sent
    nomad lab check show LAB        what the lab's PI is asked, and their answers
    nomad lab check answer LAB NETID here|left|not_mine|mine|guest [--data keep|remove|ask]
    nomad lab check todo            what ARC is left to do, a cluster at a time
    nomad lab check done LAB NETID remove|add|data|no_answer --note "..."
    nomad lab check mail [--send] [--lab LAB] [--text]   the emails due today (review_to while set)
    nomad lab check timer [--apply] send them each day, from systemd

    Settings: [console.labs.check] in the hub's nomad.toml (docs/lab-check.md).
    Run as the account the Console runs as (sudo -u nomad nomad lab check ...).
    """


@check_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="As JSON.")
@click.pass_context
def check_status(ctx, as_json):
    """This term's check: each lab, how many asked about and answered,
    whether its PI sent it in, and the emails sent."""
    config, report, s, conn, rec_path = _ctx(ctx)
    rec = L.open_records(rec_path)
    st = L.status(conn, rec, config, report, report_problem=_REPORT_PROBLEM.get("why"))
    if as_json:
        click.echo(json.dumps(st, indent=2))
        return
    _problems(s)
    if not st["on"]:
        click.echo("The check is off: set opens in [console.labs.check].")
        return
    r = st["round"]
    click.echo(f"This term's check opened {r['opened']}; unanswered labs go on the to-do after {r['closes']}; "
               f"the next opens {r['next_opens']}.")
    if st["review_to"]:
        click.echo(f"Reviewing: every email goes to {', '.join(st['review_to'])}.")
    if not st["labs"]:
        click.echo("No lab has anyone to ask about.")
        return
    for lab in st["labs"]:
        sent = ", ".join(f"{m['kind']} {m['at'][:10]}{' (review)' if m['review'] else ''}" for m in lab["mail"]) \
            or "none yet"
        done = f"sent in {lab['submitted'][:10]}" if lab["submitted"] else f"{lab['answered']} of {lab['asked']} answered"
        click.echo(f"  {lab['lab']:<14} {lab['name'][:28]:<28} PI {', '.join(lab['leaders'])}: "
                   f"{lab['members']} members, {lab['claims']} condo users; {done}; emails: {sent}")


@check_group.command("show")
@click.argument("lab")
@click.option("--json", "as_json", is_flag=True, help="As JSON.")
@click.pass_context
def check_show(ctx, lab, as_json):
    """What LAB's PI is asked this term (LAB: its group, e.g. smith$), and
    the answers so far."""
    config, report, s, conn, rec_path = _ctx(ctx)
    _round(s)
    v = L.view(conn, L.open_records(rec_path), config, report, lab)
    if v is None:
        raise click.ClickException(f"{lab}: no lab with anyone to ask about this term")
    if as_json:
        click.echo(json.dumps(v, indent=2))
        return
    click.echo(f"{v['name']} ({v['lab']}), PI {', '.join(v['leaders'])}: this term's check, opened {v['opened']}; "
               f"{v['answered']} of {v['asked']} answered"
               + (f"; sent in {v['submitted']['at'][:10]} by {v['submitted']['by']}" if v["submitted"] else ""))
    for q in v["questions"]:
        said = L.SAID.get(q["answer"], "not answered yet") if q["answer"] else "not answered yet"
        if q["data"]:
            said += f"; {L.SAID[q['data']]}"
        where = (f"in the group on {', '.join(q['sites'])}" if q["kind"] == "member" else
                 "used " + "; ".join(f"{u['site']} {', '.join(u['partitions'])}" for u in q["used"]))
        last = f" (last term: {L.SAID.get(q['last_term']['answer'], q['last_term']['answer'])})" if q["last_term"] else ""
        job = f"last job {q['last_job']}" if q.get("last_job") else "no job on record"
        click.echo(f"  {q['netid']:<12} {job:<20} {where}: {said}{last}")


@check_group.command("answer")
@click.argument("lab")
@click.argument("netid")
@click.argument("answer", type=click.Choice(L.MEMBER_ANSWERS + L.CLAIM_ANSWERS))
@click.option("--data", type=click.Choice(L.DATA_ANSWERS), help="With left: what to do with their data.")
@click.option("--note", help="A note kept with the answer.")
@click.option("--by", help="Who answers (default: who runs this); kept as entered at the command line.")
@click.pass_context
def check_answer(ctx, lab, netid, answer, data, note, by):
    """Keep an answer for LAB's check this term, as its PI would give it
    on My lab (one they sent by email, say): a member is here, left (with
    --data) or not_mine; someone who used the lab's condo is mine or
    guest. Kept as entered at the command line, by --by or who runs this."""
    config, report, s, conn, rec_path = _ctx(ctx)
    rnd = _round(s)
    try:
        the_lab = L.lab_for(conn, config, report, lab)
        with L.open_records(rec_path, write=True) as rec:
            row = L.record(rec, rnd, the_lab, netid, answer, data, note, by=by or "", as_role="cli")
    except L.CheckError as e:
        raise click.ClickException(str(e))
    click.echo(f"{row['netid']} in {the_lab.name}: {L.SAID[row['answer']]}"
               + (f"; {L.SAID[row['data']]}" if row["data"] else "") + f" (kept, by {row['by']})")


@check_group.command("submit")
@click.argument("lab")
@click.option("--by", help="Who sends it in (default: who runs this).")
@click.pass_context
def check_submit(ctx, lab, by):
    """LAB's check is done for this term (every question answered)."""
    config, report, s, conn, rec_path = _ctx(ctx)
    rnd = _round(s)
    try:
        the_lab = L.lab_for(conn, config, report, lab)
        with L.open_records(rec_path, write=True) as rec:
            L.submit(rec, rnd, the_lab, by=by or "", as_role="cli")
    except L.CheckError as e:
        raise click.ClickException(str(e))
    click.echo(f"{the_lab.name}: sent in for the term that opened {rnd.id}")


@check_group.command("todo")
@click.option("--json", "as_json", is_flag=True, help="As JSON.")
@click.option("--done-days", default=30, show_default=True, type=click.IntRange(0, 3650),
              help="Also what was done in the last this many days.")
@click.pass_context
def check_todo(ctx, as_json, done_days):
    """What the PIs' answers leave ARC to do, a cluster at a time: who to
    take out of a group or put in one, what to do with a leaver's data
    (and where it is), and labs that didn't answer. A group change is done
    once nomad's groups reading shows it; mark the rest with `done`."""
    config, report, s, conn, rec_path = _ctx(ctx)
    t = L.todo(conn, L.open_records(rec_path), config, report, done_days=done_days,
               report_problem=_REPORT_PROBLEM.get("why"))
    if as_json:
        click.echo(json.dumps(t, indent=2))
        return
    _problems(s)
    if not t["items"]:
        click.echo("Nothing to do.")
    by_site: dict = {}
    for i in t["items"]:
        by_site.setdefault(i["site"] or "", []).append(i)
    for site in sorted(by_site):
        click.echo(f"{site}:" if site else "Everywhere:")
        for i in by_site[site]:
            extra = ""
            if i["kind"] == "data" and i.get("where"):
                extra = " -- " + "; ".join(f"{w['server']}:{w['path']} {_gb(w['used_bytes'])}" for w in i["where"])
            elif i["kind"] == "data":
                extra = " -- where it is isn't read here (storage_use)"
            note = f" (PI's note: {i['note']})" if i.get("note") else ""
            if i.get("as_role") == "cli":
                note += f" (entered at the command line by {i['by']})"
            elif i.get("as_role") == "admin":
                note += f" (answered by an administrator, {i['by']})"
            click.echo(f"  [{i['kind']}] {i['what']}{note}{extra}  (term {i['round']}, {i['lab']})")
    if t["done"]:
        click.echo(f"Done in the last {done_days} days:")
        for i in t["done"]:
            click.echo(f"  [{i['kind']}] {i['what']}: {i['done']['note']} ({i['done']['by']}, {i['done']['at'][:10]})")


@check_group.command("done")
@click.argument("lab")
@click.argument("netid")
@click.argument("kind", type=click.Choice(L.KINDS))
@click.option("--site", default="", help="The cluster, for remove and add.")
@click.option("--note", required=True, help="What was done (or why not).")
@click.option("--round", "round_id", help="The term's check it is of (default: the one the to-do shows).")
@click.option("--by", help="Who did it (default: who runs this).")
@click.pass_context
def check_done(ctx, lab, netid, kind, site, note, round_id, by):
    """Mark a to-do item done, with a note: a leaver's data moved, someone
    kept in a group on purpose, a lab that answered by email. NETID is "-"
    for no_answer."""
    config, report, s, conn, rec_path = _ctx(ctx)
    n = "" if netid == "-" else netid.strip().lower()
    if round_id is None:
        t = L.todo(conn, L.open_records(rec_path), config, report)
        match = [i for i in t["items"] if i["lab"] == lab and i["netid"] == n and i["kind"] == kind
                 and (i.get("site") or "") == site]
        if not match:
            raise click.ClickException("no such item on the to-do (check lab, NetID, kind and --site)")
        round_id = match[0]["round"]
    try:
        with L.open_records(rec_path, write=True) as rec:
            L.mark_done(rec, round_id, lab, n, kind, site, by=by or "", note=note)
    except L.CheckError as e:
        raise click.ClickException(str(e))
    click.echo(f"done: [{kind}] {lab} {n or '-'}{' on ' + site if site else ''}")


@check_group.command("mail")
@click.option("--send", is_flag=True, help="Send them (otherwise only say what is due).")
@click.option("--lab", "only", metavar="LAB", help="Only this lab's (its group, e.g. smith$); the others stay due.")
@click.option("--text", "show_text", is_flag=True, help="Without --send: print each email as it would go.")
@click.pass_context
def check_mail(ctx, send, only, show_text):
    """The emails due today: when the check opens, and each reminder while
    a lab's PI hasn't answered. While review_to is set, they go there."""
    config, report, s, conn, rec_path = _ctx(ctx)
    _problems(s)
    if not s.on:
        click.echo("The check is off: nothing to send.")
        return
    if send:
        with L.open_records(rec_path, write=True) as rec:
            out = L.send(conn, rec, config, report, report_problem=_REPORT_PROBLEM.get("why"), only=only)
    else:
        out = L.send(conn, L.open_records(rec_path), config, report, dry=True,
                     report_problem=_REPORT_PROBLEM.get("why"), only=only)
    if not out:
        click.echo(f"Nothing due today{' for ' + only if only else ''}.")
    for e in out:
        to = ", ".join(e["to"]) or "(no address)"
        why = f": {e.get('problem') or e.get('error')}" if e["status"] in ("can't send", "failed") else ""
        click.echo(f"  {e['status']}: {e['lab']} ({e['kind']}) to {to}"
                   + (f" for {', '.join(e['for'])}" if e["review"] else "") + why)
    if show_text and not send:
        for e in out:
            click.echo(f"\n--- To: {', '.join(e['to']) or '(no address)'}\nSubject: {e['subject']}\n\n{e['text']}")
    if not send and out:
        click.echo("A dry run: --send sends them.")
    if any(e["status"] in ("can't send", "failed") for e in out):
        sys.exit(1)


@check_group.command("timer")
@click.option("--user", "account", help="The account it runs as (default: nomad, if there is one).")
@click.option("--at", "when", default="*-*-* 08:00", show_default=True,
              help="When, as systemd writes it (OnCalendar).")
@click.option("--apply", is_flag=True, help="Install and start it (as root).")
@click.option("--force", is_flag=True, help="Replace a timer installed with other settings.")
@click.pass_context
def check_timer(ctx, account, when, apply, force):
    """A systemd timer that sends the day's emails (`nomad lab check mail
    --send`) as the nomad account (or --user). Without --apply it prints
    the two files."""
    import os
    import pwd
    import shutil
    import subprocess

    from nomad.cli import _readable_by, _unit_safe, _unit_settings, resolve_config_path
    if any(ch in (when + (account or "")) for ch in "\n\r"):
        raise click.UsageError("--at and --user are one line")
    if account is None:
        try:
            pwd.getpwnam("nomad")
            account = "nomad"
        except KeyError:
            if apply:
                raise click.ClickException("there is no nomad account here: say which with --user (not root)")
            account = pwd.getpwuid(os.geteuid()).pw_name
    else:
        try:
            pwd.getpwnam(account)
        except KeyError:
            raise click.ClickException(f"no account {account} here")
    if account == "root":
        raise click.ClickException("not as root: the answers are the Console account's (--user nomad)")
    if shutil.which("systemd-analyze"):
        r = subprocess.run(["systemd-analyze", "calendar", when], capture_output=True, text=True)
        if r.returncode != 0:
            raise click.ClickException(f"--at {when!r}: systemd doesn't read it ({(r.stderr or r.stdout).strip()})")
    _unit_safe(account, "--user")
    exe = Path(sys.executable).with_name("nomad")
    nomad_exe = _unit_safe(str(exe) if exe.exists() else (shutil.which("nomad") or "nomad"), "nomad")
    target = Path(ctx.obj.get("config_target") or resolve_config_path()).expanduser()
    config = _unit_safe(str(target.resolve()), "nomad.toml") if target.exists() else None
    if config and not _readable_by(Path(config), account):
        msg = f"{account} can't read {config}, so the emails would fail"
        if apply:
            raise click.ClickException(msg)
        click.echo(f"(note: {msg})")
    run = nomad_exe + (f" -c {config}" if config else "") + " lab check mail --send"
    service = ("[Unit]\nDescription=NOMAD lab membership check emails\nAfter=network-online.target\n\n"
               f"[Service]\nType=oneshot\nUser={account}\nExecStart={run}\n")
    timer = ("[Unit]\nDescription=NOMAD lab membership check emails, daily\n\n"
             f"[Timer]\nOnCalendar={when}\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
    unit_dir = Path("/etc/systemd/system")
    name = "nomad-lab-check"
    paths = {unit_dir / f"{name}.service": service, unit_dir / f"{name}.timer": timer}
    if not apply:
        for p, text in paths.items():
            click.echo(f"# {p}")
            click.echo(text)
        click.echo("Install with --apply (as root), or by hand:")
        click.echo("  sudo systemctl daemon-reload")
        click.echo(f"  sudo systemctl enable --now {name}.timer")
        return
    if not shutil.which("systemctl"):
        raise click.ClickException(f"no systemctl here: run it from cron each day instead, as {account}:\n"
                                   f"  0 8 * * * {run}")
    if os.geteuid() != 0:
        raise click.ClickException("--apply writes to /etc/systemd/system: run it as root (sudo)")
    same = [p for p, text in paths.items() if p.exists() and _unit_settings(p.read_text()) == _unit_settings(text)]
    other = [p for p, text in paths.items() if p.exists() and p not in same]
    if other and not force:
        raise click.ClickException(f"{', '.join(map(str, other))} already there with other settings "
                                   "(--force replaces it)")
    for p, text in paths.items():
        if p not in same:
            p.write_text(text)
            p.chmod(0o644)
    for cmd in (["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", f"{name}.timer"]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise click.ClickException(f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()}")
    click.echo("already installed; enabled" if len(same) == len(paths) else "installed and enabled")

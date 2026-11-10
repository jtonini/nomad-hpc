# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad maintenance`: declared maintenance windows (nomad.maintenance), and
at a site, what its hub said (nomad.hub_watch)."""
from __future__ import annotations

import getpass
import json
import os
from dataclasses import asdict
from datetime import datetime

import click

from nomad import maintenance as M


def _config(ctx) -> dict:
    return (ctx.obj or {}).get("config") or {}


def _hub_cfg(ctx) -> dict:
    """The hub's settings as `nomad sync` reads them (its sites, its
    combined database); {} when this is no hub. Read once a command; what
    reading them prints (a warning about a sync.toml) goes to standard
    error, so --json's answer stays the only thing on standard output."""
    obj = ctx.obj if isinstance(ctx.obj, dict) else {}
    if "_maintenance_hub" not in obj:
        import contextlib
        import sys
        try:
            from nomad.cli import _load_hub_config
            with contextlib.redirect_stdout(sys.stderr):
                cfg = _load_hub_config(None, obj.get("config_target"))
        except Exception:
            cfg = None
        obj["_maintenance_hub"] = cfg or {}
    return obj["_maintenance_hub"]


def _hub_sites(ctx) -> list[dict]:
    return list(_hub_cfg(ctx).get("sites") or [])


def _who() -> str:
    return os.environ.get("SUDO_USER") or getpass.getuser()


TELL_SECONDS = 20        # per site, to tell it at once


def _tell(ctx, names) -> list[str]:
    """Tell the sites now (each is told again whenever the hub reads it): a
    site that can't be reached is told at the next sync. What was said of
    each, as lines."""
    sites = _hub_sites(ctx)
    if not sites:
        return []
    try:
        # The hub as its sync names itself to the sites: by the combined
        # database the same settings give (nomad.db.delta_hub.hub_id).
        from nomad.config import combined_db_path
        from nomad.db import delta_hub
        hub = delta_hub.hub_id(combined_db_path(_hub_cfg(ctx).get("output_db")))
    except Exception:
        return []
    config = _config(ctx)
    out = []
    for site in sites:
        name = site.get("name")
        if not name or (names is not None and name not in names):
            continue
        notes = M.site_args(name, config)
        got = delta_hub.tell_site(site, hub, notes, timeout=TELL_SECONDS)
        if got is None:
            said = "not reached now; told when the hub next reads it"
        elif any(a.startswith("--maint=") for a in notes) and not got.get("maint"):
            said = "it did not take the window (too long for it): its own alerts are not held"
        elif any(a.startswith("--quiet=") for a in notes) and not got.get("quiet"):
            said = "it did not take the hub's window (too long for it): it may report the hub as silent"
        else:
            said = "told"
        out.append(f"  {name}: {said}")
    return out


def _line(w: dict) -> str:
    who = f", declared by {w['by']}" if w.get("by") else ""
    if w["status"] == "now" and w["site"] == M.HUB:
        return (f"{w['site']}: in maintenance until {M.shown(w['until'])} ({w['reason']}){who}. "
                "The sites don't report it as silent meanwhile.")
    if w["status"] == "now":
        return (f"{w['site']}: in maintenance until {M.shown(w['until'])} ({w['reason']}){who}. "
                f"Alerts about it are held until {M.shown(w['held_until'])}.")
    if w["status"] == "grace":
        return (f"{w['site']}: its window ended {M.shown(w['ends'])} ({w['reason']}); alerts about it "
                f"are held until {M.shown(w['held_until'])}, for it to come back.")
    if w["status"] == "coming":
        return f"{w['site']}: from {M.shown(w['start'])} until {M.shown(w['until'])} ({w['reason']}){who}."
    return f"{w['site']}: {M.shown(w['start'])} to {M.shown(w['ends'])} ({w['reason']}){who}."


@click.group("maintenance", invoke_without_command=True)
@click.option("--days", type=int, default=7, show_default=True, help="Past windows of this many days.")
@click.pass_context
def maintenance_group(ctx, days):
    """Declared maintenance: a site (or the hub) taken down on purpose.

    \b
    nomad maintenance                                   what is declared
    nomad maintenance start SITE --until WHEN --reason WHY [--from WHEN] [--apply]
    nomad maintenance end SITE [--apply]

    While a site's window lasts (and an hour after, for it to come back),
    alerts about it are held, the hub's and the site's own, and the Console
    shows it as in maintenance. What still holds afterwards is raised then.
    SITE is one of the hub's sites, `all`, or `hub` for the hub itself: the
    sites then don't report it as silent. WHEN: "2026-11-12 18:00", "18:00"
    (the next one), "+4h", "+2d"; for --until, counted from the start. start
    and end show what they would do; --apply does it. Run it on the hub, as
    the account its nomad runs as."""
    if ctx.invoked_subcommand is not None:
        return
    config = _config(ctx)
    rows = M.listing(config, past_days=max(0, days))
    # At a site: what the hubs that read it have said.
    try:
        from nomad import hub_watch as H
        from nomad.cli import get_db_path
        found = H.read_hubs(get_db_path(config))
    except Exception:
        found = []
    now = datetime.now().timestamp()
    if not rows and not any((h.maint and now < h.maint[2]) or (h.quiet and now < h.quiet[1]) for h in found):
        click.echo("No maintenance declared.")
    for title, statuses in (("Now", ("now", "grace")), ("Coming", ("coming",)),
                            (f"Over (last {days} days)", ("over",))):
        some = [w for w in rows if w["status"] in statuses]
        if some:
            click.echo(f"{title}:")
            for w in some:
                click.echo(f"  {_line(w)}")

    def at(ts) -> str:
        return M.shown(datetime.fromtimestamp(ts))
    for h in found:
        click.echo(f"{h.label[0].upper()}{h.label[1:]} "
                   + (f"last read this site {at(h.seen)}." if h.seen else "has not read this site yet."))
        if h.maint and now < h.maint[2]:
            when = "in maintenance" if h.maint[0] <= now < h.maint[1] else (
                "maintenance over, coming back" if h.maint[0] <= now else "maintenance coming")
            if h.maint[0] > now:
                held = f"; this site's alerts are held from then until {at(h.maint[2])}."
            elif H.trusted(h, now):
                held = f"; this site's alerts are held until {at(h.maint[2])}."
            else:
                held = (f"; this site's alerts are not held: that hub has not read it for "
                        f"{H.TRUST_HOURS} hours.")
            click.echo(f"  It says this site: {when}, {at(h.maint[0])} to {at(h.maint[1])}"
                       + (f" ({h.reason})" if h.reason else "") + held)
        if h.quiet and now < h.quiet[1]:
            click.echo(f"  It says it will be away {at(h.quiet[0])} to {at(h.quiet[1])}.")


def _finish(as_json: bool, what: str, site: str, lines: list, applied: bool, told: list, windows: list) -> None:
    """Say what was (or would be) done: lines for a person, or JSON for the
    Console's action."""
    if as_json:
        click.echo(json.dumps({"what": what, "site": site, "ok": True, "applied": applied,
                               "said": "\n".join(lines + told), "windows": windows}, indent=1))
        return
    for line in lines + told:
        click.echo(line)
    if not applied:
        click.echo("\nNothing changed. Run again with --apply to do it.")


def _cant_write(config, e: OSError) -> str:
    """Why the windows could not be written: the file is the one the hub's
    sync reads, so it is written as the account nomad runs as."""
    return (f"can't write {M.state_path(config)} ({e.strerror or e}); nothing changed. Run it as the "
            "account the hub's nomad runs as")


def _fail(as_json: bool, what: str, site: str, why: str):
    if as_json:
        click.echo(json.dumps({"what": what, "site": site, "ok": False, "applied": False, "error": why}))
        raise SystemExit(1)
    raise click.ClickException(why)


@maintenance_group.command("start")
@click.argument("site")
@click.option("--until", "until", required=True,
              help='When it ends: "2026-11-12 18:00", "18:00", or how long it lasts: "+4h", "+2d".')
@click.option("--from", "start", default=None, help='When it starts: "2026-11-12 14:00", "14:00", "+1h" '
                                                    '(default: now).')
@click.option("--reason", required=True, help="Why: shown in the Console and with the next alert.")
@click.option("--by", default=None, help="Who asked (default: you).")
@click.option("--apply", is_flag=True, help="Declare it (default: only show what would be declared).")
@click.option("--json", "as_json", is_flag=True, help="The answer as JSON (what the Console reads).")
@click.pass_context
def maintenance_start(ctx, site, until, start, reason, by, apply, as_json):
    """SITE is in maintenance from now (or --from) until --until: alerts
    about it are held, and the Console says so. Declaring again changes it.
    Shows what it would declare; --apply declares it."""
    config = _config(ctx)
    now = datetime.now()
    names = M.site_names(config, _hub_sites(ctx))
    if not names:
        _fail(as_json, "maintenance.start", site, "this host has no [hub] sites: declare maintenance on the hub")
    targets = names if site == M.ALL else [site]
    try:
        begins = M.parse_when(start, now) if start else None
        # From its start: "+4h" is how long it lasts, "18:00" the first 18:00 after it starts.
        ends = M.parse_when(until, begins or now)
        # All checked before any is declared: `all` is all or none.
        for t in targets:
            M.declare(t, ends, reason, start=begins, by=by or _who(), config=config, now=now, known=names,
                      write=False)
        done = [M.declare(t, ends, reason, start=begins, by=by or _who(), config=config, now=now, known=names,
                          write=apply) for t in targets]
    except M.MaintenanceError as e:
        _fail(as_json, "maintenance.start", site, str(e))
    except OSError as e:
        _fail(as_json, "maintenance.start", site, _cant_write(config, e))
    grace = M.grace_minutes(config)
    lines = []
    for new, replaced in done:
        lines.append(f"{new.site}: in maintenance {M.shown(new.start)} to {M.shown(new.until)} ({new.reason})."
                     + (f" It replaces the window until {M.shown(replaced.until)}." if replaced else ""))
        if new.site == M.HUB:
            lines.append("  The sites won't report the hub as silent during it; if it is still silent an hour "
                         "after it, they will.")
        else:
            lines.append(f"  Alerts about it are held until {M.shown(new.held_until(grace))} "
                         f"(the window, and {grace:g} minutes for it to come back).")
    told = []
    if apply:
        told = _tell(ctx, None if any(n.site == M.HUB for n, _ in done) else {n.site for n, _ in done})
    _finish(as_json, "maintenance.start", site, lines, apply, told, [asdict(n) for n, _ in done])


@maintenance_group.command("end")
@click.argument("site")
@click.option("--by", default=None, help="Who asked (default: you).")
@click.option("--apply", is_flag=True, help="End it (default: only show what would end).")
@click.option("--json", "as_json", is_flag=True, help="The answer as JSON (what the Console reads).")
@click.pass_context
def maintenance_end(ctx, site, by, apply, as_json):
    """SITE's maintenance is over now: alerts about it are no longer held.
    One thing at a time: the window running; else the hour after one just
    over; else the window coming, which is called off. Shows what would end;
    --apply ends it."""
    config = _config(ctx)
    names = M.site_names(config, _hub_sites(ctx))
    targets = names if site == M.ALL else [site]
    try:
        ended = [w for w in (M.end(t, config=config, write=apply, by=by or _who()) for t in targets)
                 if w is not None]
    except OSError as e:
        _fail(as_json, "maintenance.end", site, _cant_write(config, e))
    if not ended:
        _fail(as_json, "maintenance.end", site, f"{site}: no maintenance running or coming, and no alerts held")
    lines = [f"{w.site}: its maintenance (until {M.shown(w.until)}, {w.reason}) "
             + ("is over now. Alerts about it are no longer held." if apply
                else "would be over now, and alerts about it no longer held.") for w in ended]
    told = []
    if apply:
        told = _tell(ctx, None if any(w.site == M.HUB for w in ended) else {w.site for w in ended})
    _finish(as_json, "maintenance.end", site, lines, apply, told, [asdict(w) for w in ended])

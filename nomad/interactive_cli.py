# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad interactive sessions|stop|clear|restart` (1.43.0): see nomad/interactive_admin.py."""
from __future__ import annotations

import json
import sys

import click

from nomad.config.course_cli import interactive_group


def _db(ctx, db):
    from pathlib import Path
    if db:
        return Path(db).expanduser()
    from nomad.cli import get_db_path
    return get_db_path((ctx.obj or {}).get("config") or {})


def _answer(res: dict, as_json: bool) -> None:
    from nomad.interactive_admin import lines
    if as_json:
        click.echo(json.dumps(res, indent=1))
    else:
        for ln in lines(res):
            click.echo(ln)
    if not res["ok"]:
        sys.exit(1)


def _refused(what: str, exc, as_json: bool):
    if as_json:
        click.echo(json.dumps({"what": what, "ok": False, "error": str(exc)}))
        sys.exit(1)
    raise click.ClickException(str(exc)) from None


@interactive_group.command("sessions")
@click.option("--db", type=click.Path(), help="This host's database, for how long each has been idle "
                                              "(default: from nomad.toml).")
@click.option("--json", "as_json", is_flag=True, help="The answer as JSON (what the Console reads).")
@click.pass_context
def sessions_cmd(ctx, db, as_json):
    """The RStudio and Jupyter sessions here now, how long each has been idle,
    and what nomad may do about them here. Changes nothing."""
    from nomad.interactive_admin import look
    _answer(look(config=(ctx.obj or {}).get("config") or {}, db=_db(ctx, db)), as_json)


@interactive_group.command("stop")
@click.argument("person")
@click.argument("pid", type=int)
@click.option("--started", help="When the session started, as `sessions` says: it is that very process.")
@click.option("--by", help="Who asked (a NetID); default this account.")
@click.option("--apply", is_flag=True, help="Do it (otherwise show what would happen).")
@click.option("--json", "as_json", is_flag=True, help="The answer as JSON (what the Console reads).")
@click.pass_context
def stop_cmd(ctx, person, pid, started, by, apply, as_json):
    """Suspend PERSON's RStudio session PID (R saves it; it comes back when
    they next open RStudio), or stop their Jupyter server or kernel PID (as
    them). A dry run unless --apply."""
    from nomad.interactive_admin import SessionError, stop
    try:
        res = stop(person, pid, started=started, by=by, apply=apply, config=(ctx.obj or {}).get("config") or {})
    except SessionError as exc:
        _refused("stop", exc, as_json)
    _answer(res, as_json)


@interactive_group.command("clear")
@click.option("--hours", type=click.IntRange(1, 1440),
              help="Idle this long is stale (default [actions] stale_hours, else the collector's 24).")
@click.option("--kind", type=click.Choice(["all", "rstudio", "jupyter"]), default="all", show_default=True)
@click.option("--db", type=click.Path(), help="This host's database (default: from nomad.toml).")
@click.option("--by", help="Who asked (a NetID); default this account.")
@click.option("--apply", is_flag=True, help="Do it (otherwise show which would be cleared).")
@click.option("--json", "as_json", is_flag=True, help="The answer as JSON (what the Console reads).")
@click.pass_context
def clear_cmd(ctx, hours, kind, db, by, apply, as_json):
    """Suspend each RStudio session, and stop each Jupyter server, idle at
    least --hours. A dry run unless --apply."""
    from nomad.interactive_admin import SessionError, clear
    try:
        res = clear(hours=hours, kind=kind, by=by, apply=apply, config=(ctx.obj or {}).get("config") or {},
                    db=_db(ctx, db))
    except SessionError as exc:
        _refused("clear", exc, as_json)
    _answer(res, as_json)


@interactive_group.command("restart")
@click.argument("service", type=click.Choice(["rstudio", "jupyter"]))
@click.option("--servers", type=click.IntRange(0, None),
              help="JupyterHub: how many people's servers the restart stops, as the dry run says.")
@click.option("--by", help="Who asked (a NetID); default this account.")
@click.option("--apply", is_flag=True, help="Restart it (otherwise show what would happen).")
@click.option("--json", "as_json", is_flag=True, help="The answer as JSON (what the Console reads).")
@click.pass_context
def restart_cmd(ctx, service, servers, by, apply, as_json):
    """Restart RStudio (Workbench) or JupyterHub. A dry run unless --apply."""
    from nomad.interactive_admin import SessionError, restart
    try:
        res = restart(service, servers=servers, by=by, apply=apply, config=(ctx.obj or {}).get("config") or {})
    except SessionError as exc:
        _refused("restart", exc, as_json)
    _answer(res, as_json)

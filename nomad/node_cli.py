# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad node drain|resume` (1.31.0): see nomad/drain.py."""
from __future__ import annotations

import json
import sys

import click


@click.group("node")
def node_group():
    """Drain a Slurm node, or resume it: a dry run unless --apply.

    \b
    nomad node drain node07 --reason "disk errors"            what would happen
    nomad node drain node07 --reason "disk errors" --apply    do it
    nomad node resume node07 --apply

    Off until this account's nomad.toml says who runs scontrol:
    [actions] slurm_as = "slurm" (through sudo) or "self". The Console's
    Nodes page does the same through the site's agent. See docs/actions.md.
    """


def _change(ctx, what, node, reason, by, apply, as_json):
    from nomad.config import resolve_cluster_name
    from nomad.drain import NodeError, change, lines
    config = (ctx.obj or {}).get("config") or {}
    try:
        cluster = resolve_cluster_name(config)
    except Exception:                              # noqa: BLE001 -- a name to show, nothing more
        cluster = None
    try:
        res = change(node, what, reason=reason, by=by, apply=apply, config=config,
                     config_path=(ctx.obj or {}).get("config_target"), cluster=cluster)
    except NodeError as exc:
        if as_json:
            click.echo(json.dumps({"what": what, "node": node, "ok": False, "error": str(exc)}))
            sys.exit(1)
        raise click.ClickException(str(exc)) from None
    if as_json:
        click.echo(json.dumps(res, indent=1))
    else:
        for ln in lines(res):
            click.echo(ln)
    if not res["ok"]:
        sys.exit(1)


@node_group.command("drain")
@click.argument("node")
@click.option("--reason", required=True, help="Why: Slurm keeps it as the node's reason (one line, at most 200).")
@click.option("--by", help="Who asked (a NetID); default this account. Slurm's reason ends '(BY via nomad)'.")
@click.option("--apply", is_flag=True, help="Drain it (otherwise show what would happen).")
@click.option("--json", "as_json", is_flag=True, help="The answer as JSON (what the Console reads).")
@click.pass_context
def drain_cmd(ctx, node, reason, by, apply, as_json):
    """No new job starts on NODE; the jobs running there run to their end."""
    _change(ctx, "drain", node, reason, by, apply, as_json)


@node_group.command("resume")
@click.argument("node")
@click.option("--reason", help="Why (kept in the logs: Slurm drops a node's reason when it resumes).")
@click.option("--by", help="Who asked (a NetID); default this account.")
@click.option("--apply", is_flag=True, help="Resume it (otherwise show what would happen).")
@click.option("--json", "as_json", is_flag=True, help="The answer as JSON (what the Console reads).")
@click.pass_context
def resume_cmd(ctx, node, reason, by, apply, as_json):
    """A drained (or down) NODE takes jobs again."""
    _change(ctx, "resume", node, reason, by, apply, as_json)

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad actions` (the hub) and `nomad agent` (a site)."""
from __future__ import annotations

import json
import sys

import click


@click.group("actions")
def actions_group():
    """The catalog of actions the Console can start, here or on a site.

    \b
    nomad actions list                          what there is
    nomad actions run collectors --site c1      on a site, through its agent
    nomad actions run usage.report -p from=2025-10-01 -p to=2026-10-07 -p cluster=c1
    nomad actions key                           the hub's key, to install on each site

    Read-only for now. See docs/actions.md for how a site lets the hub in.
    """


@actions_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="The catalog as JSON (what the Console reads).")
def list_cmd(as_json):
    """The actions, where each runs, and their parameters."""
    from nomad import actions
    cat = actions.catalog()
    if as_json:
        click.echo(json.dumps(cat, indent=2))
        return
    for a in cat:
        where = " and ".join("the hub" if w == "hub" else "sites" for w in sorted(a["where"]))
        click.echo(f"{a['name']:<16} {a['title']} (on {where})")
        click.echo(f"{'':<16} {a['help']}")
        for p in a["params"]:
            extra = []
            if p.get("choices"):
                extra.append("one of " + ", ".join(p["choices"]))
            if p.get("min") is not None:
                extra.append(f"{p['min']}–{p['max']}")
            if p.get("default") is not None:
                extra.append(f"default {p['default']}")
            req = "required" if p["required"] else "optional"
            click.echo(f"{'':<18}-p {p['name']}=…  {p['help']} ({req}"
                       + (", " + ", ".join(extra) if extra else "") + ")")


def _params(pairs):
    out = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.BadParameter(f"{pair!r}: write NAME=VALUE", param_hint="-p")
        k, v = pair.split("=", 1)
        out[k.strip()] = v
    return out


@actions_group.command("run")
@click.argument("name")
@click.option("--site", help="A site of the hub (default: the hub itself).")
@click.option("--here", is_flag=True, help="On this host, as a site would run it.")
@click.option("-p", "--param", "pairs", multiple=True, help="NAME=VALUE (repeat for several).")
@click.option("--json", "as_json", is_flag=True, help="The whole result as JSON.")
def run_cmd(name, site, here, pairs, as_json):
    """Run one action and print its output."""
    from nomad import actions
    if site and here:
        raise click.UsageError("--site and --here are two different places: choose one")
    try:
        res = actions.run(name, _params(pairs), site=site, here=here)
    except actions.ActionError as exc:
        raise click.ClickException(str(exc)) from None
    if as_json:
        click.echo(json.dumps(res, indent=2))
    else:
        if res.get("output"):
            click.echo(res["output"].rstrip())
        if res.get("errors") and not res.get("ok"):
            click.echo(res["errors"].rstrip(), err=True)
        for f in res.get("files") or []:
            click.echo(f"file: {f}", err=True)
        if site and res.get("restricted") is False:
            click.echo(f"Note: {site} let the hub's key run more than `nomad agent`. Restrict it there: "
                       "nomad agent install-key (see docs/actions.md).", err=True)
        if res.get("error"):
            click.echo(f"Error: {res['error']}", err=True)
    if not res.get("ok"):
        sys.exit(1)


@actions_group.command("key")
def key_cmd():
    """The hub's agent key (made the first time) and how to install it on a site."""
    from nomad.actions.remote import ensure_key
    path, pub = ensure_key()
    click.echo(f"The hub's agent key: {path} (its public half: {path}.pub)")
    click.echo()
    click.echo("On each site, as the account nomad runs as there, run once (a dry run; add --apply to write):")
    click.echo()
    click.echo(f"  nomad agent install-key '{pub}'")
    click.echo()
    click.echo("Then from here: nomad actions run version --site SITE")


@click.group("agent", invoke_without_command=True)
@click.pass_context
def agent_group(ctx):
    """A site's side of the actions: answer one request from the hub.

    sshd runs this as the forced command of the hub's key; it reads one
    request on its input. `nomad agent install-key` sets that up.
    """
    if ctx.invoked_subcommand is None:
        from nomad.actions.agent import serve
        sys.exit(serve())


@agent_group.command("install-key")
@click.argument("pubkey")
@click.option("--from", "from_pattern", help="Only from these hosts or addresses (sshd's from= patterns).")
@click.option("--apply", is_flag=True, help="Write it (otherwise show what would change).")
def install_key_cmd(pubkey, from_pattern, apply):
    """Let the hub's key run `nomad agent` here, and nothing else.

    Adds PUBKEY to ~/.ssh/authorized_keys with `restrict` (no terminal, no
    forwarding) and `command=` (whatever the hub asks, sshd runs `nomad agent`).
    A line already holding the same key is replaced.
    """
    from nomad.actions.agent import install_key
    try:
        status, lines = install_key(pubkey, from_pattern=from_pattern, apply=apply)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from None
    for ln in lines:
        click.echo(ln)

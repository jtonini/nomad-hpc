# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad gate`: setting up and trying nomad-gate (1.25), the way the hub
acts on a cluster as a person. See docs/submit.md, "From the Console"."""
import json
import sys

import click

DEFAULT_GATE = "/usr/local/sw/nomad/bin/nomad-gate"


@click.group("gate")
def gate_group():
    """The hub acting on a cluster as a person (the Console's Submit page).

    \b
    On the hub, as the Console's account:
      nomad gate key                        the hub's gate key (made the first time)
      nomad gate test --site S --user U     is the gate there, and may it act as U?
      nomad gate ask --site S --user U jobs  one request, its answer as JSON
    On the cluster, as its nomad account:
      nomad gate install-key 'PUBKEY' --from HUB-ADDRESS    (a dry run; --apply writes it)
    \b
    The key the hub uses for this can run nothing but nomad-gate, which does
    only its own short list (size, submit, again, jobs, cancel, ls, show) as
    the person asked for, with the cluster account's sudo.
    """


@gate_group.command("key")
@click.pass_context
def key_cmd(ctx):
    """The hub's gate key (made the first time) and how to let it in on a cluster."""
    from nomad.actions.spec import ActionError
    from nomad.submit import remote
    sites, hub = remote.hub_sites(ctx.obj.get("config_target") if ctx.obj else None)
    try:
        path, pub = remote.ensure_key(remote.key_path(hub))
    except ActionError as e:
        raise click.ClickException(str(e)) from None
    click.echo(f"The hub's gate key: {path} (its public half: {path}.pub)")
    click.echo()
    click.echo("On each cluster, as the account nomad runs as there (installer on spydur), once (a dry run;")
    click.echo("--apply writes it). --from: this hub's address(es) as the cluster sees them:")
    click.echo()
    click.echo(f"  nomad gate install-key '{pub}' --from HUB-ADDRESS")
    click.echo()
    click.echo('Then here: submit = "gate" in the site\'s [[hub.sites]], and nomad gate test --site SITE --user YOU')
    gated = [s["name"] for s in sites if remote.mode(s) == "gate"]
    if gated:
        click.echo(f"(submit = \"gate\" now: {', '.join(gated)})")


@gate_group.command("install-key")
@click.argument("pubkey")
@click.option("--from", "origins", help="Only from these addresses (sshd's from=): the hub's, as this cluster "
              "sees it (host names work only where sshd has UseDNS yes).")
@click.option("--from-anywhere", is_flag=True, help="No from= (not advised: the key acts as people here).")
@click.option("--gate", "gate_path", default=DEFAULT_GATE, show_default=True,
              help="nomad-gate's full path here (install_nomad_for_users.sh puts it in its bin/).")
@click.option("--apply", is_flag=True, help="Write it (otherwise show what would change).")
def install_key_cmd(pubkey, origins, from_anywhere, gate_path, apply):
    """Let the hub's gate key run `nomad-gate serve` here, and nothing else.

    Adds PUBKEY to ~/.ssh/authorized_keys with `restrict` (no terminal, no
    forwarding), `from=` (the hub only) and `command=` (whatever the hub
    asks, sshd runs nomad-gate serve). A line already holding the same key
    is replaced. Run as the cluster's nomad account (the one whose sudo may
    act as the people who use it); nomad-gate must be there already.
    """
    from nomad.submit import remote
    if not origins and not from_anywhere:
        raise click.UsageError("give --from (the hub's address as this cluster sees it), or --from-anywhere")
    if origins and from_anywhere:
        raise click.UsageError("--from or --from-anywhere, not both")
    try:
        status, lines = remote.install_key(pubkey, origins=origins, gate_path=gate_path, apply=apply)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from None
    for ln in lines:
        click.echo(ln)


def _site(ctx, name):
    from nomad.submit import remote
    sites, hub = remote.hub_sites(ctx.obj.get("config_target") if ctx.obj else None)
    s = remote.site_named(sites, name)
    if s is None:
        known = ", ".join(x["name"] for x in sites) or "none"
        raise click.ClickException(f"{name!r} isn't one of the hub's [[hub.sites]] (they are: {known})")
    return s, hub


@gate_group.command("test")
@click.option("--site", "site_name", required=True, help="One of the hub's sites.")
@click.option("--user", help="Someone the gate should act as (you, say): asks as them too.")
@click.pass_context
def test_cmd(ctx, site_name, user):
    """Is the gate there, and may it act as --user? Asks it its version
    (as itself, then as the person) and the person's jobs now. Changes
    nothing."""
    from nomad.submit import remote
    s, hub = _site(ctx, site_name)
    ok = True

    def say(what, doc):
        nonlocal ok
        if doc.get("ok"):
            click.echo(f"  ok   {what}")
        else:
            ok = False
            click.echo(click.style(f"  no   {what}: {doc.get('error')}", fg="red"))
        return doc

    click.echo(f"{site_name} ({remote.mode(s)}):")
    a = say("the gate answers", remote.call(s, "version", hub=hub))
    if a.get("ok"):
        click.echo(f"       nomad-gate {a.get('gate')}, nomad {a.get('nomad')}, as {a.get('account')}")
    if user and a.get("ok"):
        b = say(f"it acts as {user}", remote.call(s, "version", user, hub=hub))
        if b.get("ok") and b.get("account") != user:
            ok = False
            click.echo(click.style(f"  no   it answered as {b.get('account')}, not {user}", fg="red"))
        if b.get("ok"):
            c = say(f"{user}'s jobs now", remote.call(s, "jobs", user, hub=hub))
            if c.get("ok"):
                click.echo(f"       {len(c.get('jobs') or [])} job(s)")
    if not ok:
        sys.exit(1)


@gate_group.command("ask", context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
@click.option("--site", "site_name", required=True, help="One of the hub's sites.")
@click.option("--user", required=True, help="Who the gate acts as.")
@click.argument("words", nargs=-1, type=click.UNPROCESSED, required=True)
@click.pass_context
def ask_cmd(ctx, site_name, user, words):
    """Ask the gate one thing, as --user; print its answer (JSON).

    \b
      nomad gate ask --site spydur --user jdoe jobs
      nomad gate ask --site spydur --user jdoe size --script /scratch/jdoe/run/job.sh
      nomad gate ask --site spydur --user jdoe show --script /scratch/jdoe/run/job.sh
      nomad gate ask --site spydur --user jdoe submit --script /scratch/jdoe/run/job.sh --sha256 HEX --mem 8G
      nomad gate ask --site spydur --user jdoe job --job 4815162
    """
    from nomad.submit import remote
    s, hub = _site(ctx, site_name)
    words = list(words)
    doc = remote.ask(s, words[:1] + ["--user", user] + words[1:], hub)
    click.echo(json.dumps(doc, indent=2, sort_keys=True))
    if not doc.get("ok"):
        sys.exit(1)

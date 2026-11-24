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
    On a host the hub reaches the cluster through (gate_jump), as that account:
      nomad gate jump-key 'PUBKEY' --to CLUSTER:22 --from HUB-ADDRESS
    \b
    The key the hub uses for this can run nothing but nomad-gate, which does
    only its own short list (size, submit, again, starts, jobs, job,
    cancel, ls, show; save and read only with install-key --edit) as the
    person asked for, with the cluster account's sudo.
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
    click.echo("On each cluster, as the account nomad runs as there (hpcadmin on hpc1), once (a dry run;")
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
@click.option("--edit", is_flag=True, help="The Console may also save people's job scripts here, as them "
              "(nomad-gate serve --edit; 1.25.3). The hub's [[hub.sites]] must say edit = true too.")
@click.option("--apply", is_flag=True, help="Write it (otherwise show what would change).")
def install_key_cmd(pubkey, origins, from_anywhere, gate_path, edit, apply):
    """Let the hub's gate key run `nomad-gate serve` here, and nothing else.

    Adds PUBKEY to ~/.ssh/authorized_keys with `restrict` (no terminal, no
    forwarding), `from=` (the hub only) and `command=` (whatever the hub
    asks, sshd runs nomad-gate serve). A line already holding the same key
    is replaced. Run as the cluster's nomad account (the one whose sudo may
    act as the people who use it); nomad-gate must be there already.

    With --edit, the hub's key may also write people's job scripts as them
    (the Console's Edit): whoever holds that key could then run code as
    anyone the gate acts for, not only send the scripts they have. Left off,
    the gate refuses every save.
    """
    from nomad.submit import remote
    if not origins and not from_anywhere:
        raise click.UsageError("give --from (the hub's address as this cluster sees it), or --from-anywhere")
    if origins and from_anywhere:
        raise click.UsageError("--from or --from-anywhere, not both")
    try:
        status, lines = remote.install_key(pubkey, origins=origins, gate_path=gate_path, apply=apply, edit=edit)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from None
    for ln in lines:
        click.echo(ln)


@gate_group.command("jump-key")
@click.argument("pubkey")
@click.option("--to", "to", multiple=True, required=True,
              help="The cluster's ssh as this host reaches it, host:port (hpc3:22); again for more.")
@click.option("--from", "origins", help="Only from these addresses (sshd's from=): the hub's, as this host "
              "sees it.")
@click.option("--from-anywhere", is_flag=True, help="No from= (not advised).")
@click.option("--apply", is_flag=True, help="Write it (otherwise show what would change).")
def jump_key_cmd(pubkey, to, origins, from_anywhere, apply):
    """Let the hub's gate key through this host to a cluster's ssh, and
    nothing else (1.40.0): for a site whose [[hub.sites]] has gate_jump =
    "THIS-ACCOUNT@THIS-HOST".

    Adds PUBKEY to ~/.ssh/authorized_keys with `restrict` (no terminal), its
    forwarding allowed again but only to --to (permitopen), `command=`
    /bin/false (should anyone ask it for a session) and `from=` the hub. A
    line already holding the same key is replaced. The cluster's own
    install-key then takes this host's address as --from.
    """
    from nomad.submit import remote
    if not origins and not from_anywhere:
        raise click.UsageError("give --from (the hub's address as this host sees it), or --from-anywhere")
    if origins and from_anywhere:
        raise click.UsageError("--from or --from-anywhere, not both")
    try:
        _, lines = remote.install_jump_key(pubkey, to=list(to), origins=origins, apply=apply)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from None
    for ln in lines:
        click.echo(ln)


def _machines(ctx) -> list:
    """[[console.files.machines]] (1.58.0): machines with the gate alone."""
    from nomad.actions.remote import _config
    from nomad.submit import files as F
    return list(F.settings(_config(ctx.obj.get("config_target") if ctx.obj else None)).machines)


def _site(ctx, name):
    from nomad.submit import remote
    sites, hub = remote.hub_sites(ctx.obj.get("config_target") if ctx.obj else None)
    s = remote.site_named(sites, name) or remote.site_named(_machines(ctx), name)
    if s is None:
        known = ", ".join(x["name"] for x in sites) or "none"
        raise click.ClickException(f"{name!r} isn't one of the hub's [[hub.sites]] (they are: {known}) nor "
                                   "of its [[console.files.machines]]")
    return s, hub


@gate_group.command("test")
@click.option("--site", "site_name", help="One of the hub's sites, or of its [[console.files.machines]].")
@click.option("--machines", "all_machines", is_flag=True,
              help="Each of [[console.files.machines]] (lab machines with the gate alone), one after another.")
@click.option("--user", help="Someone the gate should act as (you, say): asks as them too.")
@click.pass_context
def test_cmd(ctx, site_name, all_machines, user):
    """Is the gate there, and may it act as --user? Asks it its version
    (as itself, then as the person) and the person's jobs now (not on a
    machine with the gate alone: it runs no jobs); where moving files is on,
    sends half a file and removes it. Changes nothing."""
    if bool(site_name) == bool(all_machines):
        raise click.UsageError("--site NAME, or --machines")
    if all_machines:
        machines = _machines(ctx)
        if not machines:
            raise click.ClickException("no [[console.files.machines]] in this nomad.toml")
        bad = []
        for m in machines:
            try:
                ctx.invoke(test_cmd, site_name=m["name"], all_machines=False, user=user)
            except SystemExit as e:
                if e.code:
                    bad.append(m["name"])
        click.echo(f"{len(machines) - len(bad)} of {len(machines)} machine(s) ok"
                   + (f"; not: {', '.join(bad)}" if bad else ""))
        if bad:
            sys.exit(1)
        return
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

    click.echo(f"{site_name} ({remote.mode(s)}" + (f", through {s['gate_jump']}" if s.get("gate_jump") else "")
               + "):")
    a = say("the gate answers", remote.call(s, "version", hub=hub))
    if a.get("ok"):
        click.echo(f"       nomad-gate {a.get('gate')}, nomad {a.get('nomad')}, as {a.get('account')}")
        if "edit" in a:
            here = "on" if a["edit"] else "off (install-key --edit turns it on)"
            hub_says = "on" if remote.editing(s) else "off (edit = true in its [[hub.sites]])"
            if s.get("machine"):
                hub_says = "on (a machine of [[console.files.machines]])"
            click.echo(f"       saving scripts and moving files from the Console: the cluster {here}; "
                       f"the hub {hub_says}")
    if user and a.get("ok"):
        b = say(f"it acts as {user}", remote.call(s, "version", user, hub=hub))
        if b.get("ok") and b.get("account") != user:
            ok = False
            click.echo(click.style(f"  no   it answered as {b.get('account')}, not {user}", fg="red"))
        if b.get("ok") and not s.get("machine"):
            c = say(f"{user}'s jobs now", remote.call(s, "jobs", user, hub=hub))
            if c.get("ok"):
                click.echo(f"       {len(c.get('jobs') or [])} job(s)")
        # Moving files (1.55.0), where both sides have editing on: nothing is left behind.
        if b.get("ok") and a.get("edit") and remote.editing(s):
            say("moving files as them (half a file sent, seen and removed; a piece of one taken)",
                files_check(remote, s, hub, user, b.get("home")))
            # Copying a folder (1.58.0): the gate lists what is under one (their home, the first 5)
            if s.get("machine") or _at_least(a.get("nomad"), (1, 58, 0)):
                t = remote.call(s, "tree", user, hub=hub, dir=b.get("home") or "/", most=5)
                say("listing a folder for copying it (their home, the first few)", t if t.get("ok") or
                    "not something the gate does" not in str(t.get("error") or "") else
                    {"ok": False, "error": "the gate is older than 1.58.0"})
    if not ok:
        sys.exit(1)


def _at_least(version, want: tuple) -> bool:
    try:
        return tuple(int(x) for x in str(version).split(".")[:3]) >= want
    except ValueError:
        return False


def files_check(remote, s, hub, user, home) -> dict:
    """My files' requests, as ``user``, leaving nothing: the first half of a
    tiny file sent to their home (only its hidden part is written), how much
    of it is there, that part removed; then a few bytes of a file everyone
    may read (/etc/os-release) taken back."""
    import secrets
    if not isinstance(home, str) or not home.startswith("/"):
        return {"ok": False, "error": "the gate didn't say where their home is"}
    up = secrets.token_hex(8)
    path = f"{home.rstrip('/')}/nomad-gate-test-{up}.txt"
    older = "the cluster's nomad-gate is older than 1.55.0 (not something the gate does: put)"
    sent = remote.call(s, "put", user, hub=hub, data=b"ok", path=path, upload=up, offset=0, total=4)
    if not sent.get("ok"):
        err = str(sent.get("error") or "")
        return {"ok": False, "error": older if "not something the gate does" in err else f"sending: {err}"}
    seen = remote.call(s, "put-status", user, hub=hub, path=path, upload=up)
    gone = remote.call(s, "put-cancel", user, hub=hub, path=path, upload=up)
    if not seen.get("ok") or seen.get("have") != 2:
        return {"ok": False, "error": f"how much is there: {seen.get('error') or seen.get('have')}"}
    if not gone.get("removed"):
        return {"ok": False, "error": f"removing it: {gone.get('error') or 'not removed'}"}
    took = remote.call(s, "get", user, hub=hub, path="/etc/os-release", length=16)
    if not took.get("ok"):
        return {"ok": False, "error": f"taking a piece back: {took.get('error')}"}
    return {"ok": True}


@gate_group.command("ask", context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
@click.option("--site", "site_name", required=True, help="One of the hub's sites.")
@click.option("--user", required=True, help="Who the gate acts as.")
@click.argument("words", nargs=-1, type=click.UNPROCESSED, required=True)
@click.pass_context
def ask_cmd(ctx, site_name, user, words):
    """Ask the gate one thing, as --user; print its answer (JSON).

    \b
      nomad gate ask --site hpc1 --user jdoe jobs
      nomad gate ask --site hpc1 --user jdoe size --script /scratch/jdoe/run/job.sh
      nomad gate ask --site hpc1 --user jdoe show --script /scratch/jdoe/run/job.sh
      nomad gate ask --site hpc1 --user jdoe submit --script /scratch/jdoe/run/job.sh --sha256 HEX --mem 8G
      nomad gate ask --site hpc1 --user jdoe job --job 4815162
    """
    from nomad.submit import remote
    s, hub = _site(ctx, site_name)
    words = list(words)
    doc = remote.ask(s, words[:1] + ["--user", user] + words[1:], hub)
    click.echo(json.dumps(doc, indent=2, sort_keys=True))
    if not doc.get("ok"):
        sys.exit(1)

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad demos`: practice runs (1.34.0). See nomad.demos.kits."""
from __future__ import annotations

import click

from nomad.demos import kits as K


def _config(ctx) -> dict:
    return (ctx.obj or {}).get("config") or {}


@click.group("demos")
def demos_group():
    """Practice runs: small inputs, known to work, that show how a job runs.

    \b
    nomad demos list [--program P] [--site S]   the kits (with --site: whether each fits its template there)
    nomad demos show NAME                        what it does, its files, what to look at
    nomad demos copy NAME [--to DIR]             a copy, as you, in a new folder
    nomad demos look NAME DIR                    once a copy has run in DIR: what to look at
    nomad demos build NAME --to DIR              staff: make a kit's files once, at a site
    nomad demos check --site S                   staff: every kit against the site's templates
    nomad demos scripts --site S --hub --root R --out DIR
                                                 staff: each kit's script, as its template makes it

    A practice run's results mean nothing for research, and its size says
    nothing about what a real calculation needs.
    """


@demos_group.command("list")
@click.option("--program", help="Only this program's kits.")
@click.option("--site", help="Say whether each kit fits that site's template (the hub's nomad.toml).")
@click.pass_context
def demos_list(ctx, program, site):
    """The kits: nomad's own and a site's ([submit.demos] dirs)."""
    config = _config(ctx)
    got, problems = K.kits(config, program=program)
    if not got:
        click.echo("No kits" + (f" for {program}" if program else "") + ".")
    for k in got:
        size = f"{k.cpus} CPU(s), {k.mem}" + (f", {k.gpus} GPU" if k.gpus else "") + f", ~{k.minutes} min"
        state = "" if k.ready else "  [build it once: nomad demos build]"
        click.echo(f"  {k.name:<22} {k.program:<14} {k.licence:<9} {size}{state}")
        if site:
            why = K.check(k, config, site)
            click.echo("      " + (f"{site}: fits its template" if not why else f"{site}: " + "; ".join(why)))
    for p in problems:
        click.echo(f"  ! {p}")
    click.echo(f"\n{K.PRACTICE}")


@demos_group.command("show")
@click.argument("name")
@click.pass_context
def demos_show(ctx, name):
    """A kit: what it does, its files, and what to look at once it has run."""
    try:
        k = K.find(_config(ctx), name)
    except K.KitError as e:
        raise click.ClickException(str(e)) from None
    click.echo(f"{k.title}  ({k.name}: {k.program})\n")
    click.echo(K.PRACTICE + "\n")
    click.echo(k.about + "\n")
    click.echo(f"Licence: {K.LICENCES[k.licence]}." + (f" {k.licence_note}" if k.licence_note else ""))
    click.echo(f"Size: {k.cpus} CPU(s), {k.mem}" + (f", {k.gpus} GPU(s)" if k.gpus else "")
               + f", at most {k.time}; about {k.minutes} minute(s)")
    click.echo(f"Input: {'the folder' if k.input == '.' else k.input}; files: {', '.join(k.files)}"
               + ("" if k.ready else "  (not built yet: nomad demos build)"))
    if k.choose:
        click.echo("Its template's choices: " + ", ".join(f"{o}={n}" for o, n in k.choose.items()))
    for x in k.look:
        click.echo(f"  look at {x['file']}: {x['what']}" + (f"  [{x['match']}]" if x.get("match") else ""))
    click.echo(f"Source: {k.source}" + ("" if k.bundled else f"  (from {k.path})"))


def _with_dirs(config: dict, extra: tuple) -> dict:
    """config with more kit folders before [submit.demos] dirs (--from)."""
    if not extra:
        return config
    sub = dict(config.get("submit") or {})
    demos = dict(sub.get("demos") or {}) if isinstance(sub.get("demos"), dict) else {}
    have = demos.get("dirs", [])
    demos["dirs"] = [str(x) for x in extra] + (list(have) if isinstance(have, list) else [have])
    sub["demos"] = demos
    return {**config, "submit": sub}


@demos_group.command("copy")
@click.argument("name")
@click.option("--to", "dest", type=click.Path(file_okay=False), help="This folder (new, or empty) instead of a new one.")
@click.option("--from", "extra", multiple=True, type=click.Path(file_okay=False),
              help="A folder of kits to look in first (a site's built ones), as [submit.demos] dirs.")
@click.pass_context
def demos_copy(ctx, name, dest, extra):
    """Copy a kit, as you, into a new folder ([submit.demos] into: ~/nomad-demos by default)."""
    config = _with_dirs(_config(ctx), extra)
    try:
        k = K.find(config, name)
        got = K.copy(k, dest=dest, into=K.settings(config).into)
    except (K.KitError, OSError) as e:
        raise click.ClickException(str(e)) from None
    click.echo(f"Copied {k.name} to {got['folder']}: {', '.join(got['files'])}")
    click.echo(f"Its input: {got['input']}")
    click.echo("To run it: the Console's Submit a job page, No script yet? Make one, "
               f"{k.program}, with that input.")
    click.echo(K.PRACTICE)


@demos_group.command("look")
@click.argument("name")
@click.argument("folder", type=click.Path(file_okay=False))
@click.pass_context
def demos_look(ctx, name, folder):
    """Once a copy of kit NAME has run in FOLDER: each thing to look at, and
    the lines that show it."""
    try:
        k = K.find(_config(ctx), name)
    except K.KitError as e:
        raise click.ClickException(str(e)) from None
    for x in K.look(k, folder):
        click.echo(f"{x['file']}: {x['what']}")
        if not x["found"]:
            click.echo("    (not there: has it run, and ended?)")
        for f in x["found"]:
            if "lines" in f:
                for line in f["lines"] or ["    (no line of it says so yet)"]:
                    click.echo("    " + line.strip()[:200])
            else:
                click.echo(f"    {f['path']} ({f['size']} bytes)")
    click.echo(K.PRACTICE)


@demos_group.command("build")
@click.argument("name")
@click.option("--to", "to", required=True, type=click.Path(file_okay=False),
              help="The folder of a site's kits ([submit.demos] dirs): NAME is made in it.")
@click.option("--site", help="Its module folders ([submit.build.SITE] module_paths), from the hub's nomad.toml.")
@click.option("--module", help="Build with this module instead of the kit's.")
@click.option("--module-path", "module_paths", multiple=True,
              help="module use this first (more than once); default: --site's, else none.")
@click.pass_context
def demos_build(ctx, name, to, site, module, module_paths):
    """Staff: make a kit's files once (GROMACS's .tpr, AMBER's topology),
    running its commands as you with its module loaded, in TO/NAME."""
    config = _config(ctx)
    try:
        k = K.find(config, name)
        click.echo(f"Building {k.name} in {to}/{k.name} with {module or k.build['module'] if k.build else '-'}...")
        got = K.build(k, to, config=config, site=site, module=module, module_paths=list(module_paths))
    except (K.KitError, OSError) as e:
        raise click.ClickException(str(e)) from None
    click.echo(f"Made {', '.join(got['made'])} in {got['folder']} (log: {got['log']}).")
    click.echo(f"Offer it with [submit.demos] dirs = [\"{to}\"] in nomad.toml, where the kits are copied.")


@demos_group.command("check")
@click.option("--site", required=True, help="The site whose templates (the hub's nomad.toml) to check against.")
@click.pass_context
def demos_check(ctx, site):
    """Staff: does each kit fit the site's template? Exits 1 if one doesn't."""
    config = _config(ctx)
    got, problems = K.kits(config)
    bad = 0
    from nomad.submit import templates as TT
    offered = {t.program.lower() for t in TT.templates(config, site)[0]}
    for k in got:
        if k.program.lower() not in offered:
            click.echo(f"  -  {k.name:<22} {site} has no {k.program} template: not offered there")
            continue
        why = K.check(k, config, site)
        bad += bool(why)
        click.echo(f"  {'ok' if not why else '!!'} {k.name:<22} " + ("fits" if not why else "; ".join(why)))
    for p in problems:
        click.echo(f"  ! {p}")
        bad += 1
    if bad:
        raise SystemExit(1)


@demos_group.command("scripts")
@click.option("--site", required=True, help="The site whose templates make the scripts (the hub's nomad.toml).")
@click.option("--hub", "on_hub", is_flag=True, help="Its modules from the hub's combined database.")
@click.option("--root", required=True, help="Where the copies will be on the cluster: ROOT/NAME for each kit.")
@click.option("--out", required=True, type=click.Path(file_okay=False), help="Write NAME.sh for each kit here.")
@click.pass_context
def demos_scripts(ctx, site, on_hub, root, out):
    """Staff: each kit's job script, as the site's template makes it for a
    copy in ROOT/NAME (nomad demos copy NAME --to ROOT/NAME), at the kit's
    own size: to send every practice run once and see they work."""
    import posixpath
    import sqlite3
    from pathlib import Path

    from nomad.collectors.modules import available
    from nomad.submit import templates as TT
    config = _config(ctx)
    if not root.startswith("/"):
        raise click.ClickException("--root should be a full path on the cluster")
    if on_hub:
        from nomad.config import combined_db_path
        hub = config.get("hub") if isinstance(config.get("hub"), dict) else {}
        path = combined_db_path(hub.get("output_db"))
    else:
        from nomad.cli import get_db_path
        path = get_db_path(config)
    mods = []
    if Path(path).exists():
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            mods = available(conn, site if on_hub else None)
        finally:
            conn.close()
    offer = {p["program"].lower(): p for p in TT.offer(config, site, mods)["programs"]}
    dest = Path(out).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    dest.chmod(0o755)                       # scripts for others to send: readable, whatever the umask
    got, _ = K.kits(config)
    s = K.settings(config)
    made = 0
    for k in got:
        p = offer.get(k.program.lower())
        if p is None:
            click.echo(f"  -  {k.name:<22} {site} has no {k.program} template")
            continue
        folder = posixpath.join(root, k.name)
        try:
            m = TT.make(config, site, p["program"], module=p["choose"], available=mods,
                        input_path=folder if k.input == "." else posixpath.join(folder, k.input),
                        cpus=k.cpus, mem=k.mem, time=k.time, gpus=k.gpus or None,
                        partition=s.partition_for(site, k.gpus), choose=dict(k.choose) or None)
        except TT.TemplateError as e:
            click.echo(f"  !! {k.name:<22} {e}")
            continue
        target = dest / f"{k.name}.sh"
        if target.exists():
            click.echo(f"  !! {k.name:<22} {target} is there already: not written over")
            continue
        target.write_text(m["text"])
        target.chmod(0o644)
        made += 1
        click.echo(f"  ok {k.name:<22} {target}  ({p['choose']}{'; not built yet' if not k.ready else ''})")
    click.echo(f"{made} script(s) in {dest}. On {site}, as yourself: nomad demos copy NAME --to {root}/NAME, "
               f"put NAME.sh in that folder, and sbatch it there.")

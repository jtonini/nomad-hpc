# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad files`: who the Console's My files is open to (1.60.0).

On the hub, as the account the Console runs as (it reads the combined
database and the lab check's answers): nomad.submit.files says how it works.
"""
from __future__ import annotations

import click

from nomad.submit import files as F

FILES = ("console", "files")


def _ctx(ctx):
    config = (ctx.obj or {}).get("config") or {}
    return config, (ctx.obj or {}).get("config_target")


def _who(config: dict, s) -> str:
    console = config.get("console") if isinstance(config.get("console"), dict) else {}
    submit = str(console.get("submit") or "staff").strip().lower()
    if s.open == "submit":
        return (f"whoever may Submit a job: {'everyone signed in' if submit == 'everyone' else 'staff only'} "
                f"([console] submit = \"{submit}\")")
    if s.open == "labs":
        if not s.labs:
            return 'staff only (open = "labs", but no labs listed)'
        seen = (f", those nomad saw at work in the last {s.active_days} days" if s.active_days
                else ", whether nomad saw them at work or not")
        return f"staff, and the people of {', '.join(s.labs)}{seen} (open = \"labs\")"
    return {"off": 'nobody (open = "off")', "staff": 'staff only (open = "staff")',
            "everyone": 'everyone signed in with the campus sign-in (open = "everyone")'}[s.open]


def _say_labs(s, got: dict, names: bool) -> None:
    for lab in s.labs:
        x = got["labs"].get(lab) or {}
        parts = [f"{len(x.get('members', ()))} in its group now"]
        if x.get("claimed"):
            parts.append(f"{len(x['claimed'])} more their PI said are in the lab")
        if x.get("left"):
            parts.append(f"{len(x['left'])} their PI said have left or aren't in it")
        if got["since"]:
            parts.append(f"{len(x.get('quiet', ()))} not seen at work since {got['since']}")
        click.echo(f"  {lab}: {'; '.join(parts)} -> open to {len(x.get('open', ()))}")
        if not x.get("members") and not x.get("claimed"):
            click.echo(f"    note: nomad knows no one in {lab} now (the groups as the hub last read them)")
        if names and x.get("open"):
            click.echo("    " + " ".join(sorted(x["open"])))
    click.echo(f"In all: {len(got['open'])} people besides staff.")


@click.group("files")
def files_group():
    """Who the Console's My files is open to ([console.files] open), and
    setting it.

    \b
    nomad files status [--names]                     who it is open to, and how many
    nomad files open labs 'kim$' [--active-days 365] [--apply]
                                                     staff and these labs' people seen at work lately
    nomad files open staff|everyone|submit|off [--apply]
    """


@files_group.command("status")
@click.option("--names", is_flag=True, help="With open = \"labs\": the NetIDs it is open to, too.")
@click.pass_context
def files_status(ctx, names):
    """Who My files is open to now; with open = "labs", each lab: its
    members now, those their PI said are in it or have left, those not
    seen at work lately, and how many it is open to."""
    config, target = _ctx(ctx)
    s = F.settings(config)
    for p in s.problems:
        click.echo(f"note: {p}", err=True)
    click.echo(f"My files is open to: {_who(config, s)}")
    if s.open == "labs" and s.labs:
        _say_labs(s, F.lab_people_here(s, config, target), names)


@files_group.command("open")
@click.argument("mode", type=click.Choice(F.OPENS, case_sensitive=False))
@click.argument("labs", nargs=-1)
@click.option("--active-days", type=click.IntRange(0, F.ACTIVE_DAYS_CAP),
              help=f"With labs: only people seen at work in the last this many days (0: all; default "
                   f"{F.ACTIVE_DAYS}, or as it is).")
@click.option("--apply", is_flag=True, help="Write it (otherwise only show it).")
@click.pass_context
def files_open(ctx, mode, labs, active_days, apply):
    """Set who My files is open to: [console.files] open (and, with labs,
    labs and active_days). Shows it first, with how many it would be open
    to; --apply writes it, with a copy of the file before. The Console reads
    it at each request: no restart."""
    from nomad.cli import _lab_edit
    from nomad.config.edit import loads
    mode = mode.lower()
    want: dict = {"open": mode}
    if mode == "labs":
        if not labs:
            raise click.UsageError("which labs? e.g. nomad files open labs 'kim$'")
        want["labs"] = list(dict.fromkeys(labs))
        if active_days is not None:
            want["active_days"] = active_days
    elif labs or active_days is not None:
        raise click.UsageError("labs and --active-days go with: nomad files open labs ...")
    probe = F.settings({"console": {"files": want}})
    if probe.problems:
        raise click.UsageError("; ".join(probe.problems))
    edit = _lab_edit(ctx)
    if edit.assigned(FILES):
        raise click.ClickException("[console.files] is set inside another table here (files = {...}): "
                                   "change it by hand")
    now = ((edit.data.get("console") or {}).get("files") or {})
    now = now if isinstance(now, dict) else {}
    if all(now.get(k) == v for k, v in want.items()):
        click.echo(f"My files is open to: {_who(edit.data, F.settings(edit.data))}. Nothing to change.")
        return
    lines = sorted((i for i, table, k in edit.key_lines() if table == FILES and k and k[0] in want),
                   reverse=True)
    for i in lines:
        try:
            edit.remove_line(i)
        except ValueError:
            raise click.ClickException("a [console.files] setting goes on over several lines: set it by hand") \
                from None
    for k in reversed(list(want)):
        edit.insert_key(FILES, k, want[k])

    def check(d):
        got = ((d.get("console") or {}).get("files") or {})
        return isinstance(got, dict) and all(got.get(k) == v for k, v in want.items()) \
            and F.settings(d).open == mode

    after = loads(edit.text)
    if not check(after):
        raise click.ClickException("the edit didn't read back as meant: nothing written")
    s = F.settings(after)
    config, target = _ctx(ctx)
    if not apply:
        click.echo(f"Would set, in {edit.path} [console.files]: "
                   + ", ".join(f"{k} = {v!r}".replace("'", '"') for k, v in want.items()))
        click.echo(f"My files would be open to: {_who(after, s)}")
        if mode == "labs":
            _say_labs(s, F.lab_people_here(s, config, target), False)
        click.echo("Run again with --apply to write it.")
        return
    try:
        keep = edit.save(check, tag="files-open")
    except ValueError as e:
        raise click.ClickException(f"nothing written: {e}") from None
    click.echo(f"Written in {edit.path}" + (f" (the file before: {keep.name})" if keep else "") + ".")
    click.echo(f"My files is open to: {_who(after, s)}")
    if mode == "labs":
        _say_labs(s, F.lab_people_here(s, config, target), False)
    click.echo("The Console reads it at each request: no restart needed.")

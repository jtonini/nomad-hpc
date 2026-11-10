# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A systemd timer that runs one nomad command as an account (1.39.0): the
weekly notices (``nomad notices timer``), written the way ``nomad lab check
timer`` writes its own."""
from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
from pathlib import Path

import click

UNIT_DIR = Path("/etc/systemd/system")


def account_for(account: str | None, apply: bool) -> str:
    """The account the timer runs as: ``account``, else nomad if there is
    one, else (only to print the units) whoever runs this. Never root."""
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
            raise click.ClickException(f"no account {account} here") from None
    if account == "root":
        raise click.ClickException("not as root: the record is the nomad account's (--user nomad)")
    return account


def install(ctx, name: str, description: str, args: str, when: str, account: str | None, apply: bool,
            force: bool, every_word: str = "daily") -> None:
    """Print, or with ``apply`` install and start, ``name``.service (runs
    ``nomad [-c CONFIG] ARGS`` as the account) and ``name``.timer
    (OnCalendar ``when``)."""
    from nomad.cli import _readable_by, _unit_safe, _unit_settings, resolve_config_path
    if any(ch in (when + (account or "")) for ch in "\n\r"):
        raise click.UsageError("--at and --user are one line")
    account = account_for(account, apply)
    if shutil.which("systemd-analyze"):
        r = subprocess.run(["systemd-analyze", "calendar", when], capture_output=True, text=True)
        if r.returncode != 0:
            raise click.ClickException(f"--at {when!r}: systemd doesn't read it ({(r.stderr or r.stdout).strip()})")
    _unit_safe(account, "--user")
    exe = Path(sys.executable).with_name("nomad")
    nomad_exe = _unit_safe(str(exe) if exe.exists() else (shutil.which("nomad") or "nomad"), "nomad")
    target = Path((ctx.obj or {}).get("config_target") or resolve_config_path()).expanduser()
    config = _unit_safe(str(target.resolve()), "nomad.toml") if target.exists() else None
    if config and not _readable_by(Path(config), account):
        msg = f"{account} can't read {config}, so it would fail"
        if apply:
            raise click.ClickException(msg)
        click.echo(f"(note: {msg})")
    run = nomad_exe + (f" -c {config}" if config else "") + f" {args}"
    service = (f"[Unit]\nDescription={description}\nAfter=network-online.target\n\n"
               f"[Service]\nType=oneshot\nUser={account}\nExecStart={run}\n")
    timer = (f"[Unit]\nDescription={description}, {every_word}\n\n"
             f"[Timer]\nOnCalendar={when}\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")
    paths = {UNIT_DIR / f"{name}.service": service, UNIT_DIR / f"{name}.timer": timer}
    if not apply:
        for p, text in paths.items():
            click.echo(f"# {p}")
            click.echo(text)
        click.echo("Install with --apply (as root), or by hand:")
        click.echo("  sudo systemctl daemon-reload")
        click.echo(f"  sudo systemctl enable --now {name}.timer")
        return
    if not shutil.which("systemctl"):
        raise click.ClickException(f"no systemctl here: run it from cron instead, as {account}:\n  {run}")
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

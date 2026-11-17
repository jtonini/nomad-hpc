# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad metrics`: what nomad publishes for Prometheus (nomad.metrics, 1.52.0)."""
from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import click

from nomad import metrics as M

UNIT = "nomad-metrics.service"


def _settings(ctx) -> M.Settings:
    s = M.settings((ctx.obj or {}).get("config") or {})
    for p in s.problems:
        click.echo(f"note: {p}", err=True)
    return s


def _db(ctx, db) -> Path:
    from nomad.facts.cli import _db as facts_db
    return facts_db(ctx, db)


def _page(ctx, db, s: M.Settings) -> tuple[Path, M.Page]:
    from nomad.facts import open_readonly
    path = _db(ctx, db)
    try:
        conn = open_readonly(path)
    except FileNotFoundError:
        raise click.ClickException(f"no database at {path} (give one with --db)") from None
    try:
        return path, M.build(conn, datetime.now(), s=s)
    finally:
        conn.close()


def _token_line(s: M.Settings) -> str:
    if s.token_file is None:
        return "no token (only this host can ask)" if M.loopback(s.host) else "no token_file: it won't serve there"
    try:
        token = M.read_token(s)
    except ValueError as e:
        return f"token: {e}"
    return f"token: {s.token_file} (fingerprint {M.fingerprint(token)})"


@click.group("metrics", invoke_without_command=True)
@click.option("--summary", "show_summary", is_flag=True,
              help="Counts only: how many of each, for which sites, and what was left out as too old.")
@click.option("--catalog", is_flag=True, help="Each name it publishes, and what it is, in words.")
@click.option("--out", "out", type=click.Path(dir_okay=False),
              help="Write the page to this file instead (node_exporter's textfile collector reads *.prom); "
                   "replaced whole, never half written.")
@click.option("--db", type=click.Path(dir_okay=False),
              help="This database (default: the hub's combined one, else this host's).")
@click.pass_context
def metrics_group(ctx, show_summary, catalog, out, db):
    """What nomad publishes for Prometheus: each site's last pull and
    collectors, each node's condition, cores, CPU clock and power
    problems, each GPU's use, each partition's jobs and wait, each
    filesystem's space. Nothing about one person.

    \b
    nomad metrics                 the page, as Prometheus would get it
    nomad metrics --summary       counts only (safe to share)
    nomad metrics --catalog       each name and what it is
    nomad metrics --out FILE.prom for node_exporter's textfile collector
    nomad metrics dashboard       a Grafana dashboard for it (JSON)
    nomad metrics serve           answer Prometheus ([metrics] enabled = true)
    nomad metrics token [--apply] a token for Prometheus to send
    nomad metrics service [--apply]  run `serve` from systemd

    A reading counts until its next was due, plus [metrics] grace_minutes;
    then it is left out (a gap in Grafana), not repeated."""
    if ctx.invoked_subcommand:
        return
    if catalog:
        for name, (text, labels) in M.CATALOG.items():
            click.echo(f"{name}{{{','.join(labels)}}}")
            click.echo(f"    {text}")
        return
    s = _settings(ctx)
    path, page = _page(ctx, db, s)
    if show_summary:
        from nomad import __version__
        click.echo(f"nomad metrics {__version__}: from {path}, at {page.generated_at:%Y-%m-%d %H:%M}.")
        if s.enabled:
            click.echo(f"Serving: on ([metrics] enabled), at http://{s.listen}/metrics; {_token_line(s)}.")
        else:
            click.echo(f"Serving: off ([metrics] enabled = true turns it on; it would answer at "
                       f"http://{s.listen}/metrics; {_token_line(s)}).")
        click.echo(f"A reading counts until its next was due, plus {s.grace_minutes:g} minutes.")
        for line in M.summary(page):
            click.echo(line)
        return
    text = M.render(page)
    if out:
        target = Path(out).expanduser()
        tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(text)
            tmp.chmod(0o644)
            os.replace(tmp, target)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise click.ClickException(f"{target}: {e.strerror or e}") from None
        return
    sys.stdout.write(text)


@metrics_group.command("dashboard")
@click.option("--out", "out", type=click.Path(dir_okay=False), help="Write it to this file instead.")
@click.pass_context
def dashboard_cmd(ctx, out):
    """A Grafana dashboard for what nomad publishes, as JSON: import it
    (Dashboards > New > Import) or put it in a provisioning folder. It asks
    which Prometheus, and which sites."""
    import json

    from nomad.metrics_dashboard import dashboard
    text = json.dumps(dashboard(), indent=2, ensure_ascii=False) + "\n"
    if not out:
        sys.stdout.write(text)
        return
    target = Path(out).expanduser()
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.chmod(0o644)
        os.replace(tmp, target)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise click.ClickException(f"{target}: {e.strerror or e}") from None
    click.echo(f"Wrote {target}.")


@metrics_group.command("serve")
@click.option("--listen", help="address:port (default: [metrics] listen, 127.0.0.1:10062).")
@click.option("--db", type=click.Path(dir_okay=False),
              help="This database (default: the hub's combined one, else this host's).")
@click.pass_context
def serve_cmd(ctx, listen, db):
    """Answer Prometheus at http://ADDRESS:PORT/metrics until stopped.
    Only when [metrics] enabled = true; on an address other hosts can
    reach, only with [metrics] token_file (Prometheus sends it as a bearer
    token). The page is built again at most every cache_seconds."""
    s = _settings(ctx)
    if not s.enabled:
        raise click.ClickException("serving is off: [metrics] enabled = true in nomad.toml turns it on")
    host, port = s.host, s.port
    if listen:
        try:
            host, port = M.split_listen(listen)
        except ValueError:
            raise click.UsageError(f"--listen {listen!r}: an address and port, like {M.DEFAULT_LISTEN}") from None
    try:
        token = M.read_token(s)
    except ValueError as e:
        raise click.ClickException(str(e)) from None
    if token is None and not M.loopback(host):
        raise click.ClickException(f"{host} can be reached from other hosts: set [metrics] token_file "
                                   "(`nomad metrics token --apply` writes one), or listen on 127.0.0.1")
    path = _db(ctx, db)
    if not path.exists():
        raise click.ClickException(f"no database at {path} (give one with --db)")

    def page() -> str:
        from nomad.facts import open_readonly
        conn = open_readonly(path)
        try:
            return M.render(M.build(conn, datetime.now(), s=s))
        finally:
            conn.close()

    def log(text: str) -> None:
        click.echo(text, err=True)
    try:
        server = M.make_server(page, host, port, token, s.cache_seconds, log)
    except OSError as e:
        raise click.ClickException(f"can't listen on {host}:{port}: {e.strerror or e}") from None
    shown = f"[{host}]" if ":" in host else host
    log(f"nomad metrics: answering at http://{shown}:{port}/metrics from {path}"
        + (f", token fingerprint {M.fingerprint(token)}" if token else ", no token"))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@metrics_group.command("token")
@click.option("--apply", is_flag=True, help="Write it.")
@click.option("--force", is_flag=True, help="Replace a token already there (Prometheus needs the new one).")
@click.pass_context
def token_cmd(ctx, apply, force):
    """A new token in [metrics] token_file, readable by this account only.
    Run it as the account that serves (sudo -u nomad). It never prints the
    token: copy the file to the Prometheus host (its credentials_file)."""
    import secrets
    s = _settings(ctx)
    if s.token_file is None:
        raise click.ClickException("[metrics] token_file is not set: say where the token goes first")
    p = s.token_file
    if os.geteuid() == 0:
        raise click.ClickException("not as root: the service reads it as its own account (sudo -u nomad ...)")
    if p.exists() and not force:
        try:
            have = M.read_token(s)
            raise click.ClickException(f"{p} already holds a token (fingerprint {M.fingerprint(have)}); "
                                       "--force replaces it")
        except ValueError as e:
            raise click.ClickException(f"{e}; --force writes a new one") from None
    if not apply:
        click.echo(f"Would write a new token to {p} (mode 600). Again with --apply to write it.")
        return
    token = secrets.token_urlsafe(32)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.replace(tmp, p)
    click.echo(f"Wrote {p} (fingerprint {M.fingerprint(token)}). Copy it to the Prometheus host, readable "
               "by Prometheus only, and name it there as the scrape's credentials_file.")


@metrics_group.command("service")
@click.option("--user", "account", help="The account it runs as (default: nomad, if there is one).")
@click.option("--apply", is_flag=True, help="Install and start it (as root).")
@click.option("--force", is_flag=True, help="Replace a unit installed with other settings.")
@click.pass_context
def service_cmd(ctx, account, apply, force):
    """A systemd service that runs `nomad metrics serve` as the nomad
    account (or --user), started again if it stops. Without --apply it
    prints the unit."""
    import shutil
    import subprocess

    from nomad.cli import _readable_by, _unit_safe, _unit_settings, resolve_config_path
    from nomad.timers import UNIT_DIR, account_for
    if any(ch in (account or "") for ch in "\n\r"):
        raise click.UsageError("--user is one line")
    s = _settings(ctx)
    try:
        account = account_for(account, apply)
    except click.ClickException as e:
        if "not as root" in e.message:
            raise click.ClickException("not as root: it serves as the nomad account (--user nomad)") from None
        raise
    _unit_safe(account, "--user")
    exe = Path(sys.executable).with_name("nomad")
    nomad_exe = _unit_safe(str(exe) if exe.exists() else (shutil.which("nomad") or "nomad"), "nomad")
    target = Path((ctx.obj or {}).get("config_target") or resolve_config_path()).expanduser()
    config = _unit_safe(str(target.resolve()), "nomad.toml") if target.exists() else None
    problems = []
    if config and not _readable_by(Path(config), account):
        problems.append(f"{account} can't read {config}, so it would fail")
    if not s.enabled:
        problems.append("[metrics] enabled is not true, so it would stop at once")
    if s.token_file is None and not M.loopback(s.host):
        problems.append(f"[metrics] listen = {s.listen} needs a token_file")
    elif s.token_file is not None and not _readable_by(s.token_file, account):
        problems.append(f"{account} can't read {s.token_file}")
    run = nomad_exe + (f" -c {config}" if config else "") + " metrics serve"
    unit = ("[Unit]\nDescription=NOMAD metrics for Prometheus (nomad metrics serve)\n"
            "After=network-online.target\n\n"
            f"[Service]\nType=simple\nUser={account}\nExecStart={run}\nRestart=on-failure\nRestartSec=10\n"
            "NoNewPrivileges=true\n\n[Install]\nWantedBy=multi-user.target\n")
    path = UNIT_DIR / UNIT
    if not apply:
        click.echo(f"# {path}")
        click.echo(unit)
        for p in problems:
            click.echo(f"(note: {p})")
        click.echo("Install with --apply (as root), or by hand:")
        click.echo("  sudo systemctl daemon-reload")
        click.echo(f"  sudo systemctl enable --now {UNIT}")
        return
    if problems:
        raise click.ClickException("; ".join(problems))
    if not shutil.which("systemctl"):
        raise click.ClickException(f"no systemctl here: run it another way, as {account}:\n  {run}")
    if os.geteuid() != 0:
        raise click.ClickException(f"--apply writes to {UNIT_DIR}: run it as root (sudo)")
    same = path.exists() and _unit_settings(path.read_text()) == _unit_settings(unit)
    if path.exists() and not same and not force:
        raise click.ClickException(f"{path} already there with other settings (--force replaces it)")
    if not same:
        path.write_text(unit)
        path.chmod(0o644)
    steps = [["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", UNIT]]
    if not same and path.exists():
        steps.append(["systemctl", "restart", UNIT])
    for cmd in steps:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise click.ClickException(f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()}")
    click.echo("already installed; enabled and running" if same else "installed; enabled and running")

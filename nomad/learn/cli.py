# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad ml`: training the model from the Console (1.45.0); nomad.learn says how it works."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path

import click

from nomad.learn import records as R
from nomad.learn.settings import read_key, settings, worker_settings


def _config(ctx) -> tuple[dict, object]:
    obj = ctx.find_root().obj or {}
    return obj.get("config") or {}, obj.get("config_target")


def _hub(ctx):
    config, target = _config(ctx)
    s = settings(config)
    for p in s.problems:
        click.echo(f"note: {p}", err=True)
    return config, target, s, R.db_path(config, target)


@click.group("ml")
def ml_group():
    """Train the model from the Console: TESSERA on a worker with a GPU.

    \b
    On the hub (as the nomad account):
      nomad ml status [--json]              the request queued or running, the last result
      nomad ml ask [--note "..."]           queue a request (as Data readiness's Train...)
      nomad ml cancel ID                    a queued request
      nomad ml scores ID failure|waste [--part now|holdout] [--top N]   a run's riskiest jobs
      nomad ml key                          make the worker's key (once), and say its fingerprint
    On the worker ([ml.worker]):
      nomad ml key                          the key copied from the hub: its fingerprint
      nomad ml check                        can it reach the Console, with its key?
      nomad ml work                         one pass: take a request, if any, train, answer
      nomad ml timer [--apply]              do that every 5 minutes (a systemd timer)
    """


def _say(r: dict | None) -> str:
    if not r:
        return "none"
    when = (r.get("finished_at") or r.get("progress_at") or r.get("asked_at") or "")[:16].replace("T", " ")
    tail = f": {r['error']}" if r.get("error") else (f": {r['progress']}" if r.get("progress") and
                                                     r["state"] == "running" else "")
    return f"request {r['id']} {r['state']} ({when}, asked by {r['asked_by']})" + tail


def _target_lines(summary: dict) -> list[str]:
    lines = []
    for t, x in (summary.get("targets") or {}).items():
        if x.get("skipped"):
            lines.append(f"  {t}: {x['skipped']}")
            continue
        auc, top = x.get("auc") or {}, x.get("top_tenth") or {}
        rate = x.get("rate_holdout")
        line = f"  {t}: {x['train']} jobs learned from, {x['holdout']} newest held out"
        if rate is not None:
            line += f" ({round(100 * rate)}% of them {'failed' if t == 'failure' else 'wasted their time'})"
        lines.append(line)
        for name, key in (("TESSERA", "tessera"), ("logistic regression", "baseline")):
            if auc.get(key) is None and top.get(key) is None:
                continue
            lines.append(f"    {name}: AUC {auc.get(key)}; of the tenth it ranked riskiest, "
                         f"{'–' if top.get(key) is None else str(round(100 * top[key])) + '%'} did")
        if x.get("weights"):
            lines.append("    TESSERA's blend: " + ", ".join(f"{k} {v}" for k, v in x["weights"].items()))
    return lines


@ml_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="As JSON.")
@click.pass_context
def ml_status(ctx, as_json):
    """The request queued or running, and the last result."""
    config, target, s, path = _hub(ctx)
    got = R.status(R.open_records(path), s)
    if as_json:
        click.echo(json.dumps(got, indent=2))
        return
    click.echo(f"Training from the Console: {'on' if s.enabled else 'off ([ml.train] enabled)'}; engine {s.engine}; "
               f"predicts {', '.join(s.predicts)}; trainer {s.trainer or '(not named)'}; records {path}")
    click.echo(f"Now: {_say(got['current'])}")
    last = got["last"]
    click.echo(f"Last result: {_say(last)}")
    if last and last.get("summary"):
        sm = last["summary"]
        click.echo(f"  on {last.get('worker')}, {sm.get('engine')} on {sm.get('device') or 'cpu'}, "
                   f"{sm.get('seconds')} s")
        for line in _target_lines(sm):
            click.echo(line)


def _write(path):
    try:
        return R.open_records(path, write=True)
    except R.RequestError as e:
        raise click.ClickException(str(e)) from None


@ml_group.command("ask")
@click.option("--note", help="Why, for the record.")
@click.option("--by", help="Who asks (default: who runs this).")
@click.pass_context
def ml_ask(ctx, note, by):
    """Queue a training request with [ml.train]'s settings now."""
    import getpass
    config, target, s, path = _hub(ctx)
    try:
        r = R.ask(_write(path), s, by or getpass.getuser(), note)
    except R.RequestError as e:
        raise click.ClickException(str(e)) from None
    click.echo(f"{_say(r)}: the worker takes it when it next asks ({s.trainer or '[ml.worker]'}).")


@ml_group.command("cancel")
@click.argument("rid", type=int)
@click.pass_context
def ml_cancel(ctx, rid):
    """Cancel a queued request (one running finishes or is given up)."""
    import getpass
    config, target, s, path = _hub(ctx)
    try:
        click.echo(_say(R.cancel(_write(path), rid, getpass.getuser())))
    except R.RequestError as e:
        raise click.ClickException(str(e)) from None


@ml_group.command("scores")
@click.argument("rid", type=int)
@click.argument("target", type=click.Choice(["failure", "waste"]))
@click.option("--part", type=click.Choice(["now", "holdout"]), default="now", show_default=True)
@click.option("--top", default=20, show_default=True)
@click.pass_context
def ml_scores(ctx, rid, target, part, top):
    """A run's riskiest jobs: those running or waiting then, or the holdout's (with what became of them)."""
    config, target_, s, path = _hub(ctx)
    rows = R.scores(R.open_records(path), rid, target, part, top)
    if not rows:
        click.echo("No scores (an older run's are pruned: [ml.train] keep_runs).")
    for r in rows:
        did = "" if r["label"] is None else ("  (did)" if r["label"] else "  (didn't)")
        click.echo(f"{r['site'] or '':<10} {r['job_id']:<14} TESSERA {r['risk']}  logistic {r['baseline']}{did}")


def _fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:16]


@ml_group.command("key")
@click.pass_context
def ml_key(ctx):
    """The worker's key: made on the hub when there is none ([ml.train] key_file,
    the nomad account's, 600); on the worker, the copy of it ([ml.worker]
    key_file). Either way only its fingerprint is said: compare them."""
    config, _ = _config(ctx)
    ws = worker_settings(config)
    on_worker = bool((config.get("ml") or {}).get("worker"))
    path = Path(ws.key_file if on_worker else settings(config).key_file).expanduser()
    key = read_key(str(path))
    if key is None and not on_worker:
        if path.exists():
            raise click.ClickException(f"{path} is there but isn't a key (32 characters or more): look at it")
        if os.geteuid() == 0:
            raise click.ClickException("as the account the Console runs as: sudo -u nomad nomad ml key")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
        key = read_key(str(path))
        click.echo(f"made {path}")
    if key is None:
        raise click.ClickException(f"no key in {path}: copy the hub's there (600)")
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        click.echo(f"note: {path} is mode {oct(mode)}: chmod 600 {path}", err=True)
    click.echo(f"{path}: fingerprint {_fingerprint(key)}")


@ml_group.command("check")
@click.pass_context
def ml_check(ctx):
    """On the worker: reach the Console with the key, and say what it said."""
    from nomad.learn.worker import Client, WorkerError
    config, _ = _config(ctx)
    ws = worker_settings(config)
    for p in ws.problems:
        click.echo(f"note: {p}", err=True)
    try:
        got = Client(ws).post("/api/ml/worker/hello", {"worker": ws.name})
    except WorkerError as e:
        raise click.ClickException(str(e)) from None
    click.echo(f"{ws.console}: {'on' if got.get('enabled') else 'off ([ml.train] enabled on the hub)'}; "
               f"engine {got.get('engine')}; queued {got.get('queued', 0)}; this worker is {ws.name} "
               f"(device {ws.device})")
    click.echo(_can_train(ws))


def _can_train(ws) -> str:
    """Whether TESSERA can train here, on what: a line (nothing is trained)."""
    from nomad.learn import train
    v = train._versions()
    if not v.get("tessera-ml"):
        return "TESSERA isn't installed here: pip install 'nomad-hpc[predict]' (in nomad's environment)"
    try:
        used = train._device(ws.device)
    except train.TrainError as e:
        return f"TESSERA {v['tessera-ml']} is here, but: {e}"
    gpu = ""
    if used == "cuda":
        import torch
        gpu = f" ({torch.cuda.get_device_name(0)})"
    return f"training here: TESSERA {v['tessera-ml']}, PyTorch {v.get('torch')}, on {used}{gpu}"


@ml_group.command("work")
@click.pass_context
def ml_work(ctx):
    """On the worker: one pass. Takes the oldest queued request, if any,
    trains, and sends back the result (or why it failed)."""
    from nomad.learn.train import TrainError
    from nomad.learn.worker import WorkerError, work_once
    import logging
    import sys
    for name in ("matplotlib", "PIL"):          # their INFO lines (a font cache built once) aren't the worker's
        logging.getLogger(name).setLevel(logging.WARNING)
    for stream in (sys.stdout, sys.stderr):    # under systemd, each line to the journal as it is said (1.45.1)
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass
    config, _ = _config(ctx)
    ws = worker_settings(config)
    for p in ws.problems:
        click.echo(f"note: {p}", err=True)
    try:
        click.echo(work_once(ws, say=click.echo))
    except (WorkerError, TrainError) as e:
        raise click.ClickException(str(e)) from None


@ml_group.command("timer")
@click.option("--at", "when", default="*:0/5", show_default=True, help="When, as systemd's OnCalendar says it.")
@click.option("--user", "account", help="The account it runs as (default: nomad, else who runs this).")
@click.option("--apply", is_flag=True, help="Install and start it (as root); else print the units.")
@click.option("--force", is_flag=True, help="Replace units that differ.")
@click.pass_context
def ml_timer(ctx, when, account, apply, force):
    """On the worker: `nomad ml work` from a systemd timer (nomad-ml-worker),
    every 5 minutes; a run still training isn't started again."""
    from nomad import timers
    timers.install(ctx, "nomad-ml-worker", "NOMAD training worker: takes the Console's training requests",
                   "ml work", when, account, apply, force, every_word="every 5 minutes")

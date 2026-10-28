# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad size` and `nomad submit`: what a job should ask for, and sending
it with that.

Run by the person, on a cluster's login node: nomad reads their script (and
the input file it names), their own past jobs from Slurm's accounting, and
the cluster's partitions, and says, value by value, what to ask for and
why. `nomad size` stops there. `nomad submit` then runs sbatch -- as the
person, since it is their own run of nomad -- with the values they keep or
choose, and tags the job with who chose each one (Slurm's "extra" field):
so nomad's figures can later tell the person's choices from nomad's.
Nothing is stored by nomad.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass, field

import click

from nomad.submit import apps, cluster, forms, history, sizing, spec


class NotYou(click.ClickException):
    pass


def me() -> str:
    """Who is running nomad, from the system (never from $USER, which the
    person can set)."""
    try:
        import pwd                     # POSIX only: nomad itself must load elsewhere
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, AttributeError):
        raise NotYou("nomad can't tell who you are here (no user name for your uid)")


def read_text(path: str, limit: int) -> str | None:
    """A regular file's text, at most ``limit`` bytes; None when it can't
    be read (missing, not a file, too big, not text)."""
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        with open(path, "rb") as f:
            data = f.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit or b"\x00" in data[:4096]:
        return None
    return data.decode("utf-8", errors="replace")


def _own_chdir(path: str) -> str | None:
    """The --chdir a script's own #SBATCH lines set (None: none, or it
    can't be read)."""
    text = read_text(path, spec.MAX_SCRIPT)
    return spec.script_options(text).get("chdir") if text is not None else None


def locate(script: str, workdir: str | None, moved: bool) -> tuple[str | None, bool]:
    """Where a job's script was, from the name sbatch was given and the
    folder Slurm keeps (WorkDir: where the job ran), and whether that is a
    guess. sbatch found a relative script from the folder it ran in: the
    job's folder, unless --chdir took the job elsewhere -- on its command
    line (``moved``: that folder isn't kept, so a guess), or in the script
    itself (then it is the folder that script's --chdir leads from to
    WorkDir)."""
    if script.startswith("/"):
        p = posixpath.normpath(script)
        return (p, False) if os.path.isfile(p) else (None, False)
    if not workdir:
        return None, False
    here = posixpath.normpath(workdir)
    direct = posixpath.normpath(posixpath.join(here, script))
    if moved:
        return (direct, True) if os.path.isfile(direct) else (None, False)
    stays = os.path.isfile(direct) and _own_chdir(direct) is None
    came, absolute = None, False                   # a script whose own --chdir leads here
    up = here
    for _ in range(8):
        cand = posixpath.normpath(posixpath.join(up, script))
        d = _own_chdir(cand) if os.path.isfile(cand) else None
        if d is not None and posixpath.normpath(posixpath.join(up, os.path.expanduser(d))) == here:
            came, absolute = cand, d.startswith(("/", "~"))
            break
        if up == "/":
            break
        up = posixpath.dirname(up)
    if stays:
        return direct, came is not None            # both could have run it: a guess
    if came:
        return came, absolute                      # an absolute --chdir: it could be anywhere
    return (direct, True) if os.path.isfile(direct) else (None, False)


def name_programs(runs: list, limit: int = 120) -> None:
    """Each past run's script (where it was: see ``locate``; a guess isn't
    kept) and program, from that script as it is now (the person's own
    files; at most ``limit`` scripts looked for)."""
    found: dict = {}
    seen: dict = {}
    for r in runs:
        sub = spec.parse_submit_line(r.submit_line) if r.submit_line else None
        if sub is None or not sub.readable or not sub.script or sub.wrap is not None:
            continue
        key = (sub.script, r.workdir, sub.get("chdir") is not None)
        if key not in found:
            if len(found) >= limit:
                r.script = None                     # not looked for: not compared as "this script"
                continue
            path, guessed = locate(*key)
            found[key] = None if guessed else path
        r.script = found[key]
        if not r.script:
            continue
        if r.script not in seen:
            text = read_text(r.script, spec.MAX_SCRIPT)
            seen[r.script] = apps.program_of(spec.sbatch_lines(text)[1]) if text else None
        prog = seen[r.script]
        if prog is not None:
            r.family, r.executable = prog.family, prog.executable


@dataclass
class Plan:
    """Everything `nomad size` works out, and what `nomad submit` sends from."""
    sizing: sizing.Sizing
    script: str | None                  # as found (absolute for a job sent again)
    folder: str                         # where the job runs (sbatch's folder, or the job's)
    job: str | None = None
    outcome: history.PastRun | None = None
    line: spec.SubmitLine | None = None  # a job sent again: its own command line
    script_sub: spec.SubmitLine = field(default_factory=spec.SubmitLine)
    needs: apps.Needs | None = None
    notes: list = field(default_factory=list)
    args_unsure: bool = False           # its arguments read back from Slurm without their quotes
    script_unsure: bool = False         # a job sent again: its script found, but not for certain
    submit_dir: str | None = None       # where sbatch runs (the person's folder; a job sent again: its own)
    whole_array: bool = False           # --again gave an array's own number: all its tasks again

    def to_dict(self) -> dict:
        out = self.sizing.to_dict()
        out["notes"] = list(self.notes) + out["notes"]
        out.update(script=self.script, folder=self.folder, job=self.job,
                   needs=self.needs.to_dict() if self.needs else None,
                   outcome=self.outcome.state if self.outcome is not None else None,
                   unsure=list(self.sizing.sub.unsure), args_unsure=self.args_unsure,
                   script_unsure=self.script_unsure)
        return out


def _looks_like_a_script(path: str) -> bool:
    text = read_text(path, spec.MAX_SCRIPT)
    return text is not None and (text.startswith("#!") or "#SBATCH" in text)


def _rerun_script(line: spec.SubmitLine, workdir: str | None) -> tuple[str | None, bool, int | None]:
    """The script a job ran, from its submit line: the one sbatch was
    given, else (Slurm keeps the line without its quotes, so a quoted job
    name splits) the first word after the options that is a job script;
    whether that was a guess; and where among those words it was (what
    follows it are its arguments)."""
    moved = line.get("chdir") is not None
    if line.script:
        p, guessed = locate(line.script, workdir, moved)
        if p:
            return p, guessed, 0
    for i, w in enumerate(line.rest[1:], 1):
        p, _guessed = locate(w, workdir, moved)
        if p and _looks_like_a_script(p):
            return p, True, i
    return None, False, None


def plan(script_path: str | None, *, job: str | None = None, user: str | None = None,
         days: int = 180, db: str | None = None, site: str | None = None, args: list | None = None,
         run_sacct=history._run, run_slurm=cluster._run) -> Plan:
    """Size a script before it runs, or a job of the person's to run again."""
    user = user or me()
    notes: list = []
    outcome = None
    line = None
    unsure = False
    script_unsure = False
    whole_array = False
    if job:
        try:
            outcome = history.job_from_sacct(job, user, site=site, run=run_sacct)
        except history.HistoryError as e:
            raise click.ClickException(str(e))
        if outcome is None:
            raise click.ClickException(f"no job {job} of {user}'s in Slurm's accounting here")
        if "_" not in str(job) and outcome.array_job == str(job):
            whole_array = True
            notes.append(f"Job {job} is an array: sized from task {outcome.array_task}'s record, and sent "
                         f"again whole (one task alone: {job}_{outcome.array_task}).")
        line = spec.parse_submit_line(outcome.submit_line)
        if script_path is None:
            script_path, guessed, at = _rerun_script(line, outcome.workdir)
            script_unsure = guessed
            if at:
                line.script, line.args = line.rest[at], list(line.rest[at + 1:])
            if script_path is None:
                notes.append("Slurm's record doesn't say which script this job ran (or it came on "
                             "standard input, or has moved): sized from the job's own settings.")
            elif at:
                notes.append("Slurm keeps the command line without its quotes: nomad found the script "
                             "among its words. Check the command before sending it.")
            elif guessed:
                notes.append(f"Slurm keeps the folder the job ran in, not the one sbatch ran in: {script_path} "
                             "may not be the script that ran (--chdir moved the job, or another script "
                             "could have). Check it before sending it.")
        if line.args:
            unsure = True
    text = read_text(script_path, spec.MAX_SCRIPT) if script_path else None
    if script_path and text is None:
        if job:
            notes.append(f"{script_path} can't be read now (moved, or changed): sized from the job's "
                         "own settings.")
        else:
            raise click.ClickException(f"{script_path}: not a job script nomad can read (missing, "
                                       f"not a file, or over {spec.MAX_SCRIPT // 1024} KB)")
    script_sub = spec.script_options(text or "")
    sub = spec.merged(script_sub, line) if line is not None else script_sub
    if outcome is not None and text is None:
        # No script to read: what the job ran with, as Slurm recorded it,
        # for what its own options don't say (theirs come after, and win).
        had = []
        nodes = spec.request_of(sub).nodes or outcome.nodes or 1
        if not sub.has("nodes") and nodes > 1:
            had.append(("nodes", str(nodes)))
        if not any(sub.has(o) for o in forms.CPU_OPTIONS + ("cpus-per-gpu",)) and outcome.cpus:
            n = outcome.tasks or 1
            if n > 1 or nodes > 1:
                if n <= 1 or outcome.cpus % n:
                    n = outcome.cpus                       # how they were placed isn't known: a CPU each
                had.append(("ntasks", str(n)))
                if outcome.cpus // n > 1:
                    had.append(("cpus-per-task", str(outcome.cpus // n)))
            else:
                had += [("ntasks", "1"), ("cpus-per-task", str(outcome.cpus))]
        if not any(sub.has(o) for o in forms.MEM_OPTIONS) and outcome.mem_mb:
            had.append(("mem", spec.slurm_mem(outcome.mem_mb)))
        if not sub.has("time") and outcome.time_s:
            had.append(("time", sizing.format_time_slurm(outcome.time_s)))
        if not sub.has("partition") and outcome.partition:
            had.append(("partition", outcome.partition))
        if not any(sub.has(o) for o in forms.GPU_OPTIONS) and outcome.gpus:
            had.append(("gpus", str(outcome.gpus)))        # in all (--gres would be per node)
        sub.options = had + sub.options
    if spec.request_of(sub).job_name is None and script_path:
        sub.options.append(("job-name", posixpath.basename(script_path)))   # sbatch names a job after its script
    # Where the job runs: a job sent again, its own folder; else as sbatch
    # does, --chdir or the folder sbatch is run in.
    here_now = os.getcwd()
    if outcome is not None and outcome.workdir:
        folder = outcome.workdir
    else:
        d = sub.get("chdir")
        folder = os.path.normpath(os.path.join(here_now, os.path.expanduser(d))) if d else here_now
    commands = spec.sbatch_lines(text or "")[1]
    program = apps.program_of(commands) if text else apps.Program(None, None)
    needs = None
    path = None
    if program.input:
        here = folder
        if program.cd == "?":
            here = None
        elif program.cd:
            cd = os.path.expanduser(program.cd)
            here = cd if cd.startswith("/") else os.path.join(folder, cd)
        path = program.input if program.input.startswith("/") else (
            os.path.join(here, program.input) if here else None)
        if path is None:
            notes.append(f"The script changes folder in a way nomad doesn't follow (a variable, cd -, "
                         f"popd) before {program.executable}: {program.input} wasn't looked for.")
    if program.family in apps.READERS or program.family in apps.COMMANDS:
        # The command line's own sizes count even when the input can't be read.
        needs = apps.needs(program, lambda _p: read_text(path, apps.MAX_INPUT) if path else None)
        if path and program.family in apps.READERS and "$" not in program.input \
                and read_text(path, apps.MAX_INPUT) is None:
            beside = os.path.join(os.path.dirname(os.path.abspath(script_path)), program.input) \
                if script_path else None
            if beside and beside != path and os.path.isfile(beside):
                notes.append(f"{program.input} isn't in {os.path.dirname(path)}, where the job will run; it "
                             "is next to the script. Submit from that folder (or give --chdir).")
            else:
                notes.append(f"{program.input} couldn't be read in {os.path.dirname(path)}: sizes not taken "
                             "from it.")
    try:
        if db:
            runs = history.from_db(db, user, site=site)
        else:
            runs = history.from_sacct(user, days=days, site=site, run=run_sacct)
            name_programs(runs)
    except history.HistoryError as e:
        runs = []
        notes.append(f"Your past jobs couldn't be read ({e}): nothing to compare with.")
    parts = [] if db else cluster.live(run=run_slurm)
    access = None if db else cluster.my_access(user, run=run_slurm)
    abs_script = posixpath.normpath(os.path.abspath(script_path)) if script_path else None
    s = sizing.size(sub, program, runs, needs=needs, partitions=parts, access=access, outcome=outcome,
                    script=abs_script, folder=folder if not db else None,
                    args=list(line.args) if line is not None else (list(args) if args is not None else None))
    return Plan(sizing=s, script=abs_script if job else script_path, folder=folder, job=job,
                outcome=outcome, line=line, script_sub=script_sub, needs=needs, notes=notes,
                args_unsure=unsure, script_unsure=script_unsure, submit_dir=folder if job else here_now,
                whole_array=whole_array)


# ── showing it ──────────────────────────────────────────────────────────────

LABELS = {"cpus": "CPUs", "mem": "memory", "time": "time", "gpus": "GPUs", "partition": "partition"}


def _row(label, asked, value, why, width=(10, 13, 13)):
    return f"  {label:<{width[0]}} {asked or '-':<{width[1]}} {value or '-':<{width[2]}} {why}"


def show(pl: Plan, hints: bool = True) -> None:
    s = pl.sizing
    prog = s.program
    head = pl.script or (f"job {pl.job}" if pl.job else "this job")
    what = []
    if prog.family:
        what.append(f"{prog.family} ({prog.executable})")
    elif prog.executable:
        what.append(f"runs {prog.executable}")
    if prog.input:
        what.append(f"input {prog.input}")
    click.echo(f"{head}" + (": " + ", ".join(what) if what else ""))
    if pl.job and pl.outcome is not None:
        click.echo(f"  job {pl.job} ended {pl.outcome.state.lower().replace('_', ' ')}")
    if s.matched:
        click.echo(f"  compared with your {len(s.similar)} run{'' if len(s.similar) == 1 else 's'} "
                   f"of {s.matched}")
    click.echo("")
    click.echo(_row("", "asked", "nomad", "why"))
    for name in sizing.ORDER[1:] + ("partition",):
        c = s.choices[name]
        mark = "*" if c.changed else " "
        click.echo(_row(LABELS[name], c.asked_text(), (c.value_text() or "-") + mark, c.reason))
    click.echo("  (* nomad would change it)")
    for n in pl.notes + s.notes:
        click.echo(f"\n  {n}")
    for u in s.sub.unsure:
        click.echo(f"\n  ! {u}")
    for x in s.problems:
        click.echo(click.style(f"\n  ! {x}", fg="yellow"))
    if not hints:
        return
    if s.lines():
        click.echo("\n  To take nomad's values, put these in the script (or run nomad submit):")
        for line in s.lines():
            click.echo(f"    {line}")
    else:
        click.echo("\n  Nothing to change.")


@click.command("size")
@click.argument("script", required=False, type=click.Path(dir_okay=False))
@click.option("--job", help="A job of yours that ran (its number): size it to run again.")
@click.option("--days", default=180, show_default=True, type=click.IntRange(1, 730),
              help="How far back to look at your past jobs.")
@click.option("--db", type=click.Path(exists=True, dir_okay=False),
              help="nomad's database instead of Slurm's accounting (on the hub; with --user).")
@click.option("--user", help="Whose jobs (only with --db, for a script: on a cluster it is always you).")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def size_cmd(script, job, days, db, user, as_json):
    """What a job should ask for: CPUs, memory, time, GPUs and partition,
    each with where it comes from and why.

    \b
      nomad size job.sh          a script before it runs
      nomad size --job 4815162   a job of yours, to run it again

    nomad reads the script (and the input file or command line of programs
    that state sizes there: Gaussian, ORCA, Q-Chem, Columbus, GROMACS,
    NAMD), your own past jobs from Slurm's accounting, and the cluster's
    partitions. Nothing is submitted or stored."""
    if not script and not job:
        raise click.UsageError("give a job script, or --job NUMBER")
    if user and not db:
        raise click.UsageError("--user goes with --db: on a cluster, nomad sizes your own jobs")
    if job and user and user != me():
        raise click.UsageError("--job looks at your own jobs only")
    pl = plan(script, job=job, user=user, days=days, db=db)
    if as_json:
        click.echo(json.dumps(pl.to_dict(), default=str, indent=2))
        return
    show(pl)


# ── submitting ──────────────────────────────────────────────────────────────

# Where each value came from, in the tag (one letter each).
CODES = {"asked": "s", "input": "i", "outcome": "o", "history": "h", "program": "g",
         "partition": "p", "default": "d", "user": "u"}
TAG_FIELDS = (("cpus", "cpus"), ("mem", "mem"), ("time", "time"), ("gpus", "gpus"),
              ("partition", "part"))
_PARTITION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
EXTRA_SINCE = (23, 2)          # Slurm's --extra (and AccountingStoreFlags=job_extra)
# A job's own command-line options that aren't sent again: nomad sets the
# folder itself; a dependency was on jobs long gone (singleton, on a job
# name, is kept); --wait would hold nomad; an array's other tasks aren't the
# one run again (handled apart); --begin and --deadline were times for the
# first run.
NOT_AGAIN = {"chdir", "dependency", "wait", "parsable", "test-only", "wrap", "array", "begin", "deadline"}
NOMAD_CODES = set(CODES.values()) - {"d"}


def old_tag(line: spec.SubmitLine | None) -> dict | None:
    """nomad's own tag on a job it sent ('nomad=1.18.0;via=cli;mem=h;...'),
    as {field: code}; None when the job has none."""
    v = line.get("extra") if line is not None else None
    if not v or not str(v).startswith("nomad="):
        return None
    return dict(part.partition("=")[::2] for part in str(v).split(";") if "=" in part)


def tag(sizing_: sizing.Sizing, again: str | None = None, via: str = "cli",
        before: dict | None = None) -> str:
    """'nomad=1.18.0;via=cli;cpus=s;mem=h;time=h;gpus=d;part=s': who chose
    each value (s: the script's own, u: the person on nomad's command line,
    h: nomad from their past runs, i: from the input file or command line,
    o: from how the job run again ended, g: what the program needs, p: a
    partition that fits, d: left to Slurm's defaults). ``before``: the tag
    of the job sent again, when nomad sent it: a value it kept from that
    job's own command line keeps the code it had there."""
    from nomad import __version__
    bits = [f"nomad={__version__}", f"via={via}"]
    for name, short in TAG_FIELDS:
        c = sizing_.choices.get(name)
        if c is not None and (c.changed or c.source == "user"):
            code = CODES.get(c.source, "d")
        elif c is not None and c.asked is not None:
            old = (before or {}).get(short)
            code = old if old in NOMAD_CODES else "s"
        else:
            code = "d"
        bits.append(f"{short}={code}")
    if again:
        bits.append(f"again={again}")
    return ";".join(bits)


def _yours(s: sizing.Sizing, cpus, mem, time_, gpus, partition, keep: bool) -> None:
    """The person's own values (and --keep: none of nomad's), into the
    sizing; then memory written for the CPUs and GPUs sent, nomad's time
    and memory capped at what the partition gives, and the whole checked."""
    req = s.request
    if keep:
        for c in s.choices.values():
            if c.source != "user":
                c.opts, c.value, c.reason = [], c.asked, "kept (--keep)"
                c.source = "asked" if c.asked is not None else "default"
                c.each_value = c.each_asked
    if cpus is not None:
        try:
            opts, repl = forms.cpus(req, cpus, "asked")
        except ValueError as e:
            raise click.BadParameter(str(e), param_hint="--cpus")
        s.choices["cpus"] = sizing.Choice("cpus", req.cpus, cpus, "user", "your choice", 0, opts, repl)
    if gpus is not None:
        if gpus == 0 and not req.gpus:
            s.choices["gpus"] = sizing.Choice("gpus", None, None, "user", "your choice: none")
        else:
            try:
                opts, repl = forms.gpus(req, s.sub, gpus)
            except ValueError as e:
                raise click.BadParameter(str(e), param_hint="--gpus")
            s.choices["gpus"] = sizing.Choice("gpus", req.gpus, gpus, "user", "your choice", 0, opts, repl)
    if not keep and (cpus is not None or gpus is not None):
        sizing.route_mem(s)            # memory the way the job will now be sent
    if mem is not None:
        mb = spec.parse_memory_mb(mem)
        if mb is None:
            raise click.BadParameter("a size like 8G, 512M or 1.5T (0: all of a node's)", param_hint="--mem")
        r = s.request_without("mem")
        if r.spread and r.mem_form in sizing.PER:
            raise click.BadParameter("the job asks memory per CPU (or GPU) for tasks Slurm places over its "
                                     "nodes, and --mem is per node: change it in the script", param_hint="--mem")
        s.choices["mem"] = sizing.Choice("mem", req.mem_per_node(s.node_cpus), mb, "user", "your choice",
                                         0, [("mem", str(mb))], forms.MEM_OPTIONS)
    sizing.settle_mem(s)
    if time_ is not None:
        t = spec.parse_time_seconds(time_)
        if t is None or t < 60:
            raise click.BadParameter("a time like 4:00:00, 1-12:00:00 or 90 (minutes)", param_hint="--time")
        opts, repl = forms.time(t)
        s.choices["time"] = sizing.Choice("time", req.time_s, t, "user", "your choice", 0, opts, repl)
    if partition is not None:
        if not _PARTITION.fullmatch(partition):
            raise click.BadParameter("not a partition name", param_hint="--partition")
        opts, repl = forms.partition(partition)
        s.choices["partition"] = sizing.Choice("partition", req.partition, partition, "user",
                                               "your choice", 0, opts, repl)
    elif not keep and any(x is not None for x in (cpus, gpus, mem, time_)):
        s.choices["partition"] = sizing._partition(s)       # where your values fit
    if not keep:
        sizing.reweigh(s)          # what the job doesn't ask, against the defaults where it goes now
    if sizing.cap(s) and partition is None and not keep:
        s.choices["partition"] = sizing._partition(s)
    s.problems = sizing.check(s)


def command(pl: Plan, *, args: list, tagged: bool) -> dict:
    """The sbatch command: {"argv", "display", "notes"}. Only what changes
    is set on the command line (sbatch reads the rest from the script); a
    job sent again gets its own command line's other options first, and its
    folder."""
    s = pl.sizing
    notes = []
    argv = ["sbatch", "--parsable"]
    if pl.line is not None:
        drop = s.replaces() | NOT_AGAIN
        for name, value in pl.line.options:
            if name == "dependency" and value == "singleton":
                pass                                # on the job's name: still meant
            elif name in drop:
                if name == "dependency":
                    notes.append("Its --dependency (on jobs long gone) isn't sent again.")
                elif name in ("begin", "deadline"):
                    notes.append(f"Its --{name} (a time for its first run) isn't sent again.")
                continue
            if name == "extra" and str(value or "").startswith("nomad="):
                continue                            # nomad's tag on it: a new one is written
            argv.append(f"--{name}={value}" if value is not None else f"--{name}")
        if pl.outcome is not None and pl.outcome.array_task is not None and not pl.whole_array:
            argv.append(f"--array={pl.outcome.array_task}")
            notes.append(f"Only task {pl.outcome.array_task} of array {pl.outcome.array_job} runs again.")
        elif pl.line.get("array"):
            argv.append(f"--array={pl.line.get('array')}")
            if pl.whole_array:
                notes.append(f"All of array {pl.job} runs again (--array={pl.line.get('array')}).")
        elif pl.whole_array:
            notes.append(f"All of array {pl.job} runs again (the script's own --array).")
        argv.append(f"--chdir={pl.folder}")
    argv += s.options()
    if tagged:
        argv.append(f"--extra={tag(s, pl.job, before=old_tag(pl.line))}")
    argv.append(pl.script)
    argv += list(args)
    return {"argv": argv, "display": shlex.join(argv), "notes": notes}


def extra_ok(pl: Plan) -> tuple[bool, str | None]:
    """Whether nomad may tag the job in --extra here, and why not."""
    own = pl.line is not None and pl.line.has("extra") and old_tag(pl.line) is None
    if pl.script_sub.has("extra") or own:
        return False, "The job sets Slurm's --extra itself, so nomad doesn't tag it."
    v = cluster.slurm_version()
    if v is None or v < EXTRA_SINCE:
        return False, ("This Slurm has no --extra (it came in 23.02), so nomad doesn't tag the job."
                       if v else "nomad couldn't tell this Slurm's version, so it doesn't tag the job.")
    constraints = cluster.extra_is_constraints()
    if constraints is None:
        return False, "nomad couldn't read Slurm's settings here (scontrol show config), so it doesn't tag the job."
    if constraints:
        return False, ("Slurm here reads --extra as node constraints (extra_constraints), so nomad "
                       "doesn't tag the job.")
    return True, None


def _run_sbatch(argv: list, cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=300, cwd=cwd,
                          stdin=subprocess.DEVNULL)


_OURS = {"--again", "--cpus", "--mem", "--time", "--gpus", "--partition", "--keep", "--yes", "-y",
         "--dry-run", "--days"}


@click.command("submit", context_settings={"allow_interspersed_args": False})
@click.argument("script", required=False, type=click.Path(dir_okay=False))
@click.argument("args", nargs=-1)
@click.option("--again", "job", metavar="JOB", help="A job of yours that ran: send it again.")
@click.option("--cpus", type=click.IntRange(1, 4096),
              help="CPUs, the way the script asks them (CPUs per task, or tasks): your choice.")
@click.option("--mem", help="Memory per node, e.g. 12G (your choice).")
@click.option("--time", "time_", help="Time limit, e.g. 4:00:00 (your choice).")
@click.option("--gpus", type=click.IntRange(0, 64), help="GPUs (your choice).")
@click.option("--partition", help="Partition (your choice).")
@click.option("--keep", is_flag=True, help="Keep the script's own values: nomad only checks and tags.")
@click.option("--yes", "-y", is_flag=True, help="Don't ask before submitting.")
@click.option("--dry-run", is_flag=True, help="Show the sbatch command; don't run it.")
@click.option("--days", default=180, show_default=True, type=click.IntRange(1, 730),
              help="How far back to look at your past jobs.")
def submit_cmd(script, args, job, cpus, mem, time_, gpus, partition, keep, yes, dry_run, days):
    """Size a job and submit it: nomad's values, or yours.

    \b
      nomad submit job.sh [ARGS...]      a script (nomad's options before it, as sbatch's)
      nomad submit --again 4815162       a job of yours that ran, sent again
      nomad submit --mem 12G job.sh      your own value for memory

    Shows what nomad suggests and why (as `nomad size`), and the sbatch
    command it will run; asks before running it. The job is tagged with
    who chose each value (Slurm's "extra" field, Slurm 23.02 and later)."""
    if not script and not job:
        raise click.UsageError("give a job script, or --again JOB")
    if job and args:
        raise click.UsageError("--again sends the job with its own arguments; give no others")
    stray = [a for a in args if a.split("=")[0] in _OURS]
    pl = plan(script, job=job, days=days, args=list(args))
    if not pl.script:
        raise click.ClickException("nomad doesn't know this job's script, so it can't send it again "
                                   "(submit the script itself: nomad submit SCRIPT)")
    script_path = os.path.abspath(pl.script)
    if read_text(script_path, spec.MAX_SCRIPT) is None:
        raise click.ClickException(f"{script_path} can't be read: nothing to submit")
    pl.script = script_path
    _yours(pl.sizing, cpus, mem, time_, gpus, partition, keep)
    show(pl, hints=False)
    if job:
        args = list(pl.line.args) if pl.line is not None else []
        try:
            if pl.outcome is not None and pl.outcome.submitted and os.stat(script_path).st_mtime > \
                    __import__("datetime").datetime.fromisoformat(str(pl.outcome.submitted)).timestamp():
                click.echo(click.style(f"\n  ! {script_path} changed since job {job} ran: the new one runs.",
                                       fg="yellow"))
        except (OSError, ValueError):
            pass
    ok, why = extra_ok(pl)
    cmd = command(pl, args=list(args), tagged=ok)
    for n in cmd["notes"] + ([why] if why else []):
        click.echo(f"\n  {n}")
    if stray:
        click.echo(click.style(f"\n  ! {', '.join(stray)} came after the script, so they go to the "
                               "script, not to nomad: put nomad's options before the script.", fg="yellow"))
    if pl.args_unsure:
        click.echo(click.style("\n  ! Slurm keeps the command line without its quotes: check the "
                               "arguments below.", fg="yellow"))
    if pl.script_unsure:
        click.echo(click.style("\n  ! nomad isn't sure the script below is the one that ran: check it.",
                               fg="yellow"))
    click.echo(f"\n  sbatch command:\n    {cmd['display']}")
    if pl.sizing.problems:
        click.echo(click.style("\n  Slurm would likely refuse it as it stands (see above).", fg="yellow"))
    if dry_run:
        click.echo("\n  Not submitted (--dry-run).")
        return
    if yes and (stray or pl.args_unsure or pl.script_unsure):
        raise click.ClickException("not submitting without asking: check the command above, then run "
                                   "again without --yes")
    if not yes:
        if not sys.stdin.isatty():
            raise click.ClickException("not asking without a terminal: run with --yes to submit")
        if not click.confirm("\n  Submit it?", default=True):
            click.echo("  Not submitted.")
            return
    try:
        r = _run_sbatch(cmd["argv"], pl.submit_dir or pl.folder)
        if r.returncode != 0 and ok and "--extra" in (r.stderr or "") and (
                "unrecognized" in (r.stderr or "") or "invalid" in (r.stderr or "").lower()):
            # A Slurm that has no --extra after all: once more, untagged.
            click.echo("\n  sbatch doesn't take --extra here: sending it untagged.")
            r = _run_sbatch([a for a in cmd["argv"] if not a.startswith("--extra=")],
                            pl.submit_dir or pl.folder)
    except subprocess.TimeoutExpired:
        raise click.ClickException("sbatch didn't answer in 5 minutes: check squeue before sending it "
                                   "again (it may have been submitted)")
    except OSError as e:
        raise click.ClickException(f"sbatch could not run here: {e}")
    if r.returncode != 0:
        raise click.ClickException("sbatch refused it: " + ((r.stderr or r.stdout or "").strip()[-500:]
                                                            or f"exit {r.returncode}"))
    jid = (r.stdout or "").strip().split(";")[0]
    chosen = [n for n, c in pl.sizing.choices.items() if c.changed and c.source != "user"]
    mine = [n for n, c in pl.sizing.choices.items() if c.source == "user"]
    click.echo(f"\n  Submitted job {jid}."
               + (f" nomad set: {', '.join(LABELS[n] for n in chosen)}." if chosen else "")
               + (f" You set: {', '.join(LABELS[n] for n in mine)}." if mine else ""))

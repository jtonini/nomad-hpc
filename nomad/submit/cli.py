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
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field

import click

from nomad.submit import apps, cluster, forms, history, logs as logs_, programs, sizing, spec, stage as stage_


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
    own: dict = {}                                  # nomad's copy that ran it -> the person's script
    noted: dict = {}                                # a job's number -> nomad's record it came from
    here = []                                       # this cluster's name, once asked
    for r in runs:
        sub = spec.parse_submit_line(r.submit_line) if r.submit_line else None
        if sub is None or not sub.readable or sub.wrap is not None:
            continue
        if sub.script in (None, stage_.STDIN):
            # On standard input: nomad's own sending (1.25.2), by its note.
            key = r.array_job or r.job_id
            if key not in noted:
                if not here:
                    here.append(cluster.cluster_name())
                noted[key] = stage_.record_of_job(key, cluster=here[0], folder=r.workdir,
                                                  submitted=getattr(r, "submitted", None))
            r.script = noted[key]
            if not r.script:
                continue
        else:
            key = (sub.script, r.workdir, sub.get("chdir") is not None)
            if key not in found:
                if len(found) >= limit:
                    r.script = None                 # not looked for: not compared as "this script"
                    continue
                path, guessed = locate(*key)
                found[key] = None if guessed else path
            r.script = found[key]
            if not r.script:
                continue
        if r.script not in seen:
            text = read_text(r.script, spec.MAX_SCRIPT)
            inner = stage_.sent(r.script, text)
            if inner is not None:                   # nomad's own copy: the person's script it carried
                own[r.script], text = inner[0], inner[1]
            seen[r.script] = apps.program_of(spec.sbatch_lines(text)[1]) if text else None
        prog = seen[r.script]
        r.script = own.get(r.script, r.script)
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
    text: str | None = None             # the script as it was read: sized, staged and sent from this
    shown: bytes | None = None          # from the Console: the bytes the person was shown (sha256 checked)

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
    if line.script == stage_.STDIN:
        return None, False, None                    # it came on standard input: its words are its arguments
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
         run_sacct=history._run, run_slurm=cluster._run, pin: str | None = None) -> Plan:
    """Size a script before it runs, or a job of the person's to run again.
    ``pin`` (from the Console): the sha256 of the script as the person was
    shown it; the script is read once, checked against it, and that text is
    what is sized and sent (1.25.1)."""
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
            record = None
            if line.readable and line.wrap is None and line.script in (None, stage_.STDIN):
                record = stage_.record_of_job(outcome.array_job or outcome.job_id,   # nomad sent it (1.25.2)
                                              cluster=cluster.cluster_name(run=run_slurm),
                                              folder=outcome.workdir, submitted=outcome.submitted)
            if record:
                script_path, guessed, at = record, False, None
            else:
                script_path, guessed, at = _rerun_script(line, outcome.workdir)
            script_unsure = guessed
            inner = stage_.sent(script_path, read_text(script_path, spec.MAX_SCRIPT)) if script_path else None
            gone = False
            if inner is not None:
                how = "ran through local scratch" if inner[2] == "staged" else "was sent from the Console"
                if os.path.isfile(inner[0]):
                    notes.append(f"Job {job} {how}; its own script is {inner[0]}.")
                    script_path = inner[0]
                else:
                    notes.append(f"Job {job} {how}, and its own script ({inner[0]}) isn't "
                                 "there now: sized from the job's own settings.")
                    script_path, gone = None, True
            if at:
                line.script, line.args = line.rest[at], list(line.rest[at + 1:])
            if gone:
                pass
            elif script_path is None:
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
    shown = None
    if pin is not None:
        if not script_path:
            raise click.ClickException("nomad doesn't know this job's script, so it can't be sent from the Console")
        shown = _pinned(os.path.abspath(script_path), pin)
        text = shown.decode("utf-8", errors="replace")
    else:
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
                whole_array=whole_array, text=text, shown=shown)


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
@click.option("--via", type=click.Choice(["cli", "console"]), default="cli", hidden=True,
              help="Where it was asked from (console: nomad-gate, for the Console).")
def size_cmd(script, job, days, db, user, as_json, via):
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
    _programs(via, "Not run.")
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
NOT_AGAIN = {"chdir", "dependency", "wait", "parsable", "test-only", "wrap", "array", "begin", "deadline",
             "quiet"}
NOMAD_CODES = set(CODES.values()) - {"d"}


def old_tag(line: spec.SubmitLine | None) -> dict | None:
    """nomad's own tag on a job it sent ('nomad=1.18.0;via=cli;mem=h;...'),
    as {field: code}; None when the job has none."""
    v = line.get("extra") if line is not None else None
    if not v or not str(v).startswith("nomad="):
        return None
    return dict(part.partition("=")[::2] for part in str(v).split(";") if "=" in part)


def tag(sizing_: sizing.Sizing, again: str | None = None, via: str = "cli",
        before: dict | None = None, staged: bool = False) -> str:
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
    if staged:
        bits.append("stage=l")                      # through the node's local scratch
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


def command(pl: Plan, *, args: list, tagged: bool, delay: bool = False, script: str | None = None,
            job_name: str | None = None, via: str = "cli", staged: bool | None = None,
            on_stdin: bool = False) -> dict:
    """The sbatch command: {"argv", "display", "notes"}. Only what changes
    is set on the command line (sbatch reads the rest from the script); a
    job sent again gets its own command line's other options first, and its
    folder. ``delay``: sent to start in logs.DELAY s, for nomad to set
    where its output goes first. ``script``: what sbatch is given instead
    of the person's script (nomad's copy that runs it through local
    scratch), named ``job_name`` as sbatch would have named theirs. ``via``:
    where the person sent it from, in the tag (cli, or console: nomad-gate).
    ``staged``: whether ``script`` is the local-scratch wrapper (default: when
    there is one) or nomad's plain copy of the script (from the Console).
    ``on_stdin`` (1.25.2): sbatch reads nomad's text on its standard input
    (/dev/stdin when the job has arguments to give it); ``script`` is then
    only nomad's record of that text, for the command shown."""
    staged = (script is not None) if staged is None else staged
    s = pl.sizing
    notes = []
    argv = ["sbatch", "--parsable"] + ([f"--begin=now+{logs_.DELAY}"] if delay else [])
    if pl.line is not None:
        drop = s.replaces() | NOT_AGAIN
        for name, value in pl.line.options:
            if name == "dependency" and value == "singleton":
                pass                                # on the job's name: still meant
            elif name in drop:
                nomads = name == "begin" and value == f"now+{logs_.DELAY}" and \
                    [n for n, _ in pl.line.options[:2]] == ["parsable", "begin"]
                if name == "dependency":
                    notes.append("Its --dependency (on jobs long gone) isn't sent again.")
                elif name in ("begin", "deadline") and not nomads:
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
    if job_name:
        argv.append(f"--job-name={job_name}")
    if tagged:
        argv.append(f"--extra={tag(s, pl.job, via=via, before=old_tag(pl.line), staged=staged)}")
    if not on_stdin:
        argv.append(script or pl.script)
    elif args:
        argv.append(stage_.STDIN)                    # sbatch reads it from there; its arguments follow
    argv += list(args)
    # Shown without nomad's --begin: the command to run by hand.
    shown = [a for i, a in enumerate(argv) if not (delay and i == 2)]
    display = shlex.join(shown) + (f" < {shlex.quote(script)}" if on_stdin and script else "")
    return {"argv": argv, "display": display, "notes": notes}


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


def _run_sbatch(argv: list, cwd: str, data: bytes | None = None) -> subprocess.CompletedProcess:
    """sbatch, from ``cwd``. ``data`` (1.25.2): the script itself, which
    sbatch reads on its standard input -- nomad's own text, never a file
    someone could change before sbatch reads it."""
    argv = programs.argv(argv)                    # from the Console: by its full path
    if data is None:
        return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=300, cwd=cwd, stdin=subprocess.DEVNULL)
    r = subprocess.run(argv, capture_output=True, timeout=300, cwd=cwd, input=data)
    return subprocess.CompletedProcess(argv, r.returncode, r.stdout.decode("utf-8", errors="replace"),
                                       r.stderr.decode("utf-8", errors="replace"))


_OURS = {"--again", "--cpus", "--mem", "--time", "--gpus", "--partition", "--keep", "--yes", "-y",
         "--dry-run", "--days", "--logs", "--localscratch", "--no-localscratch", "--via", "--script-sha256"}


def logs_for(pl: Plan, flag: str | None, user: str, env=None) -> tuple:
    """Where nomad puts the job's output (logs.Logs), and a warning when
    $NOMAD_LOGS can't be used."""
    env = os.environ if env is None else env
    line = pl.line if pl.line is not None else spec.SubmitLine()
    own = None
    for where, sub in (("#SBATCH", pl.script_sub), ("its command line's", line)):
        name = sub.last_of("output", "error")
        if name:
            own = f"{where} --{name}"
    for var in ("SBATCH_OUTPUT", "SBATCH_ERROR"):
        if var in env:                               # set, even empty, sbatch takes it
            own = own or f"${var}"
    text = pl.text if pl.text is not None else (read_text(pl.script, spec.MAX_SCRIPT) or "") if pl.script else ""
    ignore_pbs = "SBATCH_IGNORE_PBS" in env or pl.script_sub.has("ignore-pbs") or line.has("ignore-pbs")
    pbs = set() if ignore_pbs else _pbs_options(text)
    if not own and pbs & {"o", "e"}:
        own = "#PBS -o" if "o" in pbs else "#PBS -e"
    cannot = None
    if pl.script_sub.has("wait") or "SBATCH_WAIT" in env:
        cannot = "sbatch waits for the job (--wait), so nomad can't set that before it starts"
    elif pl.script_sub.has("test-only"):
        cannot = "--test-only: no job is made"
    elif pl.script_sub.has("begin") or "a" in pbs:
        cannot = "the job sets its own start time (--begin)"
    elif pl.script_sub.has("deadline"):
        cannot = "the job has a --deadline, which a later start could miss"
    elif any(o.split()[:1] in (["hetjob"], ["packjob"]) for o in spec.sbatch_lines(text)[0]):
        cannot = "a heterogeneous job"
    here = os.getcwd()
    try:
        return logs_.choose(flag=flag, env=env, user=user, here_dir=here, own_output=own,
                            cannot=cannot), None
    except logs_.LogsError as e:
        if flag is not None:
            raise click.BadParameter(str(e), param_hint="--logs")
        warn = f"$NOMAD_LOGS can't be used ({e})."
        return logs_.Logs(None, "env", "its output goes where Slurm puts it", kind="here"), warn


def _pbs_options(text: str) -> set:
    """The letters of the options a script's #PBS lines give, which sbatch
    reads too (unless --ignore-pbs; anywhere in the script): o, e, a, h."""
    got = set()
    for raw in text.splitlines():
        if raw.startswith("#PBS"):
            for m in re.finditer(r"(?:^|\s)-([oeah])", raw[4:]):
                got.add(m.group(1))
    return got


def _name_for(pl: Plan, env=None) -> str | None:
    """The job's name as sbatch will give it (before it has one to read
    back): its command line's -J, else $SBATCH_JOB_NAME, else the script's."""
    env = os.environ if env is None else env
    if pl.line is not None and pl.line.get("job-name"):
        return pl.line.get("job-name")
    return env.get("SBATCH_JOB_NAME") or spec.request_of(pl.sizing.sub).job_name


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
@click.option("--logs", metavar="here|FOLDER",
              help="Where the job's Slurm output goes: a folder of its own in FOLDER, or `here` "
                   "(Slurm's own place). Default: $NOMAD_LOGS, else /scratch/USER/jobs/JOBID-NAME.")
@click.option("--localscratch/--no-localscratch", "localscratch", default=None,
              help="Run it through its node's local scratch: the job's folder copied there, what is new "
                   "or changed copied back to the job's own folder. Default: on for a job on one node "
                   "($NOMAD_LOCALSCRATCH=off turns it off).")
@click.option("--via", type=click.Choice(["cli", "console"]), default="cli", hidden=True,
              help="Where it was sent from, for the tag (console: nomad-gate, for the Console).")
@click.option("--script-sha256", "pin", default=None, hidden=True,
              help="With --via console: the sha256 of the script as the person was shown it; nomad gives "
                   "sbatch only that text, itself.")
@click.option("--starts", "starts", default=None, metavar="P1,P2,...", hidden=True,
              help="Submit nothing: when Slurm would start this job (as nomad would send it) in each of these "
                   "partitions (sbatch --test-only), and their nodes' states; JSON (1.25.4, for the Console).")
def submit_cmd(script, args, job, cpus, mem, time_, gpus, partition, keep, yes, dry_run, days, logs,
               localscratch, via, pin, starts):
    """Size a job and submit it: nomad's values, or yours.

    \b
      nomad submit job.sh [ARGS...]      a script (nomad's options before it, as sbatch's)
      nomad submit --again 4815162       a job of yours that ran, sent again
      nomad submit --mem 12G job.sh      your own value for memory

    Shows what nomad suggests and why (as `nomad size`), and the sbatch
    command it will run; asks before running it. The job is tagged with
    who chose each value (Slurm's "extra" field, Slurm 23.02 and later).
    Its Slurm output goes in a folder of its own, /scratch/USER/jobs/JOBID-NAME
    (--logs, $NOMAD_LOGS), unless the job says where. A job on one node runs
    through its node's local scratch (/localscratch/USER/job_JOBID): its
    folder copied there, what it makes or changes copied back to its own
    folder and checked (--no-localscratch: as it is)."""
    if not script and not job:
        raise click.UsageError("give a job script, or --again JOB")
    if via == "console" and not (pin and re.fullmatch(r"[0-9a-f]{64}", pin)):
        raise click.UsageError("--via console needs --script-sha256: the script as the person was shown it")
    if pin and via != "console":
        raise click.UsageError("--script-sha256 goes with --via console")
    if job and args:
        raise click.UsageError("--again sends the job with its own arguments; give no others")
    _programs(via, "Not sent.")
    if via == "console" and programs.where("sbatch") is None:
        raise click.ClickException("there's no sbatch on your PATH here: not sent")
    stray = [a for a in args if a.split("=")[0] in _OURS]
    pl = plan(script, job=job, days=days, args=list(args), pin=pin)
    if not pl.script:
        raise click.ClickException("nomad doesn't know this job's script, so it can't send it again "
                                   "(submit the script itself: nomad submit SCRIPT)")
    script_path = os.path.abspath(pl.script)
    if pl.text is None and read_text(script_path, spec.MAX_SCRIPT) is None:
        raise click.ClickException(f"{script_path} can't be read: nothing to submit")
    pl.script = script_path
    shown = pl.shown                                     # from the Console: the bytes the person saw, checked
    _yours(pl.sizing, cpus, mem, time_, gpus, partition, keep)
    if starts is not None:
        sent = list(pl.line.args) if job and pl.line is not None else list(args)   # --again: its own arguments
        click.echo(json.dumps(start_times(pl, sent, starts, via=via, shown=shown), default=str))
        return
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
    user = me()
    out, warn = logs_for(pl, logs, user)
    text = pl.text
    if shown is not None:
        _tidy_store()                                    # nomad's records: the person's alone
    plain = shown is None or _utf8(shown)
    st, st_note, wrapper, wpath, job_name = _staging(pl, localscratch if plain else False, user, out, text=text)
    if not plain and localscratch is not False:
        st_note = "Not through local scratch: the script isn't plain UTF-8 text, so it is sent exactly as it is."
    carried = None
    data = None                  # what sbatch reads on its standard input: nomad's own text (1.25.2)
    if st is not None:
        data = wrapper.encode("utf-8")
        cmd = command(pl, args=list(args), tagged=ok, delay=out.delay, script=wpath, job_name=job_name, via=via,
                      on_stdin=True)
    elif shown is not None:
        # From the Console: sbatch is given the very text the person saw,
        # never a path (which someone could change meanwhile).
        carried = stage_.carry_path(pl.script, shown)
        data = shown
        job_name = _job_name(pl, text)
        cmd = command(pl, args=list(args), tagged=ok, delay=out.delay, script=carried, job_name=job_name,
                      via=via, staged=False, on_stdin=True)
    else:
        cmd = command(pl, args=list(args), tagged=ok, delay=out.delay, via=via)
    for n in cmd["notes"] + ([why] if why else []):
        click.echo(f"\n  {n}")
    if warn:
        click.echo(click.style(f"\n  ! {warn}", fg="yellow"))
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
    if st is not None:
        click.echo(f"\n  {st.describe(user)}\n  ({wpath} is nomad's copy of {pl.script} that does it: sbatch "
                   "is given its text.)")
    elif st_note:
        click.echo(f"\n  {st_note}")
    if carried:
        click.echo(f"\n  ({carried} is nomad's copy of {pl.script} as you saw it: sbatch is given its text.)")
    if out.pattern:
        click.echo(f"\n  Its output: {logs_.shown(out.pattern, user=user, name=_name_for(pl))}/"
                   f" (nomad sends it to start in {logs_.DELAY // 60} minutes, sets that, and lets it "
                   "start at once)")
    elif not (out.kind == "own" and out.source == "default"):
        click.echo(f"\n  {out.why[0].upper() + out.why[1:]}.")
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
    where = pl.submit_dir or pl.folder
    said, failed, placed = [], None, None
    record = None                # nomad's record of what it sends: for --again, and the job's own name
    if st is not None:
        try:
            record = stage_.keep(wpath, wrapper)
        except OSError as e:
            said.append(f"nomad couldn't keep its record of what it sent ({e.strerror or e}: {wpath}): sending it "
                        "again won't know it ran through local scratch.")
    if carried:
        try:
            record = stage_.carry(carried, shown, pl.script)
        except OSError as e:
            said.append(f"nomad couldn't keep its record of what it sent ({e.strerror or e}: {carried}): sending "
                        "it again will go by Slurm's record of it alone.")
    argv = cmd["argv"]

    def send(argv_):
        return _run_sbatch(argv_, where) if data is None else _run_sbatch(argv_, where, data)
    try:
        r = send(argv)
        if r.returncode != 0 and ok and "--extra" in (r.stderr or "") and (
                "unrecognized" in (r.stderr or "") or "invalid" in (r.stderr or "").lower()):
            # A Slurm that has no --extra after all: once more, untagged.
            said.append("sbatch doesn't take --extra here: it was sent untagged.")
            r = send([a for a in argv if not a.startswith("--extra=")])
    except subprocess.TimeoutExpired:
        r, failed = None, "sbatch didn't answer in 5 minutes"
    except OSError as e:
        r, failed = None, f"sbatch could not run here: {e}"
    jid, on = logs_.job_number(r.stdout) if r is not None else (None, None)
    if jid and record:
        try:
            stage_.note_job(jid.split("_")[0], record, cluster=cluster.cluster_name() or on, folder=pl.folder)
        except OSError as e:
            said.append(f"nomad couldn't note which of its records job {jid} came from ({e.strerror or e}).")
    if jid and out.pattern:
        placed = logs_.place(out, jid, on, user=user)
    _said_after(pl, out, r, jid, placed, failed, said, staged=st is not None)


# ── when it would start (1.25.4) ─────────────────────────────────────────────
# sbatch --test-only: Slurm's own plan for the job, given the queue and its
# backfill, in each partition; nothing is submitted. "sbatch: Job 86313 to
# start at 2026-11-02T13:55:12 using 1 processors on nodes spdr01 in
# partition basic". Where no job has a time limit Slurm can't tell when a
# node frees: then it is only "now" or "later".
# Slurm says more between the time and "using" in some versions (spydur's 25.11: "...T09:49:39 a using 1
# processors ..."): whatever is there, on the same line.
_TEST_ONLY = re.compile(r"to start at (\S+)[^\n]*? using (\d+) processors? on nodes? (\S+) in partition (\S+)")
MAX_STARTS = 12
STARTS_AT_ONCE = 4          # sbatch --test-only calls at a time (each asks slurmctld)
STARTS_TIMEOUT = 60         # one call: a busy slurmctld answers in seconds, or not at all


def _test_only(argv: list, cwd: str, data: bytes) -> subprocess.CompletedProcess:
    """sbatch --test-only: the script on standard input, the times in ISO
    form whatever the person's SLURM_TIME_FORMAT, at most STARTS_TIMEOUT."""
    env = {**os.environ, "SLURM_TIME_FORMAT": "standard"}
    r = subprocess.run(programs.argv(argv), capture_output=True, timeout=STARTS_TIMEOUT, cwd=cwd, input=data,
                       env=env)
    return subprocess.CompletedProcess(argv, r.returncode, r.stdout.decode("utf-8", errors="replace"),
                                       r.stderr.decode("utf-8", errors="replace"))


def start_times(pl: Plan, args: list, partitions: str, *, via: str = "cli", shown: bytes | None = None,
                run=None, now=None) -> dict:
    """{"ok", "now", "starts": [{partition, start, soon, nodes, error}], "nodes": {partition: {state: n}}}
    for the job as nomad would send it (the person's values in, nomad's
    tag and output folder aside), in each of ``partitions`` (comma list)."""
    from datetime import datetime, timedelta
    names = [p.strip() for p in str(partitions).split(",") if p.strip()]
    if not names or len(names) > MAX_STARTS or not all(_PARTITION.fullmatch(p) for p in names):
        raise click.ClickException(f"--starts: 1 to {MAX_STARTS} partition names, comma between")
    run = run or _test_only
    fixed = now                                          # tests give one; else each call's own
    now = now or datetime.now()
    text = shown if shown is not None else (pl.text.encode("utf-8") if pl.text is not None
                                             else (read_text(pl.script, spec.MAX_SCRIPT) or "").encode("utf-8"))
    cmd = command(pl, args=list(args), tagged=False, delay=False, script=pl.script, job_name=_job_name(pl, text.decode(
        "utf-8", errors="replace")), via=via, staged=False, on_stdin=True)
    base = [a for a in cmd["argv"] if a != "--parsable" and not a.startswith(("--partition=", "--extra="))]
    i = base.index("-p") if "-p" in base else None
    if i is not None:
        del base[i:i + 2]
    where = pl.submit_dir or pl.folder or "."

    def one(name: str) -> dict:
        argv = base[:1] + ["--test-only", f"--partition={name}"] + base[1:]
        row = {"partition": name, "start": None, "soon": False, "nodes": None, "error": None}
        try:
            asked_at = fixed or datetime.now()          # a call in the third round is asked minutes later
            r = run(argv, where, text)
            said = f"{r.stdout or ''}\n{r.stderr or ''}"
            m = _TEST_ONLY.search(said)
            if m and m.group(4) != name:
                # the site's job_submit sent it elsewhere: an estimate for another partition, not this one
                row["error"] = f"Slurm would run it in partition {m.group(4)[:64]}, not {name}"
            elif m:
                try:
                    at = datetime.fromisoformat(m.group(1))
                    row["start"] = at.isoformat(timespec="seconds")
                    row["soon"] = at <= asked_at + timedelta(minutes=2)
                except ValueError:
                    row["start"] = m.group(1)
                row["nodes"] = m.group(3)
            else:
                lines = [ln.strip() for ln in said.splitlines() if ln.strip()]
                row["error"] = (lines[-1] if lines else f"sbatch --test-only ended with {r.returncode}")[:300]
        except subprocess.TimeoutExpired:
            row["error"] = f"sbatch --test-only didn't answer in {STARTS_TIMEOUT} s"
        except OSError as e:
            row["error"] = f"sbatch could not run here: {e}"
        return row
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(STARTS_AT_ONCE, len(names))) as pool:
        out = list(pool.map(one, names))                 # in the order asked
    f = pl.sizing.final()
    per_node = f.mem_mb if f.mem_mb else (f.mem_per_cpu_mb * -(-(f.cpus or 1) // max(1, f.nodes or 1))
                                          if f.mem_per_cpu_mb else None)
    job = {"cpus": f.cpus, "mem_mb": per_node or None, "time_s": None if f.time_unlimited else f.time_s,
           "gpus": f.gpus or 0, "nodes": f.nodes or 1}
    return {"ok": True, "now": now.isoformat(timespec="seconds"), "starts": out, "nodes": _node_states(names),
            "job": job}


def _node_states(names: list) -> dict:
    """{partition: {state: nodes}} from sinfo (idle, mixed, allocated, ...)."""
    try:
        r = subprocess.run(programs.argv(["sinfo", "-h", "-p", ",".join(names), "-o", "%P|%T|%D"]),
                           capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    out: dict = {}
    for line in (r.stdout or "").splitlines() if r.returncode == 0 else []:
        bits = line.strip().split("|")
        if len(bits) == 3 and bits[2].isdigit():
            part = bits[0].rstrip("*")
            out.setdefault(part, {})
            state = bits[1].rstrip("*~#!%$@^-+").lower()
            out[part][state] = out[part].get(state, 0) + int(bits[2])
    return out


def _pinned(path: str, sha: str) -> bytes:
    """Sent from the Console (nomad-gate): the person's own script, only if
    it is still the very text they were shown (its sha256); a job sent
    again too. What is sent is these bytes (on sbatch's standard input, 1.25.2)."""
    from nomad.submit import gate
    try:
        return gate.pinned(path, sha)
    except gate.Refused as e:
        raise click.ClickException(str(e)) from None


def _utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _tidy_store() -> None:
    """From the Console: nomad's own folder for its records is the person's
    alone (nomad makes it so, as far as it can). What is sent never comes
    from it (1.25.2): sbatch reads nomad's text on its standard input; who
    else could change the folders above it is said when the script is
    shown (gate.account_open)."""
    store = os.path.expanduser(stage_.STORE)
    try:
        os.makedirs(store, mode=0o700, exist_ok=True)
        for d in (os.path.dirname(store), store):                # ~/.local/share/nomad and its staged/
            st_ = os.lstat(d)
            if stat.S_ISDIR(st_.st_mode) and st_.st_uid == os.getuid() and st_.st_mode & 0o077:
                os.chmod(d, 0o700)
    except OSError:
        pass                                                     # its records then can't be kept: said after


def _programs(via: str, then: str) -> None:
    """From the Console: Slurm's programs found once on the person's PATH,
    and run by their full paths (programs.pin); nothing is run when one
    could have been put there by someone else. By hand: by name."""
    if via != "console":
        programs.unpin()
        return
    from nomad.submit import gate
    try:
        programs.pin(private=gate.private_groups(me()))
    except programs.Unsafe as e:
        raise click.ClickException(f"{e}. {then}") from None


def _job_name(pl: Plan, text: str | None, env=None) -> str | None:
    """--job-name for a job sbatch is given nomad's text of: the person's
    script's name, as sbatch would have named it (None when the job names
    itself)."""
    env = os.environ if env is None else env
    named = spec.script_options(text).has("job-name") or (pl.line is not None and pl.line.has("job-name")) \
        or "SBATCH_JOB_NAME" in env           # (the plan names a script's job in its options: not sbatch)
    return None if named else posixpath.basename(pl.script)


def _staging(pl: Plan, flag: bool | None, user: str, out, env=None, text: str | None = None) -> tuple:
    """Whether the job goes through local scratch: (stage.Stage or None,
    what to say when it doesn't, nomad's script, where it is kept, the
    --job-name sbatch needs). ``text``: the script as it is sent (from the
    Console: as the person saw it), else read from it."""
    env = os.environ if env is None else env
    if text is None:
        text = read_text(pl.script, spec.MAX_SCRIPT) if pl.script else None
    options = list(pl.script_sub.options) + (list(pl.line.options) if pl.line is not None else [])
    final = pl.sizing.final()                    # as it will be sent: nomad's values with the job's own
    limit = final.time_s
    if limit is None and not getattr(final, "time_unlimited", False):
        ps = sizing._going_to(final, pl.sizing.partitions)
        limit = ps[0].time_default_s() if len(ps) == 1 else None
    array = any(n == "array" for n, _v in options) or (pl.outcome is not None and pl.whole_array)
    st, note = stage_.decide(flag=flag, env=env, user=user, script=pl.script, text=text, folder=pl.folder,
                             logs=out, name=_name_for(pl, env), nodes=final.nodes, options=options,
                             limit_s=limit, array=array)
    if st is None:
        return None, note, None, None, None
    from nomad import __version__
    wrapper = stage_.wrapper_text(pl.script, text, st, __version__)
    return st, None, wrapper, stage_.store_path(pl.script, wrapper), _job_name(pl, text, env)


def _said_after(pl, out, r, jid, placed, failed, said, staged: bool = False) -> None:
    """What `nomad submit` says once the job is sent."""
    for n in said:
        click.echo(f"\n  {n}")
    if failed:
        raise click.ClickException(failed + ("" if failed.startswith("sbatch could") else
                                             ": check squeue before sending it again (it may have been submitted)"))
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip()[-500:] or f"exit {r.returncode}"
        if jid:
            raise click.ClickException(f"sbatch ended with an error ({msg}), but it had submitted job {jid}: "
                                       "check it with squeue before sending it again")
        raise click.ClickException("sbatch refused it: " + msg)
    if not jid:
        click.echo("\n  Submitted (sbatch didn't print the job's number).")
        return
    chosen = [n for n, c in pl.sizing.choices.items() if c.changed and c.source != "user"]
    mine = [n for n, c in pl.sizing.choices.items() if c.source == "user"]
    click.echo(f"\n  Submitted job {jid}."
               + (f" nomad set: {', '.join(LABELS[n] for n in chosen)}." if chosen else "")
               + (f" You set: {', '.join(LABELS[n] for n in mine)}." if mine else ""))
    if placed is not None:
        if placed.path:
            click.echo(f"  Its output: {placed.path}")
        if placed.note:
            click.echo(f"  {placed.note}")
        if placed.late:
            click.echo(f"  It starts in {logs_.DELAY // 60} minutes at the latest (nomad couldn't move its "
                       "start to now).")
    if staged:
        click.echo(f"  It runs in its node's local scratch; what it makes or changes comes back to "
                   f"{pl.folder} when it ends.")
    text = pl.text if pl.text is not None else (read_text(pl.script, spec.MAX_SCRIPT) or "") if pl.script else ""
    if pl.script_sub.has("hold") or (pl.line is not None and pl.line.has("hold")) or "h" in _pbs_options(text):
        click.echo(f"  It is held, as the job asks: scontrol release {jid} when it should run.")

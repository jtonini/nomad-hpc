# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What a job should ask for, field by field, and why.

``size()`` takes what the job asks (its sbatch options), the program it
runs and what its input file or command line says (``apps``), the person's
past runs (``history``), the cluster's partitions (``cluster``) and, for a
job being run again, how that one ended. For each of CPUs, memory (per
node), time, GPUs and partition it gives a ``Choice``: what was asked, what
nomad suggests, where that comes from, a reason in words, and the sbatch
options that ask for it in the job's own form (``forms``).

Where a value comes from:

- ``input``: the program's input file or command line states it (Gaussian's
  %mem, %nprocshared; ORCA's %maxcore, %pal; Q-Chem's MEM_TOTAL, -nt/-np;
  Columbus's runc -m, -nproc; GROMACS's mdrun -nt; NAMD's +p);
- ``outcome``: the job being run again ran out of memory or time: half as
  much again, or what its past runs needed, whichever is more;
- ``history``: the person's past runs like this one, with the room nomad's
  scores ask for (peak memory and run time × 1.5);
- ``program``: what the program is built for (pmemd.cuda needs a GPU);
- ``partition``: a partition that fits, when the one asked doesn't; and a
  time or memory nomad suggests, capped at what the partition it goes to
  can give;
- ``asked``: what the job asks, kept; ``default``: it asks nothing, and the
  partition's default is kept.

What history may do: lower a value only from at least three runs that
completed, of the same script, or of the same program (not an interpreter
such as python) in the same folder; anything weaker (the same job name, the
same program anywhere) may only raise one. A value the job doesn't ask for
is weighed against the partition's default. A run that failed never counts,
nor a memory reading larger than its nodes have. CPUs are lowered only as
CPUs per task of a one-task job, never as tasks (an MPI launch counts on
them), and only from runs that had at least as many; a job on several nodes
keeps its CPUs and memory (only its time and partition are looked at).

Memory is decided per node, after CPUs and GPUs: asked per CPU or per GPU,
its options are written for the CPUs and GPUs the job will have, so a change
of CPUs never changes its memory unseen (``settle_mem``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from nomad.edu.scoring import format_time_slurm, round_time_up
from nomad.submit import forms
from nomad.submit.apps import GENERIC, Needs, Program
from nomad.submit.cluster import Access, Partition, may_use
from nomad.submit.history import PastRun
from nomad.submit.spec import (Request, SubmitLine, mem_text, parse_memory_mb, parse_submit_line, request_of,
                               slurm_mem)

ROOM = 1.5               # peak memory and run time × this (nomad's scores' rule)
FLOOR = 1.2              # a proven peak, beside an input's own figure
MAX_SIMILAR = 20         # the most recent runs compared
CHANGE = 0.25            # a value within a quarter of what's asked is kept
MIN_TO_LOWER = 3         # runs that completed, before history lowers anything
NOMADS = ("history", "outcome", "partition")    # values nomad works out (capped at what a partition gives)


def round_mem(mb: float) -> int:
    """Memory rounded up to a value people type, finer than the scores'
    ladder (which goes from 16G to 32G): up to 4G by 512M, 16G by 1G, 64G by
    2G, 256G by 8G, then by 32G; at least 1G."""
    mb = max(float(mb), 1024)
    for top, step in ((4096, 512), (16384, 1024), (65536, 2048), (262144, 8192)):
        if mb <= top:
            return int(math.ceil(mb / step) * step)
    return int(math.ceil(mb / 32768) * 32768)


@dataclass
class Choice:
    name: str                # cpus, mem, time, gpus, partition
    asked: Any               # what the job asks (None: it doesn't say)
    value: Any               # what nomad suggests (None: leave it to Slurm's defaults)
    source: str              # input, outcome, history, program, partition, asked, default, user
    reason: str
    runs: int = 0
    opts: list = field(default_factory=list)        # [(option, value)] that ask for it
    replaces: tuple = ()                            # options those replace
    wanted: Any = None                              # capped: what nomad worked out before the cap
    why_wanted: str | None = None                   # ... why
    wanted_from: str | None = None                  # ... where from
    capped_by: str | None = None                    # ... and the partition it was capped for
    unit: str | None = None                         # memory weighed a CPU (or GPU) at a time: "CPU", "GPU"
    each_asked: Any = None                          # ... what the job asks a CPU or GPU
    each_value: Any = None                          # ... and what nomad suggests

    @property
    def changed(self) -> bool:
        return bool(self.opts)

    def asked_text(self) -> str | None:
        if self.unit and self.each_asked is not None:
            return f"{mem_text(self.each_asked)}/{self.unit}"
        return self.text(self.asked) if self.asked is not None else None

    def value_text(self) -> str | None:
        if self.unit and self.each_value is not None:
            return f"{mem_text(self.each_value)}/{self.unit}"
        return self.text() if self.value is not None else None

    def text(self, v=None) -> str | None:
        v = self.value if v is None else v
        if v is None:
            return None
        if self.name == "mem":
            return mem_text(v)
        if self.name == "time":
            return v if isinstance(v, str) else format_time_slurm(v)
        return str(v)

    def to_dict(self) -> dict:
        return {"name": self.name, "asked": self.asked, "value": self.value, "source": self.source,
                "reason": self.reason, "runs": self.runs, "changed": self.changed,
                "options": forms.as_args(self.opts),
                "asked_text": self.asked_text(), "value_text": self.value_text(), "unit": self.unit,
                "each_asked": self.each_asked, "each_value": self.each_value}


ORDER = ("partition", "cpus", "mem", "time", "gpus")


@dataclass
class Sizing:
    sub: SubmitLine
    request: Request
    program: Program
    choices: dict = field(default_factory=dict)       # name -> Choice
    matched: str | None = None                        # which past runs were compared
    strong: bool = False                              # ... and whether they may lower values
    similar: list = field(default_factory=list)       # those runs
    fits: list = field(default_factory=list)          # [{partition, fits, why, guest, may}]
    notes: list = field(default_factory=list)
    problems: list = field(default_factory=list)      # it can't run as it stands
    partitions: list = field(default_factory=list)
    access: Access | None = None
    node_cpus: int | None = None                      # the most CPUs a node here has
    ctx: dict = field(default_factory=dict)           # what was weighed: to weigh again (reweigh)

    def value(self, name: str):
        c = self.choices.get(name)
        return c.value if c else None

    def opts(self) -> list:
        """The sbatch options for what changes (the rest stays the script's)."""
        return [o for name in ORDER if name in self.choices for o in self.choices[name].opts]

    def options(self) -> list:
        return forms.as_args(self.opts())

    def replaces(self) -> set:
        out: set = set()
        for c in self.choices.values():
            if c.opts:
                out |= set(c.replaces)
        return out

    def lines(self) -> list:
        return [f"#SBATCH {o}" for o in self.options()]

    def final(self) -> Request:
        """The request as it would be sent: the job's own options, then these."""
        return request_of(SubmitLine(options=list(self.sub.options) + list(self.opts())))

    def request_without(self, name: str) -> Request:
        """The request as it would be sent, but for one field's options."""
        opts = [o for n in ORDER if n in self.choices and n != name for o in self.choices[n].opts]
        return request_of(SubmitLine(options=list(self.sub.options) + opts))

    def to_dict(self) -> dict:
        return {"request": self.request.to_dict(), "program": self.program.to_dict(),
                "choices": {k: self.choices[k].to_dict() for k in ORDER if k in self.choices},
                "matched": self.matched, "strong": self.strong, "runs": len(self.similar),
                "similar": [r.to_dict() for r in self.similar], "fits": self.fits,
                "notes": list(self.notes), "problems": list(self.problems),
                "options": self.options(), "lines": self.lines()}


# ── which past runs ─────────────────────────────────────────────────────────

ENDED = ("COMPLETED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY")


def similar(runs: list, *, script: str | None = None, program: Program | None = None,
            job_name: str | None = None, folder: str | None = None,
            limit: int = MAX_SIMILAR) -> tuple[str | None, list, bool]:
    """The past runs most like this job, newest first, how they were picked,
    and whether they are close enough to lower a value: the same script, or
    the same program in the very same folder (yes); the same job name, or
    the same program anywhere (raise only). An interpreter (python, R...)
    or a program nomad doesn't know isn't "the same program": two scripts
    run with python can be anything."""
    ended = [r for r in runs or [] if r.state in ENDED]
    exe = program.executable if program else None
    fam = program.family if program else None
    specific = bool(fam) and fam not in GENERIC
    tiers = []
    if script:
        tiers.append(("this script", [r for r in ended if r.script == script], True))
    if specific and exe and folder:
        tiers.append((f"{exe} in this folder",
                      [r for r in ended if r.executable == exe and r.workdir and r.workdir == folder], True))
    if job_name:
        tiers.append((f"jobs named {job_name}",
                      [r for r in ended if r.job_name == job_name
                       and (fam is None or r.family is None or r.family == fam)], False))
    if specific:
        tiers.append((fam, [r for r in ended if r.family == fam], False))
    for what, got, strong in tiers:
        if got:
            return what, got[:limit], strong
    return None, [], False


def _p(values: list, q: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    i = (len(s) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return s[lo] + (s[hi] - s[lo]) * (i - lo)


def _runs_word(n: int) -> str:
    return f"{n} run{'' if n == 1 else 's'}"


def _held(strong: bool) -> str:
    """Why history leaves a value as it is."""
    if strong:
        return "too few runs of this completed to lower it"
    return "they aren't runs of this script (or of its program in this folder): nomad only raises from them"


def _best(known: list) -> tuple:
    """The largest of several (value, source, reason), with its reason and
    the next one's (from another source: the largest of each counts)."""
    top: dict = {}
    for k in known:
        if k[1] not in top or k[0] > top[k[1]][0]:
            top[k[1]] = k
    ranked = sorted(top.values(), key=lambda k: -k[0])
    v, source, reason = ranked[0]
    if len(ranked) > 1:
        reason = f"{reason}; {ranked[1][2]}"
    return v, source, reason


# ── each field ──────────────────────────────────────────────────────────────

def _cpus(req: Request, needs: Needs | None, sim: list, what: str | None, strong: bool) -> Choice:
    asked = req.cpus
    if req.multi_node:
        return Choice("cpus", asked, asked, "asked", "a job on several nodes: its CPUs are kept")
    if req.per_gpu or req.cpus_tres:
        said = f" (the input sets {needs.cpus})" if needs is not None and needs.cpus else ""
        how = "per GPU (--cpus-per-gpu)" if req.per_gpu else "through --tres-per-task"
        return Choice("cpus", asked, asked, "asked", f"asked {how}: kept{said}")
    if needs is not None and needs.cpus:
        n = needs.cpus
        said = needs.cpus_from or str(n)
        where = "the input" if needs.cpus_where == "input" else "its command line"
        if needs.kind == "processes":
            right = req.tasks == n and (req.cpus_per_task or 1) == 1      # a CPU each
            how = f"{n} processes (tasks), a CPU each"
        else:
            right = req.tasks == 1 and (req.cpus_per_task or 1) == n
            how = f"{n} threads of one task"
        if right:
            return Choice("cpus", asked, asked, "input", f"matches {where} ({said})")
        opts, repl = forms.cpus(req, n, needs.kind)
        if asked and asked != n:
            why = f": {asked} CPU{'' if asked == 1 else 's'} would " + (
                "leave some idle" if asked > n else "make them share cores")
        elif asked == n:
            why = ": asked the other way, Slurm could place them wrongly"
        else:
            why = ""
        return Choice("cpus", asked, n, "input", f"{where} sets {how} ({said}){why}", 0, opts, repl)
    measured = [r for r in sim if r.busy_cores and r.state in ("COMPLETED", "TIMEOUT")
                and (r.nodes or 1) == 1]
    if measured and asked and req.threaded and req.cpus_per_task:
        # Busy cores can't be more than a run had: only runs with at least
        # as many CPUs as now asked can show some of them idle.
        wide = [r for r in measured if r.cpus and r.cpus >= asked]
        if not wide:
            return Choice("cpus", asked, asked, "asked",
                          f"your {_runs_word(len(measured))} of {what} had fewer than the {asked} CPUs "
                          "asked: they can't show how many it keeps busy", len(measured))
        busy = [r.busy_cores for r in wide]
        done = [r for r in wide if r.state == "COMPLETED"]
        typical = _p(busy, 0.75)
        v = max(1, math.ceil(typical))
        basis = f"your {_runs_word(len(busy))} of {what} kept about {typical:.1f} cores busy"
        if v < asked * (1 - CHANGE) and asked - v >= 2:
            if strong and len(done) >= MIN_TO_LOWER:
                opts, repl = forms.cpus(req, v, "threads")
                return Choice("cpus", asked, v, "history", f"{basis} of the {asked} asked", len(busy),
                              opts, repl)
            return Choice("cpus", asked, asked, "asked", f"{basis} of the {asked} asked; {_held(strong)}",
                          len(busy))
        return Choice("cpus", asked, asked, "asked", f"fits: {basis}", len(busy))
    if asked is not None:
        return Choice("cpus", asked, asked, "asked",
                      "what the job asks" + ("" if measured else ": no measured run to compare with"))
    return Choice("cpus", None, None, "default", "not asked: one CPU (Slurm's default)")


def _mem(req: Request, needs: Needs | None, sim: list, what: str | None, strong: bool,
         outcome: PastRun | None, node_cpus: int | None, default_mb: int | None) -> Choice:
    asked = req.mem_per_node(node_cpus)
    whole = asked == 0

    def choice(v, source, reason, runs=0):
        if asked is not None and not whole and v == asked:
            return Choice("mem", asked, asked, source, reason, runs)
        opts, repl = forms.mem(req, v, node_cpus)       # written again for the CPUs sent: settle_mem
        return Choice("mem", asked, v, source, reason, runs, opts, repl)

    good = [r for r in sim if r.state != "FAILED"]
    peaks = [r.peak_mem_mb for r in good if r.peak_mem_mb and r.state != "OUT_OF_MEMORY"]
    done_peaks = [r.peak_mem_mb for r in good if r.peak_mem_mb and r.state == "COMPLETED"]
    ooms = [r for r in good if r.state == "OUT_OF_MEMORY"]
    may_lower = strong and len(done_peaks) >= MIN_TO_LOWER
    past = None             # (value, basis, runs): what the past runs needed, with room
    if peaks or ooms:
        need = max(peaks) * ROOM if peaks else 0
        for r in ooms:
            need = max(need, (r.mem_mb or 0) * ROOM)
        basis = (f"your {_runs_word(len(peaks))} of {what} peaked at {mem_text(max(peaks))}"
                 if peaks else f"{len(ooms)} of your runs of {what} ran out of memory")
        if ooms and peaks:
            basis += f", and {len(ooms)} ran out of memory"
        past = (round_mem(need), basis, len(peaks) + len(ooms))
    known = []              # (value, source, reason): the run again, its past runs, the input, the script
    if outcome is not None and outcome.state == "OUT_OF_MEMORY":
        had = outcome.mem_mb or asked or outcome.peak_mem_mb or 0       # what Slurm gave it, a node
        if had:
            known.append((round_mem(max(had, outcome.peak_mem_mb or 0) * ROOM), "outcome",
                          f"it ran out of memory with {mem_text(had)}: half as much again"))
            if past and past[0] > known[0][0] and not req.multi_node:
                known.append((past[0], "history", f"{past[1]}: half as much again"))
            if asked and not whole and asked > known[0][0]:
                known.append((asked, "asked", f"the job asks {mem_text(asked)} now"))
    if req.multi_node:
        if known:
            v, source, reason = _best(known)
            return choice(v, source, reason, 1)
        return Choice("mem", asked, asked, "asked", "a job on several nodes: its memory is kept")
    if needs is not None and needs.mem_mb:
        said = "; ".join(needs.notes) or needs.mem_from or ", ".join(needs.said)
        where = "the input" if needs.mem_where == "input" else "its command line"
        known.append((round_mem(needs.mem_mb), "input", f"what {where} sets ({said})"))
        if strong and peaks:
            known.append((round_mem(max(peaks) * FLOOR), "history",
                          f"your {_runs_word(len(peaks))} of {what} peaked at {mem_text(max(peaks))}"))
    if known:
        v, source, reason = _best(known)
        if source == "input" and len(known) == 1 and asked and not whole \
                and v <= asked <= v * (1 + CHANGE) + 1024:
            return Choice("mem", asked, asked, "input", f"fits {reason[0].lower() + reason[1:]}")
        if whole and source != "outcome" and not may_lower:
            return Choice("mem", asked, asked, "asked", f"all of a node's memory, which covers {reason}")
        return choice(v, source, reason, 1 if source == "outcome" else 0)
    if past:
        v, basis, n = past
        eff = asked if asked is not None else default_mb
        keep = "asked" if asked is not None else "default"
        if eff is None:
            # Not asked, and the partition's default isn't known (Slurm's own
            # is all of a node's): only runs close enough to lower set one.
            if may_lower:
                return choice(v, "history", f"{basis}: half as much again", n)
            return Choice("mem", None, None, "default",
                          f"not asked: the partition's default applies ({basis}; {_held(strong)})", n)
        if eff == 0:
            if may_lower:
                return choice(v, "history", f"{basis}: {mem_text(v)} rather than all of a node's, "
                              "and it starts sooner", n)
            return Choice("mem", asked, asked, keep, f"all of a node's memory: {basis}", n)
        label = mem_text(asked) if asked is not None else f"the partition's default ({mem_text(eff)})"
        if v > eff:
            return choice(v, "history", f"{basis}: {label} may not be enough", n)
        if v < eff * (1 - CHANGE):
            if may_lower:
                return choice(v, "history", f"{basis}: {mem_text(v)} is plenty, and starts sooner", n)
            return Choice("mem", asked, asked, keep, f"{basis}; {_held(strong)}", n)
        return Choice("mem", asked, asked, keep, f"fits: {basis}", n)
    if asked is not None:
        return Choice("mem", asked, asked, "asked",
                      "all of a node's memory" if whole else "what the job asks: no past run to compare with")
    return Choice("mem", None, None, "default", "not asked: the partition's default applies")


PER = ("mem-per-cpu", "mem-per-gpu")


def _mem_each(req: Request, sent: Request, needs: Needs | None, outcome: PastRun | None,
              node_cpus: int | None) -> Choice:
    """Memory for tasks Slurm places over as many nodes as it takes (or on
    several nodes), asked per CPU or per GPU: what one node gets isn't
    known (nor alike on each), so it is weighed a
    CPU (or GPU) at a time -- the input's memory for each process, half as
    much again a CPU when the job ran out -- and past runs' per-node
    readings aren't. ``sent``: the job as it will be sent (its CPUs), but
    for memory. Per CPU, it stays right however many tasks there are."""
    unit = "CPU" if sent.mem_form == "mem-per-cpu" else "GPU"
    each_asked = sent.mem_per_cpu_mb if unit == "CPU" else sent.mem_per_gpu_mb
    asked = req.mem_per_node(node_cpus)

    def a_node(each):
        n = (sent.cpus_per_node(node_cpus) or 1) if unit == "CPU" else (sent.gpus_per_node() or sent.gpus or 1)
        return each * n
    base = f"tasks Slurm places over its nodes: memory weighed a {unit} at a time"
    known = []
    if outcome is not None and outcome.state == "OUT_OF_MEMORY":
        n = outcome.cpus if unit == "CPU" else outcome.gpus
        ran = (outcome.mem_mb * (outcome.nodes or 1) / n) if (outcome.mem_mb and n) else each_asked
        if ran:
            known.append((forms._up(ran * ROOM), "outcome",
                          f"it ran out of memory with {mem_text(ran)} a {unit}: half as much again"))
    if needs is not None and needs.mem_mb and unit == "CPU":
        n = sent.cpus or needs.cpus or 1
        said = "; ".join(needs.notes) or needs.mem_from or ", ".join(needs.said)
        where = "the input" if needs.mem_where == "input" else "its command line"
        known.append((forms._up(needs.mem_mb / n), "input",
                      f"what {where} sets ({said}): {mem_text(needs.mem_mb / n)} a CPU"))
    if outcome is not None and known and each_asked and each_asked > max(k[0] for k in known):
        known.append((each_asked, "asked", f"the job asks {mem_text(each_asked)} a {unit} now"))
    keep = {"unit": unit, "each_asked": each_asked, "each_value": each_asked}
    if not known:
        return Choice("mem", asked, asked, "asked", f"{base}: what the job asks", **keep)
    v, source, reason = _best(known)
    if v == each_asked or (source == "input" and len(known) == 1 and each_asked
                           and v <= each_asked <= v * (1 + CHANGE) + 64):
        return Choice("mem", asked, asked, "asked" if source != "input" else "input",
                      f"{base}; {'fits ' if v != each_asked else ''}{reason}", 1 if outcome else 0, **keep)
    return Choice("mem", asked, a_node(v), source, f"{base}; {reason}", 1 if source == "outcome" else 0,
                  [(sent.mem_form, slurm_mem(v))], forms.MEM_OPTIONS, unit=unit, each_asked=each_asked,
                  each_value=v)


def _time(req: Request, sim: list, what: str | None, strong: bool, outcome: PastRun | None,
          default_s: int | None) -> Choice:
    asked = req.time_s

    def choice(v, source, reason, runs=0):
        if v == asked:
            return Choice("time", asked, asked, source, reason, runs)
        opts, repl = forms.time(v)
        return Choice("time", asked, v, source, reason, runs, opts, repl)

    done = [r.elapsed_s for r in sim if r.state == "COMPLETED" and r.elapsed_s]
    tos = [r for r in sim if r.state == "TIMEOUT"]
    may_lower = strong and len(done) >= MIN_TO_LOWER
    past = None
    if done or tos:
        need = max(done) * ROOM if done else 0
        for r in tos:
            need = max(need, (r.time_s or r.elapsed_s or 0) * ROOM)
        basis = (f"the longest of your {_runs_word(len(done))} of {what} took "
                 f"{format_time_slurm(max(done))}" if done
                 else f"{len(tos)} of your runs of {what} reached their limit")
        if tos and done:
            basis += f", and {len(tos)} reached their limit"
        past = (round_time_up(need), basis, len(done) + len(tos))
    if req.time_unlimited:
        return Choice("time", "UNLIMITED", "UNLIMITED", "asked", "no limit, as the job asks: kept")
    if outcome is not None and outcome.state == "TIMEOUT":
        had = outcome.time_s or asked or outcome.elapsed_s or 3600
        v = round_time_up(had * ROOM)
        reason = f"it reached its time limit of {format_time_slurm(had)}: half as long again"
        source = "outcome"
        if past and past[0] > v:
            v, source = past[0], "history"
            reason = f"{past[1]}: half as long again; it reached its limit of {format_time_slurm(had)}"
        if asked and asked > v:
            return Choice("time", asked, asked, "asked",
                          f"it reached its limit of {format_time_slurm(had)}; the job asks "
                          f"{format_time_slurm(asked)} now", 1)
        return choice(v, source, reason, 1)
    if past:
        v, basis, n = past
        eff = asked if asked is not None else default_s
        keep = "asked" if asked is not None else "default"
        if eff is None:
            if may_lower:
                return choice(v, "history", f"{basis}: half as long again", n)
            return Choice("time", None, None, "default",
                          f"not asked: the partition's default limit applies ({basis}; {_held(strong)})", n)
        label = (format_time_slurm(asked) if asked is not None
                 else f"the partition's default ({format_time_slurm(eff)})")
        if v > eff:
            return choice(v, "history", f"{basis}: {label} may not be enough", n)
        if v < eff * (1 - CHANGE):
            if may_lower:
                return choice(v, "history",
                              f"{basis}: a shorter limit starts sooner (the scheduler fits it in gaps)", n)
            return Choice("time", asked, asked, keep, f"{basis}; {_held(strong)}", n)
        return Choice("time", asked, asked, keep, f"fits: {basis}", n)
    if asked is not None:
        return Choice("time", asked, asked, "asked", "what the job asks: no past run to compare with")
    return Choice("time", None, None, "default", "not asked: the partition's default limit applies")


def _gpus(req: Request, sub: SubmitLine, program: Program) -> Choice:
    asked = req.gpus
    if not asked and program.gpu_build:
        n = max(1, req.nodes or 1)                      # one on each node
        opts, repl = forms.gpus(req, sub, n)
        return Choice("gpus", asked, n, "program", f"{program.executable} runs on a GPU"
                      + (" (one a node)" if n > 1 else ""), 0, opts, repl)
    if asked:
        return Choice("gpus", asked, asked, "asked", "what the job asks")
    return Choice("gpus", None, None, "default", "none asked")


def _mem_opts(s: Sizing, r: Request, v: int, at_most: bool) -> tuple[list, tuple]:
    """Memory options for ``v`` a node, in the job's form (``r``: the job
    as sent, but for memory). Per CPU or GPU they are rounded up, unless
    that passes the biggest node where it goes while ``v`` doesn't: then
    down."""
    opts, repl = forms.mem(r, v, s.node_cpus, at_most=at_most)
    if at_most or r.mem_form not in PER:
        return opts, repl
    top = max((p.node_mem_mb or 0 for p in _going_to(r, s.partitions)), default=0)
    n = (r.cpus_per_node(s.node_cpus) or 1) if r.mem_form == "mem-per-cpu" else (r.gpus_per_node() or r.gpus or 1)
    each = parse_memory_mb(opts[0][1]) or 0
    if top and v <= top < each * n:
        return forms.mem(r, v, s.node_cpus, at_most=True)
    return opts, repl


def settle_mem(s: Sizing) -> None:
    """Memory's options written for the CPUs and GPUs the job will be sent
    with: memory asked per CPU or per GPU is per node what the table says,
    whatever the CPUs become (nomad's, or the person's). Tasks Slurm places
    over its nodes keep memory per CPU or GPU as it is (see _mem_each)."""
    c = s.choices.get("mem")
    if c is None or c.value is None or c.unit:
        return
    r = s.request_without("mem")
    if r.spread and r.mem_form in PER:
        return
    if c.opts:
        # a node's memory (a cap) is written per CPU or GPU rounding down: never past it
        c.opts, c.replaces = _mem_opts(s, r, c.value, c.source == "partition")
        return
    if c.value and c.asked == c.value and r.mem_form in PER:
        now = r.mem_per_node(s.node_cpus)
        if now is not None and now != c.value:
            c.opts, c.replaces = _mem_opts(s, r, c.value, False)
            unit = "CPU" if r.mem_form == "mem-per-cpu" else "GPU"
            c.source = "asked"
            c.reason += (f"; it is asked per {unit}, so {forms.as_args(c.opts)[0]} keeps "
                         f"{mem_text(c.value)} a node")


def route_mem(s: Sizing) -> None:
    """Memory decided the way the job will be sent: a CPU (or GPU) at a
    time for tasks Slurm places over its nodes, or on several, with memory
    per CPU or GPU (_mem_each); else a node at a time (_mem, settle_mem).
    Again when the person's CPUs or GPUs change that."""
    ctx = s.ctx
    req, r = s.request, s.request_without("mem")
    c = s.choices.get("mem")
    if c is not None and c.source == "user":
        settle_mem(s)
        return
    if r.mem_form in PER and (r.spread or r.multi_node):
        s.choices["mem"] = _mem_each(req, r, ctx.get("needs"), ctx.get("outcome"), s.node_cpus)
        return
    if c is not None and c.unit and r.mem_form in PER:
        return                     # weighed a CPU (or GPU) at a time: still right for one task
    if c is None or c.unit:
        ps = _going_to(r, s.partitions)
        p = ps[0] if len(ps) == 1 else None
        s.choices["mem"] = _mem(req, ctx.get("needs"), ctx.get("sim", []), ctx.get("label"),
                                ctx.get("strong", False), ctx.get("outcome"), s.node_cpus,
                                p.mem_default_mb(r.cpus_per_node(p.node_cpus)) if p else None)
    settle_mem(s)


def reweigh(s: Sizing) -> None:
    """What the job doesn't ask (time, memory), weighed again against the
    defaults of the partition it goes to now (nomad's pick, or yours)."""
    ctx = s.ctx
    if not ctx:
        return
    final = s.final()
    ps = _going_to(final, s.partitions)
    q = ps[0] if len(ps) == 1 else None
    name = q.name if q else (final.partition if not ps and len(final.partition_alts) == 1 else None)
    if name is None or name == ctx.get("weighed_for"):
        return
    req = s.request
    ctx["weighed_for"] = name
    if req.time_s is None and not req.time_unlimited and s.choices["time"].source != "user":
        s.choices["time"] = _time(req, ctx["sim"], ctx["label"], ctx["strong"], ctx["outcome"],
                                  q.time_default_s() if q else None)
        s.notes = [n for n in s.notes if not n.endswith(_TIME_NOTE)]
    if req.mem_per_node(s.node_cpus) is None and s.choices["mem"].source != "user":
        interim = s.request_without("mem")
        s.choices["mem"] = _mem(req, ctx["needs"], ctx["sim"], ctx["label"], ctx["strong"], ctx["outcome"],
                                s.node_cpus, q.mem_default_mb(interim.cpus_per_node(q.node_cpus)) if q else None)
        s.notes = [n for n in s.notes if not n.endswith(_MEM_NOTE)]
        settle_mem(s)


# ── where it fits ───────────────────────────────────────────────────────────

def why_not(p: Partition, req: Request, access: Access | None) -> list:
    """Why ``req`` can't run in ``p`` (empty: it fits): its limits, a node
    with the memory, CPUs and GPUs one node needs (all on one node), its
    GPUs in all, and whether the job's account may use it."""
    out = []
    if not p.up:
        out.append(f"it is {str(p.state).lower()}")
    if req.time_s and p.max_time_s and req.time_s > p.max_time_s:
        out.append(f"it allows at most {format_time_slurm(p.max_time_s)}")
    elif req.time_unlimited and p.max_time_s:
        out.append(f"it allows at most {format_time_slurm(p.max_time_s)} (the job asks no limit)")
    gpn = req.least_gpus_per_node()
    if req.spread:
        # Slurm puts as many tasks on a node as fit: a node needs room for one
        cpn = req.cpus_per_task or 1
        mem = (req.mem_per_cpu_mb * cpn if req.mem_form == "mem-per-cpu"
               else req.mem_per_gpu_mb * (gpn or 1) if req.mem_form == "mem-per-gpu"
               else req.mem_per_node(p.node_cpus))
    else:
        cpn = req.cpus_per_node(p.node_cpus)
        if req.cpus_per_task and cpn and req.cpus_per_task > cpn:
            cpn = req.cpus_per_task
        mem = req.mem_per_node(p.node_cpus)
    if p.max_cpus_per_node and cpn and cpn > p.max_cpus_per_node:
        out.append(f"it allows at most {p.max_cpus_per_node} CPUs a node")
    if p.max_mem_per_node_mb and mem and mem > p.max_mem_per_node_mb:
        out.append(f"it allows at most {mem_text(p.max_mem_per_node_mb)} a node")
    if p.shapes:
        def ok(shape):
            m, c, g = shape
            return ((not mem or not m or mem <= m) and (not cpn or not c or cpn <= c)
                    and (not gpn or gpn <= (g or 0)))
        if not any(ok(s) for s in p.shapes):
            if gpn and not p.node_gpus:
                out.append("it has no GPUs")
            else:
                need = [x for x in (f"{cpn} CPU{'' if cpn == 1 else 's'}" if cpn else None,
                                    mem_text(mem) if mem else None,
                                    f"{gpn} GPU{'' if gpn == 1 else 's'}" if gpn else None) if x]
                out.append("none of its nodes has " + " and ".join(need)
                           + (" together" if len(need) > 1 else ""))
        elif req.gpus and p.total_gpus and req.gpus > p.total_gpus:
            out.append(f"it has {p.total_gpus} GPUs in all")
    if req.spread and p.counts and not out:
        # How many such tasks its nodes hold in all (by CPUs, and memory per CPU).
        cpt = req.cpus_per_task or 1
        each_mem = req.mem_per_cpu_mb * cpt if req.mem_form == "mem-per-cpu" else None
        room = 0
        for (m, c, _g), n in p.counts.items():
            fit = (c or 0) // cpt
            if each_mem and m:
                fit = min(fit, m // each_mem)
            room += fit * n
        if room and req.tasks > room:
            out.append(f"its nodes hold at most {room} such task{'' if room == 1 else 's'}")
    if req.nodes and p.max_nodes and req.nodes > p.max_nodes:
        out.append(f"it allows at most {p.max_nodes} nodes")
    if req.cpus and p.total_cpus and req.cpus > p.total_cpus:
        out.append(f"it has {p.total_cpus} CPUs in all")
    ok_use, why = may_use(p, access, req.account)
    if ok_use is False:
        out.append(why)
    return out


def _default_name(parts: list) -> str | None:
    return next((p.name for p in parts if p.default), None)


def _going_to(req: Request, parts: list) -> list:
    """The partitions a request goes to: the ones it names, else the default."""
    names = req.partition_alts or [n for n in [_default_name(parts)] if n]
    return [p for p in parts if p.name in names]


def _partition(s: Sizing) -> Choice:
    req, parts, access = s.request, s.partitions, s.access
    asked = req.partition
    final = s.final()
    s.fits = []
    if not parts:
        return Choice("partition", asked, asked, "asked" if asked else "default",
                      "the cluster's partitions weren't read here" if asked
                      else "not asked: the cluster's default partition")
    names = {p.name: p for p in parts}
    for p in parts:
        why = why_not(p, final, access)
        may, may_why = may_use(p, access, final.account)
        s.fits.append({"partition": p.name, "fits": not why, "why": why, "guest": p.guest,
                       "default": p.default, "may_use": may, "may_why": may_why,
                       "max_time": format_time_slurm(p.max_time_s) if p.max_time_s else None,
                       "node_mem": mem_text(p.node_mem_mb) if p.node_mem_mb else None,
                       "node_cpus": p.node_cpus, "node_gpus": p.node_gpus})
    by = {f["partition"]: f for f in s.fits}
    if len(req.partition_alts) > 1:
        # -p a,b: Slurm starts it in whichever can run it first; nomad checks them.
        text = ",".join(req.partition_alts)
        listed = [a for a in req.partition_alts if a in by]
        good = [a for a in listed if by[a]["fits"]]
        if good or len(listed) < len(req.partition_alts):
            return Choice("partition", text, text, "asked",
                          f"Slurm starts it in the first of {', '.join(req.partition_alts)} that can run it"
                          + (f" (it fits {', '.join(good)})" if good else ""))
        return Choice("partition", text, text, "asked", f"none of {', '.join(listed)} fits it: "
                      + "; ".join(f"{a}: {'; '.join(by[a]['why'])}" for a in listed))
    if asked and asked not in names:
        return Choice("partition", asked, asked, "asked",
                      f"{asked} isn't among the partitions Slurm lists here: kept, Slurm checks it")
    target = asked or _default_name(parts)
    if target and by[target]["fits"]:
        if asked:
            return Choice("partition", asked, asked, "asked", f"it fits {asked}")
        return Choice("partition", None, None, "default", f"not asked: the default, {target}, fits")
    # Something that fits: one they may use, not preemptible, then the
    # smallest nodes (big nodes left for jobs that need them; nodes nomad
    # couldn't read last).
    # A partition whose nodes weren't read (when others' were) can't be
    # said to fit: not offered.
    read = any(p.shapes for p in parts)
    ok = [f for f in s.fits if f["fits"] and f["may_use"] is not False
          and (names[f["partition"]].shapes or not read)]
    ok.sort(key=lambda f: (f["may_use"] is not True, f["guest"], not f["default"],
                           names[f["partition"]].node_mem_mb or 0))
    head = (f"{target} doesn't fit: " + "; ".join(by[target]["why"])) if target else "no default partition"
    if not ok:
        return Choice("partition", asked, asked, "asked" if asked else "default",
                      f"{head}; no partition fits it as it stands")
    pick = ok[0]
    extra = " (preemptible: it may be stopped and requeued)" if pick["guest"] else ""
    opts, repl = forms.partition(pick["partition"])
    return Choice("partition", asked, pick["partition"], "partition",
                  f"{head}; {pick['partition']} fits{extra}", 0, opts, repl)


_TIME_NOTE = ": the job may not finish (a checkpoint, or a partition with a longer limit, would help)."
_MEM_NOTE = ": the job may run out of memory."


def cap(s: Sizing) -> bool:
    """A time or memory nomad works out (from past runs, or how a job
    ended) that the partitions the job goes to can't give: asked at the
    most they give, with a note. True when something was capped."""
    ps = _going_to(s.final(), s.partitions)
    capped = False
    # A value capped for one partition, now going to one that gives it all
    # (or one Slurm doesn't list here: Slurm checks it): as nomad worked it out.
    for name in ("time", "mem"):
        c = s.choices.get(name)
        if c is None or c.source != "partition" or c.wanted is None:
            continue
        room = [p.max_time_s for p in ps] if name == "time" else [p.node_mem_mb for p in ps]
        if not ps or None in room or c.wanted <= max(room):
            opts, repl = (forms.time(c.wanted) if name == "time" else ([("mem", str(c.wanted))], forms.MEM_OPTIONS))
            if c.wanted == c.asked:
                opts, repl = [], ()
            s.choices[name] = Choice(name, c.asked, c.wanted, c.wanted_from or "history",
                                     c.why_wanted or c.reason, c.runs, opts, repl)
            s.notes = [n for n in s.notes if not n.endswith(_TIME_NOTE if name == "time" else _MEM_NOTE)]
            if name == "mem":
                settle_mem(s)
    if not ps:
        return False
    names = {p.name for p in ps}
    t = s.choices.get("time")
    limits = [p.max_time_s for p in ps]
    wanted = t.wanted if t is not None and t.wanted is not None else (t.value if t is not None else None)
    if t is not None and t.source in NOMADS and wanted and None not in limits and wanted > max(limits) \
            and (t.value != max(limits) or t.capped_by not in names):
        top = max(limits)
        p = next(x for x in ps if x.max_time_s == top)
        opts, repl = forms.time(top) if top != t.asked else ([], ())
        why = t.why_wanted or t.reason
        s.notes = [n for n in s.notes if not n.endswith(_TIME_NOTE)]
        s.notes.append(f"{format_time_slurm(wanted)} would be safer than {p.name} allows{_TIME_NOTE}")
        s.choices["time"] = Choice("time", t.asked, top, "partition",
                                   f"{why}; {p.name} allows at most {format_time_slurm(top)}, so that "
                                   "is asked", t.runs, opts, repl, wanted, why,
                                   t.wanted_from or t.source, p.name)
        capped = True
    m = s.choices.get("mem")
    biggest = [p.node_mem_mb for p in ps]
    final = s.final()
    notional = final.spread and final.mem_form in PER          # no one node's figure
    wanted = m.wanted if m is not None and m.wanted is not None else (m.value if m is not None else None)
    if m is not None and m.source in NOMADS and wanted and None not in biggest and not notional and not m.unit \
            and wanted > max(biggest) and (m.value != max(biggest) or m.capped_by not in names):
        top = max(biggest)
        p = next(x for x in ps if x.node_mem_mb == top)
        why = m.why_wanted or m.reason
        s.notes = [n for n in s.notes if not n.endswith(_MEM_NOTE)]
        s.notes.append(f"{mem_text(wanted)} would be safer than {p.name}'s biggest node has{_MEM_NOTE}")
        s.choices["mem"] = Choice("mem", m.asked, top, "partition",
                                  f"{why}; {p.name}'s biggest node has {mem_text(top)}, so that is "
                                  "asked", m.runs, [("mem", str(top))] if top != m.asked else [],
                                  forms.MEM_OPTIONS if top != m.asked else (), wanted, why,
                                  m.wanted_from or m.source, p.name)
        settle_mem(s)
        capped = True
    return capped


def check(s: Sizing) -> list:
    """What would stop the job as it would be sent (its own options and
    these) in the partitions it goes to (any one of them is enough)."""
    final = s.final()
    ps = _going_to(final, s.partitions)
    if not ps:
        return []
    whys = [(p.name, why_not(p, final, s.access)) for p in ps]
    if any(not w for _, w in whys):
        return []
    return [f"{name}: " + "; ".join(w) for name, w in whys]


# ── all together ────────────────────────────────────────────────────────────

def size(sub: SubmitLine, program: Program, runs: list, *, needs: Needs | None = None,
         partitions: list | None = None, access: Access | None = None,
         outcome: PastRun | None = None, script: str | None = None,
         folder: str | None = None, args: list | None = None) -> Sizing:
    """What the job should ask for. ``sub``: its options (#SBATCH lines,
    then its command line); ``runs``: the person's past jobs; ``outcome``:
    the job being run again, as it ended; ``folder``: where it runs (the
    whole path)."""
    req = request_of(sub)
    parts = partitions or []
    node_cpus = max((p.node_cpus or 0 for p in parts), default=0) or None
    out = Sizing(sub=sub, request=req, program=program, partitions=parts, access=access,
                 node_cpus=node_cpus)
    # A memory reading larger than the nodes it ran on have isn't real (Slurm
    # adding up memory the processes share): left out.
    biggest = max((p.node_mem_mb or 0 for p in parts), default=0) or None
    node_mem = {p.name: p.node_mem_mb for p in parts if p.node_mem_mb}
    bad = []
    for r in runs or []:
        cap_mb = node_mem.get(r.partition or "", biggest)
        if cap_mb and r.peak_mem_mb and r.peak_mem_mb > cap_mb:
            r.peak_mem_mb = None
            bad.append(r)
    what, sim, strong = similar(runs, script=script, program=program, job_name=req.job_name,
                                folder=folder)
    out.matched, out.similar, out.strong = what, sim, strong
    if any(r in sim for r in bad):
        out.notes.append("A memory reading larger than any node here was left out (Slurm can count "
                         "memory the processes share more than once).")
    label = what or "this job"
    # CPUs and GPUs first: memory asked per CPU or per GPU is written for them.
    out.choices["cpus"] = _cpus(req, needs, sim, label, strong)
    out.choices["gpus"] = _gpus(req, sub, program)
    there = _going_to(req, parts)
    p = there[0] if len(there) == 1 else None
    out.ctx = {"needs": needs, "sim": sim, "label": label, "strong": strong, "outcome": outcome,
               "weighed_for": p.name if p else None}
    route_mem(out)
    out.choices["time"] = _time(req, sim, label, strong, outcome, p.time_default_s() if p else None)
    out.choices["partition"] = _partition(out)
    if cap(out):
        out.choices["partition"] = _partition(out)
    # Moved to another partition: what the job doesn't ask is weighed
    # against that one's defaults instead.
    reweigh(out)
    cap(out)
    if outcome is not None:
        if outcome.state == "FAILED":
            out.notes.append("The job this is based on stopped with an error"
                             + (f" (exit code {outcome.exit_code})" if outcome.exit_code else "")
                             + ": more resources won't fix that. Look at its error output first.")
        elif outcome.state in ("NODE_FAIL", "PREEMPTED", "BOOT_FAIL"):
            out.notes.append("The job this is based on stopped for a reason outside it (a node failed, "
                             "or it was preempted): running it again as it was is fine.")
    done = [r for r in sim if r.state == "COMPLETED"]
    if not sim and needs is None:
        out.notes.append("None of your past jobs looks like this one, and nomad can't read sizes from "
                         "its program: the values are the job's own.")
    elif sim and not strong:
        out.notes.append(f"Compared with your runs of {what}, not of this script: nomad only raises "
                         "values from those.")
    elif sim and len(done) < MIN_TO_LOWER:
        out.notes.append((f"Only {_runs_word(len(done))} of this completed" if done
                          else "None of these runs completed") + ": too few for nomad to lower a value from them.")
    if args is not None and strong and what == "this script":
        other = [r for r in sim if r.submit_line and parse_submit_line(r.submit_line).args != list(args)]
        if other:
            out.notes.append(f"{len(other)} of these runs had other arguments (other inputs?): "
                             "check the values.")
    if req.multi_node:
        out.notes.append("A job on several nodes: nomad checks its time and partition, and keeps its "
                         "CPUs and memory.")
    if outcome is not None and outcome.state == "OUT_OF_MEMORY" and out.choices["mem"].source not in (
            "outcome", "history", "input", "partition"):
        if not (outcome.mem_mb or req.mem_per_node(node_cpus) or outcome.peak_mem_mb):
            out.notes.append("It ran out of memory, but Slurm's record doesn't say how much it had: give "
                             "--mem yourself.")
    out.problems = check(out)
    return out

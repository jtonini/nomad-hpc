# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Making a job script for someone who has none (1.26.0): from a program's
template, or from a past script of theirs.

A template says how a site runs a program, written once by its staff in the
hub's nomad.toml; the person picks the program, the version (one of the
modules the cluster has: the modules collector reads them daily), and the
input (a file, or a folder of inputs), and nomad writes the script::

    [submit.build.spydur]                  # a site's own settings
    module_paths = ["/usr/local/sw/modulefiles"]   # module use, before module load
    scratch = "/scratch/$USER/$SLURM_JOB_ID"       # {scratch} in a template

    [[submit.templates]]
    program = "Gaussian"
    sites = ["spydur", "arachne"]          # where it is offered (default: every site)
    modules = ["gaussian/*"]               # the versions to choose from (shell patterns)
    default = "gaussian/latest"            # chosen first, when the cluster has it (or a list: the first there)
    inputs = [".gjf", ".com"]              # the input: a file with one of these endings ...
    # folder = "control.run"               # ... or a folder holding this file (run there)
    setup = ["export GAUSS_SCRDIR={scratch}", "mkdir -p {scratch}"]
    run = "g16 < {input} > {stem}.log"
    after = ["rm -rf {scratch}"]
    outputs = ["{stem}.log"]               # said if one is there already
    cpus = 8                               # what to ask when nothing better is known
    mem = "16G"
    time = "1-00:00:00"
    # gpus = 1
    # partition = "gpunodes"               # or one a site: { spydur = "gpunodes" }
    # parallel = "mpi"                     # cpus as --ntasks (MPI) rather than --cpus-per-task
    [submit.templates.run_for]
    "gaussian/g09*" = "g09 < {input} > {stem}.log"   # a version that runs otherwise

A template may offer a few choices (1.29.0): at most four options, each of
two to six choices, the first chosen unless the person picks another. A
choice changes what it names: the run line, lines added to setup and after,
outputs, how it runs in parallel, the size to ask for, #SBATCH lines::

    [[submit.templates.options]]
    name = "parallel"                      # a short key: letters, digits, _
    label = "Runs as"
    [[submit.templates.options.choices]]
    label = "MPI processes, one a core"
    parallel = "mpi"
    run = "srun cp2k.psmp -i {input} -o {stem}.out"
    [[submit.templates.options.choices]]
    label = "MPI processes of 2 threads each"
    parallel = "hybrid"
    threads_per_task = 2                   # --ntasks = cpus / 2, --cpus-per-task = 2
    omp = true                             # export OMP_NUM_THREADS={threads}
    run = "srun cp2k.psmp -i {input} -o {stem}.out"

Only one option may set the run line, and only one how it runs in parallel.
``parallel`` is "threads" (one process: --cpus-per-task), "mpi" (one process a
CPU: --ntasks, one node) or "hybrid" (--ntasks processes of
``threads_per_task`` threads, one node).

In a template's lines, ``{input}`` (the input file's name, quoted for the
shell), ``{stem}`` (its name without the ending), ``{folder}`` (where the job
runs), ``{cpus}``, ``{tasks}`` and ``{threads}`` (the processes, and each one's
threads: 1 and cpus with threads, cpus and 1 with MPI), ``{mem_mb}`` (the job's
memory, MB), ``{mem_mb_each}`` (each process's share of nine tenths of it, for
programs told their memory per process: Columbus's runc -m), ``{gpus}``,
``{module}``, ``{scratch}``; anything else in braces is left as it is
(``${SLURM_CPUS_PER_TASK}``).

Nothing here runs anything or reads a file: the script is text, shown to the
person in full, and saved (as them, with the editor's safeguards) only when
they say so; then it is sized, checked and sent as any other.
"""
from __future__ import annotations

import fnmatch
import itertools
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime

from nomad.submit.gate import _PATH as _PERSON_PATH
from nomad.submit.spec import parse_memory_mb, parse_time_seconds, slurm_mem

PLACES = ("input", "stem", "folder", "cpus", "tasks", "threads", "mem_mb", "mem_mb_each", "gpus", "module",
          "scratch")
_PLACE = re.compile(r"(?<!\$)\{(" + "|".join(PLACES) + r")\}")
_ANY_PLACE = re.compile(r"(?<!\$)\{([A-Za-z_]+)\}")
_PROGRAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,39}$")
_MODULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/*?\[\]-]{0,127}$")
_ENDING = re.compile(r"^\.[A-Za-z0-9_]{1,12}$")
_PARTITION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PATH = re.compile(r"^/[A-Za-z0-9._/$@{}+-]{1,255}$")
_MODULE_PATH = re.compile(r"/[A-Za-z0-9._/+-]{1,255}")      # written quoted: no $ to expand
_JOB_ID = re.compile(r"\$\{?SLURM_JOB_ID\}?")
# A job's time limit as Slurm takes it: minutes, M:S, H:M:S, D-H, D-H:M, D-H:M:S.
_TIME = re.compile(r"(?:[0-9]{1,3}-)?[0-9]{1,4}(?::[0-9]{1,2}){0,2}")
# Copies of a module file kept aside (columbus/722.bak-20260630): never a version to offer.
_ASIDE = re.compile(r"(\.bak|\.old|\.orig|\.save|~|\.rpmsave|\.rpmnew)([.-].*)?$", re.I)
MAX_LINES = 20
MAX_LINE = 400
MAX_OPTIONS = 4
MAX_CHOICES = 6
PARALLEL = ("threads", "mpi", "hybrid")
_OPTION = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
# what no line or label holds (the gate's own rule for paths): C0 and C1 controls but the tab, line and
# paragraph separators, invisible and direction marks -- a label with U+2028 would be two lines to a
# reader that splits there, the second an #SBATCH line Slurm never sees
_UNSEEN = re.compile("[\x00-\x08\x0a-\x1f\x7f-\x9f\xad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f"
                     "\u200b-\u200f\u2028-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0\ufff0-\ufffb"
                     "\ud800-\udfff\U000e0000-\U000e0fff]")
# an #SBATCH line a template may add: --name or --name=value, no spaces or quotes
_SBATCH = re.compile(r"^--([a-z][a-z-]{1,30})(=[A-Za-z0-9_.,:=@%/+-]{1,100})?$")
# set by nomad itself (a template that set them too would ask twice, or sbatch would refuse the two
# together), or that would break the script nomad writes (--export=NONE: no module command; --chdir:
# not where the input is; --no-requeue: a guest's job cancelled, not requeued)
_SBATCH_OWN = ("job-name", "cpus-per-task", "ntasks", "nodes", "mem", "time", "gres", "partition", "output",
               "open-mode", "mem-per-cpu", "mem-per-gpu", "cpus-per-gpu", "gpus", "gpus-per-node",
               "gpus-per-task", "gpus-per-socket", "ntasks-per-node", "ntasks-per-core", "ntasks-per-socket",
               "ntasks-per-gpu", "export", "chdir", "wrap", "array", "no-requeue", "error", "input",
               "tasks-per-node", "tres-per-task", "container", "container-id")


class TemplateError(ValueError):
    """A template in nomad.toml that can't be used: said, never guessed at."""


@dataclass
class Choice:
    """One of an option's choices: what it changes (None: nothing)."""
    label: str
    run: str | None = None
    setup: list = field(default_factory=list)
    after: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    sbatch: list = field(default_factory=list)
    parallel: str | None = None
    threads_per_task: int | None = None
    omp: bool | None = None
    cpus: int | None = None
    mem: str | None = None
    time: str | None = None
    gpus: int | None = None
    partition: str | dict | None = None

    def sizes(self, site: str | None = None) -> dict:
        """What it asks for, for the page to show when it is picked."""
        out = {k: getattr(self, k) for k in ("cpus", "mem", "time", "gpus") if getattr(self, k) is not None}
        part = self.partition.get(site) if isinstance(self.partition, dict) and site else \
            self.partition if isinstance(self.partition, str) else None
        if part:
            out["partition"] = part
        return out


@dataclass
class Option:
    name: str
    label: str
    choices: list


@dataclass
class Template:
    program: str
    modules: list
    run: str
    sites: list = field(default_factory=list)
    default: list = field(default_factory=list)
    inputs: list = field(default_factory=list)
    folder: str | None = None
    setup: list = field(default_factory=list)
    after: list = field(default_factory=list)
    outputs: list = field(default_factory=list)
    run_for: dict = field(default_factory=dict)
    cpus: int = 1
    mem: str = "4G"
    time: str = "1:00:00"
    gpus: int = 0
    partition: str | dict | None = None
    parallel: str = "threads"
    threads_per_task: int | None = None
    omp: bool = False
    sbatch: list = field(default_factory=list)
    options: list = field(default_factory=list)

    def offered_at(self, site: str) -> bool:
        return not self.sites or site in self.sites

    def partition_at(self, site: str | None) -> str | None:
        if isinstance(self.partition, dict):
            return self.partition.get(site) if site else None
        return self.partition

    def to_dict(self, site: str | None = None) -> dict:
        return {"program": self.program, "modules": self.modules, "default": self.default,
                "inputs": self.inputs, "folder": self.folder, "cpus": self.cpus, "mem": self.mem,
                "time": self.time, "gpus": self.gpus, "partition": self.partition, "parallel": self.parallel,
                "options": [{"name": o.name, "label": o.label,
                             "choices": [dict(label=c.label, **c.sizes(site),
                                              **({"parallel": c.parallel} if c.parallel else {}))
                                         for c in o.choices]} for o in self.options]}


def _line(text, what: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise TemplateError(f"{what} isn't a line of text")
    if "\n" in text or "\r" in text or len(text) > MAX_LINE:
        raise TemplateError(f"{what} must be one line, at most {MAX_LINE} characters")
    if _UNSEEN.search(text):
        raise TemplateError(f"{what} holds a control character, a line separator or an invisible mark")
    unknown = sorted(set(_ANY_PLACE.findall(text)) - set(PLACES))
    if unknown:
        raise TemplateError(f"{what}: {{{unknown[0]}}} isn't a place nomad fills ({', '.join(PLACES)}); "
                            "a shell variable is written ${NAME}")
    return text


def _lines(value, what: str) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or len(value) > MAX_LINES:
        raise TemplateError(f"{what} must be a list of at most {MAX_LINES} lines")
    return [_line(v, f"{what} line {i + 1}") for i, v in enumerate(value)]


def _sizes(d: dict, where: str, base: dict | None = None) -> dict:
    """cpus, gpus, mem, time, partition as given in ``d`` (checked); with
    ``base``, the template's defaults for those not given."""
    out = dict(base or {})
    try:
        if "cpus" in d or base is not None:
            out["cpus"] = int(d.get("cpus", (base or {}).get("cpus", 1)))
        if "gpus" in d or base is not None:
            out["gpus"] = int(d.get("gpus", (base or {}).get("gpus", 0)))
    except (TypeError, ValueError):
        raise TemplateError(f"{where}: cpus and gpus are whole numbers") from None
    if "cpus" in out and not 1 <= out["cpus"] <= 4096 or "gpus" in out and not 0 <= out["gpus"] <= 64:
        raise TemplateError(f"{where}: cpus 1 to 4096, gpus 0 to 64")
    if "mem" in d or base is not None:
        out["mem"] = str(d.get("mem", (base or {}).get("mem", "4G")))
        if not parse_memory_mb(out["mem"]):
            raise TemplateError(f"{where}: mem {out['mem']!r} isn't a size (\"16G\")")
    if "time" in d or base is not None:
        out["time"] = str(d.get("time", (base or {}).get("time", "1:00:00"))).strip()
        if not _TIME.fullmatch(out["time"]) or not parse_time_seconds(out["time"]):
            raise TemplateError(f"{where}: time {out['time']!r} isn't a time (\"1-00:00:00\")")
    if "partition" in d:
        partition = d.get("partition")
        parts = list(partition.values()) if isinstance(partition, dict) else [partition]
        if not all(isinstance(x, str) and _PARTITION.fullmatch(x) for x in parts):
            raise TemplateError(f"{where}: partition {partition!r} isn't a partition's name (or one a site)")
        out["partition"] = partition
    return out


def _parallel(d: dict, where: str, given_only: bool) -> dict:
    """parallel, threads_per_task, omp, as given (checked)."""
    out = {}
    if "parallel" in d or not given_only:
        parallel = d.get("parallel", "threads")
        if parallel not in PARALLEL:
            raise TemplateError(f"{where}: parallel is \"threads\" (--cpus-per-task), \"mpi\" (--ntasks) or "
                                "\"hybrid\" (processes of threads_per_task threads)")
        out["parallel"] = parallel
    if "threads_per_task" in d:
        tpt = d.get("threads_per_task")
        if not isinstance(tpt, int) or isinstance(tpt, bool) or not 1 <= tpt <= 256:
            raise TemplateError(f"{where}: threads_per_task is a whole number, 1 to 256")
        out["threads_per_task"] = tpt
    if out.get("parallel") == "hybrid" and "threads_per_task" not in out:
        raise TemplateError(f"{where}: parallel = \"hybrid\" needs threads_per_task")
    if "omp" in d:
        if not isinstance(d["omp"], bool):
            raise TemplateError(f"{where}: omp is true or false")
        out["omp"] = d["omp"]
    return out


def _sbatch_lines(value, where: str) -> list:
    from nomad.submit.spec import _SBATCH_LONG, _long_name
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or len(value) > 8 or not all(isinstance(x, str) and _SBATCH.fullmatch(x)
                                                                  for x in value):
        raise TemplateError(f"{where}: sbatch is a list of at most 8 options (\"--constraint=a100\"), no spaces")
    out = []
    for x in value:
        given = _SBATCH.fullmatch(x).group(1)
        name = _long_name(given)                  # sbatch takes any prefix that is one option's: --part
        if name not in _SBATCH_LONG:
            raise TemplateError(f"{where}: sbatch {x}: --{given} isn't one of sbatch's options")
        if name in _SBATCH_OWN:
            raise TemplateError(f"{where}: sbatch {x}: --{name} is nomad's to write, or breaks the script it "
                                "writes (give the size as cpus, mem, time, gpus or partition)")
        out.append("--" + name + x[2 + len(given):])
    return out


_CHOICE_KEYS = {"label", "run", "setup", "after", "outputs", "sbatch", "parallel", "threads_per_task", "omp",
                "cpus", "mem", "time", "gpus", "partition"}


def _options(value, where: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_OPTIONS:
        raise TemplateError(f"{where}: options is a list of at most {MAX_OPTIONS} ([[submit.templates.options]])")
    out, names = [], set()
    for i, o in enumerate(value):
        w = f"{where}: option {i + 1}"
        if not isinstance(o, dict):
            raise TemplateError(f"{w} isn't a table")
        name = o.get("name")
        if not isinstance(name, str) or not _OPTION.fullmatch(name) or name in names:
            raise TemplateError(f"{w}: name {name!r} is a short key (letters, digits, _), once a template")
        names.add(name)
        w = f"{where}: option {name}"
        label = o.get("label", name)
        if not isinstance(label, str) or not 0 < len(label.strip()) <= 80 or _UNSEEN.search(label) or "\t" in label:
            raise TemplateError(f"{w}: label is one line, at most 80 characters")
        raw = o.get("choices")
        if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_CHOICES:
            raise TemplateError(f"{w}: two to {MAX_CHOICES} choices ([[submit.templates.options.choices]])")
        choices, labels = [], set()
        for j, c in enumerate(raw):
            cw = f"{w}, choice {j + 1}"
            if not isinstance(c, dict):
                raise TemplateError(f"{cw} isn't a table")
            unknown = sorted(set(c) - _CHOICE_KEYS)
            if unknown:
                raise TemplateError(f"{cw}: {unknown[0]} isn't something a choice sets "
                                    f"({', '.join(sorted(_CHOICE_KEYS))})")
            cl = c.get("label")
            if not isinstance(cl, str) or not 0 < len(cl.strip()) <= 80 or _UNSEEN.search(cl) or "\t" in cl \
                    or cl.lower() in labels or re.fullmatch(r"[0-9]+", cl.strip()):
                raise TemplateError(f"{cw}: label is one line, at most 80 characters, not a number alone (the "
                                    "choices are picked by number too), each choice its own (whatever the case)")
            labels.add(cl.lower())
            cw = f"{w}, choice {cl!r}"
            par = _parallel(c, cw, given_only=True)
            choices.append(Choice(label=cl, run=_line(c["run"], f"{cw}: run") if "run" in c else None,
                                  setup=_lines(c.get("setup"), f"{cw}: setup"),
                                  after=_lines(c.get("after"), f"{cw}: after"),
                                  outputs=_lines(c.get("outputs"), f"{cw}: outputs"),
                                  sbatch=_sbatch_lines(c.get("sbatch"), cw), **par, **_sizes(c, cw)))
        out.append(Option(name=name, label=label, choices=choices))
    for what, keys in (("run", ("run",)), ("how it runs in parallel", ("parallel", "threads_per_task"))):
        setting = [o.name for o in out if any(getattr(c, k) is not None for c in o.choices for k in keys)]
        if len(setting) > 1:
            raise TemplateError(f"{where}: options {' and '.join(setting)} both set {what}: one option may")
    return out


def _combinations(t: Template) -> None:
    """Every way the options can be chosen must make a script: threads a
    process only with parallel = "hybrid", and CPUs a multiple of them."""
    where = f"the {t.program} template"
    if t.threads_per_task is not None and t.parallel != "hybrid":
        raise TemplateError(f"{where}: threads_per_task is for parallel = \"hybrid\"")
    if t.run_for and any(c.run is not None for o in t.options for c in o.choices):
        raise TemplateError(f"{where}: run_for (a version's own run line) and an option's run line: which would "
                            "run? Give each version its line in the choices instead, or no run in them")
    for o in t.options:
        for c in o.choices:
            if c.threads_per_task is not None and (c.parallel or t.parallel) != "hybrid":
                raise TemplateError(f"{where}: option {o.name}, choice {c.label!r}: threads_per_task is for "
                                    "parallel = \"hybrid\"")
    for combo in itertools.product(*(o.choices for o in t.options)):
        parallel, tpt, cpus = t.parallel, t.threads_per_task, t.cpus
        for c in combo:
            parallel = c.parallel or parallel
            tpt = c.threads_per_task if c.threads_per_task is not None else tpt
            cpus = c.cpus if c.cpus is not None else cpus
        if parallel == "hybrid" and (not tpt or cpus % tpt or cpus < tpt):
            said = "; ".join(f"{o.label}: {c.label}" for o, c in zip(t.options, combo, strict=True))
            raise TemplateError(f"{where}: {said or 'as it is'}: {cpus} CPUs aren't processes of "
                                f"{tpt or '?'} threads (a multiple of {tpt or 'threads_per_task'})")


def template(d: dict) -> Template:
    """One [[submit.templates]] table, checked; else TemplateError."""
    if not isinstance(d, dict):
        raise TemplateError("a [[submit.templates]] entry isn't a table")
    program = d.get("program")
    if not isinstance(program, str) or not _PROGRAM.fullmatch(program):
        raise TemplateError(f"program {program!r} isn't a program's name")
    where = f"the {program} template"
    modules = d.get("modules") or []
    if isinstance(modules, str):
        modules = [modules]
    if not modules or not all(isinstance(m, str) and _MODULE.fullmatch(m) for m in modules):
        raise TemplateError(f"{where}: modules must name its modules (\"gaussian/*\")")
    default = d.get("default") or []
    if isinstance(default, str):
        default = [default]
    if not isinstance(default, list) or not all(isinstance(x, str) and _MODULE.fullmatch(x) for x in default):
        raise TemplateError(f"{where}: default {default!r} isn't a module's name (or a list of them)")
    inputs = d.get("inputs") or []
    if isinstance(inputs, str):
        inputs = [inputs]
    if not all(isinstance(e, str) and _ENDING.fullmatch(e) for e in inputs):
        raise TemplateError(f"{where}: inputs are file endings (\".gjf\")")
    folder = d.get("folder")
    if folder is not None and not (isinstance(folder, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}",
                                                                       folder)):
        raise TemplateError(f"{where}: folder names the file a folder of inputs holds (\"control.run\")")
    if bool(inputs) == bool(folder):
        raise TemplateError(f"{where}: give inputs (file endings) or folder (a file it holds), one of them")
    run_for = d.get("run_for") or {}
    if not isinstance(run_for, dict) or not all(isinstance(k, str) and _MODULE.fullmatch(k) for k in run_for):
        raise TemplateError(f"{where}: run_for maps module patterns to run lines")
    sites = d.get("sites") or []
    if isinstance(sites, str):
        sites = [sites]
    if not isinstance(sites, list) or not all(isinstance(s, str) and s for s in sites):
        raise TemplateError(f"{where}: sites is a list of site names")
    sizes = _sizes(d, where, base={})
    if d.get("partition") is None:
        sizes["partition"] = None
    par = _parallel(d, where, given_only=False)
    t = Template(program=program, modules=list(modules), run=_line(d.get("run"), f"{where}: run"),
                    sites=list(sites), default=default, inputs=list(inputs), folder=folder,
                    setup=_lines(d.get("setup"), f"{where}: setup"), after=_lines(d.get("after"), f"{where}: after"),
                    outputs=_lines(d.get("outputs"), f"{where}: outputs"),
                    run_for={k: _line(v, f"{where}: run_for {k}") for k, v in run_for.items()},
                 sbatch=_sbatch_lines(d.get("sbatch"), where), options=_options(d.get("options"), where),
                 **sizes, **par)
    _combinations(t)
    return t


def templates(config: dict | None, site: str | None = None) -> tuple[list, list]:
    """(the templates offered at site (all, without site), what is wrong with
    the others): one bad template doesn't take the rest away."""
    sub = (config or {}).get("submit") if isinstance((config or {}).get("submit"), dict) else {}
    raw = sub.get("templates") or []
    out, problems = [], []
    for i, d in enumerate(raw if isinstance(raw, list) else [raw]):
        try:
            t = template(d)
        except TemplateError as e:
            problems.append(f"[[submit.templates]] #{i + 1}: {e}")
            continue
        if site is None or t.offered_at(site):
            out.append(t)
    # one program may have a template a site (CP2K: mpirun at one, srun at another); two offered at
    # the same site are one too many
    def overlap(a, b):
        return not a.sites or not b.sites or bool(set(a.sites) & set(b.sites))
    unique, said = [], set()
    for t in out:
        first = next((u for u in unique if u.program == t.program and overlap(u, t)), None)
        if first is None:
            unique.append(t)
        elif t.program not in said:
            said.add(t.program)
            problems.append(f"two templates for {t.program}{f' at {site}' if site else ' at the same site'}: "
                            "the first is used")
    return unique, problems


def site_settings(config: dict | None, site: str) -> dict:
    """[submit.build.SITE]: module_paths (module use) and scratch, checked."""
    sub = (config or {}).get("submit") if isinstance((config or {}).get("submit"), dict) else {}
    build = sub.get("build") if isinstance(sub.get("build"), dict) else {}
    mine = build.get(site) if isinstance(build.get(site), dict) else {}
    problems = []
    given = mine.get("module_paths") or []
    paths = []
    for p in (given if isinstance(given, list) else [given]):
        if isinstance(p, str) and _MODULE_PATH.fullmatch(p):
            paths.append(p)
        else:
            problems.append(f"[submit.build.{site}] module_paths: {p!r} isn't a folder's full path "
                            "(letters, digits, . _ / + -; no $)")
    scratch = mine.get("scratch")
    if scratch is not None and not (isinstance(scratch, str) and _PATH.fullmatch(scratch)
                                    and _JOB_ID.search(scratch)):
        # a template's "rm -rf {scratch}" must take only the job's own folder
        problems.append(f"[submit.build.{site}] scratch: {scratch!r} must be a full path naming the job "
                        "($SLURM_JOB_ID): /tmp/$USER/$SLURM_JOB_ID is used instead")
        scratch = None
    return {"module_paths": paths, "scratch": scratch, "name": site, "problems": problems}


# ── versions ──────────────────────────────────────────────────────────────

def _natural(name: str) -> list:
    return [(0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in re.split(r"(\d+)", name)]


def versions(t: Template, available: list) -> tuple[list, str | None]:
    """(the cluster's modules this template offers, the one to choose first,
    then the rest in name order; that one): the template's default when the
    cluster has it (the first of its list there), else one the cluster marks
    as its default, else the highest-looking name (qchem611 over qchem54 by
    that count, so a template names its default). available: the cluster's
    module names, as the modules collector read them, each a name or
    (name, is_default)."""
    names, marked = [], set()
    for a in available or []:
        name, mark = (a, False) if isinstance(a, str) else (a[0], bool(a[1]))
        if not name or name.endswith("/") or _ASIDE.search(name):
            continue
        if any(fnmatch.fnmatchcase(name, p) for p in t.modules) and name not in names:
            names.append(name)
            if mark:
                marked.add(name)
    if not names:
        return [], None
    names.sort()
    first = next((w for w in t.default if w in names), None) \
        or next((n for n in names if n in marked), None) or max(names, key=_natural)
    return [first] + [n for n in names if n != first], first


def run_line(t: Template, module: str) -> str:
    for pattern, line in t.run_for.items():
        if fnmatch.fnmatchcase(module, pattern):
            return line
    return t.run


def picked(t: Template, choose: dict | None) -> list:
    """[(option, choice)] for each of the template's options: the one asked
    for in ``choose`` ({name: its number, from 1, or its label}), else the
    first. TemplateError for an option or choice the template hasn't."""
    choose = dict(choose or {})
    names = {o.name for o in t.options}
    for k in choose:
        if k not in names:
            raise TemplateError(f"the {t.program} template has no option {k!r}"
                                + (f" ({', '.join(sorted(names))})" if names else ""))
    out = []
    for o in t.options:
        want = choose.get(o.name)
        if want is None:
            out.append((o, o.choices[0]))
            continue
        if isinstance(want, str) and re.fullmatch(r"[0-9]{1,3}", want.strip()):
            want = int(want)
        if isinstance(want, int) and not isinstance(want, bool) and 1 <= want <= len(o.choices):
            out.append((o, o.choices[want - 1]))
            continue
        c = next((c for c in o.choices if isinstance(want, str) and c.label.lower() == want.lower()), None)
        if c is None:
            raise TemplateError(f"{o.label}: {want!r} isn't one of its choices (1 to {len(o.choices)}: "
                                + "; ".join(c.label for c in o.choices) + ")")
        out.append((o, c))
    return out


def _effective(t: Template, module: str, chosen: list, site: str | None = None) -> dict:
    """What the template and its chosen choices make: run, setup, after,
    outputs, sbatch, parallel, threads_per_task, omp, and the sizes."""
    e = {"run": run_line(t, module), "setup": list(t.setup), "after": list(t.after), "outputs": list(t.outputs),
         "sbatch": list(t.sbatch), "parallel": t.parallel, "threads_per_task": t.threads_per_task, "omp": t.omp,
         "cpus": t.cpus, "mem": t.mem, "time": t.time, "gpus": t.gpus, "partition": t.partition}
    for _o, c in chosen:
        if c.run is not None:
            e["run"] = c.run
        e["setup"] += c.setup
        e["after"] = c.after + e["after"]          # the template's own clean-up last
        e["outputs"] += [x for x in c.outputs if x not in e["outputs"]]
        e["sbatch"] += [x for x in c.sbatch if x not in e["sbatch"]]
        for k in ("parallel", "threads_per_task", "omp", "cpus", "mem", "time", "gpus"):
            if getattr(c, k) is not None:
                e[k] = getattr(c, k)
        if isinstance(c.partition, str):
            e["partition"] = c.partition
        elif isinstance(c.partition, dict) and site and site in c.partition:
            e["partition"] = c.partition[site]        # only where it names that site
    return e


# ── the script ────────────────────────────────────────────────────────────

def _fill(line: str, values: dict) -> str:
    return _PLACE.sub(lambda m: values[m.group(1)], line)


def _stem(name: str) -> str:
    base = posixpath.basename(name.rstrip("/"))
    stem, dot, ext = base.rpartition(".")
    return stem if dot and stem else base


def _names(t: Template, input_path: str) -> tuple[str, str, str, str]:
    """(the input's path, the folder the job runs in, the input's name there,
    the stem outputs are named by): a file template's input is the file; a
    folder template's is the folder, its input the file it holds."""
    path = posixpath.normpath(input_path)
    if t.folder:
        return path, path, t.folder, _stem(posixpath.basename(path))
    return path, posixpath.dirname(path), posixpath.basename(path), _stem(posixpath.basename(path))


def _word(name: str) -> str:
    """A file name as one shell word, never taken for an option (-x.inp -> ./-x.inp)."""
    return shlex.quote("./" + name if name.startswith("-") else name)


def job_name(stem: str) -> str:
    """A job name Slurm and the shell take as it is: letters, digits, . _ -."""
    return (re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_.-") or "job")[:64]


def render(t: Template, *, module: str, input_path: str, site: dict | None = None, cpus: int | None = None,
           mem: str | None = None, time: str | None = None, gpus: int | None = None,
           partition: str | None = None, when: datetime | None = None, choose: dict | None = None) -> str:
    """The script, as text. input_path: the input file (or, with a folder
    template, the folder of inputs), absolute. Values not given: the chosen
    choices', else the template's. ``choose``: {option: choice} (picked).
    Raises TemplateError for an input the template doesn't take, a module it
    doesn't offer, or a choice it hasn't."""
    if not isinstance(input_path, str) or not _PERSON_PATH.fullmatch(input_path) \
            or any(part in (".", "..") for part in input_path.split("/")[1:]):
        # the gate's own rule: no control characters (a carriage return would end a line once
        # the text is edited or pasted), no line separators, no invisible or direction marks
        raise TemplateError("the input is a full path, without control characters or . and .. parts")
    if not any(fnmatch.fnmatchcase(module, p) for p in t.modules) or not _MODULE.fullmatch(module) \
            or any(c in module for c in "*?["):
        raise TemplateError(f"{module} isn't one of the {t.program} template's modules")
    path, folder, name, stem = _names(t, input_path)
    if not t.folder and not any(name.lower().endswith(e.lower()) for e in t.inputs):
        raise TemplateError(f"{name} isn't a {t.program} input ({', '.join(t.inputs)})")
    chosen = picked(t, choose)
    site = site or {}
    e = _effective(t, module, chosen, site.get("name"))
    cpus = int(cpus or e["cpus"])
    mem = mem or e["mem"]
    mem_mb = parse_memory_mb(mem)
    if not mem_mb:
        raise TemplateError(f"memory {mem!r} isn't a size")
    time = str(time or e["time"]).strip()
    if not _TIME.fullmatch(time) or not parse_time_seconds(time):          # 0: no limit at all
        raise TemplateError(f"time {time!r} isn't a time (\"1-00:00:00\")")
    gpus = e["gpus"] if gpus is None else int(gpus)
    where = e["partition"]
    partition = partition or (where.get(site.get("name")) if isinstance(where, dict) else where)
    parallel = e["parallel"]
    if parallel == "threads":
        tasks, threads = 1, cpus
    elif parallel == "mpi":
        tasks, threads = cpus, 1
    else:
        threads = int(e["threads_per_task"] or 1)
        if cpus % threads or cpus < threads:
            raise TemplateError(f"with {threads} threads a process, the CPUs are a multiple of {threads} "
                                f"({cpus} isn't)")
        tasks = cpus // threads
    scratch = site.get("scratch") or "/tmp/$USER/$SLURM_JOB_ID"
    # The counts as the job has them when it runs: the size is the person's to change when it is sent
    # (nomad size, the gate's sizing: on sbatch's command line, over these #SBATCH lines), and a line
    # that said 16 processes where the job has 8 would fail ("not enough slots"). The numbers written
    # here are what the script asks for, used only where Slurm says nothing.
    n_tasks = f"${{SLURM_NTASKS:-{tasks}}}"
    n_threads = f"${{SLURM_CPUS_PER_TASK:-{threads}}}"
    if parallel == "threads":
        p_tasks, p_threads, p_cpus = "1", n_threads, n_threads
    elif parallel == "mpi":
        p_tasks, p_threads, p_cpus = n_tasks, "1", n_tasks
    else:
        p_tasks, p_threads, p_cpus = n_tasks, n_threads, f"$(( {n_tasks} * {n_threads} ))"
    values = {"input": _word(name), "stem": _word(stem), "folder": shlex.quote(folder),
              "cpus": p_cpus, "tasks": p_tasks, "threads": p_threads, "mem_mb": str(mem_mb),
              # each process's share: one process with threads has it all
              "mem_mb_each": str(max(1, int(mem_mb * 0.9) // tasks)),
              "gpus": str(gpus), "module": module, "scratch": f'"{scratch}"'}
    when = when or datetime.now()
    lines = ["#!/bin/bash", f"#SBATCH --job-name={job_name(stem)}"]
    if parallel == "threads":
        lines.append(f"#SBATCH --cpus-per-task={cpus}")
    else:
        lines.append(f"#SBATCH --ntasks={tasks}")
        if parallel == "hybrid":
            lines.append(f"#SBATCH --cpus-per-task={threads}")
        lines.append("#SBATCH --nodes=1")
    lines += [f"#SBATCH --mem={slurm_mem(mem_mb)}", f"#SBATCH --time={time}"]
    if gpus:
        lines.append(f"#SBATCH --gres=gpu:{gpus}")
    if partition:
        lines.append(f"#SBATCH --partition={partition}")
    lines += [f"#SBATCH {x}" for x in e["sbatch"]]
    # --open-mode=append: a job Slurm requeues (a condo's owners took its node) keeps what it printed
    lines += [f"#SBATCH --output={job_name(stem)}-%j.out",
              "#SBATCH --open-mode=append",
              "",
              f"# Made by nomad from the {t.program} template ({module}) on {when:%Y-%m-%d}, for "
              f"{shlex.quote(posixpath.basename(path))}."]
    for o, c in chosen:
        lines.append(f"# {o.label}: {c.label}")
    lines += ["# Change anything: it is yours. Checked when you send it.", ""]
    body = [*e["setup"], e["run"], *e["after"]]
    if any("{scratch}" in x for x in body):
        # its scratch folder is named by the job: run by hand (bash water.sh), "rm -rf {scratch}"
        # would take /scratch/$USER/ itself
        # (an if, not ${SLURM_JOB_ID:?}: a script sourced in a terminal goes on past a failed expansion)
        lines.append('if [ -z "${SLURM_JOB_ID:-}" ]; then echo "send this with sbatch: its scratch folder is named '
                     'by the job" >&2; return 1 2>/dev/null; exit 1; fi')
    lines.append(f"cd {values['folder']} || exit 1")
    lines += [f"module use {shlex.quote(p)}" for p in site.get("module_paths") or []]
    lines.append(f"module load {module} || exit 1")
    def sets(name):                               # a line of its own gives it (not one that only reads it)
        at = r"(?:^\s*|[;&|(]\s*|\bexport\s+(?:-\w+\s+)?|\benv\s+(?:\S+=\S*\s+)*)"
        return any(re.search(at + name + "=", x) for x in body)
    # each process's threads as the job has them (they follow --cpus-per-task when it is changed: by
    # nomad size, or by hand); MPI processes one thread each
    per_task = "1" if parallel == "mpi" else "${SLURM_CPUS_PER_TASK:-" + str(threads) + "}"
    if e["omp"] and not sets("OMP_NUM_THREADS"):
        lines.append(f"export OMP_NUM_THREADS={per_task}")
    if parallel != "mpi" and threads > 1 and any(re.search(r"\bsrun\b", x) for x in body) \
            and not sets("SRUN_CPUS_PER_TASK"):
        # srun doesn't take the job's --cpus-per-task by itself (Slurm 22.05 on): its steps would get one CPU
        lines.append(f"export SRUN_CPUS_PER_TASK={per_task}")
    lines += [_fill(x, values) for x in e["setup"]]
    lines.append(_fill(e["run"], values))
    if e["after"]:
        lines.append("status=$?")
        lines += [_fill(x, values) for x in e["after"]]
        lines.append("exit $status")
    return "\n".join(lines) + "\n"


def script_name(t: Template, input_path: str) -> str:
    """Where the script is saved: beside the input (water.gjf -> water.sh;
    a folder of inputs -> run.sh in it)."""
    path = posixpath.normpath(input_path)
    if t.folder:
        return posixpath.join(path, "run.sh")
    return posixpath.join(posixpath.dirname(path), job_name(_stem(path)) + ".sh")


def outputs(t: Template, input_path: str, choose: dict | None = None) -> list:
    """The files the template's outputs (and the chosen choices') name, for
    this input (to say which would be written over)."""
    _path, _folder, name, stem = _names(t, input_path)
    plain = dict.fromkeys(PLACES, "")
    plain.update(input=name, stem=stem)
    names = list(t.outputs)
    for _o, c in picked(t, choose):
        names += [x for x in c.outputs if x not in names]
    return [_fill(o, plain) for o in names]


# ── from a past script ────────────────────────────────────────────────────

def swap_input(text: str, old_input: str, new_input: str) -> tuple[str, list]:
    """A past script of the person's for another input: the old input's name
    replaced in its command lines (as a whole word), and the files named
    after it (h2o.log -> water.log, the job's name). (text, what changed);
    TemplateError when the old name isn't in it."""
    old, new = posixpath.basename(old_input), posixpath.basename(new_input)
    if not old or not new or "/" in new:
        raise TemplateError("the inputs are file names")
    if shlex.quote(new) != new or new.startswith("-"):
        # put in the old one's place as it is: a name the shell would split, or take as an option, isn't
        raise TemplateError(f"{new!r} needs quoting in a script: rename it (letters, digits, . _ + -) "
                            "or edit a copy yourself")
    old_stem, new_stem = _stem(old), _stem(new)
    word = re.compile(r"(?<![A-Za-z0-9._/-])" + re.escape(old) + r"(?![A-Za-z0-9._-])")
    stem_word = re.compile(r"(?<![A-Za-z0-9._/-])" + re.escape(old_stem) + r"(?=\.[A-Za-z0-9]{1,8}\b)")
    out, changed, found = [], [], False
    for n, line in enumerate(text.split("\n"), 1):
        body = line
        if line.lstrip().startswith("#SBATCH"):
            body = re.sub(r"((?:--job-name|-J)[= ])" + re.escape(old_stem) + r"\b", r"\g<1>" + job_name(new_stem),
                          line)
        elif not line.lstrip().startswith("#"):
            body = word.sub(new.replace("\\", "\\\\"), line)
            found = found or body != line
            body = stem_word.sub(new_stem.replace("\\", "\\\\"), body)
        if body != line:
            changed.append({"line": n, "was": line, "now": body})
        out.append(body)
    if not found:
        raise TemplateError(f"{old} isn't named in that script's commands, so nomad can't tell where the input "
                            "goes: edit a copy yourself")
    return "\n".join(out), changed


# ── what the Console asks ────────────────────────────────────────────────

def offer(config: dict | None, site: str, available: list) -> dict:
    """What a site offers: each template with the versions the cluster has
    and the one to choose first; templates without any version there are
    said (the module list not read yet, or a pattern that matches nothing)."""
    got, problems = templates(config, site)
    out = []
    for t in got:
        names, first = versions(t, available)
        if not names:
            problems.append(f"{t.program}: none of {', '.join(t.modules)} is among {site}'s modules"
                            + ("" if available else " (its module list isn't read yet: [collectors.modules])"))
            continue
        out.append(dict(t.to_dict(site), partition=t.partition_at(site), versions=names, choose=first))
    settings = site_settings(config, site)
    return {"site": site, "programs": out, "problems": problems + settings["problems"], "settings": settings}


def make(config: dict | None, site: str, program: str, *, module: str, input_path: str, available: list,
         cpus: int | None = None, mem: str | None = None, time: str | None = None, gpus: int | None = None,
         partition: str | None = None, choose: dict | None = None) -> dict:
    """The script for one program's template at a site: {text, path (where
    it is saved), outputs (files it writes), chosen ({option: choice's
    label})}; TemplateError when the program, version, input or a choice
    isn't one the site offers."""
    got, _problems = templates(config, site)
    t = next((x for x in got if x.program == program), None)
    if t is None:
        raise TemplateError(f"{site} has no template for {program}")
    names, _first = versions(t, available)
    if module not in names:
        raise TemplateError(f"{module} isn't one of {site}'s {program} modules ({', '.join(names) or 'none'})")
    text = render(t, module=module, input_path=input_path, site=site_settings(config, site), cpus=cpus, mem=mem,
                  time=time, gpus=gpus, partition=partition, choose=choose)
    return {"text": text, "path": script_name(t, input_path), "outputs": outputs(t, input_path, choose),
            "program": program, "module": module,
            "chosen": {o.name: c.label for o, c in picked(t, choose)}}


def check_all(config: dict | None, site: str, available: list, limit: int = 2000) -> tuple[int, list]:
    """(scripts made, what failed): each template the site offers, with the
    version chosen first, for every way its options can be chosen, for a
    made-up input -- what a template's load can't see (a size a choice and
    a site's settings make together). Templates without a version there are
    left to offer()'s problems."""
    got, problems = templates(config, site)
    settings = site_settings(config, site)
    made, failed = 0, [x for x in problems if not x.startswith("two templates for")]
    failed += [x for x in problems if x.startswith("two templates for")]
    for t in got:
        names, first = versions(t, available)
        if not names:
            continue
        sample = "/home/nomad-check/check" + (t.inputs[0] if t.inputs else "")
        for combo in itertools.product(*([(o, c) for c in o.choices] for o in t.options)):
            if made + len(failed) >= limit:
                return made, failed + [f"more than {limit} scripts: the rest not made"]
            choose = {o.name: o.choices.index(c) + 1 for o, c in combo}
            try:
                render(t, module=first, input_path=sample, site=settings, choose=choose)
            except TemplateError as e:
                said = ", ".join(f"{o.label}: {c.label}" for o, c in combo)
                failed.append(f"{t.program} ({first}{'; ' + said if said else ''}): {e}")
                continue
            made += 1
    return made, failed


__all__ = ["Template", "Option", "Choice", "TemplateError", "check_all", "template", "templates", "site_settings", "versions",
           "run_line", "picked", "render", "script_name", "outputs", "swap_input", "job_name", "PLACES", "offer",
           "make"]

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

In a template's lines, ``{input}`` (the input file's name, quoted for the
shell), ``{stem}`` (its name without the ending), ``{folder}`` (where the job
runs), ``{cpus}``, ``{mem_mb}`` (the job's memory, MB), ``{mem_mb_each}`` (each
process's share of nine tenths of it, for programs told their memory per
process: Columbus's runc -m), ``{gpus}``, ``{module}``, ``{scratch}``;
anything else in braces is left as it is (``${SLURM_CPUS_PER_TASK}``).

Nothing here runs anything or reads a file: the script is text, shown to the
person in full, and saved (as them, with the editor's safeguards) only when
they say so; then it is sized, checked and sent as any other.
"""
from __future__ import annotations

import fnmatch
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime

from nomad.submit.gate import _PATH as _PERSON_PATH
from nomad.submit.spec import parse_memory_mb, parse_time_seconds, slurm_mem

PLACES = ("input", "stem", "folder", "cpus", "mem_mb", "mem_mb_each", "gpus", "module", "scratch")
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


class TemplateError(ValueError):
    """A template in nomad.toml that can't be used: said, never guessed at."""


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

    def offered_at(self, site: str) -> bool:
        return not self.sites or site in self.sites

    def partition_at(self, site: str | None) -> str | None:
        if isinstance(self.partition, dict):
            return self.partition.get(site) if site else None
        return self.partition

    def to_dict(self) -> dict:
        return {"program": self.program, "modules": self.modules, "default": self.default,
                "inputs": self.inputs, "folder": self.folder, "cpus": self.cpus, "mem": self.mem,
                "time": self.time, "gpus": self.gpus, "partition": self.partition, "parallel": self.parallel}


def _line(text, what: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise TemplateError(f"{what} isn't a line of text")
    if "\n" in text or "\r" in text or len(text) > MAX_LINE:
        raise TemplateError(f"{what} must be one line, at most {MAX_LINE} characters")
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
    try:
        cpus = int(d.get("cpus", 1))
        gpus = int(d.get("gpus", 0))
    except (TypeError, ValueError):
        raise TemplateError(f"{where}: cpus and gpus are whole numbers") from None
    if not 1 <= cpus <= 4096 or not 0 <= gpus <= 64:
        raise TemplateError(f"{where}: cpus 1 to 4096, gpus 0 to 64")
    mem = str(d.get("mem", "4G"))
    if not parse_memory_mb(mem):
        raise TemplateError(f"{where}: mem {mem!r} isn't a size (\"16G\")")
    time = str(d.get("time", "1:00:00")).strip()
    if not _TIME.fullmatch(time) or not parse_time_seconds(time):
        raise TemplateError(f"{where}: time {time!r} isn't a time (\"1-00:00:00\")")
    partition = d.get("partition")
    parts = list(partition.values()) if isinstance(partition, dict) else [partition] if partition is not None else []
    if not all(isinstance(x, str) and _PARTITION.fullmatch(x) for x in parts):
        raise TemplateError(f"{where}: partition {partition!r} isn't a partition's name (or one a site)")
    parallel = d.get("parallel", "threads")
    if parallel not in ("threads", "mpi"):
        raise TemplateError(f"{where}: parallel is \"threads\" (--cpus-per-task) or \"mpi\" (--ntasks)")
    return Template(program=program, modules=list(modules), run=_line(d.get("run"), f"{where}: run"),
                    sites=list(sites), default=default, inputs=list(inputs), folder=folder,
                    setup=_lines(d.get("setup"), f"{where}: setup"), after=_lines(d.get("after"), f"{where}: after"),
                    outputs=_lines(d.get("outputs"), f"{where}: outputs"),
                    run_for={k: _line(v, f"{where}: run_for {k}") for k, v in run_for.items()},
                    cpus=cpus, mem=mem, time=time, gpus=gpus, partition=partition, parallel=parallel)


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
    names = [t.program for t in out]
    for name in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"two templates for {name}{f' at {site}' if site else ''}: the first is used")
    seen, unique = set(), []
    for t in out:
        if t.program not in seen:
            seen.add(t.program)
            unique.append(t)
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
           partition: str | None = None, when: datetime | None = None) -> str:
    """The script, as text. input_path: the input file (or, with a folder
    template, the folder of inputs), absolute. Values not given: the
    template's. Raises TemplateError for an input the template doesn't take
    or a module it doesn't offer."""
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
    cpus = int(cpus or t.cpus)
    mem = mem or t.mem
    mem_mb = parse_memory_mb(mem)
    if not mem_mb:
        raise TemplateError(f"memory {mem!r} isn't a size")
    time = str(time or t.time).strip()
    if not _TIME.fullmatch(time) or not parse_time_seconds(time):          # 0: no limit at all
        raise TemplateError(f"time {time!r} isn't a time (\"1-00:00:00\")")
    gpus = t.gpus if gpus is None else int(gpus)
    site = site or {}
    partition = partition or t.partition_at(site.get("name"))
    scratch = site.get("scratch") or "/tmp/$USER/$SLURM_JOB_ID"
    values = {"input": _word(name), "stem": _word(stem), "folder": shlex.quote(folder),
              "cpus": str(cpus), "mem_mb": str(mem_mb), "mem_mb_each": str(max(1, int(mem_mb * 0.9) // cpus)),
              "gpus": str(gpus), "module": module, "scratch": f'"{scratch}"'}
    when = when or datetime.now()
    lines = ["#!/bin/bash",
             f"#SBATCH --job-name={job_name(stem)}",
             f"#SBATCH --{'ntasks' if t.parallel == 'mpi' else 'cpus-per-task'}={cpus}"]
    if t.parallel == "mpi":
        lines.append("#SBATCH --nodes=1")
    lines += [f"#SBATCH --mem={slurm_mem(mem_mb)}", f"#SBATCH --time={time}"]
    if gpus:
        lines.append(f"#SBATCH --gres=gpu:{gpus}")
    if partition:
        lines.append(f"#SBATCH --partition={partition}")
    lines += [f"#SBATCH --output={job_name(stem)}-%j.out",
              "",
              f"# Made by nomad from the {t.program} template ({module}) on {when:%Y-%m-%d}, for "
              f"{shlex.quote(posixpath.basename(path))}.",
              "# Change anything: it is yours. Checked when you send it.",
              ""]
    if any("{scratch}" in x for x in [*t.setup, run_line(t, module), *t.after]):
        # its scratch folder is named by the job: run by hand (bash water.sh), "rm -rf {scratch}"
        # would take /scratch/$USER/ itself
        # (an if, not ${SLURM_JOB_ID:?}: a script sourced in a terminal goes on past a failed expansion)
        lines.append('if [ -z "${SLURM_JOB_ID:-}" ]; then echo "send this with sbatch: its scratch folder is named '
                     'by the job" >&2; return 1 2>/dev/null; exit 1; fi')
    lines.append(f"cd {values['folder']} || exit 1")
    lines += [f"module use {shlex.quote(p)}" for p in site.get("module_paths") or []]
    lines.append(f"module load {module} || exit 1")
    lines += [_fill(x, values) for x in t.setup]
    lines.append(_fill(run_line(t, module), values))
    if t.after:
        lines.append("status=$?")
        lines += [_fill(x, values) for x in t.after]
        lines.append("exit $status")
    return "\n".join(lines) + "\n"


def script_name(t: Template, input_path: str) -> str:
    """Where the script is saved: beside the input (water.gjf -> water.sh;
    a folder of inputs -> run.sh in it)."""
    path = posixpath.normpath(input_path)
    if t.folder:
        return posixpath.join(path, "run.sh")
    return posixpath.join(posixpath.dirname(path), job_name(_stem(path)) + ".sh")


def outputs(t: Template, input_path: str) -> list:
    """The files the template's outputs name, for this input (to say which
    would be written over)."""
    _path, _folder, name, stem = _names(t, input_path)
    plain = dict.fromkeys(PLACES, "")
    plain.update(input=name, stem=stem)
    return [_fill(o, plain) for o in t.outputs]


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
        out.append(dict(t.to_dict(), partition=t.partition_at(site), versions=names, choose=first))
    settings = site_settings(config, site)
    return {"site": site, "programs": out, "problems": problems + settings["problems"], "settings": settings}


def make(config: dict | None, site: str, program: str, *, module: str, input_path: str, available: list,
         cpus: int | None = None, mem: str | None = None, time: str | None = None, gpus: int | None = None,
         partition: str | None = None) -> dict:
    """The script for one program's template at a site: {text, path (where
    it is saved), outputs (files it writes)}; TemplateError when the program,
    version or input isn't one the site offers."""
    got, _problems = templates(config, site)
    t = next((x for x in got if x.program == program), None)
    if t is None:
        raise TemplateError(f"{site} has no template for {program}")
    names, _first = versions(t, available)
    if module not in names:
        raise TemplateError(f"{module} isn't one of {site}'s {program} modules ({', '.join(names) or 'none'})")
    text = render(t, module=module, input_path=input_path, site=site_settings(config, site), cpus=cpus, mem=mem,
                  time=time, gpus=gpus, partition=partition)
    return {"text": text, "path": script_name(t, input_path), "outputs": outputs(t, input_path),
            "program": program, "module": module}


__all__ = ["Template", "TemplateError", "template", "templates", "site_settings", "versions", "run_line",
           "render", "script_name", "outputs", "swap_input", "job_name", "PLACES", "offer", "make"]

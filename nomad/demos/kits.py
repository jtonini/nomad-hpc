# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Practice runs (1.34.0): kits of small input files, known to work, that
show a newcomer how a job runs on a cluster.

A practice run means nothing for anyone's research. It shows how a job is
sent, waits, starts and runs, what it writes, and where its time, memory
and cores are read. Its numbers are not results, and its size says nothing
about what a real calculation needs (that depends on the real input).

A kit is a folder holding its input files and a ``kit.toml``::

    name = "gaussian-benzene"
    program = "Gaussian"              # the [[submit.templates]] program it runs with
    title = "Benzene: optimise its shape, then its vibrations"
    about = "What it does and what to look at, in a few sentences."
    licence = "licensed"              # open | academic | licensed (needs a site licence)
    licence_note = "Gaussian runs only where your HPC has a Gaussian licence."
    source = "Written for nomad (AGPL-3.0)"
    input = "benzene.gjf"             # the template's input: one of files, or "." for the folder
    files = ["benzene.gjf"]           # what a copy holds
    cpus = 4                          # the practice run's own size: small and quick
    mem = "8G"
    time = "00:30:00"
    minutes = 3                       # about how long it runs
    # gpus = 1
    # choose = { engine = 2 }         # a choice of the template's options (its number, from 1)

    [[look]]                          # what to look at once it has run
    file = "benzene.log"              # a file in the folder (a pattern: "bootstrap-*.out")
    what = "How long it took: the CPU time over the elapsed time is about the cores it used"
    match = "Job cpu time|Elapsed time"

    [build]                           # files a program must make once, at a site
    module = "gromacs/2026.2-gpu"     # the module it is made with
    commands = ["gmx solvate ...", "gmx grompp ..."]
    makes = ["water.tpr"]             # among files: made by the commands

    [recorded]                        # one real run of it (1.35.0), to compare a new one with
    site = "spydur"
    date = "2026-11-09"               # text, not a TOML date
    module = "gromacs/2026.2-gpu"
    partition = "gpunodes"
    cpus = 4
    gpus = 1
    gpu = "NVIDIA A40"
    runtime_s = 88                    # start to end
    cpu_s = 342                       # CPU time, all cores
    cores_busy = 3.9                  # cpu_s over runtime_s
    peak_mb = 149                     # its largest memory
    files = ["recorded/water.log"]    # what it wrote, kept in the kit's recorded/ folder

The template writes the job script around the kit's input, as for anyone's
own input: a practice run runs the way real work will.

Kits come with nomad (nomad/demos/kits) and from a site's own folders::

    [submit.demos]
    dirs = ["/usr/local/share/nomad-demos"]   # folders of kit folders (a site's own, and those built there)
    bundled = true                            # also the kits that come with nomad (default)
    into = "~/nomad-demos"                    # where a copy goes: a new folder each time
    partition = { spydur = "condos" }         # where practice runs with no GPU are sent (default: the template's)
    gpu_partition = "gpunodes"                # and those with one (a name: every site)
    site_dirs = { spydur = "/usr/local/sw/nomad-demos" }   # the hub's: each site's own kits, for the Console
                                              # (there: root's or nomad's installer's, as is each folder
                                              # above it, and no one else may write; build leaves it so)

A site's kit of the same name is used instead of nomad's (a built one).
Copying never writes over anything; building runs a kit's commands as
whoever builds it, in a new folder.
"""
from __future__ import annotations

import os
import re
import shlex
import stat
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

BUNDLED = Path(__file__).parent / "kits"
LICENCES = {
    "open": "Open source",
    "academic": "Free for academic use, under the program's own licence",
    "licensed": "Needs a licence: it runs only where your HPC has one",
}
PRACTICE = ("A practice run: it shows how a job runs and what it writes. Its results mean nothing for your "
            "research, and its size says nothing about what your own calculations need.")
MAX_BYTES = 5 * 1024 * 1024
MAX_FILES = 40
MAX_LOOK = 8
MAX_COMMANDS = 12
README = "README-practice.txt"
DEFAULT_INTO = "~/nomad-demos"

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
_PROGRAM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,39}$")
_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LOOK_FILE = re.compile(r"^[A-Za-z0-9*?][A-Za-z0-9._*?/-]{0,127}$")
_MEM = re.compile(r"^[1-9][0-9]{0,4}[MG]$")
_TIME = re.compile(r"^(?:[0-9]{1,2}-)?[0-9]{1,2}:[0-5][0-9]:[0-5][0-9]$")
_MODULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+/-]{0,127}$")
_OPTION = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_PARTITION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_KEYS = {"name", "program", "title", "about", "licence", "licence_note", "source", "input", "files", "cpus",
         "mem", "time", "gpus", "minutes", "choose", "look", "build", "recorded"}
_LOOK_KEYS = {"file", "what", "match"}
_BUILD_KEYS = {"module", "commands", "makes"}
_RECORDED_KEYS = {"site", "date", "module", "partition", "cpus", "gpus", "gpu", "runtime_s", "cpu_s", "cores_busy",
                  "peak_mb", "files"}
_DATE = re.compile(r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$")
_RECORDED_FILE = re.compile(r"^recorded/(?:[A-Za-z0-9][A-Za-z0-9._-]{0,127}/){0,2}[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class KitError(ValueError):
    """A kit that can't be used, or a copy or build that can't be made: said."""


@dataclass(frozen=True)
class Kit:
    name: str
    program: str
    title: str
    about: str
    licence: str
    source: str
    input: str
    files: tuple
    cpus: int
    mem: str
    time: str
    minutes: int
    path: Path
    bundled: bool
    gpus: int = 0
    choose: dict = field(default_factory=dict)
    look: tuple = ()
    licence_note: str = ""
    build: dict | None = None
    recorded: dict | None = None
    recorded_from: Path | None = None          # the folder its recorded/ files are in (a built kit: nomad's)

    @property
    def ready(self) -> bool:
        """Every file a copy holds is there (a kit to build: once built)."""
        return all((self.path / f).is_file() and not (self.path / f).is_symlink() for f in self.files)

    def to_dict(self) -> dict:
        return {"name": self.name, "program": self.program, "title": self.title, "about": self.about,
                "licence": self.licence, "licence_text": LICENCES[self.licence], "licence_note": self.licence_note,
                "source": self.source, "input": self.input, "files": list(self.files), "cpus": self.cpus,
                "mem": self.mem, "time": self.time, "gpus": self.gpus, "minutes": self.minutes,
                "choose": dict(self.choose), "look": [dict(x) for x in self.look], "practice": PRACTICE,
                "ready": self.ready, "needs_building": bool(self.build) and not self.ready,
                "bundled": self.bundled, "recorded": dict(self.recorded) if self.recorded else None,
                "recorded_text": recorded_text(self)}


def _text(raw: dict, key: str, where: str, limit: int, required: bool = True) -> str:
    v = raw.get(key, "")
    if not isinstance(v, str) or (required and not v.strip()) or len(v) > limit:
        raise KitError(f"{where}: {key} should be text of at most {limit} characters")
    return " ".join(v.split()) if key != "about" else v.strip()


def _int(raw: dict, key: str, where: str, low: int, high: int, default: int | None = None) -> int:
    v = raw.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
        raise KitError(f"{where}: {key} should be a whole number from {low} to {high}")
    return v


def load(path: Path, bundled: bool = False) -> Kit:
    """The kit in folder ``path`` (its kit.toml), checked; KitError says what is wrong."""
    path = Path(path)
    where = f"kit {path.name}"
    toml = path / "kit.toml"
    if toml.is_symlink() or not toml.is_file():
        raise KitError(f"{where}: no kit.toml")
    from nomad.config import read_toml
    try:
        raw = read_toml(toml)
    except Exception as e:                               # noqa: BLE001 -- said, the kit left out
        raise KitError(f"{where}: kit.toml doesn't read ({e})") from None
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise KitError(f"{where}: unknown setting(s) {', '.join(unknown)}")
    name = raw.get("name")
    if not isinstance(name, str) or not _NAME.match(name):
        raise KitError(f"{where}: name should be lower-case letters, digits and -")
    where = f"kit {name}"
    program = raw.get("program")
    if not isinstance(program, str) or not _PROGRAM.match(program):
        raise KitError(f"{where}: program should be a template's program name")
    licence = raw.get("licence")
    if licence not in LICENCES:
        raise KitError(f"{where}: licence should be one of {', '.join(LICENCES)}")
    files = raw.get("files")
    if (not isinstance(files, list) or not files or len(files) > MAX_FILES
            or not all(isinstance(x, str) and _FILE.match(x) for x in files) or len(set(files)) != len(files)):
        raise KitError(f"{where}: files should list 1 to {MAX_FILES} file names (no folders)")
    if README in files or "kit.toml" in files:
        raise KitError(f"{where}: files may not hold {README} or kit.toml")
    inp = raw.get("input")
    if not isinstance(inp, str) or (inp != "." and inp not in files):
        raise KitError(f"{where}: input should be one of its files, or \".\" for the folder")
    build = raw.get("build")
    if build is not None:
        if not isinstance(build, dict) or set(build) - _BUILD_KEYS:
            raise KitError(f"{where}: [build] takes {', '.join(sorted(_BUILD_KEYS))}")
        mod = build.get("module")
        cmds = build.get("commands")
        makes = build.get("makes")
        if not isinstance(mod, str) or not _MODULE.match(mod):
            raise KitError(f"{where}: [build] module should be a module's name")
        if (not isinstance(cmds, list) or not cmds or len(cmds) > MAX_COMMANDS
                or not all(isinstance(c, str) and c.strip() and len(c) <= 400 and "\n" not in c for c in cmds)):
            raise KitError(f"{where}: [build] commands should be 1 to {MAX_COMMANDS} one-line commands")
        if not isinstance(makes, list) or not makes or not all(m in files for m in makes):
            raise KitError(f"{where}: [build] makes should list files (among files) the commands make")
        build = {"module": mod, "commands": list(cmds), "makes": list(makes)}
    else:
        missing = [f for f in files if not (path / f).is_file() or (path / f).is_symlink()]
        if missing:
            raise KitError(f"{where}: missing {', '.join(missing)}")
    size = sum((path / f).stat().st_size for f in files if (path / f).is_file())
    if size > MAX_BYTES:
        raise KitError(f"{where}: {size} bytes, more than {MAX_BYTES}")
    mem = raw.get("mem")
    if not isinstance(mem, str) or not _MEM.match(mem):
        raise KitError(f"{where}: mem should be like 8G or 500M")
    time = raw.get("time")
    if not isinstance(time, str) or not _TIME.match(time):
        raise KitError(f"{where}: time should be like 00:30:00 (or 1-00:00:00)")
    choose = raw.get("choose", {})
    if (not isinstance(choose, dict) or not all(isinstance(k, str) and _OPTION.match(k) for k in choose)
            or not all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 6 for v in choose.values())):
        raise KitError(f"{where}: choose should be {{option = its choice's number, from 1}}")
    look = raw.get("look", [])
    if not isinstance(look, list) or len(look) > MAX_LOOK:
        raise KitError(f"{where}: at most {MAX_LOOK} [[look]] entries")
    looks = []
    for i, x in enumerate(look, 1):
        if not isinstance(x, dict) or set(x) - _LOOK_KEYS or "file" not in x or "what" not in x:
            raise KitError(f"{where}: [[look]] #{i} takes file, what and (optionally) match")
        f = x["file"]
        if not isinstance(f, str) or not _LOOK_FILE.match(f) or ".." in f.split("/"):
            raise KitError(f"{where}: [[look]] #{i} file should be a file in the folder (a pattern may use * and ?)")
        what = _text(x, "what", f"{where}: [[look]] #{i}", 300)
        m = x.get("match")
        if m is not None:
            if not isinstance(m, str) or len(m) > 200:
                raise KitError(f"{where}: [[look]] #{i} match should be a regular expression")
            try:
                re.compile(m)
            except re.error as e:
                raise KitError(f"{where}: [[look]] #{i} match doesn't compile ({e})") from None
        looks.append({"file": f, "what": what, **({"match": m} if m else {})})
    recorded = _recorded(raw.get("recorded"), path, where)
    return Kit(name=name, program=program, title=_text(raw, "title", where, 120),
               about=_text(raw, "about", where, 1200), licence=licence,
               licence_note=_text(raw, "licence_note", where, 300, required=False),
               source=_text(raw, "source", where, 300), input=inp, files=tuple(files),
               cpus=_int(raw, "cpus", where, 1, 64), mem=mem, time=time, gpus=_int(raw, "gpus", where, 0, 8, 0),
               minutes=_int(raw, "minutes", where, 1, 120), path=path, bundled=bundled, choose=dict(choose),
               look=tuple(looks), build=build, recorded=recorded, recorded_from=path if recorded else None)


def _recorded(raw, path: Path, where: str) -> dict | None:
    """[recorded], checked: one real run's numbers, and the files it wrote
    that are kept in the kit (those not there are left out: the numbers stand)."""
    if raw is None:
        return None
    w = f"{where}: [recorded]"
    if not isinstance(raw, dict):
        raise KitError(f"{w} should be a table")
    unknown = sorted(set(raw) - _RECORDED_KEYS)
    if unknown:
        raise KitError(f"{w}: unknown setting(s) {', '.join(unknown)}")
    site = raw.get("site")
    if not isinstance(site, str) or not _PARTITION.match(site):
        raise KitError(f"{w} site should be the cluster's name")
    date = raw.get("date")
    if not isinstance(date, str) or not _DATE.match(date):
        raise KitError(f'{w} date should be text like "2026-11-09"')
    out = {"site": site, "date": date}
    for key, rx in (("module", _MODULE), ("partition", _PARTITION)):
        v = raw.get(key)
        if v is not None:
            if not isinstance(v, str) or not rx.match(v):
                raise KitError(f"{w} {key} should be a {key}'s name")
            out[key] = v
    out["cpus"] = _int(raw, "cpus", w, 1, 64)
    out["gpus"] = _int(raw, "gpus", w, 0, 8, 0)
    gpu = raw.get("gpu")
    if gpu is not None:
        out["gpu"] = _text(raw, "gpu", w, 60)
    out["runtime_s"] = _int(raw, "runtime_s", w, 0, 10_000_000)
    for key, high in (("cpu_s", 1_000_000_000), ("peak_mb", 10_000_000)):
        if key in raw:
            out[key] = _int(raw, key, w, 0, high)
    if "cores_busy" in raw:
        v = raw["cores_busy"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= out["cpus"] + 0.5:
            raise KitError(f"{w} cores_busy should be a number from 0 to its cpus")
        out["cores_busy"] = round(float(v), 1)
    files = raw.get("files", [])
    if (not isinstance(files, list) or len(files) > MAX_FILES or len(set(files)) != len(files)
            or not all(isinstance(f, str) and _RECORDED_FILE.match(f) for f in files)):
        raise KitError(f"{w} files should list files in the kit's recorded/ folder (recorded/NAME), each once")
    kept, size = [], 0
    for f in files:
        p = path
        ok = True
        for part in f.split("/"):
            p = p / part
            if p.is_symlink():
                ok = False
                break
        if ok and p.is_file():
            kept.append(f)
            size += p.stat().st_size
    if size > MAX_BYTES:
        raise KitError(f"{w}: {size} bytes of files, more than {MAX_BYTES}")
    out["files"] = kept
    return out


def recorded_text(kit: Kit) -> str:
    """One real run of the kit, in a sentence ("" with none): what a new run
    can be held against. Still a practice run's numbers: they size nothing."""
    r = kit.recorded
    if not r:
        return ""
    where = ", ".join(x for x in (r["date"], r.get("module"), r.get("partition") and f"partition {r['partition']}")
                      if x)
    size = f"{r['cpus']} CPU(s)" + (f" and {r['gpus']} GPU(s)" + (f" ({r['gpu']})" if r.get("gpu") else "")
                                    if r["gpus"] else "")
    parts = [f"{_secs(r['runtime_s'])} from start to end"]
    if "cpu_s" in r:
        parts.append(f"{_secs(r['cpu_s'])} of CPU time"
                     + (f" (about {r['cores_busy']:g} of its {r['cpus']} core(s) kept busy)" if "cores_busy" in r
                        else ""))
    text = f"A run of it on {r['site']} ({where}): {size}; " + ", ".join(parts)
    if "peak_mb" in r:
        text += f"; at most {_mb(r['peak_mb'])} of memory"
    return text + "."


def _secs(n: int) -> str:
    if n < 120:
        return f"{n} s"
    if n < 7200:
        return f"{n // 60} min {n % 60:02d} s"
    return f"{n // 3600} h {n % 3600 // 60:02d} min"


def _mb(n: int) -> str:
    return f"{n / 1024:.1f} GB" if n >= 1024 else f"{n} MB"


@dataclass(frozen=True)
class Settings:
    dirs: tuple = ()
    bundled: bool = True
    into: str = DEFAULT_INTO
    partition: dict = field(default_factory=dict)       # site -> where practice runs with no GPU go ("*": any site)
    gpu_partition: dict = field(default_factory=dict)   # site -> where those with a GPU go
    site_dirs: dict = field(default_factory=dict)       # site -> its own kits' folder there (the Console's gate)
    problems: tuple = ()

    def partition_for(self, site: str | None, gpus: int = 0) -> str | None:
        """Where a practice run is sent at ``site``: the setting for its kind
        (GPU or not), that site's or "*"; None: its template's."""
        d = self.gpu_partition if gpus else self.partition
        return d.get(site) or d.get("*") if site else d.get("*")


def settings(config: dict | None) -> Settings:
    """[submit.demos], checked: a wrong value is said and its default used."""
    sub = (config or {}).get("submit")
    raw = sub.get("demos") if isinstance(sub, dict) else None
    if raw is None:
        return Settings()
    if not isinstance(raw, dict):
        return Settings(problems=("[submit.demos] should be a table: ignored",))
    problems = [f"[submit.demos] {k}: unknown setting"
                for k in sorted(set(raw) - {"dirs", "bundled", "into", "partition", "gpu_partition", "site_dirs"})]
    dirs = raw.get("dirs", [])
    if isinstance(dirs, str):
        dirs = [dirs]
    if not isinstance(dirs, list) or not all(isinstance(d, str) and d.startswith("/") for d in dirs):
        problems.append("[submit.demos] dirs should be a list of full paths: none used")
        dirs = []
    bundled = raw.get("bundled", True)
    if not isinstance(bundled, bool):
        problems.append("[submit.demos] bundled should be true or false: true used")
        bundled = True
    into = raw.get("into", DEFAULT_INTO)
    if not isinstance(into, str) or not (into.startswith("/") or into.startswith("~/")):
        problems.append(f"[submit.demos] into should be a full path or one in your home (~/...): {DEFAULT_INTO} used")
        into = DEFAULT_INTO
    parts = {}
    for key in ("partition", "gpu_partition"):
        v = raw.get(key)
        if v is None:
            parts[key] = {}
        elif isinstance(v, str) and _PARTITION.match(v):
            parts[key] = {"*": v}
        elif isinstance(v, dict) and all(isinstance(k, str) and isinstance(x, str) and _PARTITION.match(x)
                                         for k, x in v.items()):
            parts[key] = dict(v)
        else:
            problems.append(f"[submit.demos] {key} should be a partition's name, or {{site = partition}}: "
                            "the templates' used")
            parts[key] = {}
    site_dirs = raw.get("site_dirs", {})
    if not isinstance(site_dirs, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and v.startswith("/") and ".." not in v.split("/")
            for k, v in site_dirs.items()):
        problems.append("[submit.demos] site_dirs should be {site = \"/full/path\"}: none used")
        site_dirs = {}
    return Settings(dirs=tuple(dirs), bundled=bundled, into=into, partition=parts["partition"],
                    gpu_partition=parts["gpu_partition"], site_dirs=dict(site_dirs), problems=tuple(problems))


def kits(config: dict | None = None, program: str | None = None, ready_only: bool = False) -> tuple[list, list]:
    """(the kits offered, what is wrong with the others): a site's own folders
    first (a kit of the same name there replaces nomad's), then nomad's."""
    s = settings(config)
    found: dict = {}
    nomads: dict = {}
    problems = list(s.problems)
    places = [(Path(d), False) for d in s.dirs] + ([(BUNDLED, True)] if s.bundled else [])
    for base, bundled in places:
        if not base.is_dir():
            if not bundled:
                problems.append(f"[submit.demos] dirs: {base} isn't a folder here")
            continue
        for sub in sorted(p for p in base.iterdir() if p.is_dir() and not p.is_symlink()):
            if not (sub / "kit.toml").exists():
                continue
            try:
                k = load(sub, bundled=bundled)
            except KitError as e:
                problems.append(str(e))
                continue
            found.setdefault(k.name, k)
            if bundled:
                nomads[k.name] = k
    for name, k in list(found.items()):
        n = nomads.get(name)
        same = n is not None and (k.program, k.files, k.cpus, k.gpus, (k.build or {}).get("module")) == (
            n.program, n.files, n.cpus, n.gpus, (n.build or {}).get("module"))
        if not k.bundled and not k.recorded and same and n.recorded:
            found[name] = replace(k, recorded=n.recorded, recorded_from=n.recorded_from)   # built from nomad's
    out = [k for k in found.values()
           if (program is None or k.program.lower() == program.lower()) and (k.ready or not ready_only)]
    return sorted(out, key=lambda k: (k.program.lower(), k.name)), problems


def find(config: dict | None, name: str) -> Kit:
    got, problems = kits(config)
    for k in got:
        if k.name == name:
            return k
    raise KitError(f"no kit named {name}" + (f" ({'; '.join(problems)})" if problems else ""))


def check(kit: Kit, config: dict | None, site: str) -> list:
    """What keeps the kit from running with the site's template: [] when it fits."""
    from nomad.submit import templates as TT
    got, _ = TT.templates(config, site)
    t = next((x for x in got if x.program.lower() == kit.program.lower()), None)
    if t is None:
        return [f"{site} has no {kit.program} template"]
    out = []
    if t.folder:
        if kit.input != ".":
            out.append(f"{kit.program} takes a folder holding {t.folder}: input should be \".\"")
        if t.folder not in kit.files:
            out.append(f"{kit.program} runs in a folder holding {t.folder}: the kit has none")
    else:
        if kit.input == "." or not any(kit.input.endswith(e) for e in t.inputs):
            out.append(f"{kit.program} takes a file ending in {', '.join(t.inputs)}: the kit's input is {kit.input}")
    for opt, n in kit.choose.items():
        o = next((x for x in t.options if x.name == opt), None)
        if o is None:
            out.append(f"{kit.program} has no option {opt}")
        elif n > len(o.choices):
            out.append(f"{kit.program}'s {opt} has {len(o.choices)} choices, not {n}")
    if kit.gpus and not (t.gpus or any(c.gpus for o in t.options for c in o.choices)):
        out.append(f"the kit asks for GPUs; {kit.program}'s template asks for none")
    if not kit.ready:
        out.append("not built yet: nomad demos build " + kit.name)
    return out


def _readme(kit: Kit) -> str:
    lines = [kit.title, "=" * len(kit.title), "", PRACTICE, "", kit.about, "",
             f"Program: {kit.program}. {LICENCES[kit.licence]}." + (f" {kit.licence_note}" if kit.licence_note else ""),
             f"Size: {kit.cpus} CPU(s), {kit.mem}" + (f", {kit.gpus} GPU(s)" if kit.gpus else "")
             + f", at most {kit.time}; it runs for about {kit.minutes} minute(s).",
             f"Input: {'this folder' if kit.input == '.' else kit.input}",
             f"Kit: {kit.name}"]
    if not kit.bundled:
        lines.append(f"Kit folder: {kit.path.parent}")            # a site's own: where the gate finds it again
    lines += [f"(nomad demos show {kit.name}; once it has run, nomad demos look {kit.name} THIS-FOLDER)", ""]
    if kit.recorded:
        lines += [recorded_text(kit), "(Yours may take longer or shorter: it depends on the node, and the queue.)", ""]
    if kit.look:
        lines.append("Once it has run, look at:")
        for x in kit.look:
            lines.append(f"  - {x['file']}: {x['what']}" + (f" (lines matching {x['match']})" if x.get("match") else ""))
        lines.append("")
    lines += ["To send it: nomad submit on the cluster (a script is made for you), or the Console's",
              "Submit a job page: No script yet? Make one, with this folder's input.", "",
              f"Where the files come from: {kit.source}", ""]
    return "\n".join(lines)


def copy(kit: Kit, dest: str | Path | None = None, into: str | None = None, now: datetime | None = None) -> dict:
    """Copy the kit, as whoever runs this, into ``dest`` (a new folder, or an
    empty one), else a new folder in ``into``. Never writes over a file.
    {folder, input, files}."""
    if not kit.ready:
        raise KitError(f"{kit.name} isn't built yet: staff build it once with nomad demos build {kit.name}")
    now = now or datetime.now()
    if dest is None:
        base = Path(os.path.expanduser(into or DEFAULT_INTO))
        base.mkdir(parents=True, exist_ok=True)
        dest = base / f"{kit.name}-{now:%Y%m%d-%H%M%S}"
    dest = Path(os.path.expanduser(str(dest)))
    if dest.is_symlink():
        raise KitError(f"{dest} is a link: give a folder")
    if dest.exists():
        if not dest.is_dir() or any(dest.iterdir()):
            raise KitError(f"{dest} is there already and isn't an empty folder: nothing is copied over anything")
    else:
        dest.mkdir(parents=True)
    written = []
    # Through the two folders' descriptors, never following a link (the copy is
    # made as the person: a kit file swapped for a link to one of theirs is refused).
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        sfd = os.open(str(kit.path), flags)
    except OSError as e:
        raise KitError(f"{kit.name}: its folder can't be read ({e.strerror})") from None
    try:
        try:
            dfd = os.open(str(dest), flags)
        except OSError as e:
            raise KitError(f"{dest}: {e.strerror}") from None
        try:
            for f in kit.files:
                _write_at(dfd, f, _read_at(sfd, f, kit.name), dest)
                written.append(f)
            _write_at(dfd, README, _readme(kit).encode("utf-8"), dest)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)
    return {"folder": str(dest), "input": str(dest if kit.input == "." else dest / kit.input),
            "files": written + [README]}


def _read_at(dfd: int, name: str, kit: str) -> bytes:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
    except OSError:
        raise KitError(f"{kit}: {name} isn't a plain file") from None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise KitError(f"{kit}: {name} isn't a plain file")
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise KitError(f"{kit}: {name} is larger than {MAX_BYTES} bytes")
    return data


def _write_at(dfd: int, name: str, data: bytes, dest: Path) -> None:
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=dfd)
    except FileExistsError:
        raise KitError(f"{dest / name} is there already: nothing is written over") from None
    except OSError as e:
        raise KitError(f"{dest / name}: {e.strerror}") from None
    with os.fdopen(fd, "wb") as f:
        f.write(data)


def is_practice(folder: str | None) -> bool:
    """Whether a folder holds a practice run (a copy's README-practice.txt):
    its jobs say nothing about what real work needs (nomad.submit.sizing)."""
    if not folder:
        return False
    try:
        return os.path.isfile(os.path.join(folder, README))
    except (OSError, ValueError):
        return False


def kit_values(raw) -> dict:
    """A kit's name, program, size and choices -- a Kit, or a cluster's gate's
    description of one (its kits answer: the hub may not have that kit) --
    checked as load() checks a kit.toml; KitError says what is wrong."""
    if isinstance(raw, Kit):
        return {"name": raw.name, "program": raw.program, "cpus": raw.cpus, "mem": raw.mem, "time": raw.time,
                "gpus": raw.gpus, "choose": dict(raw.choose)}
    if not isinstance(raw, dict):
        raise KitError("a practice kit should be described by its name, program, size and choices")
    name, program = raw.get("name"), raw.get("program")
    if not isinstance(name, str) or not _NAME.match(name):
        raise KitError("a practice kit's name is lower-case letters, digits and -")
    if not isinstance(program, str) or not _PROGRAM.match(program):
        raise KitError(f"kit {name}: program should be a template's program name")
    mem, time, choose = raw.get("mem"), raw.get("time"), raw.get("choose") or {}
    if not isinstance(mem, str) or not _MEM.match(mem):
        raise KitError(f"kit {name}: mem should be like 8G or 500M")
    if not isinstance(time, str) or not _TIME.match(time):
        raise KitError(f"kit {name}: time should be like 00:30:00")
    if (not isinstance(choose, dict) or not all(isinstance(k, str) and _OPTION.match(k) for k in choose)
            or not all(isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 6 for v in choose.values())):
        raise KitError(f"kit {name}: choose should be {{option = its choice's number, from 1}}")
    return {"name": name, "program": program, "cpus": _int(raw, "cpus", f"kit {name}", 1, 64),
            "mem": mem, "time": time, "gpus": _int(raw, "gpus", f"kit {name}", 0, 8, 0), "choose": dict(choose)}


def job_values(kit, config: dict | None, site: str | None, choose: dict | None = None) -> dict:
    """What a kit's job script is made with at ``site`` (templates.make):
    its own small size, where practice runs go there ([submit.demos]
    partition, gpu_partition, by the GPUs it asks for), and its choice of the
    template's options. ``kit``: a Kit, or a gate's description of one
    (kit_values). ``choose``: the person's own choices, over the kit's
    (r-bootstrap asks for a second run with 8 cores). A choice that differs
    from the kit's is sized as the template sizes it -- its CPUs and GPUs
    (AMBER's "one CPU core": no GPU, and not on the GPU nodes) -- with the
    kit's memory and time. Choosing an option's first choice, which is
    what the kit has anyway, changes nothing."""
    v = kit_values(kit)
    own = v["choose"]
    picked = {**own, **{k: n for k, n in (choose or {}).items()}}
    changed = {o for o in set(picked) | set(own) if picked.get(o, 1) != own.get(o, 1)}
    cpus, gpus = v["cpus"], v["gpus"]
    if changed:
        from nomad.submit import templates as TT
        try:
            got, _ = TT.templates(config, site)
            t = next((x for x in got if x.program.lower() == v["program"].lower()), None)
            if t is not None:
                def sized(c: dict) -> tuple:
                    n, g = t.cpus, t.gpus
                    for _o, x in TT.picked(t, c):
                        n = x.cpus if x.cpus is not None else n
                        g = x.gpus if x.gpus is not None else g
                    return n, g
                was, now = sized(own), sized(picked)
                # only what the person's choice changes is the template's (a precision, say, changes neither)
                cpus = int(now[0] or v["cpus"]) if now[0] != was[0] else cpus
                gpus = int(now[1] or 0) if now[1] != was[1] else gpus
        except TT.TemplateError:
            pass                                       # a choice it hasn't: templates.make says so
    kept = {o: n for o, n in picked.items() if n != 1 or o in own}
    return {"cpus": cpus, "mem": v["mem"], "time": v["time"], "gpus": gpus,
            "partition": settings(config).partition_for(site, gpus), "choose": kept or None}


def mark(text: str, kit) -> str:
    """A kit's job script, saying under its first line that it is a practice
    run: whoever reads it (or its job's script later) is told. ``kit``: a Kit,
    or a kit's name."""
    name = kit.name if isinstance(kit, Kit) else kit
    if not isinstance(name, str) or not _NAME.match(name):
        raise KitError("a practice kit's name is lower-case letters, digits and -")
    note = (f"# A practice run (nomad's {name} kit): its results mean nothing for research,\n"
            "# and its size says nothing about what real work needs.\n")
    # Its job is named practice-KIT (1.35.1): squeue, sacct, the hub's records and the Console say what it is.
    text, n = _JOB_NAME.subn(lambda m: m.group(1) + f"practice-{name}", text, count=1)
    first, nl, rest = text.partition("\n")
    if first.startswith("#!") and nl:
        return first + nl + note + ("" if n else f"#SBATCH --job-name=practice-{name}\n") + rest
    return note + ("" if n else f"#SBATCH --job-name=practice-{name}\n") + text


_JOB_NAME = re.compile(r"(?m)^(#SBATCH[ \t]+(?:--job-name[= ]|-J[ \t]*))[^\s#]+")
PRACTICE_JOB = "practice-"          # the job name's start: practice-KIT
_COPY_FOLDER = re.compile(r"^(?P<kit>[a-z0-9][a-z0-9-]{1,47}?)(?:-[0-9]{1,3}|-[0-9]{8}-[0-9]{6})?$")


def practice_names(config: dict | None = None) -> dict:
    """{kit name: the job name its script gives it (the input's stem; None for a
    folder kit)}, for practice_label: nomad's kits, and a site's in dirs."""
    from nomad.submit.templates import job_name
    got, _ = kits(config)
    return {k.name: (None if k.input == "." else job_name(Path(k.input).stem)) for k in got}


def practice_label(job_name: str | None, work_tail: str | None, names: dict) -> str | None:
    """The kit a job of the hub's records ran, or None. A practice copy is a
    folder named after its kit (KIT, KIT-2, KIT-YYYYMMDD-HHMMSS: the Console's
    and nomad demos copy's; the hub keeps a job's folder's last two parts
    only), and its job is named practice-KIT (1.35.1's scripts; a site's own
    kit too) or, before 1.35.1, as KIT's script names it. A script copied
    into real work, or a kit-named job elsewhere, is not one."""
    name = str(job_name or "")
    last = str(work_tail or "").rstrip("/").rpartition("/")[2]
    m = _COPY_FOLDER.match(last)
    if not m:
        return None
    for kit in dict.fromkeys((m.group("kit"), last)):
        if not _NAME.match(kit):
            continue
        if name == PRACTICE_JOB + kit:
            return kit
        if names.get(kit) and names[kit] == name:
            return kit
    return None


_KNOWN: dict = {}


def practice_row(row: dict) -> str | None:
    """practice_label for a row of the hub's jobs table (its job_name and
    work_tail), with nomad's own kits: what scores and advice leave out."""
    if "names" not in _KNOWN:
        try:
            _KNOWN["names"] = practice_names()
        except Exception:                                  # noqa: BLE001 -- none known: nothing left out
            _KNOWN["names"] = {}
    return practice_label(row.get("job_name"), row.get("work_tail"), _KNOWN["names"])


def kit_source(folder: str | Path) -> tuple:
    """(kit name, the site folder it came from or None, title) from a
    practice copy's README, read without following a link; (None, None, None)
    when there is none."""
    try:
        fd = os.open(os.path.join(str(folder), README), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None, None, None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            return None, None, None
        text = f.read(64 * 1024).decode("utf-8", errors="replace")
    m = re.search(r"(?m)^Kit: ([a-z0-9][a-z0-9-]{1,47})[ \t]*$", text)
    d = re.search(r"(?m)^Kit folder: (/[^\n]{1,500})$", text)
    src = d.group(1).strip() if d and ".." not in d.group(1).split("/") else None
    return (m.group(1) if m else None), src, text.split("\n", 1)[0].strip()


def kit_of(folder: str | Path, config: dict | None = None) -> Kit | None:
    """The kit a practice copy in ``folder`` was made from, by its README
    (its Kit: line, 1.35.1; before, its title), among the kits ``config``
    offers, or None."""
    name, _src, title = kit_source(folder)
    if name is None and title is None:
        return None
    got, _ = kits(config)
    if name:
        return next((k for k in got if k.name == name), None)
    same = [k for k in got if k.title == title]
    return same[0] if len(same) == 1 else None


def recorded_look(kit: Kit, last: int = 6) -> list:
    """What the recorded run's kept files show, as look() shows a copy's:
    [] when the kit keeps none."""
    if not kit.recorded or not kit.recorded.get("files") or kit.recorded_from is None:
        return []
    return look(kit, kit.recorded_from / "recorded", last=last)


LOOK_BYTES = 4 * 1024 * 1024       # of a file's end, looked through (the gate does this on a job's page)
LOOK_ENTRIES = 2000                 # entries of a folder looked through for a pattern
LOOK_FOUND = 6                      # files shown for one pattern
LOOK_LINE = 400                     # characters of a line shown


def look(kit: Kit, folder: str | Path, last: int = 6, job: str | None = None) -> list:
    """What to look at once a copy of the kit has run in ``folder``: for each
    [[look]] entry, the files it names there and (with match) their last
    ``last`` matching lines, in the last LOOK_BYTES of each file. ``job``: a
    job's number -- its own files (a name holding it) are shown, else the
    newest first. [{file, what, found: [{path, lines|size}]}]."""
    import fnmatch
    import itertools
    base = Path(os.path.expanduser(str(folder)))
    own = re.compile(rf"(?<![0-9]){re.escape(str(job))}(?![0-9])") if job else None
    out = []
    for x in kit.look:
        pattern = x["file"]
        head, _, name = pattern.rpartition("/")
        where = base / head if head else base
        found = []
        if where.is_dir() and not where.is_symlink():
            if any(c in name for c in "*?["):
                try:
                    with os.scandir(where) as it:
                        names = [e.name for e in itertools.islice(it, LOOK_ENTRIES)]
                except OSError:
                    names = []
                names = [n for n in names if fnmatch.fnmatch(n, name)]
            else:
                names = [name]                             # one file: looked at directly, however full the folder
            files = []
            for n in names:
                p = where / n
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    files.append((p, st))
            if own and any(own.search(p.name) for p, _ in files):
                files = [(p, st) for p, st in files if own.search(p.name)]
            files.sort(key=lambda f: (-f[1].st_mtime, f[0].name))
            for p, st in files[:LOOK_FOUND]:
                item = {"path": str(p), "size": st.st_size}
                if x.get("match"):
                    item["lines"] = _matching(p, re.compile(x["match"]), last)
                found.append(item)
        out.append({"file": pattern, "what": x["what"], "found": found})
    return out


def _matching(p: Path, rx, last: int) -> list:
    """The last ``last`` lines of file p matching rx, in its last LOOK_BYTES
    (opened without following a link, and only a plain file: never a FIFO)."""
    try:
        fd = os.open(str(p), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return []
    with os.fdopen(fd, "rb") as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode):
            return []
        f.seek(max(0, st.st_size - LOOK_BYTES))
        data = f.read(LOOK_BYTES).decode("utf-8", errors="replace")
    hits = []
    for line in data.splitlines():
        if rx.search(line):
            hits.append(line[:LOOK_LINE])
            hits = hits[-last:]
    return hits


def practice_of(folder: str | None, config: dict | None = None, job: str | None = None) -> dict | None:
    """For a job that ran in ``folder``: None unless it is a practice copy;
    else its kit, what to look at there now (``job``'s own files first), and
    the kit's recorded run (its numbers, and the same things to look at in
    what it wrote), for the Console's job pages (the gate's job answer,
    1.35.1). ``config``: the kits to know it among (the gate adds the site's
    folder the copy names, once it has checked it)."""
    if not folder or not is_practice(folder):
        return None
    kit = kit_of(folder, config)
    out = {"folder": str(folder), "practice": PRACTICE, "kit": None}
    if kit is None:
        return out
    rec = []
    top = (kit.recorded_from / "recorded") if kit.recorded_from else None
    for x in recorded_look(kit):
        rec.append({**x, "found": [{**f, "path": "recorded/" + os.path.relpath(f["path"], top)} for f in x["found"]]})
    out.update({"kit": kit.name, "title": kit.title, "program": kit.program, "look": look(kit, folder, job=job),
                "recorded_text": recorded_text(kit), "recorded": rec})
    return out


def build(kit: Kit, to: str | Path, config: dict | None = None, site: str | None = None, module: str | None = None,
          run=subprocess.run, timeout: int = 1800, module_paths: list | None = None) -> dict:
    """Make a kit's files once, at a site: its folder copied to ``to``/name
    (new), its commands run there with its module loaded (as whoever runs
    this), and what they make checked. ``to`` is then one of [submit.demos]
    dirs. {folder, made, log}."""
    if not kit.build:
        raise KitError(f"{kit.name} has nothing to build: it is ready as it is")
    mod = module or kit.build["module"]
    if not _MODULE.match(mod):
        raise KitError(f"{mod!r} isn't a module's name")
    target = Path(os.path.expanduser(str(to))) / kit.name
    if target.exists() or target.is_symlink():
        raise KitError(f"{target} is there already: give another folder, or remove it first")
    target.mkdir(parents=True)
    for p in sorted(kit.path.iterdir()):
        if p.is_file() and not p.is_symlink() and p.name not in kit.build["makes"]:
            with open(p, "rb") as a, open(target / p.name, "xb") as b:
                b.write(a.read())
    if kit.recorded and kit.recorded_from is not None:
        for f in kit.recorded.get("files", []):          # the recorded run's files, checked by load()
            (target / f).parent.mkdir(parents=True, exist_ok=True)
            with open(kit.recorded_from / f, "rb") as a, open(target / f, "xb") as b:
                b.write(a.read())
    paths = list(module_paths or [])
    if site and config and not paths:
        from nomad.submit import templates as TT
        try:
            paths = TT.site_settings(config, site).get("module_paths") or []
        except Exception:                              # noqa: BLE001 -- without them: a login's own
            paths = []
    script = "set -e\n" + "".join(f"module use {shlex.quote(p)}\n" for p in paths) + \
        f"module load {shlex.quote(mod)}\n" + "\n".join(kit.build["commands"]) + "\n"
    with open(target / "build.sh", "x", encoding="utf-8") as f:
        f.write(script)
    got = run(["bash", "-l", "build.sh"], cwd=str(target), capture_output=True, text=True, timeout=timeout)
    log = (got.stdout or "") + (got.stderr or "")
    with open(target / "build.log", "w", encoding="utf-8") as f:
        f.write(log)
    missing = [m for m in kit.build["makes"] if not (target / m).is_file()]
    # what the gate copies as each person must be changeable by no one else (whatever the builder's umask)
    for d, dirs, names in os.walk(target):
        for x in [d] + [os.path.join(d, n) for n in names]:
            if not os.path.islink(x):
                os.chmod(x, stat.S_IMODE(os.lstat(x).st_mode) & ~0o022)
    if got.returncode != 0 or missing:
        raise KitError(f"building {kit.name} failed (exit {got.returncode}"
                       + (f"; not made: {', '.join(missing)}" if missing else "") + f"): see {target / 'build.log'}")
    return {"folder": str(target), "made": list(kit.build["makes"]), "log": str(target / "build.log")}

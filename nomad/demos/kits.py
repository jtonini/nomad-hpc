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

The template writes the job script around the kit's input, as for anyone's
own input: a practice run runs the way real work will.

Kits come with nomad (nomad/demos/kits) and from a site's own folders::

    [submit.demos]
    dirs = ["/usr/local/share/nomad-demos"]   # folders of kit folders (a site's own, and those built there)
    bundled = true                            # also the kits that come with nomad (default)
    into = "~/nomad-demos"                    # where a copy goes: a new folder each time
    partition = { spydur = "condos" }         # where practice runs with no GPU are sent (default: the template's)
    gpu_partition = "gpunodes"                # and those with one (a name: every site)

A site's kit of the same name is used instead of nomad's (a built one).
Copying never writes over anything; building runs a kit's commands as
whoever builds it, in a new folder.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
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
                "bundled": self.bundled, "recorded": dict(self.recorded) if self.recorded else None}


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
    recorded = raw.get("recorded")
    if recorded is not None and not isinstance(recorded, dict):
        raise KitError(f"{where}: [recorded] should be a table")
    return Kit(name=name, program=program, title=_text(raw, "title", where, 120),
               about=_text(raw, "about", where, 1200), licence=licence,
               licence_note=_text(raw, "licence_note", where, 300, required=False),
               source=_text(raw, "source", where, 300), input=inp, files=tuple(files),
               cpus=_int(raw, "cpus", where, 1, 64), mem=mem, time=time, gpus=_int(raw, "gpus", where, 0, 8, 0),
               minutes=_int(raw, "minutes", where, 1, 120), path=path, bundled=bundled, choose=dict(choose),
               look=tuple(looks), build=build, recorded=dict(recorded) if recorded else None)


@dataclass(frozen=True)
class Settings:
    dirs: tuple = ()
    bundled: bool = True
    into: str = DEFAULT_INTO
    partition: dict = field(default_factory=dict)       # site -> where practice runs with no GPU go ("*": any site)
    gpu_partition: dict = field(default_factory=dict)   # site -> where those with a GPU go
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
                for k in sorted(set(raw) - {"dirs", "bundled", "into", "partition", "gpu_partition"})]
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
    return Settings(dirs=tuple(dirs), bundled=bundled, into=into, partition=parts["partition"],
                    gpu_partition=parts["gpu_partition"], problems=tuple(problems))


def kits(config: dict | None = None, program: str | None = None, ready_only: bool = False) -> tuple[list, list]:
    """(the kits offered, what is wrong with the others): a site's own folders
    first (a kit of the same name there replaces nomad's), then nomad's."""
    s = settings(config)
    found: dict = {}
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
             f"Input: {'this folder' if kit.input == '.' else kit.input}", ""]
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
    try:
        for f in kit.files:
            src = kit.path / f
            if src.is_symlink() or not src.is_file():
                raise KitError(f"{kit.name}: {f} isn't a plain file")
            with open(src, "rb") as a, open(dest / f, "xb") as b:
                b.write(a.read())
            written.append(f)
        with open(dest / README, "x", encoding="utf-8") as b:
            b.write(_readme(kit))
    except FileExistsError as e:
        raise KitError(f"{e.filename} is there already: nothing is written over") from None
    return {"folder": str(dest), "input": str(dest if kit.input == "." else dest / kit.input),
            "files": written + [README]}


def look(kit: Kit, folder: str | Path, last: int = 6) -> list:
    """What to look at once a copy of the kit has run in ``folder``: for each
    [[look]] entry, the files it names there and (with match) their last
    ``last`` matching lines. [{file, what, found: [{path, lines|size}]}]."""
    import fnmatch
    base = Path(os.path.expanduser(str(folder)))
    out = []
    for x in kit.look:
        pattern = x["file"]
        head, _, name = pattern.rpartition("/")
        where = base / head if head else base
        found = []
        if where.is_dir() and not where.is_symlink():
            for p in sorted(where.iterdir()):
                if not fnmatch.fnmatch(p.name, name) or p.is_symlink() or not p.is_file():
                    continue
                item = {"path": str(p), "size": p.stat().st_size}
                if x.get("match"):
                    rx = re.compile(x["match"])
                    hits = []
                    with open(p, encoding="utf-8", errors="replace") as f:
                        for line in f:
                            if rx.search(line):
                                hits.append(line.rstrip("\n"))
                                hits = hits[-last:]
                    item["lines"] = hits
                found.append(item)
        out.append({"file": pattern, "what": x["what"], "found": found})
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
    if got.returncode != 0 or missing:
        raise KitError(f"building {kit.name} failed (exit {got.returncode}"
                       + (f"; not made: {', '.join(missing)}" if missing else "") + f"): see {target / 'build.log'}")
    return {"folder": str(target), "made": list(kit.build["makes"]), "log": str(target / "build.log")}

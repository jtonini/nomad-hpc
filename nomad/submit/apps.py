# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Which program a job script runs, and what its input file says it needs.

``program_of(commands)`` reads the script's command lines (no shell runs
them) and names the program: the first command that isn't setting up
(module, source, cd, export, conda...), past launchers (srun, mpirun,
time...). ``needs(program, read)`` reads its input file, when the program
states sizes there:

- Gaussian: ``%mem`` and ``%nprocshared`` (``%nproc``, ``%cpu``) in Link 0
  (memory to ask: %mem and a fifth more);
- ORCA: ``%maxcore`` (MB per process) and ``%pal nprocs`` (or ``! PALn``)
  (memory to ask: maxcore × processes, and a quarter more);
- Q-Chem: ``MEM_TOTAL`` in ``$rem`` (MB, for each process), and the
  command's ``-nt`` (threads) or ``-np`` (processes);
- Columbus: ``runc -m`` (MB, for each process) and ``-nproc``;
- GROMACS: ``mdrun -nt``, or ``-ntmpi`` × ``-ntomp``;
- NAMD: ``+pN``.

The last three, and Q-Chem's threads, come from the command line itself; a
value given by a shell variable ($SLURM_CPUS_PER_TASK) isn't a number nomad
can read, and is left to the job's own request.

Anything else is only named, so a person's past runs of the same program
can be compared. ``read`` is how the file is read (the person's own run of
nomad reads their files; nothing else does), at most MAX_INPUT bytes.
"""
from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass, field

MAX_INPUT = 2 * 1024 * 1024

# Executable (its base name, lower case) -> the program's family. A name
# ending in a version (g16.b01) or a build (vasp_std, pmemd.cuda) is matched
# by its stem.
FAMILIES = {
    "g16": "Gaussian", "g09": "Gaussian", "g03": "Gaussian", "gaussian": "Gaussian",
    "orca": "ORCA",
    "runc": "Columbus", "columbus": "Columbus",
    "pmemd": "AMBER", "pmemd.mpi": "AMBER", "pmemd.cuda": "AMBER", "pmemd.cuda.mpi": "AMBER",
    "sander": "AMBER", "sander.mpi": "AMBER", "tleap": "AMBER", "cpptraj": "AMBER",
    "mmpbsa.py": "AMBER",
    "gmx": "GROMACS", "gmx_mpi": "GROMACS", "gmx_d": "GROMACS", "mdrun": "GROMACS",
    "lmp": "LAMMPS", "lmp_mpi": "LAMMPS", "lmp_serial": "LAMMPS", "lammps": "LAMMPS",
    "namd2": "NAMD", "namd3": "NAMD", "namd": "NAMD",
    "vasp_std": "VASP", "vasp_gam": "VASP", "vasp_ncl": "VASP", "vasp": "VASP",
    "pw.x": "Quantum ESPRESSO", "ph.x": "Quantum ESPRESSO", "cp.x": "Quantum ESPRESSO",
    "cp2k": "CP2K", "cp2k.popt": "CP2K", "cp2k.psmp": "CP2K", "cp2k.ssmp": "CP2K",
    "fdtd-engine": "Lumerical", "fdtd-engine-mpich2nem": "Lumerical",
    "fdtd-engine-ompi-lcl": "Lumerical", "varfdtd-engine": "Lumerical",
    "fde-engine": "Lumerical", "lumerical": "Lumerical",
    "qchem": "Q-Chem", "psi4": "Psi4", "nwchem": "NWChem", "molpro": "Molpro",
    "pymolcas": "OpenMolcas", "xtb": "xtb", "crest": "CREST",
    "comsol": "COMSOL", "matlab": "MATLAB", "rscript": "R", "r": "R", "julia": "Julia",
    "python": "Python", "python3": "Python", "ipython": "Python",
}
# Interpreters and environments: running one says little about the work, so
# a past run of another script with the same one isn't "the same program".
GENERIC = frozenset({"Python", "R", "Julia", "MATLAB"})
# What only sets up, or launches something else.
_SETUP = {"module", "ml", "source", ".", "cd", "export", "set", "unset", "echo", "printf", "mkdir",
          "cp", "mv", "rm", "ln", "date", "hostname", "conda", "mamba", "micromamba", "spack",
          "ulimit", "umask", "sleep", "cat", "touch", "env", "eval", "trap", "wait", "exit",
          "true", "false", ":", "if", "then", "fi", "else", "elif", "for", "do", "done", "while",
          "case", "esac", "local", "declare", "shopt", "alias", "pushd", "popd", "scontrol",
          "squeue", "nvidia-smi", "lscpu", "free", "df", "ls", "pwd", "which", "whoami", "id", "tar",
          "gzip", "gunzip", "rsync", "chmod", "test", "[", "[[", "unalias", "readonly", "let"}
# Launchers, and the options of each that take the next word as their value
# (anything else takes none, unless written --opt=value).
_LAUNCH_VALUES = {
    "srun": {"-n", "-N", "-c", "-p", "-t", "-w", "-x", "-J", "-o", "-e", "-A", "-q", "-G", "-m",
             "--ntasks", "--nodes", "--cpus-per-task", "--partition", "--time", "--gpus", "--mem",
             "--mpi", "--distribution", "--cpu-bind", "--export"},
    "mpirun": {"-n", "-np", "--np", "-N", "-c", "-x", "-H", "--host", "-hostfile", "--hostfile",
               "-machinefile", "--machinefile", "--map-by", "--bind-to", "--rank-by", "-ppn",
               "--ppn", "-wdir", "--wdir", "-f", "-npernode", "--npernode", "--prefix", "-genvlist",
               "-launcher", "-bootstrap", "-rr", "--display-map", "-iface"},
    "time": {"-o", "-f", "--output", "--format"},
    "numactl": {"-C", "-N", "-m", "-p", "-i", "--physcpubind", "--cpunodebind", "--membind",
                "--preferred", "--interleave"},
    "taskset": {"-c"},
    "nice": {"-n"},
    "stdbuf": {"-i", "-o", "-e"},
    "container": {"-B", "--bind", "--env", "--home", "-H", "--pwd", "--overlay", "--env-file",
                  "-W", "--workdir", "--app", "-c"},
    "charmrun": {"++nodelist", "++ppn", "++np", "++p"},
}
_LAUNCH_TWO = {"--mca", "-mca", "-genv", "-env", "-x"}
_LAUNCH_ALIASES = {"mpiexec": "mpirun", "mpiexec.hydra": "mpirun", "ibrun": "mpirun",
                   "/usr/bin/time": "time", "singularity": "container", "apptainer": "container"}
_LAUNCHERS = set(_LAUNCH_VALUES) | set(_LAUNCH_ALIASES) | {"nohup", "exec", "command", "xvfb-run"}
_MPI = {"srun", "mpirun", "charmrun"}
_CONTAINER_SUB = {"exec", "run", "shell"}
_INPUT_EXT = {"Gaussian": (".com", ".gjf", ".gau", ".inp"), "ORCA": (".inp",),
              "LAMMPS": (".in", ".lmp"), "Lumerical": (".fsp", ".lms", ".ldev", ".icp"),
              "GROMACS": (".tpr",), "AMBER": (".in", ".mdin"), "NAMD": (".conf", ".namd"),
              "Q-Chem": (".in", ".inp"), "Psi4": (".in", ".dat", ".py"), "NWChem": (".nw",),
              "Molpro": (".com", ".inp"), "Python": (".py",), "R": (".r", ".R"),
              "MATLAB": (".m",), "Julia": (".jl",), "COMSOL": (".mph",), "xtb": (".xyz",),
              "CREST": (".xyz",)}


@dataclass
class Program:
    """The program a script runs: its family ("Gaussian"), executable
    ("g16"), the command line it was found on, its input file as the script
    names it (None when it can't tell), the launcher it runs under (srun,
    mpirun, charmrun...) and the folder a literal ``cd`` before it went to."""
    family: str | None
    executable: str | None
    command: str | None = None
    input: str | None = None
    gpu_build: bool = False             # an executable built for GPUs (pmemd.cuda, gmx with -nb gpu)
    launcher: str | None = None
    cd: str | None = None               # None: the job's own folder

    def to_dict(self) -> dict:
        return {"family": self.family, "executable": self.executable, "command": self.command,
                "input": self.input, "gpu_build": self.gpu_build, "launcher": self.launcher,
                "cd": self.cd}


def _family(word: str) -> str | None:
    base = posixpath.basename(word).lower()
    if base in FAMILIES:
        return FAMILIES[base]
    stem = re.sub(r"(\.[a-z]?[0-9][0-9a-z.]*)$", "", base)      # g16.b01, python3.11
    if stem in FAMILIES:
        return FAMILIES[stem]
    stem = re.sub(r"[0-9.]+$", "", base)                         # python3.11 -> python
    if stem in FAMILIES:
        return FAMILIES[stem]
    if base.startswith("vasp"):
        return "VASP"
    if base.startswith("lmp"):
        return "LAMMPS"
    if base.startswith("pmemd") or base.startswith("sander"):
        return "AMBER"
    if base.startswith("gmx"):
        return "GROMACS"
    return None


def _words(line: str) -> list:
    try:
        return shlex.split(line, comments=True)
    except ValueError:
        return line.split()


def commands_of(lines: list) -> list:
    """Each command of the script's lines: 'cd x && g16 < a.com' is two.
    Split on ;, && and || outside quotes; a here-document's body is skipped;
    a line continued with a backslash is joined to the next."""
    out = []
    heredoc = None
    joined, buf = [], None
    for line in lines or []:                    # a line ending in a backslash goes on
        line = line.rstrip()
        if line.endswith("\\"):
            buf = (buf + " " if buf is not None else "") + line[:-1].strip()
            continue
        joined.append(((buf + " ") if buf is not None else "") + line.strip())
        buf = None
    if buf is not None:
        joined.append(buf)
    for line in joined:
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        m = re.search(r"(?<!<)<<(?!<)-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", line)
        if m:
            heredoc = m.group(1)
        parts, buf, quote, i = [], "", None, 0
        while i < len(line):
            ch = line[i]
            if quote:
                buf += ch
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
                buf += ch
            elif ch == ";" or line[i:i + 2] in ("&&", "||"):
                parts.append(buf)
                buf = ""
                i += 1 if ch == ";" else 2
                continue
            else:
                buf += ch
            i += 1
        parts.append(buf)
        out += [p.strip() for p in parts if p.strip()]
    return out


def _strip_launchers(words: list) -> tuple[list, str | None]:
    """The command past srun/mpirun/time/containers and their options, and
    the (MPI) launcher it runs under."""
    i = 0
    launcher = None
    while i < len(words):
        w = words[i]
        base = posixpath.basename(w)
        if "=" in w and not w.startswith("-") and i == 0:
            i += 1                                  # VAR=value before a command
            continue
        name = _LAUNCH_ALIASES.get(base, _LAUNCH_ALIASES.get(w, base))
        if base in _LAUNCHERS or w in _LAUNCHERS:
            i += 1
            if name in _MPI:
                launcher = name
            if name == "container" and i < len(words) and words[i] in _CONTAINER_SUB:
                i += 1
            values = _LAUNCH_VALUES.get(name, set())
            while i < len(words) and (words[i].startswith("-") or words[i].startswith("+")):
                opt = words[i]
                i += 1
                if "=" in opt:
                    continue
                if opt in _LAUNCH_TWO and name == "mpirun":
                    i += 2
                elif opt in values:
                    i += 1
            if name == "container" and i < len(words) and (
                    words[i].endswith((".sif", ".simg", ".img")) or "://" in words[i]):
                i += 1                              # the image
            continue
        break
    return words[i:], launcher


def _input_of(family: str | None, words: list, line: str) -> str | None:
    """The input file a command names: a redirect '< file' (not a
    here-document), else the first argument with the family's usual
    extension, else its first argument that isn't an option."""
    m = re.search(r"(?<!<)<(?!<)\s*([^\s<>|;&]+)", line)
    if m:
        return m.group(1)
    if "<<" in line:
        return None
    exts = _INPUT_EXT.get(family or "", ())
    args = [w for w in words[1:] if not w.startswith("-") and not w.startswith("+")
            and w not in (">", ">>", "2>", "&>")]
    for w in words[1:]:
        if w.lower().endswith(tuple(e.lower() for e in exts)):
            return w
    if family in ("Gaussian", "ORCA", "Python", "R", "Julia") and args:
        a = args[0]
        if not a.startswith(">") and "$" not in a:
            return a
    return None


def program_of(lines: list) -> Program:
    """The program a script's command lines run (the first that isn't
    setting up), or Program(None, None) when none is recognised."""
    first = None
    cd = None
    for line in commands_of(lines):
        raw = _words(line)
        if raw and raw[0] in ("cd", "pushd"):
            if "-" in raw[1:2]:
                cd = "?"                            # cd -: back where it was, not followed
                continue
            args = [w for w in raw[1:] if not w.startswith("-")]
            target = args[0] if args else "~"
            home = re.fullmatch(r"\$\{?(?:SLURM_SUBMIT_DIR|PWD)\}?(/.*)?", target)
            if home:
                # the job's own folder (or a folder in it)
                rest = (home.group(1) or "").strip("/")
                cd = rest or None
            elif "$" in target:
                cd = "?"                            # another variable: not followed
            elif target.startswith(("/", "~")):
                cd = target
            elif cd == "?":
                pass                                # relative to a folder nomad doesn't know
            else:
                cd = target if cd is None else posixpath.join(cd, target)
            continue
        if raw and raw[0] == "popd":
            cd = "?"                                # back to where pushd was: not followed
            continue
        words, launcher = _strip_launchers(raw)
        if not words:
            continue
        exe = words[0]
        base = posixpath.basename(exe)
        if base in _SETUP or base.startswith("#") or re.match(r"[0-9]*[<>&|]", exe):
            continue                                # setting up, or only redirecting (exec > log)
        fam = _family(exe)
        if fam is None and base in ("bash", "sh") and len(words) > 1:
            continue
        if fam is None:
            if first is None:
                first = Program(None, base, line[:300], None, launcher=launcher, cd=cd)
            continue
        gpu = ".cuda" in base.lower() or "gpu" in base.lower() \
            or (fam == "GROMACS" and any(w in ("-nb", "-pme", "-bonded") for w in words)
                and "gpu" in words)
        return Program(fam, base, line[:300], _input_of(fam, words, line), gpu, launcher, cd)
    return first or Program(None, None)


# ── what an input file says ─────────────────────────────────────────────────

@dataclass
class Needs:
    """What an input file states: CPUs, memory (MB, for the whole run on one
    node) and the lines it was read from."""
    cpus: int | None = None
    mem_mb: int | None = None
    said: list = field(default_factory=list)       # the input's own lines, as written
    notes: list = field(default_factory=list)
    cpus_from: str | None = None                   # the line the CPUs come from
    mem_from: str | None = None                    # and the memory
    cpus_where: str = "input"                      # "input" (file) or "command" (its line)
    mem_where: str = "input"
    kind: str = "threads"                          # CPUs as threads of one task, or processes (tasks)

    def to_dict(self) -> dict:
        return {"cpus": self.cpus, "mem_mb": self.mem_mb, "said": list(self.said),
                "notes": list(self.notes), "cpus_from": self.cpus_from, "mem_from": self.mem_from,
                "cpus_where": self.cpus_where, "mem_where": self.mem_where, "kind": self.kind}


_G_MEM = re.compile(r"^\s*%mem\s*=\s*([0-9.]+)\s*([kmgt]?)(b|w)?\s*$", re.I)
_G_NPROC = re.compile(r"^\s*%(?:nprocshared|nprocs|nproc)\s*=\s*([0-9]+)\s*$", re.I)
_G_CPU = re.compile(r"^\s*%cpu\s*=\s*([0-9,\-\s]+)$", re.I)


def _cpu_list_count(text: str) -> int | None:
    n = 0
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        a, _, b = part.partition("-")
        if not a.isdigit() or (b and not b.isdigit()):
            return None
        n += (int(b) - int(a) + 1) if b else 1
    return n or None


def gaussian(text: str) -> Needs:
    """Link 0 lines (%...) before the route section (#): %mem (words are 8
    bytes), %nprocshared, %cpu."""
    out = Needs()
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith("#"):
            break                                    # the route section: Link 0 is over
        m = _G_MEM.match(line)
        if m:
            n = float(m.group(1))
            unit = (m.group(2) or "").lower()
            if (m.group(3) or "").lower() == "w":
                mult = {"": 8, "k": 8 * 1024, "m": 8 * 1024 ** 2, "g": 8 * 1024 ** 3,
                        "t": 8 * 1024 ** 4}[unit]
            else:
                mult = {"": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3, "t": 1024 ** 4}[unit]
                if not unit and not m.group(3):
                    mult = 8                          # a bare number is words
            out.mem_mb = int(round(n * mult / 1024 ** 2))
            out.said.append(line)
            out.mem_from = line
            continue
        m = _G_NPROC.match(line)
        if m:
            out.cpus = int(m.group(1))
            out.said.append(line)
            out.cpus_from = line
            continue
        m = _G_CPU.match(line)
        if m and out.cpus is None:
            out.cpus = _cpu_list_count(m.group(1))
            out.said.append(line)
            out.cpus_from = line
    if out.mem_mb is not None:
        stated = out.mem_mb
        out.mem_mb = int(round(stated * 1.2))
        out.notes.append(f"%mem {_mb_text(stated)}, and a fifth more: Gaussian needs room beyond %mem")
    elif out.said:
        out.notes.append("no %mem: Gaussian then uses its default (a few hundred MB)")
    return out


def _mb_text(mb: int) -> str:
    return f"{mb / 1024:g} GB" if mb >= 1024 else f"{mb} MB"


_O_MAXCORE = re.compile(r"^\s*%maxcore\s+([0-9]+)", re.I)
_O_PAL_LINE = re.compile(r"^\s*%pal\b(.*)$", re.I)
_O_NPROCS = re.compile(r"\bnprocs\s+([0-9]+)", re.I)
_O_BANG_PAL = re.compile(r"\bPAL([0-9]+)\b", re.I)


def orca(text: str) -> Needs:
    """%maxcore (MB per process) and the process count (%pal nprocs N end,
    or ! PALn). ORCA can use more than maxcore at times: nomad adds a
    quarter, as ORCA's manual advises leaving room."""
    out = Needs()
    lines = (text or "").splitlines()
    maxcore = None
    in_pal = False
    for raw in lines:
        line = raw.strip()
        if line.startswith("!"):
            m = _O_BANG_PAL.search(line)
            if m:
                out.cpus = int(m.group(1))
                out.said.append(line)
                out.cpus_from = m.group(0)
            continue
        m = _O_MAXCORE.match(line)
        if m:
            maxcore = int(m.group(1))
            out.said.append(line)
            out.mem_from = line
            continue
        m = _O_PAL_LINE.match(line)
        if m:
            in_pal = True
            rest = m.group(1)
            n = _O_NPROCS.search(rest)
            if n:
                out.cpus = int(n.group(1))
                out.said.append(line)
                out.cpus_from = f"%pal nprocs {out.cpus}"
            if re.search(r"\bend\b", rest, re.I):
                in_pal = False
            continue
        if in_pal:
            n = _O_NPROCS.search(line)
            if n:
                out.cpus = int(n.group(1))
                out.said.append(line)
                out.cpus_from = f"%pal nprocs {out.cpus}"
            if re.search(r"\bend\b", line, re.I):
                in_pal = False
    if maxcore:
        out.mem_mb = int(round(maxcore * (out.cpus or 1) * 1.25))
        out.notes.append(f"%maxcore {maxcore} MB × {out.cpus or 1} process"
                         f"{'' if (out.cpus or 1) == 1 else 'es'}, and a quarter more: ORCA can "
                         "use more than %maxcore")
    return out


_Q_MEM = re.compile(r"^\s*mem_total\s*=?\s*([0-9]+)\s*$", re.I)


def qchem(text: str) -> Needs:
    """MEM_TOTAL (MB) in a $rem section (the largest, for an input of
    several jobs: @@@)."""
    out = Needs()
    in_rem = False
    for raw in (text or "").splitlines():
        line = raw.split("!", 1)[0].strip()
        low = line.lower()
        if low.startswith("$rem"):
            in_rem = True
            continue
        if low.startswith("$end"):
            in_rem = False
            continue
        if in_rem:
            m = _Q_MEM.match(line)
            if m:
                mb = int(m.group(1))
                if out.mem_mb is None or mb > out.mem_mb:
                    out.mem_mb = mb
                    out.mem_from = line
                out.said.append(line)
    return out


def _flag(words: list, *names) -> str | None:
    """The value of a command's flag: '-nt 8' or '-nt=8' (the last given)."""
    found = None
    for i, w in enumerate(words):
        for n in names:
            if w == n and i + 1 < len(words):
                found = words[i + 1]
            elif w.startswith(n + "="):
                found = w.split("=", 1)[1]
    return found


def _number(v) -> int | None:
    return int(v) if v is not None and str(v).isdigit() and int(v) > 0 else None


def from_command(program: Program) -> Needs:
    """What the program's own command line sets: threads or processes, and
    memory where the program takes it there."""
    out = Needs()
    words, _launcher = _strip_launchers(_words(program.command or ""))
    whole = _words(program.command or "")
    fam = program.family
    if fam == "Q-Chem":
        nt, np_ = _number(_flag(words, "-nt")), _number(_flag(words, "-np"))
        out.cpus = nt or np_
        if out.cpus:
            out.said.append(f"qchem {'-nt' if nt else '-np'} {out.cpus}")
        out.kind = "processes" if np_ and not nt else "threads"
    elif fam == "Columbus":
        m, n = _number(_flag(words, "-m")), _number(_flag(words, "-nproc"))
        if n:
            out.cpus = n
            out.kind = "processes"
            out.said.append(f"runc -nproc {n}")
        if m:
            out.mem_mb = int(round(m * (n or 1) * 1.2))
            out.said.append(f"runc -m {m}")
            out.notes.append(f"runc -m {m} MB" + (f" for each of {n} processes" if n else "")
                             + ", and a fifth more")
    elif fam == "GROMACS":
        nt = _number(_flag(words, "-nt"))
        ntmpi, ntomp = _number(_flag(words, "-ntmpi")), _number(_flag(words, "-ntomp"))
        if nt:
            out.cpus = nt
            out.said.append(f"mdrun -nt {nt}")
        elif ntomp and (ntmpi or program.executable == "gmx"):
            out.cpus = (ntmpi or 1) * ntomp
            out.said.append(f"mdrun -ntmpi {ntmpi or 1} -ntomp {ntomp}")
    elif fam == "NAMD":
        for w in whole:
            m = re.fullmatch(r"\+p([0-9]+)", w)
            if m and int(m.group(1)) > 0:
                out.cpus = int(m.group(1))
                out.said.append(w)
        # Under charmrun, mpirun or srun, NAMD's +p counts processes; alone
        # (the multicore build), threads of one process.
        out.kind = "processes" if program.launcher in ("charmrun", "mpirun", "srun") else "threads"
    for x in out.said:
        if x.startswith("runc -m"):
            out.mem_from = x
        else:
            out.cpus_from = x
    return out


READERS = {"Gaussian": gaussian, "ORCA": orca, "Q-Chem": qchem}
COMMANDS = ("Q-Chem", "Columbus", "GROMACS", "NAMD")


def needs(program: Program, read) -> Needs | None:
    """What the program says it needs: its input file (when nomad knows how
    to read that program's input and the file can be read: ``read(path) ->
    text or None``) and its command line. None when neither says anything."""
    out = Needs()
    if program.family in COMMANDS:
        got = from_command(program)
        out.cpus, out.mem_mb = got.cpus, got.mem_mb
        out.cpus_from, out.mem_from = got.cpus_from, got.mem_from
        out.cpus_where = out.mem_where = "command"
        out.kind = got.kind
        out.said += got.said
        out.notes += got.notes
    procs = out.kind == "processes"
    reader = READERS.get(program.family or "")
    if reader is not None and program.input and "$" not in program.input:
        text = read(program.input)
        if text is not None:
            got = reader(text)
            if program.family == "Q-Chem" and got.mem_mb:
                each = got.mem_mb
                n = out.cpus if procs and out.cpus else 1
                got.mem_mb = int(round(each * n * 1.2))
                got.notes.append(f"MEM_TOTAL {each} MB" + (f" for each of {n} processes" if n > 1 else "")
                                 + ", and a fifth more")
            if got.cpus:
                out.cpus, out.cpus_from, out.cpus_where = got.cpus, got.cpus_from, "input"
                if program.family == "ORCA":
                    out.kind = "processes"           # ORCA starts its own MPI processes
            if got.mem_mb:
                out.mem_mb, out.mem_from, out.mem_where = got.mem_mb, got.mem_from, "input"
            out.said += got.said
            out.notes += got.notes
    return out if (out.cpus or out.mem_mb) else None

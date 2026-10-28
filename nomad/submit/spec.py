# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What a job asks for: its script's #SBATCH lines, or the command line it
was submitted with, read as sbatch reads them (no shell ever runs either).

A ``Request`` is what sizing works on, kept in the job's own forms: tasks,
CPUs per task and tasks per node apart; memory per node, per CPU or per GPU
(whichever sbatch would use); GPUs as --gpus, --gpus-per-node,
--gpus-per-task, --tres-per-task or --gres, with their type. None: the job
doesn't say (Slurm's defaults apply).
"""
from __future__ import annotations

import math
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

# ── sizes and times ─────────────────────────────────────────────────────────

_MEM_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?)(?:i?B)?", re.I)


def parse_memory_mb(text: Any) -> int | None:
    """'8G', '512M', '1.5T', '4000' (Slurm's default unit, MB) -> MB; None
    for anything else. '0' is 0 (Slurm: all of a node's memory)."""
    if text is None:
        return None
    t = str(text).strip()
    t = re.sub(r"(?<=[0-9KMGTkmgt])[nc]$", "", t)       # sacct's old per-node / per-CPU suffix
    m = _MEM_RE.fullmatch(t)
    if not m:
        return None
    n = float(m.group(1))
    mb = {"": 1, "K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}[m.group(2).upper()] * n
    if mb == 0:
        return 0
    return max(1, int(math.ceil(mb))) if mb > 0 else None


def slurm_mem(mb: float) -> str:
    """Memory as sbatch takes it: a whole number of G when it is one, else
    a whole number of M ('20G', '1536M'); never a fraction."""
    mb = int(math.ceil(mb))
    if mb >= 1024 and mb % 1024 == 0:
        return f"{mb // 1024}G"
    return f"{mb}M"


def mem_text(mb: float | None) -> str | None:
    """Memory for people: '20G', '1.5G', '512M'."""
    if mb is None:
        return None
    if mb == 0:
        return "all of a node"
    if mb >= 1024:
        g = mb / 1024
        return f"{g:.0f}G" if abs(g - round(g)) < 0.05 else f"{g:.1f}G"
    return f"{int(mb)}M"


_FRAC = r"[0-9]{1,2}(?:\.[0-9]+)?"
_TIME_RES = (
    (re.compile(r"([0-9]+)"), lambda d: int(d[0]) * 60),
    (re.compile(rf"([0-9]+):({_FRAC})"), lambda d: int(d[0]) * 60 + int(float(d[1]))),
    (re.compile(rf"([0-9]+):([0-9]{{1,2}}):({_FRAC})"),
     lambda d: int(d[0]) * 3600 + int(d[1]) * 60 + int(float(d[2]))),
    (re.compile(r"([0-9]+)-([0-9]{1,2})"), lambda d: int(d[0]) * 86400 + int(d[1]) * 3600),
    (re.compile(r"([0-9]+)-([0-9]{1,2}):([0-9]{1,2})"),
     lambda d: int(d[0]) * 86400 + int(d[1]) * 3600 + int(d[2]) * 60),
    (re.compile(rf"([0-9]+)-([0-9]{{1,2}}):([0-9]{{1,2}}):({_FRAC})"),
     lambda d: int(d[0]) * 86400 + int(d[1]) * 3600 + int(d[2]) * 60 + int(float(d[3]))),
)


def parse_time_seconds(text: Any) -> int | None:
    """Slurm's time forms (minutes, MM:SS, HH:MM:SS, D-HH, D-HH:MM,
    D-HH:MM:SS; sacct's TotalCPU carries fractions of a second, MM:SS.mmm
    under an hour) -> seconds; None for anything else, and for UNLIMITED."""
    if text is None:
        return None
    t = str(text).strip()
    for rx, f in _TIME_RES:
        m = rx.fullmatch(t)
        if m:
            return f(m.groups())
    return None


# ── sbatch's options ────────────────────────────────────────────────────────

# sbatch's options that take a value (long forms; short ones below). An option
# not here and written without '=' is read as taking none.
_LONG_VALUE = frozenset("""
account acctg-freq array batch bb bbf begin chdir cluster-constraint clusters comment constraint
container container-id core-spec cores-per-socket cpu-freq cpus-per-gpu cpus-per-task deadline
delay-boot dependency distribution error exclude export export-file extra extra-node-info gid
gpu-bind gpu-freq gpus gpus-per-node gpus-per-socket gpus-per-task gres gres-flags hint input
job-name kill-on-invalid-dep licenses mail-type mail-user mcs-label mem mem-bind mem-per-cpu
mem-per-gpu mincpus network nodefile nodelist nodes ntasks ntasks-per-core ntasks-per-gpu
ntasks-per-node ntasks-per-socket open-mode output partition power prefer priority profile qos
reservation signal sockets-per-node switches thread-spec threads-per-core time time-min tmp
tres-bind tres-per-task uid wait-all-nodes wckey wrap
""".split())
# Options taking a value only when written --name=value (sbatch's optional
# arguments): --nice job.sh is --nice, then the script.
_LONG_NONE = frozenset("""
exclusive requeue no-requeue hold wait quiet verbose parsable test-only overcommit contiguous
spread-job oversubscribe use-min-nodes ignore-pbs get-user-env no-kill propagate reboot help usage
version nice resv-ports
""".split())
_SHORT_VALUE = frozenset("AabBcCDdeFGiJLMmNnopqStwx")
_SHORT_NONE = frozenset("hHIkOQsvVW")
_SHORT_LONG = {"A": "account", "a": "array", "b": "begin", "B": "extra-node-info", "c": "cpus-per-task",
               "C": "constraint", "D": "chdir", "d": "dependency", "e": "error", "i": "input",
               "J": "job-name", "L": "licenses", "M": "clusters", "m": "distribution", "N": "nodes",
               "n": "ntasks", "o": "output", "p": "partition", "q": "qos", "S": "core-spec",
               "t": "time", "w": "nodelist", "x": "exclude", "G": "gpus", "F": "nodefile"}
_SHORT_NONE_LONG = {"H": "hold", "W": "wait", "Q": "quiet", "v": "verbose", "O": "overcommit",
                    "s": "oversubscribe", "k": "no-kill", "I": "immediate", "h": "help",
                    "V": "version"}


@dataclass
class SubmitLine:
    """What `sbatch` was given: its options (long name, value or None), the
    script and its arguments, or --wrap's command."""
    options: list = field(default_factory=list)
    script: str | None = None
    args: list = field(default_factory=list)
    wrap: str | None = None
    rest: list = field(default_factory=list)          # every word after the options
    unsure: list = field(default_factory=list)        # what couldn't be read with certainty
    readable: bool = True

    def get(self, name: str) -> str | None:
        """The last value given for an option (sbatch takes the last)."""
        found = None
        for n, v in self.options:
            if n == name:
                found = v
        return found

    def has(self, name: str) -> bool:
        return any(n == name for n, _ in self.options)

    def last_of(self, *names) -> str | None:
        """Which of these options was given last (sbatch uses the last)."""
        found = None
        for n, _ in self.options:
            if n in names:
                found = n
        return found

    def to_dict(self) -> dict:
        return {"options": [list(o) for o in self.options], "script": self.script,
                "args": list(self.args), "wrap": self.wrap, "unsure": list(self.unsure),
                "readable": self.readable}


def _options(words: list, out: SubmitLine) -> int:
    """Read sbatch options from ``words`` into ``out``; the index of the
    first word that isn't one (the script)."""
    i = 0
    while i < len(words):
        w = words[i]
        if w == "--":
            return i + 1
        if w.startswith("--") and len(w) > 2:
            name, eq, value = w[2:].partition("=")
            if eq:
                out.options.append((name, value))
            elif name in _LONG_VALUE and i + 1 < len(words):
                out.options.append((name, words[i + 1]))
                i += 1
            else:
                if name not in _LONG_VALUE and name not in _LONG_NONE:
                    out.unsure.append(f"--{name}: an option nomad doesn't know; read as taking no value")
                out.options.append((name, None))
            i += 1
            continue
        if w.startswith("-") and len(w) > 1:
            c = w[1]
            if c in _SHORT_VALUE:
                long = _SHORT_LONG.get(c, c)
                if len(w) > 2:
                    out.options.append((long, w[2:].lstrip("=")))
                elif i + 1 < len(words):
                    out.options.append((long, words[i + 1]))
                    i += 1
                else:
                    out.unsure.append(f"-{c} without its value")
            elif c in _SHORT_NONE:
                for ch in w[1:]:
                    out.options.append((_SHORT_NONE_LONG.get(ch, ch), None))
            else:
                out.unsure.append(f"-{c}: an option nomad doesn't know")
                out.options.append((c, None))
            i += 1
            continue
        return i
    return i


def parse_submit_line(line: str | None) -> SubmitLine:
    """Read a submit command line as sbatch would (no shell runs it). Slurm
    keeps it without the quotes it was typed with, so a value or argument
    with a space in it can't be read back with certainty."""
    out = SubmitLine()
    try:
        words = shlex.split(line or "")
    except ValueError:
        out.readable = False
        out.unsure.append("the command line has unbalanced quotes (Slurm keeps it without its quotes)")
        return out
    if not words or posixpath.basename(words[0]) != "sbatch":
        out.readable = False
        out.unsure.append("not an sbatch command line" if words else "no command line recorded")
        return out
    i = 1 + _options(words[1:], out)
    rest = words[i:]
    out.rest = list(rest)
    out.wrap = out.get("wrap")
    if out.wrap is not None:
        out.options = [o for o in out.options if o[0] != "wrap"]
        if rest:
            out.unsure.append("words after --wrap's command")
        return out
    if rest and rest[0][:1] in ("<", ">", "|", ";", "&"):
        out.readable = False
        out.unsure.append("the script came on standard input or through the shell, not by name")
        return out
    if rest:
        out.script, out.args = rest[0], rest[1:]
    return out


# ── a job script ────────────────────────────────────────────────────────────

MAX_SCRIPT = 256 * 1024            # a job script larger than this isn't read


def sbatch_lines(text: str) -> tuple[list[str], list[str]]:
    """The #SBATCH options of a job script (sbatch reads them until the
    first command, and only at the very start of a line: an indented
    #SBATCH is a comment to it), and the script's command lines (comments
    and blank lines left out; a line ending in a backslash joined to the
    next, as the shell does)."""
    opts, cmds = [], []
    seen_cmd = False
    buf = None
    for i, raw in enumerate((text or "").splitlines()):
        line = raw.strip()
        if buf is not None:
            if line.endswith("\\"):
                buf += " " + line[:-1].strip()
                continue
            cmds.append((buf + " " + line).strip()[:1000])
            buf = None
            continue
        if i == 0 and line.startswith("#!"):
            continue
        if not line:
            continue
        if line.startswith("#"):
            if not seen_cmd and re.match(r"#SBATCH\s", raw):
                opts.append(raw[len("#SBATCH"):].strip())
            continue
        seen_cmd = True
        if line.endswith("\\"):
            buf = line[:-1].rstrip()
            continue
        cmds.append(line[:1000])
    if buf:
        cmds.append(buf[:1000])
    return opts, cmds


def script_options(text: str) -> SubmitLine:
    """A script's #SBATCH lines, read as sbatch reads them (a comment after
    an option is left out; a quoted # is part of the value)."""
    out = SubmitLine()
    for o in sbatch_lines(text)[0]:
        try:
            words = shlex.split(o, comments=True)
        except ValueError:
            out.unsure.append(f"#SBATCH {o[:60]}: unbalanced quotes; skipped")
            continue
        _options(words, out)
    return out


# ── the request ─────────────────────────────────────────────────────────────

def _int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _gpu_count(value) -> tuple[int | None, list]:
    """'2' -> (2, []); 'a100:2' -> (2, ['a100']); 'a100' -> (1, ['a100']);
    'a100:1,v100:1' -> (2, ['a100', 'v100'])."""
    total, kinds = None, []
    for part in str(value or "").split(","):
        bits = [b for b in part.strip().split(":") if b]
        if not bits:
            continue
        n = _int(bits[-1])
        kind = bits[-1] if n is None else (bits[-2] if len(bits) > 1 else None)
        total = (total or 0) + (1 if n is None else n)
        if kind and kind not in kinds:
            kinds.append(kind)
    return total, kinds


GPU_FORMS = ("gpus", "gpus-per-node", "gpus-per-task", "tres-per-task", "gres")


def _gres_gpu(value) -> bool:
    return any(p.strip().split(":")[0] == "gpu" for p in str(value or "").split(","))


def _tres_gpu(value) -> bool:
    return any(p.strip().startswith(("gres/gpu", "gres:gpu")) for p in str(value or "").split(","))


def _asks_gpus(name: str, value) -> bool:
    if name == "gres":
        return _gres_gpu(value)
    if name == "tres-per-task":
        return _tres_gpu(value)
    n, _ = _gpu_count(value)
    return bool(n)


@dataclass
class Request:
    """What a job asks for, in its own forms; None: it doesn't say."""
    ntasks: int | None = None
    cpus_per_task: int | None = None
    ntasks_per_node: int | None = None
    nodes: int | None = None
    mem_mb: int | None = None           # --mem, per node (0: all of a node's memory)
    mem_per_cpu_mb: int | None = None
    mem_per_gpu_mb: int | None = None
    mem_form: str | None = None         # the one sbatch uses: mem, mem-per-cpu, mem-per-gpu
    time_s: int | None = None
    gpus: int | None = None             # in all
    gpu_form: str | None = None         # gpus, gpus-per-node, gpus-per-task, tres-per-task, gres
    gpu_type: str | None = None
    gpu_types: list = field(default_factory=list)    # more than one: GPUs of several kinds
    cpus_per_gpu: int | None = None
    cpus_tres: bool = False             # CPUs per task asked as --tres-per-task=cpu=N
    time_unlimited: bool = False        # --time=UNLIMITED
    partition: str | None = None        # the first named (sbatch takes a list: -p a,b)
    partition_alts: list = field(default_factory=list)   # all of them
    account: str | None = None
    job_name: str | None = None
    array: str | None = None

    # ── derived ──
    @property
    def tasks(self) -> int:
        """Tasks in all: --ntasks, else --ntasks-per-node on each node
        (one node when --nodes isn't given), else one."""
        if self.ntasks:
            return self.ntasks
        if self.ntasks_per_node:
            return self.ntasks_per_node * (self.nodes or 1)
        return 1

    @property
    def spread(self) -> bool:
        """Several tasks Slurm places over as many nodes as it takes (no
        --nodes, no --ntasks-per-node): what one node gets isn't known."""
        return self.tasks > 1 and not self.nodes and not self.ntasks_per_node

    @property
    def per_gpu(self) -> bool:
        """CPUs asked per GPU (--cpus-per-gpu, without --cpus-per-task)."""
        return bool(self.cpus_per_gpu and self.gpus and not self.cpus_per_task)

    @property
    def cpus(self) -> int | None:
        """CPUs in all; None when the job asks nothing about CPUs."""
        if self.per_gpu:
            return self.cpus_per_gpu * self.gpus
        if not (self.ntasks or self.cpus_per_task or self.ntasks_per_node):
            return None
        return self.tasks * (self.cpus_per_task or 1)

    @property
    def multi_node(self) -> bool:
        return (self.nodes or 1) > 1

    @property
    def threaded(self) -> bool:
        """One task with several CPUs (or a single CPU): what sizing may
        change by CPUs per task."""
        return self.tasks == 1

    def tasks_per_node(self) -> int | None:
        if self.ntasks_per_node:
            return self.ntasks_per_node
        if self.nodes:
            return -(-self.tasks // self.nodes)
        return self.tasks if self.tasks == 1 else None   # spread as Slurm places it

    def cpus_per_node(self, node_cpus: int | None = None) -> int | None:
        """CPUs on one node: known for one node, or with --nodes; else at
        most what a node has."""
        if self.cpus is None:
            return None
        if self.per_gpu:
            g = self.gpus_per_node()
            if g:
                return g * self.cpus_per_gpu
            return min(self.cpus, node_cpus) if node_cpus else self.cpus
        tpn = self.tasks_per_node()
        if tpn is not None:
            return tpn * (self.cpus_per_task or 1)
        return min(self.cpus, node_cpus) if node_cpus else self.cpus

    def gpus_per_node(self) -> int | None:
        """GPUs one node must have: known per node (--gpus-per-node,
        --gres), with --nodes, or for one task (a task runs on one node);
        else None (Slurm spreads them)."""
        if not self.gpus:
            return self.gpus
        if self.gpu_form in ("gpus-per-node", "gres") or self.nodes:
            return -(-self.gpus // max(1, self.nodes or 1))
        if self.tasks == 1:
            return self.gpus
        return None

    def least_gpus_per_node(self) -> int | None:
        """The fewest GPUs a node running part of the job must have."""
        g = self.gpus_per_node()
        if g is not None or not self.gpus:
            return g
        if self.gpu_form in ("gpus-per-task", "tres-per-task"):
            return -(-self.gpus // max(1, self.tasks))
        return 1

    def mem_per_node(self, node_cpus: int | None = None) -> int | None:
        """Memory per node, however it was asked (None: not asked; 0: all of
        a node's)."""
        if self.mem_form == "mem":
            return self.mem_mb
        if self.mem_form == "mem-per-cpu":
            c = self.cpus_per_node(node_cpus) or 1
            return self.mem_per_cpu_mb * c
        if self.mem_form == "mem-per-gpu":
            g = self.gpus_per_node() or self.gpus or 1
            return self.mem_per_gpu_mb * g
        return None

    def to_dict(self) -> dict:
        out = {k: getattr(self, k) for k in self.__dataclass_fields__}
        out.update(tasks=self.tasks, cpus=self.cpus, multi_node=self.multi_node,
                   mem_per_node=self.mem_per_node())
        return out


def request_of(sub: SubmitLine) -> Request:
    """The request a set of sbatch options makes (the last value of each,
    and the last-given of memory's three forms, as sbatch uses them)."""
    nodes = sub.get("nodes")
    alts = [x.strip() for x in (sub.get("partition") or "").split(",") if x.strip()]
    req = Request(
        ntasks=_int(sub.get("ntasks")), cpus_per_task=_int(sub.get("cpus-per-task")),
        ntasks_per_node=_int(sub.get("ntasks-per-node")),
        nodes=_int(str(nodes).split("-")[0]) if nodes else None,
        time_s=parse_time_seconds(sub.get("time")) if sub.get("time") else None,
        partition=alts[0] if alts else None, partition_alts=alts,
        cpus_per_gpu=_int(sub.get("cpus-per-gpu")),
        account=sub.get("account"), job_name=sub.get("job-name"), array=sub.get("array"))
    t = sub.get("time")
    if t is not None and (str(t).strip().upper() in ("UNLIMITED", "INFINITE", "-1")
                          or parse_time_seconds(t) == 0):
        req.time_unlimited, req.time_s = True, None      # sbatch: a time of 0 (and -1) means no limit too
    if req.cpus_per_task is None:
        for part in str(sub.get("tres-per-task") or "").split(","):
            k, _, v = part.strip().partition("=")
            if k == "cpu" and _int(v):
                req.cpus_per_task, req.cpus_tres = _int(v), True
    form = sub.last_of("mem", "mem-per-cpu", "mem-per-gpu")
    if form:
        v = parse_memory_mb(sub.get(form))
        if v is not None:
            req.mem_form = form
            setattr(req, {"mem": "mem_mb", "mem-per-cpu": "mem_per_cpu_mb",
                          "mem-per-gpu": "mem_per_gpu_mb"}[form], v)
    # GPUs: of the forms that ask for some (each option's last value: a later
    # --gres=lscratch:10 doesn't hide an earlier --gpus=1), the one given last.
    asking = [n for n in GPU_FORMS if _asks_gpus(n, sub.get(n))]
    gform = sub.last_of(*asking) if asking else None
    kinds: list = []
    if gform in ("gpus", "gpus-per-node", "gpus-per-task"):
        n, kinds = _gpu_count(sub.get(gform))
        if n is not None:
            mult = {"gpus": 1, "gpus-per-node": req.nodes or 1, "gpus-per-task": req.tasks}[gform]
            req.gpus, req.gpu_form = n * mult, gform
    elif gform == "tres-per-task":
        total = None
        for part in str(sub.get(gform) or "").split(","):
            k, _, v = part.strip().partition("=")
            if k.startswith("gres/gpu") or k.startswith("gres:gpu"):
                kind = k.split(":", 1)[1] if ":" in k else None
                if kind and kind not in kinds:
                    kinds.append(kind)
                total = (total or 0) + ((_int(v) if v else 1) or 1)
        if total:
            req.gpus, req.gpu_form = total * req.tasks, gform
    elif gform == "gres":
        total = None
        for part in str(sub.get("gres") or "").split(","):
            bits = re.sub(r"\(.*?\)", "", part.strip()).split(":")
            if bits and bits[0] == "gpu":
                n = _int(bits[-1]) if len(bits) > 1 else 1
                kind = bits[1] if (len(bits) == 3 or (len(bits) == 2 and n is None)) else None
                if kind and kind not in kinds:
                    kinds.append(kind)
                total = (total or 0) + (n if n is not None else 1)
        if total is not None:
            req.gpus, req.gpu_form = total * (req.nodes or 1), "gres"
    req.gpu_types = kinds
    req.gpu_type = kinds[0] if len(kinds) == 1 else None
    return req


def merged(script: SubmitLine, line: SubmitLine) -> SubmitLine:
    """The options a job ran with: the script's #SBATCH lines, then the
    command line's (sbatch lets the command line win)."""
    return SubmitLine(options=list(script.options) + list(line.options), script=line.script,
                      args=list(line.args), wrap=line.wrap, rest=list(line.rest),
                      unsure=list(script.unsure) + list(line.unsure), readable=line.readable)

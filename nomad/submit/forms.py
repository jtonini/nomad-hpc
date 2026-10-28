# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""sbatch options for a value, written the way the job already asks for it,
so the command line never asks in a second way that sbatch would refuse or
add up: memory per node, per CPU or per GPU; CPUs as threads of one task or
as tasks; GPUs as --gpus, --gpus-per-node, --gpus-per-task, --tres-per-task
or --gres, with their type and the job's other gres kept.

Each function gives ([(option, value)], options it replaces): the second
is what a job's own command line loses when it is sent again.
"""
from __future__ import annotations

import math

from nomad.edu.scoring import format_time_slurm
from nomad.submit.spec import Request, SubmitLine, slurm_mem

MEM_OPTIONS = ("mem", "mem-per-cpu", "mem-per-gpu")
CPU_OPTIONS = ("ntasks", "cpus-per-task", "ntasks-per-node")
GPU_OPTIONS = ("gpus", "gpus-per-node", "gpus-per-task", "tres-per-task", "gres")


def _up(mb: float, step: int = 64) -> int:
    return int(math.ceil(mb / step) * step)


def mem(req: Request, per_node_mb: int, node_cpus: int | None = None,
        at_most: bool = False) -> tuple[list, tuple]:
    """Memory per node, as the job asks it (per CPU and per GPU rounded up
    to 64 MB, or down to whole MB when ``at_most``: a node's memory, which
    rounding up would pass; never a fraction sbatch refuses). ``req``: the
    job as it will be sent, CPUs and GPUs included. 0: all of a node's."""
    if per_node_mb == 0:
        return [("mem", "0")], MEM_OPTIONS

    def each(n: int) -> str:
        return slurm_mem(max(1, math.floor(per_node_mb / n)) if at_most else _up(per_node_mb / n))
    if req.mem_form == "mem-per-cpu":
        return [("mem-per-cpu", each(req.cpus_per_node(node_cpus) or 1))], MEM_OPTIONS
    if req.mem_form == "mem-per-gpu":
        return [("mem-per-gpu", each(req.gpus_per_node() or req.gpus or 1))], MEM_OPTIONS
    return [("mem", slurm_mem(per_node_mb))], MEM_OPTIONS


def cpus(req: Request, n: int, kind: str) -> tuple[list, tuple]:
    """``n`` CPUs: ``kind`` "threads" (one task, n CPUs: --cpus-per-task),
    "processes" (n tasks: --ntasks), or "asked" (the way the job asks:
    CPUs per task for one task, else tasks). ValueError when the job asks
    CPUs per GPU (sbatch takes no --cpus-per-task with that)."""
    if req.per_gpu:
        raise ValueError("the job asks CPUs per GPU (--cpus-per-gpu): change that in the script")
    if req.cpus_tres:
        raise ValueError("the job asks CPUs through --tres-per-task: change that in the script")
    if kind == "asked":
        cpt = req.cpus_per_task or 1
        if req.tasks > 1 and cpt > 1:
            # tasks of several CPUs each: more or fewer tasks, as many CPUs each
            if n % cpt:
                raise ValueError(f"the job asks {req.tasks} tasks of {cpt} CPUs: give a multiple of {cpt}")
            out = _tasks(req, n // cpt, cpt)
            return out, tuple(o for o, _ in out)
        kind = "threads" if req.tasks == 1 else "processes"
    out = []
    if kind == "threads":
        if req.tasks != 1:
            out.append(("ntasks", "1"))
        if req.ntasks_per_node and req.ntasks_per_node != 1:
            out.append(("ntasks-per-node", "1"))
        out.append(("cpus-per-task", str(n)))
        return out, tuple(o for o, _ in out)
    out = _tasks(req, n, 1)
    if (req.cpus_per_task or 1) > 1:
        out.append(("cpus-per-task", "1"))       # n processes, a CPU each
    return out, tuple(o for o, _ in out)


def _tasks(req: Request, t: int, cpt: int) -> list:
    """``t`` tasks, as the job asks them: per node when it asks only tasks
    per node (then on its nodes: a multiple of them); else in all (with
    --ntasks, --ntasks-per-node is a most a node, and stays)."""
    if req.ntasks_per_node and not req.ntasks:
        nodes = req.nodes or 1
        if t % nodes:
            raise ValueError(f"the job asks tasks per node on {nodes} nodes: give a multiple of {nodes * cpt}")
        return [("ntasks-per-node", str(t // nodes))]
    return [("ntasks", str(t))]


def gpus(req: Request, sub: SubmitLine, n: int) -> tuple[list, tuple]:
    """``n`` GPUs in all, the way the job asks for them (its GPU type and
    its other gres kept). ValueError for what that way can't say: none at
    all (nomad can't take an option away), a number that doesn't divide
    among the tasks or nodes it is asked per, GPUs of several kinds."""
    if n < 1:
        raise ValueError("nomad can't take away the GPUs the script asks for: remove its GPU "
                         "option (#SBATCH --gres, --gpus...) to run without")
    if len(req.gpu_types) > 1:
        raise ValueError(f"the job asks GPUs of several kinds ({', '.join(req.gpu_types)}): "
                         "change them in the script")
    kind = f"{req.gpu_type}:" if req.gpu_type else ""
    form = req.gpu_form

    def per(units: int, what: str) -> int:
        units = max(1, units)
        if n % units:
            raise ValueError(f"the job asks GPUs per {what} ({units} {what}s): give a multiple of {units}")
        return n // units
    # Each replaces only the option it writes: the job's other gres (on its
    # command line too) stay.
    if form == "gpus":
        if (req.nodes or 1) > n:
            raise ValueError(f"--gpus asks at least one GPU a node ({req.nodes} nodes): give {req.nodes} or more")
        return [("gpus", f"{kind}{n}")], ("gpus",)
    if form == "gpus-per-node":
        return [("gpus-per-node", f"{kind}{per(req.nodes or 1, 'node')}")], ("gpus-per-node",)
    if form == "gpus-per-task":
        return [("gpus-per-task", f"{kind}{per(req.tasks, 'task')}")], ("gpus-per-task",)
    if form == "tres-per-task":
        each = per(req.tasks, "task")
        parts = []
        for part in str(sub.get("tres-per-task") or "").split(","):
            k = part.strip().partition("=")[0]
            if k.startswith(("gres/gpu", "gres:gpu")):
                parts.append(f"{k}={each}")
            elif part.strip():
                parts.append(part.strip())
        return [("tres-per-task", ",".join(parts))], ("tres-per-task",)
    each = per(req.nodes or 1, "node")          # --gres is per node
    parts, done = [], False
    for part in str(sub.get("gres") or "").split(","):
        bits = part.strip().split(":")
        if bits and bits[0] == "gpu":
            if not done:
                parts.append(f"gpu:{kind}{each}")
                done = True
        elif part.strip():
            parts.append(part.strip())
    if not done:
        parts.append(f"gpu:{kind}{each}")
    return [("gres", ",".join(parts))], ("gres",)


def time(seconds: int) -> tuple[list, tuple]:
    return [("time", format_time_slurm(seconds))], ("time",)


def partition(name: str) -> tuple[list, tuple]:
    return [("partition", name)], ("partition",)


def as_args(opts: list) -> list:
    return [f"--{k}={v}" for k, v in opts]

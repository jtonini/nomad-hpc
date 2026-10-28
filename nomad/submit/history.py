# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A person's past jobs, with what each asked for and used: the evidence
sizing rests on.

Two sources, one shape (``PastRun``):

- ``from_sacct(user)``: Slurm's own accounting, read by the person's own run
  of nomad on a login node (Slurm shows everyone their own jobs). The full
  folder and submit line come with it, so a past job's script can be found
  and its program named; nothing is stored.
- ``from_db(path, user)``: nomad's database (the hub's, for the Console),
  which keeps measurements but never a whole folder or command line, so
  jobs are compared by name only there.
"""
from __future__ import annotations

import posixpath
import re
import subprocess
from dataclasses import dataclass

from nomad.submit.spec import parse_memory_mb, parse_submit_line, parse_time_seconds

ENDED = ("COMPLETED", "FAILED", "TIMEOUT", "OUT_OF_MEMORY")
USER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SEP = "\x1f"                       # sacct's delimiter: a job name or command can hold '|'


@dataclass
class PastRun:
    job_id: str
    state: str
    site: str | None = None
    job_name: str | None = None
    partition: str | None = None
    submitted: str | None = None
    cpus: int | None = None            # allocated (or asked) CPUs
    mem_mb: int | None = None          # memory per node, asked
    time_s: int | None = None          # time limit
    gpus: int | None = None
    nodes: int | None = None
    tasks: int | None = None           # the most tasks one of its steps ran
    elapsed_s: int | None = None
    peak_mem_mb: float | None = None   # the most memory used on one node (estimated)
    cpu_s: float | None = None         # CPU time used, every core added up
    workdir: str | None = None         # whole folder: sacct only
    folder_tail: str | None = None     # the folder's last two parts (what nomad keeps)
    script: str | None = None          # the script as submitted, made absolute: sacct only
    submit_line: str | None = None
    exit_code: str | None = None
    array_job: str | None = None       # an array's task: the array's number ("5005" of 5005_7)
    array_task: str | None = None      # ... and the task's ("7")
    family: str | None = None          # the program, once known (Program.family)
    executable: str | None = None

    @property
    def busy_cores(self) -> float | None:
        """Cores kept busy on average (CPU time over elapsed time)."""
        if self.cpu_s is None or not self.elapsed_s:
            return None
        return self.cpu_s / self.elapsed_s

    def to_dict(self) -> dict:
        out = {k: getattr(self, k) for k in self.__dataclass_fields__}
        out["busy_cores"] = self.busy_cores
        return out


def _int(v) -> int | None:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def tres(text) -> dict:
    """'billing=8,cpu=8,gres/gpu=1,mem=32G,node=1' -> {"cpus": 8,
    "mem_mb": 32768, "gpus": 1, "nodes": 1} (what is there)."""
    out: dict = {}
    typed = 0
    for part in str(text or "").split(","):
        k, _, v = part.strip().partition("=")
        if k == "cpu" and _int(v) is not None:
            out["cpus"] = _int(v)
        elif k == "mem":
            mb = parse_memory_mb(v)
            if mb is not None:
                out["mem_mb"] = mb
        elif k == "gres/gpu" and _int(v) is not None:
            out["gpus"] = _int(v)
        elif k.startswith("gres/gpu:") and _int(v) is not None:
            typed += _int(v)
        elif k == "node" and _int(v) is not None:
            out["nodes"] = _int(v)
    if "gpus" not in out and typed:
        out["gpus"] = typed
    return out


def folder_tail(path: str | None) -> str | None:
    parts = [p for p in str(path or "").split("/") if p]
    return "/".join(parts[-2:]) if parts else None


def script_path(submit_line: str | None, workdir: str | None) -> str | None:
    """The script a job was submitted with, as a full path (sbatch's
    --chdir changes the folder, not where the script was found: a relative
    script is read from the folder sbatch ran in, which nomad doesn't know
    then; None)."""
    sub = parse_submit_line(submit_line)
    if not sub.readable or not sub.script or sub.wrap is not None:
        return None
    if sub.script.startswith("/"):
        return posixpath.normpath(sub.script)
    if sub.get("chdir") or not workdir:
        return None
    return posixpath.normpath(posixpath.join(workdir, sub.script))


# ── Slurm's accounting, as the person ───────────────────────────────────────

_FIELDS = ("JobIDRaw", "JobID", "JobName", "State", "Partition", "AllocCPUS", "AllocTRES", "Timelimit",
           "Elapsed", "TotalCPU", "MaxRSS", "NTasks", "NNodes", "Submit", "ExitCode", "WorkDir",
           "SubmitLine")


def _run(argv: list, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                          stdin=subprocess.DEVNULL)


class HistoryError(Exception):
    """What couldn't be read (said as it is)."""


def _sacct_rows(argv: list, run) -> list[dict]:
    try:
        r = run(argv)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HistoryError(f"sacct could not run here: {e}") from None
    fields = _FIELDS
    if r.returncode != 0 and "invalid field" in (r.stderr or "").lower():
        # A Slurm before 21.08: no SubmitLine (and so no script to find).
        fields = _FIELDS[:-1]
        argv = [a if not a.startswith("--format=") else "--format=" + ",".join(fields) for a in argv]
        try:
            r = run(argv)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise HistoryError(f"sacct could not run here: {e}") from None
    if r.returncode != 0:
        raise HistoryError(f"sacct failed: {(r.stderr or '').strip()[-300:] or r.returncode}")
    out = []
    n = len(fields)
    for line in (r.stdout or "").splitlines():
        if not line.strip():
            continue
        parts = line.split(SEP)
        if len(parts) < n:
            continue
        # The last field (the submit line) may itself hold the delimiter.
        parts = parts[:n - 1] + [SEP.join(parts[n - 1:])]
        out.append(dict(zip(fields, parts)))
    return out


def _runs(rows: list[dict], site: str | None) -> list[PastRun]:
    """Allocation lines with their steps' memory folded in."""
    jobs: dict = {}
    order = []
    for row in rows:
        raw = row.get("JobIDRaw") or ""
        base, dot, _step = raw.partition(".")
        if not base:
            continue
        if not dot:
            words = (row.get("State") or "").split()
            state = words[0].rstrip("+").upper() if words else ""
            t = tres(row.get("AllocTRES"))
            line = row.get("SubmitLine") or None
            wd = row.get("WorkDir") or None
            jid = row.get("JobID") or ""
            aj, _, at = jid.partition("_")
            run = PastRun(job_id=base, state=state, site=site, job_name=row.get("JobName") or None,
                          partition=(row.get("Partition") or "").split(",")[0] or None,
                          submitted=row.get("Submit") or None,
                          cpus=_int(row.get("AllocCPUS")) or t.get("cpus"),
                          mem_mb=(t["mem_mb"] // max(1, t.get("nodes") or 1)) if t.get("mem_mb") else None,
                          time_s=parse_time_seconds(row.get("Timelimit")),
                          gpus=t.get("gpus"),
                          nodes=max(_int(row.get("NNodes")) or 0, t.get("nodes") or 0) or None,
                          elapsed_s=parse_time_seconds(row.get("Elapsed")),
                          cpu_s=parse_time_seconds(row.get("TotalCPU")),
                          workdir=wd, folder_tail=folder_tail(wd),
                          script=script_path(line, wd), submit_line=line,
                          exit_code=row.get("ExitCode") or None,
                          array_job=aj if at else None, array_task=at if at and at.isdigit() else None)
            if base not in jobs:
                order.append(base)
            jobs[base] = run
            continue
        # A step: the most memory any of its tasks held, times the tasks a
        # node ran (each task's own peak: an estimate that errs on the high side).
        tasks, nodes = _int(row.get("NTasks")) or 1, _int(row.get("NNodes")) or 1
        run = jobs.get(base)
        if run is None:
            jobs[base] = run = PastRun(job_id=base, state="")
            order.append(base)
        if _step != "batch" and _step != "extern":
            run.tasks = max(run.tasks or 0, tasks)
        rss = parse_memory_mb(row.get("MaxRSS")) if row.get("MaxRSS") else None
        if rss is None:
            continue
        run.peak_mem_mb = max(run.peak_mem_mb or 0, rss * -(-tasks // nodes))
    return [jobs[b] for b in order if jobs[b].state]


def from_sacct(user: str, days: int = 180, site: str | None = None, run=_run,
               states: tuple = ENDED) -> list[PastRun]:
    """The person's jobs that ended in the last ``days`` days, newest first,
    as Slurm's accounting has them."""
    if not USER_RE.fullmatch(str(user or "")):
        raise HistoryError("not a user name")
    days = max(1, min(int(days), 730))
    argv = ["sacct", "-n", "-P", f"--delimiter={SEP}", "-u", str(user), "-S", f"now-{days}days",
            "-E", "now", "-s", ",".join(states), f"--format={','.join(_FIELDS)}"]
    runs = _runs(_sacct_rows(argv, run), site)
    runs.sort(key=lambda r: r.submitted or "", reverse=True)
    return runs


def job_from_sacct(job: str, user: str, site: str | None = None, run=_run) -> PastRun | None:
    """One of the person's jobs, whatever its state; None when Slurm has no
    such job of theirs (the same answer whether it doesn't exist or isn't
    theirs)."""
    if not re.fullmatch(r"[0-9]{1,12}(?:_[0-9]{1,8})?", str(job or "")):
        raise HistoryError("not a job number")
    if not USER_RE.fullmatch(str(user or "")):
        raise HistoryError("not a user name")
    argv = ["sacct", "-n", "-P", f"--delimiter={SEP}", "-j", str(job), "-u", str(user),
            f"--format={','.join(_FIELDS)}"]
    runs = _runs(_sacct_rows(argv, run), site)
    for r in runs:
        if r.job_id == str(job) or (r.array_job and f"{r.array_job}_{r.array_task}" == str(job)):
            return r
    if "_" not in str(job):
        for r in runs:
            if r.array_job == str(job):
                return r                       # an array job: its first task's record
    return runs[0] if len(runs) == 1 else None


# ── nomad's database (the hub's, for the Console) ───────────────────────────

def from_db(db_path: str, user: str, site: str | None = None, limit: int = 300) -> list[PastRun]:
    """The person's jobs nomad has measured, newest first."""
    from nomad.edu.explain import load_user_history
    out = []
    for r in load_user_history(str(db_path), user, limit=limit, site=site) or []:
        cpus = r.get("req_cpus")
        elapsed = r.get("runtime_seconds")
        avg = r.get("avg_cpu_percent")
        peak_gb = r.get("peak_memory_gb")
        ceiling = r.get("_node_memory_mb")
        peak = peak_gb * 1024 if peak_gb else None
        if peak and ceiling and peak > ceiling:
            peak = None                          # more than the node has: not a real reading
        out.append(PastRun(
            job_id=str(r.get("job_id")), state=str(r.get("state") or "").upper(),
            site=r.get("source_site") or r.get("cluster") or site, job_name=r.get("job_name"),
            partition=(r.get("partition") or "").split(",")[0] or None,
            submitted=r.get("submit_time"), cpus=cpus, mem_mb=r.get("req_mem_mb"),
            time_s=r.get("req_time_seconds"), gpus=r.get("req_gpus"),
            elapsed_s=elapsed, peak_mem_mb=peak,
            cpu_s=(avg / 100 * cpus * elapsed) if (avg and cpus and elapsed) else None,
            folder_tail=r.get("work_tail")))
    out.sort(key=lambda r: str(r.submitted or ""), reverse=True)
    return out

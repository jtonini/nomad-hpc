# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What each running job is using now (1.32.0): its cores kept busy and the
memory it holds, from Slurm's own accounting of running jobs (``sstat``),
against what it asked for -- the figures an ended job's analysis gives, while
it still runs.

Each run lists the running jobs (``squeue``), asks ``sstat`` for their steps
(every job in one call per hundred), and keeps one row per running job in
``job_live``; a job that is no longer running loses its row. Per job:

- ``cpu_s``: the CPU time its steps have used (each step's AveCPU times its
  tasks); ``busy_now``: the cores kept busy since the last run (the CPU time
  added over the time between, when the same steps ran then);
  ``busy_avg``: since it started (its steps' CPU time over its time running:
  a job whose steps came and went reads low).
- ``peak_rss_mb``: the most memory one of its tasks has held (MaxRSS, as an
  ended job's peak is measured); ``rss_now_mb``: what all its tasks hold
  now (AveRSS times tasks, every step but the extern one).

sstat shows someone else's job only to root and Slurm's own account, so it
runs as Slurm's account through sudo (``sstat_as``, by default ``[actions]
slurm_as``; "self" when this account is root or SlurmUser), by its full
path, without a terminal, and not at all when someone else could change the
nomad running here (the check `nomad node drain` makes, 1.31).

    [collectors.job_live]
    enabled = true            # a Slurm head node
    # interval = 300
    # sstat_as = "slurm"      # default: [actions] slurm_as
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime
from typing import Any

from .base import BaseCollector, CollectionError, find_tool, tool_env

logger = logging.getLogger(__name__)

BATCH = 100                     # jobs per sstat call
TIMEOUT = 90                    # seconds, each call
SUDO_SEARCH = "/usr/bin:/bin:/usr/sbin:/sbin"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_JOB = re.compile(r"[0-9]{1,12}(?:_[0-9]{1,9})?")
_SIZE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?)", re.I)
_UNIT_MB = {"": 1 / 1024 ** 2, "K": 1 / 1024, "M": 1.0, "G": 1024.0, "T": 1024.0 ** 2, "P": 1024.0 ** 3}


def size_mb(v: str) -> float | None:
    """sstat's sizes in MB: "32562868K", "512M", "1.5G"; a bare number is
    bytes, as an ended job's are read (job_metrics); "0" is 0."""
    m = _SIZE.fullmatch((v or "").strip())
    if not m:
        return None
    return float(m.group(1)) * _UNIT_MB[m.group(2).upper()]


def seconds(v: str) -> float | None:
    """sstat's durations: [D-]HH:MM:SS, MM:SS.mmm, or SS.mmm."""
    v = (v or "").strip()
    if not v:
        return None
    days = 0
    if "-" in v:
        d, _, v = v.partition("-")
        if not d.isdigit():
            return None
        days = int(d)
    try:
        parts = [float(p) for p in v.split(":")]
    except ValueError:
        return None
    if not 1 <= len(parts) <= 3:
        return None
    total = 0.0
    for p in parts:
        total = total * 60 + p
    return days * 86400 + total


def parse_squeue(text: str) -> list[dict]:
    """Running jobs: Slurm's id, the id as shown (an array's task: 5005_7),
    owner, CPUs, nodes, start."""
    out = []
    for ln in (text or "").splitlines():
        f = ln.strip().split("|")
        if len(f) != 6 or not f[0].isdigit() or not _JOB.fullmatch(f[1]) or not _NAME.fullmatch(f[2]):
            continue
        out.append({"slurm_id": f[0], "job_id": f[1], "user": f[2],
                    "cpus": int(f[3]) if f[3].isdigit() else None,
                    "nodes": int(f[4]) if f[4].isdigit() else None,
                    "start": f[5] if re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d", f[5]) else None})
    return out


def parse_sstat(text: str) -> dict[str, list[dict]]:
    """sstat's lines by job (its id before the step: "87790" of
    "87790.batch", "5005_7" of "5005_7.0"), each step once (1.32.1): asked
    for an array's own id, sstat also answers for its running tasks (vadur:
    -j 12266 gave 12987.batch and 13020.batch too), so a task asked for by
    its own id as well came back twice, and counted twice."""
    out: dict[str, dict[str, dict]] = {}
    for ln in (text or "").splitlines():
        f = ln.strip().split("|")
        if len(f) < 5:
            continue
        job, dot, step = f[0].rpartition(".")
        if not dot or not _JOB.fullmatch(job) or not step:
            continue
        out.setdefault(job, {})[step] = {
            "step": step, "max_rss_mb": size_mb(f[1]), "ave_rss_mb": size_mb(f[2]),
            "ave_cpu_s": seconds(f[3]), "tasks": int(f[4]) if f[4].strip().isdigit() else None}
    return {job: list(steps.values()) for job, steps in out.items()}


def summarise(steps: list[dict]) -> dict:
    """A job's figures from its steps' lines (the extern step left out: it
    runs nothing of the job's)."""
    real = [s for s in steps if s["step"] != "extern"]
    if not real:
        return {"measured": 0, "tasks": None, "steps": None, "cpu_s": None, "rss_now_mb": None, "peak_rss_mb": None}
    tasks = sum((s["tasks"] or 1) for s in real)
    cpu = sum((s["ave_cpu_s"] or 0.0) * (s["tasks"] or 1) for s in real)
    now = sum((s["ave_rss_mb"] or 0.0) * (s["tasks"] or 1) for s in real)
    peaks = [s["max_rss_mb"] for s in real if s["max_rss_mb"] is not None]
    return {"measured": 1, "tasks": tasks, "steps": ",".join(sorted(s["step"] for s in real)),
            "cpu_s": round(cpu, 1), "rss_now_mb": round(now, 1),
            "peak_rss_mb": round(max(peaks), 1) if peaks else None}


def _account() -> str:
    import getpass
    try:
        return getpass.getuser()
    except Exception:                              # noqa: BLE001
        return str(os.getuid())


class JobLiveCollector(BaseCollector):
    """Running jobs' cores kept busy and memory held, from sstat."""

    name = "job_live"
    description = "What each running job uses now (cores busy, memory held), from sstat"
    default_interval = 300

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS job_live (
            job_id TEXT PRIMARY KEY, slurm_id TEXT, user TEXT, cpus INTEGER, nodes INTEGER, start TEXT,
            sampled_at TEXT NOT NULL, elapsed_s INTEGER, measured INTEGER, tasks INTEGER, steps TEXT,
            cpu_s REAL, busy_now REAL, busy_avg REAL, rss_now_mb REAL, peak_rss_mb REAL);
    """

    def __init__(self, config: dict[str, Any], db_path):
        super().__init__(config, db_path)
        v = self.config.get("sstat_as")
        self.sstat_as = v if isinstance(v, str) and (v == "self" or _NAME.fullmatch(v)) else None

    def _argv(self) -> list[str]:
        """sstat's own command, through sudo unless "self"."""
        from nomad import drain
        if self.sstat_as is None:
            raise CollectionError("sstat shows other people's jobs only to root and Slurm's account: set "
                                  "[collectors.job_live] sstat_as (or [actions] slurm_as)")
        try:
            sstat = drain.find("sstat")
        except drain.NodeError as e:
            raise CollectionError(str(e)) from None
        if not sstat:
            raise CollectionError("no sstat here")
        if self.sstat_as == "self":
            return [sstat]
        why = drain._install_checked()
        if why:
            raise CollectionError(f"someone else could change the nomad that would run sstat as {self.sstat_as}: {why}")
        sudo = shutil.which("sudo", path=SUDO_SEARCH)
        if not sudo:
            raise CollectionError("no sudo here")
        return [sudo, "-n", "-u", self.sstat_as, sstat]

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess:
        env = tool_env()
        env.update(LANG="C", LC_ALL="C", SLURM_TIME_FORMAT="standard")
        return subprocess.run(argv, capture_output=True, text=True, errors="replace", timeout=TIMEOUT,
                              stdin=subprocess.DEVNULL, env=env, cwd="/", start_new_session=True)

    def collect(self) -> list[dict[str, Any]]:
        squeue = find_tool("squeue")
        if not squeue:
            raise CollectionError("no squeue here")
        try:
            r = self._run([squeue, "-h", "-a", "-t", "RUNNING", "-o", "%A|%i|%u|%C|%D|%S"])
        except subprocess.TimeoutExpired:
            raise CollectionError(f"squeue: no answer in {TIMEOUT} s") from None
        if r.returncode != 0:
            raise CollectionError(f"squeue: {(r.stderr or '').strip()[-200:] or f'exit {r.returncode}'}")
        jobs = parse_squeue(r.stdout)
        if not jobs:
            # Nothing runs: no row stays (a collector that collects nothing isn't
            # stored, so this one clears its table itself, 1.32.1).
            self.store([])
            return []
        argv = self._argv()
        found: dict[str, list[dict]] = {}
        said = []
        for i in range(0, len(jobs), BATCH):
            ids = ",".join(j["slurm_id"] for j in jobs[i:i + BATCH])
            try:
                s = self._run([*argv, "-a", "-n", "-P", "-j", ids, "-o", "JobID,MaxRSS,AveRSS,AveCPU,NTasks"])
            except subprocess.TimeoutExpired:
                said.append(f"sstat: no answer in {TIMEOUT} s")
                continue
            for k, v in parse_sstat(s.stdout).items():
                have = {x["step"]: x for x in found.get(k, [])}
                have.update({x["step"]: x for x in v})           # each step once, across calls too
                found[k] = list(have.values())
            err = (s.stderr or "").strip()
            if s.returncode != 0 and not s.stdout.strip():
                said.append(err.splitlines()[-1][:200] if err else f"sstat: exit {s.returncode}")
        if said and not found:
            raise CollectionError("; ".join(dict.fromkeys(said)))
        now = datetime.now().replace(microsecond=0)
        out = []
        for j in jobs:
            steps = found.get(j["slurm_id"]) or found.get(j["job_id"]) or []
            d = {**j, **summarise(steps), "sampled_at": now.isoformat()}
            try:
                d["elapsed_s"] = max(0, int((now - datetime.fromisoformat(j["start"])).total_seconds())) \
                    if j["start"] else None
            except ValueError:
                d["elapsed_s"] = None
            d["busy_avg"] = round(d["cpu_s"] / d["elapsed_s"], 2) if d["cpu_s"] is not None and d["elapsed_s"] else None
            out.append(d)
        return out

    def count_records(self, data) -> int:
        return len(data)

    @staticmethod
    def busy_now(prev: dict | None, d: dict) -> float | None:
        """The cores kept busy between the last run and this one: the same
        steps then and now, more CPU time now, some time between. Not held
        to the cores it asked for (1.32.1): a cluster that doesn't keep jobs
        to them (no ConstrainCores) shows here."""
        if not prev or d.get("cpu_s") is None or prev.get("cpu_s") is None or prev.get("steps") != d.get("steps") \
                or prev.get("start") != d.get("start"):
            return None
        try:
            dt = (datetime.fromisoformat(d["sampled_at"]) - datetime.fromisoformat(prev["sampled_at"])).total_seconds()
        except (TypeError, ValueError):
            return None
        used = d["cpu_s"] - prev["cpu_s"]
        if dt < 30 or used < 0:
            return None
        return round(used / dt, 2)

    def store(self, data: list[dict[str, Any]]) -> None:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript(self.SCHEMA)
            prev = {r["job_id"]: dict(r) for r in conn.execute("SELECT * FROM job_live")}
            keep = set()
            for d in data:
                keep.add(d["job_id"])
                row = {k: d.get(k) for k in ("job_id", "slurm_id", "user", "cpus", "nodes", "start", "sampled_at",
                                             "elapsed_s", "measured", "tasks", "steps", "cpu_s", "busy_avg",
                                             "rss_now_mb", "peak_rss_mb")}
                row["busy_now"] = self.busy_now(prev.get(d["job_id"]), d)
                conn.execute(
                    "INSERT INTO job_live (job_id, slurm_id, user, cpus, nodes, start, sampled_at, elapsed_s, measured, "
                    "tasks, steps, cpu_s, busy_now, busy_avg, rss_now_mb, peak_rss_mb) VALUES (:job_id, :slurm_id, "
                    ":user, :cpus, :nodes, :start, :sampled_at, :elapsed_s, :measured, :tasks, :steps, :cpu_s, "
                    ":busy_now, :busy_avg, :rss_now_mb, :peak_rss_mb) ON CONFLICT(job_id) DO UPDATE SET "
                    "slurm_id = excluded.slurm_id, user = excluded.user, cpus = excluded.cpus, nodes = excluded.nodes, "
                    "start = excluded.start, sampled_at = excluded.sampled_at, elapsed_s = excluded.elapsed_s, "
                    "measured = excluded.measured, tasks = excluded.tasks, steps = excluded.steps, "
                    "cpu_s = excluded.cpu_s, busy_now = excluded.busy_now, busy_avg = excluded.busy_avg, "
                    "rss_now_mb = excluded.rss_now_mb, peak_rss_mb = excluded.peak_rss_mb", row)
            gone = [k for k in prev if k not in keep]
            for i in range(0, len(gone), 500):
                part = gone[i:i + 500]
                conn.execute(f"DELETE FROM job_live WHERE job_id IN ({','.join('?' * len(part))})", part)
            conn.commit()
        finally:
            conn.close()


__all__ = ["JobLiveCollector", "parse_squeue", "parse_sstat", "summarise", "size_mb", "seconds"]

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Jobs on a lab machine (1.67.0): a job script of a person's, started by
nomad-gate as that person on a machine with no scheduler, the way the
Console's Submit page sends a job to a cluster.

There is no queue. A job starts at once or is refused, and the page says
why. All of nomad's jobs on one machine together hold at most ``share``
percent of its processors and of its memory (the hub says how much: its
[console.machine_runs] share, 50 by default), so whoever sits at the machine
keeps the rest; each runs at the lowest priority there is (nice 19, idle
disk time), so they always come first anyway. A job is held to the cores it
was given (each its own, never shared), stopped when it goes past its time,
or past its memory (read every few seconds), and its processes are the
first the kernel ends should the machine run out of memory. A job may ask
for GPUs (NVIDIA's, through nvidia-smi): each is given whole, to one of
nomad's jobs at a time (all of a machine's GPUs may be lent), and never one
something else is computing on now; the job sees only its own (and none when
it asked for none).

Each job has a small supervisor, a process of the person's own, that starts
it in the script's folder, through their login shell (so their modules and
settings are there, as on a cluster), writes its output beside the script
(nomad-run-ID.out, never over a file), and keeps its record in
~/.local/share/nomad/runs/MACHINE/ID/ (the script as it was sent, and its
state). The supervisors are how a start counts what nomad's jobs hold on
the machine: each says its cores and memory in its command line, which
anyone on the machine may read. A machine with Slurm refuses: jobs go
through Slurm there.

    python -m nomad.submit.machine_run supervise --dir D --cores 6,7 --mem BYTES [--gpus 0]

is the supervisor (nomad-gate starts it; nothing else should).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta

SHARE = 50                          # percent of a machine's cores and memory nomad's jobs may hold together
SHARE_MIN, SHARE_MAX = 10, 90
RUNS = (".local", "share", "nomad", "runs")    # under the person's home, then the machine's name
KEEP_DAYS = 30                      # ended jobs' records kept this long
MAX_LISTED = 50
LOCK = "/tmp/.nomad-machine-runs.lock"
LOCK_WAIT = 10                      # seconds a start waits for another to finish choosing cores
POLL = 5                            # seconds between the supervisor's looks at its job
GRACE = 30                          # seconds between asking a job to end and ending it
STARTED_WAIT = 5                    # seconds a start waits to see its supervisor running
MIN_MEM = 16 * 1024 ** 2
MIN_TIME = 60
TAIL = 64 * 1024
TAIL_LINES = 200
MODULE = "nomad.submit.machine_run"
TOOL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
JOB_PATH = "/usr/local/bin:/usr/bin:/bin"
_RUN = re.compile(r"r[0-9a-f]{10}")
_MEM = re.compile(r"([0-9]{1,7}(?:\.[0-9]{1,3})?)([KMGTkmgt]?)")
UNITS = {"": 1024 ** 2, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}   # as Slurm: M by default
# What `nomad gate test --run` starts (1.67.0): nothing of the person's, nothing written but its record.
TEST_SCRIPT = ("#!/bin/sh\n# nomad gate test: a job that waits, then ends\n"
               "echo \"nomad's test job on $(hostname): started\"\nsleep 120\necho done\n")
WHY = {"finished": "it finished", "time": "it went past its time", "memory": "it used more memory than it asked for",
       "stopped": "it was stopped", "lost": "it ended without its supervisor seeing it end (the machine "
       "restarted, or its processes were ended)", "failed": "it couldn't be started"}


class RunError(Exception):
    """A request about jobs here that isn't done: said, never half done."""


# ── the machine ─────────────────────────────────────────────────────────────

def machine() -> str:
    return socket.gethostname().split(".")[0]


def cores_here() -> list:
    """The machine's processors (as this process may use them: all of them, from sshd)."""
    try:
        return sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return list(range(os.cpu_count() or 1))


def memory_here() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def slurm_here() -> bool:
    return bool(shutil.which("sbatch", path=TOOL_PATH + ":/usr/local/bin")) or os.path.exists("/etc/slurm/slurm.conf")


def memory_of(text: str) -> int:
    """Bytes from Slurm's way of saying memory: 8G, 500M, 2048 (megabytes)."""
    m = _MEM.fullmatch(str(text).strip())
    if not m:
        raise RunError(f"{text!r}: memory as 8G or 500M")
    return int(float(m.group(1)) * UNITS[m.group(2).upper()])


def seconds_of(text: str) -> int:
    """Seconds from Slurm's way of saying time: M, M:S, H:M:S, D-H, D-H:M, D-H:M:S."""
    t = str(text).strip()
    m = re.fullmatch(r"(?:(\d{1,3})-)?(\d{1,4})(?::(\d{1,2}))?(?::(\d{1,2}))?", t)
    if not m:
        raise RunError(f"{text!r}: a time as 4:00:00 or 1-00:00:00")
    days, a, b, c = m.group(1), int(m.group(2)), m.group(3), m.group(4)
    if days is not None:                       # D-H, D-H:M, D-H:M:S
        return int(days) * 86400 + a * 3600 + int(b or 0) * 60 + int(c or 0)
    if b is None:                              # M
        return a * 60
    if c is None:                              # M:S
        return a * 60 + int(b)
    return a * 3600 + int(b) * 60 + int(c)     # H:M:S


def _cmdline(pid: int) -> list:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [x.decode("utf-8", "replace") for x in f.read().split(b"\0") if x]
    except OSError:
        return []


def _value(args: list, name: str):
    try:
        return args[args.index(name) + 1]
    except (ValueError, IndexError):
        return None


def gpus_here() -> list:
    """The machine's NVIDIA GPUs, as nvidia-smi numbers them: [{index, uuid,
    name, mem}]; none without nvidia-smi (or when it can't say)."""
    smi = shutil.which("nvidia-smi", path=TOOL_PATH + ":/usr/local/bin")
    if not smi:
        return []
    try:
        r = subprocess.run([smi, "--query-gpu=index,uuid,name,memory.total", "--format=csv,noheader,nounits"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in (r.stdout or "").splitlines() if r.returncode == 0 else []:
        parts = [x.strip() for x in line.split(",")]
        if len(parts) < 4 or not parts[0].isdigit() or not parts[1].startswith("GPU-"):
            continue
        try:
            mem = int(float(parts[3])) * 1024 ** 2
        except ValueError:
            mem = None
        out.append({"index": int(parts[0]), "uuid": parts[1], "name": parts[2], "mem": mem})
    return out


def gpus_busy() -> set:
    """The GPUs (by uuid) something computes on now (nvidia-smi's compute
    programs, whoever's: a screen alone isn't one)."""
    smi = shutil.which("nvidia-smi", path=TOOL_PATH + ":/usr/local/bin")
    if not smi:
        return set()
    try:
        r = subprocess.run([smi, "--query-compute-apps=gpu_uuid,pid", "--format=csv,noheader"],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    return {line.split(",")[0].strip() for line in (r.stdout or "").splitlines()
            if r.returncode == 0 and line.strip().startswith("GPU-")}


def held(proc: str = "/proc") -> list:
    """nomad's jobs running here now, whoever's: [{pid, cores, mem, gpus,
    dir}], from their supervisors' command lines."""
    out = []
    try:
        names = os.listdir(proc)
    except OSError:
        return out
    for n in names:
        if not n.isdigit():
            continue
        args = _cmdline(int(n)) if proc == "/proc" else _cmdline_in(proc, n)
        if MODULE not in args or "supervise" not in args:
            continue
        try:
            cores = {int(x) for x in (_value(args, "--cores") or "").split(",") if x != ""}
            mem = int(_value(args, "--mem") or 0)
            gpus = {int(x) for x in (_value(args, "--gpus") or "").split(",") if x != ""}
        except ValueError:
            continue
        out.append({"pid": int(n), "cores": cores, "mem": mem, "gpus": gpus, "dir": _value(args, "--dir")})
    return out


def _cmdline_in(proc: str, n: str) -> list:                     # a stand-in /proc (tests)
    try:
        with open(os.path.join(proc, n, "cmdline"), "rb") as f:
            return [x.decode("utf-8", "replace") for x in f.read().split(b"\0") if x]
    except OSError:
        return []


def share_of(share) -> int:
    try:
        share = int(share if share is not None else SHARE)
    except (TypeError, ValueError):
        raise RunError("share: a percent") from None
    if not SHARE_MIN <= share <= SHARE_MAX:
        raise RunError(f"share: {SHARE_MIN} to {SHARE_MAX} percent")
    return share


def _gpu_facts(now: list, gpus: list, busy: set) -> dict:
    held_gpus = set().union(*[h.get("gpus") or set() for h in now]) if now else set()
    elsewhere = {g["index"] for g in gpus if g["uuid"] in busy and g["index"] not in held_gpus}
    free = [g for g in gpus if g["index"] not in held_gpus and g["index"] not in elsewhere]
    return {"gpus": len(gpus), "gpu_names": sorted({g["name"] for g in gpus}),
            "held_gpus": len(held_gpus & {g["index"] for g in gpus}), "busy_gpus": len(elsewhere),
            "free_gpus": len(free), "_free_gpus": free}


def facts(share=None, proc: str = "/proc", gpus: list | None = None, busy: set | None = None) -> dict:
    """What the machine has, what nomad's jobs may hold of it, and what they hold now."""
    share = share_of(share)
    cores, mem = cores_here(), memory_here()
    may_cpus = max(1, len(cores) * share // 100)
    may_mem = mem * share // 100
    now = held(proc)
    held_cpus = len(set().union(*[h["cores"] for h in now])) if now else 0
    held_mem = sum(h["mem"] for h in now)
    try:
        load = round(os.getloadavg()[0], 2)
    except OSError:
        load = None
    g = _gpu_facts(now, gpus_here() if gpus is None else gpus, gpus_busy() if busy is None else busy)
    return {"machine": machine(), "cpus": len(cores), "mem": mem, "share": share, "may_cpus": may_cpus,
            "may_mem": may_mem, "held_cpus": held_cpus, "held_mem": held_mem, "jobs_here": len(now),
            "free_cpus": max(0, may_cpus - held_cpus), "free_mem": max(0, may_mem - held_mem), "load": load,
            "slurm": slurm_here(), **g}


def _gb(n: int) -> str:
    return f"{n / 1024 ** 3:.1f} GB" if n >= 1024 ** 3 else f"{n / 1024 ** 2:.0f} MB"


# ── a person's records here ─────────────────────────────────────────────────

def _home() -> str:
    import pwd
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return os.path.expanduser("~")


def runs_dir(home: str | None = None, name: str | None = None) -> str:
    return os.path.join(home or _home(), *RUNS, name or machine())


def _read_state(d: str) -> dict | None:
    try:
        with open(os.path.join(d, "state.json"), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def _write_state(d: str, doc: dict) -> None:
    tmp = os.path.join(d, ".state.json.new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(doc, f, sort_keys=True)
    os.replace(tmp, os.path.join(d, "state.json"))


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _alive(pid, d: str) -> bool:
    """Whether ``pid`` is this job's supervisor still."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    args = _cmdline(pid)
    return MODULE in args and "supervise" in args and _value(args, "--dir") == d


def _shown(doc: dict) -> dict:
    keep = ("run", "machine", "script", "folder", "output", "cpus", "cores", "mem", "gpus", "gpu_ids", "limit",
            "started", "ended", "state", "why", "exit", "cpu_s", "peak_mem", "test")
    out = {k: doc.get(k) for k in keep if k in doc}
    if doc.get("why"):
        out["why_words"] = WHY.get(doc["why"], doc["why"])
    return out


def _supervised(d: str, doc: dict) -> bool:
    if doc.get("state") == "running":
        return _alive(doc.get("pid"), d)
    return any(h.get("dir") == d for h in held())                  # starting: no pid written yet


def _refreshed(d: str, doc: dict) -> dict:
    """A record that says running, whose supervisor is gone: lost."""
    if doc.get("state") in ("starting", "running") and not _supervised(d, doc):
        doc = {**doc, "state": "ended", "why": "lost", "ended": doc.get("ended") or _now()}
        try:
            _write_state(d, doc)
        except OSError:
            pass
    return doc


def _tidy(top: str) -> None:
    """Records of jobs that ended over KEEP_DAYS ago go."""
    cut = datetime.now() - timedelta(days=KEEP_DAYS)
    try:
        names = os.listdir(top)
    except OSError:
        return
    for n in names:
        if not _RUN.fullmatch(n):
            continue
        d = os.path.join(top, n)
        doc = _read_state(d) or {}
        try:
            ended = datetime.fromisoformat(doc["ended"]) if doc.get("ended") else None
        except ValueError:
            ended = None
        if doc.get("state") == "ended" and ended and ended < cut and not os.path.islink(d):
            shutil.rmtree(d, ignore_errors=True)


def runs(home: str | None = None) -> list:
    """This person's jobs here, newest first (at most MAX_LISTED)."""
    top = runs_dir(home)
    _tidy(top)
    out = []
    try:
        names = os.listdir(top)
    except OSError:
        return out
    for n in names:
        d = os.path.join(top, n)
        if not _RUN.fullmatch(n) or os.path.islink(d):
            continue
        doc = _read_state(d)
        if doc:
            out.append(_shown(_refreshed(d, doc)))
    out.sort(key=lambda x: str(x.get("started") or ""), reverse=True)
    return out[:MAX_LISTED]


def _dir_of(run: str, home: str | None = None) -> str:
    if not _RUN.fullmatch(str(run)):
        raise RunError(f"{str(run)[:20]!r}: not a job's id here")
    d = os.path.join(runs_dir(home), run)
    if os.path.islink(d) or not os.path.isdir(d):
        raise RunError(f"no job {run} of yours on {machine()}")
    return d


def _tail(path: str) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        return {"output_error": f"{path}: {e.strerror}"}
    with os.fdopen(fd, "rb") as f:
        size = os.fstat(f.fileno()).st_size
        f.seek(max(0, size - TAIL))
        data = f.read(TAIL)
    lines = data.decode("utf-8", "replace").splitlines()[-TAIL_LINES:]
    return {"tail": "\n".join(lines), "output_bytes": size}


def one(run: str, home: str | None = None) -> dict:
    """One job of this person's here, with the end of its output."""
    d = _dir_of(run, home)
    doc = _read_state(d)
    if not doc:
        raise RunError(f"job {run}'s record can't be read")
    doc = _refreshed(d, doc)
    out = _shown(doc)
    if doc.get("output"):
        out.update(_tail(doc["output"]))
    return out


def stop(run: str, home: str | None = None, wait: float = 3.0) -> dict:
    """Stop one job of this person's here (its supervisor ends it: asked,
    then ended GRACE seconds later)."""
    d = _dir_of(run, home)
    doc = _refreshed(d, _read_state(d) or {})
    if doc.get("state") not in ("starting", "running"):
        return {"run": run, "already": True, **_shown(doc)}
    pid = doc.get("pid")
    if not _alive(pid, d):
        raise RunError(f"job {run}'s supervisor isn't running")
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        raise RunError(f"job {run} couldn't be stopped: {e.strerror}") from None
    end = time.time() + wait
    while time.time() < end:
        now = _read_state(d) or {}
        if now.get("state") == "ended":
            return {"run": run, **_shown(now)}
        time.sleep(0.2)
    return {"run": run, "stopping": True, **_shown(_read_state(d) or doc)}


# ── starting one ────────────────────────────────────────────────────────────

def _lock():
    """The machine's lock while cores are chosen (two starts at once can't take the same)."""
    import fcntl
    try:
        fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o666)
        try:
            if os.fstat(fd).st_uid == os.getuid():
                os.fchmod(fd, 0o666)
        except OSError:
            pass
    except PermissionError:
        fd = os.open(LOCK, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        raise RunError(f"the machine's lock ({LOCK}) can't be opened: {e.strerror}") from None
    end = time.time() + LOCK_WAIT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.time() >= end:
                os.close(fd)
                raise RunError("another job is being started here: try again in a minute") from None
            time.sleep(0.2)


def _output_file(folder: str, run: str) -> str:
    path = os.path.join(folder, f"nomad-run-{run}.out")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
    except OSError as e:
        raise RunError(f"its output can't be written in {folder} ({e.strerror})") from None
    os.close(fd)
    return path


def start(script: str | None, sha: str | None, cpus: int, mem: str, limit: str, share=None, test: bool = False,
          home: str | None = None, proc: str = "/proc", spawn=None, gpus: int = 0, gpu_list: list | None = None,
          gpu_busy: set | None = None) -> dict:
    """Start a job of this person's here, as them, now; else RunError, saying why."""
    if slurm_here():
        raise RunError("this machine has Slurm: send the job through Slurm here")
    cpus, gpus = int(cpus), int(gpus or 0)
    mem_b, secs = memory_of(mem), seconds_of(limit)
    if cpus < 1:
        raise RunError("cores: at least 1")
    if gpus < 0:
        raise RunError("GPUs: none or more")
    if mem_b < MIN_MEM:
        raise RunError("memory: at least 16M")
    if secs < MIN_TIME:
        raise RunError("time: at least a minute")
    if test:
        text, folder = TEST_SCRIPT.encode(), None
    else:
        from nomad.submit import gate
        try:
            text = gate.pinned(script, sha)            # the very text the person was shown
        except gate.Refused as e:
            raise RunError(str(e)) from None
        folder = os.path.dirname(script)
    lock = _lock()
    try:
        f = facts(share, proc, gpu_list, gpu_busy)
        name = f["machine"]
        if gpus > f["gpus"]:
            raise RunError(f"{name} has no GPU nomad can lend" if not f["gpus"]
                           else f"{name} has {f['gpus']} GPU{'s' if f['gpus'] != 1 else ''}: fewer than {gpus}")
        if gpus > f["free_gpus"]:
            raise RunError(f"not {gpus} GPU{'s' if gpus != 1 else ''} free on {name} now: of its {f['gpus']}, "
                           f"nomad's jobs hold {f['held_gpus']} and other programs compute on {f['busy_gpus']}. "
                           "Try another machine, or later.")
        given = f["_free_gpus"][:gpus]
        if cpus > f["may_cpus"]:
            raise RunError(f"at most {f['may_cpus']} cores on {name}: nomad's jobs may use {f['share']}% of its "
                           f"{f['cpus']} together")
        if mem_b > f["may_mem"]:
            raise RunError(f"at most {_gb(f['may_mem'])} of memory on {name}: nomad's jobs may use {f['share']}% "
                           f"of its {_gb(f['mem'])} together")
        if cpus > f["free_cpus"] or mem_b > f["free_mem"]:
            raise RunError(f"{name} is busy: nomad's jobs hold {f['held_cpus']} of the {f['may_cpus']} cores and "
                           f"{_gb(f['held_mem'])} of the {_gb(f['may_mem'])} they may use there; free now: "
                           f"{f['free_cpus']} cores, {_gb(f['free_mem'])}. Try another machine, or later.")
        taken = set().union(*[h["cores"] for h in held(proc)]) if f["jobs_here"] else set()
        cores = [c for c in reversed(cores_here()) if c not in taken][:cpus]
        if len(cores) < cpus:
            raise RunError(f"{name} has {len(cores)} cores nomad's jobs don't hold: fewer than {cpus}")
        run = "r" + secrets.token_hex(5)
        top = runs_dir(home)
        d = os.path.join(top, run)
        output = _output_file(folder, run) if not test else None      # never over a file: refused first
        try:
            os.makedirs(top, mode=0o700, exist_ok=True)
            os.mkdir(d, 0o700)
            fd = os.open(os.path.join(d, "script"), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
            with os.fdopen(fd, "wb") as fh:
                fh.write(text)
            if test:
                folder, output = d, os.path.join(d, "output")
                os.close(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
            doc = {"run": run, "machine": name, "script": script if not test else None, "sha256": sha,
                   "folder": folder, "output": output, "cpus": cpus, "cores": sorted(cores), "mem": mem_b,
                   "gpus": gpus, "gpu_ids": [g["index"] for g in given], "gpu_uuids": [g["uuid"] for g in given],
                   "limit": secs, "started": _now(), "state": "starting", "test": bool(test)}
            _write_state(d, doc)
        except OSError as e:
            if output and not test:
                try:
                    os.unlink(output)                               # still empty: it was only just made
                except OSError:
                    pass
            shutil.rmtree(d, ignore_errors=True)
            raise RunError(f"its record can't be kept in {top} ({e.strerror})") from None
        argv = [sys.executable, "-I", "-B", "-m", MODULE, "supervise", "--dir", d,
                "--cores", ",".join(str(c) for c in sorted(cores)), "--mem", str(mem_b)]
        if given:
            argv += ["--gpus", ",".join(str(g["index"]) for g in given)]      # counted by the next start
        env = {"PATH": TOOL_PATH, "HOME": home or _home(), "LANG": os.environ.get("LANG", "C.UTF-8")}
        try:
            p = (spawn or subprocess.Popen)(argv, cwd="/", env=env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            start_new_session=True, close_fds=True)
        except OSError as e:
            _write_state(d, {**doc, "state": "ended", "why": "failed", "ended": _now(), "error": e.strerror})
            raise RunError(f"its supervisor couldn't be started: {e.strerror}") from None
    finally:
        os.close(lock)
    end = time.time() + STARTED_WAIT
    while time.time() < end:
        now = _read_state(d) or doc
        if now.get("state") != "starting" or (p.poll() if hasattr(p, "poll") else None) is not None:
            break
        time.sleep(0.1)
    now = _read_state(d) or doc
    if now.get("state") == "ended" and now.get("why") == "failed":
        raise RunError(f"it couldn't be started: {now.get('error') or 'its supervisor said nothing'}")
    return {**_shown(now), "free_cpus": f["free_cpus"] - cpus, "may_cpus": f["may_cpus"],
            "free_gpus": f["free_gpus"] - gpus}


# ── the supervisor (as the person, on its own) ──────────────────────────────

def _session_rss(sid: int, proc: str = "/proc") -> int:
    page = os.sysconf("SC_PAGE_SIZE")
    total = 0
    try:
        names = os.listdir(proc)
    except OSError:
        return 0
    for n in names:
        if not n.isdigit():
            continue
        try:
            with open(os.path.join(proc, n, "stat"), "rb") as f:
                stat = f.read().decode("utf-8", "replace")
            fields = stat[stat.rindex(")") + 2:].split()
            if int(fields[3]) != sid:                       # state ppid pgrp session ...
                continue
            with open(os.path.join(proc, n, "statm"), "rb") as f:
                total += int(f.read().split()[1]) * page
        except (OSError, ValueError, IndexError):
            continue
    return total


def _signal_group(pgid: int, sig) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _prepare(cores: list):
    def prep():
        try:
            os.nice(19)
        except OSError:
            pass
        try:
            os.sched_setaffinity(0, cores)
        except (AttributeError, OSError):
            pass
        try:
            with open("/proc/self/oom_score_adj", "w") as f:      # the first ended, should memory run out
                f.write("1000")
        except OSError:
            pass
    return prep


def supervise(d: str, cores: list, mem: int) -> int:
    doc = _read_state(d)
    if not doc or doc.get("state") != "starting":
        return 2
    stop_said = {"why": None}

    def asked(signum, frame):                                     # noqa: ARG001
        stop_said["why"] = stop_said["why"] or "stopped"
    signal.signal(signal.SIGTERM, asked)
    signal.signal(signal.SIGINT, asked)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    script = os.path.join(d, "script")
    import pwd
    pw = pwd.getpwuid(os.getuid())
    n = len(cores)
    env = {"HOME": pw.pw_dir, "USER": pw.pw_name, "LOGNAME": pw.pw_name, "SHELL": pw.pw_shell or "/bin/bash",
           "PATH": JOB_PATH, "LANG": os.environ.get("LANG", "C.UTF-8"), "NOMAD_RUN": doc["run"],
           "NOMAD_CPUS": str(n), "OMP_NUM_THREADS": str(n), "NOMAD_GPUS": str(len(doc.get("gpu_uuids") or [])),
           # the GPUs it was given, by their own ids (the same whatever order CUDA counts them in); else none
           "CUDA_VISIBLE_DEVICES": ",".join(doc.get("gpu_uuids") or []), "HIP_VISIBLE_DEVICES": "",
           "ROCR_VISIBLE_DEVICES": ""}
    shell = "/bin/bash" if os.access("/bin/bash", os.X_OK) else "/bin/sh"
    argv = [shell, "-l", "-c", 'exec "$0"', script] if shell == "/bin/bash" else [script]
    ionice = shutil.which("ionice", path=TOOL_PATH)
    if ionice:
        argv = [ionice, "-c", "3", "--"] + argv
    try:
        out = os.open(doc["output"], os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
        child = subprocess.Popen(argv, cwd=doc["folder"], env=env, stdin=subprocess.DEVNULL, stdout=out,
                                 stderr=subprocess.STDOUT, start_new_session=True, close_fds=True,
                                 preexec_fn=_prepare(cores))
        os.close(out)
    except OSError as e:
        _write_state(d, {**doc, "state": "ended", "why": "failed", "ended": _now(), "error": e.strerror})
        return 1
    began = time.time()
    _write_state(d, {**doc, "state": "running", "pid": os.getpid(), "job_pid": child.pid})
    peak, why = 0, None
    while child.poll() is None:
        if stop_said["why"]:
            why = stop_said["why"]
        elif time.time() - began > doc["limit"]:
            why = "time"
        else:
            rss = _session_rss(child.pid)
            peak = max(peak, rss)
            if rss > mem:
                why = "memory"
        if why:
            _signal_group(child.pid, signal.SIGTERM)
            try:
                child.wait(GRACE)
            except subprocess.TimeoutExpired:
                _signal_group(child.pid, signal.SIGKILL)
                child.wait()
            break
        for _ in range(POLL * 5):
            if stop_said["why"] or child.poll() is not None:
                break
            time.sleep(0.2)
    code = child.wait()
    _signal_group(child.pid, signal.SIGTERM)                       # what it left running in its session
    import resource
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    peak = max(peak, int(ru.ru_maxrss) * 1024)
    _write_state(d, {**(_read_state(d) or doc), "state": "ended", "why": why or "finished", "exit": code,
                     "ended": _now(), "cpu_s": round(ru.ru_utime + ru.ru_stime, 1), "peak_mem": peak})
    return 0


def main(argv: list | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] != ["supervise"]:
        sys.stderr.write(__doc__.split("\n\n")[0] + "\n")
        return 2
    d, cores, mem = _value(argv, "--dir"), _value(argv, "--cores"), _value(argv, "--mem")
    try:
        cores = [int(x) for x in str(cores).split(",") if x != ""]
        mem = int(mem)
    except (TypeError, ValueError):
        return 2
    # a job's own folder of records: .../.local/share/nomad/runs/MACHINE/ID
    parts = os.path.normpath(str(d or "")).split(os.sep)
    if (not d or not cores or not os.path.isabs(d) or not _RUN.fullmatch(parts[-1])
            or parts[-2] != machine() or tuple(parts[-2 - len(RUNS):-2]) != RUNS):
        return 2
    return supervise(d, cores, mem)


if __name__ == "__main__":
    sys.exit(main())

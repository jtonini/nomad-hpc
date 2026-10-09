# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Running one action on this host: a nomad command, never a shell.

The command line is built from the action's fixed words and its checked
parameters, and started directly (no shell parses it). Positional values
come after "--", so none can be read as an option. The child gets no input,
a time limit, and its own process group, so a time-out or a cancel stops
everything it started. Output is kept up to a limit.
"""
from __future__ import annotations

import codecs
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from collections.abc import Callable

from nomad.actions.spec import Action

MAX_OUTPUT = 1_000_000          # characters kept of each stream
_DROP_ENV = ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONINSPECT")


def reports_dir() -> Path:
    """Where actions that write files (the usage report) write them."""
    p = Path.home() / ".local" / "share" / "nomad" / "reports"
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass
    return p


def build_argv(action: Action, values: dict, *, hub_db: Path | None = None,
               out_dir: Path | None = None) -> list[str]:
    """The command line for an action: this interpreter's nomad, the action's
    fixed words, its options, the hub's database when the action reads it,
    then "--" and any positional values."""
    argv = [sys.executable, "-m", "nomad.cli", *action.argv]
    for p in action.params:
        if p.flag and values.get(p.name) is not None:
            argv += [p.flag, str(values[p.name])]
    if action.hub_db and hub_db is not None:
        argv += ["--db", str(hub_db)]
    if action.files:
        if out_dir is None:
            raise ValueError(f"{action.name} writes files: an output directory is needed")
        argv += ["--out", str(out_dir)]
    pos = [str(values[p.name]) for p in action.params if not p.flag and values.get(p.name) is not None]
    if pos:
        argv += ["--", *pos]
    return argv


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _DROP_ENV}
    env.update(NO_COLOR="1", TERM="dumb", COLUMNS="120", PYTHONIOENCODING="utf-8", NOMAD_ACTION="1")
    return env


class _Reader(threading.Thread):
    """Reads a stream to the end, keeping at most MAX_OUTPUT characters."""

    def __init__(self, stream, on_output: Callable[[str], None] | None = None):
        super().__init__(daemon=True)
        self.stream, self.on_output = stream, on_output
        self.parts: list[str] = []
        self.kept = 0
        self.truncated = False

    def run(self):
        decode = codecs.getincrementaldecoder("utf-8")(errors="replace").decode
        for raw in iter(lambda: self.stream.read1(65536), b""):
            text = decode(raw)
            if not text:
                continue
            if self.kept + len(text) > MAX_OUTPUT:
                text = text[:max(0, MAX_OUTPUT - self.kept)]
                self.truncated = True
            if not text:
                continue          # past the limit: read on, so the child isn't blocked, keep nothing
            self.parts.append(text)
            self.kept += len(text)
            if self.on_output is not None:
                try:
                    self.on_output(text)
                except Exception:
                    pass

    def text(self) -> str:
        return "".join(self.parts)


def _stop(proc: subprocess.Popen) -> None:
    """Stop the command and everything it started: a polite signal, a short
    grace, then SIGKILL to the whole group whatever the first one did (a
    child may outlive or ignore it). Never waits for ever: a process stuck
    in the kernel (a hung NFS mount) is left to the system."""
    for sig, grace in ((signal.SIGTERM, 3), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            pass
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass


def run_argv(argv: list[str], timeout: float, *, cancel: threading.Event | None = None,
             on_output: Callable[[str], None] | None = None, cwd: Path | None = None) -> dict:
    """Run a command line (a list, never a string) and report what happened."""
    started = time.time()
    # Not the home directory: `python -m` puts the working directory first on
    # sys.path, so a stray module there would be imported.
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=_env(), cwd=str(cwd or "/"), start_new_session=True)
    out, err = _Reader(proc.stdout, on_output), _Reader(proc.stderr)
    out.start()
    err.start()
    timed_out = cancelled = False
    while True:
        try:
            proc.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            pass
        if cancel is not None and cancel.is_set():
            cancelled = True
            _stop(proc)
            break
        if time.time() - started > timeout:
            timed_out = True
            _stop(proc)
            break
    out.join(2)
    err.join(2)
    return {"exit_code": proc.returncode, "output": out.text(), "errors": err.text(),
            "truncated": out.truncated or err.truncated, "timed_out": timed_out, "cancelled": cancelled,
            "started": datetime.fromtimestamp(started).isoformat(timespec="seconds"),
            "seconds": round(time.time() - started, 1), "host": socket.gethostname().split(".")[0]}


def run_action(action: Action, values: dict, *, hub_db: Path | None = None, cancel: threading.Event | None = None,
               on_output: Callable[[str], None] | None = None, timeout: float | None = None) -> dict:
    """Run a checked action here. Files an action writes are listed by name
    (only those it wrote or rewrote in the reports directory)."""
    out_dir = reports_dir() if action.files else None
    before = _listing(out_dir) if out_dir else {}
    argv = build_argv(action, values, hub_db=hub_db, out_dir=out_dir)
    res = run_argv(argv, timeout or action.timeout, cancel=cancel, on_output=on_output, cwd=out_dir)
    res.update(action=action.name, params=values, ok=res["exit_code"] == 0 and not res["timed_out"]
               and not res["cancelled"])
    if out_dir:
        after = _listing(out_dir)
        res["files"] = sorted(n for n, m in after.items() if before.get(n) != m and not n.endswith(".tmp"))
    return res


def _listing(d: Path) -> dict:
    out = {}
    for p in d.iterdir():
        try:
            st = p.lstat()
        except OSError:
            continue
        if p.is_file() and not p.is_symlink():
            out[p.name] = (st.st_mtime_ns, st.st_size)
    return out


def report_file(name: str) -> Path:
    """A file in the reports directory, by its bare name; ValueError for
    anything else (a path, a link, a missing file, a file still being written)."""
    if not isinstance(name, str) or not name or "/" in name or "\\" in name or name.startswith(".") \
            or "\x00" in name or len(name) > 200 or name.endswith(".tmp"):
        raise ValueError("not a report file name")
    p = reports_dir() / name
    if p.is_symlink() or not p.is_file():
        raise ValueError("no such report file")
    return p


def open_report_file(name: str):
    """report_file(), opened for reading without following a link (a file
    replaced by one between the check and the open is refused)."""
    import stat
    p = report_file(name)
    fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("no such report file")
    return os.fdopen(fd, "rb")

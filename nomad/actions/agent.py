# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad agent`: a site's side of the Console's actions.

Run by sshd as the forced command of the hub's key, it reads one request
(JSON, at most 64 KB) from its input, checks it against this site's own
catalog (only actions marked for sites; parameters checked again here),
runs it, logs one line and answers after a marker line. It runs one action
at a time; one more request may wait up to 30 seconds, and any others are
refused at once. A connection that sends no request within 10 seconds is
closed.

`nomad agent install-key` adds the hub's public key to this account's
authorized_keys, restricted to `nomad agent` (dry run unless --apply).
"""
from __future__ import annotations

import base64
import fcntl
import io
import json
import os
import re
import select
import shlex
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import IO

from nomad.actions import runner
from nomad.actions import spec as catalog
from nomad.actions.remote import MARKER

MAX_REQUEST = 64 * 1024


def _data_dir() -> Path:
    p = Path.home() / ".local" / "share" / "nomad"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _version() -> str:
    try:
        from nomad import __version__
        return __version__
    except Exception:
        return "unknown"


def _answer(out: IO[str], reply: dict) -> None:
    reply.setdefault("v", 1)
    reply.setdefault("nomad_version", _version())
    reply.setdefault("restricted", os.environ.get("SSH_ORIGINAL_COMMAND") is not None)
    out.write("\n" + MARKER + "\n" + json.dumps(reply) + "\n")
    out.flush()


def _log(action: str, values: dict, reply: dict) -> None:
    client = (os.environ.get("SSH_CLIENT") or "local").split()[0]
    line = json.dumps({"at": datetime.now().isoformat(timespec="seconds"), "from": client, "action": action,
                       "params": values, "exit_code": reply.get("exit_code"), "ok": reply.get("ok"),
                       "seconds": reply.get("seconds")})
    try:
        p = _data_dir() / "agent.log"
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _lock(name: str):
    return os.open(_data_dir() / name, os.O_WRONLY | os.O_CREAT, 0o600)


def _slot(wait: float = 30.0):
    """One action at a time on this account, and one request waiting for it
    (up to ``wait`` seconds); any more are refused at once. The place in the
    queue is taken first, so a newcomer can't overtake the one waiting.
    Returns the lock to release, or None."""
    queue = _lock("agent.queue")
    try:
        fcntl.flock(queue, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(queue)
        return None
    try:
        run = _lock("agent.lock")
        deadline = time.time() + wait
        while True:
            try:
                fcntl.flock(run, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return run
            except BlockingIOError:
                if time.time() >= deadline:
                    os.close(run)
                    return None
                time.sleep(0.2)
    finally:
        os.close(queue)


READ_SECONDS = 10.0


def _read_request(stdin) -> bytes:
    """At most MAX_REQUEST + 1 bytes, until the hub closes its side, within
    READ_SECONDS: a connection that sends nothing doesn't keep an agent."""
    try:
        fd = stdin.fileno()
    except (AttributeError, OSError, ValueError, io.UnsupportedOperation):
        return stdin.read(MAX_REQUEST + 1)                       # a test's buffer
    buf = bytearray()
    deadline = time.time() + READ_SECONDS
    while len(buf) <= MAX_REQUEST:
        left = deadline - time.time()
        if left <= 0:
            raise TimeoutError
        ready, _, _ = select.select([fd], [], [], left)
        if not ready:
            raise TimeoutError
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _refuse(stdout, error: str, name: str = "", **extra) -> int:
    reply = {"ok": False, "error": error, **extra}
    _log(name[:60], {}, reply)
    _answer(stdout, reply)
    return 2


def serve(stdin: IO[bytes] | None = None, stdout: IO[str] | None = None) -> int:
    """Answer one request. Exit status 0 when the action ran (whatever its
    own exit status), 2 when the request was refused or couldn't run. Every
    request is logged, refused ones too."""
    stdin = stdin if stdin is not None else sys.stdin.buffer
    stdout = stdout if stdout is not None else sys.stdout
    try:
        raw = _read_request(stdin)
    except TimeoutError:
        return _refuse(stdout, f"no request within {READ_SECONDS:.0f} seconds")
    if len(raw) > MAX_REQUEST:
        return _refuse(stdout, "request too large")
    try:
        req = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _refuse(stdout, "the request is not JSON")
    if not isinstance(req, dict) or req.get("v") != 1:
        return _refuse(stdout, "unknown request version")
    name = req.get("action") if isinstance(req.get("action"), str) else ""
    try:
        action = catalog.get(req.get("action"))
        if catalog.SITE not in action.where:
            raise catalog.ActionError(f"{action.name} runs on the hub, not on a site")
        values = catalog.check(action, req.get("params"))
    except catalog.ActionError as exc:
        return _refuse(stdout, str(exc), name)
    try:
        fd = _slot()
    except OSError as exc:                    # no room for the lock files (a full home, say)
        return _refuse(stdout, f"the action could not start: {exc.strerror or exc}", action.name,
                       action=action.name)
    if fd is None:
        return _refuse(stdout, "busy: another action is running here", action.name, action=action.name)
    try:
        reply = runner.run_action(action, values)
    except Exception as exc:                  # never leave the hub without an answer
        return _refuse(stdout, f"the action could not run: {type(exc).__name__}", action.name, action=action.name)
    finally:
        os.close(fd)
    _log(action.name, values, reply)
    _answer(stdout, reply)
    return 0


# --- installing the hub's key ----------------------------------------------------------

_KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
              "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com", "ssh-rsa")
_COMMENT = re.compile(r"[A-Za-z0-9@._+-]{1,100}")
_FROM = re.compile(r"[A-Za-z0-9.:*?/!,-]{1,200}")


def parse_public_key(text: str) -> tuple[str, str, str]:
    """(type, blob, comment) of one public key line, or ValueError."""
    parts = text.strip().split()
    if len(parts) not in (2, 3):
        raise ValueError("a public key is: TYPE KEY [COMMENT]")
    kind, blob = parts[0], parts[1]
    comment = parts[2] if len(parts) == 3 else "nomad-actions"
    if kind not in _KEY_TYPES:
        raise ValueError(f"not a key type sshd takes here: {kind[:40]}")
    try:
        decoded = base64.b64decode(blob, validate=True)
    except ValueError:
        raise ValueError("the key is not base64") from None
    n = int.from_bytes(decoded[:4], "big") if len(decoded) >= 4 else 0
    if decoded[4:4 + n].decode("ascii", errors="replace") != kind:
        raise ValueError("the key's contents don't match its type")
    if not _COMMENT.fullmatch(comment):
        raise ValueError("the key's comment may hold letters, digits and @._+- only")
    return kind, blob, comment


def forced_command() -> str:
    """What sshd runs for the hub's key. sshd hands it to the login shell, so
    the interpreter's path is quoted. The login shell of some sites sets
    PYTHONHOME/PYTHONPATH for other software, which breaks a virtual
    environment; and `python -m` puts the working directory (the home
    directory, as sshd starts it) first on sys.path, hence `cd /`."""
    exe = sys.executable
    if any(c in exe for c in '"\\\n\r\x00') or not os.path.isabs(exe):
        raise ValueError(f"this Python's path can't go in a key line: {exe!r}")
    return f"cd / && exec /usr/bin/env -u PYTHONHOME -u PYTHONPATH {shlex.quote(exe)} -m nomad.cli agent"


def key_line(pubkey: str, from_pattern: str | None = None) -> str:
    kind, blob, comment = parse_public_key(pubkey)
    opts = f'restrict,command="{forced_command()}"'
    if from_pattern:
        if not _FROM.fullmatch(from_pattern):
            raise ValueError("--from takes host or address patterns, comma-separated")
        opts += f',from="{from_pattern}"'
    return f"{opts} {kind} {blob} {comment}"


def install_key(pubkey: str, *, from_pattern: str | None = None, apply: bool = False,
                path: Path | None = None) -> tuple[str, list[str]]:
    """What adding the hub's key to authorized_keys would change (and, with
    apply, change it). Returns (status, lines to show)."""
    line = key_line(pubkey, from_pattern)
    _, blob, _ = parse_public_key(pubkey)
    path = path or Path.home() / ".ssh" / "authorized_keys"
    if path.is_symlink():
        raise ValueError(f"{path} is a link: add the line by hand where it points")
    old = path.read_text().splitlines() if path.exists() else []
    same = [i for i, ln in enumerate(old) if blob in ln.split()]
    if same and len(same) == 1 and old[same[0]] == line:
        return "present", [f"{path} already has this key, restricted to `nomad agent`."]
    new = [ln for i, ln in enumerate(old) if i not in same] + [line]
    msg = []
    if same:
        msg.append(f"{len(same)} line(s) of {path} hold this key with other options: replaced by")
    else:
        msg.append(f"added to {path}:")
    msg.append("  " + line)
    if not apply:
        msg.append("Nothing written (dry run). Add --apply to write it.")
        return "would-change", msg
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + ".bak-nomad-agent-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(path, backup)
        msg.append(f"Backup: {backup}")
    tmp = path.with_name(path.name + ".nomad-tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(new) + "\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    msg.append("Written.")
    return "changed", msg

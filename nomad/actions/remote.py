# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Running an action on a site, from the hub.

The hub opens ssh to the site with a key of its own (not the key `nomad
sync` uses) that the site lets run one thing only: `nomad agent`, which
reads one request, checks it against its own catalog and answers. The key
line on the site says so (`restrict,command="... nomad agent"`), so whoever
holds the key, the hub included, can ask a site for catalog actions and
nothing else. See docs/actions.md.

The ssh command never reuses another connection (a shared master opened
with a stronger key would carry the request instead), offers this key
first, refuses an unknown host key and allocates no terminal.
"""
from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

from nomad.actions.spec import ActionError

MARKER = "__NOMAD_AGENT_REPLY__"
MAX_REPLY = 16_000_000          # bytes: 2 MB of output, escaped


def _config() -> dict:
    from nomad.config import find_config, read_toml
    p = find_config()
    if p is None:
        return {}
    try:
        return read_toml(p)
    except Exception:
        return {}


def hub_settings() -> dict:
    """The hub's sites and combined database, read quietly from nomad.toml's
    [hub] or the older sync.toml (the same files `nomad sync` reads)."""
    from nomad.config import read_toml
    cfg = _config()
    hub = cfg.get("hub") if isinstance(cfg.get("hub"), dict) else None
    raw, output = [], None
    if hub and hub.get("sites"):
        raw, output = hub.get("sites") or [], hub.get("output_db")
    else:
        legacy = Path.home() / ".config" / "nomad" / "sync.toml"
        if legacy.exists():
            try:
                data = read_toml(legacy)
                raw, output = data.get("sites") or [], data.get("output_db")
            except Exception:
                raw = []
    sites = []
    for s in raw:
        if not isinstance(s, dict) or not s.get("name") or not s.get("host"):
            continue
        sites.append({"name": str(s["name"]), "host": str(s["host"]),
                      "user": str(s.get("ssh_user") or s.get("user") or ""),
                      "port": int(s["port"]) if str(s.get("port", "")).isdigit() else None})
    db = Path(output).expanduser() if output else Path.home() / ".local" / "share" / "nomad" / "combined.db"
    actions = cfg.get("actions") if isinstance(cfg.get("actions"), dict) else {}
    key = Path(actions["agent_key"]).expanduser() if actions.get("agent_key") else \
        Path.home() / ".config" / "nomad" / "agent" / "id_ed25519"
    return {"sites": sites, "combined_db": db, "agent_key": key}


def site(name: str) -> dict:
    for s in hub_settings()["sites"]:
        if s["name"] == name:
            return s
    raise ActionError(f"{name!r} is not one of the hub's sites")


def ensure_key(path: Path | None = None) -> tuple[Path, str]:
    """The hub's agent key (made the first time, ed25519, no passphrase,
    readable only by its owner) and its public line."""
    import fcntl
    path = path or hub_settings()["agent_key"]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        with open(path.parent / ".lock", "w") as lock:       # two first runs at once make one key
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not path.exists():
                comment = f"nomad-actions@{socket.gethostname().split('.')[0]}"
                subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path)],
                               check=True, stdin=subprocess.DEVNULL, capture_output=True)
            os.chmod(path, 0o600)
            pub = Path(str(path) + ".pub").read_text().strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ActionError(f"the hub's agent key at {path} can't be made or read: {exc}") from None
    return path, pub


def ssh_argv(s: dict, key: Path, timeout: int = 10) -> list[str]:
    argv = ["ssh", "-T", "-i", str(key),
            "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes",
            "-o", f"ConnectTimeout={int(timeout)}", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4"]
    if s.get("port"):
        argv += ["-p", str(int(s["port"]))]
    target = f"{s['user']}@{s['host']}" if s.get("user") else s["host"]
    # The site's key line runs `nomad agent` whatever is asked; asking for it
    # too means a site whose line lacks the restriction still runs only the agent.
    return argv + ["--", target, "nomad", "agent"]


_LINE = "\n" + MARKER + "\n"


def parse_reply(text: str) -> dict:
    """The agent's answer: the JSON line after the last marker line (a site
    may print a banner before it; the JSON itself holds no raw newline)."""
    i = ("\n" + text).rfind(_LINE)
    if i < 0:
        raise ActionError("the site did not answer as nomad agent does (is its key line installed?)")
    body = ("\n" + text)[i + len(_LINE):].split("\n", 1)[0]
    def no_constants(c):                      # NaN and Infinity aren't JSON a browser takes
        raise ValueError(c)
    try:
        reply = json.loads(body, parse_constant=no_constants)
    except (ValueError, RecursionError):
        raise ActionError("the site's answer could not be read") from None
    if not isinstance(reply, dict) or reply.get("v") != 1:
        raise ActionError("the site answered in a form this nomad doesn't know (versions differ?)")
    return reply


def _text(v, limit: int) -> str:
    # A lone surrogate (valid in JSON, not in UTF-8) becomes "?".
    return v[:limit].encode("utf-8", "replace").decode("utf-8") if isinstance(v, str) else ""


def _seconds(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except (OverflowError, ValueError):
        return None
    return round(f, 1) if math.isfinite(f) and 0 <= f < 1e7 else None


def clean_reply(reply: dict, action_name: str, values: dict) -> dict:
    """What the hub passes on from a site's answer: known fields, of the
    expected types and sizes. A site is another machine; its answer is data."""
    from nomad.actions.runner import MAX_OUTPUT

    def flag(k):
        return reply.get(k) is True

    code = reply.get("exit_code")
    return {"v": 1, "action": action_name, "params": values,
            "ok": flag("ok"), "exit_code": (code if isinstance(code, int) and not isinstance(code, bool)
                                            and -1000 < code < 1000 else None),
            "output": _text(reply.get("output"), MAX_OUTPUT), "errors": _text(reply.get("errors"), MAX_OUTPUT),
            "truncated": flag("truncated"), "timed_out": flag("timed_out"), "cancelled": flag("cancelled"),
            "restricted": flag("restricted"),
            "seconds": _seconds(reply.get("seconds")),
            "started": _text(reply.get("started"), 40), "host": _text(reply.get("host"), 100),
            "nomad_version": _text(reply.get("nomad_version"), 40), "error": _text(reply.get("error"), 2000)}


class _Capped(threading.Thread):
    """Reads a pipe to the end, keeping the first ``limit`` bytes (or the
    last ones, tail=True); says whether more came."""

    def __init__(self, stream, limit: int, tail: bool = False):
        super().__init__(daemon=True)
        self.stream, self.limit, self.tail = stream, limit, tail
        self.buf = bytearray()
        self.over = False

    def run(self):
        for chunk in iter(lambda: self.stream.read1(65536), b""):
            if self.tail:
                self.buf += chunk
                del self.buf[:-self.limit]
            elif len(self.buf) + len(chunk) > self.limit:
                self.over = True
                return
            else:
                self.buf += chunk


def _allow_unrestricted() -> bool:
    a = _config().get("actions")
    return isinstance(a, dict) and a.get("allow_unrestricted") is True


def run_remote(site_name: str, action_name: str, values: dict, *, timeout: float,
               cancel: threading.Event | None = None) -> dict:
    """Ask a site's agent for one action and return its answer, with how the
    connection went. Output arrives when the action ends (no streaming)."""
    s = site(site_name)
    key, _ = ensure_key()
    request = json.dumps({"v": 1, "action": action_name, "params": values}).encode()
    base = {"v": 1, "action": action_name, "params": values, "site": site_name, "ok": False}
    started = time.time()
    proc = subprocess.Popen(ssh_argv(s, key), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    out, err = _Capped(proc.stdout, MAX_REPLY), _Capped(proc.stderr, 20000, tail=True)
    out.start()
    err.start()
    try:
        proc.stdin.write(request)
        proc.stdin.close()
    except OSError:
        pass                                  # the site hung up first: its stderr says why
    # The site may wait for its slot (30 s), run the action, then stop it.
    limit = timeout + 75
    while proc.poll() is None:
        time.sleep(0.2)
        if cancel is not None and cancel.is_set():
            proc.kill()
            proc.wait(5)
            return {**base, "cancelled": True, "error": "cancelled (the site may finish the action on its own)"}
        if out.over:
            proc.kill()
            proc.wait(5)
            return {**base, "error": "the site's answer was larger than any answer of nomad agent"}
        if time.time() - started > limit:
            proc.kill()
            proc.wait(5)
            return {**base, "timed_out": True, "error": "no answer in time"}
    out.join(5)
    err.join(5)
    if out.over:
        return {**base, "error": "the site's answer was larger than any answer of nomad agent"}
    try:
        reply = clean_reply(parse_reply(bytes(out.buf).decode("utf-8", errors="replace")), action_name, values)
    except ActionError as exc:
        said = bytes(err.buf).decode("utf-8", errors="replace").strip().splitlines()
        return {**base, "ssh_exit": proc.returncode,
                "error": str(exc) + (f" — ssh said: {said[-1][:300]}" if said else "")}
    reply.update(site=site_name, ssh_seconds=round(time.time() - started, 1))
    if not reply["restricted"] and not _allow_unrestricted():
        return {**base, "restricted": False,
                "error": f"{site_name} let the hub's key run more than `nomad agent` (its key line has no "
                         "command=, or another key of the hub's was used). Install the key there with "
                         "`nomad agent install-key` (docs/actions.md); the answer was not used."}
    return reply

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The hub's side of nomad-gate (1.25): ask a cluster's gate to act as a
person, over ssh with the gate's own key.

Like the actions' agent key (nomad actions key), and kept apart from it:
the gate key's authorized_keys line on the cluster forces `nomad-gate
serve`, from the hub's addresses only, so whoever holds it can ask the gate
and nothing else. A site is asked this way when its [[hub.sites]] entry
says so:

    [hub]
    # gate_key = "~/.config/nomad/gate/id_ed25519"   # the default (nomad gate key makes it)

    [[hub.sites]]
    name = "spydur"
    host = "spydur.richmond.edu"
    ssh_user = "installer"
    submit = "gate"            # else "copy": the Console gives the person the command to paste
    # gate_user = "installer"  # default: ssh_user
    # gate_host = "..."        # default: host

A request is checked here with the gate's own rules before it is sent, and
by the gate again (as the cluster's account, then as the person). Only
the gate's key is offered: no agent, no other key, no shared connection,
and none of this account's ssh settings (-F /dev/null: a key or a proxy
named there is never used), so the cluster is reached directly.
"""
import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from nomad.submit import gate

SSH_TIMEOUT = gate.LONGEST + 60


def SSH_ENV() -> dict:                                     # noqa: N802
    """ssh's environment: nothing of this process's but where home is."""
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.path.expanduser("~"), "LANG": "C"}
DEFAULT_KEY = "~/.config/nomad/gate/id_ed25519"
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_USER = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")


class GateError(Exception):
    """A site that can't be asked: no gate set up, or its settings are wrong."""


def hub_sites(config_path: str | Path | None = None) -> tuple:
    """([[hub.sites]] as written, [hub]) from the nomad.toml in use (-c,
    else the first found); ([], {}) when there is none."""
    from nomad.actions.remote import _config
    cfg = _config(config_path)
    hub = cfg.get("hub") if isinstance(cfg.get("hub"), dict) else {}
    sites = [s for s in hub.get("sites") or [] if isinstance(s, dict) and s.get("name")]
    return sites, hub


def site_named(sites: list, name: str) -> dict | None:
    for s in sites or []:
        if isinstance(s, dict) and s.get("name") == name:
            return s
    return None


def mode(site: dict | None) -> str:
    """How the Console sends a person's job to this site: "gate" or "copy"."""
    return "gate" if str((site or {}).get("submit") or "").strip().lower() == "gate" else "copy"


def key_path(hub: dict | None = None, site: dict | None = None) -> Path:
    return Path(os.path.expanduser(str((site or {}).get("gate_key") or (hub or {}).get("gate_key")
                                       or DEFAULT_KEY)))


def settings(site: dict, hub: dict | None = None) -> dict:
    """{name, host, user, port, key} of a site's gate; else GateError, saying why."""
    name = str(site.get("name") or "(a site with no name)")
    if mode(site) != "gate":
        raise GateError(f'{name}: no gate (submit = "gate" isn\'t set in its [[hub.sites]])')
    host = str(site.get("gate_host") or site.get("host") or "")
    user = str(site.get("gate_user") or site.get("ssh_user") or site.get("user") or "")
    port = site.get("port")
    if not _HOST.fullmatch(host):
        raise GateError(f"{name}: host {host!r} isn't a host name")
    if not _USER.fullmatch(user):
        raise GateError(f"{name}: ssh_user {user!r} isn't an account name")
    if port is not None and not str(port).isdigit():
        raise GateError(f"{name}: port {port!r} isn't a number")
    key = key_path(hub, site)
    if not key.is_file():
        raise GateError(f"{name}: the gate's key {key} isn't there (nomad gate key makes it)")
    return {"name": name, "host": host, "user": user, "port": int(port) if port is not None else None,
            "key": str(key)}


def ssh_argv(s: dict, words: list) -> list:
    """The ssh command that asks the gate ``words`` (an op and its options)."""
    argv = ["ssh", "-F", "/dev/null", "-T", "-n", "-x", "-a", "-i", s["key"],
            "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
            "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes",
            "-o", "RequestTTY=no", "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4"]
    if s.get("port"):
        argv += ["-p", str(int(s["port"]))]
    return argv + ["-l", s["user"], "--", s["host"], shlex.join([gate.GATE] + list(words))]


def request(op: str, user: str | None = None, **options) -> list:
    """A request's words, checked by the gate's own rules (gate.Refused if
    not): options as keywords, e.g. script="/x/job.sh", cpus=8, keep=True,
    localscratch=True (or False: --no-localscratch)."""
    words = [op] + (["--user", user] if user is not None else [])
    for k, v in options.items():
        if v is None:
            continue
        flag = "--" + k.replace("_", "-")
        if k == "localscratch":
            words.append("--localscratch" if v else "--no-localscratch")
        elif v is True:
            words.append(flag)
        elif v is not False:
            words += [flag, str(v)]
    gate.parse(words)
    return words


def ask(site: dict, words: list, hub: dict | None = None, *, run_=None, timeout: int = SSH_TIMEOUT) -> dict:
    """The site's gate's answer to ``words`` (a dict with "ok", "error"
    when not ok, and "where": hub, ssh or site when it went wrong before the
    gate answered). Never raises for what ssh or the gate said."""
    run_ = run_ or subprocess.run
    try:
        gate.parse(list(words))
        s = settings(site, hub)
    except (GateError, gate.Refused) as e:
        return {"ok": False, "error": str(e), "where": "hub"}
    sends = bool(words) and words[0] in ("submit", "again")
    maybe = {"maybe": True} if sends else {}
    later = " (it may have been submitted: check the jobs before sending it again)" if sends else ""
    try:
        r = run_(ssh_argv(s, words), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                 stderr=subprocess.PIPE, text=True, errors="replace", timeout=timeout, env=SSH_ENV())
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"{s['name']}: no answer in {timeout} s{later}", "where": "ssh", **maybe}
    except OSError as e:
        return {"ok": False, "error": f"ssh could not run here: {e}", "where": "hub"}
    for line in reversed((r.stdout or "").strip().splitlines()):
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if isinstance(doc, dict) and "ok" in doc:
            return doc
    # No answer: ssh never got in (nothing was sent), or the connection went
    # while the gate was at it (then a job may have been sent).
    never = r.returncode == 255 and any(m in (r.stderr or "") for m in _NEVER_IN)
    if never:
        return {"ok": False, "error": _why(s, r), "where": "ssh"}
    return {"ok": False, "error": _why(s, r) + later, "where": "ssh" if r.returncode == 255 else "site", **maybe}


# What ssh says when it didn't get in at all.
_NEVER_IN = ("Could not resolve", "Connection refused", "Connection timed out", "No route to host",
             "Network is unreachable", "Host key verification failed", "Permission denied",
             "host key", "Connection closed by", "kex_exchange_identification", "Too many authentication",
             "Name or service not known", "Operation timed out", "Bad configuration")


def _why(s: dict, r) -> str:
    err = (r.stderr or "").strip()
    target = f"{s['user']}@{s['host']}"
    if r.returncode == 255:
        last = err.splitlines()[-1] if err else "ssh failed"
        if "host key" in err.lower():
            return (f"couldn't reach {target}: {last} (this account doesn't know {s['host']}'s host key: "
                    "ssh to it once by hand, or add it to ~/.ssh/known_hosts)")
        if "Permission denied" in err:
            return (f"couldn't reach {target}: {last} (the gate's key isn't in its authorized_keys, or not "
                    f"for this address: nomad gate install-key there)")
        return f"couldn't reach {target}: {last}"
    tail = (err or (r.stdout or "").strip())[-400:]
    if not tail:
        return (f"{target} said nothing (exit {r.returncode}): is its key line's command= "
                f"nomad-gate serve? (nomad gate install-key writes it)")
    if "nomad-gate" in tail and ("not found" in tail or "No such file" in tail):
        return f"{target}: nomad-gate isn't installed there ({tail}): install_nomad_for_users.sh 1.25 or later"
    return f"{target}: the gate didn't answer (exit {r.returncode}): {tail}"


def call(site: dict, op: str, user: str | None = None, hub: dict | None = None, *, run_=None,
         timeout: int = SSH_TIMEOUT, **options) -> dict:
    """Ask the site's gate ``op`` as ``user`` (see request and ask)."""
    try:
        words = request(op, user, **options)
    except gate.Refused as e:
        return {"ok": False, "error": str(e), "where": "hub"}
    return ask(site, words, hub, run_=run_, timeout=timeout)


# ── the gate's key, on the hub and on a site ────────────────────────────────

def ensure_key(path: Path | None = None) -> tuple[Path, str]:
    """The hub's gate key (made the first time) and its public line."""
    from nomad.actions.remote import ensure_key as ensure
    return ensure(path or key_path(hub_sites()[1]), comment="nomad-gate")


_PLAIN_PATH = re.compile(r"/[A-Za-z0-9/._-]{1,1000}")


def forced_command(gate_path: str, owners: set | None = None) -> str:
    """What sshd runs for the gate's key (through this account's login
    shell, so the path holds only letters, digits and / . _ -). ``owners``:
    the path must be safe to run from for them (gate.unsafe)."""
    if not _PLAIN_PATH.fullmatch(gate_path) or os.path.basename(gate_path) != gate.GATE \
            or "/../" in gate_path + "/":
        raise ValueError(f"--gate: the full path of {gate.GATE} on this cluster, in letters, digits and / . _ - "
                         f"({gate_path!r} isn't)")
    if owners is not None:
        why = gate.unsafe(gate_path, owners)
        if why:
            raise ValueError(f"--gate {gate_path}: not safe to run from ({why}); install_nomad_for_users.sh "
                             "1.25.0 or later puts it there")
    return f"{gate_path} serve"


def install_key(pubkey: str, *, origins: str | None, gate_path: str, apply: bool = False,
                path: Path | None = None) -> tuple:
    """What adding the hub's gate key to authorized_keys changes (and, with
    apply, changing it): restricted to ``gate_path serve``, from ``origins``."""
    from nomad.actions.agent import install_key as install
    return install(pubkey, from_pattern=origins, apply=apply, path=path,
                   command=forced_command(gate_path, {0, os.getuid()}), what=f"`{gate.GATE} serve`",
                   tag="nomad-gate")

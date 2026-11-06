# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Draining a Slurm node, and resuming it (1.31.0): `nomad node drain|resume`.

A dry run unless ``apply``: what Slurm says of the node now, the jobs
running there, and the one scontrol command that would run, after asking
sudo whether it may (``sudo -n -l``, which runs nothing). Applied, that
command runs, and the node is read again to say what Slurm made of it.

Who runs scontrol is set in this account's nomad.toml, and nothing is
changed until it is:

    [actions]
    slurm_as = "slurm"     # as Slurm's own account, through sudo (one sudoers line)
    # slurm_as = "self"    # as this account, when it is itself a Slurm administrator

scontrol is run by its full path, found on PATH (then in /usr/bin,
/usr/local/bin, /opt/slurm/bin), and not at all when someone else could have
put it there -- nor when someone else could change the nomad running here
(its Python and every folder it imports from), which would then run scontrol
as Slurm's account. No shell reads anything. Slurm's programs all run with
the same environment, without SLURM_* settings (sudo drops them for the
change): what is read is what is changed. The node is one name (letters,
digits, . _ -; not ALL); the reason one line of printable characters, at
most 200, without " or \\. Slurm records the account that ran scontrol as
the reason's author (slurm, through sudo), so the reason it keeps says who
asked: "TEXT (NETID via nomad)". Each change, and each refusal to make one,
goes to syslog (authpriv) and to ~/.local/share/nomad/nodes.log.
"""
from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from nomad.actions.spec import _NAME, text_problem

DRAIN, RESUME = "drain", "resume"
SEARCH = "/usr/bin:/usr/local/bin:/opt/slurm/bin"
SUDO_SEARCH = "/usr/bin:/bin:/usr/sbin:/sbin"
TIMEOUT = 25                       # seconds, each look at Slurm (scontrol show, squeue): more than
                                   # scontrol's own wait for a busy controller
SUDO_TIMEOUT = 10                  # seconds, sudo -l (it asks no one)
UPDATE_TIMEOUT = 40                # seconds, the change itself (two controllers' time-outs)
CHECKED_FOR = 600                  # seconds a passed check of nomad's own install is taken as true
JOBS_SHOWN = 50
TAG = "nomad-node"

_SUFFIX = re.compile(r"[*~#!%$@^-]+$")            # sinfo's marks, should one appear
_TAIL = re.compile(r"\s\[([^\]@\s]*)@([^\]\s]*)\]$")
_AFTER_REASON = re.compile(r"\s(?:Comment|Extra|ReservationName|InstanceId|InstanceType)=")
_BUSY = {"ALLOCATED", "ALLOC", "MIXED", "COMPLETING"}


class NodeError(ValueError):
    """A request refused before anything ran: what was wrong, in words."""


# --- what Slurm says ------------------------------------------------------------------

def parse_node(text: str) -> dict | None:
    """``scontrol show node -o NODE`` as a dict: name, state (as printed),
    base and flags, reason (and who set it, when), cores in use and in all,
    partitions, GPUs (Gres); None when there is no node line."""
    line = next((ln.strip() for ln in (text or "").splitlines() if ln.strip().startswith("NodeName=")), None)
    if line is None:
        return None
    head, sep, tail = line.partition(" Reason=")
    reason = reason_user = reason_time = None
    raw = tail if sep else None
    if sep:
        m = _AFTER_REASON.search(" " + tail)
        if m:
            tail = tail[:max(0, m.start() - 1)]
        tail = tail.strip()
        t = _TAIL.search(tail)
        if t:
            reason_user, reason_time = t.group(1) or None, t.group(2) or None
            tail = tail[:t.start()].strip()
        reason = tail or None
    pairs = {}
    for tok in head.split():
        k, eq, v = tok.partition("=")
        if eq and k not in pairs:
            pairs[k] = v
    state = pairs.get("State", "")
    parts = [p for p in state.split("+") if p]
    base = _SUFFIX.sub("", parts[0]).upper() if parts else ""
    flags = [_SUFFIX.sub("", p).upper() for p in parts[1:]]

    def num(k):
        v = pairs.get(k)
        return int(v) if v is not None and v.isdigit() else None

    gres = pairs.get("Gres")
    return {"node": pairs.get("NodeName"), "state": state or None, "base": base or None, "flags": flags,
            "reason": reason, "reason_user": reason_user, "reason_time": reason_time, "reason_raw": raw,
            "cpus_alloc": num("CPUAlloc"), "cpus_total": num("CPUTot"),
            "partitions": pairs.get("Partitions") if pairs.get("Partitions") not in (None, "(null)") else None,
            "gres": gres if gres not in (None, "(null)") else None}


def drained(n: dict) -> bool:
    return "DRAIN" in (n.get("flags") or [])


def has_reason(n: dict, reason: str) -> bool:
    """Whether Slurm keeps ``reason`` as the node's reason: the text after
    "Reason=" is it, then the end, " [who@when]" or the next field (the
    parsed reason can't tell a reason holding " Comment=" from the field)."""
    raw = n.get("reason_raw")
    if raw is None or not raw.startswith(reason):
        return False
    rest = raw[len(reason):]
    return rest == "" or rest.startswith(" [") or bool(_AFTER_REASON.match(rest))


def taken(n: dict | None, what: str, reason: str | None) -> bool:
    """Whether Slurm shows the change: drained with this reason; or resumed --
    no longer drained, down or failing (not responding yet is how a node
    that was down comes back)."""
    if n is None:
        return False
    if what == DRAIN:
        return drained(n) and has_reason(n, reason or "")
    return not resumable(n)


def resumable(n: dict) -> bool:
    """What State=RESUME acts on: a drained or draining node, or one down or failing."""
    flags = n.get("flags") or []
    return drained(n) or n.get("base") == "DOWN" or "FAIL" in flags or "FAILING" in flags


def described(n: dict) -> str:
    """The node's state in words."""
    base, flags = n.get("base") or "?", n.get("flags") or []
    if drained(n):
        word = "draining, its jobs still running" if base in _BUSY else \
            "drained and down" if base == "DOWN" else "drained"
    elif base == "DOWN":
        word = "down"
    else:
        word = base.lower()
    if "NOT_RESPONDING" in flags:
        word += ", not responding"
    return f"{word} ({n.get('state')})" if flags else word


def parse_jobs(text: str) -> list[dict]:
    """squeue's lines, as `_jobs` asks for them."""
    out = []
    for ln in (text or "").splitlines():
        f = ln.strip().split("|")
        if len(f) != 8 or not f[0]:
            continue
        out.append({"job": f[0], "user": f[1], "partition": f[2], "state": f[3], "running": f[4],
                    "left": f[5], "cpus": int(f[6]) if f[6].isdigit() else None,
                    "nodes": int(f[7]) if f[7].isdigit() else None})
    return out


# --- running Slurm's programs ---------------------------------------------------------

def _env() -> dict:
    """The environment of every Slurm program here: this one without SLURM_*
    (sudo drops them for the change, so the reads do without them too: what
    is read is the cluster that is changed), dates in Slurm's standard form."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLURM_")}
    env.update(LANG="C", LC_ALL="C", SLURM_TIME_FORMAT="standard")
    return env


def _dropped() -> list[str]:
    return sorted(k for k in os.environ if k.startswith("SLURM_"))


def _run(argv: list[str], timeout: float = TIMEOUT) -> tuple[int, str, str]:
    """A command line (a list: no shell), with no input, in C locale."""
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           errors="replace", timeout=timeout, env=_env(), cwd="/")
    except subprocess.TimeoutExpired:
        return 124, "", f"{os.path.basename(argv[0])} did not answer within {timeout:.0f} seconds"
    except OSError as exc:
        return 127, "", f"{argv[0]} could not run: {exc.strerror or exc}"
    return p.returncode, p.stdout, p.stderr


def find(name: str, env: dict | None = None) -> str | None:
    """``name``'s full path: as PATH finds it, else in SEARCH; NodeError when
    someone else could have put it there (it would run as Slurm's account)."""
    from nomad.submit import gate
    env = os.environ if env is None else env
    found = shutil.which(name, path=env.get("PATH") or os.defpath) or shutil.which(name, path=SEARCH)
    if not found:
        return None
    found = os.path.abspath(found)
    why = gate.program_open(found)
    if why:
        raise NodeError(f"the {name} found is {found}, and {why}: someone else could have put it there")
    return found


def _account() -> str:
    try:
        return getpass.getuser()
    except Exception:                              # noqa: BLE001
        return str(os.getuid())


def slurm_as(config: dict | None) -> tuple[str | None, str | None]:
    """(who runs scontrol: an account name or "self", why not) from
    ``[actions] slurm_as``."""
    actions = (config or {}).get("actions")
    v = actions.get("slurm_as") if isinstance(actions, dict) else None
    if v is None or v == "":
        return None, None
    if not isinstance(v, str) or not (v == "self" or _NAME.fullmatch(v)) or v.startswith("-"):
        return None, f"[actions] slurm_as = {str(v)[:40]!r} is neither an account's name nor \"self\""
    return v, None


def sudo_line(account: str, runs_as: str, scontrol: str) -> str:
    """The sudoers line that lets ``account`` run these two scontrol commands
    as ``runs_as``, and no other scontrol command (sudo's * also matches
    spaces: node updates only, then)."""
    return (f"{account} ALL=({runs_as}) NOPASSWD: {scontrol} update NodeName\\=* State\\=DRAIN Reason\\=*, "
            f"{scontrol} update NodeName\\=* State\\=RESUME")


# --- the nomad running here -----------------------------------------------------------

def _mode_open(p: str, st, private: set) -> str | None:
    """Why ``p``'s mode lets others change it (a person's own group's writing is theirs)."""
    from nomad.submit import gate
    mine = st.st_gid in private and not (st.st_mode & 0o020 and gate._acl(p))
    if st.st_mode & (0o002 if mine else 0o022):
        return f"{p} can be changed by others (mode {stat.S_IMODE(st.st_mode):o})"
    return None


def _shuts(st, owners: set) -> bool:
    """A folder no one but its owner (root or this account) can pass through
    (no x for its group or others; with an access list, its mask): what is
    below it is out of everyone else's reach by any name through it."""
    return stat.S_ISDIR(st.st_mode) and st.st_uid in owners and not st.st_mode & 0o011


def _route(q: str, owners: set, private: set) -> tuple[str | None, bool]:
    """``q`` and each folder on the way to it from /: (why one of them could be
    changed by someone else, else None; whether a folder on the way shuts
    everyone else out). Each must belong to ``owners``; above such a folder
    (and it) none may be writable by others, a sticky folder (as /tmp) aside;
    below it, their modes reach no one else."""
    from nomad.submit import gate
    parts = [q]
    while parts[-1] != "/":
        parts.append(os.path.dirname(parts[-1]))
    parts.reverse()
    shut = False
    for i, p in enumerate(parts):
        try:
            st = os.lstat(p)
        except OSError as e:
            return f"{p}: {e.strerror}", shut
        if st.st_uid not in owners:
            return f"{p} belongs to {gate._name_of(st.st_uid)}", shut
        if not shut and not stat.S_ISLNK(st.st_mode):
            sticky = i < len(parts) - 1 and stat.S_ISDIR(st.st_mode) and st.st_mode & stat.S_ISVTX
            why = None if sticky else _mode_open(p, st, private)
            if why:
                return why, shut
        shut = shut or _shuts(st, owners)
    return None, shut


def _unsafe(path: str, owners: set, private: set) -> tuple[str | None, bool]:
    """gate.unsafe for the nomad running here: ``path`` as named and as it
    really is, each route from / (_route); and whether the real one is shut
    to everyone else."""
    p = os.path.abspath(path)
    shut = False
    for q in dict.fromkeys((p, os.path.realpath(p))):
        why, shut = _route(q, owners, private)
        if why:
            return why, shut
    return None, shut


def _tree_open(top: str, owners: set, private: set, shut: bool = False) -> str | None:
    """Why something in ``top`` could be changed by someone else, else None:
    an entry another account owns, or (unless a folder on the way to ``top``
    shuts everyone else out) others may write; where a link leads, checked
    from /."""
    from nomad.submit import gate
    seen = set()
    for d, dirs, files in os.walk(top):
        for n in dirs + files:
            x = os.path.join(d, n)
            try:
                st = os.lstat(x)
            except OSError:
                continue
            if st.st_uid not in owners:
                return f"{x} belongs to {gate._name_of(st.st_uid)}"
            if stat.S_ISLNK(st.st_mode):
                real = os.path.realpath(x)
                if real not in seen:
                    seen.add(real)
                    why, _ = _unsafe(real, owners, private)
                    if why:
                        return f"{x} leads to {real}: {why}"
                continue
            why = None if shut else _mode_open(x, st, private)
            if why:
                return why
    return None


def install_problem() -> str | None:
    """Why someone else could change the nomad running here -- its Python,
    and every folder Python imports from (and the folders above them) --
    else None. That code would run scontrol as Slurm's account. Owners:
    root and this account. Below a folder only its owner can pass through
    (~/.local at 700, say), modes don't matter: no one else reaches it."""
    from nomad.submit import gate
    import nomad
    owners = {0, os.getuid()}
    private = gate.private_groups(_account())
    why, _ = _unsafe(sys.executable, owners, private)
    if why:
        return why
    here = os.path.realpath(os.getcwd())
    walked: list[str] = []
    for entry in [os.path.dirname(os.path.abspath(nomad.__file__)), *sys.path]:
        p = os.path.realpath(entry or here)
        if not os.path.lexists(p):
            # whoever could make it could put modules there, ahead of others
            parent = os.path.dirname(p)
            while parent != "/" and not os.path.lexists(parent):
                parent = os.path.dirname(parent)
            why, _ = _unsafe(parent, owners, private)
        else:
            why, shut = _unsafe(p, owners, private)
            # the working folder, as `python -m` puts it first: checked as a place, not walked
            if not why and os.path.isdir(p) and p not in ("/", here) \
                    and not any(p == w or p.startswith(w.rstrip("/") + "/") for w in walked):
                why = _tree_open(p, owners, private, shut)
                walked.append(p)
        if why:
            return why
    return None


def _install_key() -> str:
    import nomad
    return f"{sys.executable}\n{os.path.realpath(sys.prefix)}\n{getattr(nomad, '__version__', '')}\n"


def _install_checked() -> str | None:
    """install_problem, its pass taken as still true for CHECKED_FOR seconds
    (a walk of the whole environment, every time, would be slow)."""
    mark, key = Path.home() / ".local" / "share" / "nomad" / "install.ok", _install_key()
    try:
        st = mark.stat()
        if time.time() - st.st_mtime < CHECKED_FOR and st.st_uid == os.getuid() and not st.st_mode & 0o022 \
                and mark.read_text() == key:
            return None
    except OSError:
        pass
    why = install_problem()
    if why is None:
        try:
            mark.parent.mkdir(parents=True, exist_ok=True)
            tmp = mark.with_name(f"install.ok.{os.getpid()}")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key)
            os.replace(tmp, mark)
        except OSError:
            pass
    return why


def update_argv(scontrol: str, node: str, what: str, reason: str | None) -> list[str]:
    """scontrol's own words: one node, one state, and (to drain) the reason as one word."""
    words = [scontrol, "update", f"NodeName={node}"]
    if what == DRAIN:
        return words + ["State=DRAIN", f"Reason={reason}"]
    return words + ["State=RESUME"]


def full_argv(update: list[str], runs_as: str, sudo: str | None) -> list[str]:
    return update if runs_as == "self" else [sudo or "sudo", "-n", "-u", runs_as, *update]


# --- the request ----------------------------------------------------------------------

def _log(entry: dict) -> None:
    """One line to syslog (authpriv) and one to ~/.local/share/nomad/nodes.log."""
    from nomad.submit.gate import clean
    line = json.dumps(entry, ensure_ascii=True)
    try:
        import syslog
        syslog.openlog(TAG, 0, getattr(syslog, "LOG_AUTHPRIV", syslog.LOG_USER))
        syslog.syslog(syslog.LOG_INFO, clean(
            f"{entry.get('what')} {entry.get('node')} by {entry.get('by')}: "
            f"{'done' if entry.get('ok') else 'not done'}; {entry.get('said', '')}")[:900])
    except Exception:                              # noqa: BLE001 -- logging never stops it
        pass
    try:
        d = Path.home() / ".local" / "share" / "nomad"
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(d / "nodes.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def change(node: str, what: str, *, reason: str | None = None, by: str | None = None, apply: bool = False,
           config: dict | None = None, config_path: str | None = None, cluster: str | None = None,
           run=_run, finder=find, checker=None) -> dict:
    """Drain ``node`` or resume it (``what``): the dry run, or with ``apply``
    the change and what Slurm says after. NodeError for a request refused
    before anything ran (logged too, when it was to apply). ``ok`` in the
    answer: the dry run found nothing in the way (or, applied, Slurm shows
    the change)."""
    try:
        return _change(node, what, reason=reason, by=by, apply=apply, config=config, config_path=config_path,
                       cluster=cluster, run=run, finder=finder, checker=checker or _install_checked)
    except NodeError as exc:
        if apply:
            _log({"at": datetime.now().isoformat(timespec="seconds"), "what": what, "node": str(node)[:80],
                  "by": by, "reason": reason, "account": _account(), "runs_as": None, "ok": False,
                  "said": f"refused: {exc}"})
        raise


def _change(node, what, *, reason, by, apply, config, config_path, cluster, run, finder, checker) -> dict:
    if what not in (DRAIN, RESUME):
        raise NodeError(f"{what!r}: drain or resume")
    if not isinstance(node, str) or not _NAME.fullmatch(node):
        raise NodeError("the node is one name: letters, digits, '.', '_' and '-', not starting with '.', '_' "
                        "or '-'")
    if node.lower() == "all":
        raise NodeError("one node, by its name: not ALL")
    if reason is not None and reason.strip() == "":
        reason = None
    if reason is not None:
        why = text_problem(reason, "the reason")
        if why:
            raise NodeError(why)
        reason = reason.strip()
    elif what == DRAIN:
        raise NodeError("draining a node needs a reason (Slurm keeps it, and shows it)")
    account = _account()
    if by is not None and not _NAME.fullmatch(by):
        raise NodeError("who asked (--by) is a NetID: letters, digits, '.', '_' and '-'")
    by = by or account
    said = f"{reason} ({by} via nomad)" if what == DRAIN else None

    out = {"what": what, "node": node, "cluster": cluster, "host": socket.gethostname().split(".")[0],
           "by": by, "reason": reason, "slurm_reason": said, "account": account, "runs_as": None,
           "apply": bool(apply), "applied": False, "ok": False, "problems": [], "notes": [],
           "before": None, "jobs": None, "jobs_running": None, "command": None, "after": None,
           "said": None, "at": datetime.now().isoformat(timespec="seconds")}
    problems, notes = out["problems"], out["notes"]

    runs_as, bad = slurm_as(config)
    where = config_path or "this account's nomad.toml"
    scontrol = finder("scontrol")
    if not scontrol:
        raise NodeError("no scontrol here (PATH, /usr/bin, /usr/local/bin, /opt/slurm/bin): is this a "
                        "cluster's head node?")
    code, text, err = run([scontrol, "show", "node", "-o", node])
    before = parse_node(text) if code == 0 else None
    if before is None or before.get("node") != node:
        said = err.strip().splitlines()[-1][:200] if err.strip() else ""
        dropped = [k for k in _dropped() if k in ("SLURM_CONF", "SLURM_CLUSTERS")]
        raise NodeError((f"Slurm could not be asked about {node} ({said})" if code == 124 else
                         f"Slurm has no node called {node} here" + (f" ({said})" if said else ""))
                        + (f"; {', '.join(dropped)} set here is not used (sudo would not pass it on)"
                           if dropped else ""))
    out["before"] = before

    squeue = finder("squeue")
    if squeue:
        code, text, err = run([squeue, "-h", "-w", node, "-t", "RUNNING,COMPLETING,SUSPENDED",
                               "-o", "%i|%u|%P|%T|%M|%L|%C|%D"])
        if code == 0:
            jobs = parse_jobs(text)
            out["jobs_running"], out["jobs"] = len(jobs), jobs[:JOBS_SHOWN]
        else:
            notes.append("squeue could not list its jobs" + (f": {err.strip()[:200]}" if err.strip() else ""))
    else:
        notes.append("no squeue here: its jobs are not listed")

    if what == RESUME and not resumable(before):
        problems.append(f"{node} is {described(before)}: not drained, down or failing, so there is nothing "
                        "to resume")
    if what == DRAIN and drained(before):
        notes.append(f"{node} is already {described(before)}" + (f", for {before['reason']!r}"
                     if before.get("reason") else "") + ": draining it again replaces the reason")

    update = update_argv(scontrol, node, what, said)
    if runs_as and not bad:
        why = checker()
        if why:
            problems.append(f"someone else could change the nomad that would run scontrol as "
                            f"{runs_as if runs_as != 'self' else account}: {why}")
    if bad:
        problems.append(bad)
    elif runs_as is None:
        problems.append(f"changing nodes is off here: [actions] slurm_as is not set in {where} "
                        "(see docs/actions.md)")
    else:
        out["runs_as"] = runs_as
        sudo = shutil.which("sudo", path=SUDO_SEARCH) if runs_as != "self" else None
        if runs_as != "self" and not sudo:
            problems.append("no sudo here (/usr/bin, /bin, /usr/sbin, /sbin), and [actions] slurm_as is "
                            f"{runs_as!r}")
        else:
            out["command"] = full_argv(update, runs_as, sudo)
            if runs_as != "self":
                code, text, err = run([sudo, "-n", "-l", "-u", runs_as, *update], SUDO_TIMEOUT)
                if code != 0:
                    problems.append(f"sudo would not let {account} run this scontrol command as {runs_as} "
                                    f"without a password; a sudoers line would (visudo -f "
                                    f"/etc/sudoers.d/nomad-node): {sudo_line(account, runs_as, scontrol)}")
            else:
                notes.append(f"scontrol runs as {account}, which must be a Slurm administrator "
                             "(sacctmgr: AdminLevel=Operator or more)")
    if out["command"] is None:
        out["command"] = full_argv(update, runs_as if runs_as else "slurm", None)

    if not apply:
        out["ok"] = not problems
        return out

    if problems:
        out["said"] = "; ".join(problems)
        _log({k: out[k] for k in ("at", "what", "node", "by", "reason", "account", "runs_as", "ok", "said")})
        return out
    code, text, err = run(out["command"], UPDATE_TIMEOUT)
    out["applied"] = True
    out["exit_code"] = code
    msg = " ".join((text + " " + err).split())[:500]
    c2, t2, _ = run([scontrol, "show", "node", "-o", node])
    after = parse_node(t2) if c2 == 0 else None
    after = after if after and after.get("node") == node else None
    out["after"] = after
    now = f"{node} is {described(after)}" if after else f"{node} could not be read again"
    if taken(after, what, said):
        out["ok"] = True
        out["said"] = f"{node} is now {described(after)}"
        if what == RESUME and "NOT_RESPONDING" in (after.get("flags") or []):
            out["said"] += ": Slurm sets it down again if its slurmd doesn't answer"
        if code != 0:
            out["said"] += f" (though scontrol {'took too long' if code == 124 else f'ended with {code}'})"
    elif code == 124:
        out["said"] = (f"not known yet: scontrol did not answer within {UPDATE_TIMEOUT} seconds (it may still "
                       f"do it), and {now}; look again in a minute")
    elif code != 0 and "password is required" in msg and runs_as != "self":
        out["said"] = (f"sudo wanted a password for it, so nothing ran (sudo -l can't tell beforehand); a "
                       f"NOPASSWD line would do: {sudo_line(account, runs_as, scontrol)}; {now}")
    elif code != 0:
        out["said"] = f"scontrol did not do it (exit {code})" + (f": {msg}" if msg else "") + f"; {now}"
    elif after is None:
        out["said"] = f"scontrol said nothing wrong, but {now}"
    elif what == DRAIN and drained(after):
        out["said"] = f"scontrol said nothing wrong, and {now}, but its reason is {after.get('reason')!r}"
    else:
        out["said"] = f"scontrol said nothing wrong, but {now}"
    _log({**{k: out[k] for k in ("at", "what", "node", "by", "reason", "account", "runs_as", "ok", "said")},
          "before": before.get("state"), "after": (after or {}).get("state"), "exit_code": code})
    return out


# --- in words -------------------------------------------------------------------------

def lines(r: dict) -> list[str]:
    """The answer, for a person at a terminal."""
    b = r["before"] or {}
    where = r.get("cluster") or r.get("host")
    out = [f"{r['node']} on {where}: {described(b)}"]
    if b.get("cpus_total") is not None:
        out[0] += f", {b.get('cpus_alloc') or 0} of {b['cpus_total']} cores in use"
    if b.get("reason"):
        who = " by " + b["reason_user"] if b.get("reason_user") else ""
        when = " on " + b["reason_time"] if b.get("reason_time") else ""
        out.append(f"  reason: {b['reason']} (set{who}{when})")
    jobs = r.get("jobs")
    if jobs is not None:
        n = r.get("jobs_running") or 0
        out.append(f"  {n} job{'' if n == 1 else 's'} running there" + (":" if n else ""))
        for j in jobs:
            out.append(f"    {j['job']:<12} {j['user']:<12} {j['partition']:<12} {j['cpus'] or '?':>5} cores"
                       f"{'' if (j['nodes'] or 1) == 1 else ' on ' + str(j['nodes']) + ' nodes'}, "
                       f"{j['running']} so far, {j['left']} left")
        if n > len(jobs):
            out.append(f"    ... and {n - len(jobs)} more")
    if r["what"] == DRAIN:
        running = "the jobs running there run to their end" if r.get("jobs_running") else "nothing runs there now"
        out.append(f"Draining it: no new job starts there; {running}.")
        out.append(f"  Slurm's reason: {r['slurm_reason']}")
    else:
        goes = f" (its reason, {b['reason']!r}, goes)" if b.get("reason") else ""
        out.append(f"Resuming it: it takes jobs again{goes}.")
    if r.get("command"):
        out.append("  " + ("Ran" if r.get("applied") else "Would run") + ": " + " ".join(
            w if re.fullmatch(r"[A-Za-z0-9_./=:,@%+-]+", w) else "'" + w.replace("'", "'\\''") + "'"
            for w in r["command"]))
    for n in r.get("notes") or []:
        out.append(f"  note: {n}")
    for p in r.get("problems") or []:
        out.append(f"  not possible: {p}")
    if r.get("applied"):
        out.append(("Done: " if r["ok"] else "Not done: ") + (r.get("said") or ""))
    elif not r.get("problems"):
        out.append(f"Nothing changed (dry run). Add --apply to {r['what']} it.")
    else:
        out.append("Nothing changed.")
    return out

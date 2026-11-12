# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Acting on an interactive server's RStudio and Jupyter sessions (1.43.0):
`nomad interactive sessions|stop|clear|restart`.

Off until this account's nomad.toml says so::

    [actions]
    interactive = true
    # rstudio_server = "/usr/sbin/rstudio-server"   # Workbench's (RStudio Server's) own command
    # rstudio_unit = "rstudio-server"               # the services restart restarts
    # jupyter_unit = "jupyterhub"
    # stale_hours = 24       # idle this long is stale (default: [collectors.interactive] idle_session_hours, 24)

nomad does only what sudo already lets this account do, and asks sudo first
(``sudo -n -l COMMAND``, which runs nothing):

- an RStudio session is suspended with Workbench's own command, as root:
  ``sudo -n rstudio-server suspend-session PID``. R saves the session, and it
  comes back as it was when the person next opens RStudio;
- a Jupyter server or kernel is stopped as its person, which is what they
  could do themselves: ``sudo -n -u PERSON kill -TERM PID``. The hub sees the
  server gone at its next check; notebooks keep what was last saved;
- a service is restarted: ``sudo -n systemctl restart UNIT``. Restarting
  JupyterHub stops every person's server unless its configuration has
  ``c.JupyterHub.cleanup_servers = False``: the dry run says which, and with
  how many, and the restart then needs that number (``servers``).

A session is one the interactive collector would list (its command line, by
nomad.collectors.interactive.session_type), owned by the person named, not
root's nor a system account's (uid below 1000), and the very process it was
when looked at (its start time): all checked again just before it is
signalled. Idle is the collector's measure: when the session last used CPU
(``interactive_state.busy_at``, kept from 1.43.0 on); a Jupyter server, which
polls all the time, is as idle as its person's kernels. nomad's own
installation is checked first, as for draining a node: someone else able to
change it could otherwise run these as root or as people. Each change, and
each refusal, goes to syslog (authpriv) and to
~/.local/share/nomad/interactive.log. A dry run unless ``apply``.
"""
from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import socket
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from nomad.actions.spec import _NAME

RSTUDIO = "RStudio"
SERVER = "Jupyter Server"
KERNELS = ("Jupyter (Python)", "Jupyter (R)")
SEARCH = "/usr/bin:/bin:/usr/sbin:/sbin"
RSTUDIO_PATHS = ("/usr/sbin/rstudio-server", "/usr/lib/rstudio-server/bin/rstudio-server")
JUPYTER_CONFIGS = ("/etc/jupyterhub/jupyterhub_config.py",)
MIN_UID = 1000
STOP_WAIT = {RSTUDIO: 45, SERVER: 20}       # seconds to see it gone (R saving a big workspace takes a while)
KERNEL_WAIT = 15
MOST_CLEARED = 200
TAG = "nomad-interactive"
SERVICES = {"rstudio": "rstudio_unit", "jupyter": "jupyter_unit"}
UNITS = {"rstudio_unit": "rstudio-server", "jupyter_unit": "jupyterhub"}
_UNIT = re.compile(r"^[A-Za-z0-9@._-]{1,80}$")
_CLEANUP = re.compile(r"^\s*c\.JupyterHub\.cleanup_servers\s*=\s*(\w+)", re.M)
_STARTED = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?")


class SessionError(ValueError):
    """A request refused before anything ran: what was wrong, in words."""


# --- settings -------------------------------------------------------------------------

def settings(config: dict | None) -> dict:
    a = (config or {}).get("actions")
    a = a if isinstance(a, dict) else {}
    coll = ((config or {}).get("collectors") or {}).get("interactive") or {}
    problems = []
    on = a.get("interactive", False)
    if not isinstance(on, bool):
        problems.append("[actions] interactive should be true or false")
        on = False
    out = {"on": on, "problems": problems}
    path = a.get("rstudio_server")
    if path is not None and not (isinstance(path, str) and path.startswith("/")):
        problems.append("[actions] rstudio_server should be the full path of rstudio-server")
        path = None
    out["rstudio_server"] = path
    for key, default in UNITS.items():
        v = a.get(key, default)
        if not (isinstance(v, str) and _UNIT.fullmatch(v)):
            problems.append(f"[actions] {key} should be a systemd unit's name")
            v = default
        out[key] = v
    hours = a.get("stale_hours", coll.get("idle_session_hours", 24) if isinstance(coll, dict) else 24)
    try:
        out["stale_hours"] = max(1, min(24 * 60, int(hours)))
    except (TypeError, ValueError):
        problems.append("[actions] stale_hours should be a number of hours")
        out["stale_hours"] = 24
    return out


# --- running things -------------------------------------------------------------------

def _run(argv: list, timeout: float = 30) -> tuple:
    from nomad.drain import _run as run
    return run(argv, timeout)


def _account() -> str:
    from nomad.drain import _account
    return _account()


def _which(name: str) -> str | None:
    return shutil.which(name, path=SEARCH)


def rstudio_server(s: dict) -> str | None:
    if s.get("rstudio_server"):
        return s["rstudio_server"] if os.path.exists(s["rstudio_server"]) else None
    return next((p for p in RSTUDIO_PATHS if os.path.exists(p)), None)


def may(argv: list, as_user: str | None = None, run=_run) -> tuple[bool, str]:
    """(whether sudo lets this account run ``argv`` without a password, what
    it said): ``sudo -n -l``, which runs nothing."""
    sudo = _which("sudo")
    if not sudo:
        return False, "no sudo here"
    code, out, err = run([sudo, "-n", "-l", *(["-u", as_user] if as_user else []), *argv], 10)
    said = " ".join((err or out or "").split())[:200]
    if code != 0:
        from nomad.drain import needs_tty
        if needs_tty(said):
            said = "sudo here insists on a terminal (Defaults requiretty), which the agent has none of"
        elif not said:
            said = f"sudo -l ended with {code}"
    return code == 0, said


def _log(entry: dict) -> None:
    """One line to syslog (authpriv) and one to ~/.local/share/nomad/interactive.log."""
    try:
        import syslog
        syslog.openlog(TAG, 0, getattr(syslog, "LOG_AUTHPRIV", syslog.LOG_USER))
        syslog.syslog(syslog.LOG_INFO, (f"{entry.get('what')} {entry.get('user', '')} {entry.get('pid', '')} "
                                        f"by {entry.get('by')}: {entry.get('said', '')}")[:900])
    except Exception:                              # noqa: BLE001 -- logging never stops it
        pass
    try:
        d = Path.home() / ".local" / "share" / "nomad"
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(d / "interactive.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    except OSError:
        pass


# --- the processes --------------------------------------------------------------------

def proc(pid: int) -> dict | None:
    """What /proc says of ``pid``: its owner, command line, kind of session
    (or None), and when it started; None when it is gone."""
    from nomad.collectors import interactive as ic
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
        uid = os.stat(f"/proc/{pid}").st_uid
    except OSError:
        return None
    fields = ic._stat_fields(pid) or []
    started, _ = ic._start(fields)
    try:
        user = pwd.getpwuid(uid).pw_name
    except KeyError:
        user = str(uid)
    return {"pid": pid, "uid": uid, "user": user, "kind": ic.session_type(cmd) if cmd else None,
            "started": started, "ppid": int(fields[3]) if len(fields) > 3 and fields[3].isdigit() else None,
            "cmd": cmd}


def busy_times(db) -> dict:
    """{"PID:START": (when it last used CPU, whether that is known or only
    when watching began)} from the collector's working state."""
    if not db or not Path(db).exists():
        return {}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(interactive_state)")}
            if "busy_at" not in cols:
                return {}
            known = "busy_known" if "busy_known" in cols else "1"
            return {k: (b, kn != 0) for k, b, kn in conn.execute(
                f"SELECT key, busy_at, {known} FROM interactive_state WHERE key != 'last_run' "
                "AND busy_at IS NOT NULL")}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def _hours_since(text, now: datetime) -> float | None:
    try:
        return round((now - datetime.fromisoformat(str(text))).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        return None


def _collect():
    from nomad.collectors import interactive as ic
    return ic.collect_sessions()


def sessions(db=None, now: datetime | None = None, collect=None) -> list[dict]:
    """The sessions here now, as the collector lists them, each with how long
    it has been idle (``idle_hours``; None when nomad hasn't watched it yet;
    ``idle_at_least`` when it was already running when nomad began watching
    and hasn't been busy since: idle at least that long). A Jupyter server is
    busy when its person uses it (opening, saving) or any of their kernels
    computes: it is as idle as the least idle of those, and not known while
    one of their kernels isn't."""
    now = now or datetime.now()
    found = (collect or _collect)()
    busy = busy_times(db)
    out = []
    for s in found:
        key = f"{s['pid']}:{s.get('start_time')}"
        b, known = busy.get(key, (None, True))
        out.append({"user": s["user"], "pid": s["pid"], "kind": s["session_type"],
                    "started": s.get("start_time"), "age_hours": s.get("age_hours"),
                    "mem_mb": s.get("mem_mb"), "processes": s.get("processes"),
                    "environment": s.get("environment"), "busy_at": b,
                    "idle_hours": _hours_since(b, now) if b else None, "idle_at_least": bool(b) and not known})
    kernels: dict = {}
    for s in out:
        if s["kind"] in KERNELS:
            kernels.setdefault(s["user"], []).append(s)
    for s in out:
        if s["kind"] == SERVER and s["idle_hours"] is not None:
            ks = kernels.get(s["user"], [])
            if any(k["idle_hours"] is None for k in ks):
                s["idle_hours"] = None
            elif ks:                           # the least idle; "at least" when that one is
                least = min([s, *ks], key=lambda x: (x["idle_hours"], x["idle_at_least"]))
                s["idle_hours"], s["idle_at_least"] = least["idle_hours"], least["idle_at_least"]
    out.sort(key=lambda x: (x["user"], x["kind"], x["pid"]))
    return out


# --- checks ---------------------------------------------------------------------------

def _person(user: str) -> pwd.struct_passwd:
    if not isinstance(user, str) or not _NAME.fullmatch(user):
        raise SessionError("the person is a NetID: letters, digits, '.', '_' and '-'")
    try:
        pw = pwd.getpwnam(user)
    except KeyError:
        raise SessionError(f"no account called {user} here") from None
    if pw.pw_uid < MIN_UID:
        raise SessionError(f"{user} is a system account: nomad never acts as one")
    return pw


def _session(user: str, pid, started: str | None) -> dict:
    """The session ``pid`` of ``user``, as checked now; SessionError if it isn't one."""
    pw = _person(user)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid < 2:
        raise SessionError("the session is a process number")
    if started is not None and not (isinstance(started, str) and _STARTED.fullmatch(started)):
        raise SessionError("when it started is a time, as the sessions list says it (2026-11-12T08:31:22)")
    p = proc(pid)
    if p is None:
        raise SessionError(f"process {pid} isn't running (any more)")
    if started and p["started"] != started:
        raise SessionError(f"process {pid} isn't the session that was looked at: it ended, and its number "
                           "is now another process's")
    if p["uid"] != pw.pw_uid:
        raise SessionError(f"process {pid} isn't {user}'s")
    if p["kind"] is None:
        raise SessionError(f"process {pid} isn't an RStudio or Jupyter session")
    return p


def _install() -> str | None:
    from nomad.drain import _install_checked
    return _install_checked()


def _base(what: str, by, s: dict) -> dict:
    account = _account()
    if by is not None and not _NAME.fullmatch(by):
        raise SessionError("who asked (--by) is a NetID: letters, digits, '.', '_' and '-'")
    return {"what": what, "host": socket.gethostname().split(".")[0], "by": by or account, "account": account,
            "apply": False, "applied": False, "ok": False, "problems": [], "notes": [], "command": None,
            "said": None, "at": datetime.now().isoformat(timespec="seconds"),
            "off": None if s["on"] else "acting on sessions is off here: [actions] interactive is not true in "
                                         "this account's nomad.toml (docs/actions.md)"}


def _plan_stop(p: dict, s: dict, run=_run) -> tuple[list, str | None, str | None]:
    """(the command that would stop session ``p``, as whom, why it can't)."""
    if p["kind"] == RSTUDIO:
        rs = rstudio_server(s)
        if not rs:
            return [], None, "no rstudio-server here (" + ", ".join(RSTUDIO_PATHS) + ")"
        argv = [rs, "suspend-session", str(p["pid"])]
        ok, said = may(argv, run=run)
        return argv, "root", None if ok else (f"sudo won't let {_account()} run {' '.join(argv)} without a "
                                              f"password ({said}); root adds to sudoers: {_account()} "
                                              f"ALL=(root) NOPASSWD: {rs} suspend-session *")
    kill = _which("kill")
    if not kill:
        return [], None, "no kill program here (/usr/bin, /bin)"
    argv = [kill, "-TERM", str(p["pid"])]
    ok, said = may(argv, as_user=p["user"], run=run)
    return argv, p["user"], None if ok else (f"sudo won't let {_account()} act as {p['user']} without a "
                                             f"password ({said}): nomad stops a Jupyter session as its person")


def _sudo(argv: list, as_whom: str | None) -> list:
    sudo = _which("sudo") or "sudo"
    return [sudo, "-n", *(["-u", as_whom] if as_whom and as_whom != "root" else []), *argv]


def _signal(p: dict, s: dict, run=_run) -> dict:
    """Ask the checked session ``p`` to stop (or, RStudio, to suspend), not
    waiting: its answer so far (``sent`` when the command ran)."""
    argv, whom, why = _plan_stop(p, s, run)
    row = {"user": p["user"], "pid": p["pid"], "kind": p["kind"], "started": p["started"],
           "command": _sudo(argv, whom) if argv else None, "runs_as": whom, "ok": False, "said": None,
           "sent": False}
    if why:
        row["said"] = why
        return row
    again = proc(p["pid"])
    if again is None or again["started"] != p["started"] or again["uid"] != p["uid"]:
        row["ok"], row["said"] = True, "it had already ended"
        return row
    code, out, err = run(row["command"], 30)
    msg = " ".join((out + " " + err).split())[:300]
    if code != 0:
        row["said"] = f"{'rstudio-server' if p['kind'] == RSTUDIO else 'kill'} ended with {code}" + \
            (f": {msg}" if msg else "")
        return row
    row["sent"] = True
    return row


def _settle(rows: list, step: float = 0.5) -> list:
    """Wait for the sessions asked to stop to end, together: each its own
    time (STOP_WAIT), and say how each went."""
    start = time.monotonic()
    waiting = [r for r in rows if r.pop("sent", False)]
    for r in rows:
        r.pop("sent", None)
    while waiting:
        left = []
        for r in waiting:
            wait = STOP_WAIT.get(r["kind"], KERNEL_WAIT)
            p = proc(r["pid"])
            if p is None or p["started"] != r["started"]:
                r["ok"] = True
                r["said"] = ("suspended: R saved the session; it comes back when they next open RStudio"
                             if r["kind"] == RSTUDIO else "stopped")
            elif time.monotonic() - start >= wait:
                r["ok"] = r["kind"] == RSTUDIO
                r["said"] = (f"asked to suspend; still saving after {wait:g} s (a big workspace takes a while)"
                             if r["kind"] == RSTUDIO else f"asked to stop; still running after {wait:g} s")
            else:
                left.append(r)
        waiting = left
        if waiting:
            time.sleep(step)
    return rows


def _stop_one(p: dict, s: dict, run=_run) -> dict:
    """Stop (or, RStudio, suspend) the checked session ``p``; its answer."""
    return _settle([_signal(p, s, run)])[0]


# --- the requests ---------------------------------------------------------------------

def stop(user: str, pid: int, *, started: str | None = None, by: str | None = None, apply: bool = False,
         config: dict | None = None, run=_run, installed=None) -> dict:
    """Suspend an RStudio session, or stop a Jupyter server or kernel, of ``user``."""
    s = settings(config)
    out = _base("stop", by, s)
    out.update(user=user, pid=pid, apply=bool(apply))
    try:
        p = _session(user, pid, started)
    except SessionError as exc:
        if apply:
            _log({**{k: out[k] for k in ("at", "what", "user", "pid", "by", "account")}, "ok": False,
                  "said": f"refused: {exc}"})
        raise
    out["session"] = {k: p[k] for k in ("user", "pid", "kind", "started")}
    if out["off"]:
        out["problems"].append(out["off"])
    for why in s["problems"]:
        out["problems"].append(why)
    why = (installed or _install)()
    if why:
        out["problems"].append(f"someone else could change the nomad that would run this: {why}")
    argv, whom, why = _plan_stop(p, s, run)
    out["command"], out["runs_as"] = (_sudo(argv, whom) if argv else None), whom
    if why:
        out["problems"].append(why)
    out["does"] = ("Workbench suspends it: R saves the session, which comes back as it was when they next "
                   "open RStudio" if p["kind"] == RSTUDIO else
                   "stopped as its person (TERM): the hub sees the server gone at its next check; their "
                   "notebooks keep what was last saved" if p["kind"] == SERVER else
                   "the kernel is stopped as its person (TERM): the notebook offers to start a new one")
    if not apply:
        out["ok"] = not out["problems"]
        return out
    out["apply"] = True
    if out["problems"]:
        out["said"] = "; ".join(out["problems"])
    else:
        row = _stop_one(p, s, run)
        out["applied"], out["ok"], out["said"] = True, row["ok"], row["said"]
    _log({k: out.get(k) for k in ("at", "what", "user", "pid", "by", "account", "runs_as", "ok", "said")})
    return out


def stale(found: list, hours: int, kind: str = "all") -> list:
    """The sessions idle at least ``hours`` (RStudio sessions and Jupyter
    servers; a server's kernels end with it), oldest first."""
    want = {"all": (RSTUDIO, SERVER), "rstudio": (RSTUDIO,), "jupyter": (SERVER,)}[kind]
    out = [x for x in found if x["kind"] in want and x["idle_hours"] is not None and x["idle_hours"] >= hours
           and (x["age_hours"] or 0) >= hours]
    return sorted(out, key=lambda x: -(x["idle_hours"] or 0))


def clear(*, hours: int | None = None, kind: str = "all", by: str | None = None, apply: bool = False,
          config: dict | None = None, db=None, run=_run, installed=None, collect=None) -> dict:
    """Suspend each RStudio session and stop each Jupyter server idle at least ``hours``."""
    s = settings(config)
    if kind not in ("all", "rstudio", "jupyter"):
        raise SessionError("kind: all, rstudio or jupyter")
    hours = s["stale_hours"] if hours is None else int(hours)
    if not 1 <= hours <= 24 * 60:
        raise SessionError("hours: between 1 and 1440")
    out = _base("clear", by, s)
    out.update(hours=hours, kind=kind, apply=bool(apply))
    found = sessions(db, collect=collect)
    picked = stale(found, hours, kind)
    out["sessions"], out["watched"] = len(found), sum(1 for x in found if x["idle_hours"] is not None)
    out["stale"] = picked[:MOST_CLEARED]
    if len(picked) > MOST_CLEARED:
        out["notes"].append(f"{len(picked)} are stale: the {MOST_CLEARED} idle longest are cleared now; run it again")
    if out["watched"] < len(found):
        out["notes"].append(f"{len(found) - out['watched']} sessions haven't been watched long enough to say "
                            "how long they have been idle (nomad keeps it from 1.43.0 on)")
    if out["off"]:
        out["problems"].append(out["off"])
    out["problems"] += s["problems"]
    why = (installed or _install)()
    if why:
        out["problems"].append(f"someone else could change the nomad that would run this: {why}")
    if not picked:
        out["notes"].append(f"no session has been idle {hours} hours")
    # Whether each can be stopped here: sudo asked once for RStudio, once for each person's Jupyter.
    asked: dict = {}
    for x in out["stale"]:
        who = "rstudio" if x["kind"] == RSTUDIO else x["user"]
        if who not in asked:
            p = proc(x["pid"])
            asked[who] = _plan_stop(p, s, run)[2] if p else None
        x["why_not"] = asked[who]
    shut = [x for x in out["stale"] if x["why_not"]]
    if shut:
        people = sorted({x["user"] for x in shut})
        out["notes"].append(f"{len(shut)} of them can't be stopped here, and {'is' if len(shut) == 1 else 'are'} left alone ("
                            + ", ".join(people[:8]) + (f" and {len(people) - 8} more" if len(people) > 8 else "")
                            + f"): {shut[0]['why_not']}")
    if picked and len(shut) == len(out["stale"]):
        out["problems"].append("none of them can be stopped here")
    if not apply:
        out["ok"] = not out["problems"]
        return out
    out["apply"] = True
    if out["problems"]:
        out["said"] = "; ".join(out["problems"])
        _log({k: out.get(k) for k in ("at", "what", "by", "account", "ok", "said")})
        return out
    results, left = [], 0
    for x in out["stale"]:
        if x["why_not"]:
            left += 1
            continue
        try:
            p = _session(x["user"], x["pid"], x["started"])
        except SessionError as exc:
            results.append({**{k: x[k] for k in ("user", "pid", "kind", "started")}, "ok": True,
                            "said": f"left alone: {exc}"})
            continue
        results.append(_signal(p, s, run))
    out["results"], out["applied"] = _settle(results), True
    done = sum(1 for r in results if r["ok"])
    out["ok"] = done == len(results)
    out["said"] = (f"{done} of {len(results)} stale sessions cleared" if results else "nothing was stale") + \
        (f"; {left} left alone (sudo can't act as their people here)" if left else "")
    _log({**{k: out.get(k) for k in ("at", "what", "by", "account", "ok", "said")}, "hours": hours,
          "sessions": [f"{r['user']}:{r['pid']}:{'ok' if r['ok'] else 'no'}" for r in results]})
    return out


def unit_show(unit: str, keys: list, run=_run) -> dict:
    """systemd's properties ``keys`` of ``unit`` ({} when systemctl can't say)."""
    systemctl = _which("systemctl")
    if not systemctl:
        return {}
    code, text, _ = run([systemctl, "show", *(w for k in keys for w in ("-p", k)), unit], 10)
    if code != 0:
        return {}
    return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)


def jupyter_config(unit: str, run=_run) -> str | None:
    """The hub's configuration file: from its unit's command line (-f/--config), else the usual place."""
    p = unit_show(unit, ["ExecStart", "WorkingDirectory"], run)
    m = re.search(r"(?:^|[\s=])(?:-f|--config)[= ]([^\s;]+)", p.get("ExecStart", ""))
    if m:
        path, wd = m.group(1), p.get("WorkingDirectory", "").strip()
        return path if path.startswith("/") or not wd else os.path.join(wd, path)
    return next((c for c in JUPYTER_CONFIGS if os.path.exists(c)), None)


def cleanup_servers(path: str | None) -> bool | None:
    """Whether stopping the hub stops every person's server (JupyterHub's
    default, True), as its configuration says; None when it can't be read."""
    if not path:
        return None
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return None
    found = _CLEANUP.findall(text)
    return True if not found else found[-1] != "False"


def restart(service: str, *, servers: int | None = None, by: str | None = None, apply: bool = False,
            config: dict | None = None, run=_run, installed=None, collect=None) -> dict:
    """Restart RStudio (Workbench) or JupyterHub: ``service`` rstudio or jupyter."""
    s = settings(config)
    if service not in SERVICES:
        raise SessionError("the service: rstudio or jupyter")
    unit = s[SERVICES[service]]
    out = _base("restart", by, s)
    out.update(service=service, unit=unit, apply=bool(apply))
    systemctl = _which("systemctl")
    if not systemctl:
        raise SessionError("no systemctl here")
    p = unit_show(unit, ["LoadState", "ActiveState", "ActiveEnterTimestamp"], run)
    out["state"], out["since"] = p.get("ActiveState") or None, p.get("ActiveEnterTimestamp") or None
    if p.get("LoadState") != "loaded":
        out["problems"].append(f"systemd has no unit called {unit} here" if p.get("LoadState") == "not-found"
                               else f"systemd couldn't say what {unit} is ({p.get('LoadState') or 'no answer'})")
    found = (collect or _collect)()
    if service == "rstudio":
        n = sum(1 for x in found if x["session_type"] == RSTUDIO)
        out["sessions"] = n
        out["does"] = (f"Workbench restarts; its {n} open session{'s' if n != 1 else ''} may be interrupted "
                       "(people reconnect, their sessions saved or not as Workbench keeps them)")
    else:
        srv = [x for x in found if x["session_type"] == SERVER]
        ker = [x for x in found if x["session_type"] in KERNELS]
        out["servers"], out["kernels"] = len(srv), len(ker)
        out["config"] = jupyter_config(unit, run)
        stops = cleanup_servers(out["config"])
        out["stops_servers"] = stops
        if stops is False:
            out["does"] = "the hub restarts; people's servers keep running (cleanup_servers = False)"
        else:
            out["does"] = (f"the hub restarts and stops every person's server: {len(srv)} servers and their "
                           f"{len(ker)} kernels, now" + ("" if stops else
                                                          " (its configuration couldn't be read to say otherwise)"))
            out["asks"] = len(srv)
            if servers is not None and servers != len(srv):
                out["problems"].append(f"{len(srv)} people's servers would stop now, not {servers}: the count "
                                       "moved on (look again)")
            elif apply and servers is None and srv:
                out["problems"].append(f"{len(srv)} people's servers would stop: say so (servers = {len(srv)}) "
                                       "to restart it")
            elif not apply and servers is None:
                out["notes"].append(f"restarting it then asks for that number: --servers {len(srv)}")
    argv = [systemctl, "restart", unit]
    ok, said = may(argv, run=run)
    out["command"], out["runs_as"] = _sudo(argv, "root"), "root"
    if not ok:
        out["problems"].append(f"sudo won't let {_account()} run {' '.join(argv)} without a password ({said}); "
                               f"root adds to sudoers: {_account()} ALL=(root) NOPASSWD: {' '.join(argv)}")
    if out["off"]:
        out["problems"].append(out["off"])
    out["problems"] += s["problems"]
    why = (installed or _install)()
    if why:
        out["problems"].append(f"someone else could change the nomad that would run this: {why}")
    if not apply:
        out["ok"] = not out["problems"]
        return out
    out["apply"] = True
    if out["problems"]:
        out["said"] = "; ".join(out["problems"])
        _log({k: out.get(k) for k in ("at", "what", "service", "by", "account", "ok", "said")})
        return out
    code, text, err = run(out["command"], 120)
    out["applied"] = True
    state = None
    for _ in range(30):
        c, t, _ = run([systemctl, "is-active", unit], 10)
        state = t.strip()
        if state == "active":
            break
        time.sleep(1)
    out["after"] = state
    if code == 0 and state == "active":
        out["ok"], out["said"] = True, f"{unit} restarted and is running"
    else:
        msg = " ".join((text + " " + err).split())[:300]
        out["said"] = f"systemctl ended with {code}" + (f": {msg}" if msg else "") + f"; {unit} is {state or 'unknown'}"
    _log({k: out.get(k) for k in ("at", "what", "service", "by", "account", "ok", "said")})
    return out


def look(*, config: dict | None = None, db=None, run=_run, collect=None) -> dict:
    """The sessions here now, the two services, and what nomad may do about
    them here (each with why not): what the Console's Interactive servers
    page shows. Changes nothing."""
    s = settings(config)
    out = _base("sessions", None, s)
    found = sessions(db, collect=collect)
    out["sessions"] = found
    out["stale_hours"] = s["stale_hours"]
    out["stale"] = len(stale(found, s["stale_hours"]))
    out["watched"] = sum(1 for x in found if x["idle_hours"] is not None)
    services = {}
    for name, key in SERVICES.items():
        unit = s[key]
        p = unit_show(unit, ["LoadState", "ActiveState", "ActiveEnterTimestamp"], run)
        row = {"unit": unit, "found": p.get("LoadState") == "loaded", "state": p.get("ActiveState") or None,
               "since": p.get("ActiveEnterTimestamp") or None}
        systemctl = _which("systemctl")
        ok, said = may([systemctl, "restart", unit], run=run) if systemctl and row["found"] else (False, "")
        row["can_restart"], row["why_not"] = ok, None if ok else (said or f"no unit called {unit} here")
        if name == "jupyter" and row["found"]:
            row["stops_servers"] = cleanup_servers(jupyter_config(unit, run))
        services[name] = row
    out["services"] = services
    rs = rstudio_server(s)
    ok, said = may([rs, "suspend-session", "1"], run=run) if rs else (False, "no rstudio-server here")
    out["can"] = {"suspend_rstudio": ok, "why_not_rstudio": None if ok else said,
                  "stop_jupyter": "as each person, when sudo lets this account act as them: checked for each"}
    if out["off"]:
        out["problems"].append(out["off"])
    out["problems"] += s["problems"]
    out["ok"] = True
    return out


def lines(res: dict) -> list:
    """An answer, in words, for the command line."""
    out = []
    if res.get("what") == "sessions":
        for x in res.get("sessions", []):
            idle = (f"idle {x['idle_hours']} h" + (" or more" if x.get("idle_at_least") else "")
                    if x.get("idle_hours") is not None else "idle: not known yet")
            out.append(f"  {x['user']:<12} {x['kind']:<18} {x['pid']:>8}  up {x.get('age_hours') or 0} h, {idle}, "
                       f"{round(x.get('mem_mb') or 0)} MB")
        out = out or ["  no sessions"]
        out.append(f"{res.get('stale', 0)} idle {res.get('stale_hours')} hours or more")
        for name, v in (res.get("services") or {}).items():
            out.append(f"{name}: {v['unit']} {v['state'] or 'not found'}"
                       + (f" since {v['since']}" if v.get("since") else "")
                       + ("" if v["can_restart"] else f"; can't restart it: {v['why_not']}"))
        if res.get("can") and not res["can"]["suspend_rstudio"]:
            out.append(f"can't suspend RStudio sessions: {res['can']['why_not_rstudio']}")
        out += [f"problem: {p}" for p in res.get("problems", [])]
        return out
    if res.get("does"):
        out.append(("Done: " if res.get("applied") else "Would: ") + res["does"])
    for x in res.get("stale", []) or []:
        out.append(f"  {x['user']:<12} {x['kind']:<18} {x['pid']:>8}  idle {x['idle_hours']} h"
                   + (" or more" if x.get("idle_at_least") else ""))
    for r in res.get("results", []) or []:
        out.append(f"  {r['user']:<12} {r['pid']:>8}  {r['said']}")
    if res.get("command"):
        out.append("command: " + " ".join(res["command"]))
    out += [f"note: {n}" for n in res.get("notes", [])]
    out += [f"problem: {p}" for p in res.get("problems", [])]
    if res.get("said"):
        out.append(res["said"])
    elif not res.get("apply"):
        out.append("Nothing in the way: add --apply to do it." if res.get("ok") else "It can't be done as things are.")
    return out

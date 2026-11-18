# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Licenses (1.56.0): when the licenses of the software a computing center
runs end, read where the software itself keeps the date, and an email to the
center before each one does. nomad never changes a license.

Each machine reads its own (the ``licenses`` collector, a few times a day)
and keeps one row a license in ``license_status``. The hub gets the rows with
everything else (``nomad sync``), lists them (``nomad licenses``, the
Console's Licenses page) and emails the center before each ends (``nomad
licenses remind --send``, daily from systemd: ``nomad licenses timer``).

What is read (``kind``):

- ``posit``: Posit's products (Workbench, Connect, Package Manager):
  ``license-manager status``, as root through ``sudo -n`` (a sudo line for
  that one command). Only its Status, Expiration and Days-Left lines are
  read: nothing else it prints is kept or shown.
- ``flexlm``: FlexLM license files (Ansys and Lumerical, MATLAB's network
  license, ...): when each feature ends. The license ends when the first
  feature still in force does. ``file`` may be a pattern (``*.lic``), so a
  file that comes with a renewal, under a new name, is read too.
- ``gurobi``: a gurobi.lic (a token server's, or a machine's): EXPIRATION.
- ``cert``: the web certificate a server presents (``host``, ``port``).
- ``command``: a command that prints the date (Mathematica's
  $LicenseExpirationDate, a vendor's own tool), run without a shell; only
  the date is kept, never what else it printed.
- ``date``: a date typed in (a contract, a site license that came by email),
  or ``never`` (a perpetual license, listed so it is known).

Settings, on the machine that can read each one (its nomad.toml)::

    [collectors.licenses]
    enabled = true
    # min_hours = 6                       # read at most this often

    [[collectors.licenses.items]]
    name = "Posit Workbench"
    kind = "posit"
    program = "/usr/lib/rstudio-server/bin/rstudio-server"
    about = "RStudio in the browser; renewed through Posit"

    [[collectors.licenses.items]]
    name = "Ansys and Lumerical"
    kind = "flexlm"
    file = "/opt/ansys_inc/shared_files/licensing/license_files/*.lic"

    [[collectors.licenses.items]]
    name = "Web certificate"
    kind = "cert"
    host = "nomad.example.edu"            # port = 443

    [[collectors.licenses.items]]
    name = "Posit Connect"
    kind = "posit"
    program = "/opt/rstudio-connect/bin/license-manager"
    renew = false                         # not being renewed: listed, never reminded

Every item may also say ``about`` (what it is, who renews it) and
``remind_days`` (its own, in place of the hub's). ``nomad licenses find``
looks for what a machine has and prints the ``nomad licenses add`` lines.

And on the hub::

    [licenses]
    remind_days = [60, 30, 14, 7, 1]      # an email this many days before each ends
    to = ["hpc@example.edu"]              # default: [support] email
    # send_from = "nomad@example.edu"     # default: [mail] from
    # link = "https://nomad.example.edu/licenses"   # default: [alerts] link + /licenses
    # db = "/home/nomad/.local/share/nomad/licenses.db"   # what was sent (default: beside combined.db)

Each reminder goes once (a renewal, which moves the end, starts them over);
when several are due at once (a license first read 5 days before its end),
one email says so. Also said once: a license that ended in the last
ENDED_DAYS days, one that couldn't be read for UNREAD_DAYS, a machine whose
readings are STALE_DAYS old. Emails name no people.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shlex
import socket
import sqlite3
import ssl
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

KINDS = ("posit", "flexlm", "gurobi", "cert", "command", "date")
SAID_KIND = {"posit": "Posit's license-manager", "flexlm": "a FlexLM license file", "gurobi": "a Gurobi license file",
             "cert": "a web certificate", "command": "a command", "date": "a date typed in"}
NEEDS = {"posit": (), "flexlm": ("file",), "gurobi": ("file",), "cert": ("host",), "command": ("command",),
         "date": ("ends",)}
ITEM_KEYS = {"name", "kind", "about", "renew", "remind_days", "program", "sudo", "file", "host", "port", "command",
             "pattern", "timeout", "ends"}
SECTION_KEYS = {"enabled", "min_hours", "items", "interval", "timeout", "max_retries", "retry_delay",
                "alerts_enabled"}
HUB_KEYS = {"remind_days", "to", "send_from", "link", "db"}
REMIND_DAYS = (60, 30, 14, 7, 1)
MIN_HOURS = 6
ENDED_DAYS = 30          # an end this recent is emailed once; an older one is only listed
UNREAD_DAYS = 2          # a license that couldn't be read this long: said once
STALE_DAYS = 3           # a machine's readings this old: said once
SOON_DAYS = 30           # the Console's Home card, and "soon" in the list
POSIT_PROGRAM = "/usr/lib/rstudio-server/bin/rstudio-server"
POSIT_PROGRAMS = (POSIT_PROGRAM, "/opt/rstudio-connect/bin/license-manager", "/opt/rstudio-pm/bin/license-manager")
POSIT_NAMES = {POSIT_PROGRAM: "Posit Workbench", "/opt/rstudio-connect/bin/license-manager": "Posit Connect",
               "/opt/rstudio-pm/bin/license-manager": "Posit Package Manager"}
MAX_FILE = 4 * 1024 * 1024
TABLE = "license_status"
_NAME = re.compile(r"[^\x00-\x1f\x7f]{1,80}")
_PATH = re.compile(r"/[A-Za-z0-9._/+@-]{1,1023}")
_PATTERN = re.compile(r"/[^\x00-\x1f\x7f]{1,1023}")
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*|\[?[0-9A-Fa-f:.]{2,45}\]?")
_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+|[^<>\n]*<[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+>")
_STATUS = re.compile(r"[A-Za-z][A-Za-z -]{0,39}")
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
DEFAULT_TEAM = "Research Computing"


class LicenseError(ValueError):
    """What was asked can't be done; said as it is."""


# -- the settings --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Item:
    name: str
    kind: str
    about: str = ""
    renew: bool = True
    remind_days: tuple = ()
    program: str = POSIT_PROGRAM
    sudo: bool = True
    file: str = ""
    host: str = ""
    port: int = 443
    command: tuple = ()
    pattern: str = ""
    timeout: int = 60
    ends: str = ""                  # date: "YYYY-MM-DD" or "never"

    def body(self) -> dict:
        """The entry as it is written in nomad.toml (only what isn't a default)."""
        out: dict = {"name": self.name, "kind": self.kind}
        if self.kind == "posit":
            out["program"] = self.program
            if not self.sudo:
                out["sudo"] = False
        if self.kind in ("flexlm", "gurobi"):
            out["file"] = self.file
        if self.kind == "cert":
            out["host"] = self.host
            if self.port != 443:
                out["port"] = self.port
        if self.kind == "command":
            out["command"] = list(self.command)
            if self.pattern:
                out["pattern"] = self.pattern
        if self.kind in ("posit", "command") and self.timeout != 60:
            out["timeout"] = self.timeout
        if self.kind == "date":
            out["ends"] = self.ends
        if self.about:
            out["about"] = self.about
        if not self.renew:
            out["renew"] = False
        if self.remind_days:
            out["remind_days"] = list(self.remind_days)
        return out

    def source(self) -> str:
        """Where it is read, in a few words (shown to staff)."""
        if self.kind == "posit":
            return " ".join(posit_args(self.program))
        if self.kind in ("flexlm", "gurobi"):
            return self.file
        if self.kind == "cert":
            return f"{self.host}:{self.port}"
        if self.kind == "command":
            return " ".join(shlex.quote(c) for c in self.command)
        return "typed in"


def _days_list(value, where: str, problems: list) -> tuple:
    if isinstance(value, bool) or not isinstance(value, list) or not value or not all(
            isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 400 for d in value):
        problems.append(f"{where} should be a list of whole numbers of days, such as [60, 30, 14, 7, 1]")
        return ()
    return tuple(sorted(set(value), reverse=True))


def _date_text(value) -> str | None:
    """A typed-in end: a TOML date, "YYYY-MM-DD", or "never"."""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("never", "perpetual", "permanent"):
            return "never"
        try:
            return date.fromisoformat(v).isoformat()
        except ValueError:
            return None
    return None


def item_from(raw, where: str, problems: list) -> Item | None:
    """One [[collectors.licenses.items]] entry, or None (and why, in ``problems``)."""
    if not isinstance(raw, dict):
        problems.append(f"{where} should be a table (name = ..., kind = ...)")
        return None
    name = raw.get("name")
    if not (isinstance(name, str) and _NAME.fullmatch(name.strip())):
        problems.append(f"{where}: name should be a name of at most 80 characters")
        return None
    name = " ".join(name.split())
    at = f"{where} ({name})"
    kind = raw.get("kind")
    if kind not in KINDS:
        problems.append(f"{at}: kind should be one of {', '.join(KINDS)}")
        return None
    unknown = sorted(set(raw) - ITEM_KEYS)
    if unknown:
        problems.append(f"{at}: nomad doesn't read {', '.join(unknown)}")
    missing = [k for k in NEEDS[kind] if k not in raw]
    if missing:
        problems.append(f"{at}: a {kind} license needs {', '.join(missing)}")
        return None
    got: dict = {"name": name, "kind": kind}
    about = raw.get("about", "")
    if not (isinstance(about, str) and len(about) <= 300 and not re.search(r"[\x00-\x09\x0b-\x1f\x7f]", about)):
        problems.append(f"{at}: about should be a few words (at most 300 characters)")
        about = ""
    got["about"] = " ".join(about.split())
    renew = raw.get("renew", True)
    if not isinstance(renew, bool):
        problems.append(f"{at}: renew should be true or false")
        renew = True
    got["renew"] = renew
    if "remind_days" in raw:
        got["remind_days"] = _days_list(raw["remind_days"], f"{at}: remind_days", problems)
    if kind == "posit":
        program = raw.get("program", POSIT_PROGRAM)
        if not (isinstance(program, str) and _PATH.fullmatch(program) and ".." not in program.split("/")):
            problems.append(f"{at}: program should be the full path of rstudio-server or license-manager")
            return None
        got["program"] = program
        sudo = raw.get("sudo", True)
        if not isinstance(sudo, bool):
            problems.append(f"{at}: sudo should be true or false")
            sudo = True
        got["sudo"] = sudo
    if kind in ("flexlm", "gurobi"):
        f = raw.get("file")
        if not (isinstance(f, str) and _PATTERN.fullmatch(f)):
            problems.append(f"{at}: file should be a full path (it may end in a pattern, such as *.lic)")
            return None
        got["file"] = f
    if kind == "cert":
        host, port = raw.get("host"), raw.get("port", 443)
        if not (isinstance(host, str) and _HOST.fullmatch(host)):
            problems.append(f"{at}: host should be a host name")
            return None
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            problems.append(f"{at}: port should be a port number")
            return None
        got["host"], got["port"] = host.strip("[]"), port
    if kind == "command":
        cmd = raw.get("command")
        if isinstance(cmd, str):
            try:
                cmd = shlex.split(cmd)
            except ValueError:
                cmd = None
        if not (isinstance(cmd, list) and cmd and all(isinstance(c, str) and "\x00" not in c for c in cmd)
                and _PATH.fullmatch(cmd[0])):
            problems.append(f"{at}: command should be a list, its first word the program's full path")
            return None
        got["command"] = tuple(cmd)
        pattern = raw.get("pattern", "")
        if pattern:
            try:
                re.compile(pattern)
                if not isinstance(pattern, str) or len(pattern) > 200:
                    raise re.error("too long")
            except (re.error, TypeError):
                problems.append(f"{at}: pattern should be a regular expression (its first group: the date)")
                pattern = ""
        got["pattern"] = pattern
    if kind in ("posit", "command"):
        t = raw.get("timeout", 60)
        if isinstance(t, bool) or not isinstance(t, int) or not 5 <= t <= 600:
            problems.append(f"{at}: timeout should be seconds, from 5 to 600")
            t = 60
        got["timeout"] = t
    if kind == "date":
        ends = _date_text(raw.get("ends"))
        if ends is None:
            problems.append(f"{at}: ends should be a date (2027-06-30) or \"never\"")
            return None
        got["ends"] = ends
    return Item(**got)


def items_of(section: dict | None) -> tuple[list[Item], list[str]]:
    """The items of a [collectors.licenses] section, and what is wrong in it."""
    section = section if isinstance(section, dict) else {}
    problems: list = []
    raw = section.get("items", [])
    if not isinstance(raw, list):
        return [], ["[[collectors.licenses.items]] should be entries ([[...]]), not one value"]
    out, seen = [], set()
    for i, r in enumerate(raw, 1):
        it = item_from(r, f"[[collectors.licenses.items]] #{i}", problems)
        if it is None:
            continue
        if it.name.lower() in seen:
            problems.append(f"[[collectors.licenses.items]]: {it.name!r} is there twice (the first is used)")
            continue
        seen.add(it.name.lower())
        out.append(it)
    return out, problems


@dataclass(frozen=True)
class Settings:
    remind_days: tuple = REMIND_DAYS
    to: tuple = ()
    send_from: str | None = None
    link: str = ""
    db: Path | None = None
    team: str = DEFAULT_TEAM
    problems: tuple = field(default_factory=tuple)


def settings(config: dict | None) -> Settings:
    """The hub's [licenses]: who is reminded, when, from whom."""
    config = config if isinstance(config, dict) else {}
    raw = config.get("licenses") if isinstance(config.get("licenses"), dict) else {}
    support = config.get("support") if isinstance(config.get("support"), dict) else {}
    mail = config.get("mail") if isinstance(config.get("mail"), dict) else {}
    alerts = config.get("alerts") if isinstance(config.get("alerts"), dict) else {}
    problems: list = []
    unknown = sorted(set(raw) - HUB_KEYS)
    if unknown:
        problems.append(f"[licenses]: nomad doesn't read {', '.join(unknown)}")
    days = _days_list(raw["remind_days"], "[licenses] remind_days", problems) if "remind_days" in raw else REMIND_DAYS
    days = days or REMIND_DAYS
    if "to" in raw:
        to = raw["to"] if isinstance(raw["to"], list) else [raw["to"]]
        if not all(isinstance(a, str) and _ADDRESS.fullmatch(a.strip()) for a in to):
            problems.append("[licenses] to should be a list of email addresses")
            to = []
    else:
        e = support.get("email")
        to = [e] if isinstance(e, str) and _ADDRESS.fullmatch(e.strip()) else []
    to = [a.strip() for a in to]
    if not to:
        problems.append("[licenses]: nobody to remind (set to = [...], or [support] email)")
    sender = raw.get("send_from", mail.get("from"))
    if sender is not None and not (isinstance(sender, str) and _ADDRESS.fullmatch(sender.strip())):
        problems.append("[licenses] send_from should be an email address")
        sender = None
    link = raw.get("link")
    if link is None:
        base = alerts.get("link") if isinstance(alerts.get("link"), str) else ""
        link = f"{base.rstrip('/')}/licenses" if base.startswith(("https://", "http://")) else ""
    if not (isinstance(link, str) and (not link or re.fullmatch(r"https?://[^\s<>\"']+", link))):
        problems.append("[licenses] link should start with https://")
        link = ""
    db = raw.get("db")
    if db is not None and not (isinstance(db, str) and db.strip()):
        problems.append("[licenses] db should be a path")
        db = None
    team = support.get("team") if isinstance(support.get("team"), str) and support.get("team").strip() else DEFAULT_TEAM
    return Settings(remind_days=days, to=tuple(to), send_from=sender.strip() if sender else None,
                    link=link.rstrip("/"), db=Path(db).expanduser() if db else None,
                    team=" ".join(team.split())[:60], problems=tuple(problems))


def config_problems(config) -> list[str]:
    """What is wrong in [collectors.licenses] and [licenses], for ``nomad config check``."""
    if not isinstance(config, dict):
        return []
    out = []
    collectors = config.get("collectors") if isinstance(config.get("collectors"), dict) else {}
    section = collectors.get("licenses")
    if isinstance(section, dict):
        unknown = sorted(set(section) - SECTION_KEYS - {"servers"})
        out += [f"[collectors.licenses]: nomad doesn't read {k}" for k in unknown]
        if section.get("servers"):
            out.append("[[collectors.licenses.servers]] is the old example's, read by nothing: "
                       "[[collectors.licenses.items]] now (docs/licenses.md)")
        out += items_of(section)[1]
    if isinstance(config.get("licenses"), dict):
        out += [p for p in settings(config).problems]
    return out


# -- dates ---------------------------------------------------------------------------------------

def _month(word: str) -> int | None:
    w = word[:3].lower()
    return MONTHS.index(w) + 1 if w in MONTHS else None


_WHEN = (
    # 2026-11-23, 2026-11-23 08:00:00, 2026-11-23T08:00
    (re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?"), "ymd"),
    # 1-jun-2027 (FlexLM)
    (re.compile(r"(?<!\d)(\d{1,2})-([A-Za-z]{3})-(\d{4})(?!\d)"), "dmy"),
    # Feb 13 23:59:59 2027 GMT (openssl)
    (re.compile(r"\b([A-Za-z]{3}) +(\d{1,2}) (\d{2}):(\d{2}):\d{2} (\d{4})\b"), "mdtY"),
    # November 23, 2026 / Nov 23 2026
    (re.compile(r"\b([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})\b"), "mdy"),
    # 23 November 2026
    (re.compile(r"\b(\d{1,2}) ([A-Za-z]{3,9})\.? (\d{4})\b"), "dMy"),
    # {2027, 6, 30, ...} (Mathematica's DateObject)
    (re.compile(r"\{(\d{4}), *(\d{1,2}), *(\d{1,2})"), "list"),
    # 2027/06/30
    (re.compile(r"(?<!\d)(\d{4})/(\d{1,2})/(\d{1,2})(?!\d)"), "ymd/"),
)


def when_of(text: str) -> str | None:
    """The first date in ``text``: "YYYY-MM-DD", or "YYYY-MM-DDTHH:MM" when a
    time is given; None when there is none."""
    best = None
    for rx, how in _WHEN:
        m = rx.search(text or "")
        if not m or (best is not None and m.start() >= best[0]):
            continue
        g = m.groups()
        try:
            hh = mm = None
            if how == "ymd":
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
                if g[3] is not None:
                    hh, mm = int(g[3]), int(g[4])
            elif how == "dmy":
                y, mo, d = int(g[2]), _month(g[1]), int(g[0])
            elif how == "mdtY":
                y, mo, d, hh, mm = int(g[4]), _month(g[0]), int(g[1]), int(g[2]), int(g[3])
            elif how == "mdy":
                y, mo, d = int(g[2]), _month(g[0]), int(g[1])
            elif how == "dMy":
                y, mo, d = int(g[2]), _month(g[1]), int(g[0])
            else:
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
            if mo is None:
                continue
            at = datetime(y, mo, d, hh or 0, mm or 0)
        except (ValueError, TypeError):
            continue
        text_at = at.strftime("%Y-%m-%dT%H:%M") if hh is not None and (hh or mm) else at.strftime("%Y-%m-%d")
        best = (m.start(), text_at)
    return best[1] if best else None


def _flexlm_end(token: str) -> tuple[date | None, bool]:
    """A FlexLM expiry: (the date, permanent)."""
    t = token.strip().lower()
    if t in ("permanent", "0", "1-jan-0", "1-jan-00", "1-jan-0000", "01-jan-0000", "01-jan-00"):
        return None, True
    m = re.fullmatch(r"(\d{1,2})-([a-z]{3})-(\d{1,4})", t)
    if not m:
        return None, False
    y = int(m.group(3))
    if y == 0:
        return None, True
    mo = _month(m.group(2))
    try:
        return (date(y, mo, int(m.group(1))) if mo else None), False
    except ValueError:
        return None, False


def day_of(ends: str | None) -> date | None:
    try:
        return date.fromisoformat(str(ends)[:10]) if ends else None
    except ValueError:
        return None


# -- reading -------------------------------------------------------------------------------------

@dataclass
class Reading:
    ends: str | None = None
    never: bool = False
    detail: str = ""
    problem: str | None = None


def posit_args(program: str) -> list[str]:
    if os.path.basename(program) == "license-manager":
        return [program, "status"]
    return [program, "license-manager", "status"]


def _run(cmd, timeout, run):
    return run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)


_SUDO_SAID = ("password is required", "a terminal is required", "must have a tty", "not allowed",
              "may not run sudo", "not in the sudoers")


def _posit_lines(said: str) -> tuple:
    """(Status, Expiration, Days-Left) as license-manager wrote them; nothing else."""
    status = expiration = left = None
    for line in said.splitlines():
        m = re.match(r"\s*(Status|Expiration|Days-Left)\s*:\s*(.*?)\s*$", line, re.I)
        if not m:
            continue
        key, val = m.group(1).lower(), m.group(2)
        if key == "status" and status is None and _STATUS.fullmatch(val):
            status = val
        elif key == "expiration" and expiration is None:
            expiration = val
        elif key == "days-left" and left is None and re.fullmatch(r"-?\d{1,6}", val):
            left = int(val)
    return status, expiration, left


def read_posit(item: Item, run=subprocess.run) -> Reading:
    if not os.path.exists(item.program):
        return Reading(problem=f"{item.program} isn't on this machine")
    args = posit_args(item.program)
    tries = ([["sudo", "-n"] + args] if item.sudo else []) + [args]
    refused = None
    for cmd in tries:
        try:
            r = _run(cmd, item.timeout, run)
        except subprocess.TimeoutExpired:
            return Reading(problem=f"license-manager gave no answer in {item.timeout} s")
        except OSError as e:
            if cmd[0] == "sudo":
                refused = f"sudo: {e.strerror or e}"
                continue
            return Reading(problem=f"{cmd[0]}: {e.strerror or e}")
        said = f"{r.stdout or ''}\n{r.stderr or ''}"
        status, expiration, left = _posit_lines(said)
        detail = "; ".join(x for x in (status, f"Posit says {left} day{'s' if left != 1 else ''} left"
                                       if left is not None else None) if x)
        if expiration:
            ends = when_of(expiration)
            if ends:
                return Reading(ends=ends, detail=detail)
            if re.fullmatch(r"(?i)never|none|perpetual|permanent|unlimited", expiration.strip()):
                return Reading(never=True, detail=detail)
        low = said.lower()
        if cmd[0] == "sudo" and any(w in low for w in _SUDO_SAID):
            # no sudo line (or one that wants a terminal): license-manager may still say it as this account
            refused = ("sudo needs a terminal here: Defaults:{0} !requiretty in sudoers".format(_who())
                       if "tty" in low or "terminal" in low else
                       f"sudo asks for a password: this account needs a sudo line for {' '.join(args)}")
            continue
        if "root privilege" in low:
            return Reading(problem=refused or ("license-manager needs root: sudo = true, with a sudo line for "
                                               f"{' '.join(args)}"))
        if status:
            return Reading(detail=detail, problem=f"Posit says {status}, with no end date")
        return Reading(problem=refused or f"license-manager said no end date (exit {r.returncode})")
    return Reading(problem=refused or "license-manager said nothing")


def matching(pattern: str) -> list[str]:
    """The files ``pattern`` names (a pattern's matches, sorted; the file itself)."""
    if glob.has_magic(pattern):
        return sorted(p for p in glob.glob(pattern) if os.path.isfile(p))
    return [pattern] if os.path.isfile(pattern) else []


def _text(path: str) -> str:
    with open(path, "rb") as f:
        return f.read(MAX_FILE).decode("utf-8", "replace")


def _who() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except (ImportError, KeyError):
        return "this account"


def read_flexlm(item: Item, today: date) -> Reading:
    files = matching(item.file)
    if not files:
        return Reading(problem=f"no file {item.file} here")
    ends: dict = {}                     # (vendor, feature) -> the latest end, None: permanent
    servers: set = set()
    use_server = False
    unreadable = []
    for f in files:
        try:
            text = _text(f)
        except OSError:
            unreadable.append(f)
            continue
        for line in re.sub(r"\\[ \t]*\r?\n", " ", text).splitlines():
            t = line.split()
            if not t:
                continue
            word = t[0].upper()
            if word in ("INCREMENT", "FEATURE") and len(t) >= 5:
                end, permanent = _flexlm_end(t[4])
                if end is None and not permanent:
                    continue
                key = (t[2], t[1])
                if permanent or ends.get(key, 0) is None:
                    ends[key] = None
                else:
                    ends[key] = max(end, ends.get(key) or end)
            elif word == "SERVER" and len(t) >= 2:
                servers.add(t[1])
            elif word == "USE_SERVER":
                use_server = True
    if not ends:
        if len(unreadable) == len(files):
            return Reading(problem=f"{_who()} can't read {unreadable[0]}")
        if servers or use_server:
            return Reading(problem="no features in it: it points at the license server"
                                   + (f" {', '.join(sorted(servers))}" if servers else "")
                                   + "; read the server's own file")
        return Reading(problem="no INCREMENT or FEATURE lines in it")
    permanent = [k for k, v in ends.items() if v is None]
    dated = [v for v in ends.values() if v is not None]
    on = [d for d in dated if d >= today]
    ended = [d for d in dated if d < today]
    vendors = sorted({k[0] for k in ends})
    words = [f"{len(ends)} feature{'s' if len(ends) != 1 else ''} of {', '.join(vendors)}"]
    if permanent:
        words.append(f"{len(permanent)} permanent")
    if ended:
        words.append(f"{len(ended)} ended")
    if len(files) > 1:
        words.append(f"from {len(files)} files")
    if unreadable:
        words.append(f"{len(unreadable)} file{'s' if len(unreadable) != 1 else ''} not readable")
    detail = ", ".join(words)
    if on:
        return Reading(ends=min(on).isoformat(), detail=detail)
    if dated:
        return Reading(ends=max(dated).isoformat(), detail=detail)
    return Reading(never=True, detail=detail)


def read_gurobi(item: Item) -> Reading:
    files = matching(item.file)
    if not files:
        return Reading(problem=f"no file {item.file} here")
    found: dict = {}
    ends = []
    unreadable = 0
    for f in files:
        try:
            text = _text(f)
        except OSError:
            unreadable += 1
            continue
        for line in text.splitlines():
            m = re.match(r"\s*([A-Z_]+)\s*=\s*(\S*)", line)
            if not m:
                continue
            key, val = m.group(1), m.group(2)
            if key in ("TYPE", "VERSION", "TOKENSERVER") and key not in found and re.fullmatch(r"[A-Za-z0-9._-]{1,64}",
                                                                                            val):
                found[key] = val
            elif key == "WLSACCESSID":
                found["WLS"] = "1"
            elif key == "EXPIRATION":
                d = when_of(val)
                if d:
                    ends.append(d[:10])
    if unreadable == len(files):
        return Reading(problem=f"{_who()} can't read {files[0]}")
    detail = ", ".join(x for x in (f"Gurobi {found['TYPE'].lower()} license" if found.get("TYPE") else "Gurobi",
                                   f"version {found['VERSION']}" if found.get("VERSION") else None,
                                   f"token server {found['TOKENSERVER']}" if found.get("TOKENSERVER") else None) if x)
    if ends:
        return Reading(ends=max(ends), detail=detail)
    if found.get("WLS"):
        return Reading(detail=detail, problem="a web license (WLS): no date in the file; Gurobi's site shows it")
    if found.get("TOKENSERVER"):
        return Reading(detail=detail, problem=f"it points at the token server {found['TOKENSERVER']}: "
                                              "read the server's own gurobi.lic")
    return Reading(detail=detail, problem="no EXPIRATION in it")


def _der(b: bytes, i: int) -> tuple[int, int, int]:
    """(tag, where its content starts, where it ends)."""
    tag, n = b[i], b[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        if not 1 <= k <= 4:
            raise ValueError("length")
        n = int.from_bytes(b[i:i + k], "big")
        i += k
    if i + n > len(b):
        raise ValueError("cut short")
    return tag, i, i + n


def not_after(der: bytes) -> datetime:
    """When a certificate (DER) stops being valid (UTC)."""
    _, i, _ = _der(der, 0)                   # Certificate
    tag, i, _ = _der(der, i)                 # tbsCertificate
    if tag != 0x30:
        raise ValueError("not a certificate")
    fields = []
    while len(fields) < 5:
        tag, s, e = _der(der, i)
        if not fields and tag == 0xA0:       # [0] version
            i = e
            continue
        fields.append((tag, s, e))
        i = e
    _, s, e = fields[3]                      # serial, signature, issuer, validity
    _, s1, e1 = _der(der, s)                 # notBefore
    tag, s2, e2 = _der(der, e1)              # notAfter
    text = der[s2:e2].decode("ascii")
    if tag == 0x17:                          # UTCTime YYMMDDHHMMSSZ
        y = int(text[:2])
        text = f"{2000 + y if y < 50 else 1900 + y}{text[2:]}"
    elif tag != 0x18:                        # GeneralizedTime YYYYMMDDHHMMSSZ
        raise ValueError("no end in it")
    return datetime.strptime(text[:14], "%Y%m%d%H%M%S")


def read_cert(item: Item, timeout: float = 15) -> Reading:
    ip = re.fullmatch(r"[0-9.]+|[0-9A-Fa-f]*:[0-9A-Fa-f:.]*", item.host) is not None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((item.host, item.port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=None if ip else item.host) as t:
                der = t.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as e:
        why = getattr(e, "strerror", None) or getattr(e, "reason", None) or str(e) or type(e).__name__
        return Reading(problem=f"{item.host}:{item.port} didn't answer with a certificate ({why})")
    if not der:
        return Reading(problem=f"{item.host}:{item.port} sent no certificate")
    try:
        end = not_after(der)
    except (ValueError, IndexError, UnicodeDecodeError):
        return Reading(problem=f"the certificate on {item.host}:{item.port} has no end nomad can read")
    trust = "trusted here"
    try:
        check = ssl.create_default_context()
        with socket.create_connection((item.host, item.port), timeout=timeout) as raw:
            with check.wrap_socket(raw, server_hostname=item.host):
                pass
    except ssl.SSLCertVerificationError as e:
        trust = f"not trusted here ({e.verify_message or 'not verified'})"
    except (OSError, ssl.SSLError):
        trust = ""
    return Reading(ends=end.strftime("%Y-%m-%dT%H:%M"),
                   detail=", ".join(x for x in (f"the certificate on {item.host}:{item.port}", trust) if x))


def read_command(item: Item, run=subprocess.run) -> Reading:
    try:
        r = _run(list(item.command), item.timeout, run)
    except subprocess.TimeoutExpired:
        return Reading(problem=f"it gave no answer in {item.timeout} s")
    except OSError as e:
        return Reading(problem=f"{item.command[0]}: {e.strerror or e}")
    out = r.stdout or ""
    text = out
    if item.pattern:
        m = re.search(item.pattern, out)
        if not m:
            return Reading(problem=f"what it printed doesn't match pattern (exit {r.returncode})")
        text = m.group(1) if m.groups() else m.group(0)
    ends = when_of(text)
    if ends:
        return Reading(ends=ends, detail=f"from {os.path.basename(item.command[0])}")
    if re.search(r"(?i)\b(infinity|never|permanent|perpetual)\b", text):
        return Reading(never=True, detail=f"from {os.path.basename(item.command[0])}")
    return Reading(problem=f"no date in what it printed (exit {r.returncode})")


def missing(item: Item) -> str | None:
    """What an entry names that isn't on this machine (a typo, or the wrong
    machine), or None."""
    if item.kind == "posit" and not os.path.exists(item.program):
        return f"{item.program} isn't on this machine"
    if item.kind == "command" and not (os.path.isfile(item.command[0]) and os.access(item.command[0], os.X_OK)):
        return f"{item.command[0]} isn't a program on this machine"
    if item.kind in ("flexlm", "gurobi") and not matching(item.file):
        return f"no file {item.file} on this machine"
    return None


def read(item: Item, today: date | None = None, run=subprocess.run) -> Reading:
    """When ``item`` ends, read where it is kept. Never raises."""
    today = today or date.today()
    try:
        if item.kind == "posit":
            return read_posit(item, run)
        if item.kind == "flexlm":
            return read_flexlm(item, today)
        if item.kind == "gurobi":
            return read_gurobi(item)
        if item.kind == "cert":
            return read_cert(item)
        if item.kind == "command":
            return read_command(item, run)
        if item.ends == "never":
            return Reading(never=True, detail="no end")
        return Reading(ends=item.ends, detail="typed in")
    except Exception as e:              # noqa: BLE001 -- one license that can't be read never stops the others
        return Reading(problem=f"reading it failed ({type(e).__name__})")


# -- the record on each machine ------------------------------------------------------------------

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    name TEXT PRIMARY KEY,
    kind TEXT,
    about TEXT,
    renew INTEGER,
    remind_days TEXT,           -- JSON list, NULL: the hub's
    ends TEXT,                  -- YYYY-MM-DD or YYYY-MM-DDTHH:MM, as last read
    never INTEGER,              -- 1: no end
    detail TEXT,
    source TEXT,
    problem TEXT,               -- why the last reading failed (NULL: it didn't)
    problem_since TEXT,         -- when readings started failing
    read_at TEXT,               -- the last reading
    ok_at TEXT,                 -- the last one that worked
    host TEXT
);
"""


def store(conn: sqlite3.Connection, items: list[Item], readings: dict, now: datetime | None = None,
          host: str | None = None) -> None:
    """Keep each reading: a failed one keeps the end read before, with why it
    failed. Licenses no longer listed are dropped."""
    now = (now or datetime.now()).replace(microsecond=0)
    at = now.isoformat(timespec="seconds")
    host = host or socket.gethostname().split(".")[0]
    conn.executescript(SCHEMA)
    names = {it.name for it in items}
    with conn:
        for (name,) in conn.execute(f"SELECT name FROM {TABLE}").fetchall():
            if name not in names:
                conn.execute(f"DELETE FROM {TABLE} WHERE name = ?", (name,))
        for it in items:
            rd = readings.get(it.name)
            if rd is None:
                continue
            days = json.dumps(list(it.remind_days)) if it.remind_days else None
            same = (it.kind, it.about or None, int(it.renew), days, it.source(), host)
            if rd.problem:
                cur = conn.execute(f"UPDATE {TABLE} SET kind = ?, about = ?, renew = ?, remind_days = ?, source = ?, "
                                   "host = ?, problem = ?, read_at = ?, detail = COALESCE(?, detail), "
                                   "problem_since = COALESCE(problem_since, ?) WHERE name = ?",
                                   same + (rd.problem, at, rd.detail or None, at, it.name))
                if cur.rowcount == 0:
                    conn.execute(f"INSERT INTO {TABLE} (name, kind, about, renew, remind_days, source, host, problem, "
                                 "read_at, detail, never, problem_since) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                                 (it.name,) + same + (rd.problem, at, rd.detail or None, at))
                continue
            conn.execute(f"INSERT INTO {TABLE} (name, kind, about, renew, remind_days, source, host, ends, never, "
                         "detail, problem, read_at, ok_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?) "
                         "ON CONFLICT(name) DO UPDATE SET kind = excluded.kind, about = excluded.about, "
                         "renew = excluded.renew, remind_days = excluded.remind_days, source = excluded.source, "
                         "host = excluded.host, ends = excluded.ends, never = excluded.never, "
                         "detail = excluded.detail, problem = NULL, problem_since = NULL, "
                         "read_at = excluded.read_at, ok_at = excluded.ok_at",
                         (it.name,) + same + (rd.ends, int(rd.never), rd.detail or None, at, at))


def last_read(conn: sqlite3.Connection, names) -> datetime | None:
    """The oldest of these licenses' last readings (None: one was never read)."""
    try:
        rows = dict(conn.execute(f"SELECT name, read_at FROM {TABLE}").fetchall())
    except sqlite3.Error:
        return None
    oldest = None
    for n in names:
        try:
            at = datetime.fromisoformat(rows[n])
        except (KeyError, TypeError, ValueError):
            return None
        oldest = at if oldest is None or at < oldest else oldest
    return oldest


# -- the list ------------------------------------------------------------------------------------

def _age_days(at: str | None, now: datetime) -> float | None:
    try:
        return (now - datetime.fromisoformat(at)).total_seconds() / 86400 if at else None
    except ValueError:
        return None


def listing(conn: sqlite3.Connection, now: datetime | None = None, site: str | None = None) -> list[dict]:
    """Every license as last read, soonest end first: on the hub, every
    site's (``site``: one site's); on a machine, its own."""
    now = now or datetime.now()
    today = now.date()
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")}
    except sqlite3.Error:
        return []
    if not cols:
        return []
    where, args = "", ()
    hub = "source_site" in cols
    if hub and site:
        where, args = " WHERE source_site = ?", (site,)
    conn_rows = conn.execute(f"SELECT * FROM {TABLE}{where}", args)
    names = [d[0] for d in conn_rows.description]
    out = []
    for raw in conn_rows.fetchall():
        r = dict(zip(names, raw))
        end = day_of(r.get("ends"))
        days = (end - today).days if end else None
        never = bool(r.get("never")) and end is None
        renew = bool(r.get("renew", 1)) if r.get("renew") is not None else True
        try:
            own = tuple(json.loads(r["remind_days"])) if r.get("remind_days") else ()
        except (TypeError, ValueError):
            own = ()
        if not renew:
            state = "not renewing"
        elif days is not None and days < 0:
            state = "ended"
        elif days is not None and days <= SOON_DAYS:
            state = "soon"
        elif days is not None:
            state = "ok"
        elif never:
            state = "no end"
        else:
            state = "unknown"
        read_age = _age_days(r.get("read_at"), now)
        out.append({
            "site": r.get("source_site") or r.get("host") or "", "host": r.get("host") or "",
            "name": r.get("name") or "", "kind": r.get("kind") or "", "about": r.get("about") or "",
            "renew": renew, "remind_days": list(own), "ends": r.get("ends"), "never": never, "days": days,
            "state": state, "detail": r.get("detail") or "", "source": r.get("source") or "",
            "problem": r.get("problem"), "problem_since": r.get("problem_since"), "read_at": r.get("read_at"),
            "ok_at": r.get("ok_at"),
            "stale": read_age is not None and read_age >= STALE_DAYS,
        })
    # what ends soonest first, then what ended (the latest first): a renewal due in days before one
    # that lapsed a year ago and is still undecided
    order = {"soon": 0, "ended": 1, "unknown": 2, "ok": 3, "no end": 4, "not renewing": 5}
    out.sort(key=lambda x: (order[x["state"]], abs(x["days"]) if x["days"] is not None else 10 ** 6,
                            x["site"], x["name"].lower()))
    return out


def counts(rows: list[dict]) -> dict:
    """For a card: how many ended, end within SOON_DAYS, can't be read."""
    return {"ended": sum(1 for r in rows if r["state"] == "ended"),
            "soon": sum(1 for r in rows if r["state"] == "soon"),
            "problems": sum(1 for r in rows if r["renew"] and (r["problem"] or r["stale"])),
            "all": len(rows)}


def ends_words(r: dict, today: date | None = None) -> str:
    """"ends Mon 23 Nov 2026, in 5 days" / "ended 23 Nov 2025, 360 days ago" / "no end"."""
    end = day_of(r.get("ends"))
    if end is None:
        return "no end" if r.get("never") else "end not known"
    days = (end - (today or date.today())).days
    shown = f"{end.strftime('%a')} {end.day} {end.strftime('%b %Y')}"
    if days < 0:
        return f"ended {shown}, {-days} day{'s' if days != -1 else ''} ago"
    if days == 0:
        return f"ends today ({shown})"
    return f"ends {shown}, in {days} day{'s' if days != 1 else ''}"


# -- reminders (the hub) -------------------------------------------------------------------------

def db_path(s: Settings, combined=None) -> Path:
    """What was sent: [licenses] db, else licenses.db beside the combined database."""
    if s.db is not None:
        return s.db
    if combined is None:
        from nomad.config import combined_db_path
        combined = combined_db_path(None)
    return Path(combined).parent / "licenses.db"


RECORD = """
CREATE TABLE IF NOT EXISTS license_told (
    site TEXT NOT NULL,
    name TEXT NOT NULL,
    what TEXT NOT NULL,          -- ends:YYYY-MM-DD, unread:..., stale:...
    step TEXT NOT NULL,          -- the days before (60, 30, ...), or ended / once
    at TEXT NOT NULL,
    PRIMARY KEY (site, name, what, step)
);
CREATE TABLE IF NOT EXISTS license_mail (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    sent_to TEXT NOT NULL,
    subject TEXT NOT NULL,
    notes INTEGER NOT NULL
);
"""


def open_record(path, write: bool = False) -> sqlite3.Connection | None:
    path = Path(path)
    if not write and not path.is_file():
        return None
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
        conn.executescript(RECORD)
    else:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    return conn


def told(rec) -> set:
    if rec is None:
        return set()
    try:
        return {tuple(r) for r in rec.execute("SELECT site, name, what, step FROM license_told")}
    except sqlite3.Error:
        return set()


def last_sent(rec) -> dict:
    """{(site, name): when the last email about it went} (for the Console)."""
    if rec is None:
        return {}
    try:
        got = rec.execute("SELECT site, name, MAX(at) FROM license_told GROUP BY site, name")
        return {(s, n): at for s, n, at in got}
    except sqlite3.Error:
        return {}


@dataclass
class Note:
    row: dict
    why: str                     # "ends", "ended", "unread", "stale"
    what: str                    # the key: ends:DATE, unread:OK_AT, stale:READ_AT
    steps: tuple                 # what this note marks as told
    days: int | None = None
    extra: tuple = ()            # more (what, step) it tells: a failed reading said with its end


def due(rows: list[dict], s: Settings, done: set, now: datetime | None = None) -> list[Note]:
    """What to tell now, each once: the reminders whose day came (one note
    for several due together), an end in the last ENDED_DAYS days, a license
    not read for UNREAD_DAYS, a machine's readings STALE_DAYS old."""
    now = now or datetime.now()
    today = now.date()
    out = []
    for r in rows:
        if not r["renew"]:
            continue
        key = (r["site"], r["name"])
        end = day_of(r["ends"])
        if end is not None:
            days = (end - today).days
            what = f"ends:{end.isoformat()}"
            if days < 0:
                if -days <= ENDED_DAYS and key + (what, "ended") not in done:
                    out.append(Note(r, "ended", what, ("ended",), days))
            else:
                steps = r["remind_days"] or s.remind_days
                came = tuple(str(d) for d in steps if days <= d)
                new = [st for st in came if key + (what, st) not in done]
                if new:
                    out.append(Note(r, "ends", what, came, days))
        if r["problem"]:
            age = _age_days(r["problem_since"], now)
            what = f"unread:{r['problem_since']}"
            if age is not None and age >= UNREAD_DAYS and key + (what, "once") not in done:
                if out and out[-1].row is r:
                    # its end is told already: the email says the reading failed with it
                    out[-1].extra += ((what, "once"),)
                else:
                    out.append(Note(r, "unread", what, ("once",)))
        elif r["stale"]:
            what = f"stale:{r['read_at']}"
            if key + (what, "once") not in done:
                out.append(Note(r, "stale", what, ("once",)))
    return out


def _shown_day(at: str | None) -> str:
    try:
        d = datetime.fromisoformat(str(at))
    except (TypeError, ValueError):
        return str(at or "")
    return f"{d.day} {d.strftime('%b %Y')}"


def letter(notes: list[Note], s: Settings, today: date | None = None) -> tuple[str, str]:
    """(subject, text) of the one email for these notes. It names no people."""
    today = today or date.today()
    endings = sorted((n for n in notes if n.why == "ends"), key=lambda n: n.days)
    ended = [n for n in notes if n.why == "ended"]
    bits = []
    if endings:
        d = endings[0].days
        when = f"in {d} day{'s' if d != 1 else ''}" if d else "today"
        r = endings[0].row
        bits.append(f"{r['name']} ({r['site']}) ends {when}" if len(endings) == 1
                    else f"{len(endings)} licenses end, the first {when}")
    if ended:
        r = ended[0].row
        bits.append(f"{r['name']} ({r['site']}) ended on {_shown_day(r['ends'])}" if len(ended) == 1 and not endings
                    else f"{len(ended)} license{'s' if len(ended) != 1 else ''} ended")
    if not bits:
        bits.append(f"{len(notes)} license{'s' if len(notes) != 1 else ''} nomad can't read")
    subject = "NOMAD: " + "; ".join(bits)
    lines = ["Hello,", ""]
    for n in notes:
        r = n.row
        head = f"{r['name']} ({r['site']})"
        if n.why in ("ends", "ended"):
            lines.append(f"{head} {ends_words(r, today)}.")
            if r["detail"] and r["kind"] != "date":
                lines.append(f"  As read: {r['detail']}.")
            if r["problem"]:
                lines.append(f"  nomad can't read it since {_shown_day(r['problem_since'])}: {r['problem']}; "
                             "this is the end it read before.")
        elif n.why == "unread":
            lines.append(f"{head}: nomad can't read it since {_shown_day(r['problem_since'])}: {r['problem']}.")
            if r["ends"]:
                lines.append(f"  As last read, it {ends_words(r, today)}.")
        else:
            lines.append(f"{head}: not read since {_shown_day(r['read_at'])} (is nomad collecting there?).")
        if r["about"]:
            lines.append(f"  {r['about']}")
        lines.append("")
    if s.link:
        lines += [f"Every license: {s.link}", ""]
    days = ", ".join(str(d) for d in s.remind_days[:-1]) + (f" and {s.remind_days[-1]}" if len(s.remind_days) > 1
                                                             else str(s.remind_days[0]))
    lines += [f"nomad sends this {days} days before each license ends, once each. One that isn't being "
              "renewed: renew = false in its entry, and it stays listed without reminders "
              "(docs/licenses.md).", "", f"-- nomad, for {s.team}"]
    return subject, "\n".join(lines) + "\n"


def remind(conn, s: Settings, rec, now: datetime | None = None, dry: bool = True, sender=None,
           config: dict | None = None) -> dict:
    """What is due now, and (``dry`` False) the email that says it, kept so
    nothing goes twice. {"notes": [...], "subject", "text", "status", "error"}."""
    now = (now or datetime.now()).replace(microsecond=0)
    rows = listing(conn, now) if conn is not None else []
    notes = due(rows, s, told(rec), now)
    out: dict = {"notes": notes, "subject": "", "text": "", "status": "nothing due", "error": None,
                 "to": list(s.to)}
    if not notes:
        return out
    out["subject"], out["text"] = letter(notes, s, now.date())
    out["status"] = "would send"
    if dry:
        return out
    if not s.to:
        out["status"], out["error"] = "can't send", "nobody to remind: [licenses] to, or [support] email"
        return out
    from email.message import EmailMessage

    from nomad import mail
    cfg = mail.settings(config or {})
    frm = s.send_from or cfg.get("from")
    if not frm:
        out["status"], out["error"] = "can't send", "no sender: [licenses] send_from, or [mail] from"
        return out
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = ", ".join(s.to)
    msg["Subject"] = out["subject"]
    msg.set_content(out["text"])
    try:
        (sender or mail.send)(msg, cfg)
    except Exception as exc:            # noqa: BLE001 -- said, and tried again next time
        out["status"], out["error"] = "failed", mail.failure_label(exc)
        return out
    at = now.isoformat()
    with rec:
        for n in notes:
            for what, st in tuple((n.what, st) for st in n.steps) + n.extra:
                rec.execute("INSERT OR IGNORE INTO license_told (site, name, what, step, at) VALUES (?, ?, ?, ?, ?)",
                            (n.row["site"], n.row["name"], what, st, at))
        rec.execute("INSERT INTO license_mail (at, sent_to, subject, notes) VALUES (?, ?, ?, ?)",
                    (at, ", ".join(s.to), out["subject"], len(notes)))
    out["status"] = "sent"
    return out


# -- looking for what a machine has --------------------------------------------------------------

FIND_ROOTS = ("/usr/local/sw", "/opt", "/usr/local", "/apps", "/software", "/share/apps", "/cm/shared/apps",
              "/usr/ansys_inc")
_SKIP_DIRS = {"node_modules", "site-packages", "dist-packages", "pkgs", "share", "include", "man", "doc", "docs",
              "lib64", "__pycache__", ".git"}


def _head(path: str, n: int = 256 * 1024) -> str:
    with open(path, "rb") as f:
        return f.read(n).decode("utf-8", "replace")


def _license_name(low: str) -> bool:
    return low.endswith(".lic") or low in ("license.dat", "licence.dat", "license.txt") or \
        (low.endswith(".dat") and "lic" in low)


def find(roots=FIND_ROOTS, depth: int = 7, ports=(443, 8787, 3939)) -> list[dict]:
    """What licenses this machine seems to have, as items to add: Posit's
    programs, FlexLM and Gurobi license files (under ``roots``, never under
    /home), and the web certificates on ``ports`` here. Nothing it reads is
    shown: only where, and of what kind."""
    found: list[dict] = []
    for p in POSIT_PROGRAMS:
        if os.path.exists(p):
            found.append({"name": POSIT_NAMES[p], "kind": "posit", "program": p})
    flex: dict = {}
    gurobi: list = []
    real_roots = [os.path.realpath(r) for r in roots if os.path.isdir(r)]
    walked, seen = set(), set()
    for root in roots:
        real = os.path.realpath(root)
        if not os.path.isdir(root) or real in walked or real == "/home" or real.startswith("/home/"):
            continue
        walked.add(real)
        base = root.rstrip("/").count("/")
        for here, dirs, files in os.walk(root, followlinks=False):
            if here.count("/") - base >= depth:
                dirs[:] = []
            # another root is walked on its own; folders of code and packages hold no licenses
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")
                       and os.path.realpath(os.path.join(here, d)) not in real_roots]
            for f in files:
                low = f.lower()
                if not _license_name(low):
                    continue
                path = os.path.join(here, f)
                rp = os.path.realpath(path)
                if rp in seen or rp.startswith("/home/"):
                    continue
                seen.add(rp)
                try:
                    head = _head(path)
                except OSError:
                    continue
                if low == "gurobi.lic" or (re.search(r"(?m)^\s*(TOKENSERVER|WLSACCESSID|TYPE)=", head)
                                           and re.search(r"(?m)^\s*(EXPIRATION|TOKENSERVER|WLSACCESSID|LICENSEID)=",
                                                         head)):
                    gurobi.append(path)
                elif re.search(r"(?m)^\s*(INCREMENT|FEATURE)\s+\S+\s+\S+", head):
                    flex.setdefault(os.path.dirname(path), set()).add(os.path.splitext(f)[1])
    for folder, exts in sorted(flex.items()):
        ext = next(iter(exts)) if len(exts) == 1 else ""
        vendor = os.path.basename(os.path.dirname(folder)) if os.path.basename(folder) == "license_files" \
            else os.path.basename(folder)
        found.append({"name": f"FlexLM ({vendor})", "kind": "flexlm",
                      "file": os.path.join(folder, f"*{ext}")})
    by_dir: dict = {}
    for g in sorted(gurobi):
        by_dir.setdefault(os.path.basename(os.path.dirname(os.path.dirname(g))) or g, g)
    for name, g in sorted(by_dir.items()):
        found.append({"name": f"Gurobi ({name})", "kind": "gurobi", "file": g})
    me = None
    for port in ports:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                pass
        except OSError:
            continue
        me = me or socket.getfqdn()
        found.append({"name": f"Web certificate ({me})" if port == 443 else f"Web certificate ({me}:{port})",
                      "kind": "cert", "host": me, "port": port})
    return found


def add_line(entry: dict, config: str | None = None) -> str:
    """The ``nomad licenses add`` line for a found entry."""
    parts = ["nomad"] + (["-c", shlex.quote(config)] if config else []) + ["licenses", "add",
                                                                          shlex.quote(entry["name"]),
                                                                          entry["kind"]]
    for k in ("program", "file", "host"):
        if entry.get(k):
            parts += [f"--{k}", shlex.quote(str(entry[k]))]
    if entry.get("port") and entry["port"] != 443:
        parts += ["--port", str(entry["port"])]
    return " ".join(parts)


__all__ = ["Item", "Reading", "Settings", "KINDS", "items_of", "item_from", "settings", "config_problems", "read",
           "store", "listing", "counts", "due", "letter", "remind", "find", "when_of", "not_after", "db_path",
           "open_record", "told", "last_sent", "ends_words", "add_line"]

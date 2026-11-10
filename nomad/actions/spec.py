# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What the Console may ask nomad to do: a fixed list of actions.

Each action is one nomad command with a fixed beginning and parameters that
are checked here before anything runs: integers within bounds, dates, names
from a narrow alphabet that can't start with a dash, choices from a list.
There is no shell anywhere and no way to name a command the list doesn't
have. Patterns match whole values (a trailing newline is refused too). A site runs only the actions marked for sites, checked again by the
site itself (it never trusts the hub's checking).

Most actions only read. Those that change something (1.31: draining a
node and resuming it; 1.37: declaring a maintenance window and ending it)
are a dry run unless their ``apply`` flag is set, as ``--apply`` works on
the command line; the Console shows the dry run, and asks, before it sets
it. Who asked (a ``caller`` parameter) is filled in by
the Console from the signed-in administrator, never by the page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

SITE, HUB = "site", "hub"

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_LAB = re.compile(r"[A-Za-z0-9][A-Za-z0-9._$-]{0,63}")      # research groups end in $
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_PATH = re.compile(r"/[A-Za-z0-9._/+@,:=-]{0,1023}")        # as `nomad storage cron` takes them
_SIZE = re.compile(r"[0-9]{1,15}(?:\.[0-9]{1,6})?(?:[KMGTPkmgtp](?:i?[Bb])?|[Bb])?")
TEXT_MAX = 200
_TRUE, _FALSE = ("true", "yes", "1"), ("false", "no", "0")


def text_problem(v: str, what: str = "the text") -> str | None:
    """Why ``v`` can't be a note (a node's reason, say): one line of
    printable characters, 1 to TEXT_MAX of them, without the quote or
    backslash some programs read as their own. None when it can."""
    if not isinstance(v, str):
        return f"{what} must be text"
    t = v.strip()
    if not t:
        return f"{what} is empty"
    if len(t) > TEXT_MAX:
        return f"{what} can be at most {TEXT_MAX} characters"
    if not t.isprintable():
        return f"{what} must be one line of printable characters"
    if '"' in t or "\\" in t:
        return f"{what} can't hold \" or \\"
    return None


class ActionError(ValueError):
    """A request the catalog refuses: what was wrong, in words."""


@dataclass(frozen=True)
class Param:
    name: str
    kind: str                         # int, choice, date, name, lab, site, path, size, text,
                                      # caller (who asked: the Console's), flag (true adds the flag)
    help: str
    flag: str | None = None           # "--days"; None: a positional argument
    required: bool = False
    default: Any = None
    min: int | None = None
    max: int | None = None
    choices: tuple[str, ...] = ()

    def schema(self) -> dict:
        out = {"name": self.name, "kind": self.kind, "help": self.help, "required": self.required}
        for k in ("default", "min", "max"):
            if getattr(self, k) is not None:
                out[k] = getattr(self, k)
        if self.choices:
            out["choices"] = list(self.choices)
        return out


@dataclass(frozen=True)
class Action:
    name: str
    title: str
    help: str
    where: frozenset                  # SITE, HUB or both
    argv: tuple[str, ...]             # after "nomad"
    params: tuple[Param, ...] = ()
    hub_db: bool = False              # on the hub, read the combined database
    timeout: int = 120                # seconds
    names: bool = False               # the output can name people or groups
    files: bool = False               # writes files into the reports directory
    notes: tuple[str, ...] = field(default_factory=tuple)
    changes: bool = False             # changes something when its flag param ("apply") is set

    def schema(self) -> dict:
        out = {"name": self.name, "title": self.title, "help": self.help, "where": sorted(self.where),
               "params": [p.schema() for p in self.params], "timeout": self.timeout,
               "names": self.names, "files": self.files, "read_only": not self.changes,
               "hub_db": self.hub_db}
        if self.changes:
            out["apply"] = next(p.name for p in self.params if p.kind == "flag")
        if self.notes:
            out["notes"] = list(self.notes)
        return out


_DAYS = Param("days", "int", "Window, in days", flag="--days", default=7, min=1, max=90)
_NODE = Param("node", "name", "The node, as Slurm names it", required=True)
_BY = Param("by", "caller", "Who asked (the Console fills it in: the administrator signed in)", flag="--by")
_APPLY = Param("apply", "flag", "Make the change (otherwise a dry run: what would change)", flag="--apply",
               default=False)
_NODE_NOTES = ("Off on a site until its nomad.toml has [actions] slurm_as (see docs/actions.md).",)

ACTIONS: tuple[Action, ...] = (
    Action("version", "nomad version", "The nomad version on that host.",
           frozenset({SITE, HUB}), ("version",), timeout=30),
    Action("config.check", "Does nomad.toml read?",
           "Whether this host's nomad.toml reads, and if not the line and what is wrong there.",
           frozenset({SITE, HUB}), ("config", "check"), timeout=30),
    Action("collectors", "Collectors",
           "Which collectors run, why, and how their runs went. On the hub, every site's.",
           frozenset({SITE, HUB}), ("collectors",), (_DAYS,), hub_db=True),
    Action("syscheck", "System check", "Slurm, database, configuration and filesystems on this host.",
           frozenset({SITE}), ("syscheck",)),
    Action("status", "Status", "An overview of what nomad has collected.",
           frozenset({SITE, HUB}), ("status",), hub_db=True),
    Action("alerts", "Unresolved alerts", "Alerts not yet resolved, optionally of one severity.",
           frozenset({SITE, HUB}), ("alerts", "--unresolved"),
           (Param("severity", "choice", "Only this severity", flag="--severity",
                  choices=("info", "warning", "critical")),),
           hub_db=True, names=True),
    # Names shown (1.13): actions are for admins, and an admin asking who is
    # loading a login node needs to know who, to write to them.
    Action("per_user", "Heavy use of shared hosts",
           "What the per_user collector flagged on login nodes, and who used each host most.",
           frozenset({SITE, HUB}), ("per-user",), (_DAYS,), hub_db=True, names=True),
    Action("insights.brief", "Insights briefing", "A short operational briefing for one site.",
           frozenset({HUB}), ("insights", "brief"),
           (Param("site", "site", "The site", flag="--site", required=True),
            Param("hours", "int", "Lookback, in hours", flag="--hours", default=24, min=1, max=720)),
           hub_db=True),
    Action("console.roles", "Console roles",
           "Who may see what in the Console; with a NetID, what that person would see. Counts only.",
           frozenset({HUB}), ("console", "roles", "--mask"),
           (Param("netid", "name", "A NetID"),), hub_db=True),
    Action("lab.show", "Labs", "What nomad.toml says about labs, or about one lab.",
           frozenset({HUB}), ("lab", "show"),
           (Param("lab", "lab", "A lab: its PI's NetID, group or name"),), names=True),
    Action("storage.use", "Storage use",
           "Who holds each storage server's space: now, the change over the days, what is old, quotas, "
           "and from the scans each top folder's owner with when it was measured.",
           frozenset({SITE, HUB}), ("storage", "use"),
           (Param("server", "name", "Only this storage server", flag="--server"),
            Param("path", "path", "Only this path, as /home", flag="--path"),
            Param("over", "size", "Only owners holding more than this: 500G, 1T, 2TB", flag="--over"),
            Param("top", "int", "Owners shown per path", flag="--top", min=1, max=1000),
            Param("days", "int", "The change over this many days", flag="--days", default=30, min=1, max=3650)),
           hub_db=True, names=True),
    Action("sync.status", "Sync status",
           "How each site's last syncs to the hub went: when its data was last brought up to date, how, "
           "how many rows, and why the last one failed.",
           frozenset({HUB}), ("sync", "status"), hub_db=True, timeout=60),
    Action("usage.report", "Usage report",
           "The administrators' period report for one site, as Markdown and JSON.",
           frozenset({HUB}), ("usage-report", "--format", "both"),
           (Param("from", "date", "First day of the period", flag="--from", required=True),
            Param("to", "date", "End of the period (not included)", flag="--to", required=True),
            Param("cluster", "site", "The site to report on", flag="--cluster", required=True),
            Param("min_cell", "int", "Show counts of fewer than N people as \"fewer than N\"",
                  flag="--min-cell", min=2, max=50)),
           hub_db=True, timeout=1800, files=True,
           notes=("Takes a few minutes on a year of data.",)),
    # Declared maintenance (1.37.0): what is declared, and declaring or ending
    # a window, on the hub (it tells the sites).
    Action("maintenance", "Maintenance windows",
           "The maintenance declared for the sites and the hub: now, coming and lately. Alerts about a "
           "site are held during its window.",
           frozenset({HUB}), ("maintenance",),
           (Param("days", "int", "Past windows of this many days", flag="--days", default=7, min=0, max=400),),
           timeout=30),
    Action("maintenance.start", "Declare maintenance",
           "A site (or the hub, or all the sites) is in maintenance until a time: alerts about it are "
           "held, the hub's and its own, and the pages say so. What still holds afterwards is raised then.",
           frozenset({HUB}), ("maintenance", "start", "--json"),
           (Param("site", "name", "A site's name, hub (the hub itself) or all (every site)", required=True),
            Param("until", "text", 'When it ends: "2026-11-12 18:00", "18:00", or how long it lasts: "+4h", "+2d"',
                  flag="--until", required=True),
            Param("start", "text", 'When it starts: "2026-11-12 14:00", "14:00", "+1h" (default: now)',
                  flag="--from"),
            Param("reason", "text", "Why: shown on the pages and with the next alert", flag="--reason",
                  required=True), _BY, _APPLY),
           timeout=240, changes=True),
    Action("maintenance.end", "End maintenance",
           "A site's maintenance is over now (or its coming window is called off): alerts about it are "
           "no longer held.",
           frozenset({HUB}), ("maintenance", "end", "--json"),
           (Param("site", "name", "A site's name, hub or all", required=True), _BY, _APPLY),
           timeout=240, changes=True),
    # The first actions that change something (1.31): a dry run unless apply.
    Action("node.drain", "Drain a node",
           "No new job starts on the node; the jobs running there run to their end. Slurm keeps the "
           "reason, with who asked.",
           frozenset({SITE}), ("node", "drain", "--json"),
           (_NODE, Param("reason", "text", "Why: Slurm keeps it as the node's reason", flag="--reason",
                         required=True), _BY, _APPLY),
           timeout=180, names=True, changes=True, notes=_NODE_NOTES),
    Action("node.resume", "Resume a node",
           "A drained (or down) node takes jobs again.",
           frozenset({SITE}), ("node", "resume", "--json"),
           (_NODE, Param("reason", "text", "Why (kept in the logs: Slurm drops a node's reason when it "
                         "resumes)", flag="--reason"), _BY, _APPLY),
           timeout=180, names=True, changes=True, notes=_NODE_NOTES),
)

_BY_NAME = {a.name: a for a in ACTIONS}


def get(name: Any) -> Action:
    if not isinstance(name, str) or name not in _BY_NAME:
        raise ActionError(f"no action called {name!r}" if isinstance(name, str) and len(name) < 80
                          else "no such action")
    return _BY_NAME[name]


def catalog() -> list[dict]:
    return [a.schema() for a in ACTIONS]


def _check_one(p: Param, v: Any, sites: list[str] | None) -> Any:
    if p.kind == "int":
        if isinstance(v, bool) or not isinstance(v, (int, str)):
            raise ActionError(f"{p.name} must be a whole number")
        try:
            n = int(v)
        except ValueError:
            raise ActionError(f"{p.name} must be a whole number") from None
        if (p.min is not None and n < p.min) or (p.max is not None and n > p.max):
            raise ActionError(f"{p.name} must be between {p.min} and {p.max}")
        return n
    if p.kind == "flag":
        if isinstance(v, bool):
            return v
        if isinstance(v, str) and v.lower() in _TRUE + _FALSE:
            return v.lower() in _TRUE
        raise ActionError(f"{p.name} must be true or false")
    if not isinstance(v, str):
        raise ActionError(f"{p.name} must be text")
    if p.kind == "text":
        why = text_problem(v, p.name)
        if why:
            raise ActionError(why)
        return v.strip()
    if p.kind == "choice":
        if v not in p.choices:
            raise ActionError(f"{p.name} must be one of {', '.join(p.choices)}")
        return v
    if p.kind == "date":
        if not _DATE.fullmatch(v):
            raise ActionError(f"{p.name} must be a date, YYYY-MM-DD")
        try:
            return date.fromisoformat(v).isoformat()
        except ValueError:
            raise ActionError(f"{p.name} is not a real date") from None
    if p.kind in ("name", "caller"):
        if not _NAME.fullmatch(v):
            raise ActionError(f"{p.name} may hold letters, digits, '.', '_' and '-' only, and not start with "
                              "'.', '_' or '-'")
        return v
    if p.kind == "lab":
        if not _LAB.fullmatch(v):
            raise ActionError(f"{p.name} may hold letters, digits, '.', '_', '-' and '$' only")
        return v
    if p.kind == "path":
        if not _PATH.fullmatch(v) or "/../" in v + "/" or "//" in v:
            raise ActionError(f"{p.name} must be an absolute path of letters, digits and . _ / + @ , : = - only")
        return v.rstrip("/") or "/"
    if p.kind == "size":
        from nomad.storage import parse_size
        if not _SIZE.fullmatch(v):
            raise ActionError(f"{p.name} must be a size such as 500G, 1T or 2TB")
        try:
            parse_size(v)
        except (ValueError, ArithmeticError):
            raise ActionError(f"{p.name} must be a size such as 500G, 1T or 2TB") from None
        return v
    if p.kind == "site":
        if not _NAME.fullmatch(v):
            raise ActionError(f"{p.name} is not a site name")
        if sites is not None and v not in sites:
            raise ActionError(f"{v!r} is not one of the hub's sites")
        return v
    raise ActionError(f"{p.name}: unknown kind {p.kind}")


def check(action: Action, params: Any, sites: list[str] | None = None) -> dict:
    """The parameters as the action will use them, or ActionError.

    Unknown parameters are refused, not ignored: a request that says more than
    the action takes is not the request the action answers."""
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ActionError("parameters must be a mapping of names to values")
    known = {p.name for p in action.params}
    extra = [k for k in params if k not in known]
    if extra:
        shown = ", ".join(str(k)[:40] for k in extra[:5])
        raise ActionError(f"{action.name} takes no parameter {shown}")
    out = {}
    for p in action.params:
        v = params.get(p.name)
        if v is None or v == "":
            if p.required:
                raise ActionError(f"{action.name} needs {p.name}")
            if p.default is not None:
                out[p.name] = p.default
            continue
        out[p.name] = _check_one(p, v, sites)
    if "from" in out and "to" in out:
        a, b = date.fromisoformat(out["from"]), date.fromisoformat(out["to"])
        if a >= b:
            raise ActionError("from must come before to")
        if b > date.today() + timedelta(days=1):
            raise ActionError("to can't be after tomorrow")
        if (b - a).days > 5 * 366:
            raise ActionError("a period can be at most five years")
    return out

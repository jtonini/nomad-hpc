# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What the Console may ask nomad to do: a fixed list of actions.

Each action is one nomad command with a fixed beginning and parameters that
are checked here before anything runs: integers within bounds, dates, names
from a narrow alphabet that can't start with a dash, choices from a list.
There is no shell anywhere and no way to name a command the list doesn't
have. Patterns match whole values (a trailing newline is refused too). A site runs only the actions marked for sites, checked again by the
site itself (it never trusts the hub's checking).

Version 1 holds read-only actions. Actions that change something will come
as a dry run first and an apply after, as `--apply` works on the command
line.
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


class ActionError(ValueError):
    """A request the catalog refuses: what was wrong, in words."""


@dataclass(frozen=True)
class Param:
    name: str
    kind: str                         # int, choice, date, name, lab, site
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

    def schema(self) -> dict:
        return {"name": self.name, "title": self.title, "help": self.help, "where": sorted(self.where),
                "params": [p.schema() for p in self.params], "timeout": self.timeout,
                "names": self.names, "files": self.files, "read_only": True}


_DAYS = Param("days", "int", "Window, in days", flag="--days", default=7, min=1, max=90)

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
    Action("per_user", "Heavy use of shared hosts",
           "What the per_user collector flagged on login nodes, with people replaced by stand-ins.",
           frozenset({SITE, HUB}), ("per-user", "--mask"), (_DAYS,), hub_db=True),
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
    if not isinstance(v, str):
        raise ActionError(f"{p.name} must be text")
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
    if p.kind == "name":
        if not _NAME.fullmatch(v):
            raise ActionError(f"{p.name} may hold letters, digits, '.', '_' and '-' only, and not start with "
                              "'.', '_' or '-'")
        return v
    if p.kind == "lab":
        if not _LAB.fullmatch(v):
            raise ActionError(f"{p.name} may hold letters, digits, '.', '_', '-' and '$' only")
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

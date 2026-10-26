# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Changing the condo settings in report.toml without editing it by hand:
[report.labs] and [report.policy] values, each condo's owners, and which
condos are confirmed. `nomad usage-report condos set` and the Console both
change the file through here.

A change is planned first (the new text, the difference, and the settings
as they would read), then saved: nothing is written unless the result reads
as condo settings, the file is copied to a backup first, and it keeps its
owner and permissions (nomad.config.edit.TomlEdit). Each changed line keeps
its comment, except an owner or a confirmation, whose old comment ("guessed",
"set it") no longer holds.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path

from nomad.condo import config as ccfg

# Settings that are lists, given as A,B on the command line.
LISTS = {"review_to", "summary_to", "formats", "owner_patterns", "cc", "email"}


class EditError(ValueError):
    """The change can't be made: what is wrong, in words."""


@dataclass
class Changes:
    labs: dict = field(default_factory=dict)         # key -> value
    policy: dict = field(default_factory=dict)
    confirm: list = field(default_factory=list)      # condo specs: NAME or CLUSTER/NAME
    unconfirm: list = field(default_factory=list)
    owners: dict = field(default_factory=dict)       # condo spec -> [NetID, ...]

    def empty(self) -> bool:
        return not (self.labs or self.policy or self.confirm or self.unconfirm or self.owners)


@dataclass
class Plan:
    path: Path
    edit: object                 # TomlEdit holding the new text
    diff: str
    settings: ccfg.Settings      # as the new file reads

    @property
    def changed(self) -> bool:
        return self.edit.text != self.edit.original


def _one_line(v, what: str) -> None:
    """Text values are one line: control characters, line and paragraph
    separators (Python ends lines there and TOML doesn't, so the file would
    read otherwise than the edit meant) and lone surrogates are refused."""
    for x in (v if isinstance(v, list) else [v]):
        if isinstance(x, str) and any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F or c in "\u2028\u2029"
                                      or 0xD800 <= ord(c) <= 0xDFFF for c in x):
            raise EditError(f"{what}: one line of text, without control characters")


def parse_value(key: str, raw: str):
    """A value from the command line: A,B for a list setting; true/false;
    a number; else text."""
    raw = str(raw).strip()
    if key in LISTS:
        return [x.strip() for x in raw.split(",") if x.strip()]
    if raw in ("true", "false"):
        return raw == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        f = float(raw)
        return int(f) if f.is_integer() else f
    except ValueError:
        return raw


def parse_pairs(items, what: str) -> dict:
    out = {}
    for item in items or ():
        key, sep, raw = str(item).partition("=")
        if not sep or not key.strip():
            raise EditError(f"{what} {item!r}: KEY=VALUE")
        out[key.strip()] = parse_value(key.strip(), raw)
    return out


def _render(v) -> str:
    from nomad.config.edit import value
    if isinstance(v, float):
        return repr(v)
    return value(v)


def _set_key(edit, table: tuple, key: str, v, keep_comment: bool = True) -> None:
    """key = v in [table]: on the line that sets it now, else as a new first
    line of the table (or a new table)."""
    from nomad.config.edit import lines_of, quoted_key
    lines = lines_of(edit.text)            # as key_lines counts them: at newlines only
    for i, t, k in edit.key_lines():
        if t == table and k == (key,):
            old = lines[i]
            comment = ""
            if keep_comment and "#" in old and old.count('"', 0, old.index("#")) % 2 == 0:
                comment = "   " + old[old.index("#"):].rstrip("\n")
            lines[i] = f"{quoted_key(key)} = {_render(v)}{comment}\n"
            edit.text = "".join(lines)
            return
    if not isinstance(v, float):
        edit.insert_key(table, key, v)
        return
    # TomlEdit writes text, booleans, whole numbers and lists: a fraction
    # goes in as a placeholder, then takes its place.
    edit.insert_key(table, key, "\0")
    edit.text = edit.text.replace(f'{quoted_key(key)} = "\\u0000"', f"{quoted_key(key)} = {_render(v)}", 1)


def plan(path, changes: Changes) -> Plan:
    """The new report.toml, without writing it. Raises EditError."""
    from nomad.config.edit import DecodeError, TomlEdit, loads
    path = Path(path).expanduser()
    if not path.exists():
        raise EditError(f"{path}: no such file (`nomad usage-report condos init --apply` makes it)")
    edit = TomlEdit(path)
    try:
        data = loads(edit.text)
    except DecodeError as e:
        raise EditError(f"{path} doesn't read as TOML ({e})") from None
    clusters = ((data.get("report") or {}).get("clusters")) or {}
    where: dict = {}
    for cname, c in clusters.items():
        for name in ((c or {}).get("condos") or {}):
            where.setdefault(name, []).append(cname)

    def condo(spec: str) -> tuple:
        spec = spec.strip()
        if "/" in spec:
            cl, name = spec.split("/", 1)
            if name in (((clusters.get(cl) or {}).get("condos")) or {}):
                return ("report", "clusters", cl, "condos", name)
            raise EditError(f"no condo {name} on {cl} in {path}")
        hits = where.get(spec, [])
        if not hits:
            raise EditError(f"no condo {spec} in {path}")
        if len(hits) > 1:
            raise EditError(f"condo {spec} is on {', '.join(hits)}: write CLUSTER/{spec}")
        return ("report", "clusters", hits[0], "condos", spec)

    for key, v in list(changes.labs.items()) + list(changes.policy.items()):
        _one_line(v, key)
    for spec in list(changes.confirm) + list(changes.unconfirm) + list(changes.owners):
        _one_line(spec, "condo")
    for spec, people in changes.owners.items():
        _one_line(list(people), f"owner of {spec}")
    for key, v in changes.labs.items():
        if key not in ccfg._LABS_KEYS:
            raise EditError(f"[report.labs] has no setting {key!r} ({', '.join(ccfg._LABS_KEYS)})")
        _set_key(edit, ("report", "labs"), key, v)
    for key, v in changes.policy.items():
        if key not in ccfg._POLICY_KEYS:
            raise EditError(f"[report.policy] has no setting {key!r} ({', '.join(ccfg._POLICY_KEYS)})")
        _set_key(edit, ("report", "policy"), key, v)
    for spec in changes.confirm:
        _set_key(edit, condo(spec), "confirmed", True, keep_comment=False)
    for spec in changes.unconfirm:
        _set_key(edit, condo(spec), "confirmed", False, keep_comment=False)
    for spec, people in changes.owners.items():
        people = [p.strip() for p in people if p and p.strip()]
        if not people:
            raise EditError(f"owner of {spec}: give a NetID")
        _set_key(edit, condo(spec), "owner", people[0] if len(people) == 1 else people, keep_comment=False)
    try:
        settings = ccfg.from_dict(loads(edit.text), path)
    except (DecodeError, ccfg.CondoConfigError) as e:
        raise EditError(f"the result wouldn't read ({e}); nothing changed") from None
    from nomad.config.edit import lines_of
    diff = "".join(difflib.unified_diff(lines_of(edit.original), lines_of(edit.text),
                                        str(path), f"{path} (new)", n=1))
    return Plan(path, edit, diff, settings)


def save(p: Plan) -> Path | None:
    """Write the plan; the backup's path. Raises EditError."""
    if not p.changed:
        return None
    try:
        return p.edit.save(lambda d: ccfg.from_dict(d) is not None)
    except (OSError, ValueError) as e:
        raise EditError(str(e)) from None

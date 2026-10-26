# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""One person's Console role in nomad.toml (``[console.roles]``).

``nomad console role NETID admin|operator|viewer|none`` uses it, and so can
the Console's Settings > People: the change is to that person's name in the
``admin``, ``operator`` and ``viewer`` lists and to nothing else. ``viewer``
names them a viewer, whatever else would make them more (a directory admin
group, an older role in the Console's own records); ``none`` takes them off
every list, so the Console's own record decides again. A list's line keeps
its place and its comment (the other names in it are written out afresh,
with the same values); a list that isn't there is added to
``[console.roles]``, and the table after ``[console]`` when there is none.
Roles written some other way (``roles = {...}`` inline, ``roles.admin =`` in
``[console]``, a list over several lines, a multi-line string) are refused,
to be changed by hand, rather than rewritten. The file is saved through
TomlEdit: refused if it changed since it was read, a backup first (the
newest BACKUPS of them kept), and only if it reads back with that one
change.
"""
from __future__ import annotations

import copy
import re

from nomad.config.access import access_from
from nomad.config.edit import TomlEdit, lines_of, loads, value

ROLE_CHOICES = ("admin", "operator", "viewer", "none")
LISTS = ("admin", "operator", "viewer")
TABLE = ("console", "roles")
BACKUPS = 20                          # role backups kept beside nomad.toml
# What the Console takes as a sign-in name (its auth._LOGIN_NAME), lower case.
_NETID = re.compile(r"[a-z0-9_][a-z0-9_.-]{0,63}")


class RoleError(ValueError):
    """The change can't be made safely; nothing was changed."""


def netid_of(text) -> str:
    n = str(text or "").strip().lower()
    if not _NETID.fullmatch(n):
        raise RoleError(f"{text!r} is not a NetID")
    return n


def role_of(data: dict, netid: str) -> str:
    """'admin', 'operator', 'viewer' (named as one) or 'none' (named in no
    list: the Console's own record decides), as the file has it."""
    return access_from(data, log=False).file_role(netid) or "none"


def _lists(data: dict) -> dict:
    roles = (data.get("console") or {}).get("roles") or {}
    return roles if isinstance(roles, dict) else {}


def _names(raw) -> list | None:
    """The names of a list as written (a single string is a list of one);
    None when it isn't a list of names."""
    if raw is None:
        return []
    names = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else None
    return names if names is not None and all(isinstance(x, str) for x in names) else None


def _on(raw, netid: str) -> bool:
    return any(x.strip().lower() == netid for x in _names(raw) or [])


def _value_end(text: str) -> int | None:
    """Where the value at the start of ``text`` ends (a one-line string or
    list); None when it goes on past the line, is a multi-line string, or
    is something else."""
    if text.startswith(('"""', "'''")):
        return None
    i, depth, quote = 0, 0, None
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                if text.startswith(quote * 3, i):
                    return None              # a multi-line string inside the list
                quote = None
                if depth == 0:
                    return i + 1
        elif ch in "\"'":
            if text.startswith(ch * 3, i):
                return None
            quote = ch
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        elif ch == "#" or ch == "\n":
            return None
        elif depth == 0 and not ch.isspace():
            return None                      # neither a string nor a list
        i += 1
    return None


def _set_list(edit: TomlEdit, key: str, names: list) -> None:
    """The [console.roles] ``key = [...]`` line, with these names; its
    comment and layout otherwise as they were."""
    from nomad.config.problems import _before_equals
    hits = [i for i, table, k in edit.key_lines() if table == TABLE and k == (key,)]
    if len(hits) != 1:
        raise RoleError(f"[console.roles] {key} isn't one line of its own in {edit.path.name}: "
                        "change it by hand")
    lines = lines_of(edit.text)
    line = lines[hits[0]]
    left = _before_equals(line)
    rest = line[len(left) + 1:]
    lead = len(rest) - len(rest.lstrip(" \t"))
    end = _value_end(rest[lead:])
    if end is None:
        raise RoleError(f"[console.roles] {key} in {edit.path.name} goes on over several lines "
                        "(or isn't a list of names): change it by hand")
    lines[hits[0]] = f"{left}={rest[:lead] or ' '}{value(names)}{rest[lead + end:]}"
    edit.text = "".join(lines)


def set_role(edit: TomlEdit, netid: str, role: str) -> list[str]:
    """Give ``netid`` this role in the edit; what changed, in words ([] when
    the file says so already). RoleError, the edit unchanged, when it can't
    be done safely."""
    n = netid_of(netid)
    if role not in ROLE_CHOICES:
        raise RoleError(f"{role!r} is not a role; roles are {', '.join(ROLE_CHOICES)}")
    try:
        data = edit.data
    except Exception as exc:
        raise RoleError(f"{edit.path} can't be read ({exc})") from exc
    console = data.get("console", {})
    roles = console.get("roles") if isinstance(console, dict) else None
    if not isinstance(console, dict) or (roles is not None and not isinstance(roles, dict)):
        raise RoleError(f"[console.roles] in {edit.path.name} isn't a table: change it by hand")
    want = {role} & set(LISTS)
    if {k for k in LISTS if _on((roles or {}).get(k), n)} == want:
        return []
    if edit.assigned(TABLE) or edit.defined_inline(TABLE):
        raise RoleError(f"the roles in {edit.path.name} are written some other way than a "
                        "[console.roles] table (roles = {...}, roles.admin = ... under [console], "
                        "or only [console.roles.*] tables): change them by hand")
    before = edit.text
    changes = []
    try:
        for key in LISTS:
            roles = _lists(edit.data)
            raw = roles.get(key)
            names = _names(raw)
            if names is None:
                raise RoleError(f"[console.roles] {key} isn't a list of names: change it by hand")
            listed = _on(raw, n)
            if key in want and not listed:
                if raw is not None:
                    _set_list(edit, key, names + [n])
                elif (edit.data.get("console") or {}).get("roles") is not None:
                    edit.insert_key(TABLE, key, [n])
                else:
                    edit.set_table(TABLE, {key: [n]}, after=("console",))
                changes.append(f"{n} is {'an' if key[0] in 'aeiou' else 'a'} {key} "
                               f"([console.roles] {key})")
            elif key not in want and listed:
                _set_list(edit, key, [x for x in names if x.strip().lower() != n])
                changes.append(f"{n} is no longer listed as {'an' if key[0] in 'aeiou' else 'a'} {key}")
        loads(edit.text)
        if role_of(edit.data, n) != role:
            raise RoleError(f"{edit.path.name} wouldn't say {n} is {role}: change it by hand")
    except RoleError:
        edit.text = before
        raise
    except Exception as exc:                 # anything the file holds that this can't read
        edit.text = before
        raise RoleError(f"[console.roles] in {edit.path.name} can't be changed here ({exc}): "
                        "change it by hand") from exc
    if role == "viewer":
        changes.append(f"{n} is a viewer: their own work, whatever their record in the Console says "
                       "(a PI's lab view comes from [console.labs])")
    elif role == "none":
        changes.append(f"{n} is on no list: the Console's own record for them decides")
    return changes


def _outside_roles(data: dict) -> dict:
    """The file but for the role lists (an empty [console] left by taking
    them out counts as none, so adding the table to a file without one
    reads back the same)."""
    d = copy.deepcopy(data)
    console = d.get("console")
    if isinstance(console, dict):
        roles = console.get("roles")
        if isinstance(roles, dict):
            for key in LISTS:
                roles.pop(key, None)
            if not roles:
                console.pop("roles")
        if not console:
            d.pop("console")
    return d


def check_for(before: dict, netid: str, role: str):
    """What save() checks: this person has the role, everyone else theirs,
    and nothing but the role lists changed."""
    n = netid_of(netid)
    old = access_from(before, log=False)
    outside = _outside_roles(before)

    def check(after: dict) -> bool:
        new = access_from(after, log=False)
        return (role_of(after, n) == role
                and new.admins - {n} == old.admins - {n}
                and new.operators - {n} == old.operators - {n}
                and new.viewers - {n} == old.viewers - {n}
                and _outside_roles(after) == outside)
    return check


__all__ = ["BACKUPS", "LISTS", "ROLE_CHOICES", "RoleError", "check_for", "netid_of", "role_of", "set_role"]

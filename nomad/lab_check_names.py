# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The PIs' names for the lab check's email (1.36.0).

The email greets each lab's PI by name ("Dear Dr. Parish,"). nomad finds
what it can on the system, read-only, and asks a person for the rest:

1. the account's full name (getent passwd, its GECOS field);
2. the directory, without a login: [console.labs.check] directory, or the
   system's ldap.conf (URI and BASE), searched with ``ldapsearch -x`` for
   givenName and sn. Never with a password: a directory that shows no
   names without one is said so, and left;
3. whatever is left, a person types ("First Last"), leaves for later, or
   says is not a lab at all (then the group goes to [console.labs]
   not_labs, as ``nomad lab hide`` does).

Names go to [console.labs.check.names] in nomad.toml, through nomad's own
careful edit (config.edit.TomlEdit): the change shown first, a backup, the
file's owner and mode kept, nothing written unless the file reads back
with only those changes, nor over someone else's edit made meanwhile.
"""
from __future__ import annotations

import base64
import copy
import os
import re
import shutil
import subprocess

from nomad import lab_check as L

CHECK = ("console", "labs", "check")
NAMES = CHECK + ("names",)
LDAP_CONF = ("/etc/openldap/ldap.conf", "/etc/ldap/ldap.conf", "/etc/ldap.conf")
TIMEOUT = 5                     # seconds, per directory search
BATCH = 40                      # NetIDs per search
NOT_A_LAB = "-"


# -- finding names ---------------------------------------------------------------------------

def _ldap_conf(paths=LDAP_CONF) -> tuple[str, str] | None:
    """(URI, BASE) from the system's ldap.conf, when it names both."""
    for p in paths:
        try:
            text = open(p, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        uri = base = None
        for line in text.splitlines():
            parts = line.split(None, 1)
            if len(parts) != 2 or parts[0].startswith("#"):
                continue
            key, val = parts[0].upper(), parts[1].strip()
            if key == "URI" and uri is None:
                uri = val.split()[0]
            elif key == "BASE" and base is None:
                base = val
        if uri and base and L._LDAP_URI.match(uri) and L._LDAP_BASE.match(base):
            return uri, base
    return None


def directory_of(s: L.Settings, paths=LDAP_CONF) -> tuple[tuple[str, str, str] | None, str]:
    """((uri, base, id attribute) or None, how it was found or why not)."""
    if s.directory is None:
        return None, "off ([console.labs.check] directory = false)"
    if s.directory == "auto":
        got = _ldap_conf(paths)
        if got is None:
            return None, ("not set ([console.labs.check] directory), and no ldap.conf here names one"
                          )
        return (got[0], got[1], "uid"), "from ldap.conf"
    return s.directory, "from [console.labs.check] directory"


def _escape(value: str) -> str:
    """An LDAP filter value (RFC 4515)."""
    return "".join(f"\\{ord(c):02x}" if c in "*()\\\0" else c for c in value)


def _ldif(text: str) -> list[dict]:
    """Entries of ldapsearch -LLL output: {attribute (lower case): [values]}."""
    entries, cur, last = [], [], None
    for line in text.splitlines():
        if line.startswith(" ") and last is not None:
            cur[-1][2] += line[1:]
            continue
        if not line.strip():
            if cur:
                entries.append(cur)
            cur, last = [], None
            continue
        if line.startswith("#"):
            continue
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        if rest.startswith(":"):
            cur.append([key.lower(), "b64", rest[1:].strip()])
        elif rest.startswith("<"):
            cur.append([key.lower(), "url", ""])
        else:
            cur.append([key.lower(), "text", rest[1:] if rest.startswith(" ") else rest])
        last = key
    if cur:
        entries.append(cur)
    out = []
    for e in entries:
        d: dict = {}
        for key, kind, val in e:
            if kind == "url":
                continue
            if kind == "b64":
                try:
                    val = base64.b64decode(val).decode("utf-8", "replace")
                except (ValueError, TypeError):
                    continue
            d.setdefault(key, []).append(val)
        out.append(d)
    return out


_WHY = {32: "shows no one without a login", 48: "does not answer without a login",
        49: "does not answer without a login", 50: "does not answer without a login",
        53: "does not answer without a login", 255: "could not be reached"}


def from_directory(netids, d, run=subprocess.run, which=shutil.which) -> tuple[dict, str | None]:
    """({netid: (first, last)}, why it found none or None): an anonymous
    search (ldapsearch -x, no bind DN, no password, the account's own
    ldaprc not read) for each NetID's givenName and sn (else cn or
    displayName)."""
    uri, base, ident = d
    netids = [n for n in dict.fromkeys(netids) if L._NETID.match(n)]
    if not netids:
        return {}, None
    exe = which("ldapsearch")
    if exe is None:
        return {}, "ldapsearch is not installed here (openldap-clients)"
    env = {**os.environ, "LDAPRC": ".nomad-reads-no-ldaprc"}
    env.pop("LDAPBINDDN", None)
    found: dict = {}
    for i in range(0, len(netids), BATCH):
        batch = netids[i:i + BATCH]
        terms = "".join(f"({ident}={_escape(n)})" for n in batch)
        flt = terms if len(batch) == 1 else f"(|{terms})"
        cmd = [exe, "-x", "-LLL", "-H", uri, "-b", base, "-o", f"nettimeout={TIMEOUT}", "-l", str(TIMEOUT),
               "-z", str(2 * len(batch)), flt, ident, "givenName", "sn", "cn", "displayName"]
        try:
            p = run(cmd, capture_output=True, text=True, timeout=3 * TIMEOUT, env=env,
                    stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            return found, f"could not be searched ({type(e).__name__})"
        if p.returncode not in (0, 4):              # 4: size limit, what came back still counts
            return found, _WHY.get(p.returncode, f"ldapsearch ended with {p.returncode}")
        for e in _ldif(p.stdout):
            ids = e.get(ident.lower()) or []
            n = next((x.strip().lower() for x in ids if x.strip().lower() in batch), None)
            if n is None or n in found:
                continue
            given = L._line((e.get("givenname") or [""])[0], L.NAME_MAX)
            sn = L._line((e.get("sn") or [""])[0], L.NAME_MAX)
            got = (given, sn) if sn and sn.lower() != n else None
            if got is None:
                full = (e.get("displayname") or e.get("cn") or [""])[0]
                got = L.split_name(full)
                if got and (got[1].lower() == n or (got[0] + got[1]).lower() == n):
                    got = None
            if got:
                found[n] = got
    return found, None


def find(netids, s: L.Settings, lookup: bool = True, directory=None, account=None,
         search=from_directory) -> tuple[dict, list]:
    """({netid: ((first, last), "account" | "directory")}, what each source gave, as lines)."""
    account = account or L._system_name
    netids = list(dict.fromkeys(netids))
    found, said = {}, []
    if not lookup or not netids:
        return found, said
    got = 0
    for n in netids:
        name = account(n)
        if name:
            found[n] = (name, "account")
            got += 1
    said.append(f"the account's full name (getent passwd): {got} of {len(netids)}")
    left = [n for n in netids if n not in found]
    d, how = directory if directory is not None else directory_of(s)
    if d is None:
        said.append(f"the directory: {how}")
    elif left:
        names, why = search(left, d)
        for n, name in names.items():
            found[n] = (name, "directory")
        said.append(f"the directory {d[0]} ({how}, no login): "
                    + (f"{why}" if why and not names else f"{len(names)} of {len(left)}"
                       + (f" ({why})" if why else "")))
    return found, said


# -- what a person types -----------------------------------------------------------------------

def parse(text: str):
    """What an answer means: ("skip", None), ("not a lab", None), ("name", value
    to write) or ("bad", why). "First Last", or "First | Last" for a last name
    of more than one word ("Maria | de la Cruz")."""
    t = L._line(text or "", 2 * L.NAME_MAX)
    if not t:
        return "skip", None
    if t == NOT_A_LAB:
        return "not a lab", None
    if "|" in t:
        first, _, last = (x.strip() for x in t.partition("|"))
        if not last or "|" in last or any(c.isdigit() for c in first + last):
            return "bad", "First | Last, e.g. Maria | de la Cruz"
        return "name", {"first": first, "last": last}
    if L.split_name(t) is None or any(c.isdigit() or c in "@<>/\\=" for c in t):
        return "bad", "a person's name, e.g. Carol Parish"
    return "name", t


def say(v) -> str:
    """A name as written, for a person to read."""
    if isinstance(v, dict):
        return f"{v.get('first', '')} | {v.get('last', '')}".strip(" |")
    return str(v)


def as_value(first: str, last: str):
    """What to write for a name found on the system."""
    if " " in last.strip():
        return {"first": first, "last": last}
    return f"{first} {last}".strip()


def load(path) -> dict:
    """Answers from a file: {netid: "First Last" | {first, last} | "" | "-"}.
    TOML ([console.labs.check.names], a [names] table, or NETID = "..."
    lines), or lines of NETID,First Last (a comma, a tab or a colon)."""
    from nomad.config.edit import DecodeError, loads
    text = open(path, encoding="utf-8").read()
    try:
        data = loads(text)
    except DecodeError:
        data = None
    if data is not None:
        names = data
        for k in NAMES:
            if isinstance(names, dict) and k in names:
                names = names[k]
            else:
                break
        if names is data and isinstance(data.get("names"), dict):
            names = data["names"]
        if not isinstance(names, dict):
            raise ValueError(f"{path}: no names in it")
        return {str(k).strip().lower(): v for k, v in names.items()
                if not isinstance(v, dict) or "last" in v}
    out = {}
    for i, line in enumerate(text.splitlines(), 1):
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*[,\t:]\s*(.*)$", t)
        if not m:
            raise ValueError(f"{path} line {i}: NETID,First Last")
        out[m.group(1).lower()] = m.group(2).strip().strip('"')
    return out


# -- writing ----------------------------------------------------------------------------------

def _get(data: dict, path: tuple):
    node = data
    for k in path:
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def written_names(data: dict) -> dict:
    got = _get(data, NAMES)
    return dict(got) if isinstance(got, dict) else {}


def set_names(edit, add: dict, remove=()) -> list[str]:
    """Add or change names in [console.labs.check.names] (the table where it
    is, or a new one after [console.labs.check]); the change, a line each.
    ValueError when names is written some other way (inline), or holds
    entries nomad can't write back: then a person changes it."""
    from nomad.config.edit import value
    if edit.assigned(NAMES):
        raise ValueError(f"names is written another way in {edit.path.name} (not a "
                         "[console.labs.check.names] table): change it by hand")
    current = written_names(edit.data)
    for k, v in current.items():
        try:
            value(v)
            ok = isinstance(v, str) or (isinstance(v, dict) and all(isinstance(x, str) for x in v.values()))
        except TypeError:
            ok = False
        if not ok:
            raise ValueError(f"[console.labs.check.names] {k} = {v!r} isn't a name: fix it by hand first")
    new, changes = dict(current), []
    for n, v in add.items():
        if current.get(n) == v:
            continue
        changes.append(f"{n} = {say(v)}" + (f" (was {say(current[n])})" if n in current and current[n] else ""))
        new[n] = v
    for n in remove:
        if n in new:
            del new[n]
            changes.append(f"{n}: name taken out")
    if changes:
        edit.set_table(NAMES, dict(sorted(new.items())), after=CHECK)
    return changes


def _prune(d):
    if isinstance(d, dict):
        out = {k: _prune(v) for k, v in d.items()}
        return {k: v for k, v in out.items() if v != {}}
    return d


def check(before: dict, names: dict, not_labs: list | None = None):
    """For TomlEdit.save: names (and not_labs) read back as wanted, nothing
    else changed."""
    from nomad.config.labs import NOT_LABS, not_labs_of

    def rest(d):
        d = copy.deepcopy(d)
        check_t = _get(d, CHECK)
        if isinstance(check_t, dict):
            check_t.pop("names", None)
        labs = _get(d, ("console", "labs"))
        if not_labs is not None and isinstance(labs, dict):
            labs.pop(NOT_LABS, None)
        return _prune(d)

    def ok(data: dict) -> bool:
        return (rest(data) == rest(before) and written_names(data) == names
                and (not_labs is None or not_labs_of(data) == not_labs))
    return ok


def open_to_all(path) -> str | None:
    """A note when nomad.toml can be read by anyone on the machine (it may
    hold a mail password)."""
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return None
    if mode & 0o004:
        return (f"note: {path} can be read by anyone on this machine (mode {mode:o}); it may hold a "
                f"mail password: chmod o-r {path}")
    return None

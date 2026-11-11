# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The campus directory, asked who a NetID or a name is (1.42.0).

For account requests (nomad.account_requests): a PI types NetIDs or names,
and nomad says who each is before anything goes to ARC. Asked through
``ldapsearch`` (openldap-clients), as a service account, so nomad needs no
LDAP library of its own and the password is handed over in a file, never on
a command line. From nomad.toml::

    [directory]
    uri = "ldaps://ldap.richmond.edu"           # ldaps: the password is never sent in the clear
    base = "ou=people,dc=richmond,dc=edu"       # where people are
    bind_dn = "uid=...,ou=...,dc=richmond,dc=edu"   # the service account
    password_file = "/home/nomad/.config/nomad/directory_password"   # its password, the nomad account's (600)
    timeout = 15                                # seconds a search may take

What nomad reads of a person: their NetID (``uid``) and their name, as the
directory has it: ``displayName``, else ``cn``, else given name and surname.
Nothing else is asked for. A name is looked up in the surname and given-name
fields and in the full names (some entries, staff's among them, have only
those), and only when both a first and a last name are given: a surname
alone matches hundreds of people in a campus directory that keeps its alumni.
"""
from __future__ import annotations

import base64
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

NETID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
CHUNK = 40                 # NetIDs asked about in one search
MOST_MATCHES = 10          # names: more than this is "too many to choose from"


class DirectoryError(RuntimeError):
    """The directory can't be asked: said as it is, with no secret in it."""


@dataclass(frozen=True)
class Settings:
    uri: str = ""
    base: str = ""
    bind_dn: str = ""
    password_file: str = ""
    timeout: int = 15
    problems: tuple = field(default_factory=tuple)

    @property
    def on(self) -> bool:
        return bool(self.uri and self.base and not self.problems)


def settings(config: dict | None) -> Settings:
    d = (config or {}).get("directory")
    if not isinstance(d, dict) or not d:
        return Settings()
    problems = []
    uri = str(d.get("uri") or "").strip()
    if uri and not re.match(r"^ldaps?://[A-Za-z0-9.-]+(:\d+)?/?$", uri):
        problems.append(f"[directory] uri {uri!r} is not ldaps://host or ldap://host")
    if uri.startswith("ldap://") and d.get("bind_dn"):
        problems.append("[directory] uri is ldap://: the service account's password would go in the clear; use ldaps://")
    base = str(d.get("base") or "").strip()
    if uri and not base:
        problems.append("[directory] base is not set (where people are: ou=people,dc=...)")
    bind_dn = str(d.get("bind_dn") or "").strip()
    pw = str(d.get("password_file") or "").strip()
    if bind_dn and not pw:
        problems.append("[directory] bind_dn is set but password_file isn't")
    try:
        timeout = max(3, min(60, int(d.get("timeout", 15))))
    except (TypeError, ValueError):
        timeout = 15
        problems.append("[directory] timeout is not a number of seconds")
    return Settings(uri=uri, base=base, bind_dn=bind_dn, password_file=os.path.expanduser(pw),
                    timeout=timeout, problems=tuple(problems))


def escape(value: str) -> str:
    """A value made safe inside an LDAP filter (RFC 4515)."""
    out = []
    for ch in str(value):
        if ch in "*()\\\0":
            out.append(f"\\{ord(ch):02x}")
        else:
            out.append(ch)
    return "".join(out)


def parse_ldif(text: str) -> list[dict]:
    """ldapsearch -LLL output -> [{attribute (lower case): [values]}]."""
    entries, cur, last = [], {}, None
    lines = text.splitlines()
    unfolded = []
    for ln in lines:
        if ln.startswith(" ") and unfolded:
            unfolded[-1] += ln[1:]
        else:
            unfolded.append(ln)
    for ln in unfolded + [""]:
        if not ln.strip():
            if cur:
                entries.append(cur)
            cur, last = {}, None
            continue
        if ln.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9;-]+)(::?)\s?(.*)$", ln)
        if not m:
            continue
        name, sep, value = m.group(1).lower(), m.group(2), m.group(3)
        if sep == "::":
            try:
                value = base64.b64decode(value).decode("utf-8", "replace")
            except Exception:          # noqa: BLE001 -- an odd value: left out
                continue
        cur.setdefault(name, []).append(value)
        last = name
    return entries


def name_of(entry: dict) -> str:
    for key in ("displayname", "cn"):
        if entry.get(key):
            return entry[key][0].strip()
    parts = [entry.get("givenname", [""])[0], entry.get("sn", [""])[0]]
    return " ".join(p for p in parts if p).strip()


class Directory:
    """Asks the directory, as the settings say."""

    ATTRS = ("uid", "displayName", "cn", "givenName", "sn")

    def __init__(self, s: Settings, run=subprocess.run):
        if not s.on:
            raise DirectoryError("; ".join(s.problems) or "[directory] is not set in nomad.toml")
        self.s = s
        self._run = run

    def _password(self) -> str | None:
        if not self.s.bind_dn:
            return None
        try:
            pw = Path(self.s.password_file).read_text(encoding="utf-8").rstrip("\n")
        except OSError as exc:
            raise DirectoryError(f"the service account's password file can't be read ({exc.strerror})") from None
        if not pw:
            raise DirectoryError("the service account's password file is empty")
        return pw

    def search(self, filt: str, limit: int = 50) -> tuple[list[dict], bool]:
        """(entries, more): ``more`` when the directory had more than ``limit``."""
        cmd = ["ldapsearch", "-LLL", "-x", "-o", "ldif-wrap=no", "-H", self.s.uri, "-b", self.s.base,
               "-z", str(limit + 1), "-l", str(self.s.timeout)]
        pw = self._password()
        tmp = None
        try:
            if pw is not None:
                fd, tmp = tempfile.mkstemp(prefix="nomad-dir-")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(pw)
                cmd += ["-D", self.s.bind_dn, "-y", tmp]
            cmd += [filt, *self.ATTRS]
            try:
                r = self._run(cmd, capture_output=True, text=True, timeout=self.s.timeout + 5)
            except FileNotFoundError:
                raise DirectoryError("ldapsearch is not installed here (openldap-clients)") from None
            except subprocess.TimeoutExpired:
                raise DirectoryError("the directory didn't answer in time") from None
        finally:
            if tmp:
                os.unlink(tmp)
        # 0: done; 4: size limit reached (what came is kept); 32: nothing under the base.
        if r.returncode not in (0, 4, 32):
            first = (r.stderr or r.stdout or "").strip().splitlines()[:1]
            said = re.sub(r"(uid|cn)=[^,\s]*", r"\1=…", first[0]) if first else f"exit {r.returncode}"
            raise DirectoryError(f"the directory refused the search: {said}")
        entries = parse_ldif(r.stdout)
        return entries[:limit], len(entries) > limit or r.returncode == 4

    def by_netids(self, netids) -> dict:
        """{netid: name or None (not in the directory)} for each NetID asked."""
        want = sorted({n.strip().lower() for n in netids if NETID.match(n.strip().lower())})
        out = dict.fromkeys(want)
        for i in range(0, len(want), CHUNK):
            part = want[i:i + CHUNK]
            filt = "(|" + "".join(f"(uid={escape(n)})" for n in part) + ")"
            entries, _ = self.search(filt, limit=len(part) * 2)
            for e in entries:
                uid = (e.get("uid") or [""])[0].strip().lower()
                if uid in out:
                    out[uid] = name_of(e) or uid
        return out

    def by_name(self, first: str, last: str) -> tuple[list[dict], bool]:
        """([{netid, name}], too_many) for a first and a last name: in the
        surname and given-name fields, or both in a full name."""
        f, l = escape(first.strip()), escape(last.strip())
        if not f or not l:
            return [], False
        filt = (f"(|(&(sn={l})(givenName={f}*))"
                f"(&(|(cn=*{l}*)(displayName=*{l}*))(|(cn={f}*)(displayName={f}*)(cn=* {f}*)(displayName=* {f}*))))")
        entries, more = self.search(filt, limit=MOST_MATCHES)
        found = {}
        for e in entries:
            uid = (e.get("uid") or [""])[0].strip().lower()
            if uid:
                found[uid] = {"netid": uid, "name": name_of(e) or uid}
        return sorted(found.values(), key=lambda x: (x["name"].lower(), x["netid"])), more

    def check(self) -> dict:
        """Can it be asked? A search for nothing that exists, as the service account."""
        self.search("(uid=nomad-directory-check-0)", limit=1)
        return {"uri": self.s.uri, "base": self.s.base, "as": "a service account" if self.s.bind_dn else "anonymous"}

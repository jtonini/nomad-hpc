# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Moving files from the browser (1.55.0): the hub's side.

A person signed in to the Console puts files of theirs on a cluster (dropped
on the page, or chosen) and takes files of theirs back, as themselves,
through the cluster's nomad-gate: a file goes in pieces (PIECE_MB each, 8 MB
by default), each with its own sha256, the whole file's sha256 checked on
the cluster before it is put in place, never over a file there unless they
say Replace; it comes back in pieces too, each checked, and stops if the
file changes meanwhile. Nothing is kept on the hub: a piece passes through.
A break (the network, the laptop's lid) costs nothing: the cluster says how
much of it is there, and the rest is sent from there.

Up to ``max_mb`` (2 GB by default) a file; past that, and for many files or
whole folders, the page gives the rsync command to type on their computer
instead (and the scp one, for Windows), with the cluster's login host.

The cluster's say is the gate's: only where its hub key's line runs
``nomad-gate serve --edit`` (nomad gate install-key --edit) -- the same as
saving scripts from the Console -- and the hub's ``edit = true`` in that
cluster's [[hub.sites]]. Every name here is the site's; nothing about one
institution is written into nomad.

Settings, in the hub's nomad.toml::

    [console.files]
    open = "submit"          # who may: "submit" (whoever may Submit a job: [console] submit),
                             #   "staff", "everyone" signed in with the campus sign-in, or "off"
    max_mb = 2048            # the largest file moved from the browser (up to 1 TB)
    piece_mb = 8             # a piece (1 to 16)
    folders = ["~"]          # where My files opens, as shortcuts: ~ is their home, {user} their name
    only_in_folders = false  # true: only in those folders and below

    [console.files.sites.spydur]   # one cluster's own (optional)
    folders = ["~", "/scratch/{user}"]
    login = "spydur.example.edu"   # the host people use for rsync (default: its [[hub.sites]] host)
"""
from __future__ import annotations

import base64
import hashlib
import posixpath
import re
import shlex
from dataclasses import dataclass, field

OPENS = ("submit", "staff", "everyone", "off")
MAX_MB = 2048
MAX_MB_CAP = 1024 * 1024                         # the gate's MAX_FILE
PIECE_MB = 8
PIECE_MB_CAP = 16                                # the gate's MAX_PIECE
DEFAULT_FOLDERS = ("~",)
_HOST = re.compile(r"[A-Za-z0-9]([A-Za-z0-9-]{0,62})(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*")
_SITE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_USER = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")
_PLAIN = re.compile(r"[A-Za-z0-9._/+@%=:,-]+")


class FilesError(Exception):
    """What can't be done, said for the person."""


@dataclass(frozen=True)
class Settings:
    open: str = "submit"
    max_bytes: int = MAX_MB * 1024 * 1024
    piece: int = PIECE_MB * 1024 * 1024
    folders: tuple = DEFAULT_FOLDERS
    only_in_folders: bool = False
    sites: dict = field(default_factory=dict)        # site -> {"folders": (...), "login": "host"}
    problems: tuple = ()

    @property
    def max_mb(self) -> int:
        return self.max_bytes // (1024 * 1024)


def _folders(raw, where: str, problems: list) -> tuple | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        problems.append(f"{where} folders: a list of folders, e.g. [\"~\", \"/scratch/{{user}}\"]")
        return None
    out = []
    for f in raw:
        if not isinstance(f, str) or not (f == "~" or f.startswith("~/") or f.startswith("/")):
            problems.append(f"{where} folders: {str(f)[:60]!r} isn't ~, ~/... or a full path")
            continue
        if any(p in (".", "..") or p.startswith(".") for p in f.split("/")[1:] if p):
            problems.append(f"{where} folders: {f[:60]!r} has a hidden part, . or ..")
            continue
        out.append(f.rstrip("/") or "/")
    return tuple(dict.fromkeys(out)) or None


def settings(config: dict | None) -> Settings:
    """[console.files] from the hub's nomad.toml (see above); what is wrong
    in it is said in ``problems`` and the default used."""
    console = (config or {}).get("console") if isinstance(config, dict) else None
    raw = console.get("files") if isinstance(console, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    problems: list = []
    open_ = str(raw.get("open", "submit")).strip().lower()
    if open_ not in OPENS:
        problems.append(f"open = {open_[:20]!r}: one of {', '.join(OPENS)} (submit used)")
        open_ = "submit"
    max_mb = raw.get("max_mb", MAX_MB)
    if isinstance(max_mb, bool) or not isinstance(max_mb, int) or not 1 <= max_mb <= MAX_MB_CAP:
        problems.append(f"max_mb: a whole number of MB, 1 to {MAX_MB_CAP} ({MAX_MB} used)")
        max_mb = MAX_MB
    piece_mb = raw.get("piece_mb", PIECE_MB)
    if isinstance(piece_mb, bool) or not isinstance(piece_mb, int) or not 1 <= piece_mb <= PIECE_MB_CAP:
        problems.append(f"piece_mb: 1 to {PIECE_MB_CAP} ({PIECE_MB} used)")
        piece_mb = PIECE_MB
    folders = _folders(raw.get("folders"), "[console.files]", problems) or DEFAULT_FOLDERS
    only = raw.get("only_in_folders", False)
    if not isinstance(only, bool):
        problems.append("only_in_folders: true or false (false used)")
        only = False
    sites = {}
    per = raw.get("sites") if isinstance(raw.get("sites"), dict) else {}
    for name, v in per.items():
        if not _SITE.fullmatch(str(name)) or not isinstance(v, dict):
            problems.append(f"[console.files.sites.{str(name)[:30]}]: a cluster's name, with its folders and login")
            continue
        entry = {}
        f = _folders(v.get("folders"), f"[console.files.sites.{name}]", problems)
        if f:
            entry["folders"] = f
        login = v.get("login")
        if login is not None:
            if isinstance(login, str) and _HOST.fullmatch(login.strip()):
                entry["login"] = login.strip()
            else:
                problems.append(f"[console.files.sites.{name}] login: a host name")
        sites[name] = entry
    return Settings(open=open_, max_bytes=max_mb * 1024 * 1024, piece=piece_mb * 1024 * 1024, folders=folders,
                    only_in_folders=only, sites=sites, problems=tuple(problems))


def opens_to(s: Settings, role: str | None, submit_open: str) -> bool:
    """Whether My files is open to someone of this role (the Console checks
    the rest: a campus sign-in, not viewing as someone)."""
    staff = role in ("admin", "operator")
    if s.open == "off":
        return False
    if s.open == "staff":
        return staff
    if s.open == "everyone":
        return True
    return staff or submit_open == "everyone"             # "submit": as Submit a job


def folders_for(s: Settings, site: str, user: str, home: str) -> list:
    """The folders My files offers for this person on ``site`` (full paths)."""
    if not _USER.fullmatch(user or "") or not home.startswith("/"):
        return []
    out = []
    for f in (s.sites.get(site) or {}).get("folders") or s.folders:
        f = f.replace("{user}", user)
        f = home if f == "~" else posixpath.join(home, f[2:]) if f.startswith("~/") else f
        out.append(posixpath.normpath(f))
    return list(dict.fromkeys(out))


def check_path(s: Settings, site: str, user: str, home: str, path: str, *, folder: bool = False) -> str:
    """``path`` as the gate takes it (a full path, no ., .. or hidden part;
    within the folders when only_in_folders), else FilesError."""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or len(path) > 4096:
        raise FilesError("a full path, please")
    parts = [p for p in path.split("/") if p]
    if any(p in (".", "..") for p in parts) or "//" in path:
        raise FilesError("give the path without . or .. in it")
    if any(p.startswith(".") for p in parts):
        raise FilesError("not a hidden file or folder")
    norm = "/" + "/".join(parts)
    if not folder and norm == "/":
        raise FilesError("a file's full path, please")
    if s.only_in_folders:
        mine = folders_for(s, site, user, home)
        if not any(norm == f or norm.startswith(f.rstrip("/") + "/") for f in mine):
            raise FilesError(f"only in {', '.join(mine) or 'your folders'} here")
    return norm


def login_of(s: Settings, site: dict) -> str:
    return (s.sites.get(site.get("name")) or {}).get("login") or str(site.get("host") or site.get("name") or "")


def _remote_spec(user: str, host: str, path: str) -> str:
    spec = f"{user}@{host}:{path}"
    return spec if _PLAIN.fullmatch(spec) else shlex.quote(spec)


def _local(name: str) -> str:
    name = "./" + name if name.startswith("-") else name          # never taken for an option
    return name if _PLAIN.fullmatch(name) else shlex.quote(name)


def commands(s: Settings, site: dict, user: str, folder: str, name: str | None = None) -> dict:
    """What to type on their own computer for files too big for the browser,
    or many, or whole folders: rsync (goes on where it stopped) and scp
    (Windows has it), to put a file (or a folder) in ``folder``, and to take
    ``name`` from it back."""
    host = login_of(s, site)
    folder = folder.rstrip("/") or "/"
    here = _local(name or "FILE")
    put_to = _remote_spec(user, host, folder + "/")
    out = {"host": host,
           "put_rsync": f"rsync -av --partial --progress {here} {put_to}",
           "put_folder_rsync": f"rsync -av --partial --progress {_local('FOLDER')} {put_to}",
           "put_scp": f"scp {here} {put_to}"}
    if name:
        took = _remote_spec(user, host, posixpath.join(folder, name))
        out.update(get_rsync=f"rsync -av --partial --progress {took} .", get_scp=f"scp {took} .")
    return out


# ── taking a file back, in pieces ───────────────────────────────────────────

class Changed(FilesError):
    """The file changed while it was being taken."""


def facts(call, path: str) -> dict:
    """What the file is (its size, version), from the gate: ``call(op,
    **options)`` asks it as the person. FilesError when it can't be taken."""
    doc = call("get", path=path, length=0)
    if not isinstance(doc, dict) or not doc.get("ok"):
        raise FilesError(str((doc or {}).get("error") or "the cluster didn't say"))
    return doc


def pieces(call, path: str, size: int, version: str, piece: int):
    """The file's bytes, piece by piece, each checked (its sha256), the file
    the same one throughout (its version); FilesError (Changed) when not."""
    at = 0
    while at < size:
        doc = call("get", path=path, offset=at, length=min(piece, size - at))
        if not isinstance(doc, dict) or not doc.get("ok"):
            raise FilesError(str((doc or {}).get("error") or "the cluster didn't say"))
        if doc.get("version") != version:
            raise Changed(f"{path} changed while it was being taken: take it again")
        data = base64.b64decode(doc.get("data") or "", validate=True)
        if hashlib.sha256(data).hexdigest() != doc.get("sha256") or len(data) != doc.get("length"):
            raise FilesError("a piece didn't arrive whole: take it again")
        if not data:
            raise Changed(f"{path} is shorter than it was: take it again")
        yield data
        at += len(data)


def upload_id(user: str, site: str, path: str, size: int, modified: int) -> str:
    """The id a file being sent goes by (as the page makes it): the same
    file sent again to the same place goes on where it stopped."""
    return hashlib.sha256(f"{user}|{site}|{path}|{size}|{modified}".encode()).hexdigest()[:24]

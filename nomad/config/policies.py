# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Research computing's policy documents, for the Console's Help > Policies
(``[[console.policies]]`` in nomad.toml): what each says it is, and where it
is, a file the Console serves or a link to where it lives.

    [[console.policies]]
    title = "Research computing usage policy"
    file = "/etc/nomad/policies/usage-policy.pdf"   # PDF, HTML, Markdown or text
    about = "Who may use the clusters, what each lab gets, and its term report."
    updated = "2026-09-01"                          # optional: the document's date

    [[console.policies]]
    title = "Data storage and retention"
    url = "https://example.edu/research-computing/storage-policy"

``nomad console policies`` lists them and says what is wrong with any;
``nomad console policy TITLE --file PATH|--url URL [--about ...] --apply``
adds or changes one, ``--remove`` takes it off. The Console reads the list
each time (no restart), serves only the files listed, and none larger than
MAX_BYTES.
"""
from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

TABLE = ("console", "policies")
BACKUPS = 20
MAX_BYTES = 25 * 1024 * 1024
# What the Console shows, and how it says so.
TYPES = {".pdf": ("pdf", "application/pdf"), ".html": ("html", "text/html"), ".htm": ("html", "text/html"),
         ".md": ("markdown", "text/markdown"), ".txt": ("text", "text/plain")}
KEYS = ("title", "file", "url", "about", "updated")


class PolicyError(ValueError):
    """An entry that can't be written as asked; nothing was changed."""


@dataclass
class Policy:
    id: str                        # from the title: the Console's link to it
    title: str
    about: str | None = None
    file: str | None = None
    url: str | None = None
    updated: str | None = None     # YYYY-MM-DD, as written
    kind: str | None = None        # pdf, html, markdown, text; "link" for a url
    media_type: str | None = None
    problem: str | None = None     # why the Console can't show it (for staff)

    def public(self) -> dict:
        """What anyone signed in may know of it: no path, no problem."""
        return {"id": self.id, "title": self.title, "about": self.about, "kind": self.kind,
                "url": self.url, "updated": self.updated}


def slug(title: str) -> str:
    t = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")[:60] or "policy"


def _date(v) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise PolicyError(f"updated = {v!r}: a date such as 2026-09-01") from None


def check_url(url: str) -> str:
    u = str(url or "").strip()
    if not re.match(r"^https?://[^\s/]+", u) or any(c in u for c in "\"'<> "):
        raise PolicyError(f"{url!r}: a link that starts with https:// (or http://)")
    return u


def check_path(file: str) -> Path:
    p = Path(str(file or "").strip()).expanduser()
    if not p.is_absolute():
        raise PolicyError(f"{file!r}: the file's full path, from /")
    if p.suffix.lower() not in TYPES:
        raise PolicyError(f"{p.name}: a .pdf, .html, .md or .txt file")
    return p


def file_problem(path: str) -> str | None:
    """Why this process can't serve the file, or None."""
    p = Path(path)
    try:
        st = p.stat()
    except FileNotFoundError:
        return "no such file"
    except PermissionError:
        return "the Console's account can't reach it (a folder on the way isn't open to it)"
    except OSError as e:
        return f"can't be read ({e.strerror or e})"
    if not p.is_file():
        return "not a file"
    if st.st_size > MAX_BYTES:
        return f"larger than {MAX_BYTES // (1024 * 1024)} MB"
    if not os.access(p, os.R_OK):
        return "the Console's account can't read it (e.g. chmod 644 it)"
    return None


def open_to_all(path: str) -> bool:
    """Whether any account on the machine can read the file: it, readable
    by others, in folders they can pass through."""
    p = Path(path)
    try:
        if not p.stat().st_mode & 0o004:
            return False
        return all(d.stat().st_mode & 0o001 for d in p.parents)
    except OSError:
        return False


def read(config: dict | None, *, check_files: bool = True) -> tuple[list[Policy], list[str]]:
    """The entries as the Console shows them, and what is wrong with the
    list (an entry without a title, or with neither file nor url, is left
    out and said). An entry whose file this process can't serve is kept,
    with its ``problem``."""
    raw = ((config or {}).get("console") or {}).get("policies")
    if raw is None:
        return [], []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return [], ["[console] policies: [[console.policies]] tables, each with a title and a file or url"]
    out: list[Policy] = []
    problems: list[str] = []
    seen: set = set()
    for n, e in enumerate(raw, 1):
        where = f"[[console.policies]] {n}"
        if not isinstance(e, dict):
            problems.append(f"{where}: not a table; left out")
            continue
        title = str(e.get("title") or "").strip()
        if not title:
            problems.append(f"{where}: no title; left out")
            continue
        where = f"[[console.policies]] {title!r}"
        unknown = sorted(set(e) - set(KEYS))
        if unknown:
            problems.append(f"{where}: {', '.join(unknown)}: unknown; ignored")
        file, url = e.get("file"), e.get("url")
        if bool(file) == bool(url):
            problems.append(f"{where}: give a file or a url (one of them); left out")
            continue
        pid = base = slug(title)
        k = 2
        while pid in seen:
            pid, k = f"{base}-{k}", k + 1
        seen.add(pid)
        p = Policy(id=pid, title=title, about=(str(e["about"]).strip() or None) if e.get("about") else None)
        try:
            p.updated = _date(e.get("updated"))
        except PolicyError as err:
            problems.append(f"{where}: {err}; not shown")
        if url:
            try:
                p.url, p.kind = check_url(url), "link"
            except PolicyError as err:
                problems.append(f"{where}: {err}; left out")
                continue
        else:
            try:
                path = check_path(file)
            except PolicyError as err:
                problems.append(f"{where}: {err}; left out")
                continue
            p.file = str(path)
            p.kind, p.media_type = TYPES[path.suffix.lower()]
            if check_files:
                # Said on the entry itself (the Console shows staff why).
                p.problem = file_problem(p.file)
                if not p.problem and not p.updated:
                    p.updated = datetime.fromtimestamp(Path(p.file).stat().st_mtime).date().isoformat()
        out.append(p)
    return out, problems


def entry(title: str, *, file: str | None = None, url: str | None = None, about: str | None = None,
          updated: str | None = None) -> dict:
    """The [[console.policies]] table to write, checked."""
    title = str(title or "").strip()
    if not title or "\n" in title:
        raise PolicyError("a title, on one line")
    if bool(file) == bool(url):
        raise PolicyError("give --file or --url (one of them)")
    body = {"title": title}
    if file:
        body["file"] = str(check_path(file))
    else:
        body["url"] = check_url(url)
    if about:
        body["about"] = " ".join(str(about).split())
    if updated:
        body["updated"] = _date(updated)
    return body


def find(data: dict, title: str) -> list[dict]:
    raw = (data.get("console") or {}).get("policies")
    if isinstance(raw, dict):
        raw = [raw]
    return [e for e in (raw or []) if isinstance(e, dict) and str(e.get("title") or "").strip() == title.strip()]


def set_policy(edit, body: dict | None, title: str) -> list[str]:
    """Add, change (body) or remove (None) the entry named ``title`` in a
    TomlEdit; the changes, in words ([] when it is so already)."""
    from nomad.config.edit import table_text
    if edit.defined_inline(TABLE):
        raise PolicyError("[console] policies is written inline (policies = [...]): change it by hand")
    data = edit.data
    if not isinstance((data.get("console") or {}).get("policies", []), list):
        raise PolicyError("[console] policies isn't a list of [[console.policies]] tables: change it by hand")
    now = find(data, title)
    if body is None:
        if not now:
            return []
        edit.remove_entries(TABLE, title=now[0]["title"])
        return [f"[[console.policies]] {title!r}: taken off"]
    if len(now) == 1 and now[0] == body:
        return []
    if len(now) > 1:
        raise PolicyError(f"{len(now)} entries are titled {title!r}: change them by hand")
    if now:
        edit.remove_entries(TABLE, title=now[0]["title"])
    edit.add_entry(TABLE, body)
    return [("changed:\n" if now else "") + table_text(TABLE, body, array=True).rstrip("\n")]


def check_for(title: str, body: dict | None):
    """What the saved file must read as: that entry, once (or none)."""
    def ok(data: dict) -> bool:
        got = find(data, title)
        return got == ([] if body is None else [body])
    return ok

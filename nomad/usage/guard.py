# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The last check before a report is written: no name from the data in it.

Every username, group or account, job name, working-directory part and
partition seen while reading the data is looked for in the report's text:
everything a reader sees (the Markdown) and every text value of the JSON
(not its keys or fact ids, which are the report's own). Words the report is
made of (its sentences, the tier and family names of report.toml, the labels
it prints) don't count: a user called "basic" is not leaked by the basic
tier's row. Labels that come from the data (filesystems, departments,
session types) have had usernames, groups and partitions taken out before
they get here. What is left is a name that came from the data into the
text, and then nothing is written.

Not checked: names of one character, numbers, and dates (a directory named
2026-02 is not identifying, and the report is full of months).
"""
from __future__ import annotations

import ast
import json
import re
from functools import lru_cache
from pathlib import Path

from nomad.usage.sources import REASONS, SOURCE_WORDS, Names

_TOKEN = re.compile(r"[a-z0-9_\-@+]+")
_ALNUM = re.compile(r"[a-z0-9]+")
_TOKEN_ONLY = re.compile(r"^[a-z0-9_\-@+]+$")
_DATEISH = re.compile(r"^\d{2,4}([-_]\d{1,2}){1,2}([t_ -]\d{1,2}([-_:]\d{2}){0,2})?$")
# The modules whose strings make up the report's text.
_TEXT_MODULES = ("sections.py", "render.py", "fmt.py", "facts.py")


class GuardError(RuntimeError):
    def __init__(self, found: dict[str, set[str]]):
        self.found = found
        parts = [f"{len(v)} {k}" for k, v in found.items() if v]
        super().__init__("the report would contain names from the data (" + ", ".join(parts)
                         + "); nothing was written")


@lru_cache(maxsize=1)
def vocabulary() -> frozenset[str]:
    """Every word in the report's own text: the string literals of the
    modules that write it (docstrings left out), and its labels."""
    from nomad.usage.config import ALL_NODES, CONDO, OTHER, OVERLAY
    words: set[str] = set()
    for name in _TEXT_MODULES:
        path = Path(__file__).parent / name
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                words.update(_TOKEN.findall(node.value.lower()))
    for label in (*REASONS, *SOURCE_WORDS, ALL_NODES, CONDO, OTHER, OVERLAY):
        words.update(_TOKEN.findall(label.lower()))
    # The parts of the report's own joined words ("core-hours": core, hours).
    words.update(p for w in list(words) for p in _ALNUM.findall(w))
    return frozenset(words)


def labels_of(*texts) -> set[str]:
    out: set[str] = set()
    for t in texts:
        if t:
            out.update(_TOKEN.findall(str(t).lower()))
    return out


def json_text(text: str) -> str:
    """The text values of a JSON document: not its keys, not fact ids."""
    out: list[str] = []

    def walk(v, key=None):
        if isinstance(v, dict):
            for k, x in v.items():
                if k not in ("id", "key"):
                    walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)
        elif isinstance(v, str):
            out.append(v)
    walk(json.loads(text))
    return "\n".join(out)


def check(texts: list[str], names: Names, allowed: set[str], data_labels: set[str] | None = None) -> None:
    """Raise GuardError if any name from ``names`` is in ``texts``. ``allowed``:
    words that may appear whatever they are; ``data_labels``: words of labels
    from the data, which excuse anything but a username."""
    base = vocabulary() | {a.lower() for a in allowed}
    joined = "\n".join(texts).lower()
    tokens = set(_TOKEN.findall(joined))
    # A name joined to other words by - or _ ("alice_old"): its parts too.
    parts = {p for t in tokens for p in _ALNUM.findall(t)}
    found: dict[str, set[str]] = {}
    for kind, bucket in (("usernames", names.users), ("groups or accounts", names.groups),
                         ("job names", names.job_names), ("directory names", names.paths),
                         ("partitions", names.partitions), ("other names", names.others)):
        ok = base if kind == "usernames" else base | {w.lower() for w in (data_labels or ())}
        hits = set()
        for name in bucket:
            n = str(name).strip().lower()
            if len(n) < 2 or n.isdigit() or _DATEISH.match(n):
                continue
            if _TOKEN_ONLY.match(n):
                if (n in tokens or (kind in ("usernames", "groups or accounts", "partitions")
                                    and _ALNUM.fullmatch(n) and n in parts)) and n not in ok:
                    hits.add(name)
            else:
                # A name with spaces, dots or slashes: as a phrase, unless all
                # its words are the report's own.
                words = _TOKEN.findall(n)
                if words and all(w in ok for w in words):
                    continue
                if re.search(r"(?<![a-z0-9_])" + re.escape(n) + r"(?![a-z0-9_])", joined):
                    hits.add(name)
        if hits:
            found[kind] = hits
    if found:
        raise GuardError(found)

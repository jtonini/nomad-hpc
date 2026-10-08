# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Slurm node lists: ``cn[01-02,05],gpu17`` as the nodes they name.

Slurm writes a job's nodes in its compact range form, and nomad stores them
that way (``jobs.node_list``). Splitting on commas breaks inside brackets
("cn[01-02" and "05]"), and matching a node name with LIKE misses ranges
("cn02" is not in "cn[01-02]"). Everything that asks "which nodes did
this job use" goes through here.
"""
from __future__ import annotations

import re

# A list longer than this is not expanded: no real job spans more nodes, and a
# malformed range ("n[1-99999999]") must not make anything slow.
MAX_NODES = 10_000

_EMPTY = {"", "none", "none assigned", "(null)", "n/a"}
_BRACKET = re.compile(r"\[([^\[\]]*)\]")


def _split_top(text: str) -> list[str]:
    """Split at commas outside brackets."""
    parts, depth, cur = [], 0, []
    for ch in text:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth = max(depth - 1, 0)
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _expand_one(name: str, out: list[str]) -> None:
    m = _BRACKET.search(name)
    if not m:
        if "[" in name or "]" in name:
            raise ValueError(f"unbalanced brackets: {name!r}")
        out.append(name)
        return
    pre, body, post = name[:m.start()], m.group(1), name[m.end():]
    for item in body.split(","):
        item = item.strip()
        if not item:
            continue
        if "-" in item:
            a, b = item.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"not a range: {item!r}")
            lo, hi = int(a), int(b)
            if hi < lo or hi - lo >= MAX_NODES:
                raise ValueError(f"range too large or reversed: {item!r}")
            width = len(a)
            for i in range(lo, hi + 1):
                _expand_one(f"{pre}{str(i).zfill(width)}{post}", out)
                if len(out) > MAX_NODES:
                    raise ValueError("node list too long")
        else:
            if not item.isdigit():
                raise ValueError(f"not a number: {item!r}")
            _expand_one(f"{pre}{item}{post}", out)
        if len(out) > MAX_NODES:
            raise ValueError("node list too long")


def expand_hostlist(text: str | None) -> list[str]:
    """The node names in a Slurm node list, in order, without repeats.

    ``'cn[01-02,05],gpu17'`` -> ``['cn01', 'cn02', 'cn05', 'gpu17']``;
    zero padding follows the range start; several bracket groups in one name
    expand as a product. Empty values, "None assigned" and "(null)" give [].
    A list that can't be read is returned as its comma-separated pieces with
    any bracketed piece left out, never raising: a reader shows less rather
    than fail.
    """
    if text is None:
        return []
    text = str(text).strip()
    if text.lower() in _EMPTY:
        return []
    out: list[str] = []
    for part in _split_top(text):
        names: list[str] = []
        try:
            _expand_one(part, names)
        except ValueError:
            continue        # all of this piece or none of it
        out.extend(names)
        if len(out) > MAX_NODES:
            break
    seen: set[str] = set()
    return [n for n in out[:MAX_NODES] if not (n in seen or seen.add(n))]


def node_in(node: str, text: str | None) -> bool:
    """Whether ``node`` is one of the nodes in the Slurm node list ``text``."""
    return bool(node) and node in expand_hostlist(text)


def like_patterns(node: str) -> tuple[str, ...]:
    """SQL LIKE patterns, at least one of which every node list naming
    ``node`` matches (use with ESCAPE '\\', then check with ``node_in``).

    The name itself, for lists that spell it out; and, for range lists, the
    name up to each of its runs of digits, followed by a bracket ("cn02":
    "%cn02%", "%cn[%"; "rack1n02": "%rack1n02%", "%rack[%", "%rack1n[%").
    Lists of other nodes with ranges match too, so only lists that use ranges
    (multi-node jobs) need checking one by one.
    """
    def esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    node = node or ""
    patterns = [f"%{esc(node)}%"]
    for m in re.finditer(r"\d+", node):
        p = f"%{esc(node[:m.start()])}[%"
        if p not in patterns:
            patterns.append(p)
    return tuple(patterns)


def like_clause(column: str, node: str) -> tuple[str, tuple[str, ...]]:
    """('(col LIKE ? ESCAPE ... OR ...)', params) for like_patterns(node)."""
    pats = like_patterns(node)
    sql = "(" + " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for _ in pats) + ")"
    return sql, pats


__all__ = ["expand_hostlist", "node_in", "like_patterns", "like_clause", "MAX_NODES"]

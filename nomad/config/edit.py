# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Small, careful edits to nomad.toml, for `nomad lab`.

Whole tables and array-of-tables entries are added or removed as text, so
comments and layout elsewhere in the file stay as they are. Nothing is
written unless the result parses and reads back as intended, and the file
is copied to a backup first.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

try:
    import tomllib as _parser                  # Python 3.11+
    DecodeError = _parser.TOMLDecodeError
except ModuleNotFoundError:                    # Python 3.10: the toml package
    import toml as _parser
    DecodeError = _parser.TomlDecodeError


def loads(text: str) -> dict:
    return _parser.loads(text)

_HEADER = re.compile(r'^\s*(\[\[?)\s*(.+?)\s*\]\]?\s*(#.*)?$')
_KEY = re.compile(r'\s*(?:"((?:[^"\\]|\\.)*)"|\'([^\']*)\'|([A-Za-z0-9_-]+))\s*(?:\.|$)')


def key_path(header: str) -> tuple:
    """('console', 'labs', 'resources', 'chemlab$') from
    console.labs.resources."chemlab$" (bare, "basic" and 'literal' keys)."""
    out, pos = [], 0
    while pos < len(header):
        m = _KEY.match(header, pos)
        if not m or m.end() == pos:
            return ()
        basic, literal, bare = m.groups()
        try:
            out.append(json.loads(f'"{basic}"') if basic is not None
                       else literal if literal is not None else bare)
        except ValueError:                    # an escape JSON doesn't share
            return ()
        pos = m.end()
    return tuple(out)


def quoted_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def header(path: tuple, array: bool = False) -> str:
    inner = ".".join(quoted_key(k) for k in path)
    return f"[[{inner}]]" if array else f"[{inner}]"


def value(v) -> str:
    """A TOML value from a str, bool, int or list of str."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(value(x) for x in v) + "]"
    raise TypeError(f"not a TOML value here: {v!r}")


def table_text(path: tuple, body: dict, array: bool = False) -> str:
    return header(path, array) + "\n" + "".join(f"{quoted_key(k)} = {value(v)}\n"
                                                for k, v in body.items())


class TomlEdit:
    """An edit of one TOML file, held as text until save()."""

    def __init__(self, path):
        # A symlinked nomad.toml is edited where it points.
        self.path = Path(path).expanduser().resolve()
        self.original = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        self.text = self.original
        if self.text and not self.text.endswith("\n"):
            self.text += "\n"
        self.changes: list[str] = []

    @property
    def data(self) -> dict:
        return loads(self.text)

    def _blocks(self):
        """(lines, [(start, end, path, is_array)]): each table's line range,
        from its header to the next header. Comment lines just before the
        next header are left to that table."""
        lines = self.text.splitlines(keepends=True)
        heads = []
        for i, line in enumerate(lines):
            m = _HEADER.match(line)
            if m and line.lstrip().startswith("["):
                heads.append((i, key_path(m.group(2)), m.group(1) == "[["))
        blocks = []
        for n, (start, path, array) in enumerate(heads):
            end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
            while end - 1 > start and (not lines[end - 1].strip()
                                       or lines[end - 1].lstrip().startswith("#")):
                end -= 1
            blocks.append((start, end, path, array))
        return lines, blocks

    def _drop(self, keep) -> int:
        """Remove the blocks keep(path, array, body) says no to; how many."""
        lines, blocks = self._blocks()
        dropped, out, pos = 0, [], 0
        for start, end, path, array in blocks:
            try:
                body = loads("".join(lines[start + 1:end]))
            except DecodeError:
                body = None                      # not ours to judge: kept below
            if body is None or keep(path, array, body):
                continue
            out.extend(lines[pos:start])
            pos = end
            # The blank line that separated it goes too.
            while pos < len(lines) and not lines[pos].strip():
                pos += 1
            dropped += 1
        out.extend(lines[pos:])
        self.text = "".join(out)
        return dropped

    def remove_entries(self, path: tuple, **match) -> int:
        """Remove the [[path]] entries whose keys equal all of ``match``."""
        return self._drop(lambda p, array, body: not (
            array and p == path and all(body.get(k) == v for k, v in match.items())))

    def remove_table(self, path: tuple) -> int:
        return self._drop(lambda p, array, body: array or p != path)

    def append(self, block: str) -> None:
        self.text = self.text.rstrip("\n") + ("\n\n" if self.text.strip() else "") + block

    def defined_inline(self, path: tuple) -> bool:
        """True when ``path`` holds a value that no [path] / [[path]] header
        of its own defines (``workstations = []``, ``"g$" = {...}``): an
        entry added as a table would clash with it."""
        node = self.data
        for k in path:
            if not isinstance(node, dict) or k not in node:
                return False
            node = node[k]
        lines, blocks = self._blocks()
        return not any(b[2] == path for b in blocks)

    def add_entry(self, path: tuple, body: dict) -> None:
        """A [[path]] entry, right after the last one there is (else at the
        end); comments that head the next table stay with it."""
        lines, blocks = self._blocks()
        same = [b for b in blocks if b[2] == path and b[3]]
        block = table_text(path, body, array=True)
        if same and same[-1][1] < len(lines):
            at = same[-1][1]
            self.text = ("".join(lines[:at]).rstrip("\n") + "\n\n" + block + "\n"
                         + "".join(lines[at:]).lstrip("\n"))
        else:
            self.append(block)

    def insert_key(self, path: tuple, key: str, val) -> None:
        """``key = val`` as the first line of the existing [path] table, or a
        new [path] table holding it at the end."""
        lines, blocks = self._blocks()
        old = [b for b in blocks if b[2] == path and not b[3]]
        line = f"{quoted_key(key)} = {value(val)}\n"
        if not old:
            self.append(header(path) + "\n" + line)
            return
        at = old[0][0] + 1
        self.text = "".join(lines[:at]) + line + "".join(lines[at:])

    def set_table(self, path: tuple, body: dict) -> None:
        """Replace [path] where it is, with the keys given (removed when body
        is empty); a new table goes at the end."""
        lines, blocks = self._blocks()
        old = [b for b in blocks if b[2] == path and not b[3]]
        if not old:
            if body:
                self.append(table_text(path, body))
            return
        start, end = old[0][0], old[0][1]
        rest = lines[end:]
        while rest and not rest[0].strip():
            rest = rest[1:]
        block = (table_text(path, body) + ("\n" if rest else "")) if body else ""
        self.text = "".join(lines[:start]) + block + "".join(rest)

    def save(self, check, stamp: str | None = None) -> Path | None:
        """Write the edit if it parses and check(data) holds; the backup's path
        (None for a new file). Raises ValueError, writing nothing, otherwise."""
        try:
            data = loads(self.text)
        except DecodeError as e:
            raise ValueError(f"{self.path} would not be valid TOML ({e})") from e
        if not check(data):
            raise ValueError(f"{self.path} would not read back as intended")
        backup = None
        mode = 0o600
        if self.path.exists():
            mode = self.path.stat().st_mode & 0o777
            stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = self.path.with_name(f"{self.path.name}.bak-lab-{stamp}")
            n = 1
            while backup.exists():               # two edits in one second
                n += 1
                backup = self.path.with_name(f"{self.path.name}.bak-lab-{stamp}-{n}")
            shutil.copy2(self.path, backup)
        # The new file keeps the old one's permissions (it may hold passwords).
        tmp = self.path.with_name(self.path.name + ".new")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.text)
        os.chmod(tmp, mode)
        os.replace(tmp, self.path)
        return backup

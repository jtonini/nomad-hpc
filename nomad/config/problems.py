# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What is wrong with a config file nomad can't read, said so a person can fix it.

A TOML parser says "Cannot overwrite a value (at line 211, column 9)". What
the person needs is "line 211: `leads` is set twice in [console.labs] (first
on line 209); keep one of them" -- the file, the line, the earlier line it
clashes with, and what to do. Nothing here prints a value from the file:
the reason names keys and tables only, so it can go in a log, a mail from
cron or a banner in the Console. The line itself is kept apart (``excerpt``)
for the file's owner at a terminal, with secret-looking values hidden.
"""
from __future__ import annotations

import re

# Where the parsers put the position: tomllib (3.11+) and the toml package (3.10).
_AT_TOMLLIB = re.compile(r"\s*\(at line (\d+), column (\d+)\)")
_AT_END = re.compile(r"\s*\(at end of document\)")
_AT_TOML = re.compile(r"\s*\(line (\d+) column (\d+) char \d+\)")
_SECRET = re.compile(r"(token|secret|passw|pwd|_pw\b|\bpw\b|apikey|api_key|private|credential"
                     r"|webhook)", re.I)
_QUOTED = re.compile("'[^']{4,}'|\"[^\"]{4,}\"")      # quoted text in a message
# Lines longer than this are not taken apart (only shown, cut short), and a
# table header longer than _MAX_HEADER is not one: the work stays small
# whatever is in the file.
_MAX_LINE = 1000
_MAX_HEADER = 200

# Messages that mean "this was already defined".
_DUPLICATE = ("Cannot overwrite a value", "Duplicate keys!", "Cannot declare", "already exists?")


class ConfigError(ValueError):
    """A config file that exists but can't be read.

    ``path``; ``line`` and ``column`` (1-based, None when the parser didn't
    say); ``reason``, in words, naming keys and tables but no values;
    ``earlier``, the line it clashes with when something is defined twice;
    ``excerpt``, [(line number, text)] of those lines, secrets hidden.
    str() is "PATH, line N: REASON"; ``short`` is the same without the path.
    """

    def __init__(self, path, reason, line=None, column=None, earlier=None, excerpt=()):
        self.path = str(path)
        self.reason = reason
        self.line = line
        self.column = column
        self.earlier = earlier
        self.excerpt = list(excerpt)
        super().__init__(str(self))

    @property
    def short(self) -> str:
        return (f"line {self.line}: " if self.line else "") + self.reason

    def __str__(self) -> str:
        return f"{self.path}, {self.short}" if self.line else f"{self.path}: {self.reason}"


def _position(exc) -> tuple[int | None, int | None, str]:
    """(line, column, message without the position) from either parser's error."""
    text = str(exc)
    line, column = getattr(exc, "lineno", None), getattr(exc, "colno", None)
    message = getattr(exc, "msg", None) or text
    if line is None:
        m = _AT_TOMLLIB.search(text) or _AT_TOML.search(text)
        if m:
            line, column = int(m.group(1)), int(m.group(2))
    message = _AT_TOML.sub("", _AT_TOMLLIB.sub("", message))
    if _AT_END.search(message):
        message = _AT_END.sub("", message) + " at the end of the file"
    # The toml package (Python 3.10) repeats values from the file: what is
    # already there ("What? x already exists?{'x': {...}}"), and Python's own
    # conversion errors for an unquoted value ("could not convert string to
    # float: 'the-password'"). None of that is repeated here.
    message = re.sub(r"\?\{.*", "?", message, flags=re.S)
    if message.startswith(("could not convert", "invalid literal")):
        message = "this value is not valid TOML (text needs quotes)"
    message = _QUOTED.sub("'…'", message)
    return line, column, message.strip()


def _header(text: str):
    """(raw name, key path, is an array table) when the line is a table header."""
    from nomad.config.edit import _HEADER, key_path
    if len(text) > _MAX_HEADER or not text.lstrip().startswith("[") or "]" not in text:
        return None
    m = _HEADER.match(text)
    if not m:
        return None
    return m.group(2), key_path(m.group(2)), m.group(1) == "[["


def _before_equals(text: str) -> str | None:
    """What comes before the first = outside quotes (None: no =, or a comment
    first). One pass over the line: nothing here can take long."""
    quote, i = None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return None
        elif ch == "=":
            return text[:i]
        i += 1
    return None


def _key(text: str):
    """(raw key, key path) when the line sets a key."""
    from nomad.config.edit import key_path
    if len(text) > _MAX_LINE or _header(text):
        return None
    left = _before_equals(text)
    raw = left.strip() if left else ""
    path = key_path(raw) if raw else ()
    return (raw, path) if path else None


def _shown(text: str) -> str:
    """The line as it may be shown: on a line that mentions anything
    secret-looking, everything after the first = is hidden; a password in a
    URL always is. Long lines are cut short."""
    text = text.rstrip("\r\n")
    if _SECRET.search(text):
        left = _before_equals(text)
        text = (left.rstrip() + " = …") if left is not None else "…"
    text = re.sub(r"://[^/\s@]+@", "://…@", text)
    return text if len(text) <= 200 else text[:200] + " …"


def _table_at(lines: list[str], index: int) -> tuple[int, str | None]:
    """(index of the header governing lines[index], its raw name); (-1, None) at the top."""
    for i in range(index - 1, -1, -1):
        h = _header(lines[i])
        if h:
            return i, h[0]
    return -1, None


def describe(path, text: str, exc) -> ConfigError:
    """A ConfigError for ``exc``, raised by a TOML parser reading ``text`` from ``path``."""
    line, column, message = _position(exc)
    # Lines as the parsers count them: at "\n" only (splitlines() would also
    # split at characters TOML allows inside strings).
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    if not line or not 1 <= line <= len(lines):
        return ConfigError(path, message or "is not valid TOML", line, column)
    at = line - 1
    here = lines[at]
    duplicate = any(d in message for d in _DUPLICATE)
    excerpt = [(line, _shown(here))]

    h = _header(here)
    if duplicate and h and not h[2]:
        name, keys, _ = h
        for i in range(at - 1, -1, -1):
            other = _header(lines[i])
            if other and not other[2] and other[1] == keys:
                return ConfigError(
                    path, f"[{name}] appears twice (first on line {i + 1}); put what is "
                    "under both in one of them", line, column, i + 1,
                    [(i + 1, _shown(lines[i]))] + excerpt)
        return ConfigError(path, f"[{name}] is already defined above (as a table or a "
                           "value); keep one of them", line, column, excerpt=excerpt)

    k = _key(here)
    if duplicate and k:
        raw, keys = k
        start, table = _table_at(lines, at)
        where = f"[{table}]" if table else "the top of the file (before any [table])"
        for i in range(at - 1, start, -1):
            other = _key(lines[i])
            if other and other[1] == keys:
                return ConfigError(
                    path, f"`{raw}` is set twice in {where} (first on line {i + 1}); keep one "
                    "of them", line, column, i + 1, [(i + 1, _shown(lines[i]))] + excerpt)
        return ConfigError(path, f"`{raw}` is already set in {where}; keep one of them",
                           line, column, excerpt=excerpt)

    return ConfigError(path, f"{message}", line, column, excerpt=excerpt)


def describe_safely(path, text: str, exc) -> ConfigError:
    """describe(), or the parser's own words and line if describing fails:
    an error in here must never hide the one it is about."""
    try:
        return describe(path, text, exc)
    except Exception:
        try:
            line, column, message = _position(exc)
            return ConfigError(path, message or "is not valid TOML", line, column)
        except Exception:
            return ConfigError(path, "is not valid TOML")


def unreadable(path, exc: OSError | UnicodeDecodeError) -> ConfigError:
    """A ConfigError for a file that can't be opened or isn't text."""
    if isinstance(exc, UnicodeDecodeError):
        return ConfigError(path, f"is not UTF-8 text (byte {exc.start})")
    return ConfigError(path, f"can't be opened: {exc.strerror or exc}")


def excerpt_lines(err: ConfigError) -> list[str]:
    """The lines to show under the message, numbered, the failing one marked."""
    if not err.excerpt:
        return []
    width = len(str(max(n for n, _ in err.excerpt)))
    out = []
    for n, text in err.excerpt:
        mark = ">" if n == err.line else " "
        out.append(f"  {mark} {n:>{width}} | {text}")
    return out


__all__ = ["ConfigError", "describe", "describe_safely", "unreadable", "excerpt_lines"]

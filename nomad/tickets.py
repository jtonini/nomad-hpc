# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Request numbers, as a ticket system gives them (1.44.0).

Everything a PI asks Research Computing for through nomad, and every case
Research Computing opens with someone (data unchanged for a year, say), gets
one number from one sequence, shown as ``ARC-0012``: on the PI's card, in
each email's subject, on the Admin page, in the log and in the reports. A
reply to an email keeps its subject, so the number comes back with it.

The sequence is kept in lab_check.db (beside combined.db, the nomad
account's), in ``tickets``: the number, what kind of thing it is, and when it
was taken. Account requests made before 1.44.0 keep their number: request
#12 is ARC-0012.

Settings, in the hub's nomad.toml::

    [console.tickets]
    prefix = "ARC"        # letters and digits, at most 10: ARC-0012
    digits = 4            # the number's width, with leading zeros (3 to 8); a larger number just grows
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

KINDS = {"accounts": "Account request", "data": "Data case"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    number INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, at TEXT NOT NULL);
"""
_PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9]{0,9}")


@dataclass(frozen=True)
class Settings:
    prefix: str = "ARC"
    digits: int = 4
    problems: tuple = field(default_factory=tuple)


def settings(config: dict | None) -> Settings:
    console = (config or {}).get("console") or {}
    raw = console.get("tickets") if isinstance(console.get("tickets"), dict) else {}
    problems = []
    prefix = raw.get("prefix", "ARC")
    if not (isinstance(prefix, str) and _PREFIX.fullmatch(prefix)):
        problems.append("[console.tickets] prefix should be letters and digits, at most 10, starting with a letter")
        prefix = "ARC"
    digits = raw.get("digits", 4)
    if isinstance(digits, bool) or not isinstance(digits, int) or not 3 <= digits <= 8:
        problems.append("[console.tickets] digits should be a whole number from 3 to 8")
        digits = 4
    return Settings(prefix=prefix.upper(), digits=digits, problems=tuple(problems))


def ref(number, s: Settings | None = None) -> str:
    """ARC-0012."""
    s = s or Settings()
    return f"{s.prefix}-{int(number):0{s.digits}d}"


def number_of(text, s: Settings | None = None) -> int | None:
    """The number in ARC-0012, arc-12, #12 or 12; None if it isn't one
    (another prefix included: it is another place's number)."""
    s = s or Settings()
    t = str(text if text is not None else "").strip()
    if t.startswith("#"):
        t = t[1:]
    elif t.upper().startswith(s.prefix):
        t = t[len(s.prefix):].lstrip(" -")
    if not re.fullmatch(r"\d{1,9}", t):
        return None
    n = int(t)
    return n if n >= 1 else None


def ensure(rec) -> None:
    """The sequence, with every account request already made in it (so a
    request made by an older nomad keeps its number, and no number is given
    twice). Called whenever lab_check.db is opened to write."""
    rec.executescript(SCHEMA)
    try:
        with rec:
            rec.execute("INSERT OR IGNORE INTO tickets (number, kind, at) "
                        "SELECT id, 'accounts', at FROM account_requests")
    except Exception as exc:          # noqa: BLE001 -- no account_requests table yet: nothing to carry over
        if "no such table" not in str(exc):
            raise


def take(rec, kind: str) -> int:
    """The next number, for ``kind``, inside the caller's transaction (so it
    is given back if what it numbers isn't kept)."""
    if kind not in KINDS:
        raise ValueError(f"{kind!r}: not a kind of request")
    cur = rec.execute("INSERT INTO tickets (kind, at) VALUES (?, ?)",
                      (kind, datetime.now().isoformat(timespec="seconds")))
    return int(cur.lastrowid)


def kind_of(rec, number: int) -> str | None:
    try:
        got = rec.execute("SELECT kind FROM tickets WHERE number = ?", (int(number),)).fetchone()
    except Exception:                 # noqa: BLE001 -- no sequence yet
        return None
    return got[0] if got else None


__all__ = ["KINDS", "Settings", "settings", "ref", "number_of", "ensure", "take", "kind_of"]

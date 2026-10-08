# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""How numbers read in the report."""
from __future__ import annotations

import math
from datetime import datetime, timedelta

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August",
               "September", "October", "November", "December"]


def missing(x) -> bool:
    return x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))


def num(x, digits: int = 0) -> str:
    if missing(x):
        return "–"
    return f"{x:,.{digits}f}"


def pct(x, digits: int = 0) -> str:
    """A share (0.584) as a percentage ('58%')."""
    if missing(x):
        return "–"
    v = 100 * x
    if 0 < abs(v) < 0.5 and digits == 0:
        return "<1%" if v > 0 else ">-1%"
    return f"{v:.{digits}f}%"


def millions(x, digits: int = 2) -> str:
    if missing(x):
        return "–"
    return f"{x / 1e6:.{digits}f} M"


def thousands(x, digits: int = 1) -> str:
    if missing(x):
        return "–"
    return f"{x / 1e3:,.{digits}f}"


def big(x) -> str:
    """Core-hours and the like: 4.87 M, 213.4 K, 950."""
    if missing(x):
        return "–"
    if abs(x) >= 1e6:
        return f"{x / 1e6:.2f} M"
    if abs(x) >= 1e4:
        return f"{x / 1e3:,.1f} K"
    return f"{x:,.0f}"


def tb(b, digits: int = 1) -> str:
    """Bytes in decimal terabytes."""
    if missing(b):
        return "–"
    return f"{b / 1e12:,.{digits}f} TB"


def gb(x, digits: int = 1) -> str:
    if missing(x):
        return "–"
    if x >= 100:
        return f"{x:,.0f} GB"
    return f"{x:,.{digits}f} GB"


def span(lo, hi, fn=pct) -> str:
    """'31–68%' from two shares; one value when they are equal."""
    if missing(lo) or missing(hi):
        return "–"
    a, b = fn(lo), fn(hi)
    if a == b:
        return a
    if fn is pct and a.endswith("%") and b.endswith("%"):
        return f"{a[:-1]}–{b}"
    return f"{a}–{b}"


def month(key: str, long: bool = False) -> str:
    """'2026-03' -> 'Mar 2026' (or 'March 2026')."""
    try:
        y, m = int(key[:4]), int(key[5:7])
    except (TypeError, ValueError):
        return str(key)
    return f"{(MONTHS_LONG if long else MONTHS)[m - 1]} {y}"


def day(t: datetime) -> str:
    return f"{t.day} {MONTHS[t.month - 1]} {t.year}"


def period(t0: datetime, t1: datetime) -> str:
    """[t0, t1) in words: '1 Oct 2025 – 6 Oct 2026' (the last whole day)."""
    last = t1 - timedelta(seconds=1)
    return f"{day(t0)} – {day(last)}"


def plural(n, word: str, words: str | None = None) -> str:
    return f"{num(n)} {word if n == 1 else (words or word + 's')}"

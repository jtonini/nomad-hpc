# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Facts: every number of the report with what it is and where it came from.

A fact's id is stable from one run to the next ("s01.institutional_core_hours",
"s04.waiting_share.2026-03"), so two reports can be compared figure by
figure, and anything built later (a PI-scope report, a chat over the facts)
reads the same values the report printed.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

MEASURED, ESTIMATED, PROJECTED, ASSUMED = "measured", "estimated", "projected", "assumed"
# A section's state.
OK, PARTIAL, NOT_MEASURED = "measured", "partial", "not measured"


@dataclass
class Fact:
    id: str
    label: str
    value: Any
    unit: str = ""
    period: str = ""
    source: str = ""
    kind: str = MEASURED
    n: int | None = None
    note: str = ""
    # Counts of people below the report's min_cell print as "fewer than N".
    people: bool = False
    suppressed: bool = False


@dataclass
class Range:
    """A lowest–highest pair in a table cell."""
    lo: Any
    hi: Any


@dataclass
class Table:
    title: str
    columns: list[str]
    rows: list[list[Any]]
    note: str = ""
    # Columns holding counts of people (an int, or a tuple "a of b"), which
    # min_cell may hide.
    people_columns: list[int] = field(default_factory=list)


@dataclass
class Section:
    number: int
    key: str
    title: str
    finding: str = ""
    status: str = OK
    facts: list[Fact] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    source: str = ""
    period: str = ""

    def fact(self, key: str, label: str, value, unit: str = "", **kw) -> Fact:
        f = Fact(id=f"s{self.number:02d}.{key}", label=label, value=_clean(value), unit=unit,
                 period=kw.pop("period", self.period), source=kw.pop("source", self.source), **kw)
        self.facts.append(f)
        return f

    def get(self, key: str):
        fid = f"s{self.number:02d}.{key}"
        for f in self.facts:
            if f.id == fid:
                return f.value
        return None


@dataclass
class Coverage:
    section: str
    source: str
    covered: str
    gaps: str
    kind: str


@dataclass
class SetAside:
    what: str
    count: int
    note: str = ""


@dataclass
class Report:
    cluster: str
    period_from: str
    period_to: str
    produced: str
    nomad_version: str
    source: str
    sections: list[Section] = field(default_factory=list)
    coverage: list[Coverage] = field(default_factory=list)
    set_aside: list[SetAside] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    min_cell: int = 0

    def facts(self) -> list[Fact]:
        return [f for s in self.sections for f in s.facts]

    def to_dict(self) -> dict:
        return {
            "report": {"cluster": self.cluster, "from": self.period_from, "to": self.period_to,
                       "produced": self.produced, "nomad_version": self.nomad_version,
                       "source": self.source, "min_cell": self.min_cell},
            "summary": [{"section": s.number, "title": s.title, "finding": s.finding}
                        for s in self.sections if s.finding],
            "facts": [_fact_dict(f) for f in self.facts()],
            "sections": [{"number": s.number, "key": s.key, "title": s.title, "status": s.status,
                          "finding": s.finding, "source": s.source, "period": s.period,
                          "notes": s.notes,
                          "tables": [asdict(t) for t in s.tables]} for s in self.sections],
            "coverage": [asdict(c) for c in self.coverage],
            "set_aside": [asdict(a) for a in self.set_aside],
            "assumptions": self.assumptions,
        }


def _fact_dict(f: Fact) -> dict:
    d = asdict(f)
    if f.suppressed:
        d["value"] = None
    d.pop("people")
    return d


def _clean(v):
    """NaN and infinity are not JSON: None stands for 'no value'."""
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    return v


def apply_min_cell(report: Report, n: int) -> None:
    """Counts of people below ``n`` become "fewer than n" (wider audiences:
    "1 person in department X" names someone)."""
    report.min_cell = n
    if n <= 0:
        return
    for f in report.facts():
        if f.people and isinstance(f.value, (int, float)) and 0 < f.value < n:
            f.suppressed = True
            f.note = (f.note + "; " if f.note else "") + f"fewer than {n}"
    hidden = f"fewer than {n}"

    def cell(v):
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)) and 0 < v < n:
            return hidden
        if isinstance(v, tuple):
            return tuple(cell(x) for x in v)
        if isinstance(v, Range):
            return Range(cell(v.lo), cell(v.hi))
        return v
    for s in report.sections:
        for t in s.tables:
            for row in t.rows:
                for i in t.people_columns:
                    if i < len(row):
                        row[i] = cell(row[i])

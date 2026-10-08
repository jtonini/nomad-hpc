# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The report as Markdown and as JSON."""
from __future__ import annotations

import json

from nomad.usage import fmt
from nomad.usage.facts import NOT_MEASURED, Range, Report, Table


def _cell(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, Range):
        lo, hi = _cell(v.lo), _cell(v.hi)
        return lo if lo == hi else f"{lo}–{hi}"
    if isinstance(v, tuple):
        return " / ".join(_cell(x) for x in v)
    if isinstance(v, int):
        return fmt.num(v)
    if isinstance(v, float):
        return fmt.num(v, 1)
    return str(v).replace("|", "\\|").replace("\n", " ")


def _table(t: Table, source: str, period: str) -> list[str]:
    out = [f"**{t.title}**", ""]
    out.append("| " + " | ".join(_cell(c) for c in t.columns) + " |")
    out.append("|" + "|".join("---" for _ in t.columns) + "|")
    for row in t.rows:
        cells = [_cell(c) for c in row] + [""] * (len(t.columns) - len(row))
        out.append("| " + " | ".join(cells[:len(t.columns)]) + " |")
    caption = " ".join(x for x in (t.note, f"Source: {source}." if source else "",
                                   f"Period: {period}." if period else "") if x)
    if caption:
        out += ["", f"*{caption}*"]
    out.append("")
    return out


def markdown(r: Report) -> str:
    from datetime import datetime
    t0 = datetime.fromisoformat(r.period_from)
    t1 = datetime.fromisoformat(r.period_to)
    out = [f"# {r.cluster}: usage report, {fmt.period(t0, t1)}", "",
           f"Produced {r.produced} by NØMAÐ {r.nomad_version} from {r.source}. "
           "Aggregates only: no person, lab or job is named.", ""]
    if r.min_cell:
        out += [f"Counts of fewer than {r.min_cell} people are shown as \"fewer than {r.min_cell}\".", ""]
    out += ["## Summary", ""]
    for s in r.sections:
        out.append(f"- **{s.number}. {s.title}.** {s.finding}")
    out.append("")
    for s in r.sections:
        out += [f"## {s.number}. {s.title}", ""]
        if s.finding:
            out += [s.finding, ""]
        if s.status == NOT_MEASURED:
            continue
        for t in s.tables:
            out += _table(t, s.source, s.period)
        for n in s.notes:
            out += [f"- {n}"]
        if s.notes:
            out.append("")
    out += ["## Coverage and sources", ""]
    out += _table(Table("Where each section's figures come from",
                        ["Section", "Source", "Covered", "Gaps and limits", "Kind"],
                        [[c.section, c.source, c.covered, c.gaps, c.kind] for c in r.coverage]), "", "")
    out += ["## What was set aside", ""]
    if r.set_aside:
        out += _table(Table("Left out of the figures, with counts", ["What", "How many", "Note"],
                            [[a.what, a.count, a.note] for a in r.set_aside]), "", "")
    else:
        out += ["Nothing.", ""]
    out += ["## Assumptions", ""]
    out += [f"- {a}" for a in r.assumptions]
    out.append("")
    return "\n".join(out)


def to_json(r: Report) -> str:
    return json.dumps(r.to_dict(), indent=1, ensure_ascii=False, default=str) + "\n"

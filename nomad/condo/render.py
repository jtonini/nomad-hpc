# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The blocks of nomad.condo.content as an email's HTML and as plain text.

The HTML is for mail readers: tables for layout, every style inline, bars
drawn as table cells, no images and no scripts, and readable with styles
stripped."""
from __future__ import annotations

from html import escape

from nomad.condo import content as C

INK = "#0f172a"
MUTED = "#475569"
LINE = "#e2e8f0"
FONT = "font-family:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _bar_cells(segments, height=14) -> str:
    cells = "".join(
        f'<td width="{max(p, 0.5):.1f}%" style="background:{color};height:{height}px;font-size:0;'
        f'line-height:0" title="{escape(label)} {p:.0f}%">&nbsp;</td>'
        for label, p, color in segments if p and p > 0.05)
    if not cells:
        cells = f'<td style="background:{LINE};height:{height}px;font-size:0;line-height:0">&nbsp;</td>'
    return (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="border-collapse:collapse;border-radius:3px;overflow:hidden"><tr>{cells}</tr></table>')


def _legend(items) -> str:
    return " &nbsp; ".join(
        f'<span style="display:inline-block;width:9px;height:9px;background:{c};border-radius:2px">'
        f'</span> {escape(label)}' for label, c in items)


def _masthead(b, logo_src: str | None) -> str:
    """The institution's logo and lines, as on the Console's sign-in page."""
    lines = b.masthead
    words = "".join(
        f'<div style="font-size:{14 if i == 0 else 12}px;font-weight:{600 if i == 0 else 400};'
        f'color:{INK if i == 0 else MUTED};line-height:1.35">{escape(t)}</div>'
        for i, t in enumerate(lines))
    if not (b.logo and logo_src):
        return (f'<div style="font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:{MUTED}">'
                f'{escape(" · ".join(lines))}</div>')
    alt = escape(lines[0] if lines else "logo")
    return (f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
            f'<td valign="middle" style="padding-right:16px"><img src="{escape(logo_src)}" alt="{alt}" '
            f'height="56" style="display:block;height:56px;width:auto;border:0"></td>'
            f'<td valign="middle">{words}</td></tr></table>')


def html(doc: list, logo_src: str | None = None) -> str:
    """``logo_src``: where the logo is (a data: URI in a file, cid: in an
    email); without it, no logo."""
    title = next((b for b in doc if isinstance(b, C.Title)), None)
    body = []
    for b in doc:
        if isinstance(b, C.Title):
            body.append(
                '<tr><td style="padding:24px 28px 8px 28px">'
                + _masthead(b, logo_src) +
                f'<div style="font-size:22px;font-weight:600;color:{INK};margin-top:14px">{escape(b.title)}</div>'
                f'<div style="font-size:14px;color:{MUTED};margin-top:4px">{escape(b.subtitle)} &nbsp;'
                f'<span style="display:inline-block;border:1px solid {OWNER_SOFT};color:{C.OWNER};'
                f'border-radius:999px;padding:1px 9px;font-size:12px">{escape(b.badge)}</span></div>'
                f'</td></tr>')
        elif isinstance(b, C.H):
            body.append(f'<tr><td style="padding:22px 28px 6px 28px;font-size:16px;font-weight:600;'
                        f'color:{INK};border-top:1px solid {LINE}">{escape(b.text)}</td></tr>')
        elif isinstance(b, C.P):
            color = MUTED if b.muted else INK
            body.append(f'<tr><td style="padding:6px 28px;font-size:14px;line-height:1.5;color:{color}">'
                        f'{escape(b.text)}</td></tr>')
        elif isinstance(b, C.Bar):
            body.append(f'<tr><td style="padding:8px 28px 2px 28px">{_bar_cells(b.segments, 18)}'
                        f'<div style="font-size:12px;color:{MUTED};margin-top:5px">{escape(b.caption)}</div>'
                        f'</td></tr>')
        elif isinstance(b, C.Tiles):
            w = int(100 / max(1, len(b.tiles)))
            tiles = "".join(
                f'<td width="{w}%" valign="top" style="padding:6px">'
                f'<div style="border:1px solid {LINE};border-top:3px solid {color};border-radius:6px;'
                f'padding:10px 12px"><div style="font-size:12px;color:{MUTED}">{escape(label)}</div>'
                f'<div style="font-size:22px;font-weight:600;color:{INK}">{escape(value)}</div>'
                f'<div style="font-size:12px;color:{MUTED}">{escape(sub)}</div></div></td>'
                for label, value, sub, color in b.tiles)
            body.append(f'<tr><td style="padding:4px 22px"><table role="presentation" width="100%" '
                        f'cellpadding="0" cellspacing="0"><tr>{tiles}</tr></table></td></tr>')
        elif isinstance(b, C.Table):
            align = (b.align or "l" * len(b.headers)).ljust(len(b.headers), "r")
            head = "".join(
                f'<th align="{"left" if a == "l" else "right"}" style="font-size:12px;font-weight:600;'
                f'color:{MUTED};padding:6px 8px;border-bottom:1px solid {LINE}">{escape(h)}</th>'
                for h, a in zip(b.headers, align))
            rows = "".join(
                "<tr>" + "".join(
                    f'<td align="{"left" if a == "l" else "right"}" style="font-size:13px;color:{INK};'
                    f'padding:5px 8px;border-bottom:1px solid {LINE};white-space:nowrap">{escape(str(v))}</td>'
                    for v, a in zip(r, align)) + "</tr>" for r in b.rows)
            note = (f'<div style="font-size:12px;color:{MUTED};margin-top:6px">{escape(b.note)}</div>'
                    if b.note else "")
            body.append(f'<tr><td style="padding:6px 28px"><div style="overflow-x:auto">'
                        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                        f'style="border-collapse:collapse"><tr>{head}</tr>{rows}</table></div>{note}</td></tr>')
        elif isinstance(b, C.Bars):
            rows = "".join(
                f'<tr><td width="18%" style="font-size:12px;color:{INK};padding:3px 10px 3px 0;'
                f'white-space:nowrap">{escape(label)}</td>'
                f'<td width="47%" style="padding:3px 0">{_bar_cells(segs, 12)}</td>'
                f'<td width="35%" style="font-size:12px;color:{MUTED};padding:3px 0 3px 10px;'
                f'white-space:nowrap">{escape(text)}</td></tr>' for label, segs, text in b.rows)
            legend = (f'<div style="font-size:12px;color:{MUTED};margin-top:6px">{_legend(b.legend)}</div>'
                      if b.legend else "")
            body.append(f'<tr><td style="padding:6px 28px"><table role="presentation" width="100%" '
                        f'cellpadding="0" cellspacing="0" style="table-layout:fixed">{rows}</table>'
                        f'{legend}</td></tr>')
        elif isinstance(b, C.Foot):
            lines = "".join(f'<div style="margin-top:4px">{escape(x)}</div>' for x in b.lines)
            body.append(f'<tr><td style="padding:18px 28px 24px 28px;font-size:12px;line-height:1.5;'
                        f'color:{MUTED};border-top:1px solid {LINE}">{lines}</td></tr>')
    name = escape(title.title + ": " + title.subtitle) if title else "Report"
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{name}</title></head>'
        f'<body style="margin:0;padding:0;background:#f1f5f9;{FONT}">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f1f5f9">'
        '<tr><td align="center" style="padding:20px 8px">'
        f'<table role="presentation" width="680" cellpadding="0" cellspacing="0" style="max-width:680px;'
        f'width:100%;background:#ffffff;border:1px solid {LINE};border-radius:8px;{FONT}">'
        + "".join(body) +
        '</table></td></tr></table></body></html>\n')


OWNER_SOFT = "#bfdbfe"


def text(doc: list) -> str:
    """Plain text: the email's other part, and the md format."""
    out = []
    for b in doc:
        if isinstance(b, C.Title):
            out += [f"# {b.title}", "", f"{b.subtitle} ({b.badge}). {'; '.join(b.masthead)}.", ""]
        elif isinstance(b, C.H):
            out += [f"## {b.text}", ""]
        elif isinstance(b, C.P):
            out += [b.text, ""]
        elif isinstance(b, C.Bar):
            out += ["  " + ", ".join(f"{label} {C.pct(p)}" for label, p, _ in b.segments)
                    + (f"  ({b.caption})" if b.caption else ""), ""]
        elif isinstance(b, C.Tiles):
            out += [f"- {label}: {value} ({sub})" for label, value, sub, _ in b.tiles] + [""]
        elif isinstance(b, C.Table):
            rows = [b.headers] + [[str(v) for v in r] for r in b.rows]
            widths = [max(len(str(r[i])) for r in rows) for i in range(len(b.headers))]
            align = (b.align or "l" * len(b.headers)).ljust(len(b.headers), "r")
            for k, r in enumerate(rows):
                out.append("| " + " | ".join(str(v).ljust(w) if a == "l" else str(v).rjust(w)
                                             for v, w, a in zip(r, widths, align)) + " |")
                if k == 0:
                    out.append("|" + "|".join(("-" * (w + 1) + ":") if a == "r" else ("-" * (w + 2))
                                              for w, a in zip(widths, align)) + "|")
            out += ([""] + [b.note] if b.note else []) + [""]
        elif isinstance(b, C.Bars):
            w = max((len(label) for label, _, _ in b.rows), default=0)
            for label, segs, txt in b.rows:
                blocks = "".join(ch * round(p / 5) for (_, p, _), ch in zip(segs, "#=.")) if segs else ""
                out.append(f"  {label.ljust(w)}  {blocks.ljust(20)}  {txt}")
            if b.legend:
                out.append("  " + "   ".join(f"{ch} {label}" for (label, _), ch in zip(b.legend, "#=.")))
            out.append("")
        elif isinstance(b, C.Foot):
            out += ["---"] + b.lines + [""]
    return "\n".join(out).rstrip() + "\n"

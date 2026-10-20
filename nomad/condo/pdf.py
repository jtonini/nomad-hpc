# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""The blocks of nomad.condo.content as a PDF, with fpdf2 (the optional
``report`` extra: pip install 'nomad-hpc[report]'). Without it there is no
PDF, and the email says so."""
from __future__ import annotations

from nomad.condo import content as C

INK = (15, 23, 42)
MUTED = (71, 85, 105)
LINE = (226, 232, 240)
_SWAP = {"–": "-", "—": "-", "‘": "'", "’": "'", "“": '"', "”": '"',
         "…": "...", " ": " ", "•": "-", "≤": "<=", "≥": ">="}


class PDFUnavailable(RuntimeError):
    """fpdf2 isn't installed."""


def available() -> bool:
    try:
        import fpdf  # noqa: F401
    except ImportError:
        return False
    return True


def _latin(text) -> str:
    """The built-in PDF fonts know Latin-1 only (Ø and Ð among them)."""
    s = "".join(_SWAP.get(ch, ch) for ch in str(text))
    return s.encode("latin-1", "replace").decode("latin-1")


def _rgb(hexcolor: str) -> tuple[int, int, int]:
    h = hexcolor.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def render(doc: list) -> bytes:
    try:
        from fpdf import FPDF
        from fpdf.enums import TableBordersLayout
        from fpdf.fonts import FontFace
    except ImportError as e:
        raise PDFUnavailable("fpdf2 is not installed (pip install 'nomad-hpc[report]')") from e

    pdf = FPDF(orientation="P", unit="mm", format="Letter")
    pdf.set_auto_page_break(True, margin=15)
    pdf.set_margins(16, 14, 16)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin
    title = next((b for b in doc if isinstance(b, C.Title)), None)
    if title:
        pdf.set_title(_latin(f"{title.title}: {title.subtitle}"))
        pdf.set_creator("NOMAD (nomad-hpc.com)")

    def text(size, color=INK, style=""):
        pdf.set_font("Helvetica", style, size)
        pdf.set_text_color(*color)

    def bar(x, y, w, h, segments):
        pdf.set_fill_color(*LINE)
        pdf.rect(x, y, w, h, style="F")
        at = x
        for _, p, color in segments:
            if not p or p <= 0.05:
                continue
            seg = w * min(p, 100) / 100.0
            pdf.set_fill_color(*_rgb(color))
            pdf.rect(at, y, min(seg, x + w - at), h, style="F")
            at += seg

    for b in doc:
        if isinstance(b, C.Title):
            text(8, MUTED)
            pdf.cell(width, 4, _latin(b.institution.upper()), new_x="LMARGIN", new_y="NEXT")
            text(17, INK, "B")
            pdf.multi_cell(width, 8, _latin(b.title), new_x="LMARGIN", new_y="NEXT")
            text(10, MUTED)
            pdf.cell(width, 5, _latin(f"{b.subtitle}   |   {b.badge}"), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        elif isinstance(b, C.H):
            if pdf.get_y() > pdf.h - 40:
                pdf.add_page()
            pdf.ln(3)
            pdf.set_draw_color(*LINE)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
            pdf.ln(2)
            text(12, INK, "B")
            pdf.cell(width, 6, _latin(b.text), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)
        elif isinstance(b, C.P):
            text(9.5, MUTED if b.muted else INK)
            pdf.multi_cell(width, 4.8, _latin(b.text), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1.5)
        elif isinstance(b, C.Bar):
            y = pdf.get_y() + 1
            bar(pdf.l_margin, y, width, 5, b.segments)
            pdf.set_y(y + 6.5)
            text(8, MUTED)
            pdf.cell(width, 4, _latin(b.caption), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(1)
        elif isinstance(b, C.Tiles):
            k = max(1, len(b.tiles))
            gap = 3
            w = (width - gap * (k - 1)) / k
            y = pdf.get_y()
            for i, (label, value, sub, color) in enumerate(b.tiles):
                x = pdf.l_margin + i * (w + gap)
                pdf.set_draw_color(*LINE)
                pdf.rect(x, y, w, 19)
                pdf.set_fill_color(*_rgb(color))
                pdf.rect(x, y, w, 1.2, style="F")
                pdf.set_xy(x + 3, y + 2.5)
                text(8, MUTED)
                pdf.cell(w - 6, 4, _latin(label))
                pdf.set_xy(x + 3, y + 6.5)
                text(14, INK, "B")
                pdf.cell(w - 6, 6, _latin(value))
                pdf.set_xy(x + 3, y + 13)
                text(7.5, MUTED)
                pdf.cell(w - 6, 4, _latin(sub))
            pdf.set_y(y + 21)
        elif isinstance(b, C.Table):
            align = (b.align or "l" * len(b.headers)).ljust(len(b.headers), "r")
            size = 8.0
            while True:
                # Each column as wide as its widest entry, headings in bold.
                pdf.set_font("Helvetica", "B", size)
                heads = [pdf.get_string_width(_latin(h)) for h in b.headers]
                pdf.set_font("Helvetica", "", size)
                weights = [max(hw, *(pdf.get_string_width(_latin(r[i])) for r in b.rows)) + 3.5
                           for i, hw in enumerate(heads)]
                if sum(weights) <= width or size <= 6:
                    break
                size -= 0.5
            text(size, INK)
            pdf.set_draw_color(*LINE)
            pdf.set_fill_color(255, 255, 255)
            with pdf.table(width=width, col_widths=weights, borders_layout=TableBordersLayout.HORIZONTAL_LINES,
                           text_align=tuple("LEFT" if a == "l" else "RIGHT" for a in align),
                           headings_style=FontFace(emphasis="BOLD", color=MUTED, fill_color=(248, 250, 252)),
                           cell_fill_color=(255, 255, 255), line_height=size * 0.58,
                           padding=1, align="LEFT") as t:
                row = t.row()
                for h in b.headers:
                    row.cell(_latin(h))
                for r in b.rows:
                    row = t.row()
                    for v in r:
                        row.cell(_latin(v))
            if b.note:
                pdf.ln(1)
                text(7.5, MUTED)
                pdf.multi_cell(width, 3.8, _latin(b.note), new_x="LMARGIN", new_y="NEXT")
            pdf.ln(2)
        elif isinstance(b, C.Bars):
            label_w, text_w = 26, 62
            for label, segs, txt in b.rows:
                if pdf.get_y() > pdf.h - 20:
                    pdf.add_page()
                y = pdf.get_y()
                text(8, INK)
                pdf.set_xy(pdf.l_margin, y)
                pdf.cell(label_w, 4.5, _latin(label))
                bar(pdf.l_margin + label_w, y + 0.8, width - label_w - text_w, 3, segs)
                pdf.set_xy(pdf.l_margin + width - text_w + 2, y)
                text(7.5, MUTED)
                pdf.cell(text_w - 2, 4.5, _latin(txt))
                pdf.set_y(y + 5)
            if b.legend:
                pdf.ln(1)
                x = pdf.l_margin
                y = pdf.get_y()
                text(7.5, MUTED)
                for label, color in b.legend:
                    pdf.set_fill_color(*_rgb(color))
                    pdf.rect(x, y + 1, 2.5, 2.5, style="F")
                    pdf.set_xy(x + 3.5, y)
                    w = pdf.get_string_width(_latin(label)) + 6
                    pdf.cell(w, 4.5, _latin(label))
                    x += w + 3.5
                pdf.set_y(y + 6)
        elif isinstance(b, C.Foot):
            pdf.ln(3)
            pdf.set_draw_color(*LINE)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + width, pdf.get_y())
            pdf.ln(2)
            text(7.5, MUTED)
            for line in b.lines:
                pdf.multi_cell(width, 3.8, _latin(line), new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())

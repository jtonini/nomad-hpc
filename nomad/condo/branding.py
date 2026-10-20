# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Who sends the condo reports, as the Console's sign-in page says it: the
institution, the unit lines under it and the logo, from [console.branding]
in nomad.toml.

    [console.branding]
    institution = "Example University"    # default: [support] institution
    unit = ["Research Computing", "Office of the Provost"]   # up to 3 lines
    logo = "/etc/nomad/branding/logo.png"

The logo goes on every report (the email, its PDF, the HTML file) when it is
a PNG or JPEG file of up to 1 MB at a full path; other kinds don't show in
every mail reader or in the PDF, and are left out with a warning. Turn it
all off with [report.labs] branding = false.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

MAX_LOGO = 1_000_000
MAX_TEXT = 120
MAX_UNITS = 3


@dataclass
class Brand:
    lines: list[str] = field(default_factory=list)    # institution, then its units
    logo: bytes | None = None
    logo_type: str | None = None                       # "png" or "jpeg"
    warnings: list[str] = field(default_factory=list)

    @property
    def institution(self) -> str | None:
        return self.lines[0] if self.lines else None


def _text(value) -> str:
    if not isinstance(value, str):
        return ""
    value = " ".join(value.split())
    return "".join(c for c in value if c.isprintable())[:MAX_TEXT]


def _kind(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return None


def read_logo(path: str) -> tuple[bytes | None, str | None, str | None]:
    """(bytes, kind, None), or (None, None, why not)."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        return None, None, f"logo {path!r}: give a full path"
    try:
        fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        return None, None, f"logo {p}: {e.strerror or type(e).__name__}"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_LOGO:
            return None, None, f"logo {p}: not a file of up to 1 MB"
        data = os.read(fd, MAX_LOGO + 1)
    except OSError as e:
        return None, None, f"logo {p}: {e.strerror or type(e).__name__}"
    finally:
        os.close(fd)
    kind = _kind(data)
    if kind is None:
        return None, None, f"logo {p}: not a PNG or JPEG (left out: mail readers and the PDF need one)"
    return data, kind, None


def read(nomad_config: dict | None, enabled: bool = True) -> Brand:
    brand = Brand()
    if not enabled:
        return brand
    cfg = nomad_config or {}
    console = cfg.get("console") if isinstance(cfg.get("console"), dict) else {}
    section = console.get("branding") if isinstance(console.get("branding"), dict) else {}
    support = cfg.get("support") if isinstance(cfg.get("support"), dict) else {}
    institution = _text(section.get("institution")) or _text(support.get("institution"))
    units = section.get("unit")
    units = units if isinstance(units, list) else [units]
    lines = [t for t in (_text(u) for u in units) if t][:MAX_UNITS]
    brand.lines = ([institution] if institution else []) + lines
    logo = section.get("logo")
    if isinstance(logo, str) and logo.strip():
        brand.logo, brand.logo_type, why = read_logo(logo.strip())
        if why:
            brand.warnings.append(why)
    return brand

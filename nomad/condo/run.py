# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Writing the condo reports and sending them.

Files: OUT/<cluster>/<condo>/<YYYY-MM>.{html,pdf,json,md} and
OUT/summary/<YYYY-MM>.{...}, readable only by the account that writes them
(an owner's report names their lab's members). Mail: through nomad.mail
([mail] in nomad.toml). While [report.labs] review_to is set, every report
goes there, marked with who it would have gone to. What was sent is kept in
OUT/summary/<YYYY-MM>.sent.json, so a second run doesn't mail the same
report twice (--resend does).
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from nomad.condo import content, pdf, render
from nomad.condo.config import Settings
from nomad.condo.figures import month_name


def default_out(settings: Settings, nomad_config: dict | None) -> Path:
    if settings.delivery.out_dir:
        return settings.delivery.out_dir
    default = Path.home() / ".local" / "share" / "nomad"
    data = Path(((nomad_config or {}).get("general") or {}).get("data_dir") or default).expanduser()
    return data / "reports" / "condos"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(name)).strip("._") or "_"


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _write(path: Path, data: bytes | str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(data.encode("utf-8") if isinstance(data, str) else data)
    finally:
        if fd is not None:
            os.close(fd)


class Built:
    """One report in every format asked for."""

    def __init__(self, key: str, doc: list, facts: dict, formats: list[str]):
        self.key, self.doc, self.facts = key, doc, facts
        self.html = render.html(doc)
        self.text = render.text(doc)
        self.pdf = None
        self.pdf_error = None
        if "pdf" in formats:
            try:
                self.pdf = pdf.render(doc)
            except pdf.PDFUnavailable as e:
                self.pdf_error = str(e)


def build_all(data: dict, settings: Settings, formats: list[str]) -> tuple[list[Built], Built]:
    owners = []
    for fig in data["reports"]:
        key = f"{fig['cluster']['name']}/{fig['condo']['name']}"
        owners.append(Built(key, content.owner_doc(fig, settings.delivery), fig, formats))
    summary = Built("summary", content.summary_doc(data, settings.delivery, settings.policy),
                    {k: v for k, v in data.items() if k != "reports"}, formats)
    return owners, summary


def write(out: Path, month: str, owners: list[Built], summary: Built | None, formats: list[str]) -> list[Path]:
    """Every report's files; ``summary`` None: none for the summary."""
    written = []
    _private_dir(out)
    for b in owners + ([summary] if summary is not None else []):
        parts = ["summary"] if b is summary else [_safe(p) for p in b.key.split("/")]
        folder = out
        for p in parts:
            folder = folder / p
            _private_dir(folder)
        for fmt in formats:
            path = folder / f"{month}.{fmt}"
            if fmt == "html":
                _write(path, b.html)
            elif fmt == "md":
                _write(path, b.text)
            elif fmt == "json":
                _write(path, json.dumps(b.facts, indent=2, default=str) + "\n")
            elif fmt == "pdf":
                if b.pdf is None:
                    continue
                _write(path, b.pdf)
            written.append(path)
    return written


# --- mail ------------------------------------------------------------------------

def _message(b: Built, subject: str, to: list[str], cc: list[str], sender: str | None,
             review_of: str | None, pdf_name: str) -> EmailMessage:
    doc = b.doc
    if review_of is not None:
        doc = list(doc)
        doc.insert(1, content.P(f"Review copy. Once review_to is cleared in report.toml, this goes to "
                                f"{review_of}.", muted=True))
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if sender:
        msg["From"] = sender
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain="nomad-hpc.com")
    note = "" if b.pdf is not None else (f"\n(No PDF: {b.pdf_error}.)\n" if b.pdf_error else "")
    msg.set_content(render.text(doc) + note)
    msg.add_alternative(render.html(doc), subtype="html")
    if b.pdf is not None:
        msg.add_attachment(b.pdf, maintype="application", subtype="pdf", filename=pdf_name)
    return msg


def plan_mail(owners: list[Built], summary: Built | None, settings: Settings, month: str) -> list[dict]:
    """Who each report goes to, or why it doesn't go: [{key, subject, to, cc,
    review_of, skip}]."""
    d = settings.delivery
    mname = month_name(month)
    out = []
    for b in owners:
        f = b.facts
        c = f["condo"]
        subject = f"{f['cluster']['label']}: {c['label']} condo, {mname}"
        cfg = settings.cluster(f["cluster"]["name"]).condos.get(c["name"])
        address = cfg.address(d) if cfg else None
        cc = list(cfg.cc) if cfg else []
        would = ", ".join([address] + cc) if address else None
        entry = {"key": b.key, "subject": subject, "to": [], "cc": [], "review_of": None, "skip": None}
        if d.reviewing:
            entry.update(to=list(d.review_to), review_of=would or "no one yet (no owner address)",
                         subject=f"[Review] {subject}")
        elif cfg is None or not c["confirmed"]:
            entry["skip"] = "owner not confirmed in report.toml"
        elif not address:
            entry["skip"] = "no address: set mail_domain, or email for the condo"
        else:
            entry.update(to=[address], cc=cc)
        out.append(entry)
    if summary is not None:
        to = settings.delivery.summary_recipients()
        out.append({"key": "summary", "subject": f"Condo summary, {mname}", "to": to, "cc": [],
                    "review_of": None, "skip": None if to else "no summary_to or review_to"})
    return out


def send(owners: list[Built], summary: Built | None, settings: Settings, nomad_config: dict | None,
         month: str, out: Path, resend: bool = False) -> list[dict]:
    from nomad import mail
    cfg = mail.settings(nomad_config)
    sender = settings.delivery.send_from or cfg.get("from")
    if not sender:
        raise ValueError("no sender: set send_from in [report.labs] or from in [mail]")
    log_path = out / "summary" / f"{month}.sent.json"
    try:
        log = json.loads(log_path.read_text())
    except (OSError, ValueError):
        log = {}
    by_key = {b.key: b for b in owners + ([summary] if summary is not None else [])}
    results = []
    for entry in plan_mail(owners, summary, settings, month):
        b = by_key[entry["key"]]
        done = log.get(entry["key"])
        if entry["skip"]:
            results.append({**entry, "status": "skipped"})
            continue
        if done and not resend and done.get("to") == entry["to"]:
            results.append({**entry, "status": "already sent", "when": done.get("when")})
            continue
        name = f"{_safe(b.key.replace('/', '-'))}-{month}.pdf"
        msg = _message(b, entry["subject"], entry["to"], entry["cc"], sender, entry["review_of"], name)
        try:
            mail.send(msg, cfg)
        except Exception as e:      # noqa: BLE001 -- one failure doesn't stop the others
            results.append({**entry, "status": "failed", "error": mail.failure_label(e)})
            continue
        log[entry["key"]] = {"to": entry["to"], "cc": entry["cc"], "when": datetime.now().isoformat(timespec="seconds"),
                             "review": entry["review_of"] is not None}
        _private_dir(log_path.parent)
        _write(log_path, json.dumps(log, indent=2) + "\n")
        results.append({**entry, "status": "sent"})
    return results

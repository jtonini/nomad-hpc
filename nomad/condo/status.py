# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What is left to set up for the condo reports, in one list that
`nomad usage-report condos status`, `nomad config check` and the Console
all show: condo partitions on the nodes that report.toml doesn't list yet,
condos it lists that are gone, owners not confirmed, reports that can't be
addressed or sent, and the monthly timer.

Each item is "todo" (a report won't go out as meant until it is done) or a
"note" (worth knowing: review mode, no PDF). An item's text may name condos
and owners; ``count`` and ``kind`` say the same without names.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import asdict, dataclass, field

from nomad.condo import discover

TIMER = "nomad-condo-reports.timer"


@dataclass
class Item:
    kind: str                 # new, not_configured, gone, unconfirmed, no_owner, no_address,
                              # no_sender, no_readings, timer, reviewing, no_pdf, report_toml
    level: str                # "todo" or "note"
    text: str                 # in words; may name condos and owners
    masked: str               # the same, with counts instead of names
    cluster: str | None = None
    names: list[str] = field(default_factory=list)
    how: str | None = None    # what to do, as a command

    def as_dict(self) -> dict:
        return asdict(self)


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def timer_state() -> str | None:
    """"enabled", "disabled" (installed, off), "absent", or None when this
    host has no systemd to ask."""
    if not shutil.which("systemctl"):
        return None
    try:
        r = subprocess.run(["systemctl", "is-enabled", TIMER], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    word = (r.stdout or "").strip()
    if word in ("enabled", "static", "alias", "indirect"):
        return "enabled"
    if word in ("disabled", "masked", "linked"):
        return "disabled"
    if word in ("not-found", "") or "No such file" in (r.stderr or ""):
        return "absent"
    return None


def check(conn, settings, nomad_config: dict | None = None, *, problem: str | None = None,
          timer: str | None = "ask", pdf: bool | None = None) -> list[Item]:
    """What is left to do, from the node readings in ``conn`` (a hub's
    combined database, or a site's own) and the settings. ``problem``: why
    report.toml didn't read (the settings are then the defaults). ``timer``:
    timer_state(), asked when "ask". ``pdf``: whether PDFs can be made
    (asked when None)."""
    items: list[Item] = []
    if problem:
        # The settings are then the defaults: anything else said from them
        # (no condos listed, no sender) would be about a file nobody wrote.
        return [Item("report_toml", "todo", f"report.toml doesn't read: {problem}",
                     "report.toml doesn't read (run `nomad usage-report condos status` to see why)",
                     how="nomad usage-report condos status")]
    d = settings.delivery
    any_condos = False
    confirmed = []
    seen_clusters = set()
    sites = discover.node_sites(conn) if conn is not None else []
    for site in sites:
        name = site if site is not None else discover.local_name(settings, nomad_config)
        seen_clusters.add(name)
        cc = settings.cluster(name)
        found = discover.find(discover.read_nodes(conn, site), cc.guest_partitions)
        listed = {c.part: c for c in cc.condos.values()}
        if found.condos or listed:
            any_condos = True
        if not cc.configured and found.condos:
            parts = sorted(found.condos)
            items.append(Item(
                "not_configured", "todo",
                f"{name}: {_plural(len(parts), 'condo')} on the nodes, none in report.toml yet: {', '.join(parts)}",
                f"{name}: {_plural(len(parts), 'condo')} on the nodes, none in report.toml yet",
                cluster=name, names=parts, how="nomad usage-report condos init --apply"))
            continue
        new = sorted(set(found.condos) - set(listed))
        if new:
            items.append(Item(
                "new", "todo",
                f"{name}: on the nodes, not in report.toml: {', '.join(new)}",
                f"{name}: {_plural(len(new), 'condo partition')} on the nodes, not in report.toml",
                cluster=name, names=new, how="nomad usage-report condos init --apply"))
        gone = sorted(c.name for c in cc.condos.values() if not c.nodes and c.part not in found.condos)
        if gone:
            items.append(Item(
                "gone", "todo",
                f"{name}: in report.toml, gone from the nodes: {', '.join(gone)}",
                f"{name}: {_plural(len(gone), 'condo')} in report.toml, gone from the nodes",
                cluster=name, names=gone, how=f"remove them from {settings.path}"))
        for c in cc.condos.values():
            if c.confirmed and c.owner and c.name not in gone:
                confirmed.append((name, c))
        ownerless = sorted(c.name for c in cc.condos.values() if not c.owner and c.name not in gone)
        if ownerless:
            items.append(Item(
                "no_owner", "todo",
                f"{name}: no owner set: {', '.join(ownerless)}",
                f"{name}: {_plural(len(ownerless), 'condo')} without an owner",
                cluster=name, names=ownerless,
                how=f"nomad usage-report condos set --owner {ownerless[0]}=NETID --apply"))
        waiting = sorted(c.name for c in cc.condos.values()
                         if not c.confirmed and c.owner and c.name not in gone)
        if waiting:
            items.append(Item(
                "unconfirmed", "todo",
                f"{name}: owner not confirmed: {', '.join(waiting)} (no report goes to an owner until it is)",
                f"{name}: {_plural(len(waiting), 'condo')} whose owner isn't confirmed",
                cluster=name, names=waiting,
                how=f"nomad usage-report condos set --confirm {','.join(waiting)} --apply"))
    if not sites and any(cc.condos for cc in settings.clusters.values()):
        # No node readings at all (no database here, or not this one): the
        # condos can't be checked against anything, which is one thing.
        n = sum(len(cc.condos) for cc in settings.clusters.values())
        items.append(Item(
            "no_readings", "todo",
            "no node readings in this database: the condos in report.toml can't be checked "
            "(run this where the hub's combined database is, or give it with --db)",
            f"no node readings: {_plural(n, 'condo')} in report.toml can't be checked",
            how="nomad usage-report condos status --db COMBINED.DB"))
        any_condos = True
    # report.toml's clusters the nodes don't show (another hub's, a renamed site).
    for cname, cc in sorted(settings.clusters.items()):
        if cc.condos and sites and cname not in seen_clusters:
            any_condos = True
            names = sorted(cc.condos)
            items.append(Item(
                "gone", "todo",
                f"{cname}: in report.toml, but no node readings from that cluster: {', '.join(names)}",
                f"{cname}: {_plural(len(names), 'condo')} in report.toml, no node readings from that cluster",
                cluster=cname, names=names, how=f"check the cluster's name in {settings.path}"))
    if not any_condos:
        return items
    # Addresses: a confirmed condo goes to email, else NETID@mail_domain.
    unaddressed = sorted(f"{cl}/{c.name}" for cl, c in confirmed if not c.addresses(d))
    if unaddressed:
        items.append(Item(
            "no_address", "todo",
            f"no address for {', '.join(unaddressed)}: set mail_domain in [report.labs] (or email for the condo)",
            f"{_plural(len(unaddressed), 'confirmed condo')} without an address: set mail_domain",
            names=unaddressed, how="nomad usage-report condos set --labs mail_domain=example.edu --apply"))
    from nomad.mail import _normalise
    mail = (nomad_config or {}).get("mail")
    if not d.send_from and not _normalise(mail if isinstance(mail, dict) else None).get("from"):
        items.append(Item(
            "no_sender", "todo", "no sender: set send_from in [report.labs], or from in [mail] in nomad.toml",
            "no sender: set send_from in [report.labs], or from in [mail]",
            how='nomad usage-report condos set --labs "send_from=Research Computing <rc@example.edu>" --apply'))
    state = timer_state() if timer == "ask" else timer
    if state in ("absent", "disabled"):
        words = "isn't installed" if state == "absent" else "is installed but not enabled"
        items.append(Item(
            "timer", "todo" if confirmed else "note",
            f"the monthly timer {words}: reports are made and sent only when someone runs them",
            f"the monthly timer {words}",
            how="nomad usage-report condos timer --apply (as root)"))
    if d.reviewing:
        items.append(Item(
            "reviewing", "note",
            f"review mode: every report goes to {', '.join(d.review_to)}; clear review_to to send them to owners",
            f"review mode: every report goes to {_plural(len(d.review_to), 'reviewer')}",
            how="nomad usage-report condos set --labs review_to= --apply"))
    if "pdf" in d.formats:
        if pdf is None:
            from nomad.condo import pdf as cpdf
            pdf = cpdf.available()
        if not pdf:
            items.append(Item(
                "no_pdf", "note", "no PDF: fpdf2 isn't installed here, so reports go without one",
                "no PDF: fpdf2 isn't installed here",
                how="pip install 'nomad-hpc[report]'"))
    return items


def todo_count(items: list[Item]) -> int:
    return sum(1 for i in items if i.level == "todo")

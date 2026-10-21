# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad init`'s condo step: whether this cluster has condos, which, whose,
and how their reports go out, written to report.toml beside nomad.toml.

The wizard knows each partition's nodes (it asked Slurm), so the condos are
found as `nomad usage-report condos init` finds them from node readings: the
partitions whose nodes all sit inside the guest partition. An owner the
person types is confirmed (they said so); one left blank is set later.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from nomad.condo import discover

_NETID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass
class ClusterPlan:
    name: str
    guest: str
    condos: dict[str, list[str]] = field(default_factory=dict)      # partition -> owners ([] = later)
    nodes: dict[str, int] = field(default_factory=dict)             # partition -> node count


@dataclass
class Plan:
    clusters: list[ClusterPlan] = field(default_factory=list)
    labs: dict = field(default_factory=dict)                        # [report.labs] key -> value


def find_condos(partitions: dict, guest: str) -> dict[str, set[str]]:
    """partition -> its nodes, for the partitions that are condos under
    ``guest`` (the wizard's {partition: {"nodes": [...]}})."""
    found = discover.Found(site=None)
    for p, info in (partitions or {}).items():
        nodes = info.get("nodes") if isinstance(info, dict) else info
        found.partitions[str(p)] = {str(n) for n in (nodes or [])}
    discover.find(found, [guest])
    return found.condos


def guest_guess(partitions: dict) -> str | None:
    """The partition named like "condo", as discovery takes it."""
    named = sorted(p for p in (partitions or {}) if re.search("condo", str(p), re.IGNORECASE))
    return named[0] if named else None


def ask(clusters: list[dict], prompt, confirm, echo) -> Plan | None:
    """The questions, asked with the given prompt/confirm/echo (click's)."""
    hpc = [c for c in clusters if c.get("type", "hpc") == "hpc" and c.get("partitions")]
    echo("  A condo is a set of nodes a lab bought, lent to everyone else")
    echo("  through a guest partition while the lab isn't using them.")
    echo("  NØMAÐ can send each owner a monthly report on their nodes, and")
    echo("  you a summary of every condo.")
    echo("")
    if not hpc:
        echo("  (No Slurm cluster here: nothing to register.)")
        return None
    if not confirm("  Register condos for the monthly condo reports?", default=False):
        echo("  Later: nomad usage-report condos init, or the Console's")
        echo("  Settings > Condo reports, where the cluster's data is gathered.")
        return None
    plan = Plan()
    for c in hpc:
        parts = c["partitions"]
        guess = guest_guess(parts)
        echo("")
        echo(f"  {c['name']}: partitions " + ", ".join(
            f"{p} ({len((v or {}).get('nodes') or [])})" for p, v in sorted(parts.items())))
        while True:
            g = str(prompt(f"  Guest partition on {c['name']} (the one over every condo node; "
                           "none: no condos here)", default=guess or "none")).strip()
            if g.lower() == "none" or g == "":
                g = None
                break
            if g in parts:
                break
            echo(f"    {c['name']} has no partition {g!r}")
        if g is None:
            continue
        condos = find_condos(parts, g)
        if not condos:
            echo(f"    No partition has all its nodes inside {g}: no condos found on {c['name']}.")
            continue
        echo("    Condos: " + ", ".join(f"{p} ({len(n)} node{'' if len(n) == 1 else 's'})"
                                        for p, n in condos.items()))
        # As nomad.toml names it ([clusters.ID]), so a site's own reports find it.
        cid = str(c["name"]).lower().replace(" ", "-")
        cp = ClusterPlan(name=cid, guest=g, nodes={p: len(n) for p, n in condos.items()})
        for p in condos:
            while True:
                raw = str(prompt(f"    Owner NetID for {p} (commas for several; Enter: later)",
                                 default="", show_default=False)).strip()
                people = list(dict.fromkeys(x.strip() for x in raw.replace(" ", ",").split(",") if x.strip()))
                bad = [x for x in people if not _NETID.match(x)]
                if not bad:
                    break
                echo(f"    {bad[0]!r} isn't a NetID")
            cp.condos[p] = people
        plan.clusters.append(cp)
    if not plan.clusters:
        return None
    echo("")
    while True:
        dom = str(prompt("  Owners' mail domain (an owner's address is NETID@this; Enter: later)",
                         default="", show_default=False)).strip()
        if not dom or re.match(r"^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$", dom):
            break
        echo(f"    {dom!r} isn't a domain")
    if dom:
        plan.labs["mail_domain"] = dom
    while True:
        rev = str(prompt("  Send every report here first, for review (addresses, commas; "
                         "Enter: none)", default="", show_default=False)).strip()
        addrs = list(dict.fromkeys(x.strip() for x in rev.split(",") if x.strip()))
        bad = [x for x in addrs if not _one_line(x) or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", x)]
        if not bad:
            break
        echo(f"    {bad[0]!r} isn't an email address")
    if addrs:
        plan.labs["review_to"] = addrs
    else:
        echo("    (no review: once confirmed, each owner gets their report directly)")
    return plan


def _one_line(text: str) -> bool:
    return not any(ord(c) < 32 or 0x7F <= ord(c) <= 0x9F or c in "\u2028\u2029" for c in text)


def merged(plan: Plan, have: dict) -> tuple[list[ClusterPlan], list[str]]:
    """The plan's clusters as report.toml will hold them, and notes.

    A site's database holds every cluster its nomad.toml lists, and the
    reports read it as one (on a hub too: one site, one name). So condos
    registered on one cluster keep that cluster's name (nomad.toml's
    [clusters.ID], which discover.local_name finds); on several, or where
    report.toml already uses "this-cluster", they go together under
    "this-cluster"."""
    if not plan.clusters:
        return [], []
    if discover.LOCAL not in have and len(plan.clusters) == 1:
        return plan.clusters, []
    notes = []
    out = ClusterPlan(name=discover.LOCAL, guest=[])
    for cp in plan.clusters:
        for g in ([cp.guest] if isinstance(cp.guest, str) else cp.guest):
            if g not in out.guest:
                out.guest.append(g)
        for p, owners in cp.condos.items():
            if p in out.condos:
                notes.append(f"{p} is a condo on two clusters: kept the first")
                continue
            out.condos[p] = owners
            out.nodes[p] = cp.nodes.get(p, 0)
    if len(plan.clusters) > 1:
        notes.append(f"condos on {', '.join(c.name for c in plan.clusters)} go together as "
                     f"{discover.LOCAL}: this machine's database holds them all")
    return [out], notes


def text_for(plan: Plan, existing_text: str = "", existing: dict | None = None):
    """(text to add to report.toml, what it adds, what it leaves because the
    file already has it, [report.labs] settings to set through the editor
    because the file declares that table already)."""
    existing = existing or {}
    report = existing.get("report") or {}
    labs_have = report.get("labs")
    clusters_have = report.get("clusters") or {}
    out, said, kept = [], [], []
    labs = {k: v for k, v in plan.labs.items() if not (labs_have and k in labs_have)}
    kept += [f"[report.labs] {k}" for k in plan.labs if labs_have and k in labs_have]
    labs_later = {}
    if labs:
        if labs_have is not None:
            labs_later = labs           # a table can't be declared twice
        else:
            out.append("[report.labs]")
            for k, v in labs.items():
                out.append(f"{k} = " + ("[" + ", ".join(discover._q(x) for x in v) + "]"
                                        if isinstance(v, list) else discover._q(v)))
            out.append("")
            said.append("how reports go out")
    for cp in plan.clusters:
        guests = [cp.guest] if isinstance(cp.guest, str) else list(cp.guest)
        have = clusters_have.get(cp.name)
        if have and have.get("condos"):
            kept.append(f"{cp.name}'s condos")
            continue
        head = f"report.clusters.{discover._key(cp.name)}"
        gp = "[" + ", ".join(discover._q(g) for g in guests) + "]"
        if have is None:
            out.append(f"[{head}]")
            out.append(f"guest_partitions = {gp}")
            out.append("")
        elif "guest_partitions" not in have:
            kept.append(f"{cp.name}'s table (add guest_partitions = {gp} to it)")
        for p, owners in cp.condos.items():
            out.append(f"[{head}.condos.{discover._key(p)}]")
            if owners:
                out.append("owner = " + (discover._q(owners[0]) if len(owners) == 1 else
                                         "[" + ", ".join(discover._q(o) for o in owners) + "]"))
                out.append("confirmed = true             # given at nomad init")
            else:
                out.append("# owner = \"NETID\"           # set it, then confirmed = true")
                out.append("confirmed = false")
            out.append("")
        n = len(cp.condos)
        said.append(f"{cp.name}: {n} condo{'' if n == 1 else 's'}")
    return ("\n".join(out).rstrip("\n") + "\n") if out else "", said, kept, labs_later


def _share_with_nomad(path: Path, echo) -> None:
    """Written as root (`nomad init --system`): readable by the nomad
    account, which makes the reports and runs the Console."""
    import grp
    import os
    import pwd
    if os.geteuid() != 0:
        return
    try:
        pw = pwd.getpwnam("nomad")
    except KeyError:
        return
    os.chown(path, 0, pw.pw_gid)
    os.chmod(path, 0o640)
    echo(f"  (readable by the nomad account: root:{grp.getgrgid(pw.pw_gid).gr_name}, 640)")


def write(path: Path, plan: Plan, dry_run: bool, echo) -> None:
    """Add the plan to report.toml: a new file readable only by its owner
    (and the nomad account when root writes it), or added to the end of one
    already there, a copy kept first. Nothing is written unless all of it
    reads as condo settings."""
    import tempfile
    from datetime import datetime

    from nomad.condo import config as ccfg, edit as cedit
    from nomad.config.edit import loads
    from nomad.usage.run import private_file
    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""
    existing = loads(existing_text) if existing_text.strip() else {}
    have = ccfg.from_dict(existing).clusters if existing else {}
    clusters, notes = merged(plan, have)
    text, said, kept, labs_later = text_for(Plan(clusters=clusters, labs=plan.labs), existing_text, existing)
    joiner = "" if not existing_text.strip() else ("\n" if existing_text.endswith("\n") else "\n\n")
    new = existing_text + joiner + text if text else existing_text
    ccfg.from_dict(loads(new) if new.strip() else {})      # it must read
    labs_diff = ""
    if labs_later:
        # Checked on the file as it will be, before anything is written.
        with tempfile.TemporaryDirectory() as tmp:
            probe = Path(tmp) / "report.toml"
            probe.write_text(new, encoding="utf-8")
            labs_diff = cedit.plan(probe, cedit.Changes(labs=labs_later)).diff
    for n in notes:
        echo(f"  ({n})")
    for k in kept:
        echo(f"  (report.toml already has {k}: left as it is)")
    if dry_run:
        echo("")
        echo(f"  ── {path} would get ──")
        echo(text or "  (no new tables)")
        if labs_diff:
            echo("  and in [report.labs]: " + ", ".join(f"{k} = {v}" for k, v in labs_later.items()))
        return
    if not text and not labs_later:
        echo("  report.toml already has these condos: nothing added")
        return
    if text:
        if path.exists():
            if existing_text:
                backup = path.with_name(f"{path.name}.bak-{datetime.now():%Y%m%d-%H%M%S}")
                with private_file(backup) as f:
                    f.write(existing_text)
            with open(path, "a", encoding="utf-8") as f:
                f.write(new[len(existing_text):])
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with private_file(path) as f:
                f.write(text)
            _share_with_nomad(path, echo)
    if labs_later:
        cedit.save(cedit.plan(path, cedit.Changes(labs=labs_later)))
        said.append("how reports go out")
    echo(f"  ✓ report.toml: {'; '.join(said)} ({path})")

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad watch`: what the policy watch found on a cluster's login and
compute nodes -- tunnels, ssh reverse forwards and SOCKS proxies, VS Code
tunnels, miners, relays, services open to the network on a login node --
for an administrator to look at. Names are the site's own (admins see
NetIDs); --mask gives stand-ins for anything shared outside."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta

import click

ORDER = {"critical": 0, "warning": 1, "info": 2}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _c(v) -> str:
    """A value as printed: no control characters (a terminal's escapes)."""
    return _CONTROL.sub("?", str(v if v is not None else ""))


class _Masker:
    def __init__(self, on: bool):
        self.on, self.names = on, {}

    def __call__(self, kind: str, name) -> str:
        if not self.on:
            return _c(name if name not in (None, "") else "?")
        key = (kind, name)
        if key not in self.names:
            self.names[key] = f"{kind}{sum(1 for k in self.names if k[0] == kind) + 1}"
        return self.names[key]


def _db(ctx, db):
    if db:
        return db
    from nomad.cli import get_db_path
    return str(get_db_path((ctx.obj or {}).get("config", {}) if ctx and ctx.obj else {}))


def _connect(path):
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM policy_findings LIMIT 1")
        return conn
    except sqlite3.Error:
        raise click.ClickException(f"{path}: no policy watch here yet (turn on [collectors.policy_watch], "
                                   "then nomad collect --once)")


def _short(ts) -> str:
    return str(ts or "?").replace("T", " ")[5:16]


def _masked_note(m, note) -> str:
    """A finding's note as printed; with --mask, the listed servers it names
    (the only names a note has) as stand-ins."""
    if not note:
        return ""
    head, sep, rest = str(note).partition("verified: to ")
    if m.on and sep:
        names, semi, tail = rest.partition(";")
        if names != "the cluster":
            rest = ", ".join(m("host", n.strip()) for n in names.split(",")) + semi + tail
    return _c(head + sep + rest)


def _extra(f) -> str:
    """What follows a finding's kind: its program, port, and where it goes."""
    extra = f" {f['program']}" if f.get("program") else ""
    if f.get("detail"):
        extra += f" port {f['detail']}" if str(f["detail"]).isdigit() else f" {f['detail']}"
    if f.get("verified"):
        extra += ", verified"
    elif f.get("reach") == "outside":
        extra += ", connects outside"
    elif f.get("reach") == "campus":
        extra += ", on campus"
    elif f.get("reach") == "inside":
        extra += ", stays inside"
    elif f.get("reach") == "ephemeral":
        extra += " (an ephemeral port)"
    return _c(extra)


@click.group("watch")
def watch_group():
    """What the policy watch found: tunnels, miners, relays, exposed services.

    \b
    nomad watch list              findings of the last 7 days
    nomad watch list --mask       with stand-ins for names and hosts (to share)
    nomad watch status            what each host's sweep could see
    nomad watch probe [HOST]      look now, here or on HOST (over ssh)

    Turned on per site with [collectors.policy_watch] enabled = true. It
    reads; it never stops or changes anything. See docs/policy_watch.md."""


@watch_group.command("list")
@click.option("--days", default=7, show_default=True, type=click.IntRange(1, 3650),
              help="Findings seen in the last DAYS days.")
@click.option("--all", "everything", is_flag=True, help="Also what is only listed (info): listeners on "
              "ephemeral ports on compute nodes. Verified forwards are always shown.")
@click.option("--mask", is_flag=True, help="Stand-ins for people and hosts (for sharing outside ARC).")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
@click.option("--db", type=click.Path(exists=True, dir_okay=False), help="Database (default: the site's).")
@click.pass_context
def list_cmd(ctx, days, everything, mask, as_json, db):
    """Findings, the most serious and the latest first; a verified ssh
    forward (to a listed server) is shown too, as info."""
    from nomad.collectors.policy_watch import LABELS
    conn = _connect(_db(ctx, db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(policy_findings)")}
    site = "source_site" if "source_site" in cols else "_sync_site" if "_sync_site" in cols else None
    since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    later = ", ".join(c if c in cols else "NULL" for c in ("verified", "note"))
    rows = conn.execute(
        "SELECT host, role, username, uid, kind, program, detail, reach, first_seen, last_seen, sweeps, severity, "
        + (site or "NULL") + ", " + later + " FROM policy_findings WHERE last_seen >= ?", (since,)).fetchall()
    # seen in its site's last sweep: within a minute of the latest it has (info included)
    latest: dict = {}
    for r in rows:
        latest[r[12]] = max(latest.get(r[12], ""), str(r[9]))
    if not everything:
        rows = [r for r in rows if r[11] != "info" or r[13] == 1]
    rows.sort(key=lambda r: (ORDER.get(r[11], 3), -(datetime.fromisoformat(str(r[9])[:19]).timestamp())))
    m = _Masker(mask)
    out = []
    for r in rows:
        try:
            now = (datetime.fromisoformat(latest[r[12]][:19]) - datetime.fromisoformat(str(r[9])[:19])) \
                <= timedelta(minutes=1)
        except ValueError:
            now = False
        out.append({"site": m("site", r[12]) if site else None, "host": m("host", r[0]), "role": r[1],
                    "user": m("user", r[2] or f"uid {r[3]}"), "kind": r[4], "what": LABELS.get(r[4], r[4]),
                    "program": _c(r[5]), "detail": _c(r[6]), "reach": r[7] or "", "first_seen": r[8],
                    "last_seen": r[9], "sweeps": r[10], "severity": r[11], "now": now,
                    "verified": None if r[13] is None else bool(r[13]), "note": _masked_note(m, r[14])})
    if as_json:
        click.echo(json.dumps(out, indent=2))
        return
    if not out:
        click.echo(f"Nothing found in the last {days} days" + ("" if everything else
                   " (--all: also what is only listed)") + ".")
        return
    click.echo(f"{len(out)} findings in the last {days} days (* seen in the last sweep):\n")
    for f in out:
        where = (f["site"] + ":" if f["site"] else "") + f["host"]
        click.echo(f"{'*' if f['now'] else ' '} {f['severity']:<8} {where:<16} {f['user']:<12} "
                   f"{f['what']}{_extra(f)}")
        click.echo(f"{'':<11}first {_short(f['first_seen'])}, last {_short(f['last_seen'])}, "
                   f"in {f['sweeps']} sweep{'s' if f['sweeps'] != 1 else ''} ({f['role']})")
        if f["note"]:
            click.echo(f"{'':<11}{f['note']}")


@watch_group.command("status")
@click.option("--mask", is_flag=True, help="Stand-ins for hosts.")
@click.option("--db", type=click.Path(exists=True, dir_okay=False), help="Database (default: the site's).")
@click.pass_context
def status_cmd(ctx, mask, db):
    """The last sweep: each host asked, and what the probe could see there."""
    conn = _connect(_db(ctx, db))
    try:
        last = conn.execute("SELECT MAX(timestamp) FROM policy_watch_hosts").fetchone()[0]
    except sqlite3.Error:
        last = None
    if not last:
        click.echo("No sweep here (on the hub: run it on the site, where each sweep's hosts are kept).")
        return
    rows = conn.execute("SELECT host, role, answered, processes, others, others_readable, hidepid, "
                        "listener_programs, error FROM policy_watch_hosts WHERE timestamp = ? ORDER BY role, host",
                        (last,)).fetchall()
    m = _Masker(mask)
    ok = sum(1 for r in rows if r[2])
    click.echo(f"Last sweep {_short(last)}: {ok} of {len(rows)} hosts answered.\n")
    blind, unnamed = [], 0
    for host, role, answered, procs, others, readable, hidepid, lprogs, error in rows:
        if not answered:
            click.echo(f"  {m('host', host):<14} {role:<6} no answer: {_c(error or '?')}")
            continue
        sees = "sees every account's processes"
        if not readable:
            sees = "can't see other accounts' processes" + (" (hidepid)" if hidepid else "") + ": only its own"
            blind.append(host)
        unnamed += 0 if lprogs else 1
        click.echo(f"  {m('host', host):<14} {role:<6} {procs} processes; {sees}")
    if blind:
        click.echo("\n  Where other accounts' processes are hidden, only listening sockets are seen for them "
                   "(/proc/net is readable by all).")
    if unnamed:
        click.echo("\n  Not root: which program listens on a port isn't known (another account's sockets are "
                   "its own); ignore_ports, not ignore_programs, leaves out a sanctioned listener.")


@watch_group.command("probe")
@click.argument("host", required=False)
@click.option("--mask", is_flag=True, help="Stand-ins for people.")
@click.option("--json", "as_json", is_flag=True, help="The probe's own answer, as JSON.")
@click.pass_context
def probe_cmd(ctx, host, mask, as_json):
    """Look now, here or on HOST (over ssh, as the collector does). Stores nothing."""
    import logging
    from nomad.collectors.base import find_tool
    from nomad.collectors.policy_watch import LABELS, PolicyWatchCollector, findings_of
    from nomad.collectors.node_disks import HOST_RE
    conf = ((ctx.obj or {}).get("config", {}) or {}).get("collectors", {}).get("policy_watch", {}) \
        if ctx and ctx.obj else {}
    base = logging.getLogger("nomad.collectors.base")
    was = base.level
    base.setLevel(logging.WARNING)                     # not "Initialized policy_watch collector"
    try:
        c = PolicyWatchCollector(dict(conf or {}), ":memory:")
    finally:
        base.setLevel(was)
    if host:
        if not HOST_RE.fullmatch(host):
            raise click.BadParameter("not a host name", param_hint="HOST")
        ssh = find_tool("ssh")
        if not ssh:
            raise click.ClickException("ssh not found")
        d, err = c.probe_over_ssh(ssh, host)
        role = "login" if host in c.login_hosts else "node"
    else:
        d, err = c.probe_here()
        role = "login"
    if not d:
        raise click.ClickException(f"no answer from {host or 'here'}: {err}")
    if as_json:
        if mask:
            m = _Masker(True)
            for f in (d.get("findings") or []) + (d.get("listeners") or []):
                f["user"], f["uid"] = m("user", f.get("user") or f.get("uid")), None
            d["host"] = m("host", d.get("host"))
            d["addresses"], d["cluster"] = [], []
        click.echo(json.dumps(d, indent=2))
        return
    found = findings_of(d, d.get("host") or host or "here", role, listeners_on_nodes=True,
                        ignore_users=c.ignore_users, ignore_programs=c.ignore_programs,
                        ignore_ports=c.ignore_ports, servers=c.servers())
    m = _Masker(mask)
    click.echo(f"{m('host', d.get('host'))}: {d.get('processes')} processes"
               + (" (other accounts' hidden: hidepid)" if d.get("hidepid") else "") + ".")
    from nomad.collectors.policy_watch import describe_trusted
    trusted = describe_trusted(c.trusted())
    if mask and trusted:
        click.echo(f"  trusted: {len(trusted)} hosts or networks (nomad config check names them)")
    for line in [] if mask else trusted:
        click.echo(f"  trusted: {_c(line)}")
    if mask and c.trust_notes:
        click.echo(f"  {len(c.trust_notes)} notes on the settings (nomad config check says them)")
    for n in [] if mask else c.trust_notes:
        click.echo(f"  note: {_c(n)}")
    if not found:
        click.echo("Nothing found.")
    for f in found:
        who = m("user", f["username"] or "uid " + str(f["uid"]))
        click.echo(f"  {f['severity']:<8} {who:<12} {LABELS.get(f['kind'], f['kind'])}{_extra(f)}")
        if f.get("note"):
            click.echo(f"  {'':<8} {'':<12} {_masked_note(m, f['note'])}")
    loop = [s for s in d.get("listeners") or [] if s.get("scope") == "loopback"]
    if loop:
        click.echo(f"  ({len(loop)} listening on loopback only: not counted)")

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Node alerts the hub sends for sites that can't send their own (1.17.3).

A site's own nomad raises its node alerts: the node_state collector, through
the email, Slack or webhook in that site's nomad.toml. A site that can't send
any -- a head node with no way to the mail server, or one set up with
``nomad init --quick``, which writes nothing in [alerts] -- can leave them to
the hub, which sees its nodes after every sync::

    [[hub.sites]]
    name = "c2"
    alerts = "hub"          # the hub sends this site's node alerts

After each sync the hub reads each such site's latest node reading. A site
whose latest is older than STALE_HOURS is skipped (the hub's sync and
collection alerts say it stopped), and what was sent about it is kept, so it
isn't news again when it reports. Each unhealthy node is raised as the
site's own collector would raise it (nomad.collectors.node_state
classify_state: critical for DOWN, NOT_RESPONDING ...; warning for DRAIN
...; the hub's own [alerts.node_state] states apply), through the hub's
[alerts] backends: once, again when worse, then every reminder_hours while
it lasts. A node healthy again is forgotten, so its return is news.
``[hub] quiet_node_alerts`` (``name``, ``site:name``, ``site:*``, shell
wildcards) leaves nodes out: one kept down on purpose, say. (``quiet_nodes``
is for the collection checks, a GPU node without DCGM: it doesn't silence a
node going down.) A node's reason is sent without
the name of whoever set it, and with what could name a person masked.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

STALE_HOURS = 2
STAMP = "node_alerts.json"
# Slurm adds who set a node's reason and when: "disk [jdoe@2026-10-01T09:00:00]".
_SETTER = re.compile(r"\s*\[[^\]]*@[^\]]*\]\s*$")


def reason_text(reason, names=()) -> str:
    """A node's reason without anyone's name: Slurm's [setter@time] taken
    off, then what could name a person masked (collection_health.masked)."""
    from nomad.collection_health import masked
    text = _SETTER.sub("", str(reason or "")).strip()
    return masked(text, names) if text else "no reason given"


def hub_alert_sites(config: dict | None, sites=None) -> list[str]:
    """The [[hub.sites]] whose node alerts the hub sends (alerts = "hub").
    sites: the hub's site list as `nomad sync` read it (--config-file, a
    sync.toml), else config's [[hub.sites]]."""
    if sites is None:
        hub = (config or {}).get("hub")
        sites = hub.get("sites") if isinstance(hub, dict) else None
    out = []
    for s in sites or []:
        if isinstance(s, dict) and str(s.get("alerts") or "").strip().lower() == "hub" and s.get("name"):
            out.append(str(s["name"]))
    return out


def _time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "").replace(" ", "T")) if value else None
    except ValueError:
        return None


def _listed(state, cfg) -> bool:
    from nomad.collectors.node_state import DEFAULT_CRITICAL_STATES, DEFAULT_WARNING_STATES
    s = str(state or "").upper()
    names = list(cfg.get("critical_states", DEFAULT_CRITICAL_STATES)) + list(
        cfg.get("warning_states", DEFAULT_WARNING_STATES))
    return "*" in s or any(str(n).upper() in s for n in names)


def due_node_alerts(conn: sqlite3.Connection, sites: list[str], config: dict | None = None,
                    now: datetime | None = None) -> tuple[list[dict], set]:
    """(items for send_once, sites skipped as not read lately)."""
    from nomad.collectors.node_state import classify_state
    now = now or datetime.now()
    cfg = ((config or {}).get("alerts") or {}).get("node_state") or {}
    if isinstance(cfg, dict) and cfg.get("enabled") is False:
        return [], set()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(node_state)")}
    if not {"node_name", "state", "timestamp", "source_site"} <= cols:
        return [], set(sites)
    healthy = "is_healthy" if "is_healthy" in cols else None
    reason = "reason" if "reason" in cols else "NULL"
    from nomad.collection_health import _quiet, _usernames
    hub = (config or {}).get("hub")
    quiet = tuple(str(x) for x in ((hub if isinstance(hub, dict) else {}).get("quiet_node_alerts") or ())
                  if isinstance(x, str))
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    names = _usernames(conn, tables)
    items, skipped = [], set()
    for site in sites:
        latest = conn.execute("SELECT MAX(timestamp) FROM node_state WHERE source_site = ?",
                              (site,)).fetchone()[0]
        read = _time(latest)
        if read is None or now - read > timedelta(hours=STALE_HOURS):
            skipped.add(site)
            continue
        for node, state, why, ok in conn.execute(
                f"SELECT node_name, state, {reason}, {healthy or 'NULL'} FROM node_state "
                "WHERE source_site = ? AND timestamp = ? ORDER BY node_name", (site, latest)):
            if healthy is not None and ok is not None:
                if ok not in (0, "0", False):
                    continue
            elif not _listed(state, cfg):
                continue            # no health mark stored: only the states the lists name
            if _quiet(quiet, site, node):
                continue            # [hub] quiet_node_alerts
            severity = classify_state(state, cfg)
            if severity is None:
                continue
            why = reason_text(why, names)
            items.append({
                # The site in the subject: the dispatcher knows a condition by
                # source, host and subject, and two sites can both have a node01.
                "key": f"node:{site}:{node}", "site": site, "cluster": site, "host": node,
                "subject": f"{site}/{node}", "severity": severity,
                "message": f"Node {node} at {site} is {state} ({why}); sent by the hub for {site}.",
                "details": {"state": state, "reason": why, "cluster": site,
                            "as_of": read.isoformat(timespec="seconds"), "sent_by": "hub"},
            })
    return items, skipped


def hub_node_alerts(combined_path, cache_dir=None, config: dict | None = None,
                    now: datetime | None = None, failing=(), hub_sites=None) -> list[dict]:
    """Send what is due for the sites set to alerts = "hub"; returns what was
    sent. failing: sites whose pull failed this run (kept, as skipped ones).
    hub_sites: the site list `nomad sync` read (see hub_alert_sites)."""
    sites = hub_alert_sites(config, hub_sites)
    if not sites:
        return []
    now = now or datetime.now()
    combined_path = Path(combined_path)
    try:
        conn = sqlite3.connect(f"file:{combined_path}?mode=ro", uri=True, timeout=30)
        try:
            items, skipped = due_node_alerts(conn, sites, config, now)
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    from nomad.alerts.once import send_once
    stamp = (Path(cache_dir) if cache_dir is not None else combined_path.parent) / STAMP
    return send_once(items, stamp, "node_state", config, now,
                     hold_sites=set(skipped) | set(failing or ()))

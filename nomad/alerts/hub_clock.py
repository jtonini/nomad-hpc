# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""CPU clock alerts the hub sends for sites that leave their alerts to it (1.27.0).

A site set to ``alerts = "hub"`` in [[hub.sites]] (vadur) judges its CPUs'
clocks with its own clock collector (cpu_clock_hosts: throttled, severity,
the problem in words); after each sync the hub sends what the site's own
collector would: one alert a throttled host while it lasts (once, again when
worse, then every ``[alerts] reminder_hours``). A host whose last reading is
older than STALE_HOURS, or whose site's pull failed, is held as it was. The
hub's own ``[alerts.clock]`` applies (enabled, ignore).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

STALE_HOURS = 2
STAMP = "clock_alerts.json"


def _time(text):
    try:
        return datetime.fromisoformat(str(text)) if text else None
    except ValueError:
        return None


def due_clock_alerts(conn: sqlite3.Connection, sites, config: dict | None, now: datetime, failing=()):
    """(items, held) for send_once; held: "site/node" kept as it was."""
    from nomad.collectors.clock import alert_item, fresh, hosts_now, power_of, rules
    raw = ((config or {}).get("alerts") or {}).get("clock") or {}
    raw = raw if isinstance(raw, dict) else {}
    if raw.get("enabled") is False:
        return [], set()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "cpu_clock_hosts" not in tables:
        return [], set()
    items, held = [], set()
    for site in sites:
        for h in hosts_now(conn, site=site):
            if h.get("source_site") != site:
                continue
            node = h["node"]
            if site in failing or not fresh(h, now, STALE_HOURS):
                held.add(f"{site}/{node}")
                continue
            # as the site judged it (its own [alerts.clock]: no severity when it leaves a host out), and the hub's
            if not h.get("throttled") or not h.get("severity") or not rules(raw, node)["enabled"]:
                continue
            supplies, unread = power_of(conn, node, site)
            it = alert_item(node, h, supplies, unread, where=f" at {site}",
                            every_minutes=h.get("every_minutes") or 15)
            it.update({"key": f"clock:{site}:{node}", "site": f"{site}/{node}", "cluster": site,
                       "subject": f"{site}/{node} {it['subject']}"})
            it["details"] = dict(it["details"], cluster=site, as_of=h.get("read_at"), sent_by="hub")
            it["message"] += f" Sent by the hub for {site}."
            items.append(it)
    return items, held


def hub_clock_alerts(combined_path, cache_dir=None, config: dict | None = None,
                     now: datetime | None = None, failing=(), hub_sites=None) -> list[dict]:
    """Send what is due for the sites set to alerts = "hub"; returns what was sent."""
    from nomad.alerts.hub_nodes import hub_alert_sites
    sites = hub_alert_sites(config, hub_sites)
    if not sites:
        return []
    now = now or datetime.now()
    combined_path = Path(combined_path)
    try:
        conn = sqlite3.connect(f"file:{combined_path}?mode=ro", uri=True, timeout=30)
        try:
            items, held = due_clock_alerts(conn, sites, config, now, failing=set(failing or ()))
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    from nomad.alerts.once import send_once
    folder = Path(cache_dir) if cache_dir is not None else combined_path.parent
    return send_once(items, folder / STAMP, "clock", config, now, hold_sites=held)

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Power alerts the hub sends for sites that leave their alerts to it (1.25.8).

A site set to ``alerts = "hub"`` in [[hub.sites]] (vadur: a head node with no
way to the mail server) collects its boards' power readings (the power
collector) like any other; after each sync the hub reads them and sends what
the site's own collector would: one alert a host for each of "power supply",
"voltage" and "fans" while the problem lasts (once, again when worse, then
every ``[alerts] reminder_hours``), and one for each read's new problem entries
in a host's board log (once). A host whose last read is older than
STALE_HOURS, or wasn't read (its error), or whose site's pull failed, is held
as it was: what was sent about it isn't news again when it is read; the rest
of its site goes on. The hub's own ``[alerts.power]``
applies (enabled, fans, ignore).
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

STALE_HOURS = 2
EVENT_HOURS = 12
LOOK_DAYS = 7              # a site's log entries reaching the hub this late are still news
STAMP = "power_alerts.json"
SEEN = "power_events_seen.json"   # when the hub first saw each batch of log entries


def _time(text):
    try:
        return datetime.fromisoformat(str(text)) if text else None
    except ValueError:
        return None


def due_power_alerts(conn: sqlite3.Connection, sites, config: dict | None, now: datetime, failing=(),
                     seen: dict | None = None):
    """(items, held) for send_once: held names each host ("site/node") whose
    alerts are kept as they were (not read now, read too long ago, or its
    site's pull failed). seen: when the hub first saw each batch of log
    entries (kept between runs by the caller): a batch is due for a while
    from then, however late it reached the hub (a site not pulled for a day)."""
    seen = seen if seen is not None else {}
    from nomad.alerts.once import reminder_hours
    from nomad.collectors.power import alert_items, ignored
    rules = ((config or {}).get("alerts") or {}).get("power") or {}
    rules = rules if isinstance(rules, dict) else {}
    if rules.get("enabled") is False:
        return [], set()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if not {"power_hosts", "power_sensors"} <= tables:
        return [], set()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(power_hosts)")}
    if "source_site" not in cols:
        return [], set()
    # a batch of log entries is sent once: never again as a reminder (the window is shorter than one)
    window = timedelta(hours=min(EVENT_HOURS, max(0.5, reminder_hours(config) * 0.9)))
    items, held = [], set()
    for site in sites:
        hosts = conn.execute("SELECT node, read_at, error, host_problems FROM power_hosts WHERE source_site = ?",
                             (site,)).fetchall()
        for node, read_at, error, loose in hosts:
            at = _time(read_at)
            if site in failing or error or at is None or now - at > timedelta(hours=STALE_HOURS):
                held.add(f"{site}/{node}")
                continue
            found = [{"sensor": s, "subject": subj, "severity": sev, "problem": prob} for s, subj, sev, prob in
                     conn.execute("SELECT sensor, subject, severity, problem FROM power_sensors WHERE source_site = ? "
                                  "AND node = ? AND severity IS NOT NULL ORDER BY sensor", (site, node))
                     if not ignored(rules, node, s or "") and not (subj == "fans" and rules.get("fans") is False)]
            try:
                found += [dict(p, sensor=None) for p in json.loads(loose or "[]") if isinstance(p, dict)
                          and not any(ignored(rules, node, x) for x in (p.get("sensors") or ()))]
            except ValueError:
                pass
            found = [p for p in found if p.get("severity") in ("warning", "critical")]
            for it in alert_items(node, found, where=f" at {site}"):
                items.append(_for_hub(it, site, node, read_at, f"power:{site}:{node}:{it['subject']}"))
            if "power_events" not in tables:
                continue
            from nomad.collectors.power import event_sensor, kind_of, subject_of
            batches: dict = {}
            for key, sev, prob, logged, sensor, first in conn.execute(
                    "SELECT key, severity, problem, logged, sensor, first_seen FROM power_events WHERE source_site = ? "
                    "AND node = ? AND severity IS NOT NULL AND COALESCE(history, 0) = 0 AND first_seen >= ? "
                    "ORDER BY first_seen, id",
                    (site, node, (now - timedelta(days=LOOK_DAYS)).isoformat(timespec="seconds"))):
                typ, name = event_sensor({"sensor": sensor})
                if ignored(rules, node, name) or (rules.get("fans") is False
                                                  and subject_of(kind_of({"type": typ, "name": name})) == "fans"):
                    continue
                batches.setdefault(first, []).append({"severity": sev, "problem": prob, "logged": logged, "key": key})
            # each read's new entries once, as the site's collector sends them, for a while from when
            # the hub first saw them
            for first, events in batches.items():
                key = f"power-events:{site}:{node}:{first}"
                since = _time(seen.setdefault(key, now.isoformat(timespec="seconds"))) or now
                if now - since >= window:
                    continue
                for it in alert_items(node, [], events, where=f" at {site}"):
                    items.append(_for_hub(it, site, node, read_at, key))
    return items, held


def _for_hub(it: dict, site: str, node: str, read_at, key: str) -> dict:
    it.update({"key": key, "site": f"{site}/{node}", "cluster": site, "subject": f"{site}/{node} {it['subject']}"})
    it["details"] = dict(it["details"], cluster=site, as_of=read_at, sent_by="hub")
    it["message"] += f" Sent by the hub for {site}."
    return it


def hub_power_alerts(combined_path, cache_dir=None, config: dict | None = None,
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
        folder = Path(cache_dir) if cache_dir is not None else combined_path.parent
        try:
            seen = json.loads((folder / SEEN).read_text())
            seen = seen if isinstance(seen, dict) else {}
        except (OSError, ValueError):
            seen = {}
        try:
            items, held = due_power_alerts(conn, sites, config, now, failing=set(failing or ()), seen=seen)
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    cut = (now - timedelta(days=LOOK_DAYS + 1)).isoformat(timespec="seconds")
    try:
        tmp = folder / f"{SEEN}.{os.getpid()}"
        tmp.write_text(json.dumps({k: v for k, v in seen.items() if str(v) >= cut}, indent=1))
        os.replace(tmp, folder / SEEN)
    except OSError:
        pass
    from nomad.alerts.once import send_once
    stamp = folder / STAMP
    # held per host ("site/node", each item's site): one node not read keeps only its own alerts as they were
    return send_once(items, stamp, "power", config, now, hold_sites=held)

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Alerts the hub raises about the sites, sent once, again when worse, then
every ``[alerts] reminder_hours`` while the condition lasts.

What has been sent is kept in a small JSON file beside the hub's data, so it
holds whether or not the hub's nomad.toml names a database for alert_state. A
condition no longer due is forgotten, so its return is news.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

ORDER = {"info": 0, "warning": 1, "critical": 2}


def reminder_hours(config: dict | None) -> float:
    alerts = (config or {}).get("alerts")
    try:
        return float((alerts if isinstance(alerts, dict) else {}).get("reminder_hours", 24))
    except (TypeError, ValueError):
        return 24.0


def send_once(due: list[dict], stamp: Path, source: str, config: dict | None,
              now: datetime, hold_sites=()) -> list[dict]:
    """Send what is due and not yet sent (or worse, or due a reminder).

    due: dicts with key (the condition), host, subject, severity, message,
    details, and optionally site (for hold_sites) and cluster (the cluster
    the alert is about, when not the hub). Returns the ones sent. An alert
    every backend failed to send is tried again at the next run, or when the
    dispatcher's own retry is due (it holds the alert back until then:
    dispatcher.Held, not noted as sent). What was sent
    about a site in hold_sites (one not read this time) is kept as it is
    rather than forgotten, so it isn't sent again as news when it is read."""
    from nomad.alerts.dispatcher import Held, send_alert
    stamp = Path(stamp)
    try:
        sent = json.loads(stamp.read_text())
        if not isinstance(sent, dict):
            sent = {}
    except (OSError, ValueError):
        sent = {}
    every = reminder_hours(config) * 3600
    raised, keep = [], {}
    for item in due:
        key, severity = item["key"], item["severity"]
        prev = sent.get(key) if isinstance(sent.get(key), dict) else None
        try:
            when = datetime.fromisoformat(prev["sent"]) if prev and prev.get("sent") else None
        except (TypeError, ValueError):
            when = None
        if (prev is None or ORDER.get(severity, 0) > ORDER.get(prev.get("severity"), 0)
                or when is None or (now - when).total_seconds() >= every):
            extra = {"site": item["cluster"]} if item.get("cluster") else {}
            result = send_alert(severity, source, item["message"], host=item.get("host"),
                                subject=item.get("subject"), details=item.get("details"),
                                config=config, **extra)
            if isinstance(result, Held):
                # The dispatcher holds it back (a retry after a failed send
                # not due yet, say): nothing went out, so it isn't noted as
                # sent; asked again next run.
                if prev is not None:
                    keep[key] = prev
                continue
            raised.append(item)
            if not result or any(result.values()):
                keep[key] = {"severity": severity, "sent": now.isoformat()}
                if item.get("site") is not None:
                    keep[key]["site"] = item["site"]
            elif prev is not None:
                keep[key] = prev
        else:
            keep[key] = prev
    held = set(hold_sites or ())
    for key, prev in sent.items():
        if key not in keep and isinstance(prev, dict) and prev.get("site") in held:
            keep[key] = prev
    try:
        if keep or sent:
            tmp = stamp.with_name(f"{stamp.name}.{os.getpid()}")
            tmp.write_text(json.dumps(keep, indent=1))
            os.replace(tmp, stamp)
    except OSError:
        pass
    return raised

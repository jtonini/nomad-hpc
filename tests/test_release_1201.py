# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.20.1: Signals place an alert by what it is *about*, not where it was
stored. The hub raises a site's node alerts on its behalf (1.17.3); such an
alert lives in the hub's own rows (source_site = the hub) but names the site
it is about in details.site. Attributing it by source_site showed vadur's
"node down" under the hub (mingus) and hid it from vadur. The effective site
is details.site when set, else source_site (or the lone site on a single
database)."""
import json
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from nomad.insights import signals as S


def _combined(rows):
    """A combined database's `alerts` table (it carries source_site, as a
    real `nomad sync` writes it) filled with (severity, host, message,
    details_site, source_site) rows at 'now'."""
    p = Path(tempfile.mkdtemp()) / "combined.db"
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE alerts (id INTEGER PRIMARY KEY, timestamp TEXT,
                 severity TEXT, category TEXT, source TEXT, host TEXT,
                 message TEXT, details TEXT, dedup_key TEXT, source_site TEXT)""")
    ts = datetime.now().isoformat()
    for sev, host, msg, dsite, ssite in rows:
        det = json.dumps({"site": dsite}) if dsite else "{}"
        dedup = f"node_state|{host}|{dsite}/{host}" if dsite else None
        c.execute("INSERT INTO alerts (timestamp, severity, category, source, host, "
                  "message, details, dedup_key, source_site) VALUES (?,?,?,?,?,?,?,?,?)",
                  (ts, sev, "node_state", "node_state", host, msg, det, dedup, ssite))
    c.commit()
    c.close()
    return p


def _msgs(db, site):
    sig = S.read_alert_signals(db, hours=168, site=site)
    return sig[0].metrics["messages"] if sig else []


def test_hub_raised_alert_shows_under_the_site_it_is_about_not_the_hub():
    # mingus is the hub; it raised vadur's node-down on vadur's behalf, so the
    # row is mingus's own (source_site="mingus") but about vadur (details.site).
    db = _combined([
        ("warning", "mingus01", "/scratch at 90%", "mingus", "mingus"),
        ("critical", "node03", "Node node03 at vadur is DOWN; sent by the hub for vadur.",
         "vadur", "mingus"),
        ("warning", "vadurfs", "/home at 85%", "vadur", "vadur"),
    ])
    mingus = _msgs(db, "mingus")
    vadur = _msgs(db, "vadur")
    # The hub's Signals are about the hub only.
    assert any("/scratch" in m for m in mingus)
    assert not any("vadur" in m for m in mingus)
    # vadur's own Signals carry the node-down the hub sent for it, and its storage.
    assert any("node03" in m and "DOWN" in m for m in vadur)
    assert any("/home" in m for m in vadur)


def test_source_site_is_the_fallback_when_details_names_no_site():
    # An alert with no details.site falls back to source_site, so it is still
    # placed on a combined database (older hub-merged alerts).
    db = _combined([
        ("warning", "a01", "A down", None, "alpha"),
        ("warning", "b01", "B down", None, "beta"),
    ])
    assert _msgs(db, "alpha") == ["A down"]
    assert _msgs(db, "beta") == ["B down"]


def test_pooled_read_counts_every_alert_once():
    db = _combined([
        ("warning", "mingus01", "/scratch at 90%", "mingus", "mingus"),
        ("critical", "node03", "Node node03 at vadur is DOWN.", "vadur", "mingus"),
        ("warning", "vadurfs", "/home at 85%", "vadur", "vadur"),
    ])
    sig = S.read_alert_signals(db, hours=168, site=None)
    assert sig and sig[0].metrics["total"] == 3

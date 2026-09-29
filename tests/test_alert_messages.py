# SPDX-License-Identifier: AGPL-3.0-or-later
"""Alert messages say which cluster they come from, when, and what happened.

Every site mails the same inbox; a subject of "[WARNING] NØMAÐ Alert: disk"
and a time of "2026-09-29T17:33:41.698908" didn't say which cluster's disk.
"""

import json
import sqlite3

from nomad.alerts.backends import EmailBackend, readable_time
from nomad.alerts.dispatcher import AlertDispatcher

ALERT = {"severity": "warning", "source": "disk", "host": "spydur",
         "message": "Disk /scratch at 85.0% (threshold: 80%)",
         "timestamp": "2026-09-29T17:33:41.698908", "site": "spydur"}


def _backend():
    return EmailBackend({"enabled": True, "recipients": ["a@example.org"]},
                        {"host": "localhost", "port": 25})


def test_subject_names_the_cluster_and_the_problem():
    assert _backend()._format_subject(ALERT) == \
        "[WARNING] NØMAÐ spydur: Disk /scratch at 85.0% (threshold: 80%)"
    long = dict(ALERT, message="x" * 300, site=None)
    s = _backend()._format_subject(long)
    assert s.startswith("[WARNING] NØMAÐ: xxx") and len(s) < 130


def test_body_has_cluster_and_readable_time():
    b = _backend()
    text, page = b._format_text(ALERT), b._format_html(ALERT)
    assert "Cluster: spydur" in text and "2026-09-29 17:33" in text
    assert "T17:33:41" not in text
    assert "<strong>Cluster:</strong> spydur" in page and "2026-09-29 17:33" in page


def test_html_is_escaped():
    page = _backend()._format_html(dict(ALERT, message="Node n1 is DOWN (<b>bad</b> & worse)"))
    assert "&lt;b&gt;bad&lt;/b&gt; &amp; worse" in page and "<b>bad</b>" not in page


def test_readable_time():
    assert readable_time("2026-09-29T17:33:41.698908").startswith("2026-09-29 17:33")
    assert readable_time(None) == "unknown" and readable_time("garbage") == "garbage"


def test_dispatcher_stamps_the_site(tmp_path):
    db = tmp_path / "s.db"
    c = sqlite3.connect(db)
    c.execute("""CREATE TABLE alerts (id INTEGER PRIMARY KEY, timestamp TEXT, severity TEXT NOT NULL,
        category TEXT NOT NULL, source TEXT, message TEXT NOT NULL, details TEXT, dedup_key TEXT)""")
    c.commit(); c.close()
    sent = []

    class B:
        def send(self, alert):
            sent.append(alert)
            return True

    d = AlertDispatcher({"clusters": {"spydur": {"name": "spydur"}},
                         "database": {"path": db.name}, "general": {"data_dir": str(tmp_path)}})
    d.backends = [B()]
    d.dispatch({"severity": "warning", "source": "disk", "host": "spdr-head", "message": "m"})
    assert sent[0]["site"] == "spydur"
    c = sqlite3.connect(db)
    details = json.loads(c.execute("SELECT details FROM alerts").fetchone()[0])
    assert details == {"host": "spdr-head", "site": "spydur"}

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.37.0: a site watches its hub (nomad.hub_watch), and declared maintenance
windows hold the alerts about a site, the hub's and its own (nomad.maintenance)."""
from __future__ import annotations

import ast
import io
import json
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from nomad import hub_watch as W
from nomad import maintenance as M
from nomad.alerts import dispatcher as D
from nomad.alerts import once as O
from nomad.db import delta_hub as H
from nomad.db import delta_site as S
from tests.test_alert_episodes import _Backend, _db
from tests.test_sync_incremental import _cli_hub

NOW = datetime(2026, 11, 10, 14, 0, 0)
T = NOW.timestamp()
HOUR, DAY = 3600, 86400


@pytest.fixture(autouse=True)
def no_notes():
    yield
    H.set_notes(None)


def site(tmp_path, name="site.db"):
    p = tmp_path / name
    with sqlite3.connect(p) as c:
        c.execute("CREATE TABLE IF NOT EXISTS samples (id INTEGER PRIMARY KEY, v REAL)")
    return p


def call(db, *args):
    """The exporter as the hub runs it at a site; its first record."""
    out = io.BytesIO()
    assert S.main(["--db", str(db), *args], out=out) == 0
    recs = list(S.read(out.getvalue()))
    return recs[0] if recs else None


def notes(db):
    with sqlite3.connect(db) as c:
        try:
            return {r[0]: r[1:] for r in c.execute(
                f"SELECT hub, name, first, seen, reads, quiet, maint, reason FROM {S.HUBS}")}
        except sqlite3.OperationalError:
            return {}                                  # the last hub left: the notes went with the log


# -- what a hub leaves at a site -----------------------------------------------------------------

def test_a_window_a_site_takes():
    assert S.window("100,200") == "100,200,200"
    assert S.window("100,200,260") == "100,200,260"
    assert S.window("100.9,200.2") == "100,200,200"
    assert S.window("200,100") is None and S.window("100,100") is None
    assert S.window(f"0,{S.MAX_WINDOW + 1}") is None                 # a window has to end
    assert S.window(f"0,{14 * DAY + HOUR}") is not None              # 14 days over a clock change
    assert S.window(f"100,200,{200 + S.MAX_HELD + 1}") == "100,200,200"   # held too long: to its end
    assert S.window("100,200,150") == "100,200,200"
    for bad in ("", None, "x,y", "1", "1,2,3,4", "-5,10"):
        assert S.window(bad) is None, bad


def test_the_hub_leaves_its_notes_with_each_read(tmp_path):
    db = site(tmp_path)
    assert call(db, "--hub", "h1", "--init", "--hub-name", "mingus")["init"] is True
    first = notes(db)["h1"]
    assert first[0] == "mingus" and first[3] == 1 and first[4:] == (None, None, None)
    epoch = call(db, "--hub", "h1", "--init")["epoch"]               # says nothing of itself: not noted
    assert notes(db)["h1"][3] == 1
    got = call(db, "--hub", "h1", "--since", "0", "--epoch", epoch, "--hub-name", "mingus",
               "--quiet", "1000,2000", "--maint", "3000,4000,4600", "--reason", "OS   patches\n")
    assert got["h"] == "nomad-delta"
    name, start, seen, reads, quiet, maint, reason = notes(db)["h1"]
    assert (name, reads, quiet, maint, reason) == ("mingus", 2, "1000,2000,2000", "3000,4000,4600", "OS patches")
    assert start == first[1] and seen >= start                       # when it first read is kept
    # what it does not say this time is no longer so; a reason without a window is dropped
    call(db, "--hub", "h1", "--since", "0", "--epoch", epoch, "--hub-name", "mingus", "--reason", "x")
    assert notes(db)["h1"][3:] == (3, None, None, None)
    # alone, without reading (a window just declared): answered as kept, and not counted as a read
    before = int(time.time()) - 500
    with sqlite3.connect(db) as c:
        c.execute(f"UPDATE {S.HUBS} SET seen = ? WHERE hub = 'h1'", (before,))
    assert call(db, "--hub", "h1", "--note", "--hub-name", "mingus", "--maint", "10,20") == {
        "noted": True, "quiet": None, "maint": "10,20,20"}
    assert notes(db)["h1"][2:] == (before, 3, None, "10,20,20", None)
    # one this site does not take (too long) is answered as not taken
    assert call(db, "--hub", "h1", "--note", "--hub-name", "mingus", "--maint", f"0,{S.MAX_WINDOW + 1}",
                "--quiet", f"0,{S.MAX_WINDOW + 1}") == {"noted": True, "quiet": None, "maint": None}
    # a reason may start with a dash, said as one word
    call(db, "--hub", "h1", "--note", "--hub-name=mingus", "--maint=10,20", "--reason=--force reboot")
    assert notes(db)["h1"][6] == "--force reboot"
    # another hub keeps its own; one that leaves is forgotten; a retired one too
    call(db, "--hub", "h2", "--note", "--hub-name", "trial")
    assert notes(db)["h2"][1:4] == (None, None, 0)                   # it said so, and never read
    with sqlite3.connect(db) as c:
        c.execute(f"INSERT INTO {S.HUBS} (hub, name, first, seen, reads) VALUES ('old', 'gone', 1, 2, 9)")
        # one silent for longer, but away on purpose until yesterday: still one to report
        c.execute(f"INSERT INTO {S.HUBS} (hub, name, first, seen, reads, quiet) VALUES ('away', 'b', 1, ?, 9, ?)",
                  (before - 10 * DAY, f"{before - 10 * DAY},{before - DAY},{before - DAY}"))
    call(db, "--hub", "h2", "--forget")
    assert set(notes(db)) == {"h1", "old", "away"}
    call(db, "--hub", "h1", "--note", "--hub-name", "mingus")
    assert set(notes(db)) == {"h1", "away"}
    with sqlite3.connect(db) as c:
        c.execute(f"UPDATE {S.HUBS} SET quiet = 'x,y' WHERE hub = 'away'")       # not a window: by its last read
    call(db, "--hub", "h1", "--note", "--hub-name", "mingus")
    assert set(notes(db)) == {"h1"}
    # a log no hub read for a week goes; what the hubs said stays (one away that long on purpose)
    with sqlite3.connect(db, isolation_level=None) as c:
        assert S.expire(c, now=time.time() + 8 * DAY) == "removed"
    assert set(notes(db)) == {"h1"}
    # the log taken out by the hub (going back to whole copies): the notes go with it
    call(db, "--hub", "h1", "--init", "--hub-name", "mingus")
    call(db, "--remove")
    with sqlite3.connect(db) as c:
        assert not c.execute("SELECT name FROM sqlite_master WHERE name LIKE '\\_nomad\\_%' ESCAPE '\\'").fetchall()
    ast.parse(Path(S.__file__).read_text(), feature_version=(3, 6))  # runs on the site's own python3


def test_the_hub_passes_its_notes_to_every_read(tmp_path):
    import socket
    db = site(tmp_path)
    s = {"name": "alpha", "host": "localhost", "db_path": str(db)}
    seen = []
    H.set_notes(lambda site_: seen.append(site_["name"]) or ["--hub-name=mingus", "--maint=10,20,30",
                                                              "--reason=-disk swap"])
    H.init_site(s, hub="h1")
    assert notes(db)["h1"][0] == "mingus" and notes(db)["h1"][5:] == ("10,20,30", "-disk swap")
    H.forget_at_site(s, "h1")                                        # leaving: nothing said, the note gone
    assert seen == ["alpha"] and notes(db) == {}
    # a provider that fails still says who read: the site does not take the hub for one that stopped
    H.set_notes(lambda site_: 1 / 0)
    assert H.init_site(s, hub="h1")["init"] is True
    assert notes(db)["h1"][0] == socket.gethostname().split(".")[0] and notes(db)["h1"][3] == 1
    # notes the site's parser can't read stop that read, as an error the sync reports (not an exit)
    H.set_notes(lambda site_: ["--reason", "-x"])
    with pytest.raises(H.DeltaError, match="refused its arguments"):
        H.run_site(s, ["--hub", "h1", "--init"], 30)
    H.set_notes(None)
    assert H.tell_site(s, "h1", ["--hub-name=mingus", "--quiet=5,9"]) == {
        "noted": True, "quiet": "5,9,9", "maint": None}
    assert notes(db)["h1"][4] == "5,9,9" and notes(db)["h1"][3] == 1  # told, not read
    assert H.tell_site(s, "h1", ["--hub-name=mingus", f"--maint=0,{S.MAX_WINDOW + 1}"])["maint"] is None
    assert H.tell_site({"name": "x", "host": "localhost", "db_path": str(tmp_path / "none.db")}, "h1", []) is None


# -- a site watches its hub ----------------------------------------------------------------------

def hub(seen, reads=4320, first=None, quiet=None, maint=None, name="mingus"):
    """A hub that has read every ten minutes for a month (4320 reads)."""
    return W.Hub("h1", name, T - 30 * DAY if first is None else first, seen, reads, quiet, maint, None)


def test_a_hub_that_stopped_reading():
    s = W.Settings()
    assert W.due([hub(T - 59 * 60)], s, T, "spydur") == []
    (it,) = W.due([hub(T - 61 * 60)], s, T, "spydur")
    assert it["severity"] == "warning" and it["key"] == "hub:h1" and it["subject"] == "hub mingus"
    assert it["message"].startswith("The hub mingus has not read spydur for 1 h (last at 2026-11-10 12:59)")
    assert it["details"]["metric"] == "hub_read" and it["details"]["last_read"] == "2026-11-10T12:59:00"
    assert W.due([hub(T - 25 * HOUR)], s, T, "spydur")[0]["severity"] == "critical"
    late = W.due([hub(T - 6 * DAY)], s, T, "spydur")[0]["message"]
    assert "After 7 days of silence this site takes it as retired" in late
    assert W.due([hub(T - 8 * DAY)], s, T, "spydur") == []            # retired
    # not a hub to miss yet: a few reads, or all within a day (a trial sync)
    assert W.due([hub(T - 5 * HOUR, reads=W.REGULAR_READS - 1)], s, T, "spydur") == []
    assert W.due([hub(T - 5 * HOUR, first=T - 20 * HOUR)], s, T, "spydur") == []
    # one that reads now and then is given three of its usual intervals (20 reads in 30 days: 38 h apart)
    seldom = hub(T - 2 * DAY, reads=20, first=T - 32 * DAY)
    assert 37 * HOUR < seldom.usual_gap < 39 * HOUR and W.due([seldom], s, T, "spydur") == []
    assert len(W.due([hub(T - 5 * DAY, reads=20, first=T - 35 * DAY)], s, T, "spydur")) == 1
    # off, or another threshold
    assert W.due([hub(T - 5 * HOUR)], W.settings({"alerts": {"hub": {"enabled": False}}}), T, "spydur") == []
    assert W.due([hub(T - 2 * HOUR)], W.settings({"alerts": {"hub": {"after_minutes": 180}}}), T, "spydur") == []
    assert W.settings({"alerts": {"hub": {"after_minutes": "x"}}}).after_minutes == 60
    assert W.settings({"alerts": {"hub": {"after_minutes": 0}}}).after_minutes == 60
    assert W.settings({"alerts": "x"}).enabled and W.settings(None).hold
    # the same hub reading under another id (its combined database moved) has not stopped
    moved = W.Hub("h2", "mingus", T - HOUR, T - 5 * 60, 3, None, None, None)
    assert W.due([hub(T - 5 * HOUR), moved], s, T, "spydur") == []
    other = W.Hub("h2", "badenpowell", T - HOUR, T - 5 * 60, 3, None, None, None)
    assert len(W.due([hub(T - 5 * HOUR), other], s, T, "spydur")) == 1
    nameless = W.due([W.Hub("h1", None, T - 30 * DAY, T - 2 * HOUR, 4320, None, None, None)], s, T, "c1")[0]
    assert nameless["subject"] == "its hub" and nameless["message"].startswith("Its hub has not read c1 for 2 h")


def test_only_silence_the_site_saw_counts(tmp_path):
    s = W.Settings()
    # the site itself was down: the hub is given its hour again from when collection resumed
    assert W.due([hub(T - 5 * HOUR)], s, T, "spydur", watching_since=T - 10 * 60) == []
    (it,) = W.due([hub(T - 5 * HOUR)], s, T, "spydur", watching_since=T - 61 * 60)
    assert "for 5 h" in it["message"] and it["severity"] == "warning"
    assert W.due([hub(T - 30 * HOUR)], s, T, "spydur", watching_since=T - 2 * HOUR)[0]["severity"] == "warning"
    assert W.due([hub(T - 30 * HOUR)], s, T, "spydur")[0]["severity"] == "critical"
    # since when collection has run without a break
    assert W._watching_since(tmp_path, 1000.0) == 1000.0             # the first run
    assert W._watching_since(tmp_path, 1060.0) == 1000.0
    assert W._watching_since(tmp_path, 1060.0 + W.BREAK_MINUTES * 60) == 1000.0
    back = 1660.0 + W.BREAK_MINUTES * 60 + 1
    assert W._watching_since(tmp_path, back) == back                 # a break: from when it resumed
    assert W._watching_since(tmp_path, back + 60) == back
    assert W._watching_since(tmp_path, back - 500) == back - 500     # the clock went back
    (tmp_path / W.STATE).write_text("{not json")
    assert W._watching_since(tmp_path, 5000.0) == 5000.0
    # a site that collects once an hour: watched once that is known as its usual step
    hourly = tmp_path / "hourly"
    hourly.mkdir()
    got = [W._watching_since(hourly, 1000.0 + i * HOUR) for i in range(6)]
    assert got[:4] == [1000.0 + i * HOUR for i in range(4)] and got[4] == got[3] == got[5]
    assert W._watching_since(hourly, 1000.0 + 5 * HOUR + 4 * HOUR) == 1000.0 + 9 * HOUR   # down for hours


def test_a_read_that_left_no_note_is_a_read(tmp_path):
    db = site(tmp_path)
    call(db, "--hub", "h1", "--init", "--hub-name", "mingus")
    with sqlite3.connect(db) as c:
        c.execute(f"UPDATE {S.HUBS} SET first = ?, seen = ?, reads = 4320", (T - 30 * DAY, T - 5 * HOUR))
        c.execute(f"UPDATE {S.META} SET value = ? WHERE key = 'heard:h1'", (str(int(T - 600)),))
        (h,) = W.hubs(c)
        assert h.seen == T - 600 and h.noted == T - 5 * HOUR         # the log's own record of its read
        assert W.due([h], W.Settings(), T, "spydur") == []
        c.execute(f"UPDATE {S.META} SET value = 'x' WHERE key = 'heard:h1'")
        assert W.hubs(c)[0].seen == T - 5 * HOUR
    assert [h.label for h in W.read_hubs(db)] == ["the hub mingus"]
    assert W.read_hubs(tmp_path / "none.db") == []
    # a database that can't be read just now is not one no hub reads
    closed = sqlite3.connect(db)
    closed.close()
    with pytest.raises(W.Unreadable):
        W.hubs(closed)


def test_a_hub_that_said_it_would_be_away():
    s = W.Settings()
    away = (T - 3 * HOUR, T + HOUR, T + HOUR)
    assert W.due([hub(T - 3 * HOUR, quiet=away)], s, T, "spydur") == []            # in its window
    over = (T - 5 * HOUR, T - 30 * 60, T - 30 * 60)
    assert W.due([hub(T - 5 * HOUR, quiet=over)], s, T, "spydur") == []            # half an hour to come back
    over = (T - 5 * HOUR, T - 61 * 60, T - 61 * 60)
    (it,) = W.due([hub(T - 5 * HOUR, quiet=over)], s, T, "spydur")
    assert "for 5 h" in it["message"]
    assert "The maintenance it declared for itself ended at 2026-11-10 12:59." in it["message"]
    # it read again after its window, then stopped: its window says nothing of that
    (it,) = W.due([hub(T - 2 * HOUR, quiet=(T - 9 * HOUR, T - 3 * HOUR, T - 3 * HOUR))], s, T, "spydur")
    assert "declared for itself" not in it["message"]
    # a window still to come changes nothing; silent before it starts is said
    assert len(W.due([hub(T - 2 * HOUR, quiet=(T + HOUR, T + 2 * HOUR, T + 2 * HOUR))], s, T, "spydur")) == 1
    # away for ten days and still silent after: said then, not taken as retired on its return date
    long_ = (T - 10 * DAY, T - 2 * HOUR, T - 2 * HOUR)
    (it,) = W.due([hub(T - 10 * DAY - 300, quiet=long_)], s, T, "spydur")
    assert it["severity"] == "warning" and "retired" not in it["message"] and "for 10 days" in it["message"]
    gone = (T - 20 * DAY, T - 8 * DAY, T - 8 * DAY)
    assert W.due([hub(T - 20 * DAY - 300, quiet=gone)], s, T, "spydur") == []      # a week past its window
    # this site in maintenance: it may be the one that can't be reached
    here = (T - HOUR, T + HOUR, T + 2 * HOUR)
    assert W.due([hub(T - 5 * HOUR, maint=here)], s, T, "spydur") == []


def test_the_site_s_own_window(tmp_path):
    db = site(tmp_path)
    assert W.site_window(db) is None and W.site_window(tmp_path / "none.db") is None
    win = f"{int(T - HOUR)},{int(T + HOUR)},{int(T + 2 * HOUR)}"
    call(db, "--hub", "h1", "--note", "--hub-name", "mingus", "--maint", win, "--reason", "OS patches")
    assert W.site_window(db, now=T) is None                          # a hub that never read it: not taken
    call(db, "--hub", "h1", "--init", "--hub-name", "mingus", "--maint", win, "--reason", "OS patches")
    with sqlite3.connect(db) as c:
        c.execute(f"UPDATE {S.HUBS} SET seen = ?", (T - 300,))
    w = W.site_window(db, now=T)
    assert w == {"start": int(T - HOUR), "until": int(T + HOUR), "held": int(T + 2 * HOUR),
                 "reason": "OS patches", "hub": "mingus"}
    assert W.site_window(db, now=T + 90 * 60)["hub"] == "mingus"      # the grace after it
    assert W.site_window(db, now=T + 2 * HOUR) is None and W.site_window(db, now=T - 2 * HOUR) is None
    # the hub that declared it stopped reading: after a day its word no longer holds the site's alerts
    days = (T - 30 * HOUR, T + 10 * DAY, T + 10 * DAY)
    assert W._site_window([hub(T - 23 * HOUR, maint=days)], T)["until"] == T + 10 * DAY
    assert W._site_window([hub(T - 25 * HOUR, maint=days)], T) is None
    assert W.due([hub(T - 25 * HOUR, maint=days)], W.Settings(), T, "spydur")[0]["severity"] == "critical"
    # the same hub under two ids (its combined database moved): the one that read last speaks
    stale = W.Hub("h0", "mingus", T - 30 * DAY, T - 2 * HOUR, 4000, None, days, "old window")
    fresh = W.Hub("h1", "mingus", T - HOUR, T - 300, 12, None, None, None)
    assert W._site_window([stale, fresh], T) is None
    # can't be read just now: the alert goes out
    import unittest.mock as mock
    with mock.patch.object(W, "read_hubs", side_effect=W.Unreadable("locked")):
        assert W.site_window(db, now=T) is None


def test_watch_says_so_once(tmp_path, monkeypatch):
    db = site(tmp_path)
    sent = []
    monkeypatch.setattr(D, "send_alert", lambda *a, **k: sent.append((a, k)) or {"Email": True})
    assert W.watch(db, {}, NOW) == [] and not (tmp_path / W.STAMP).exists()     # no hub reads it
    assert W.watch(tmp_path / "none.db", {}, NOW) == []
    with sqlite3.connect(db) as c:
        c.execute(f"CREATE TABLE {S.HUBS} (hub TEXT PRIMARY KEY, name TEXT, first INTEGER, seen INTEGER, "
                  "reads INTEGER, quiet TEXT, maint TEXT, reason TEXT, noted INTEGER)")
        c.execute(f"INSERT INTO {S.HUBS} VALUES ('h1', 'mingus', ?, ?, 5000, NULL, NULL, NULL, NULL)",
                  (T - 30 * DAY, T - 2 * HOUR))
    # collection starts (or resumes) here: the hub has an hour from now, whatever it did before
    for m in range(0, 60, 5):
        assert W.watch(db, {}, NOW + timedelta(minutes=m)) == [], m
    (it,) = W.watch(db, {}, NOW + timedelta(minutes=60))
    assert "The hub mingus has not read" in it["message"] and "for 3 h" in it["message"]
    (args, kw), = sent
    assert args[:2] == ("warning", "hub") and kw["hold"] is False and kw["subject"] == "hub mingus"
    assert W.watch(db, {}, NOW + timedelta(minutes=65)) == []                   # said once
    # can't be read this time: nothing said, and what was said is not forgotten
    monkeypatch.setattr(W, "read_hubs", lambda p: (_ for _ in ()).throw(W.Unreadable("locked")))
    assert W.watch(db, {}, NOW + timedelta(minutes=70)) == []
    assert set(json.loads((tmp_path / W.STAMP).read_text())) == {"hub:h1"}
    monkeypatch.undo()
    monkeypatch.setattr(D, "send_alert", lambda *a, **k: sent.append((a, k)) or {"Email": True})
    (tmp_path / W.STATE).write_text(json.dumps({"last_run": T + 24 * HOUR - 60, "since": T, "steps": [60] * 5}))
    (worse,) = W.watch(db, {}, NOW + timedelta(hours=24))                       # critical: said again
    assert worse["severity"] == "critical"
    with sqlite3.connect(db) as c:
        c.execute(f"UPDATE {S.HUBS} SET seen = ?", (T + 24 * HOUR,))
    assert W.watch(db, {}, NOW + timedelta(hours=24, minutes=5)) == []          # it reads again: forgotten
    assert json.loads((tmp_path / W.STAMP).read_text()) == {}
    with sqlite3.connect(db) as c:
        c.execute(f"UPDATE {S.HUBS} SET seen = 'x'")
    assert W.watch(db, {}, NOW) == []                                           # never stops collection


def test_collect_watches_the_hub(tmp_path, monkeypatch):
    import nomad.cli as cli_mod
    seen = []
    monkeypatch.setattr(W, "watch", lambda db, config, now=None: seen.append(db) or [{"message": "The hub is quiet"}])
    cli_mod._watch_hub("x.db", {})
    assert seen == ["x.db"]
    monkeypatch.setattr(W, "watch", lambda *a, **k: 1 / 0)
    cli_mod._watch_hub("x.db", {})                                              # never raises


# -- a site in maintenance holds its own alerts ------------------------------------------------------

def _dispatch(path, backend, alerts_cfg=None, hold=True, **alert):
    d = D.AlertDispatcher({"database": {"path": path.name}, "general": {"data_dir": str(path.parent)},
                           "alerts": alerts_cfg or {}})
    d.backends = [backend]
    a = {"severity": "critical", "source": "node_state", "host": "spdr05", "subject": "spdr05",
         "message": "Node spdr05 is DOWN"}
    a.update(alert)
    return d.dispatch(a, hold=hold)


def test_a_site_in_maintenance_holds_its_alerts(tmp_path):
    path = _db(tmp_path)
    b = _Backend()
    now = time.time()
    win = f"{int(now - 60)},{int(now + 3600)},{int(now + 7200)}"
    call(path, "--hub", "h1", "--init", "--hub-name", "mingus", "--maint", win, "--reason", "OS patches")
    got = _dispatch(path, b)
    assert isinstance(got, D.Held) and b.sent == []
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT COUNT(*) FROM alerts").fetchone() == (0,)      # not stored, not counted as raised
        assert c.execute("SELECT COUNT(*) FROM alert_state").fetchone() == (0,)
    _dispatch(path, b, source="test", message="a test")                         # a test is always sent
    assert [a["message"] for a in b.sent] == ["a test"]
    _dispatch(path, b, hold=False, source="hub", subject="hub mingus")          # not about this site's state
    _dispatch(path, b, alerts_cfg={"maintenance_hold": False}, subject="spdr06", host="spdr06")
    assert len(b.sent) == 3
    # the hub that declared it has not read for a day: its word no longer holds this site's alerts
    with sqlite3.connect(path) as c:
        c.execute(f"UPDATE {S.HUBS} SET seen = ?", (int(now - 25 * HOUR),))
        c.execute(f"UPDATE {S.META} SET value = ? WHERE key = 'heard:h1'", (str(int(now - 25 * HOUR)),))
    assert _dispatch(path, b, subject="spdr07", host="spdr07") and len(b.sent) == 4
    with sqlite3.connect(path) as c:
        c.execute(f"UPDATE {S.HUBS} SET seen = ?", (int(now),))
    assert isinstance(_dispatch(path, b), D.Held)
    # the window over: what still holds is raised then, as news
    call(path, "--hub", "h1", "--note", "--hub-name", "mingus")
    assert _dispatch(path, b) and b.sent[-1]["message"] == "Node spdr05 is DOWN"


# -- declaring maintenance ---------------------------------------------------------------------------

def test_a_time_as_someone_types_it():
    assert M.parse_when("2026-11-12 18:00", NOW) == datetime(2026, 11, 12, 18, 0)
    assert M.parse_when("2026-11-12T18:00", NOW) == datetime(2026, 11, 12, 18, 0)
    assert M.parse_when("2026-11-12", NOW) == datetime(2026, 11, 12)
    assert M.parse_when("18:00", NOW) == datetime(2026, 11, 10, 18, 0)
    assert M.parse_when("9:30", NOW) == datetime(2026, 11, 11, 9, 30)           # passed today: tomorrow
    assert M.parse_when("+4h", NOW) == NOW + timedelta(hours=4)
    assert M.parse_when("+90m", NOW) == NOW + timedelta(minutes=90)
    assert M.parse_when(" +2 d ", NOW) == NOW + timedelta(days=2)
    assert M.parse_when("+14d", NOW).timestamp() - T == 14 * DAY                # by the clock
    for bad in ("", "soon", "25:00", "+4w", "12/11/2026"):
        with pytest.raises(M.MaintenanceError):
            M.parse_when(bad, NOW)


def test_declaring_a_window(tmp_path):
    f = tmp_path / "m" / "maintenance.json"
    kw = {"now": NOW, "path": f, "known": ["spydur", "arachne"]}
    with pytest.raises(M.MaintenanceError, match="say why"):
        M.declare("spydur", NOW + timedelta(hours=4), "  ", **kw)
    with pytest.raises(M.MaintenanceError, match="has passed"):
        M.declare("spydur", NOW - timedelta(hours=1), "x", **kw)
    with pytest.raises(M.MaintenanceError, match="before it starts"):
        M.declare("spydur", NOW + timedelta(hours=1), "x", start=NOW + timedelta(hours=2), **kw)
    with pytest.raises(M.MaintenanceError, match="longer than 14 days"):
        M.declare("spydur", NOW + timedelta(days=15), "x", **kw)
    with pytest.raises(M.MaintenanceError, match="longer than 2 days"):
        M.declare("spydur", NOW + timedelta(days=3), "x", config={"hub": {"maintenance_max_days": 2}}, **kw)
    with pytest.raises(M.MaintenanceError, match="not one of this hub's sites \\(arachne, spydur, or hub\\)"):
        M.declare("spydr", NOW + timedelta(hours=1), "x", **kw)
    assert not f.exists()
    new, replaced = M.declare("spydur", NOW + timedelta(hours=4), "OS  patches\n", by="jtonini", write=False, **kw)
    assert new.site == "spydur" and replaced is None and not f.exists()         # checked, not declared
    new, replaced = M.declare("spydur", NOW + timedelta(hours=4), "OS  patches\n", by="jtonini", **kw)
    assert (new.site, new.start, new.until, new.reason, new.by) == (
        "spydur", "2026-11-10T14:00:00", "2026-11-10T18:00:00", "OS patches", "jtonini") and replaced is None
    M.declare("hub", NOW + timedelta(days=2, hours=2), "new disk", start=NOW + timedelta(days=2), **kw)
    assert set(M.current(now=NOW, path=f)) == {"spydur"}
    assert set(M.held(now=NOW + timedelta(hours=4, minutes=59), path=f)) == {"spydur"}     # the grace after it
    assert M.held(now=NOW + timedelta(hours=5), path=f) == {}
    assert M.current(now=NOW + timedelta(hours=4), path=f) == {}
    assert M.ended_lately(now=NOW + timedelta(hours=4, minutes=30), path=f) == {}           # still held
    assert M.ended_lately(now=NOW + timedelta(hours=6), path=f) == {"spydur": datetime(2026, 11, 10, 18, 0)}
    assert M.ended_lately(now=NOW + timedelta(hours=30), path=f) == {}
    assert M.held({"hub": {"maintenance_grace_minutes": 0}}, NOW + timedelta(hours=4), f) == {}
    # declaring again changes it: the one running is kept as ended, one not started leaves no trace
    later, replaced = M.declare("spydur", NOW + timedelta(hours=9), "patches run long", **{**kw, "now": NOW + timedelta(hours=1)})
    assert replaced.until == "2026-11-10T18:00:00"
    M.declare("hub", NOW + timedelta(days=3), "new disk, later", start=NOW + timedelta(days=2, hours=12), **kw)
    all_ = M.load(path=f)
    assert [(w.site, w.reason, bool(w.ended)) for w in all_] == [
        ("spydur", "OS patches", True), ("spydur", "patches run long", False), ("hub", "new disk, later", False)]
    rows = M.listing(now=NOW + timedelta(hours=2), path=f)
    assert [(r["site"], r["status"]) for r in rows] == [("spydur", "now"), ("hub", "coming"), ("spydur", "over")]
    assert M.next_for("hub", now=NOW, path=f).reason == "new disk, later"
    assert M.next_for("arachne", now=NOW, path=f) is None
    # ended by hand: over now, no grace; who ended it is kept
    assert M.end("spydur", now=NOW + timedelta(hours=2), path=f, write=False).reason == "patches run long"
    assert set(M.held(now=NOW + timedelta(hours=2), path=f)) == {"spydur"}      # only looked at
    w = M.end("spydur", now=NOW + timedelta(hours=2), path=f, by="zeus")
    assert w.reason == "patches run long" and M.held(now=NOW + timedelta(hours=2, minutes=1), path=f) == {}
    assert [x.ended_by for x in M.load(path=f) if x.reason == "patches run long"] == ["zeus"]
    assert M.end("spydur", now=NOW + timedelta(hours=3), path=f) is None
    assert M.end("hub", now=NOW, path=f).site == "hub" and M.next_for("hub", now=NOW, path=f) is None
    assert [w.site for w in M.load(path=f)] == ["spydur", "spydur"]            # the hub's never started
    assert M.shown("2026-11-12T18:00:00") == "Thu 12 Nov 18:00"


def test_one_window_after_another(tmp_path):
    f = tmp_path / "maintenance.json"
    kw = {"path": f, "known": ["spydur"]}
    M.declare("spydur", NOW + timedelta(hours=4), "OS patches", now=NOW, **kw)
    grace = NOW + timedelta(hours=4, minutes=30)                                # over, its alerts still held
    assert M.next_for("spydur", now=grace, path=f).reason == "OS patches"
    # the work was not done: another window declared while the first's grace holds
    new, replaced = M.declare("spydur", NOW + timedelta(hours=7), "patches, second try",
                              start=NOW + timedelta(hours=5), now=grace, **kw)
    assert replaced is None and len(M.load(path=f)) == 2
    both = M.next_for("spydur", now=grace, path=f)                              # told to the site as one
    assert (both.start, both.until, both.reason) == ("2026-11-10T14:00:00", "2026-11-10T21:00:00",
                                                     "patches, second try")
    args = M.site_args("spydur", {"hub": {"maintenance_file": str(f)}}, grace)
    assert args[1] == f"--maint={int(T)},{int(T + 7 * HOUR)},{int(T + 8 * HOUR)}"
    assert S.window(args[1].split("=")[1]) is not None
    assert set(M.held(now=grace, path=f)) == {"spydur"}
    assert set(M.held(now=NOW + timedelta(hours=6), path=f)) == {"spydur"}      # then the second
    # ending now ends the grace, not what is declared for later; ending again calls that off
    assert M.end("spydur", now=grace, path=f).reason == "OS patches"
    assert M.held(now=grace + timedelta(minutes=1), path=f) == {}
    assert M.next_for("spydur", now=grace + timedelta(minutes=1), path=f).reason == "patches, second try"
    assert M.end("spydur", now=grace + timedelta(minutes=2), path=f).reason == "patches, second try"
    assert M.next_for("spydur", now=grace + timedelta(minutes=3), path=f) is None
    assert [w.reason for w in M.load(path=f)] == ["OS patches"]
    # one far enough after the first is its own window
    M.declare("spydur", NOW + timedelta(hours=12), "later", start=NOW + timedelta(hours=10),
              now=NOW + timedelta(hours=4, minutes=10), **kw)
    assert M.next_for("spydur", now=NOW + timedelta(hours=4, minutes=5), path=f).reason == "later"
    # two long ones joined are still a window a site takes
    g = tmp_path / "long.json"
    M.declare("spydur", NOW + timedelta(days=14), "a", now=NOW, path=g)
    end1 = NOW + timedelta(days=14, minutes=10)
    M.declare("spydur", end1 + timedelta(days=13), "b", start=end1 + timedelta(minutes=30), now=end1, path=g)
    joined = M.site_args("spydur", {"hub": {"maintenance_file": str(g)}}, end1)[1]
    assert joined.startswith("--maint=") and S.window(joined.split("=")[1]) is not None
    # the hub's own window has no grace: nothing of the hub's is held
    h = tmp_path / "hub.json"
    M.declare("hub", NOW + timedelta(hours=2), "new disk", now=NOW, path=h)
    assert set(M.held(now=NOW + timedelta(hours=1), path=h)) == {"hub"}
    assert M.held(now=NOW + timedelta(hours=2, minutes=1), path=h) == {}
    assert [r["status"] for r in M.listing(now=NOW + timedelta(hours=2, minutes=1), path=h)] == ["over"]
    assert M.end("hub", now=NOW + timedelta(hours=2, minutes=1), path=h) is None


def test_a_store_that_cant_be_read_is_no_windows(tmp_path):
    f = tmp_path / "maintenance.json"
    good = {"site": "spydur", "start": "2026-11-10T14:00:00", "until": "2026-11-10T18:00:00", "reason": "x"}
    for text in ("{not json", "[1, 2]", '"x"', "null", '{"windows": "x"}', '{"windows": [1, "y", null]}'):
        f.write_text(text)
        assert M.load(path=f) == [] and M.held(now=NOW, path=f) == {}, text
        assert M.listing(now=NOW, path=f) == [] and M.site_args("spydur", now=NOW, path=f)[1:] == []
    # entries that aren't windows are left out; the others stand
    f.write_text(json.dumps({"windows": [{"site": "a"}, {**good, "start": "soon"}, {**good, "until": None},
                                         good, {**good, "site": "b", "start": "2026-11-10T19:00:00+00:00",
                                                "until": "2026-11-11T19:00:00+00:00", "ended": ""}]}))
    got = M.load(path=f)
    assert [w.site for w in got] == ["spydur", "b"] or [w.site for w in got] == ["b", "spydur"]
    assert all(w.starts.tzinfo is None and w.ends.tzinfo is None for w in got)  # one clock for all of them
    assert set(M.held(now=NOW, path=f)) >= {"spydur"}
    assert M.declare("c1", NOW + timedelta(hours=1), "y", now=NOW, path=f)[0].site == "c1"
    assert {w.site for w in M.load(path=f)} == {"spydur", "b", "c1"}


def test_what_the_hub_tells_a_site(tmp_path):
    import socket
    from nomad.config import combined_db_path
    f = tmp_path / "maintenance.json"
    cfg = {"hub": {"maintenance_file": str(f)}}
    assert M.state_path(cfg) == f
    # beside the combined database, whoever runs the command
    assert M.state_path({"hub": {"output_db": "/srv/nomad/combined.db"}}) == Path("/srv/nomad/maintenance.json")
    assert M.state_path({}) == combined_db_path(None).parent / "maintenance.json"
    assert M.state_path({"hub": "x"}) == M.state_path(None)
    assert M.site_args("spydur", cfg, NOW) == ["--hub-name=" + socket.gethostname().split(".")[0]]
    M.declare("spydur", NOW + timedelta(hours=4), "-OS patches", config=cfg, now=NOW)
    M.declare("hub", NOW + timedelta(days=1, hours=2), "new disk", start=NOW + timedelta(days=1), config=cfg, now=NOW)
    args = M.site_args("spydur", cfg, NOW)
    assert args[1:] == [f"--quiet={int(T + DAY)},{int(T + DAY + 2 * HOUR)}",
                        f"--maint={int(T)},{int(T + 4 * HOUR)},{int(T + 5 * HOUR)}", "--reason=-OS patches"]
    # the site's own parser takes them as they are, a reason that starts with a dash too
    db = site(tmp_path)
    assert call(db, "--hub", "h1", "--note", *args) == {
        "noted": True, "quiet": f"{int(T + DAY)},{int(T + DAY + 2 * HOUR)},{int(T + DAY + 2 * HOUR)}",
        "maint": f"{int(T)},{int(T + 4 * HOUR)},{int(T + 5 * HOUR)}"}
    assert notes(db)["h1"][6] == "-OS patches"
    assert not [a for a in M.site_args("arachne", cfg, NOW) if a.startswith("--maint")]
    assert [a for a in M.site_args("spydur", cfg, NOW + timedelta(hours=4, minutes=30))
            if a.startswith("--maint")]                                         # in its grace
    assert not [a for a in M.site_args("spydur", cfg, NOW + timedelta(hours=5)) if a.startswith("--maint")]
    assert not [a for a in M.site_args("spydur", cfg, NOW + timedelta(days=1, hours=3))
                if a.startswith("--quiet")]                                     # the hub's is over
    assert M.grace_minutes({"hub": {"maintenance_grace_minutes": 99999}}) == M.MAX_GRACE_MINUTES
    assert M.max_days({"hub": {"maintenance_max_days": 60}}) == M.MAX_DAYS
    assert M.MAX_DAYS * DAY <= S.MAX_WINDOW and M.MAX_GRACE_MINUTES * 60 <= S.MAX_HELD   # what a site takes


# -- the hub holds its alerts about a site in maintenance ------------------------------------------------

def test_the_hub_holds_what_it_would_say_of_a_site_in_maintenance(tmp_path, monkeypatch):
    f = tmp_path / "maintenance.json"
    cfg = {"hub": {"maintenance_file": str(f)}}
    stamp = tmp_path / "sync_alerts.json"
    sent = []
    monkeypatch.setattr(D, "send_alert", lambda sev, src, msg, **k: sent.append(msg) or {"Email": True})
    due = [{"key": "site:spydur", "cluster": "spydur", "host": "mingus", "subject": "spydur",
            "severity": "warning", "message": "Site spydur: not synced for 3 h", "details": {}},
           {"key": "node:spydur:spdr05", "site": "spydur/spdr05", "cluster": "spydur", "host": "spdr05",
            "subject": "spdr05", "severity": "critical", "message": "Node spdr05 is DOWN", "details": {}},
           {"key": "collector:arachne:gpu", "site": "arachne", "host": "arachne", "subject": "gpu",
            "severity": "warning", "message": "Site arachne: gpu stopped", "details": {}}]
    assert len(O.send_once(due, stamp, "sync", cfg, NOW)) == 3                  # nothing declared
    M.declare("spydur", NOW + timedelta(hours=4), "OS patches", config=cfg, now=NOW)
    sent.clear()
    got = O.send_once(due, stamp, "sync", cfg, NOW + timedelta(hours=1), hold_sites={"spydur/spdr05"})
    assert got == [] and sent == []
    assert set(json.loads(stamp.read_text())) == {"collector:arachne:gpu"}      # what was said of spydur: forgotten
    assert O.send_once(due, stamp, "sync", cfg, NOW + timedelta(hours=4, minutes=30)) == []   # the grace
    got = O.send_once(due, stamp, "sync", cfg, NOW + timedelta(hours=5, minutes=1))
    assert [i["key"] for i in got] == ["site:spydur", "node:spydur:spdr05"]     # still so: news again
    assert sent[0] == ("Site spydur: not synced for 3 h (The maintenance declared for spydur ended at "
                       "2026-11-10 18:00.)")
    assert due[0]["message"] == "Site spydur: not synced for 3 h"               # the caller's item untouched
    assert O._about({"site": "c1/n1"}) == "c1" and O._about({"key": "hub:combined"}) is None
    # the hub's own window holds nothing the hub says, and windows that can't be read hold nothing
    M.declare("hub", NOW + timedelta(days=2), "new disk", start=NOW + timedelta(days=1), config=cfg, now=NOW)
    sent.clear()
    (tmp_path / "s2.json").unlink(missing_ok=True)
    assert len(O.send_once(due, tmp_path / "s2.json", "sync", cfg, NOW + timedelta(days=1, hours=1))) == 3
    monkeypatch.setattr(M, "held", lambda *a, **k: 1 / 0)
    assert len(O.send_once(due, tmp_path / "s3.json", "sync", cfg, NOW + timedelta(hours=1))) == 3


def test_sync_alerts_say_which_site(tmp_path, monkeypatch):
    import nomad.cli as cli_mod
    f = tmp_path / "maintenance.json"
    cfg = {"hub": {"maintenance_file": str(f), "alert_after_minutes": 60}}
    sent = []
    monkeypatch.setattr(D, "send_alert", lambda sev, src, msg, **k: sent.append(msg) or {"Email": True})
    combined = tmp_path / "combined.db"
    old = (NOW - timedelta(hours=3)).isoformat()
    with sqlite3.connect(combined) as c:
        c.execute("CREATE TABLE sync_sites (name TEXT, last_success TEXT, synced_at TEXT)")
        c.executemany("INSERT INTO sync_sites VALUES (?, ?, ?)", [("spydur", old, old), ("arachne", old, old)])
    failing = {"spydur": "ssh: connect to host spydur: timed out", "arachne": "no answer"}
    M.declare("spydur", NOW + timedelta(hours=4), "OS patches", config=cfg, now=NOW)
    got = cli_mod._sync_alerts(combined, failing, cache_dir=tmp_path, config=cfg, now=NOW)
    assert [g["site"] for g in got] == ["arachne"] and len(sent) == 1


# -- the commands ----------------------------------------------------------------------------------------

def _runner():
    try:
        return CliRunner(mix_stderr=False)                                      # click before 8.2
    except TypeError:
        return CliRunner()


def test_nomad_maintenance_on_a_hub(tmp_path, monkeypatch):
    import nomad.cli as cli_mod
    run_sync, sites, out = _cli_hub(tmp_path, monkeypatch, names=("alpha", "beta"))
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(cfg.read_text().replace("[hub]\n", f'[hub]\noutput_db = "{out}"\n'))
    conf = {"hub": {"output_db": str(out)}}
    store = tmp_path / "maintenance.json"                                       # beside the combined database
    assert M.state_path(conf) == store
    r = run_sync()
    assert r.exit_code == 0, r.output
    hub_id = H.hub_id(out)
    assert notes(sites[0][1])[hub_id][3] == 1 and notes(sites[0][1])[hub_id][5] is None    # it left its name

    def nomad(*args):
        return _runner().invoke(cli_mod.cli, ["-c", str(cfg), "maintenance", *args])
    assert nomad().output == "No maintenance declared.\n"
    r = nomad("start", "alpha", "--until", "+4h")
    assert r.exit_code != 0 and "--reason" in r.stderr
    r = nomad("start", "gamma", "--until", "+4h", "--reason", "x", "--apply")
    assert r.exit_code != 0 and "not one of this hub's sites (alpha, beta, or hub)" in r.stderr
    # shown first; --apply declares it
    r = nomad("start", "alpha", "--until", "+4h", "--reason", "OS patches")
    assert r.exit_code == 0, r.output
    assert "alpha: in maintenance" in r.output and "Nothing changed. Run again with --apply" in r.output
    assert "told" not in r.output and not store.exists() and notes(sites[0][1])[hub_id][5] is None
    since = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")    # under way for an hour
    r = nomad("start", "alpha", "--from", since, "--until", "+4h", "--reason", "OS patches", "--apply")
    assert r.exit_code == 0, r.output
    assert "alpha: in maintenance" in r.output and "Alerts about it are held until" in r.output
    assert "  alpha: told" in r.output and "beta" not in r.output and "Nothing changed" not in r.output
    assert set(M.held(conf)) == {"alpha"}
    name, _first, _seen, reads, quiet, maint, reason = notes(sites[0][1])[hub_id]
    assert maint and reason == "OS patches" and quiet is None                   # the site knows at once,
    assert reads == 1                                                           # by the id the sync reads it under
    assert notes(sites[1][1])[hub_id][5] is None
    assert W.site_window(sites[0][1])["reason"] == "OS patches"
    r = nomad()
    assert "Now:\n  alpha: in maintenance until" in r.output and "(OS patches)" in r.output
    r = run_sync()
    assert r.exit_code == 0 and "In maintenance: alpha until" in r.output
    assert notes(sites[0][1])[hub_id][5] == maint and notes(sites[0][1])[hub_id][3] == 2   # said again with each read
    # the hub itself: every site is told it will be away; nothing of the hub's is held
    r = nomad("start", "hub", "--from", "+1h", "--until", "+2h", "--reason", "new disk", "--apply")
    assert r.exit_code == 0 and "  alpha: told" in r.output and "  beta: told" in r.output
    assert "won't report the hub as silent" in r.output and "Alerts about it are held" not in r.output
    assert notes(sites[1][1])[hub_id][4] is not None
    assert "Coming:\n  hub: from" in nomad().output
    # over: shown, then told at once, no longer held; who ended it is kept
    r = nomad("end", "alpha")
    assert r.exit_code == 0 and "would be over now" in r.output and W.site_window(sites[0][1]) is not None
    r = nomad("end", "alpha", "--by", "jdoe", "--apply")
    assert r.exit_code == 0 and "is over now" in r.output and "  alpha: told" in r.output
    assert notes(sites[0][1])[hub_id][5] is None and W.site_window(sites[0][1]) is None
    assert [w.ended_by for w in M.load(conf) if w.site == "alpha"] == ["jdoe"]
    assert nomad("end", "alpha", "--apply").exit_code != 0
    # --until counts from the start: how long it lasts, or the first such time of day after it starts
    day = (datetime.now() + timedelta(days=3)).replace(hour=14, minute=0, second=0, microsecond=0)
    r = nomad("start", "beta", "--from", f"{day:%Y-%m-%d %H:%M}", "--until", "+2h", "--reason", "x", "--json")
    assert json.loads(r.stdout)["windows"][0]["until"] == (day + timedelta(hours=2)).isoformat()
    r = nomad("start", "beta", "--from", f"{day:%Y-%m-%d %H:%M}", "--until", "9:30", "--reason", "x", "--json")
    assert json.loads(r.stdout)["windows"][0]["until"] == (day + timedelta(days=1)).replace(hour=9, minute=30).isoformat()
    # all: every site, or none of them
    r = nomad("start", "all", "--until", "+30d", "--reason", "x", "--apply")
    assert r.exit_code != 0 and M.held(conf) == {}
    r = nomad("start", "all", "--until", "+30m", "--reason", "switch firmware", "--apply")
    assert r.exit_code == 0 and r.output.count("in maintenance") == 2 and set(M.held(conf)) == {"alpha", "beta"}
    # a site that can't be told now, and one that does not take the window, are said as they are
    monkeypatch.setattr(H, "tell_site", lambda site_, hub, notes_, timeout=30: None if site_["name"] == "alpha"
                        else {"noted": True, "quiet": None, "maint": None})
    r = nomad("start", "all", "--until", "+40m", "--reason", "switch firmware", "--apply")
    assert "  alpha: not reached now; told when the hub next reads it" in r.output
    assert "  beta: it did not take the window" in r.output and "told\n" not in r.output.replace("told when", "")
    monkeypatch.undo()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(H, "WAL_WAIT_SECONDS", 0.5)
    # as the Console asks: JSON on standard output, and nothing else there
    real = cli_mod._load_hub_config

    def noisy(*a, **k):
        print("WARNING: sync.toml is deprecated")
        return real(*a, **k)
    monkeypatch.setattr(cli_mod, "_load_hub_config", noisy)
    r = nomad("start", "beta", "--until", "+2h", "--reason", "x", "--by", "jdoe", "--json")
    got = json.loads(r.stdout)
    assert got["ok"] is True and got["applied"] is False and got["windows"][0]["by"] == "jdoe"
    assert "replaces the window" in got["said"] and "deprecated" in r.stderr
    r = nomad("end", "gamma", "--json")
    assert r.exit_code == 1 and json.loads(r.stdout) == {
        "what": "maintenance.end", "site": "gamma", "ok": False, "applied": False,
        "error": "gamma: no maintenance running or coming, and no alerts held"}
    # the windows can't be written (run as someone else): said as JSON too, and nothing changed
    before = store.read_text()
    with monkeypatch.context() as m:
        m.setattr(M, "_save", lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "Permission denied")))
        for args in (("start", "beta", "--until", "+3h", "--reason", "x", "--apply", "--json"),
                     ("end", "beta", "--apply", "--json")):
            r = nomad(*args)
            got = json.loads(r.stdout)
            assert r.exit_code == 1 and got["ok"] is False and "can't write" in got["error"], r.output
            assert "Permission denied" in got["error"] and store.read_text() == before
        r = nomad("end", "beta", "--apply")
        assert r.exit_code != 0 and "can't write" in r.stderr and "Traceback" not in r.output
    assert json.loads(nomad("end", "all", "--apply", "--json").stdout)["applied"] is True
    assert nomad("end", "hub", "--apply").exit_code == 0
    assert M.held(conf) == {}
    # windows that can't be read never stop a sync
    store.write_text("[1, 2]")
    r = run_sync()
    assert r.exit_code == 0 and "In maintenance" not in r.output
    monkeypatch.setattr(M, "listing", lambda *a, **k: 1 / 0)
    r = run_sync()
    assert r.exit_code == 0 and "Maintenance windows not read" in r.output, r.output


def test_nomad_maintenance_at_a_site(tmp_path, monkeypatch):
    import nomad.cli as cli_mod
    monkeypatch.setenv("HOME", str(tmp_path))
    db = site(tmp_path)
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f'[database]\npath = "{db}"\n')

    def nomad():
        r = CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "maintenance"])
        assert r.exit_code == 0, r.output
        return r.output
    assert nomad() == "No maintenance declared.\n"
    now = int(time.time())
    call(db, "--hub", "h1", "--note", "--hub-name=mingus", f"--maint={now + HOUR},{now + 2 * HOUR},{now + 3 * HOUR}",
         "--reason=OS patches")
    out = nomad()
    assert "The hub mingus has not read this site yet." in out
    assert "It says this site: maintenance coming," in out and "(OS patches); this site's alerts are held from then" in out
    call(db, "--hub", "h1", "--init", "--hub-name=mingus", f"--maint={now - 60},{now + HOUR},{now + 2 * HOUR}",
         "--reason=OS patches", f"--quiet={now + DAY},{now + DAY + HOUR}")
    out = nomad()
    assert "The hub mingus last read this site " in out and "No maintenance declared" not in out
    assert "It says this site: in maintenance," in out and "(OS patches); this site's alerts are held until" in out
    assert "It says it will be away " in out
    with sqlite3.connect(db) as c:
        c.execute(f"UPDATE {S.HUBS} SET seen = ?", (now - 25 * HOUR,))
        c.execute(f"UPDATE {S.META} SET value = ? WHERE key = 'heard:h1'", (str(now - 25 * HOUR),))
    assert "this site's alerts are not held: that hub has not read it for 24 hours." in nomad()
    call(db, "--hub", "h1", "--note", "--hub-name=mingus", f"--maint={now - 2 * HOUR},{now - 60},{now + HOUR}")
    with sqlite3.connect(db) as c:
        c.execute(f"UPDATE {S.HUBS} SET seen = ?", (now - 60,))
    assert "It says this site: maintenance over, coming back," in nomad()
    # a hub that reads it and has declared nothing: said so, with when it last read
    call(db, "--hub", "h1", "--note", "--hub-name=mingus")
    out = nomad()
    assert out.startswith("No maintenance declared.\nThe hub mingus last read this site ") and out.count("\n") == 2


def test_the_console_may_ask_for_maintenance():
    from nomad.actions import spec
    from nomad.actions.runner import build_argv
    a = spec.get("maintenance.start")
    assert a.changes and a.where == frozenset({spec.HUB}) and a.schema()["apply"] == "apply"
    v = spec.check(a, {"site": "spydur", "until": "+4h", "reason": "-OS patches", "by": "jtonini", "apply": True},
                   sites=["spydur"])
    argv = build_argv(a, v)
    assert argv[2:] == ["nomad.cli", "maintenance", "start", "--json", "--until=+4h", "--reason=-OS patches",
                        "--by", "jtonini", "--apply", "--", "spydur"]
    with pytest.raises(spec.ActionError):
        spec.check(a, {"site": "spydur", "until": "+4h"})                      # no reason
    with pytest.raises(spec.ActionError):
        spec.check(a, {"site": "--apply", "until": "+4h", "reason": "x"})
    e = spec.get("maintenance.end")
    assert build_argv(e, spec.check(e, {"site": "hub"}))[2:] == ["nomad.cli", "maintenance", "end", "--json", "--", "hub"]
    assert build_argv(e, spec.check(e, {"site": "hub", "by": "jdoe", "apply": True}))[2:] == [
        "nomad.cli", "maintenance", "end", "--json", "--by", "jdoe", "--apply", "--", "hub"]
    assert not spec.get("maintenance").changes


def test_nomad_maintenance_is_declared_on_the_hub(tmp_path, monkeypatch):
    import nomad.cli as cli_mod
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = tmp_path / "nomad.toml"
    cfg.write_text("[general]\n")
    r = CliRunner().invoke(cli_mod.cli, ["-c", str(cfg), "maintenance", "start", "x", "--until", "+1h", "--reason", "y",
                                         "--apply"])
    assert r.exit_code != 0 and "declare maintenance on the hub" in r.output

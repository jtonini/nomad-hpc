# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A site watches its hub (1.37.0).

The hub says so when a site stops answering. Nothing said so when the hub
itself stopped: its disk died one night and every page showed that evening's
data until someone looked. Now each site notices, and tells whoever its own
alerts go to.

Each time a hub reads a site (``nomad sync``), it leaves a note in the site's
database (``_nomad_hubs``, written by nomad.db.delta_site): its name, when it
read, and what it has declared (nomad.maintenance): when it will be away
itself, and when this site is in maintenance. The site's own ``nomad
collect`` reads the notes after each run:

- **A hub that read this site regularly and has stopped**: REGULAR_READS
  reads over at least MIN_SPAN_HOURS (a trial sync is not a hub to miss), and
  none for ``[alerts.hub] after_minutes`` (60), or for three of its usual
  intervals when that is longer. A warning, critical after a day, through the
  site's own [alerts] backends: once, again when worse, then every ``[alerts]
  reminder_hours`` while it lasts. A hub that reads again is forgotten, so
  its next silence is news. One silent for RETIRED_DAYS is taken as retired,
  and the last reminders say so.
- **Only silence this site saw counts.** After the site's own outage (a
  reboot, its collection stopped for BREAK_MINUTES or more), the hub is
  given the time again from when collection resumed: the site was not there
  to be read. So the watch starts an hour after nomad is first upgraded to
  one that has it.
- **A read without a note is a read.** A hub of an older nomad, or one whose
  maintenance file can't be read, still records each read in the change
  log's own bookkeeping; the later of the two is when it last read.
- **While the hub said it would be away** (its own maintenance window),
  nothing is said; if it is still silent ``after_minutes`` past the window's
  end, it is, with the window's end.
- **While this site is in maintenance**, nothing is said either: it may be
  the site that can't be reached.

``site_window`` is the other thing read here: the window this site is in
maintenance for, as its hub declared it. The site's alert dispatcher holds
every alert while it lasts (``[alerts] maintenance_hold = false`` to keep
them coming). A window is trusted only while the hub that declared it keeps
reading: TRUST_HOURS after its last read the hold lapses, so a hub that died
after declaring a long window does not keep the site quiet.

Settings, at the site::

    [alerts.hub]
    enabled = true          # watch the hub (default)
    after_minutes = 60      # silent this long: say so
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

TABLE = "_nomad_hubs"
LOG_META = "_nomad_capture_meta"   # nomad.db.delta_site META: heard:<hub> = its last read of the log
REGULAR_READS = 20       # fewer reads than this: not a hub to miss yet
MIN_SPAN_HOURS = 24      # nor reads that all fall within a day (a trial)
AFTER_MINUTES = 60
INTERVALS = 3            # ...or this many of its usual intervals, when longer
RETIRED_DAYS = 7         # silent this long: retired (the site's change log goes then too)
TRUST_HOURS = 24         # a site's window holds its alerts only this long past its hub's last read
BREAK_MINUTES = 10       # this site's own collection silent this long: it was not there to be read
STEPS = 5                # the steps between its runs kept, for its usual one
STAMP = "hub_watch.json"
STATE = "hub_watch_state.json"


class Unreadable(Exception):
    """The site's database could not be read just now (locked, say): not
    the same as no hub having left a note."""


@dataclass
class Hub:
    id: str
    name: str | None
    first: float | None
    seen: float | None       # its last read: its note, or the change log's own record of it
    reads: int
    quiet: tuple | None      # (from, until, held): the hub away
    maint: tuple | None      # (from, until, held): this site in maintenance
    reason: str | None
    noted: float | None = None   # its last read that left a note (for its usual interval)

    @property
    def label(self) -> str:
        """As a sentence names it: "the hub mingus", or "its hub" when it
        left no name."""
        return f"the hub {self.name}" if self.name else "its hub"

    @property
    def usual_gap(self) -> float | None:
        """Seconds between its reads, on average, as its notes count them."""
        last = self.noted if self.noted is not None else self.seen
        if self.first is None or last is None or self.reads < 2 or last <= self.first:
            return None
        return (last - self.first) / (self.reads - 1)


@dataclass
class Settings:
    enabled: bool = True
    after_minutes: float = AFTER_MINUTES
    hold: bool = True


def settings(config: dict | None) -> Settings:
    alerts = (config or {}).get("alerts")
    alerts = alerts if isinstance(alerts, dict) else {}
    hub = alerts.get("hub") if isinstance(alerts.get("hub"), dict) else {}
    try:
        after = float(hub.get("after_minutes", AFTER_MINUTES))
    except (TypeError, ValueError):
        after = float(AFTER_MINUTES)
    if not after > 0:
        after = float(AFTER_MINUTES)
    return Settings(enabled=hub.get("enabled", True) is not False, after_minutes=after,
                    hold=alerts.get("maintenance_hold", True) is not False)


def _window(text) -> tuple | None:
    try:
        parts = tuple(int(float(x)) for x in str(text or "").split(","))
    except (TypeError, ValueError):
        return None
    if len(parts) == 2:
        parts += (parts[1],)
    if len(parts) != 3 or parts[1] <= parts[0] or parts[2] < parts[1]:
        return None
    return parts


def _tables(conn) -> set:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def hubs(conn: sqlite3.Connection) -> list[Hub]:
    """The hubs that have read this database, as they left their notes; []
    when none has. Unreadable when the database can't be read just now."""
    try:
        tables = _tables(conn)
        if TABLE not in tables:
            return []
        rows = conn.execute(f"SELECT hub, name, first, seen, reads, quiet, maint, reason FROM {TABLE}").fetchall()
        heard = {}
        if LOG_META in tables:
            for key, value in conn.execute(f"SELECT key, value FROM {LOG_META} WHERE key LIKE 'heard:%'"):
                try:
                    heard[str(key)[6:]] = float(value)
                except (TypeError, ValueError):
                    continue
    except sqlite3.Error as e:
        raise Unreadable(str(e)) from None
    out = []
    for hub, name, first, seen, reads, quiet, maint, reason in rows:
        try:
            noted = float(seen) if seen is not None else None
            # A read that left no note (an older hub, a hub whose notes
            # failed) is still in the change log's own bookkeeping.
            last = max((t for t in (noted, heard.get(str(hub))) if t is not None), default=None)
            out.append(Hub(str(hub), str(name) if name else None,
                           float(first) if first is not None else None, last, int(reads or 0),
                           _window(quiet), _window(maint), str(reason) if reason else None, noted))
        except (TypeError, ValueError):
            continue
    return out


def _open(db_path) -> sqlite3.Connection | None:
    try:
        p = Path(str(db_path))
        if not p.is_file():
            return None
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=5)
    except (sqlite3.Error, OSError, ValueError):
        return None


def read_hubs(db_path) -> list[Hub]:
    """hubs() of the database at ``db_path``: [] when there is none."""
    conn = _open(db_path)
    if conn is None:
        return []
    try:
        return hubs(conn)
    finally:
        conn.close()


def site_window(db_path, now: float | None = None) -> dict | None:
    """The window this site is in maintenance for now, as a hub that reads
    it declared it: {start, until, held, reason, hub} (epoch seconds; alerts
    are held until ``held``), or None. With several, the one held longest.
    None too when the database can't be read: an alert then goes out, which
    is the safe way to be wrong."""
    try:
        found = read_hubs(db_path)
    except Unreadable:
        return None
    return _site_window(found, datetime.now().timestamp() if now is None else now)


def _latest(found: list[Hub]) -> list[Hub]:
    """One row per hub: of several with the same name (its combined
    database moved, so the site knows it under two ids), the one that read
    last."""
    best: dict = {}
    for h in found:
        key = ("name", h.name) if h.name else ("id", h.id)
        cur = best.get(key)
        if cur is None or (h.seen or 0) > (cur.seen or 0):
            best[key] = h
    return list(best.values())


def trusted(h: Hub, now: float) -> bool:
    """Whether what ``h`` declared still holds this site's alerts: only
    while it keeps reading the site (TRUST_HOURS past its last read)."""
    return h.seen is not None and now - h.seen <= TRUST_HOURS * 3600


def _site_window(found: list[Hub], now: float) -> dict | None:
    best = None
    for h in _latest(found):
        if not (h.maint and h.maint[0] <= now < h.maint[2]):
            continue
        if not trusted(h, now):
            continue                      # its hub has stopped reading: no longer taken on its word
        if best is None or h.maint[2] > best["held"]:
            best = {"start": h.maint[0], "until": h.maint[1], "held": h.maint[2],
                    "reason": h.reason, "hub": h.name or h.id}
    return best


def _span(minutes: float) -> str:
    hours = minutes / 60
    if hours < 1:
        return f"{minutes:.0f} min"
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} days"


def _shown(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def due(found: list[Hub], s: Settings, now: float, host: str, watching_since: float | None = None) -> list[dict]:
    """What to say of the hubs now, for nomad.alerts.once.send_once.
    ``watching_since``: since when this site has been collecting without a
    break; silence before it is not the hub's (the site was not there)."""
    if not s.enabled or _site_window(found, now):
        return []
    out = []
    for h in _latest(found):
        if h.seen is None or h.first is None:
            continue
        if h.reads < REGULAR_READS or h.seen - h.first < MIN_SPAN_HOURS * 3600:
            continue                                  # not one to miss yet
        # Silence counts from its last read, or later: from the end of the
        # window it said it would be away for, and from when this site
        # itself was collecting again.
        since, ended = h.seen, None
        if h.quiet and h.quiet[0] <= now:
            if now < h.quiet[1]:
                continue                              # it said it would be away
            if h.seen < h.quiet[1]:
                since, ended = h.quiet[1], h.quiet[1]   # still silent after its window
        if watching_since is not None and watching_since > since:
            since = watching_since
        if now - since > RETIRED_DAYS * 86400:
            continue                                  # retired
        limit = max(s.after_minutes * 60, INTERVALS * (h.usual_gap or 0))
        counted = now - since
        if counted < limit:
            continue
        silent = (now - h.seen) / 60
        label = h.label
        message = (f"{label[0].upper()}{label[1:]} has not read {host} for {_span(silent)} "
                   f"(last at {_shown(h.seen)}): "
                   f"its pages show {host} as of then. If it reads no site, it is down, cannot reach "
                   f"the sites, or its sync has stopped.")
        if ended is not None:
            message += f" The maintenance it declared for itself ended at {_shown(ended)}."
        if counted >= (RETIRED_DAYS - 2) * 86400:
            message += (f" After {RETIRED_DAYS} days of silence this site takes it as retired and "
                        f"stops saying so.")
        out.append({"key": f"hub:{h.id}", "host": host, "subject": f"hub {h.name}" if h.name else "its hub",
                    "severity": "critical" if counted >= 24 * 3600 else "warning", "message": message,
                    "details": {"metric": "hub_read", "hub": h.name or h.id,
                                "last_read": datetime.fromtimestamp(h.seen).isoformat(timespec="seconds")}})
    return out


def _watching_since(folder: Path, now: float) -> float:
    """Since when this site's collection has run without a break (kept in
    STATE beside the database): now, when this is the first run or the first
    after a break. A break is collection silent for BREAK_MINUTES, or for
    three of its usual steps when those are longer (a site that collects
    once an hour from cron is not always just back)."""
    path = folder / STATE
    try:
        state = json.loads(path.read_text())
        last, since = float(state["last_run"]), float(state["since"])
        steps = [float(x) for x in state.get("steps") or []][-STEPS:]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        last = since = None
        steps = []
    if last is None or since is None or since > now or now < last:
        since, steps = now, []                        # the first run (or the clock went back)
    else:
        gap = now - last
        usual = sorted(steps)[len(steps) // 2] if len(steps) >= 3 else 0
        if gap > max(BREAK_MINUTES * 60, 3 * usual):
            since = now
        steps = (steps + [gap])[-STEPS:]
    try:
        tmp = path.with_name(f"{path.name}.{os.getpid()}")
        tmp.write_text(json.dumps({"last_run": now, "since": since, "steps": steps}))
        os.replace(tmp, path)
    except OSError:
        pass
    return since


def watch(db_path, config: dict | None, now: datetime | None = None) -> list[dict]:
    """Run after a site's collection: say so (once, again when worse, then
    every reminder_hours) when a hub that read this site has stopped.
    Returns what was sent. Never raises."""
    try:
        s = settings(config)
        try:
            found = read_hubs(db_path)
        except Unreadable:
            return []                                 # not known this time: nothing said, nothing forgotten
        folder = Path(str(db_path)).parent
        stamp = folder / STAMP
        if not found and not stamp.exists():
            return []                                 # no hub reads this site: nothing to watch
        now = now or datetime.now()
        since = _watching_since(folder, now.timestamp())
        items = due(found, s, now.timestamp(), socket.gethostname().split(".")[0], watching_since=since)
        from nomad.alerts.once import send_once
        return send_once(items, stamp, "hub", config or {}, now)
    except Exception:                                 # collection goes on whatever happens here
        import logging
        logging.getLogger(__name__).debug("hub watch failed", exc_info=True)
        return []

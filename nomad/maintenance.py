# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Declared maintenance (1.37.0).

A site taken down on purpose and one that broke look the same from the hub:
it stops answering. Unless someone says which it is, the hub's alerts fire
for planned work, and get ignored the next time. A window says it::

    nomad maintenance start c1 --until "2026-11-12 18:00" --reason "OS patches"
    nomad maintenance start hub --from 14:00 --until 16:00 --reason "new disk"
    nomad maintenance end c1
    nomad maintenance

While a site's window lasts, and for ``[hub] maintenance_grace_minutes``
(60) after it to come back:

- the hub holds every alert it raises about that site: not synced, not
  collecting, a collector or a GPU node stopped, its nodes, clocks and power
  supplies (nomad.alerts.once);
- the site's own nomad holds its alerts too (nomad.alerts.dispatcher): the
  hub tells it of the window each time it reads it, and at once when the
  window is declared (nomad.db.delta_site ``--maint``);
- the Console shows the site as in maintenance, not as stale.

What still holds once the window and its grace are over is raised then, as
news (the hub's alerts say when the window ended). What only happened once
during it (an entry in a board's event log) is not sent afterwards. A
window ended by hand (``nomad maintenance end``) has no grace: the site is
back; so is the grace itself ended.

``hub`` is the hub itself: for its window, the sites' own watch of the hub
(nomad.hub_watch) stays quiet; a hub still silent an hour after it is said
so.

A window always ends: it needs ``--until``, and can't be longer than
``[hub] maintenance_max_days`` (14, which is also the most a site takes).
One window per site at a time: declaring another replaces it.

The windows are kept in ``maintenance.json`` beside the hub's combined
database (``[hub] maintenance_file`` for another place), whoever runs the
command, past ones too: what was collected during a window is not what the
site usually does.
"""
from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

HUB = "hub"                     # the hub itself, as a site name here
ALL = "all"                     # every site (not the hub)
GRACE_MINUTES = 60
MAX_DAYS = 14                   # nomad.db.delta_site MAX_WINDOW: the most a site takes
MAX_GRACE_MINUTES = 24 * 60     # nomad.db.delta_site MAX_HELD
JOIN_MINUTES = 30               # a window starting this soon after another's grace: told as one
KEEP_DAYS = 400                 # past windows kept this long
KEEP = 500
LATELY_HOURS = 24
REASON_MAX = 200
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_PLUS = re.compile(r"^\+\s*(\d{1,4})\s*([mhd])$", re.I)
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")


class MaintenanceError(ValueError):
    """What was asked can't be a window; said as it is."""


@dataclass
class Window:
    site: str
    start: str                  # local time, ISO, to the second
    until: str
    reason: str
    by: str = ""
    declared: str = ""
    ended: str | None = None    # ended by hand (or replaced) then
    ended_by: str = ""

    @property
    def starts(self) -> datetime:
        return datetime.fromisoformat(self.start)

    @property
    def ends(self) -> datetime:
        """When it is over: its end, or when it was ended by hand."""
        until = datetime.fromisoformat(self.until)
        if self.ended:
            return min(until, datetime.fromisoformat(self.ended))
        return until

    def active(self, now: datetime) -> bool:
        return self.starts <= now < self.ends

    def coming(self, now: datetime) -> bool:
        return now < self.starts and not self.ended

    def held_until(self, grace_minutes: float) -> datetime:
        """Alerts are held until then: its end and the grace after it, or
        just when it was ended by hand."""
        return self.ends if self.ended else self.ends + timedelta(minutes=grace_minutes)

    def holding(self, now: datetime, grace_minutes: float) -> bool:
        return self.starts <= now < self.held_until(grace_minutes)


def _hub(config) -> dict:
    hub = (config or {}).get("hub")
    return hub if isinstance(hub, dict) else {}


def _number(config, key, default, most) -> float:
    try:
        v = float(_hub(config).get(key, default))
    except (TypeError, ValueError):
        return float(default)
    return min(v, float(most)) if v >= 0 else float(default)


def grace_minutes(config) -> float:
    return _number(config, "maintenance_grace_minutes", GRACE_MINUTES, MAX_GRACE_MINUTES)


def max_days(config) -> float:
    return _number(config, "maintenance_max_days", MAX_DAYS, MAX_DAYS) or MAX_DAYS


def state_path(config=None) -> Path:
    """Beside the hub's combined database ([hub] output_db), so it is the
    same file whoever runs the command and for the sync that reads it."""
    given = _hub(config).get("maintenance_file")
    if isinstance(given, str) and given.strip():
        return Path(given).expanduser()
    from nomad.config import combined_db_path
    out = _hub(config).get("output_db")
    return combined_db_path(out if isinstance(out, str) and out.strip() else None).parent / "maintenance.json"


def site_names(config, sites=None) -> list[str]:
    """The hub's sites by name ([hub] sites, or the list `nomad sync` read)."""
    if sites is None:
        sites = _hub(config).get("sites")
    return [str(s["name"]) for s in sites or [] if isinstance(s, dict) and s.get("name")]


# -- the file ----------------------------------------------------------------------------------

def load(config=None, path=None) -> list[Window]:
    """Every window on record, oldest first. An unreadable file is no windows
    (alerts go on as if nothing was declared)."""
    try:
        p = Path(path) if path is not None else state_path(config)
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    rows = data.get("windows") if isinstance(data, dict) else None
    out = []
    for w in rows if isinstance(rows, list) else []:
        try:
            win = Window(site=str(w["site"]), start=_local(w["start"]), until=_local(w["until"]),
                         reason=str(w.get("reason") or ""), by=str(w.get("by") or ""),
                         declared=str(w.get("declared") or ""),
                         ended=_local(w["ended"]) if w.get("ended") else None,
                         ended_by=str(w.get("ended_by") or ""))
            out.append(win)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue                      # an entry that isn't a window (edited by hand): left out
    return sorted(out, key=lambda w: (w.start, w.site))


def _local(value) -> str:
    """A time on record as this module keeps it: local, to the second, no
    offset (one written with an offset by hand is taken to local time).
    ValueError when it isn't a time."""
    t = datetime.fromisoformat(str(value))
    if t.tzinfo is not None:
        t = t.astimezone().replace(tzinfo=None)
    return t.replace(microsecond=0).isoformat()


class _Locked:
    """The file, held for one change at a time (the CLI and the Console)."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = None

    def __enter__(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = open(f"{self.path}.lock", "a")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        import fcntl
        try:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
        finally:
            self.lock.close()
        return False


def _save(path: Path, windows: list[Window], now: datetime) -> None:
    old = now - timedelta(days=KEEP_DAYS)
    kept = [w for w in windows if w.ends >= old][-KEEP:]
    tmp = path.with_name(f"{path.name}.{os.getpid()}")
    tmp.write_text(json.dumps({"windows": [asdict(w) for w in kept]}, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# -- declaring -----------------------------------------------------------------------------------

def parse_when(text, now: datetime) -> datetime:
    """A time as someone types it: "2026-11-12 18:00" (or with a T, or the
    date alone: midnight), "18:00" (today; tomorrow when that has passed),
    "+4h", "+90m", "+2d"."""
    t = " ".join(str(text or "").split())
    if not t:
        raise MaintenanceError("a time is needed")
    m = _PLUS.match(t)
    if m:
        # By the clock, not the calendar: "+2h" is two hours from now on the
        # night daylight saving time changes too.
        n, unit = int(m.group(1)), m.group(2).lower()
        seconds = n * {"m": 60, "h": 3600, "d": 86400}[unit]
        return datetime.fromtimestamp(now.timestamp() + seconds).replace(microsecond=0)
    m = _CLOCK.match(t)
    if m:
        try:
            at = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        except ValueError:
            raise MaintenanceError(f"{text!r} is not a time of day") from None
        return at if at > now else at + timedelta(days=1)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    raise MaintenanceError(f'{text!r} is not a time: "2026-11-12 18:00", "18:00", "+4h", "+2d"')


def _reason(text) -> str:
    return "".join(c for c in " ".join(str(text or "").split()) if c.isprintable())[:REASON_MAX]


def declare(site: str, until: datetime, reason: str, start: datetime | None = None, by: str = "",
            config=None, now: datetime | None = None, path=None, known=None,
            write: bool = True) -> tuple[Window, Window | None]:
    """A window for ``site`` (a site's name, or "hub"). Returns (the window,
    the one it replaces or None). ``known``: the names a site may have (the
    hub's sites); None: any name that reads as one. ``write=False``: checked
    and returned, nothing declared."""
    now = (now or datetime.now()).replace(microsecond=0)
    site = str(site or "").strip()
    if site != HUB and (not _NAME.match(site) or (known is not None and site not in known)):
        raise MaintenanceError(f"{site!r} is not one of this hub's sites"
                               + (f" ({', '.join(sorted(known))}, or hub)" if known else ""))
    start = (start or now).replace(microsecond=0)
    until = until.replace(microsecond=0)
    reason = _reason(reason)
    if not reason:
        raise MaintenanceError("say why (--reason): whoever reads the page or the next alert needs it")
    if until <= now:
        raise MaintenanceError(f"it would end at {until:%Y-%m-%d %H:%M}, which has passed")
    if until <= start:
        raise MaintenanceError("it would end before it starts")
    days = max_days(config)
    if until - start > timedelta(days=days):
        raise MaintenanceError(f"longer than {days:g} days ([hub] maintenance_max_days, {MAX_DAYS} at most): "
                               "a window has to end; declare the rest when it is nearer")
    p = Path(path) if path is not None else state_path(config)
    new = Window(site=site, start=start.isoformat(), until=until.isoformat(), reason=reason,
                 by=_reason(by)[:64], declared=now.isoformat())
    if not write:
        return new, next((w for w in load(path=p) if w.site == site and not w.ended
                          and datetime.fromisoformat(w.until) > now), None)
    with _Locked(p):
        windows = load(path=p)
        replaced = None
        for w in windows:
            if w.site == site and not w.ended and datetime.fromisoformat(w.until) > now:
                replaced = Window(**asdict(w))
                w.ended = now.isoformat()
        # One that had not started leaves no trace: it never was.
        windows = [w for w in windows if not (w.site == site and w.ended and w.ends <= w.starts)]
        windows.append(new)
        _save(p, sorted(windows, key=lambda w: (w.start, w.site)), now)
    return new, replaced


def end(site: str, config=None, now: datetime | None = None, path=None, write: bool = True,
        by: str = "") -> Window | None:
    """``site``'s window is over now: the one running; else the one just
    over whose grace still holds its alerts (then the grace ends); else the
    one coming (called off). Returns it, or None when there was none.
    ``write=False``: only found."""
    now = (now or datetime.now()).replace(microsecond=0)
    grace = 0 if site == HUB else grace_minutes(config)
    p = Path(path) if path is not None else state_path(config)

    def the_one(windows: list[Window]) -> Window | None:
        # One at a time: the one running; else the one whose grace holds;
        # else the next coming (ending the grace of one window does not
        # call off the one declared for tomorrow).
        mine = [w for w in windows if w.site == site and not w.ended]
        for pick in (lambda w: w.active(now), lambda w: w.starts <= now < w.held_until(grace),
                     lambda w: w.coming(now)):
            some = sorted((w for w in mine if pick(w)), key=lambda w: w.start)
            if some:
                return some[0]
        return None
    if not write:
        return the_one(load(path=p))
    with _Locked(p):
        windows = load(path=p)
        w = the_one(windows)
        if w is None:
            return None
        found = Window(**asdict(w))
        w.ended, w.ended_by = now.isoformat(), _reason(by)[:64]
        windows = [w for w in windows if not (w.site == site and w.ended and w.ends <= w.starts)]
        _save(p, windows, now)
    return found


# -- reading -------------------------------------------------------------------------------------

def _grace(w: Window, grace: float) -> float:
    """The grace after ``w``: none for the hub's own (nothing of the hub's
    is held; the sites give it an hour of their own)."""
    return 0 if w.site == HUB else grace


def held(config=None, now: datetime | None = None, path=None) -> dict:
    """{site: its window} for the sites whose alerts are held now: in their
    window, or within the grace after it (with two, the later one)."""
    now = now or datetime.now()
    grace = grace_minutes(config)
    return {w.site: w for w in load(config, path) if w.holding(now, _grace(w, grace))}


def current(config=None, now: datetime | None = None, path=None) -> dict:
    """{site: its window} for the sites in maintenance now (no grace): what
    a page shows."""
    now = now or datetime.now()
    return {w.site: w for w in load(config, path) if w.active(now)}


def ended_lately(config=None, now: datetime | None = None, path=None, hours: float = LATELY_HOURS) -> dict:
    """{site: when its window ended} for windows over (and no longer held)
    within ``hours``: what an alert raised just after says."""
    now = now or datetime.now()
    grace = grace_minutes(config)
    out: dict = {}
    for w in load(config, path):
        if w.starts <= now and not w.holding(now, _grace(w, grace)) and now - w.ends <= timedelta(hours=hours) \
                and w.ends > w.starts:
            out[w.site] = max(out.get(w.site, w.ends), w.ends)
    quiet = held(config, now, path)
    return {s: t for s, t in out.items() if s not in quiet}


def next_for(site: str, config=None, now: datetime | None = None, path=None) -> Window | None:
    """``site``'s window that matters now: the one it is in; else the one
    just over whose grace still holds (run together with one that starts
    before that grace is well over, so the site is told of both at once);
    else the next one coming."""
    now = now or datetime.now()
    grace = 0 if site == HUB else grace_minutes(config)
    mine = [w for w in load(config, path) if w.site == site]
    running = [w for w in mine if w.active(now)]
    if running:
        return max(running, key=lambda w: w.until)
    coming = sorted((w for w in mine if w.coming(now)), key=lambda w: w.start)
    over = [w for w in mine if w.holding(now, grace)]
    if over:
        w = max(over, key=lambda w: w.until)
        nxt = coming[0] if coming else None
        if nxt is not None and nxt.starts <= w.held_until(grace) + timedelta(minutes=JOIN_MINUTES):
            # One window from the first's start to the second's end: a site
            # keeps one, and is told again only when the hub next reads it.
            # No longer than a site takes (MAX_DAYS): it starts later then.
            start = max(w.starts, datetime.fromisoformat(nxt.until) - timedelta(days=MAX_DAYS))
            return Window(site=site, start=start.isoformat(), until=nxt.until, reason=nxt.reason, by=nxt.by,
                          declared=nxt.declared)
        return w
    return coming[0] if coming else None


def listing(config=None, now: datetime | None = None, path=None, past_days: float = 7) -> list[dict]:
    """The windows for a page or the command line, soonest first: each as a
    dict with ``status``: "now", "grace" (over, alerts still held), "coming"
    or "over" (those of the last ``past_days``)."""
    now = now or datetime.now()
    grace = grace_minutes(config)
    out = []
    for w in load(config, path):
        g = _grace(w, grace)
        if w.active(now):
            status = "now"
        elif w.coming(now):
            status = "coming"
        elif w.holding(now, g):
            status = "grace"
        elif now - w.ends <= timedelta(days=past_days) and w.ends > w.starts:
            status = "over"
        else:
            continue
        out.append({**asdict(w), "status": status, "ends": w.ends.isoformat(),
                    "held_until": w.held_until(g).isoformat()})
    order = {"now": 0, "grace": 1, "coming": 2, "over": 3}
    return sorted(out, key=lambda d: (order[d["status"]], d["start"] if d["status"] != "over" else "",
                                      d["site"]))


def site_args(site_name: str, config=None, now: datetime | None = None, path=None) -> list[str]:
    """What the hub tells a site each time it reads it (nomad.db.delta_site):
    its own name, when it will be away, and the site's own window."""
    now = now or datetime.now()
    grace = grace_minutes(config)
    # Each as one word (--reason=...): a reason that starts with a dash is
    # not taken for an option by the site's argument parser.
    args = ["--hub-name=" + socket.gethostname().split(".")[0]]
    away = next_for(HUB, config, now, path)
    if away is not None and away.ends > now:
        args.append(f"--quiet={int(away.starts.timestamp())},{int(away.ends.timestamp())}")
    mine = next_for(str(site_name), config, now, path) if str(site_name) != HUB else None
    if mine is not None:
        args.append(f"--maint={int(mine.starts.timestamp())},{int(mine.ends.timestamp())},"
                    f"{int(mine.held_until(grace).timestamp())}")
        if mine.reason:
            args.append("--reason=" + mine.reason)
    return args


def shown(t: datetime | str) -> str:
    """A time for a person: "Thu 12 Nov 18:00"."""
    if isinstance(t, str):
        t = datetime.fromisoformat(t)
    return f"{t:%a} {t.day} {t:%b %H:%M}"

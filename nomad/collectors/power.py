# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Power supplies, voltages and fans, as the boards' own controllers read them
(1.25.8).

vadur's gpu09 ran on one of its two power supplies from February to
November 2026: the other had no input power ("PSU1 AC Lost"), and nothing
said so. Its board knew. Reading what the board knows takes root (ipmitool
on /dev/ipmi0), which nomad never runs as: a small helper does, by root's
cron (scripts/power_root_probe.py: it only reads), and writes a JSON file
this collector reads as nomad's own account.

At each run the helper's files (``files``: paths or shell patterns) are read;
a host's readings already stored are skipped. For each host:

- every Power Supply, Power Unit, Voltage, Current and Fan sensor, kept as
  it is now (``power_sensors``; rewritten when its state changes, and its
  reading at most hourly);
- what is wrong, judged here, not by the helper: a supply with no input
  power, failed, failing or misconfigured; redundancy lost; a voltage past
  its board's thresholds; a fan stopped; a supply giving 0 W while another
  carries the node. A threshold of 0 isn't one: a supply that doesn't
  report its power reads 0 W "below" it at every start (vadur's CPU nodes),
  and that is said, not alerted;
- the board's event log: each entry not seen before (``power_events``),
  the log's first read kept as history.

Alerts go through this host's [alerts] (source ``power``), one a host for
each of "power supply", "voltage" and "fans" (once, again when worse, then
every reminder), and "power events" for new problem entries in the log. A
site that leaves its alerts to the hub (``alerts = "hub"``) gets them from
the hub after each sync (nomad.alerts.hub_power).

    [collectors.power]
    enabled = true
    files = ["/var/lib/nomad-power/power.json"]   # the helper's --out
    # max_age_minutes = 20     # an older file is said (is the helper's cron running?)

    [alerts.power]
    # enabled = true
    # fans = true              # alert on fans too
    # ignore = ["node03:PMBPower"]   # host:sensor (shell patterns): never alerted

Tables: ``power_hosts`` (each host's last read), ``power_sensors`` (each
sensor now), ``power_events`` (the boards' event logs).
"""
from __future__ import annotations

import fnmatch
import glob
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .base import BaseCollector, CollectionError, registry

logger = logging.getLogger(__name__)

KINDS = {"Power Supply": "power_supply", "Power Unit": "power_unit", "Voltage": "voltage",
         "Current": "current", "Fan": "fan"}
SUBJECTS = ("power supply", "voltage", "fans")
MAX_AGE_MINUTES = 20
REFRESH_MINUTES = 60          # a sensor's reading is rewritten at most this often when nothing else changed
_PSU_FAN = re.compile(r"\bPSU?\s*\d|\bPS\d|\bPWS", re.I)
_READING = re.compile(r"^(-?\d+(?:\.\d+)?)\s*(.*)$")
_THRESHOLD_EVENT = re.compile(r"Reading\s+(-?[\d.]+)\s*([<>])\s*Threshold\s+(-?[\d.]+)\s*(.*)$", re.I)
# A supply's or power unit's states that say something is wrong (ipmitool's words).
BAD_STATES = (
    (re.compile(r"\bAC lost\b|input lost", re.I), "has no input power"),
    (re.compile(r"out-of-range|out of range", re.I), "has its input out of range"),
    (re.compile(r"predictive failure", re.I), "is predicted to fail"),
    (re.compile(r"(?<!predictive )failure", re.I), "has failed"),
    (re.compile(r"config(uration)? error", re.I), "is misconfigured"),
    (re.compile(r"redundancy lost|redundancy degraded|non-redundant|insufficient", re.I), "lost its redundancy"),
)
LEVELS = {"lnc": ("lower", "non-critical"), "lcr": ("lower", "critical"), "lnr": ("lower", "non-recoverable"),
          "unc": ("upper", "non-critical"), "ucr": ("upper", "critical"), "unr": ("upper", "non-recoverable")}
ADVICE = {
    "power supply": ("A node with two supplies keeps running on one, with no spare: check the supply's power "
                     "cord and the outlet or PDU it is plugged into; if they're live, the supply has likely "
                     "failed."),
    "voltage": "A voltage out of its range points at a failing supply or board regulator.",
    "fans": "A fan has stopped or slowed: the node's cooling depends on the rest.",
}
RANK = {"warning": 1, "critical": 2}


def kind_of(sensor: dict) -> str:
    kind = KINDS.get(sensor.get("type"), "other")
    if kind == "fan" and _PSU_FAN.search(sensor.get("name") or ""):
        return "power_supply"      # a supply's own fan: part of the supply
    return kind


def subject_of(kind: str) -> str:
    return {"voltage": "voltage", "fan": "fans"}.get(kind, "power supply")


def split_value(value: str) -> tuple[float | None, str, str]:
    """(reading, unit, states) from what ipmitool prints for a sensor:
    ``11.90 Volts`` -> (11.9, "Volts", ""); ``0 Watts, Presence detected, Power
    Supply AC lost`` (a reading and states, as some boards give) -> (0.0, "Watts",
    "Presence detected, Power Supply AC lost"); ``Presence detected`` -> (None,
    "", "Presence detected"); ``No Reading`` -> (None, "", "")."""
    raw = (value or "").strip()
    if raw.lower() == "no reading":
        return None, "", ""
    m = _READING.match(raw)
    if not m:
        return None, "", raw
    unit, _sep, states = m.group(2).partition(",")
    return float(m.group(1)), unit.strip(), states.strip()


def reading(value: str) -> tuple[float | None, str]:
    """``11.90 Volts`` -> (11.9, "Volts"); "No Reading" or a state -> (None, "")."""
    v, unit, _states = split_value(value)
    return v, unit


def _num(v: float) -> str:
    return f"{v:g}"


def judge(sensor: dict) -> tuple[str | None, str | None]:
    """(severity, what is wrong) for one sensor as the helper read it, or
    (None, None). A discrete sensor by its states; a threshold sensor by its
    status, unless the threshold it crossed is 0 or below."""
    kind = kind_of(sensor)
    name = sensor.get("name") or "?"
    status = (sensor.get("status") or "").lower()
    value, unit, states = split_value(sensor.get("value") or "")
    if kind in ("power_supply", "power_unit") and states:
        said = []
        for rx, words in BAD_STATES:
            if rx.search(states) and words not in said:
                said.append(words)
        if said:
            return "warning", f'{name} says "{states}" (it {" and ".join(said)})'
        if value is None:
            return None, None
    if status not in LEVELS:
        return None, None        # ok, ns (no reading), or a status this doesn't know
    side, level = LEVELS[status]
    limits = sensor.get("thresholds") if isinstance(sensor.get("thresholds"), dict) else {}
    limit = limits.get(status)
    if side == "lower":
        if limit is not None and limit == 0 and (value is None or value <= 0):
            return None, None    # 0 W under a threshold of 0: no threshold at all
        if limit is None and (value is None or value == 0) and unit.lower() in ("watts", "amps"):
            return None, None    # the same, where the threshold couldn't be read
    if kind == "fan" or kind == "power_supply" and unit.upper() == "RPM":
        severity = "warning"
    elif kind == "voltage":
        severity = "warning" if level == "non-critical" else "critical"
    else:
        severity = "warning"
    shown = f"{_num(value)} {unit}".strip() if value is not None else "no reading"
    against = f", {'below' if side == 'lower' else 'above'} its {level} {_num(limit)}" if limit is not None \
        else f" ({side} {level})"
    return severity, f"{name} {shown}{against}"


def supplies_unequal(sensors: list[dict]) -> str | None:
    """Two or more supplies reporting their power, one giving 0 W while
    another carries the node: said, as a supply with no AC-lost sensor
    would show it."""
    watts = []
    for s in sensors:
        if KINDS.get(s.get("type")) == "power_supply":
            v, unit = reading(s.get("value") or "")
            if v is not None and unit.lower() == "watts":
                watts.append((s.get("name") or "?", v))
    if len(watts) < 2:
        return None
    zero = [n for n, v in watts if v <= 0]
    live = [(n, v) for n, v in watts if v > 0]
    if zero and live:
        return (f"{', '.join(zero)} {'gives' if len(zero) == 1 else 'give'} 0 W while "
                f"{', '.join(f'{n} gives {_num(v)} W' for n, v in live)}: one supply carries the node")
    return None


def supply_watts(sensors: list[dict]) -> list[str]:
    """The supplies' sensors that report power in watts (what supplies_unequal compares)."""
    return [s.get("name") or "?" for s in sensors if KINDS.get(s.get("type")) == "power_supply"
            and reading(s.get("value") or "")[0] is not None
            and reading(s.get("value") or "")[1].lower() == "watts"]


def named(sensors: list[dict]) -> list[dict]:
    """The sensors, each with a name of its own: two with one name (older
    Dells name both supplies' "Status") get their sensor number with it."""
    count: dict = {}
    for s in sensors:
        count[s.get("name")] = count.get(s.get("name"), 0) + 1
    return [dict(s, name=f"{s.get('name')} #{s.get('number') or '?'}") if count[s.get("name")] > 1 else s
            for s in sensors]


def ignored(rules: dict, node: str, sensor: str) -> bool:
    for pat in rules.get("ignore") or ():
        if isinstance(pat, str) and ":" in pat:
            h, s = pat.split(":", 1)
            if fnmatch.fnmatchcase(node, h) and fnmatch.fnmatchcase(sensor, s):
                return True
    return False


def problems(node: str, sensors: list[dict], rules: dict | None = None) -> list[dict]:
    """What is wrong on a host now: [{sensor, subject, severity, problem}]."""
    rules = rules or {}
    out = []
    for s in sensors:
        severity, what = judge(s)
        if not severity or ignored(rules, node, s.get("name") or ""):
            continue
        subject = subject_of(kind_of(s))
        if subject == "fans" and rules.get("fans") is False:
            continue
        out.append({"sensor": s.get("name"), "subject": subject, "severity": severity, "problem": what})
    kept = [s for s in sensors if not ignored(rules, node, s.get("name") or "")]
    unequal = supplies_unequal(kept)
    if unequal:
        out.append({"sensor": None, "subject": "power supply", "severity": "warning", "problem": unequal,
                    "sensors": supply_watts(kept)})
    return out


def event_sensor(e: dict) -> tuple[str | None, str]:
    """(its sensor type, its sensor's name) from a log entry's sensor field
    ("Power Supply PSU1 AC Lost" -> ("Power Supply", "PSU1 AC Lost"))."""
    sensor = e.get("sensor") or ""
    typ = next((t for t in KINDS if sensor.startswith(t + " ")), None)
    return typ, sensor[len(typ) + 1:] if typ else sensor


def judge_event(e: dict) -> tuple[str | None, str | None]:
    """(severity, what) for a log entry that says something is wrong with
    power or a fan, or (None, None): one that ended (Deasserted), a
    threshold of 0, and every other kind of entry (kept, not alerted)."""
    typ, name = event_sensor(e)
    if not typ:
        return None, None
    if (e.get("direction") or "").lower().startswith("deasserted"):
        return None, None
    kind = kind_of({"type": typ, "name": name})
    event = e.get("event") or ""
    m = _THRESHOLD_EVENT.search(e.get("detail") or "")
    if "going low" in event.lower() or "going high" in event.lower():
        if m:
            value, sign, limit, unit = float(m.group(1)), m.group(2), float(m.group(3)), m.group(4).strip()
            if sign == "<" and limit == 0:
                return None, None            # 0 under a threshold of 0
            what = f"{name} {_num(value)} {unit} {'below' if sign == '<' else 'above'} {_num(limit)}".strip()
        else:
            what = f"{name}: {event}"
        if kind == "voltage" and "critical" in event.lower() and "non-critical" not in event.lower():
            return "critical", what
        return "warning", what
    if kind in ("power_supply", "power_unit"):
        for rx, words in BAD_STATES:
            if rx.search(event):
                return "warning", f'{name}: "{event}" (it {words})'
    return None, None


def local_time(text) -> str:
    """The helper's time (with its UTC offset) as this host's local time,
    as nomad keeps times: 2026-11-03T17:30:00+00:00 -> 2026-11-03T12:30:00 here."""
    try:
        t = datetime.fromisoformat(str(text))
    except ValueError:
        return str(text or "")
    if t.tzinfo is not None:
        t = t.astimezone().replace(tzinfo=None)
    return t.isoformat(timespec="seconds")


def logged_at(e: dict) -> str:
    d, t = (e.get("date") or "").strip(), (e.get("time") or "").strip()
    if d.lower() == "pre-init":
        return "(clock not set)"            # logged before the board's clock was set
    return f"{d} {t}".strip()


class PowerCollector(BaseCollector):
    """The boards' power sensors and event logs, from the root helper's files."""

    name = "power"
    description = "Power supplies, voltages and fans (IPMI), from the root helper's files"
    default_interval = 300

    def __init__(self, config: dict[str, Any], db_path):
        super().__init__(config, db_path)
        files = self.config.get("files") or []
        self.files = [str(f) for f in (files if isinstance(files, list) else [files]) if f]
        self.max_age = float(self.config.get("max_age_minutes") or MAX_AGE_MINUTES)
        rules = self.config.get("alert_rules")
        if rules is None:
            rules = ((getattr(registry, "_config", {}) or {}).get("alerts") or {}).get("power")
        self.rules = rules if isinstance(rules, dict) else {}

    def _paths(self) -> list[str]:
        out = []
        for f in self.files:
            got = sorted(glob.glob(f)) if any(c in f for c in "*?[") else [f]
            out += [g for g in got if g not in out]
        return out

    def collect(self) -> list[dict[str, Any]]:
        if not self.files:
            self.note = "no files listed ([collectors.power] files: the helper's --out)"
            return []
        now = datetime.now()
        hosts, notes, read = {}, [], 0
        self._listed = {}
        for path in self._paths():
            try:
                with open(path, encoding="utf-8") as fh:
                    doc = json.load(fh)
            except FileNotFoundError:
                notes.append(f"{path} isn't there (has the helper run?)")
                continue
            except (OSError, ValueError) as e:
                notes.append(f"{path} can't be read ({type(e).__name__})")
                continue
            if not isinstance(doc, dict) or doc.get("nomad_power") != 1 or not isinstance(doc.get("hosts"), dict):
                notes.append(f"{path} isn't the power helper's file")
                continue
            read += 1
            if not doc.get("partial"):         # not the whole list (sinfo failed): no host is forgotten
                self._listed[path] = {str(n)[:64] for n in doc["hosts"]}
            notes += [f"{path}: {n}" for n in (doc.get("notes") or []) if isinstance(n, str)][:3]
            try:
                written = datetime.fromisoformat(local_time(doc.get("written")))
            except ValueError:
                written = None
            if written is None or now - written > timedelta(minutes=self.max_age):
                age = f"{int((now - written).total_seconds() // 60)} min old" if written else "undated"
                notes.append(f"{path} is {age}: is the helper's cron running?")
            for node, h in doc["hosts"].items():
                if isinstance(h, dict) and isinstance(node, str) and node:
                    h = dict(h, node=node[:64], file=path, read_at=local_time(h.get("read_at")))
                    old = hosts.get(node)
                    if old is None or str(h.get("read_at") or "") > str(old.get("read_at") or ""):
                        hosts[node] = h
        if not read and notes:
            raise CollectionError("; ".join(notes)[:400])
        new = self._unstored(list(hosts.values()))
        errs = sorted(h["node"] for h in hosts.values() if h.get("error"))
        if errs:
            notes.append(f"not read: {', '.join(errs[:8])}" + (f" and {len(errs) - 8} more" if len(errs) > 8 else ""))
        if hosts and not new:
            notes.append("nothing newer than what is stored")
        self.note = "; ".join(notes)[:400] or None
        return new

    def _unstored(self, hosts: list[dict]) -> list[dict]:
        try:
            conn = sqlite3.connect(str(self.db_path), timeout=10)
            try:
                conn.executescript(self.SCHEMA)
                last = dict(conn.execute("SELECT node, read_at FROM power_hosts").fetchall())
            finally:
                conn.close()
        except sqlite3.Error:
            last = {}
        # another read than the one stored (not "later": the clock goes back an hour in the autumn)
        return [h for h in hosts if str(h.get("read_at") or "") != str(last.get(h["node"]) or "")]

    def count_records(self, data) -> int:
        return len(data)

    # ── keeping ────────────────────────────────────────────────────────────
    SCHEMA = """
        CREATE TABLE IF NOT EXISTS power_hosts (
            node TEXT PRIMARY KEY, read_at TEXT, file TEXT, error TEXT, sensors INTEGER,
            problems INTEGER, severity TEXT, host_problems TEXT, sel_entries INTEGER,
            sel_used_pct INTEGER, sel_overflow INTEGER, notes TEXT);
        CREATE TABLE IF NOT EXISTS power_sensors (
            node TEXT NOT NULL, sensor TEXT NOT NULL, type TEXT, kind TEXT, status TEXT,
            reading REAL, unit TEXT, state TEXT, thresholds TEXT, subject TEXT, severity TEXT,
            problem TEXT, read_at TEXT, changed_at TEXT, PRIMARY KEY (node, sensor));
        CREATE TABLE IF NOT EXISTS power_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, node TEXT NOT NULL, key TEXT NOT NULL,
            record_id TEXT, logged TEXT, sensor TEXT, type TEXT, event TEXT, direction TEXT,
            detail TEXT, first_seen TEXT, history INTEGER, severity TEXT, problem TEXT,
            UNIQUE (node, key));
        CREATE INDEX IF NOT EXISTS idx_power_events_node ON power_events(node, first_seen);
    """

    def store(self, data: list[dict[str, Any]]) -> None:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        fresh = []
        try:
            conn.executescript(self.SCHEMA)
            # a host its helper no longer reads (a node taken out of Slurm): gone from here too
            for path, listed in (getattr(self, "_listed", None) or {}).items():
                for (node,) in conn.execute("SELECT node FROM power_hosts WHERE file = ?", (path,)).fetchall():
                    if node not in listed:
                        conn.execute("DELETE FROM power_hosts WHERE node = ?", (node,))
                        conn.execute("DELETE FROM power_sensors WHERE node = ?", (node,))
            for h in data:
                fresh.append(self._store_host(conn, h))
            conn.commit()
        finally:
            conn.close()
        self._alerts([f for f in fresh if f])

    def _store_host(self, conn, h: dict) -> dict | None:
        node, at = h["node"], str(h.get("read_at") or "")
        sel = h.get("sel") if isinstance(h.get("sel"), dict) else {}
        if h.get("error"):
            # not read: what was known of it stays (its sensors), with why it wasn't read now
            conn.execute(
                "INSERT INTO power_hosts (node, read_at, file, error) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(node) DO UPDATE SET read_at = excluded.read_at, file = excluded.file, "
                "error = excluded.error", (node, at, h.get("file"), str(h["error"])[:300]))
            return None
        sensors = named([s for s in (h.get("sensors") or []) if isinstance(s, dict) and s.get("name")])
        found = problems(node, sensors, self.rules)
        by_sensor = {p["sensor"]: p for p in found if p["sensor"]}
        old = {r[0]: r[1:] for r in conn.execute(
            "SELECT sensor, status, state, severity, problem, read_at, type FROM power_sensors WHERE node = ?",
            (node,))}
        unread = set(h.get("unread_types") or ())
        try:
            now = datetime.fromisoformat(at)
        except ValueError:
            now = datetime.now()
        for s in sensors:
            name = str(s["name"])[:40]
            value, unit, states = split_value(s.get("value") or "")
            state = states[:200] or None
            p = by_sensor.get(name) or {}
            row = (s.get("status"), state, p.get("severity"), p.get("problem"))
            prev = old.get(name)
            if prev is not None and tuple(prev[:4]) == row:
                try:
                    if now - datetime.fromisoformat(prev[4]) < timedelta(minutes=REFRESH_MINUTES):
                        continue        # nothing changed, and its reading was kept within the hour
                except (TypeError, ValueError):
                    pass
            changed = prev is None or tuple(prev[:4]) != row
            conn.execute(
                "INSERT INTO power_sensors (node, sensor, type, kind, status, reading, unit, state, thresholds, "
                "subject, severity, problem, read_at, changed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(node, sensor) DO UPDATE SET type = excluded.type, kind = excluded.kind, "
                "status = excluded.status, reading = excluded.reading, unit = excluded.unit, "
                "state = excluded.state, thresholds = excluded.thresholds, subject = excluded.subject, "
                "severity = excluded.severity, problem = excluded.problem, read_at = excluded.read_at, "
                "changed_at = CASE WHEN ? THEN excluded.changed_at ELSE power_sensors.changed_at END",
                (node, name, s.get("type"), kind_of(s), s.get("status"), value, unit or None, state,
                 json.dumps(s["thresholds"]) if isinstance(s.get("thresholds"), dict) else None,
                 subject_of(kind_of(s)), p.get("severity"), p.get("problem"), at, at, 1 if changed else 0))
        # a sensor no longer there goes (one of a type this read couldn't get stays as it was)
        gone = set(old) - {str(s["name"])[:40] for s in sensors}
        for name in gone:
            if old[name][5] not in unread:
                conn.execute("DELETE FROM power_sensors WHERE node = ? AND sensor = ?", (node, name))
        # the host's problems not tied to one sensor (one supply carrying the node) are kept on the host
        loose = [{k: p[k] for k in ("subject", "severity", "problem", "sensors")} for p in found if not p["sensor"]]
        worst = max((p["severity"] for p in found), key=lambda x: RANK[x], default=None)
        conn.execute(
            "INSERT INTO power_hosts (node, read_at, file, error, sensors, problems, severity, host_problems, "
            "sel_entries, sel_used_pct, sel_overflow, notes) VALUES (?,?,?,NULL,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(node) DO UPDATE SET read_at = excluded.read_at, file = excluded.file, error = NULL, "
            "sensors = excluded.sensors, problems = excluded.problems, severity = excluded.severity, "
            "host_problems = excluded.host_problems, sel_entries = excluded.sel_entries, "
            "sel_used_pct = excluded.sel_used_pct, sel_overflow = excluded.sel_overflow, notes = excluded.notes",
            (node, at, h.get("file"), len(sensors), len(found), worst, json.dumps(loose) if loose else None,
             sel.get("entries"), sel.get("used_pct"), 1 if sel.get("overflow") else 0,
             "; ".join(str(n) for n in (h.get("notes") or []))[:500] or None))
        events = []
        history = bool(sel.get("baseline"))
        for e in sel.get("new") or []:
            if not isinstance(e, dict) or not e.get("key"):
                continue
            severity, what = judge_event(e)
            sensor = str(e.get("sensor") or "")
            typ, name = event_sensor(e)
            quiet = ignored(self.rules, node, name) or (
                self.rules.get("fans") is False and subject_of(kind_of({"type": typ, "name": name})) == "fans")
            typ = typ or sensor.split(" ")[0]
            cur = conn.execute(
                "INSERT OR IGNORE INTO power_events (node, key, record_id, logged, sensor, type, event, direction, "
                "detail, first_seen, history, severity, problem) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (node, str(e["key"])[:32], e.get("id"), logged_at(e), sensor[:80], typ, e.get("event"),
                 e.get("direction"), e.get("detail"), at, 1 if history else 0, severity, what))
            if cur.rowcount and severity and not history and not quiet:
                events.append({"severity": severity, "problem": what, "logged": logged_at(e),
                               "key": str(e["key"])[:32]})
        return {"node": node, "problems": found, "events": events}

    # ── alerting ───────────────────────────────────────────────────────────
    def _alerts(self, fresh: list[dict]) -> list[dict]:
        if self.rules.get("enabled") is False:
            return []
        items = []
        for f in fresh:
            items += alert_items(f["node"], f["problems"], f["events"])
        for it in items:
            try:
                from nomad.alerts import send_alert
                send_alert(severity=it["severity"], source="power", host=it["host"], subject=it["subject"],
                           message=it["message"], details=it["details"])
            except Exception as e:                     # noqa: BLE001 -- never stops collection
                logger.warning(f"power: alert not sent: {e}")
        return items


def alert_items(node: str, found: list[dict], events: list[dict] = (), where: str = "") -> list[dict]:
    """One alert a subject for a host's problems now, and one for its new
    problem log entries: [{host, subject, severity, message, details}]."""
    out = []
    for subject in SUBJECTS:
        mine = [p for p in found if p["subject"] == subject]
        if not mine:
            continue
        severity = max((p["severity"] for p in mine), key=lambda x: RANK[x])
        what = "; ".join(p["problem"] for p in mine)
        out.append({"host": node, "subject": subject, "severity": severity,
                    "message": f"{node}{where}: {what}. {ADVICE[subject]}",
                    "details": {"node": node, "problems": [p["problem"] for p in mine]}})
    if events:
        severity = max((e["severity"] for e in events), key=lambda x: RANK[x])
        shown = "; ".join(f"{e['logged']}: {e['problem']}" for e in events[:6])
        more = f" (and {len(events) - 6} more)" if len(events) > 6 else ""
        # each batch its own condition (the newest entry's key): two batches within the
        # dispatcher's episode gap are both sent
        out.append({"host": node, "subject": "power events", "severity": severity,
                    "message": f"{node}{where}'s board logged: {shown}{more}. It may have passed already; "
                               "nomad power --host shows what its sensors say now.",
                    "details": {"node": node, "events": [e["problem"] for e in events],
                                "metric": str(events[-1].get("key") or len(events))}})
    return out


def hosts_now(conn: sqlite3.Connection, node: str | None = None, site: str | None = None) -> list[tuple]:
    """(node, read_at, error, sensors, problems, severity, sel_entries, notes[, site])."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(power_hosts)")}
    if not cols:
        return []
    sitecol = "source_site" if "source_site" in cols else None
    where, args = [], []
    if node:
        where.append("node = ?")
        args.append(node)
    if site and sitecol:
        where.append(f"{sitecol} = ?")
        args.append(site)
    sql = ("SELECT node, read_at, error, sensors, problems, severity, sel_entries, notes"
           + (f", {sitecol}" if sitecol else "") + " FROM power_hosts"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY node")
    return conn.execute(sql, args).fetchall()


__all__ = ["PowerCollector", "judge", "judge_event", "problems", "supplies_unequal", "alert_items", "reading", "ignored",
           "split_value", "named", "event_sensor", "supply_watts",
           "kind_of", "subject_of", "hosts_now", "BAD_STATES", "SUBJECTS"]

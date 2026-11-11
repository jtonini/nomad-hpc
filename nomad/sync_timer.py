# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""How often the hub reads its sites (1.40.0): ``[hub] sync_interval_minutes``
in nomad.toml, and the systemd timer that runs ``nomad sync`` set to match.

The sites read themselves every few minutes (5 by default); the hub's sync
brings what they read to the Console. Reading more often costs no storage
(the same rows arrive, in smaller batches) and little network (a site's
running and waiting jobs are sent again each time); it shortens how long a
job that started takes to show as running. The timer runs a couple of
minutes after each five-minute mark, when the sites have read themselves::

    [hub]
    sync_interval_minutes = 5      # 2 to 60, a number of minutes that divides an hour

``nomad sync timer`` says what the timer would be; ``--apply`` (as root)
writes it as a drop-in for the hub's timer (nomad-sync.timer, ``--unit``),
leaving the timer's own unit as it was installed. systemd never starts a
sync while one is still running.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

UNIT = "nomad-sync.timer"
OFFSET = 2                  # minutes after the five-minute marks the sites read themselves on
CHOICES = (2, 3, 4, 5, 6, 10, 12, 15, 20, 30, 60)


def interval(config: dict | None) -> int | None:
    """[hub] sync_interval_minutes, when it is one of CHOICES; else None."""
    hub = (config or {}).get("hub")
    raw = hub.get("sync_interval_minutes") if isinstance(hub, dict) else None
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw in CHOICES else None


def config_problems(config: dict | None) -> list[str]:
    hub = (config or {}).get("hub")
    if not isinstance(hub, dict) or "sync_interval_minutes" not in hub:
        return []
    if interval(config) is None:
        return [f"[hub] sync_interval_minutes = {hub['sync_interval_minutes']!r}: a number of minutes that "
                f"divides an hour, one of {', '.join(map(str, CHOICES))}"]
    return []


def on_calendar(every: int) -> str:
    """systemd's OnCalendar for every ``every`` minutes, OFFSET after the marks."""
    if every == 60:
        return f"*-*-* *:{OFFSET:02d}:00"
    return f"*-*-* *:{OFFSET % every:02d}/{every}:00"


def dropin_path(unit: str = UNIT) -> Path:
    return Path("/etc/systemd/system") / f"{unit}.d" / "interval.conf"


def dropin_text(every: int, source: str) -> str:
    return (f"# From [hub] sync_interval_minutes = {every} in {source} (nomad sync timer)\n"
            f"[Timer]\nOnCalendar=\nOnCalendar={on_calendar(every)}\n")


def _settings(text: str) -> list[str]:
    """A drop-in's lines that say something (not its comments)."""
    return [x.strip() for x in text.splitlines() if x.strip() and not x.strip().startswith("#")]


def plan(config: dict | None, source: str, unit: str = UNIT, path: Path | None = None) -> dict:
    """{"every", "when", "path", "text", "same", "problem"}: what the timer
    would be, and whether its drop-in says so already."""
    path = path or dropin_path(unit)
    every = interval(config)
    if every is None:
        hub = (config or {}).get("hub")
        why = (config_problems(config) or [f"no [hub] sync_interval_minutes in {source}: the timer is left as "
                                            "it is"]) if isinstance(hub, dict) else ["no [hub] here: not a hub"]
        return {"every": None, "problem": why[0], "path": path}
    text = dropin_text(every, source)
    try:
        now = path.read_text()
    except OSError:
        now = None
    return {"every": every, "when": on_calendar(every), "path": path, "text": text,
            "same": now is not None and _settings(now) == _settings(text), "problem": None}


def apply(p: dict, unit: str = UNIT, run_=subprocess.run) -> str:
    """Write the drop-in, reload systemd and restart the timer; what the
    timer says now."""
    if run_(["systemctl", "cat", unit], capture_output=True, text=True).returncode != 0:
        raise RuntimeError(f"no {unit} here: this hub's syncs run from something else (cron?); set that "
                           f"to {p['when']}")
    p["path"].parent.mkdir(parents=True, exist_ok=True)
    tmp = p["path"].with_name(p["path"].name + ".new")
    tmp.write_text(p["text"])
    tmp.replace(p["path"])
    for argv in (["systemctl", "daemon-reload"], ["systemctl", "restart", unit]):
        r = run_(argv, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(argv)}: {(r.stderr or r.stdout).strip()[-300:]}")
    r = run_(["systemctl", "show", unit, "-p", "TimersCalendar", "--value"], capture_output=True, text=True)
    return (r.stdout or "").strip()

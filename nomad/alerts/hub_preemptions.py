# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Telling a person their job was preempted (1.28.0).

After each sync the hub reads the sites' preempted job instances (the
preemptions collector: job_preemptions) and, when ``[submit.preemption]
notices = true``, emails each person once about each, in one message a
person and site a run: their job, where it ran and for how long, what Slurm
did (put it back in the queue, or cancelled it), and how to make it resume
next time. Only where the site's Slurm preempts that partition
(nomad.preemption.preempts). The address: the person's NetID at report.toml's
[report.labs] mail_domain. Sent through the hub's mail settings ([mail], as
its alerts' [alerts.email]); a ledger beside the combined database keeps
each sent once; the first run only notes what is there (as does one after
LOOK_HOURS with no run: notices off a while). An instance that ended in the
last HOLD_MINUTES and isn't known to be back in the queue waits a sync.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

logger = logging.getLogger(__name__)

LEDGER = "preemption_notices.json"
LOOK_HOURS = 48            # instances first seen this recently are still news
MAX_A_RUN = 50
# an instance that ended this recently and isn't known to be back in the queue
# waits (Slurm may not have requeued it yet when the site read it)
HOLD_MINUTES = 20
LAST_RUN = "_last_run"     # in the ledger: a ledger untouched this long (notices off) starts over
_USER = re.compile(r"[a-z_][a-z0-9_.-]{0,31}")


def _mins(elapsed: str | None) -> int | None:
    """Slurm's [D-]HH:MM:SS as minutes."""
    m = re.fullmatch(r"(?:(\d+)-)?(\d+):(\d+):(\d+)", str(elapsed or ""))
    if not m:
        return None
    d, h, mi, _s = (int(x or 0) for x in m.groups())
    return d * 1440 + h * 60 + mi


def _span(mins: int | None) -> str:
    if mins is None:
        return "for a while"
    if mins < 60:
        return f"for {mins} min"
    h, m = divmod(mins, 60)
    return f"for {h} h {m:02d} min" if h < 48 else f"for {h // 24} days {h % 24} h"


def _what(row: dict, treated: dict) -> str:
    grace = int(treated.get("grace_s") or 0) // 60
    then = {1: "put it back in the queue: it runs again, from the start of its script, when nodes are free",
            0: "cancelled it (it couldn't be requeued: --no-requeue, or not a batch job): send it again when "
               "you are ready"}.get(row.get("requeued"),
                                    "put it back in the queue, if it could be requeued (else it was cancelled)")
    return ("Slurm stopped it" + (f" after a grace of up to {grace} minutes" if grace else "") + f" and {then}.")


ADVICE = [
    "Unless a job saves its work and picks it up again, what it computed is lost. To lose less next time:",
    "  - save a checkpoint now and then, and have the script resume from it ($SLURM_RESTART_COUNT says",
    "    how many times the job was requeued);",
    "  - GROMACS: mdrun -cpi state.cpt resumes from its checkpoint;",
    "  - #SBATCH --open-mode=append keeps what the first run printed;",
    "  - or send long jobs to the shared partitions, where nobody preempts them.",
    "",
    "To check a script before you send it, on the cluster: nomad size --requeue job.sh",
]


def message(site: str, rows: list, to: str) -> EmailMessage:
    """One notice for one person's preempted jobs at a site: [(row, treated)]."""
    m = EmailMessage()
    m["To"] = to
    if len(rows) == 1:
        row, treated = rows[0]
        name = f" ({row['job_name']})" if row.get("job_name") else ""
        m["Subject"] = f"Your job {row['job_id']}{name} on {site} was stopped for a condo's owners"
        lines = [f"Your job {row['job_id']}{name} ran in {row.get('partition') or 'a guest partition'} on {site}, "
                 f"on another lab's condo nodes ({row.get('nodes') or '?'}), {_span(_mins(row.get('elapsed')))}, "
                 f"until {str(row.get('end') or '?').replace('T', ' ')}.",
                 "", "The lab that owns those nodes needed them. " + _what(row, treated), ""]
    else:
        m["Subject"] = f"{len(rows)} of your jobs on {site} were stopped for a condo's owners"
        # the cancelled first: those need sending again
        rows = sorted(rows, key=lambda x: {0: 0, None: 1}.get(x[0].get("requeued"), 2))
        n = {k: sum(1 for r, _t in rows if r.get("requeued") == k) for k in (0, 1, None)}
        said = [f"{n[0]} cancelled (send {'it' if n[0] == 1 else 'them'} again when you are ready)"] if n[0] else []
        said += [f"{n[1]} put back in the queue"] if n[1] else []
        said += [f"{n[None]} put back in the queue if {'it' if n[None] == 1 else 'they'} could be"] if n[None] else []
        lines = [f"These jobs of yours ran in a guest partition on {site}, on other labs' condo nodes, and were "
                 "stopped because the labs that own those nodes needed them: " + "; ".join(said) + ".", ""]
        for row, treated in rows[:20]:
            name = f" ({row['job_name']})" if row.get("job_name") else ""
            lines.append(f"  - {row['job_id']}{name}, {row.get('partition')}, {_span(_mins(row.get('elapsed')))} "
                         f"until {str(row.get('end') or '?').replace('T', ' ')}: " + _what(row, treated))
        if len(rows) > 20:
            lines.append(f"  ... and {len(rows) - 20} more")
        lines.append("")
    m.set_content("\n".join(lines + ADVICE) + "\n")
    return m


def due(conn: sqlite3.Connection, domain: str | None, now: datetime, sent: dict) -> list[tuple]:
    """[(key, site, row, treated, to)] not sent yet; ``domain``: the people's
    mail domain (report.toml's [report.labs] mail_domain)."""
    from nomad import partitions as parts
    from nomad import preemption as pre
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "job_preemptions" not in tables or not domain:
        return []
    cols = {r[1] for r in conn.execute("PRAGMA table_info(job_preemptions)")}
    site_col = "source_site" if "source_site" in cols else "NULL"
    req_col = "requeued" if "requeued" in cols else "NULL"
    since = (now - timedelta(hours=LOOK_HOURS)).isoformat(timespec="seconds")
    rows = conn.execute(f"SELECT job_id, job_name, user, partition, state, start, end, elapsed, nodes, {site_col}, "
                        f"{req_col} FROM job_preemptions WHERE first_seen >= ? AND state = 'PREEMPTED' "
                        "ORDER BY first_seen", (since,)).fetchall()
    out, cache = [], {}
    for job_id, job_name, user, partition, state, start, end, elapsed, nodes, site, requeued in rows:
        key = f"{site}:{job_id}:{start}"
        if key in sent or not user or not _USER.fullmatch(user):
            continue
        if requeued != 1 and _ended_within(end, now, HOLD_MINUTES):
            continue                           # not known to be back yet: next sync
        if site not in cache:
            prow = parts.current(conn, site)
            cache[site] = ({r["partition"]: r for r in prow}, prow, pre.config_of(conn, site))
        by, prow, cfg = cache[site]
        p = by.get(partition)
        treated = pre.preempts(p, prow, cfg) if p else None
        if not treated:
            continue                           # not a partition Slurm preempts guests in
        row = {"job_id": job_id, "job_name": job_name, "user": user, "partition": partition, "state": state,
               "start": start, "end": end, "elapsed": elapsed, "nodes": nodes, "requeued": requeued}
        out.append((key, site or "the cluster", row, treated, f"{user}@{domain}"))
    return out


def _ended_within(end, now: datetime, minutes: int) -> bool:
    try:
        return now - datetime.fromisoformat(str(end)) < timedelta(minutes=minutes)
    except (TypeError, ValueError):
        return False


def _save(folder: Path, sent: dict, now: datetime) -> bool:
    cut = (now - timedelta(hours=LOOK_HOURS * 2)).isoformat(timespec="seconds")
    sent[LAST_RUN] = now.isoformat(timespec="seconds")
    try:
        tmp = folder / f"{LEDGER}.{os.getpid()}"
        tmp.write_text(json.dumps({k: v for k, v in sent.items() if str(v) >= cut}, indent=1))
        os.replace(tmp, folder / LEDGER)
        return True
    except OSError as e:
        logger.warning(f"preemption notices: the ledger can't be saved ({e}): nothing sent")
        return False


def hub_preemption_notices(combined_path, cache_dir=None, config: dict | None = None,
                           now: datetime | None = None, send=None, config_target=None,
                           domain: str | None = None) -> list[str]:
    """Email what is due when [submit.preemption] notices = true: one message
    a person and site a run, each preempted job in one message once. The
    first run (no ledger yet) only notes what is there: turning notices on
    doesn't send the last two days at once. Nothing is sent when the ledger
    can't be saved (it would be sent again at every sync). Returns the keys
    sent."""
    from nomad import preemption as pre
    if not pre.settings(config).notices:
        return []
    if domain is None:
        try:
            from nomad.condo import config as ccfg
            domain = ccfg.load(None, config_target).delivery.mail_domain
        except Exception as e:                 # noqa: BLE001 -- no report.toml: no addresses
            logger.warning(f"preemption notices: report.toml's mail_domain can't be read: {e}")
            return []
    now = now or datetime.now()
    combined_path = Path(combined_path)
    folder = Path(cache_dir) if cache_dir is not None else combined_path.parent
    first = not (folder / LEDGER).exists()
    try:
        sent = {} if first else json.loads((folder / LEDGER).read_text())
        sent = sent if isinstance(sent, dict) else {}
    except (OSError, ValueError):
        sent = {}
    # notices off for a while (or the hub stopped): its ledger is old, and what
    # the last two days hold isn't news to tell at once
    try:
        if not first and now - datetime.fromisoformat(str(sent.get(LAST_RUN))) > timedelta(hours=LOOK_HOURS):
            first = True
    except (TypeError, ValueError):
        first = True
    try:
        conn = sqlite3.connect(f"file:{combined_path}?mode=ro", uri=True, timeout=30)
        try:
            items = due(conn, domain, now, sent)
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    if first:
        for key, *_rest in items:
            sent[key] = now.isoformat(timespec="seconds")
        _save(folder, sent, now)
        return []
    if not _save(folder, sent, now) or not items:
        return []
    if send is None:
        from nomad import mail
        cfg = mail.settings(config, ((config or {}).get("alerts") or {}).get("email"))

        def send(m):
            mail.send(m, cfg)
    by: dict = {}
    for key, site, row, treated, to in items:
        by.setdefault((site, to), []).append((key, row, treated))
    done = []
    for (site, to), got in list(by.items())[:MAX_A_RUN]:
        try:
            send(message(site, [(row, treated) for _k, row, treated in got], to))
        except Exception as e:                 # noqa: BLE001 -- tried again next run
            logger.warning(f"preemption notice to {to} not sent: {e}")
            continue
        for key, _row, _t in got:
            sent[key] = now.isoformat(timespec="seconds")
            done.append(key)
    _save(folder, sent, now)
    return done


__all__ = ["message", "due", "hub_preemption_notices"]

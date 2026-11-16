# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
from __future__ import annotations

"""
Alert Dispatcher - Routes alerts to configured backends.

Usage:
    from nomad.alerts import AlertDispatcher, send_alert
    
    # Using dispatcher directly
    dispatcher = AlertDispatcher(config)
    dispatcher.dispatch(alert)
    
    # Using convenience function
    send_alert(
        severity='WARNING',
        source='disk',
        message='Disk usage at 90%',
        host='compute-01'
    )
"""

import json
import logging
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .backends import EmailBackend, SlackBackend, WebhookBackend

logger = logging.getLogger(__name__)

# Global dispatcher instance
_dispatcher: AlertDispatcher | None = None


class AlertDispatcher:
    """Routes alerts to configured notification backends."""

    def __init__(self, config: dict):
        """
        Initialize dispatcher with configuration.
        
        Config structure:
            [alerts]
            min_severity = "warning"  # Only dispatch warning and above
            cooldown_minutes = 15     # Don't repeat same alert within this window
            
            [alerts.email]
            enabled = true
            smtp_server = "smtp.example.com"
            recipients = ["admin@example.com"]
            
            [alerts.slack]
            enabled = true
            webhook_url = "https://hooks.slack.com/..."
            
            [alerts.webhook]
            enabled = true
            url = "https://api.example.com/alerts"
        """
        self.config = config.get('alerts', {})
        # Which cluster or site the alerts come from -- the same name the
        # node_state collector records -- so a message says where it is from.
        try:
            from nomad.config import resolve_cluster_name
            self.site = resolve_cluster_name(config) if config else None
        except Exception:
            self.site = None
        self.min_severity = self.config.get('min_severity', 'warning').lower()
        # Without a database (nothing to remember between runs): the same
        # alert is held back for this long, in this process only.
        self.cooldown_minutes = self.config.get('cooldown_minutes', 15)
        # With a database, a condition (a disk path, a mount, a node) is
        # raised when it appears or gets worse, then reminded of every
        # reminder_hours while it lasts. Not seen for episode_gap_minutes,
        # it has ended; seen again later, it is a new episode.
        self.reminder_hours = float(self.config.get('reminder_hours', 24))
        self.episode_gap_minutes = float(self.config.get('episode_gap_minutes', 60))
        # What may pass on its own waits before it is mailed (1.49.0): a
        # warning is raised once it has lasted its source's wait ([alerts.wait],
        # minutes; critical at once), or when it comes back the recur_times-th
        # time within recur_days. Every episode is kept (alert_episodes).
        self.waits = waits(self.config.get('wait'))
        # The Console, for a link in each alert to what it is about (1.49.0):
        # [alerts] link, else [notices] link (the hub's).
        link = self.config.get('link') or ((config or {}).get('notices') or {}).get('link')
        self.link = link.rstrip('/') if isinstance(link, str) and _LINK.match(link.strip()) else None
        self.recur_times = _int(self.config.get('recur_times'), RECUR_TIMES, 0, 50)
        self.recur_days = _int(self.config.get('recur_days'), RECUR_DAYS, 1, 90)
        # Resolve full database path
        db_rel = config.get('database', {}).get('path')
        if db_rel:
            from pathlib import Path
            data_dir = config.get('general', {}).get('data_dir',
                str(Path.home() / '.local' / 'share' / 'nomad'))
            # A leading ~ is the home directory (as in get_db_path).
            db_full = Path(data_dir).expanduser() / Path(db_rel).expanduser()
            self.db_path = str(db_full)
        else:
            self.db_path = None

        # Initialize backends
        self.backends = []

        if self.config.get('email', {}).get('enabled'):
            self.backends.append(EmailBackend(self.config['email'], config.get('mail', {})))
            logger.info("Email backend enabled")

        if self.config.get('slack', {}).get('enabled'):
            self.backends.append(SlackBackend(self.config['slack']))
            logger.info("Slack backend enabled")

        if self.config.get('webhook', {}).get('enabled'):
            self.backends.append(WebhookBackend(self.config['webhook']))
            logger.info("Webhook backend enabled")

        # Track recent alerts for deduplication
        self._recent_alerts: dict[str, datetime] = {}

    def dispatch(self, alert: dict, hold: bool = True) -> dict[str, bool]:
        """
        Dispatch alert to all enabled backends.
        
        Args:
            alert: Dict with keys:
                - severity: 'info', 'warning', 'critical'
                - source: e.g., 'disk', 'nfs', 'slurm'
                - message: Human-readable message
                - host: Hostname (optional)
                - details: Additional data (optional)
            hold: False for an alert that is not about this site's own
                state (the hub's about the sites, a site's about its hub):
                this site being in maintenance does not hold it
        
        Returns:
            Dict mapping backend name to success status
        """
        # Add timestamp if not present
        if 'timestamp' not in alert:
            alert['timestamp'] = datetime.now().isoformat()
        if self.site and not alert.get('site'):
            alert['site'] = self.site

        # Check minimum severity
        severity_order = {'info': 0, 'warning': 1, 'critical': 2}
        alert_severity = severity_order.get(alert.get('severity', 'info').lower(), 0)
        min_severity = severity_order.get(self.min_severity, 0)

        if alert_severity < min_severity:
            logger.debug(f"Alert below min severity: {alert.get('severity')} < {self.min_severity}")
            return {}

        # This site in maintenance, as its hub declared it (1.37.0): its own
        # alerts are held, not stored and not counted as raised, so what
        # still holds when the window is over is raised then, as news. A
        # test is always sent.
        if hold and not (alert.get('source') == 'test' or alert.get('test')):
            window = self._in_maintenance()
            if window:
                logger.info("Alert held: this site is in maintenance until %s (%s)",
                            datetime.fromtimestamp(window['until']).strftime('%Y-%m-%d %H:%M'),
                            window.get('reason') or 'no reason given')
                return Held()

        # Which condition this is: the same disk path, mount or node on the
        # same host, whatever its numbers. (Source, host and severity alone
        # made /home and /scratch one alert: a /home warning waited behind
        # /scratch's.)
        key = alert_key(alert)
        decision = RAISE
        tracked = False
        if alert.get('source') == 'test' or alert.get('test'):
            pass                          # nomad test-alerts, a collector's own test: always sent
        elif self.db_path:
            # Under cron every run is a new process: what has been raised is
            # remembered in the database (alert_state), not in memory.
            decision = self._advance_episode(key, alert)
            tracked = decision is not None
            if decision is None:          # the database could not be used
                # (A full disk -- nomad's own database on it -- is when
                # this happens, and when alerts matter most.)
                decision = RAISE if self._fallback_raise(key, alert) else None
            if not decision:
                logger.debug(f"Alert already raised: {key}")
                return Held()
        elif not self._outside_cooldown(key):
            logger.debug(f"Alert in cooldown: {key}")
            return Held()

        # Where to see it in the Console, when nomad knows the Console's address.
        where = console_link(self.link, alert, self.site) if not alert.get('test') else None
        if where and where not in (alert.get('message') or ''):
            alert['message'] = (alert.get('message') or '').rstrip() + f" In the Console: {where}"

        # Store in database -- once: a retry after a failed send is the
        # same alert, sent again.
        if decision != RETRY:
            self._store_alert(alert)

        # Dispatch to backends
        results = {}
        for backend in self.backends:
            backend_name = backend.__class__.__name__
            try:
                results[backend_name] = backend.send(alert)
            except Exception as e:
                logger.error(f"Backend {backend_name} failed: {e}")
                results[backend_name] = False

        if tracked:
            # Nothing reached anyone -- a backend failing, or none configured at all (1.25.7: an alert
            # raised with nowhere to go counted as sent, and a backend set up later never heard of it
            # until its reminder): tried again, as a failed send is.
            self._after_send(key, failed=not any(results.values()))
        return results

    def wait_for(self, alert: dict) -> float:
        """Minutes this alert waits before it is mailed: its source's (or
        source.kind's: policy_watch.heavy) [alerts.wait]; 0 for critical, a
        test, or a source without one."""
        if _rank(alert.get('severity')) >= _rank('critical') or alert.get('test'):
            return 0.0
        source = str(alert.get('source') or '')
        kind = str(alert.get('subject') or '').split(':', 1)[0]
        for k in (f"{source}.{kind}" if kind else None, source, "default"):
            if k and k in self.waits:
                return self.waits[k]
        return 0.0

    def _recurring(self, conn, key: str, first: str, now: datetime, recur_sent) -> list | None:
        """The earlier episodes of this condition within recur_days when this
        one is the recur_times-th (oldest first), unless its recurrence was
        mailed within recur_days; else None."""
        if not self.recur_times or self.recur_times < 2:
            return None
        since = now - timedelta(days=self.recur_days)
        if recur_sent and _time(recur_sent, since - timedelta(days=1)) >= since:
            return None
        rows = conn.execute(
            "SELECT first_seen, last_seen FROM alert_episodes WHERE key = ? AND first_seen >= ? "
            "AND first_seen < ? ORDER BY first_seen", (key, since.isoformat(), first)).fetchall()
        return rows if len(rows) + 1 >= self.recur_times else None

    def _in_maintenance(self) -> dict | None:
        """The maintenance window this site is in now (nomad.hub_watch
        site_window), unless [alerts] maintenance_hold = false. None when
        there is no database to read it from, or it can't be read."""
        if not self.db_path or self.config.get('maintenance_hold', True) is False:
            return None
        try:
            from nomad.hub_watch import site_window
            return site_window(self.db_path)
        except Exception:
            return None

    def _slack(self) -> timedelta:
        """A few minutes early, so a daily reminder from 5-minute runs stays
        within each 24 hours instead of drifting later."""
        return timedelta(minutes=10) if self.reminder_hours > 1 else timedelta(0)

    def _after_send(self, key: str, failed: bool) -> None:
        """Nothing reached anyone (mail down): try again soon, not a day
        later -- after 15 minutes, then 30, an hour... up to the reminder
        interval, so a backend that stays broken is not tried every run."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                if failed:
                    row = conn.execute("SELECT send_failures FROM alert_state WHERE key = ?",
                                       (key,)).fetchone()
                    n = (row[0] or 0) + 1 if row else 1
                    wait = min(timedelta(minutes=RETRY_MINUTES) * 2 ** min(n - 1, 12),
                               timedelta(hours=self.reminder_hours))
                    conn.execute("UPDATE alert_state SET send_failures = ?, retry_at = ? "
                                 "WHERE key = ?",
                                 (n, (datetime.now() + wait).isoformat(), key))
                else:
                    conn.execute("UPDATE alert_state SET send_failures = 0, retry_at = NULL "
                                 "WHERE key = ? AND (send_failures > 0 OR retry_at IS NOT NULL)",
                                 (key,))
                conn.commit()
            finally:
                conn.close()
        except Exception as e:                    # bookkeeping must not stop alerts
            logger.debug(f"Could not record the send for {key}: {e}")

    def _fallback_raise(self, key: str, alert: dict) -> bool:
        """When the database cannot be written: the same rules as an episode
        (new, worse, or a reminder due), remembered in a small file on local
        temporary storage, so each cron run doesn't send it again."""
        path = Path(tempfile.gettempdir()) / f"nomad-alerts-{os.getuid()}.json"
        now = datetime.now()
        sev = (alert.get('severity') or 'info').lower()
        try:
            try:
                sent = json.loads(path.read_text())
            except (OSError, ValueError):
                sent = {}
            if not isinstance(sent, dict):
                sent = {}
            last = sent.get(key)
            raise_it = (not isinstance(last, list) or len(last) < 2
                        or _rank(sev) > _rank(last[1])
                        or now - _time(last[0], now - timedelta(days=365))
                        >= timedelta(hours=self.reminder_hours) - self._slack())
            if raise_it:
                sent[key] = [now.isoformat(), sev]
                keep = now - 2 * timedelta(hours=self.reminder_hours)
                sent = {k: v for k, v in sent.items()
                        if isinstance(v, list) and v and _time(v[0], keep) >= keep}
                tmp = path.with_suffix(f".{os.getpid()}.tmp")
                tmp.write_text(json.dumps(sent))
                os.replace(tmp, path)
            return raise_it
        except OSError as e:
            logger.debug(f"No local alert memory either ({e}); in-process cooldown")
            return self._outside_cooldown(key)

    def _outside_cooldown(self, key: str) -> bool:
        """In-process cooldown, for a dispatcher without a database."""
        last = self._recent_alerts.get(key)
        now = datetime.now()
        if last is not None and (now - last).total_seconds() < self.cooldown_minutes * 60:
            return False
        self._recent_alerts[key] = now
        return True

    def _advance_episode(self, key: str, alert: dict) -> str | None:
        """Record that the condition holds now; RAISE if it is to be raised
        (stored and sent), RETRY if only sent again (an earlier send failed),
        "" if neither.

        Raised when it starts (first seen, or seen again after
        episode_gap_minutes without it), when it gets worse than it has
        been raised at in this episode (warning -> critical), and every
        reminder_hours while it lasts. A short dip (critical -> warning ->
        critical) raises nothing new; after episode_gap_minutes below its
        worst, or once reminded at the lower level, getting worse again is
        raised again. None if the database could not be used.
        """
        now = datetime.now()
        sev = (alert.get('severity') or 'info').lower()
        gap = timedelta(minutes=max(self.episode_gap_minutes, _own_gap(alert)))
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            try:
                conn.execute("BEGIN IMMEDIATE")
                _ensure_state_table(conn)
                row = conn.execute(
                    "SELECT severity, first_seen, last_seen, last_raised, raised_count, "
                    "worst_seen, retry_at, send_failures FROM alert_state WHERE key = ?",
                    (key,)).fetchone()
                new = row is None or now - _time(row[2], now - 2 * gap) > gap
                if new:
                    worst, first, raised, count, worst_seen = sev, now.isoformat(), None, 0, None
                    retry_at, failures = None, 0
                else:
                    worst, first, raised, count, worst_seen = row[0], row[1], row[3], \
                        row[4] or 0, row[5]
                    retry_at, failures = row[6], row[7] or 0
                if _rank(sev) >= _rank(worst):
                    worst_seen = now.isoformat()
                elif now - _time(worst_seen, now - 2 * gap) > gap:
                    # Below its worst for a while: getting worse again is news.
                    worst, worst_seen = sev, now.isoformat()
                worse = _rank(sev) > _rank(worst)
                due = raised is None or (
                    now - _time(raised, now)
                    >= timedelta(hours=self.reminder_hours) - self._slack())
                retry = retry_at is not None and now >= _time(retry_at, now)
                decision = RAISE if (new or worse or due) else RETRY if retry else ""
                _ensure_episodes_table(conn)
                cols = {r[1] for r in conn.execute("PRAGMA table_info(alert_state)")}
                recur_sent = conn.execute("SELECT recur_raised FROM alert_state WHERE key = ?",
                                          (key,)).fetchone() if "recur_raised" in cols else None
                recur_sent = recur_sent[0] if recur_sent else None
                wait = self.wait_for(alert)
                if decision == RAISE and raised is None and wait and \
                        now - _time(first, now) < timedelta(minutes=wait):
                    # Not mailed yet: it may pass on its own. Unless it keeps coming back.
                    decision = ""
                    earlier = self._recurring(conn, key, first, now, recur_sent)
                    if earlier is not None:
                        decision = RAISE
                        recur_sent = now.isoformat()
                        alert['message'] = (alert.get('message') or '').rstrip() + " " + \
                            recurrence_words(earlier, first, self.recur_days)
                        alert.setdefault('details', {})
                        if isinstance(alert['details'], dict):
                            alert['details']['recurring'] = len(earlier) + 1
                if decision:
                    # A reminder at a lower level is the episode's level now.
                    worst, worst_seen = sev, now.isoformat()
                if decision == RAISE:
                    raised, count = now.isoformat(), count + 1
                conn.execute(
                    "INSERT OR REPLACE INTO alert_state (key, source, host, subject, "
                    "severity, first_seen, last_seen, last_raised, raised_count, message, "
                    "last_severity, worst_seen, retry_at, send_failures, recur_raised) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, alert.get('source'), alert.get('host'), alert.get('subject'), worst,
                     first, now.isoformat(), raised, count, alert.get('message'), sev,
                     worst_seen, retry_at, failures, recur_sent))
                _record_episode(conn, key, alert, first, now, worst, count, wait)
                conn.commit()
            finally:
                conn.close()
            return decision
        except sqlite3.Error as e:
            logger.warning(f"Alert state not available ({e}); remembering sent alerts in "
                           f"{tempfile.gettempdir()} instead")
            return None

    def _store_alert(self, alert: dict):
        """Store alert in database."""
        if not self.db_path:
            return

        try:
            conn = sqlite3.connect(self.db_path)

            # Use existing alerts table schema (from migrations)
            # Columns: severity, category, source, message, details, dedup_key
            details = dict(alert.get('details') or {})
            if alert.get('host'):
                details['host'] = alert['host']
            if alert.get('site'):
                details['site'] = alert['site']

            if alert.get('subject'):
                details['subject'] = alert['subject']
            dedup_key = alert_key(alert)

            conn.execute('''
                INSERT INTO alerts
                    (timestamp, severity, category, source, message, details, dedup_key)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                alert.get('timestamp'),
                alert.get('severity'),
                alert.get('source'),
                alert.get('host', 'unknown'),
                alert.get('message'),
                json.dumps(details),
                dedup_key,
            ))

            conn.commit()
            conn.close()

        except Exception as e:
            logger.error(f"Failed to store alert: {e}")

    def test_backends(self) -> dict[str, bool]:
        """Test all configured backends."""
        results = {}
        for backend in self.backends:
            backend_name = backend.__class__.__name__
            try:
                results[backend_name] = backend.test()
            except Exception as e:
                logger.error(f"Backend {backend_name} test failed: {e}")
                results[backend_name] = False
        return results


ALERT_STATE_SQL = """
CREATE TABLE IF NOT EXISTS alert_state (
    key             TEXT PRIMARY KEY,
    source          TEXT,
    host            TEXT,
    subject         TEXT,
    severity        TEXT NOT NULL,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    last_raised     TEXT,
    raised_count    INTEGER NOT NULL DEFAULT 0,
    message         TEXT,
    last_severity   TEXT,
    worst_seen      TEXT,
    retry_at        TEXT,
    send_failures   INTEGER NOT NULL DEFAULT 0
)
"""
# After every backend failed, the first retry; each next one waits twice
# as long, up to the reminder interval.
RETRY_MINUTES = 15
RAISE, RETRY = "raise", "retry"
_STATE_COLUMNS = {"last_severity": "TEXT", "worst_seen": "TEXT", "retry_at": "TEXT",
                  "send_failures": "INTEGER NOT NULL DEFAULT 0", "recur_raised": "TEXT"}

# Each episode of each condition (1.49.0): first and last seen, how often it
# was mailed (0: it passed within its wait), how long it waited; the Console's
# Alerts > Now shows the last days' from it.
ALERT_EPISODES_SQL = """
CREATE TABLE IF NOT EXISTS alert_episodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT NOT NULL,
    source          TEXT,
    host            TEXT,
    subject         TEXT,
    site            TEXT,
    severity        TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    raised_count    INTEGER NOT NULL DEFAULT 0,
    wait_minutes    REAL,
    message         TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_episodes_key ON alert_episodes(key, first_seen);
CREATE INDEX IF NOT EXISTS idx_alert_episodes_seen ON alert_episodes(last_seen);
"""
# The waits mailing holds what may pass on its own for (minutes), by source
# or source.kind; [alerts.wait] in nomad.toml over these. spydur, one week:
# 24 CPU clock alerts, each over in 15-90 minutes.
WAITS = {"clock": 60.0, "policy_watch.heavy": 30.0}
RECUR_TIMES, RECUR_DAYS = 3, 7
EPISODES_KEEP_DAYS = 90


def _ensure_state_table(conn) -> None:
    conn.execute(ALERT_STATE_SQL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(alert_state)")}
    for col, kind in _STATE_COLUMNS.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE alert_state ADD COLUMN {col} {kind}")


def _ensure_episodes_table(conn) -> None:
    for stmt in ALERT_EPISODES_SQL.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def _record_episode(conn, key: str, alert: dict, first: str, now: datetime, severity: str,
                    raised_count: int, wait: float) -> None:
    """This sighting in its episode's row (one row per episode, as it goes);
    episodes older than EPISODES_KEEP_DAYS go."""
    args = (now.isoformat(), severity, raised_count, (alert.get('message') or '')[:2000], wait, key, first)
    cur = conn.execute("UPDATE alert_episodes SET last_seen = ?, severity = ?, raised_count = ?, message = ?, "
                       "wait_minutes = ? WHERE key = ? AND first_seen = ?", args)
    if not cur.rowcount:
        conn.execute("INSERT INTO alert_episodes (key, source, host, subject, site, severity, first_seen, "
                     "last_seen, raised_count, wait_minutes, message) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (key, alert.get('source'), alert.get('host'), alert.get('subject'), alert.get('site'),
                      severity, first, now.isoformat(), raised_count, wait, (alert.get('message') or '')[:2000]))
        conn.execute("DELETE FROM alert_episodes WHERE last_seen < ?",
                     ((now - timedelta(days=EPISODES_KEEP_DAYS)).isoformat(),))


_LINK = re.compile(r"^https?://[^\s<>\"']+$")


def console_link(link: str | None, alert: dict, here: str | None = None) -> str | None:
    """The Console's page for what an alert is about: a node's timeline for
    its CPU clock, Policy watch for its findings, else Alerts."""
    if not link:
        return None
    from urllib.parse import quote
    source = alert.get('source')
    details = alert.get('details') if isinstance(alert.get('details'), dict) else {}
    if source == 'clock' and alert.get('host'):
        site = details.get('cluster') or str(alert.get('site') or here or '').split('/')[0]
        return f"{link}/nodes/timeline?site={quote(site)}&node={quote(str(alert['host']))}"
    if source == 'policy_watch':
        return f"{link}/watch"
    return f"{link}/alerts"


def _int(v, default: int, lo: int, hi: int) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi else default


def waits(raw) -> dict:
    """[alerts.wait] over WAITS: minutes by source or source.kind
    ("default" for the rest); a value that isn't 0 or more is left out."""
    out = dict(WAITS)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if isinstance(v, dict):                   # [alerts.wait.policy_watch] heavy = 30
                for kk, vv in v.items():
                    if isinstance(vv, (int, float)) and not isinstance(vv, bool) and vv >= 0:
                        out[f"{k}.{kk}"] = float(vv)
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                out[str(k)] = float(v)
    return out


def _ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def recurrence_words(earlier: list, first: str, days: int) -> str:
    """"The 3rd time in 7 days (before: Sat 14 06:10, 30 min; Sun 15 01:25,
    15 min)." The earlier episodes, oldest first, the last five."""
    def one(a, b):
        t0 = _time(a, None)
        t1 = _time(b, None)
        if t0 is None:
            return str(a)
        mins = round(((t1 - t0).total_seconds() / 60) if t1 else 0)
        return f"{t0:%a %d %H:%M}" + (f", {mins} min" if mins else ", one reading")
    shown = [one(a, b) for a, b in earlier[-5:]]
    more = len(earlier) - len(shown)
    return (f"The {_ordinal(len(earlier) + 1)} time in {days} days (before: " + "; ".join(shown)
            + (f"; and {more} more" if more else "") + ").")

_RANKS = {'info': 0, 'warning': 1, 'critical': 2}


def _rank(severity) -> int:
    return _RANKS.get(str(severity or '').lower(), 0)


def _time(text, default: datetime) -> datetime:
    try:
        return datetime.fromisoformat(str(text))
    except (TypeError, ValueError):
        return default


def _own_gap(alert: dict) -> float:
    """A condition's own episode gap, in minutes (0: none), for one read less
    often than episode_gap_minutes allows (a compute node's local disk)."""
    details = alert.get('details')
    g = details.get('episode_gap_minutes') if isinstance(details, dict) else None
    return float(g) if isinstance(g, (int, float)) and not isinstance(g, bool) and g > 0 else 0.0


class Held(dict):
    """What dispatch returns when it sent nothing because it holds the
    alert back itself: already raised and no reminder due, a retry after a
    failed send not due yet, or a cooldown. Empty, so it reads as {} -- but
    not "sent": nomad.alerts.once asks again next run (1.17.3)."""


def alert_key(alert: dict) -> str:
    """The condition an alert is about: source|host|subject, and the metric
    when the alert has one (an NFS mount's latency and its retransmissions
    are two conditions)."""
    metric = (alert.get('details') or {}).get('metric') \
        if isinstance(alert.get('details'), dict) else None
    parts = [alert.get('source'), alert.get('host'), alert.get('subject')]
    if metric:
        parts.append(metric)
    return "|".join(str(p or '').replace("|", "/") for p in parts)


def init_dispatcher(config: dict):
    """Initialize global dispatcher."""
    global _dispatcher
    _dispatcher = AlertDispatcher(config)
    return _dispatcher


def get_dispatcher() -> AlertDispatcher | None:
    """Get global dispatcher instance."""
    return _dispatcher


def send_alert(
    severity: str,
    source: str,
    message: str,
    host: str = None,
    details: dict = None,
    config: dict = None,
    subject: str = None,
    site: str = None,
    test: bool = False,
    hold: bool = True,
) -> dict[str, bool]:
    """
    Convenience function to send an alert.
    
    Args:
        severity: 'info', 'warning', 'critical'
        source: Alert source (e.g., 'disk', 'nfs', 'slurm')
        message: Human-readable message
        host: Hostname (optional)
        details: Additional data (optional)
        config: Config dict (uses global dispatcher if not provided)
        subject: What on the host it is about -- a path, a mount, a GPU
            (optional); with source and host it identifies the condition
        site: The cluster it is about, when not this one (the hub sending a
            site's node alerts, 1.17.3): its name in the subject and the
            record, instead of this host's
        test: A test of this alert (nomad temps --check --send, 1.25.7):
            always sent, never counted as raised
        hold: False when the alert is not about this site's own state (the
            hub's about its sites, a site's about its hub): this site being
            in maintenance does not hold it (1.37.0)
    
    Returns:
        Dict mapping backend name to success status
    
    Example:
        send_alert(
            severity='critical',
            source='disk',
            message='Disk /home at 95% capacity',
            host='fileserver-01',
            details={'path': '/home', 'used_pct': 95}
        )
    """
    global _dispatcher

    if config:
        dispatcher = AlertDispatcher(config)
    elif _dispatcher:
        dispatcher = _dispatcher
    else:
        logger.warning("No dispatcher configured, alert not sent")
        return {}

    alert = {
        'severity': severity,
        'source': source,
        'message': message,
        'host': host or 'unknown',
        'details': details or {}
    }
    if subject:
        alert['subject'] = str(subject)
    if site:
        alert['site'] = str(site)
    if test:
        alert['test'] = True

    return dispatcher.dispatch(alert, hold=hold)

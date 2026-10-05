# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Threshold-based alert triggering for collectors.

Monitors collected data and dispatches alerts when thresholds are exceeded.

Configuration example (nomad.toml):
    [alerts.thresholds.disk]
    warning = 80    # Warn at 80% usage
    critical = 95   # Critical at 95%
    
    [alerts.thresholds.nfs]
    retrans_warning = 1.0    # Retransmit % warning
    latency_critical = 100   # RTT ms critical
    
    [alerts.thresholds.gpu]
    memory_warning = 90
    temperature_critical = 85

Disks are also forecast: the disk collector fits the last hours of readings
(``forecast_window_hours``) and, when a filesystem is filling, says when it
will be full; ``full_within_hours_warning`` / ``_critical`` decide when that
is an alert (source ``disk_forecast``).

Each alert names what it is about (``subject``: the disk path, the mount, the
GPU, the node), so the dispatcher treats ``/home`` and ``/scratch`` on one
host as two conditions, raised and reminded of separately.
"""

import copy
import logging
from datetime import datetime

from .dispatcher import get_dispatcher, init_dispatcher, send_alert

logger = logging.getLogger(__name__)

# Default thresholds
DEFAULT_THRESHOLDS = {
    'disk': {
        'used_percent_warning': 80,
        'used_percent_critical': 95,
        # Forecast: full within this many hours at the recent fill rate.
        'full_within_hours_warning': 72,
        'full_within_hours_critical': 24,
    },
    'nfs': {
        'retrans_percent_warning': 1.0,
        'retrans_percent_critical': 5.0,
        'avg_rtt_ms_warning': 50,
        'avg_rtt_ms_critical': 100,
    },
    'gpu': {
        'memory_percent_warning': 90,
        'memory_percent_critical': 98,
        'temperature_warning': 80,
        'temperature_critical': 90,
    },
    'node': {
        'load_warning': 0.9,      # load / n_cpus
        'load_critical': 1.5,
        'memory_percent_warning': 90,
        'memory_percent_critical': 98,
    },
    'job': {
        'failure_rate_warning': 0.2,   # 20% failure rate
        'failure_rate_critical': 0.5,  # 50% failure rate
    },
    'interactive': {
        'idle_sessions_warning': 50,       # Total idle sessions
        'idle_sessions_critical': 100,
        'memory_gb_warning': 32,           # Total memory held
        'memory_gb_critical': 64,
        'stale_sessions_warning': 5,       # Sessions idle >24h
        'stale_sessions_critical': 20,
        'user_idle_sessions_warning': 5,   # Per-user idle sessions
        'user_idle_sessions_critical': 10,
    }
}


# Keys the example nomad.toml documented flat under [alerts.thresholds],
# which were never read: their place in the nested form. (Its
# disk_fill_days_warning, like [alerts.predictive]'s days, was written for
# a forecast that never ran; the forecast starts from its own defaults.)
_FLAT_KEYS = {
    'disk_warning_percent': ('disk', 'used_percent_warning'),
    'disk_critical_percent': ('disk', 'used_percent_critical'),
}


def subject_of(collector_name: str, item: dict):
    """What on the host an item is about: the condition's name."""
    if collector_name == 'disk':
        return item.get('path')
    if collector_name == 'nfs':
        return item.get('mount_point')
    if collector_name == 'gpu':
        idx = item.get('gpu_index', item.get('index'))
        node = item.get('node_name') or item.get('node')
        if idx is None:
            return node
        return f"{node}:{idx}" if node else str(idx)
    if collector_name == 'node':
        return item.get('hostname') or item.get('node_name') or item.get('node')
    return None


def thresholds_from(config: dict) -> dict:
    """Built-in thresholds with the site's laid over them."""
    out = copy.deepcopy(DEFAULT_THRESHOLDS)
    alerts = config.get('alerts', {}) or {}
    for category, values in (alerts.get('thresholds', {}) or {}).items():
        if isinstance(values, dict):
            for key, value in values.items():
                number = _number(value)
                if number is None:
                    logger.warning(f"[alerts.thresholds.{category}] {key} = {value!r} "
                                   "is not a number; left out")
                else:
                    out.setdefault(category, {})[key] = number
        elif category in _FLAT_KEYS:
            cat, key = _FLAT_KEYS[category]
            number = _number(values)
            if number is None:
                logger.warning(f"[alerts.thresholds] {category} = {values!r} is not a number")
            else:
                out[cat][key] = number
        else:
            logger.debug(f"[alerts.thresholds] {category} is not a threshold nomad reads")
    predictive = alerts.get('predictive', {}) or {}
    out['disk']['forecast_enabled'] = bool(predictive.get('enabled', True))
    return out


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ThresholdChecker:
    """Check collected data against thresholds and trigger alerts."""

    def __init__(self, config: dict):
        """
        Config structure:
            [alerts]
            enabled = true

            [alerts.thresholds.disk]
            used_percent_warning = 80
            used_percent_critical = 95
            full_within_hours_warning = 72
            full_within_hours_critical = 24
        """
        self.config = config
        self.enabled = (config.get('alerts', {}) or {}).get('enabled', True)
        # A deep copy: update() on the shared defaults changed them for
        # every checker after the first.
        self.thresholds = thresholds_from(config)

        # Initialize dispatcher if not already done
        if not get_dispatcher():
            init_dispatcher(config)

    def check(self, collector_name: str, data: list[dict], host: str = None) -> list[dict]:
        """Check collected data against thresholds; returns the alerts triggered."""
        if not self.enabled:
            return []
        alerts = []
        for item in data:
            if not isinstance(item, dict):
                continue
            # A disk collector's quota records are about people, not disks.
            if collector_name == 'disk' and item.get('type', 'filesystem') != 'filesystem':
                continue
            alerts.extend(self._check_item(collector_name, item, host))
            if collector_name == 'disk':
                alerts.extend(self._check_forecast(item, host))
        return alerts

    def _check_item(self, collector_name: str, item: dict, host: str) -> list[dict]:
        """Check a single data item against thresholds."""
        alerts = []
        thresholds = self.thresholds.get(collector_name, {})

        for key, value in item.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue

            critical_key = f"{key}_critical"
            if critical_key in thresholds and value >= thresholds[critical_key]:
                alerts.append(self._create_alert('critical', collector_name, host, key,
                                                 value, thresholds[critical_key], item))
                continue  # Don't also trigger warning

            warning_key = f"{key}_warning"
            if warning_key in thresholds and value >= thresholds[warning_key]:
                alerts.append(self._create_alert('warning', collector_name, host, key,
                                                 value, thresholds[warning_key], item))

        return alerts

    def _check_forecast(self, item: dict, host: str) -> list[dict]:
        """A disk that will be full soon at its recent fill rate."""
        t = self.thresholds.get('disk', {})
        hours = item.get('hours_until_full')
        if not t.get('forecast_enabled', True) or hours is None or hours <= 0:
            return []
        # Past its critical threshold, the disk has that alert; "full in 20
        # minutes" each time a little space is freed and taken again (spydur
        # /home, Sunday 4 Oct) adds nothing to it.
        if (item.get('used_percent') or 0) >= t.get('used_percent_critical', 95):
            return []
        if hours <= t.get('full_within_hours_critical', 24):
            severity, limit = 'critical', t.get('full_within_hours_critical', 24)
        elif hours <= t.get('full_within_hours_warning', 72):
            severity, limit = 'warning', t.get('full_within_hours_warning', 72)
        else:
            return []
        path = item.get('path', 'unknown')
        rate = (item.get('fill_rate_bytes_per_day') or 0)
        window = item.get('forecast_window_hours') or 0
        message = (f"Disk {path} will be full in about {_duration(hours)} "
                   f"(filling {_size(rate)}/day over the last {_duration(window)}; "
                   f"{item.get('used_percent', 0):.0f}% used)")
        details = {'metric': 'hours_until_full', 'value': round(hours, 1),
                   'threshold': limit, 'rate_bytes_per_day': rate,
                   'window_hours': window, 'item': item}
        send_alert(severity=severity, source='disk_forecast', message=message,
                   host=host or 'unknown', details=details, subject=path)
        logger.info(f"Alert triggered: {severity} - disk_forecast - {message}")
        return [{'severity': severity, 'source': 'disk_forecast', 'host': host or 'unknown',
                 'subject': path, 'message': message, 'details': details,
                 'timestamp': datetime.now().isoformat()}]

    def _create_alert(
        self,
        severity: str,
        source: str,
        host: str,
        metric: str,
        value: float,
        threshold: float,
        item: dict
    ) -> dict:
        """Create and dispatch an alert."""
        message = self._format_message(source, metric, value, threshold, item)
        subject = subject_of(source, item)
        alert = {
            'severity': severity,
            'source': source,
            'host': host or 'unknown',
            'subject': subject,
            'message': message,
            'details': {
                'metric': metric,
                'value': value,
                'threshold': threshold,
                'item': item
            },
            'timestamp': datetime.now().isoformat()
        }
        send_alert(severity=alert["severity"], source=alert["source"], message=alert["message"],
                   host=alert["host"], details=alert["details"], subject=subject)
        logger.info(f"Alert triggered: {severity} - {source} - {message}")
        return alert

    def _format_message(
        self,
        source: str,
        metric: str,
        value: float,
        threshold: float,
        item: dict
    ) -> str:
        """Generate human-readable alert message."""

        if source == 'disk':
            path = item.get('path', 'unknown')
            free = item.get('available_bytes')
            free = f"{_size(free)} free; " if isinstance(free, (int, float)) else ""
            return f"Disk {path} at {value:.1f}% ({free}threshold: {threshold:g}%)"

        elif source == 'nfs':
            mount = item.get('mount_point', 'unknown')
            if 'retrans' in metric:
                return f"NFS {mount} retransmit rate {value:.2f}% (threshold: {threshold:g}%)"
            elif 'rtt' in metric:
                return f"NFS {mount} latency {value:.1f}ms (threshold: {threshold:g}ms)"
            else:
                return f"NFS {mount} {metric}={value:.2f} (threshold: {threshold})"

        elif source == 'gpu':
            gpu_id = subject_of('gpu', item) or '?'
            if 'memory' in metric:
                return f"GPU {gpu_id} memory at {value:.1f}% (threshold: {threshold}%)"
            elif 'temp' in metric:
                return f"GPU {gpu_id} temperature {value:.0f}°C (threshold: {threshold}°C)"
            else:
                return f"GPU {gpu_id} {metric}={value:.2f} (threshold: {threshold})"

        elif source == 'node':
            node_name = item.get('hostname', item.get('node', 'unknown'))
            if 'load' in metric:
                return f"Node {node_name} load {value:.2f} (threshold: {threshold})"
            elif 'memory' in metric:
                return f"Node {node_name} memory at {value:.1f}% (threshold: {threshold}%)"
            else:
                return f"Node {node_name} {metric}={value:.2f} (threshold: {threshold})"

        else:
            return f"{source}: {metric}={value:.2f} exceeded threshold {threshold}"


def _duration(hours: float) -> str:
    if hours < 1:
        m = max(1, round(hours * 60))
        return f"{m} minute{'s' if m != 1 else ''}"
    if hours < 48:
        h = round(hours)
        return f"{h} hour{'s' if h != 1 else ''}"
    return f"{hours / 24:.1f} days"


def _size(n: float) -> str:
    for unit, scale in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6)):
        if abs(n) >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n:.0f} B"


def check_and_alert(collector_name: str, data: list[dict], config: dict, host: str = None) -> list[dict]:
    """
    Check a collector's data and trigger alerts.

    Example:
        from nomad.alerts.thresholds import check_and_alert

        data = disk_collector.collect()
        alerts = check_and_alert('disk', data, config, host='compute-01')
    """
    checker = ThresholdChecker(config)
    return checker.check(collector_name, data, host)

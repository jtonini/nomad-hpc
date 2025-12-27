"""
Alert Dispatcher - Routes alerts to configured backends.

Usage:
    from nomade.alerts import AlertDispatcher, send_alert
    
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
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .backends import EmailBackend, SlackBackend, WebhookBackend

logger = logging.getLogger(__name__)

# Global dispatcher instance
_dispatcher: Optional['AlertDispatcher'] = None


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
        self.min_severity = self.config.get('min_severity', 'warning').lower()
        self.cooldown_minutes = self.config.get('cooldown_minutes', 15)
        self.db_path = config.get('database', {}).get('path')
        
        # Initialize backends
        self.backends = []
        
        if self.config.get('email', {}).get('enabled'):
            self.backends.append(EmailBackend(self.config['email']))
            logger.info("Email backend enabled")
        
        if self.config.get('slack', {}).get('enabled'):
            self.backends.append(SlackBackend(self.config['slack']))
            logger.info("Slack backend enabled")
        
        if self.config.get('webhook', {}).get('enabled'):
            self.backends.append(WebhookBackend(self.config['webhook']))
            logger.info("Webhook backend enabled")
        
        # Track recent alerts for deduplication
        self._recent_alerts: dict[str, datetime] = {}
    
    def dispatch(self, alert: dict) -> dict[str, bool]:
        """
        Dispatch alert to all enabled backends.
        
        Args:
            alert: Dict with keys:
                - severity: 'info', 'warning', 'critical'
                - source: e.g., 'disk', 'nfs', 'slurm'
                - message: Human-readable message
                - host: Hostname (optional)
                - details: Additional data (optional)
        
        Returns:
            Dict mapping backend name to success status
        """
        # Add timestamp if not present
        if 'timestamp' not in alert:
            alert['timestamp'] = datetime.now().isoformat()
        
        # Check minimum severity
        severity_order = {'info': 0, 'warning': 1, 'critical': 2}
        alert_severity = severity_order.get(alert.get('severity', 'info').lower(), 0)
        min_severity = severity_order.get(self.min_severity, 0)
        
        if alert_severity < min_severity:
            logger.debug(f"Alert below min severity: {alert.get('severity')} < {self.min_severity}")
            return {}
        
        # Check cooldown (deduplication)
        alert_key = f"{alert.get('source')}:{alert.get('host')}:{alert.get('severity')}"
        if alert_key in self._recent_alerts:
            last_time = self._recent_alerts[alert_key]
            if (datetime.now() - last_time).total_seconds() < self.cooldown_minutes * 60:
                logger.debug(f"Alert in cooldown: {alert_key}")
                return {}
        
        self._recent_alerts[alert_key] = datetime.now()
        
        # Store in database
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
        
        return results
    
    def _store_alert(self, alert: dict):
        """Store alert in database."""
        if not self.db_path:
            return
        
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    severity TEXT,
                    source TEXT,
                    host TEXT,
                    message TEXT,
                    details TEXT,
                    resolved INTEGER DEFAULT 0,
                    resolved_at TEXT
                )
            ''')
            
            conn.execute('''
                INSERT INTO alerts (timestamp, severity, source, host, message, details)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                alert.get('timestamp'),
                alert.get('severity'),
                alert.get('source'),
                alert.get('host'),
                alert.get('message'),
                json.dumps(alert.get('details', {}))
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


def init_dispatcher(config: dict):
    """Initialize global dispatcher."""
    global _dispatcher
    _dispatcher = AlertDispatcher(config)
    return _dispatcher


def get_dispatcher() -> Optional[AlertDispatcher]:
    """Get global dispatcher instance."""
    return _dispatcher


def send_alert(
    severity: str,
    source: str,
    message: str,
    host: str = None,
    details: dict = None,
    config: dict = None
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
    
    return dispatcher.dispatch(alert)

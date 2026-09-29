# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Notification backends for alert dispatch.

Each backend handles a specific notification channel:
- Email (SMTP)
- Slack (webhook)
- Generic Webhook (HTTP POST)
"""

import json
import logging
from abc import ABC, abstractmethod
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


class NotificationBackend(ABC):
    """Base class for notification backends."""

    def __init__(self, config: dict):
        self.config = config
        self.enabled = config.get('enabled', False)

    @abstractmethod
    def send(self, alert: dict) -> bool:
        """Send alert notification. Returns True on success."""
        pass

    @abstractmethod
    def test(self) -> bool:
        """Test the backend configuration. Returns True if working."""
        pass


class EmailBackend(NotificationBackend):
    """Send alerts by email, through nomad.mail.

    `config` is [alerts.email]: `enabled` and `recipients`, and optionally its
    own server settings. `mail` is the shared [mail] section, used for anything
    [alerts.email] does not set itself.
    """

    def __init__(self, config: dict, mail: dict | None = None):
        super().__init__(config)
        from nomad import mail as _mail
        self._mail = _mail
        # `to_addrs` is how the documentation spelled it until 1.7.5.
        self.recipients = config.get('recipients') or config.get('to_addrs') or []
        self.settings_error = None
        own = dict(config)
        if ('smtp_server' in own and not own.get('smtp_port') and not own.get('port')
                and not (mail or {}).get('port')):
            own['port'] = 587   # what [alerts.email] has always defaulted to
        try:
            self.mail_settings = _mail.settings({'mail': mail or {}}, own)
        except ValueError as exc:
            self.mail_settings = None
            self.settings_error = str(exc)
        self.from_addr = (self.mail_settings or {}).get('from') or 'nomad@localhost'

    def send(self, alert: dict) -> bool:
        if not self.enabled or not self.recipients:
            return False
        if self.mail_settings is None:
            logger.error(f"Email not sent: {self.settings_error}")
            return False

        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = self._format_subject(alert)
            msg['From'] = self.from_addr
            msg['To'] = ', '.join(self.recipients)

            text_body = self._format_text(alert)
            msg.attach(MIMEText(text_body, 'plain'))

            html_body = self._format_html(alert)
            msg.attach(MIMEText(html_body, 'html'))

            self._mail.send(msg, self.mail_settings)
            logger.info(f"Email sent to {self.recipients}")
            return True

        except Exception as e:
            logger.error(f"Email send failed: {self._mail.failure_label(e)}: {e}")
            return False

    def test(self) -> bool:
        if self.mail_settings is None:
            logger.error(f"SMTP test failed: {self.settings_error}")
            return False
        try:
            self._mail.check(self.mail_settings)
            return True
        except Exception as e:
            logger.error(f"SMTP test failed: {self._mail.failure_label(e)}: {e}")
            return False

    def _format_subject(self, alert: dict) -> str:
        severity = alert.get('severity', 'INFO').upper()
        source = alert.get('source', 'NØMAÐ')
        return f"[{severity}] NØMAÐ Alert: {source}"

    def _format_text(self, alert: dict) -> str:
        return f"""NØMAÐ Alert
============
Severity: {alert.get('severity', 'INFO')}
Source: {alert.get('source', 'unknown')}
Host: {alert.get('host', 'unknown')}
Time: {alert.get('timestamp', 'unknown')}

Message:
{alert.get('message', 'No message')}

Details:
{json.dumps(alert.get('details', {}), indent=2)}
"""

    def _format_html(self, alert: dict) -> str:
        severity = alert.get('severity', 'INFO').upper()
        color = {'CRITICAL': '#e74c3c', 'WARNING': '#f39c12', 'INFO': '#3498db'}.get(severity, '#95a5a6')

        return f"""
<html>
<body style="font-family: Arial, sans-serif; padding: 20px;">
    <div style="background: {color}; color: white; padding: 10px 20px; border-radius: 4px;">
        <h2 style="margin: 0;">NØMAÐ Alert: {severity}</h2>
    </div>
    <div style="padding: 20px; background: #f9f9f9; border-radius: 4px; margin-top: 10px;">
        <p><strong>Source:</strong> {alert.get('source', 'unknown')}</p>
        <p><strong>Host:</strong> {alert.get('host', 'unknown')}</p>
        <p><strong>Time:</strong> {alert.get('timestamp', 'unknown')}</p>
        <hr>
        <p><strong>Message:</strong></p>
        <p>{alert.get('message', 'No message')}</p>
    </div>
</body>
</html>
"""


class SlackBackend(NotificationBackend):
    """Send alerts to Slack via webhook."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.webhook_url = config.get('webhook_url')
        self.channel = config.get('channel')
        self.username = config.get('username', 'NØMAÐ')
        self.icon_emoji = config.get('icon_emoji', ':warning:')

    def send(self, alert: dict) -> bool:
        if not self.enabled or not self.webhook_url:
            return False

        try:
            severity = alert.get('severity', 'INFO').upper()
            color = {'CRITICAL': '#e74c3c', 'WARNING': '#f39c12', 'INFO': '#3498db'}.get(severity, '#95a5a6')

            payload = {
                'username': self.username,
                'icon_emoji': self.icon_emoji,
                'attachments': [{
                    'color': color,
                    'title': f"NØMAÐ Alert: {severity}",
                    'text': alert.get('message', 'No message'),
                    'fields': [
                        {'title': 'Source', 'value': alert.get('source', 'unknown'), 'short': True},
                        {'title': 'Host', 'value': alert.get('host', 'unknown'), 'short': True},
                        {'title': 'Time', 'value': alert.get('timestamp', 'unknown'), 'short': True},
                    ],
                    'footer': 'NØMAÐ HPC Monitor'
                }]
            }

            if self.channel:
                payload['channel'] = self.channel

            req = Request(
                self.webhook_url,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )

            with urlopen(req, timeout=10) as response:
                if response.status == 200:
                    logger.info("Slack notification sent")
                    return True

            return False

        except Exception as e:
            logger.error(f"Slack send failed: {e}")
            return False

    def test(self) -> bool:
        if not self.webhook_url:
            return False
        try:
            payload = {'text': 'NØMAÐ test message - configuration working!'}
            req = Request(
                self.webhook_url,
                data=json.dumps(payload).encode('utf-8'),
                headers={'Content-Type': 'application/json'}
            )
            with urlopen(req, timeout=10) as response:
                return response.status == 200
        except Exception as e:
            logger.error(f"Slack test failed: {e}")
            return False


class WebhookBackend(NotificationBackend):
    """Send alerts to generic HTTP webhook."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.url = config.get('url')
        self.method = config.get('method', 'POST')
        self.headers = config.get('headers', {})
        self.auth_token = config.get('auth_token')

    def send(self, alert: dict) -> bool:
        if not self.enabled or not self.url:
            return False

        try:
            headers = {'Content-Type': 'application/json'}
            headers.update(self.headers)

            if self.auth_token:
                headers['Authorization'] = f'Bearer {self.auth_token}'

            payload = {
                'event': 'nomad_alert',
                'alert': alert
            }

            req = Request(
                self.url,
                data=json.dumps(payload).encode('utf-8'),
                headers=headers,
                method=self.method
            )

            with urlopen(req, timeout=10) as response:
                if response.status in (200, 201, 202):
                    logger.info(f"Webhook sent to {self.url}")
                    return True

            return False

        except Exception as e:
            logger.error(f"Webhook send failed: {e}")
            return False

    def test(self) -> bool:
        return bool(self.url)

"""
Notification backends for alert dispatch.

Each backend handles a specific notification channel:
- Email (SMTP)
- Slack (webhook)
- Generic Webhook (HTTP POST)
"""

import json
import logging
import smtplib
import ssl
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
    """Send alerts via SMTP email."""
    
    def __init__(self, config: dict):
        super().__init__(config)
        self.smtp_server = config.get('smtp_server', 'localhost')
        self.smtp_port = config.get('smtp_port', 587)
        self.use_tls = config.get('use_tls', True)
        self.username = config.get('username')
        self.password = config.get('password')
        self.from_addr = config.get('from_address', 'nomade@localhost')
        self.recipients = config.get('recipients', [])
    
    def send(self, alert: dict) -> bool:
        if not self.enabled or not self.recipients:
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
            
            if self.use_tls:
                context = ssl.create_default_context()
                with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                    server.starttls(context=context)
                    if self.username and self.password:
                        server.login(self.username, self.password)
                    server.sendmail(self.from_addr, self.recipients, msg.as_string())
            else:
                with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                    if self.username and self.password:
                        server.login(self.username, self.password)
                    server.sendmail(self.from_addr, self.recipients, msg.as_string())
            
            logger.info(f"Email sent to {self.recipients}")
            return True
            
        except Exception as e:
            logger.error(f"Email send failed: {e}")
            return False
    
    def test(self) -> bool:
        try:
            if self.use_tls:
                context = ssl.create_default_context()
                with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                    server.starttls(context=context)
                    return True
            else:
                with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                    return True
        except Exception as e:
            logger.error(f"SMTP test failed: {e}")
            return False
    
    def _format_subject(self, alert: dict) -> str:
        severity = alert.get('severity', 'INFO').upper()
        source = alert.get('source', 'NOMADE')
        return f"[{severity}] NOMADE Alert: {source}"
    
    def _format_text(self, alert: dict) -> str:
        return f"""NOMADE Alert
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
        <h2 style="margin: 0;">NOMADE Alert: {severity}</h2>
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
        self.username = config.get('username', 'NOMADE')
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
                    'title': f"NOMADE Alert: {severity}",
                    'text': alert.get('message', 'No message'),
                    'fields': [
                        {'title': 'Source', 'value': alert.get('source', 'unknown'), 'short': True},
                        {'title': 'Host', 'value': alert.get('host', 'unknown'), 'short': True},
                        {'title': 'Time', 'value': alert.get('timestamp', 'unknown'), 'short': True},
                    ],
                    'footer': 'NOMADE HPC Monitor'
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
            payload = {'text': 'NOMADE test message - configuration working!'}
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
                'event': 'nomade_alert',
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

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Sending mail: one sender for every part of NØMAÐ that sends it.

Settings live in the [mail] section of nomad.toml:

    [mail]
    host = "smtp.your-institution.edu"   # default: localhost
    port = 25                            # default: 25
    starttls = "required"                # required | if-offered | off
    tls_name = ""                        # the name on the server's certificate,
                                         # when it is not `host`
    ca_bundle = ""                       # trusted roots; default: the OS bundle
    extra_ca = ""                        # an intermediate the server fails to send
    from = "hpc@your-institution.edu"    # a sender the server accepts
    username = ""                        # only if the server needs a login
    password_file = ""                   # a file holding the password (mode 0600)

`starttls` defaults to "required", except for a server on this machine, where
the message never crosses the network. The certificate is always verified:
against the operating system's CA bundle rather than whichever one the Python
build ships, optionally completed by an intermediate the server fails to send
(`extra_ca`), and against `tls_name` when a server in a pool presents the
pool's certificate.

A section that sends mail for its own purpose -- [alerts.email] -- may name a
different server with its own keys; otherwise it uses [mail]. Its older key
names (smtp_server, smtp_port, use_tls, from_address, smtp_username,
smtp_password) are still understood.
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import Message
from pathlib import Path

from nomad.config import read_secret

LOCAL_HOSTS = ('localhost', '127.0.0.1', '::1')
SYSTEM_CA_BUNDLES = (
    '/etc/pki/tls/certs/ca-bundle.crt',       # RHEL, Rocky, Fedora
    '/etc/ssl/certs/ca-certificates.crt',     # Debian, Ubuntu
    '/etc/ssl/cert.pem',                      # macOS, Alpine
)
STARTTLS_MODES = ('required', 'if-offered', 'off')

# Older key names, as [alerts.email] has always spelled them.
_ALIASES = {
    'smtp_server': 'host',
    'smtp_host': 'host',
    'smtp_port': 'port',
    'from_address': 'from',
    'from_email': 'from',
    'from_addr': 'from',
    'smtp_username': 'username',
    'smtp_password': 'password',
    'smtp_password_file': 'password_file',
}
_KEYS = ('host', 'port', 'starttls', 'tls_name', 'ca_bundle', 'extra_ca',
         'from', 'username', 'password', 'password_file')


def _normalise(section: dict | None) -> dict:
    """The settings a section states, under [mail]'s key names."""
    out = {}
    for key, value in (section or {}).items():
        key = _ALIASES.get(key, key)
        if key == 'use_tls':
            out['starttls'] = 'required' if value else 'off'
        elif key in _KEYS and value not in (None, ''):
            out[key] = value
    return out


def settings(config: dict | None, section: dict | None = None) -> dict:
    """Mail settings for one sender.

    `config` is the whole nomad config; `section` is the sender's own section
    (e.g. config['alerts']['email']), whose settings win over [mail]'s.
    """
    merged = _normalise((config or {}).get('mail'))
    merged.update(_normalise(section))
    host = str(merged.get('host') or 'localhost')
    starttls = str(merged.get('starttls') or ('off' if host in LOCAL_HOSTS else 'required'))
    if starttls not in STARTTLS_MODES:
        raise ValueError(f"[mail] starttls must be one of {', '.join(STARTTLS_MODES)}, not {starttls!r}")
    return {
        'host': host,
        'port': int(merged.get('port') or 25),
        'starttls': starttls,
        'tls_name': merged.get('tls_name') or None,
        'ca_bundle': merged.get('ca_bundle') or None,
        'extra_ca': merged.get('extra_ca') or None,
        'from': merged.get('from') or None,
        'username': merged.get('username') or None,
        'password': read_secret(merged, 'password') or None,
    }


class _SMTP(smtplib.SMTP):
    """smtplib, checking the server's certificate against a chosen name.

    smtplib checks it against the name it connected to. A server in a pool may
    present the pool's certificate instead (cotton.richmond.edu presents
    smtp.richmond.edu); verification then still demands a valid certificate for
    exactly that name.
    """

    def __init__(self, tls_name=None, **kwargs):
        self._tls_name = tls_name
        super().__init__(**kwargs)

    def connect(self, host='localhost', port=0, source_address=None):
        reply = super().connect(host, port, source_address)
        self._host = self._tls_name or host   # the name starttls() verifies
        return reply


def tls_context(cfg: dict) -> ssl.SSLContext:
    """A verifying TLS context: the OS CA bundle, plus `extra_ca` if given."""
    bundle = cfg.get('ca_bundle')
    if bundle:
        bundle = str(Path(bundle).expanduser())
    else:
        bundle = next((p for p in SYSTEM_CA_BUNDLES if Path(p).exists()), None)
    ctx = ssl.create_default_context(cafile=bundle)
    if cfg.get('extra_ca'):
        ctx.load_verify_locations(str(Path(cfg['extra_ca']).expanduser()))
    return ctx


def _open(cfg: dict, timeout: float) -> _SMTP:
    """Connect, say hello, and secure the connection as `cfg` requires."""
    server = _SMTP(cfg.get('tls_name'), timeout=timeout)
    try:
        server.connect(cfg['host'], cfg['port'])
        server.ehlo()
        encrypted = False
        if cfg['starttls'] != 'off':
            if server.has_extn('starttls'):
                server.starttls(context=tls_context(cfg))
                server.ehlo()
                encrypted = True
            elif cfg['starttls'] == 'required':
                raise smtplib.SMTPNotSupportedError(
                    f"{cfg['host']} does not offer STARTTLS and [mail] starttls is 'required'")
        if cfg.get('username') and cfg.get('password'):
            if not encrypted and cfg['host'] not in LOCAL_HOSTS:
                raise smtplib.SMTPNotSupportedError(
                    f"refusing to send a password to {cfg['host']} over an unencrypted connection")
            server.login(cfg['username'], cfg['password'])
        return server
    except BaseException:
        server.close()
        raise


def send(message: Message, cfg: dict, timeout: float = 15) -> None:
    """Send `message` as `cfg` (from settings()) says. Raises on any failure.

    The sender is cfg['from'] unless the message already names one.
    """
    if not message.get('From'):
        if not cfg.get('from'):
            raise ValueError('no sender address: set [mail] from in nomad.toml')
        message['From'] = cfg['from']
    server = _open(cfg, timeout)
    try:
        server.send_message(message)
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            server.close()


def check(cfg: dict, timeout: float = 15) -> None:
    """Connect, secure and (if configured) log in, without sending. Raises on failure."""
    server = _open(cfg, timeout)
    try:
        server.quit()
    except smtplib.SMTPException:
        server.close()


def failure_label(exc: BaseException) -> str:
    """A short description of why sending failed, safe to show an administrator."""
    if isinstance(exc, ssl.SSLCertVerificationError):
        return f"certificate not verified ({exc.verify_message})"
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return str(exc) or 'mail server offers no encryption'
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return f"login refused by mail server ({exc.smtp_code})"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return 'recipient refused by mail server'
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return f"sender refused by mail server ({exc.smtp_code})"
    if isinstance(exc, smtplib.SMTPResponseException):
        return f"refused by mail server ({exc.smtp_code})"
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, (OSError, smtplib.SMTPException)):
        return f"could not reach mail server ({type(exc).__name__})"
    return type(exc).__name__

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Tests for nomad.mail, the one mail sender, and the [mail]/[support] settings.

A small SMTP server runs in a thread, with its own certificate authority
(root -> intermediate -> certificate for "mail.test"), so the TLS rules are
tested for real: the certificate is verified, an intermediate the server
fails to send can be supplied, and a certificate for another name is refused.
"""

import shutil
import smtplib
import socket
import ssl
import subprocess
import threading
from email.message import EmailMessage

import pytest

from nomad import mail
from nomad.config import read_secret, support_settings

# ── A certificate authority and an SMTP server to talk to ──────────────


def _openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl not available")
    d = tmp_path_factory.mktemp("pki")
    (d / "ca.ext").write_text(
        "basicConstraints=critical,CA:TRUE\n"
        "keyUsage=critical,keyCertSign,cRLSign\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n")
    (d / "leaf.ext").write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\n"
        "subjectAltName=DNS:mail.test\n"
        "subjectKeyIdentifier=hash\n"
        "authorityKeyIdentifier=keyid,issuer\n")
    _openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "root.key",
             "-out", "root.pem", "-days", "2", "-subj", "/CN=Test Root",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=d)
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "int.key",
             "-out", "int.csr", "-subj", "/CN=Test Intermediate", cwd=d)
    _openssl("x509", "-req", "-in", "int.csr", "-CA", "root.pem", "-CAkey", "root.key",
             "-CAcreateserial", "-out", "int.pem", "-days", "2", "-extfile", "ca.ext", cwd=d)
    _openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "leaf.key",
             "-out", "leaf.csr", "-subj", "/CN=mail.test", cwd=d)
    _openssl("x509", "-req", "-in", "leaf.csr", "-CA", "int.pem", "-CAkey", "int.key",
             "-CAcreateserial", "-out", "leaf.pem", "-days", "2", "-extfile", "leaf.ext", cwd=d)
    return d


class FakeSMTP:
    """Just enough ESMTP: EHLO, STARTTLS, AUTH PLAIN, MAIL, RCPT, DATA, QUIT."""

    def __init__(self, pki=None, starttls=True, auth=False, refuse_sender=False):
        self.ctx = None
        if pki is not None:
            # The server sends its own certificate only, like cotton: no intermediate.
            self.ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            self.ctx.load_cert_chain(pki / "leaf.pem", pki / "leaf.key")
        self.offer_starttls = starttls and self.ctx is not None
        self.offer_auth = auth
        self.refuse_sender = refuse_sender
        self.messages, self.logins, self.encrypted = [], [], []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                self._session(conn)
            except (OSError, ssl.SSLError):
                pass
            finally:
                conn.close()

    def _session(self, conn):
        tls = False
        f = conn.makefile("rb")

        def say(line):
            conn.sendall(line.encode() + b"\r\n")

        say("220 fake ESMTP")
        while True:
            raw = f.readline()
            if not raw:
                return
            line = raw.decode().rstrip("\r\n")
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                caps = ["fake"]
                if self.offer_starttls and not tls:
                    caps.append("STARTTLS")
                if self.offer_auth:
                    caps.append("AUTH PLAIN")
                caps.append("8BITMIME")
                for c in caps[:-1]:
                    say("250-" + c)
                say("250 " + caps[-1])
            elif verb == "STARTTLS":
                say("220 go ahead")
                conn = self.ctx.wrap_socket(conn, server_side=True)
                f = conn.makefile("rb")
                tls = True
            elif verb == "AUTH":
                self.logins.append(line)
                say("235 ok")
            elif verb == "MAIL":
                if self.refuse_sender:
                    say("553 sender not in directory")
                else:
                    say("250 ok")
            elif verb == "RCPT":
                say("250 ok")
            elif verb == "DATA":
                say("354 go")
                data = b""
                while not data.endswith(b"\r\n.\r\n"):
                    chunk = f.readline()
                    if not chunk:
                        return
                    data += chunk
                self.messages.append(data.decode(errors="replace"))
                self.encrypted.append(tls)
                say("250 queued")
            elif verb == "QUIT":
                say("221 bye")
                return
            else:
                say("250 ok")

    def close(self):
        self.sock.close()


def _message():
    m = EmailMessage()
    m["Subject"] = "test"
    m["To"] = "hpc@example.edu"
    m.set_content("hello")
    return m


def _cfg(server, pki=None, **over):
    section = {"host": "127.0.0.1", "port": server.port, "from": "hpc@example.edu"}
    if pki is not None:
        section.update(tls_name="mail.test", ca_bundle=str(pki / "root.pem"),
                       extra_ca=str(pki / "int.pem"))
    section.update(over)
    return mail.settings({"mail": section})


# ── TLS ────────────────────────────────────────────────────────────────


def test_verified_tls_with_supplied_intermediate(pki):
    server = FakeSMTP(pki)
    try:
        cfg = _cfg(server, pki, starttls="required")
        mail.send(_message(), cfg)
        assert len(server.messages) == 1
        assert server.encrypted == [True]
        assert "From: hpc@example.edu" in server.messages[0]
    finally:
        server.close()


def test_missing_intermediate_is_refused(pki):
    server = FakeSMTP(pki)
    try:
        cfg = _cfg(server, pki, starttls="required")
        cfg["extra_ca"] = None
        with pytest.raises(ssl.SSLCertVerificationError) as info:
            mail.send(_message(), cfg)
        assert server.messages == []
        assert mail.failure_label(info.value).startswith("certificate not verified")
    finally:
        server.close()


def test_certificate_for_another_name_is_refused(pki):
    server = FakeSMTP(pki)
    try:
        cfg = _cfg(server, pki, starttls="required", tls_name="other.test")
        with pytest.raises(ssl.SSLCertVerificationError):
            mail.send(_message(), cfg)
        assert server.messages == []
    finally:
        server.close()


def test_required_starttls_not_offered_sends_nothing():
    server = FakeSMTP(starttls=False)
    try:
        cfg = _cfg(server, starttls="required")
        with pytest.raises(smtplib.SMTPNotSupportedError):
            mail.send(_message(), cfg)
        assert server.messages == []
    finally:
        server.close()


def test_if_offered_sends_in_the_clear_when_not_offered():
    server = FakeSMTP(starttls=False)
    try:
        mail.send(_message(), _cfg(server, starttls="if-offered"))
        assert server.encrypted == [False]
    finally:
        server.close()


def test_no_password_over_an_unencrypted_connection(monkeypatch):
    server = FakeSMTP(starttls=False, auth=True)
    monkeypatch.setattr(mail, "LOCAL_HOSTS", ())   # treat 127.0.0.1 as remote
    try:
        cfg = _cfg(server, starttls="if-offered", username="u", password="p")
        with pytest.raises(smtplib.SMTPNotSupportedError, match="password"):
            mail.send(_message(), cfg)
        assert server.logins == [] and server.messages == []
    finally:
        server.close()


def test_login_after_starttls(pki):
    server = FakeSMTP(pki, auth=True)
    try:
        mail.send(_message(), _cfg(server, pki, starttls="required", username="u", password="p"))
        assert len(server.logins) == 1 and server.encrypted == [True]
    finally:
        server.close()


def test_refused_sender_is_labelled():
    server = FakeSMTP(starttls=False, refuse_sender=True)
    try:
        with pytest.raises(smtplib.SMTPSenderRefused) as info:
            mail.send(_message(), _cfg(server, starttls="off"))
        assert mail.failure_label(info.value) == "sender refused by mail server (553)"
    finally:
        server.close()


def test_check_connects_without_sending(pki):
    server = FakeSMTP(pki)
    try:
        mail.check(_cfg(server, pki, starttls="required"))
        assert server.messages == []
    finally:
        server.close()


# ── Settings ───────────────────────────────────────────────────────────


def test_starttls_default_depends_on_where_the_server_is():
    assert mail.settings({"mail": {"host": "localhost"}})["starttls"] == "off"
    assert mail.settings({"mail": {"host": "smtp.example.edu"}})["starttls"] == "required"
    assert mail.settings({})["host"] == "localhost"


def test_invalid_starttls_is_an_error():
    with pytest.raises(ValueError):
        mail.settings({"mail": {"host": "x", "starttls": "yes"}})


def test_alert_section_overrides_mail_and_old_names_work():
    config = {"mail": {"host": "cotton.example.edu", "port": 25, "from": "hpc@example.edu",
                       "tls_name": "smtp.example.edu"}}
    legacy = {"enabled": True, "smtp_server": "relay.example.edu", "smtp_port": 587,
              "use_tls": False, "from_address": "alerts@example.edu",
              "smtp_username": "u", "smtp_password": "p", "recipients": ["a@example.edu"]}
    cfg = mail.settings(config, legacy)
    assert cfg["host"] == "relay.example.edu" and cfg["port"] == 587
    assert cfg["starttls"] == "off" and cfg["from"] == "alerts@example.edu"
    assert cfg["username"] == "u" and cfg["password"] == "p"
    assert cfg["tls_name"] == "smtp.example.edu"     # not overridden, so [mail]'s


def test_alert_section_without_server_uses_mail():
    cfg = mail.settings({"mail": {"host": "cotton.example.edu", "from": "hpc@example.edu"}},
                        {"enabled": True, "recipients": ["a@example.edu"]})
    assert cfg["host"] == "cotton.example.edu" and cfg["from"] == "hpc@example.edu"


def test_password_file(tmp_path):
    secret = tmp_path / "pw"
    secret.write_text("s3cret\n")
    cfg = mail.settings({"mail": {"host": "x", "password_file": str(secret),
                                  "password": "ignored"}})
    assert cfg["password"] == "s3cret"


def test_no_sender_is_an_error_before_connecting():
    with pytest.raises(ValueError, match="sender"):
        mail.send(_message(), mail.settings({"mail": {"host": "unreachable.invalid"}}))


def test_read_secret(tmp_path):
    token = tmp_path / "token"
    token.write_text("ghp_abc\n")
    assert read_secret({"github_token_file": str(token)}, "github_token") == "ghp_abc"
    assert read_secret({"github_token": "inline"}, "github_token") == "inline"
    assert read_secret({"github_token_file": str(tmp_path / "missing")}, "github_token") == ""
    assert read_secret({}, "github_token") == ""


def test_support_settings_prefers_support_then_old_names():
    assert support_settings({}) == {"email": None, "institution": None, "user_email_domain": None}
    old = {"issue_reporting": {"support_email": "old@x.edu", "institution_name": "Old U"}}
    assert support_settings(old)["email"] == "old@x.edu"
    assert support_settings(old)["institution"] == "Old U"
    both = {**old, "support": {"email": "new@x.edu", "institution": "New U",
                               "user_email_domain": "x.edu"}}
    assert support_settings(both) == {"email": "new@x.edu", "institution": "New U",
                                      "user_email_domain": "x.edu"}


# ── Alert email goes through [mail] ────────────────────────────────────


def test_alert_email_uses_mail_section():
    from nomad.alerts.backends import EmailBackend
    server = FakeSMTP(starttls=False)
    try:
        backend = EmailBackend(
            {"enabled": True, "to_addrs": ["admin@example.edu"]},
            {"host": "127.0.0.1", "port": server.port, "starttls": "off",
             "from": "hpc@example.edu"})
        assert backend.recipients == ["admin@example.edu"]
        assert backend.send({"severity": "warning", "source": "disk", "message": "full"})
        assert "From: hpc@example.edu" in server.messages[0]
        assert backend.test()
    finally:
        server.close()


def test_old_alert_settings_keep_their_old_port():
    from nomad.alerts.backends import EmailBackend
    old = EmailBackend({"enabled": True, "smtp_server": "relay.example.edu",
                        "recipients": ["a@x.edu"]})
    assert old.mail_settings["port"] == 587
    assert old.mail_settings["starttls"] == "required"
    shared = EmailBackend({"enabled": True, "recipients": ["a@x.edu"]},
                          {"host": "cotton.example.edu", "port": 25})
    assert shared.mail_settings["port"] == 25


def test_alert_email_with_bad_setting_fails_quietly():
    from nomad.alerts.backends import EmailBackend
    backend = EmailBackend({"enabled": True, "recipients": ["a@x.edu"]},
                           {"host": "x", "starttls": "sometimes"})
    assert backend.send({"severity": "warning"}) is False
    assert backend.test() is False

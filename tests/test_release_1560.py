# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.56.0: licenses -- when each ends, read where it is kept, and the
center reminded before. Made-up names only."""
from __future__ import annotations

import base64
import socket
import sqlite3
import ssl
import subprocess
import threading
from datetime import date, datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad import licenses as L

CERT = """-----BEGIN CERTIFICATE-----
MIIBeDCCAR6gAwIBAgIBBzAKBggqhkjOPQQDAjAbMRkwFwYDVQQDDBBraW0uZXhh
bXBsZS50ZXN0MB4XDTI2MTExODE5NTcxN1oXDTM2MTExNTE5NTcxN1owGzEZMBcG
A1UEAwwQa2ltLmV4YW1wbGUudGVzdDBZMBMGByqGSM49AgEGCCqGSM49AwEHA0IA
BL3kwzNJEXS1eJirdk8jCniVFnzbwHaxw2PaU/623rVs/EYVdRDHR09+J2SzlkgW
sRDmcahhtSvTYRXP/MBhcj6jUzBRMB0GA1UdDgQWBBTHkZ8Ir2Y4fqxBh+0laCW/
+2x4QTAfBgNVHSMEGDAWgBTHkZ8Ir2Y4fqxBh+0laCW/+2x4QTAPBgNVHRMBAf8E
BTADAQH/MAoGCCqGSM49BAMCA0gAMEUCIQDOsPe0+TmiDe5Jes/T2PjisKAXhak3
cVuhC1UZeeH0ywIgYFADxa7EULIbTfap/9YlWoagdv6a+RXLdXZHGhAGXJc=
-----END CERTIFICATE-----
"""
KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgYm9Sqa7eLj3kIoKi
yRLydY9rFGypzlpdsBsyvMJv9JuhRANCAAS95MMzSRF0tXiYq3ZPIwp4lRZ828B2
scNj2lP+tt61bPxGFXUQx0dPfidks5ZIFrEQ5nGoYbUr02EVz/zAYXI+
-----END PRIVATE KEY-----
"""
# notAfter 2059 (GeneralizedTime)
LATE = base64.b64decode(
    "MIIBejCCASCgAwIBAgIBCTAKBggqhkjOPQQDAjAbMRkwFwYDVQQDDBBwYXQuZXhhbXBsZS50ZXN0MCAXDTI2MTExODE5NTcyMloYDzIwNTkw"
    "OTI2MTk1NzIyWjAbMRkwFwYDVQQDDBBwYXQuZXhhbXBsZS50ZXN0MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEyRrB1CrmKPL5iscgM3w9"
    "tKDPlkPdi1aVndQc+RpvOUCGyK4PR8iznW/SZxFuN0vogspRI9L3l6iK8vEYuOhzfqNTMFEwHQYDVR0OBBYEFEfV2r2IjjDh40sPRKwxrdOQ"
    "nAbBMB8GA1UdIwQYMBaAFEfV2r2IjjDh40sPRKwxrdOQnAbBMA8GA1UdEwEB/wQFMAMBAf8wCgYIKoZIzj0EAwIDSAAwRQIgVDD6kZoAd7m8"
    "dp9UuanaFhlH228OtIZHmudkTJ7qQKMCIQCnUdAz/0AOuzAi+X1B00Mpvc+mpHpjaBM9P62eje8VSg==")
TODAY = date(2026, 11, 18)
NOW = datetime(2026, 11, 18, 8, 20)


def _ran(out="", err="", rc=0, seen=None):
    def run(cmd, **kw):
        if seen is not None:
            seen.append(cmd)
        return subprocess.CompletedProcess(cmd, rc, out, err)
    return run


def test_dates_as_each_program_writes_them():
    assert L.when_of("Expiration: 2026-11-23 08:00:00") == "2026-11-23T08:00"
    assert L.when_of("2027-08-20") == "2027-08-20"
    assert L.when_of("INCREMENT x v 1.0 1-jun-2027 10") == "2027-06-01"
    assert L.when_of("notAfter=Feb 13 23:59:59 2027 GMT") == "2027-02-13T23:59"
    assert L.when_of('DateObject[{2027, 6, 30, 0, 0, 0.}, "Instant"]') == "2027-06-30"
    assert L.when_of("ends November 23, 2026") == "2026-11-23"
    assert L.when_of("ends 23 Nov 2026") == "2026-11-23"
    assert L.when_of("No valid password found.") is None
    assert L.when_of("2027-02-30") is None
    assert L._flexlm_end("permanent") == (None, True)
    assert L._flexlm_end("1-jan-0") == (None, True)
    assert L._flexlm_end("01-jun-2027") == (date(2027, 6, 1), False)


def test_items_are_read_and_what_is_wrong_is_said():
    items, problems = L.items_of({"items": [
        {"name": "Posit Workbench", "kind": "posit"},
        {"name": "Solver", "kind": "gurobi", "file": "/opt/solver/gurobi.lic", "remind_days": [90, 7]},
        {"name": "Site", "kind": "cert", "host": "web.example.edu", "port": 8443},
        {"name": "Chem", "kind": "date", "ends": date(2027, 6, 30), "about": "  site   license "},
        {"name": "Old code", "kind": "date", "ends": "never", "renew": False},
        {"name": "Posit Workbench", "kind": "date", "ends": "2027-01-01"},
        {"name": "Odd", "kind": "magic"},
        {"name": "No file", "kind": "flexlm"},
        {"name": "Run", "kind": "command", "command": "/opt/m/bin/math -run 'Print[1]'", "colour": "red"},
    ]})
    assert [i.name for i in items] == ["Posit Workbench", "Solver", "Site", "Chem", "Old code", "Run"]
    assert items[0].program == L.POSIT_PROGRAM and items[0].sudo
    assert items[1].remind_days == (90, 7)
    assert items[3].ends == "2027-06-30" and items[3].about == "site license"
    assert items[4].ends == "never" and not items[4].renew
    assert items[5].command == ("/opt/m/bin/math", "-run", "Print[1]")
    text = " | ".join(problems)
    assert "there twice" in text and "kind should be one of" in text and "needs file" in text
    assert "colour" in text
    assert items[2].body() == {"name": "Site", "kind": "cert", "host": "web.example.edu", "port": 8443}


def test_posit_only_its_status_lines_are_read(tmp_path):
    prog = tmp_path / "rstudio-server"
    prog.write_text("")
    item = L.Item("Posit Workbench", "posit", program=str(prog))
    seen = []
    out = ('{"result":44,"action":"status","message":"root privilege is required"}\n'
           "RStudio License Manager 2024.09.1\n-- License file status --\nStatus: Activated\n"
           "Licensee: Made-up University\nLicense-File: /var/lib/x/ABCD-EFGH-IJKL-MNOP.lic\n"
           "Expiration: 2026-11-23 08:00:00\nDays-Left: 5\n")
    rd = L.read(item, TODAY, run=_ran(out, seen=seen))
    assert seen[0] == ["sudo", "-n", str(prog), "license-manager", "status"]
    assert rd.ends == "2026-11-23T08:00" and not rd.problem
    assert rd.detail == "Activated; Posit says 5 days left"
    assert "ABCD" not in repr(rd) and "Made-up" not in repr(rd)
    lm = tmp_path / "license-manager"
    lm.write_text("")
    seen = []
    rd = L.read(L.Item("Posit Connect", "posit", program=str(lm), sudo=False), TODAY,
                run=_ran("Status: Expired\nExpiration: 2025-11-23 00:00:00\nDays-Left: 0\n", seen=seen))
    assert seen[0] == [str(lm), "status"] and rd.ends == "2025-11-23"
    rd = L.read(item, TODAY, run=_ran("", "sudo: a password is required\n", 1))
    assert "sudo line" in rd.problem and rd.ends is None
    # no sudo line, and license-manager says it as this account all the same
    def answers(cmd, **kw):
        if cmd[0] == "sudo":
            return subprocess.CompletedProcess(cmd, 1, "", "sudo: a password is required\n")
        return subprocess.CompletedProcess(cmd, 0, "Status: Activated\nExpiration: 2026-11-23 08:00:00\n", "")
    assert L.read(item, TODAY, run=answers).ends == "2026-11-23T08:00"
    rd = L.read(item, TODAY, run=_ran("", "sudo: sorry, you must have a tty to run sudo\n", 1))
    assert "requiretty" in rd.problem
    rd = L.read(L.Item("Gone", "posit", program=str(tmp_path / "nothere")), TODAY)
    assert "isn't on this machine" in rd.problem


def test_flexlm_the_first_feature_still_on(tmp_path):
    folder = tmp_path / "license_files"
    folder.mkdir()
    (folder / "a_100.lic").write_text(
        "SERVER kimhost ANY 1055\nVENDOR ansyslmd\n"
        "INCREMENT solver ansyslmd 2026.0 1-dec-2026 10 \\\n  SIGN=ABCD\n"
        "INCREMENT viewer ansyslmd 2026.0 1-jun-2027 5 SIGN=EF\n"
        "INCREMENT old ansyslmd 2020.0 1-jan-2025 1 SIGN=00\n"
        "FEATURE base ansyslmd 1.0 permanent uncounted HOSTID=ANY\n")
    item = L.Item("Ansys", "flexlm", file=str(folder / "*.lic"))
    rd = L.read(item, TODAY)
    assert rd.ends == "2026-12-01"
    assert rd.detail == "4 features of ansyslmd, 1 permanent, 1 ended"
    # the renewal comes as a second file: solver goes on to 2027
    (folder / "a_200.lic").write_text("INCREMENT solver ansyslmd 2027.0 1-dec-2027 10 SIGN=99\n")
    rd = L.read(item, TODAY)
    assert rd.ends == "2027-06-01" and "from 2 files" in rd.detail
    # everything ended: the latest end
    rd = L.read(item, date(2028, 1, 1))
    assert rd.ends == "2027-12-01"
    only = tmp_path / "client.lic"
    only.write_text("SERVER kimhost ANY 1055\nUSE_SERVER\n")
    rd = L.read(L.Item("Client", "flexlm", file=str(only)), TODAY)
    assert "license server kimhost" in rd.problem
    perm = tmp_path / "perm.lic"
    perm.write_text("FEATURE x v 1.0 permanent 1\n")
    assert L.read(L.Item("P", "flexlm", file=str(perm)), TODAY).never
    assert "no file" in L.read(L.Item("N", "flexlm", file=str(tmp_path / "none" / "*.lic")), TODAY).problem


def test_gurobi(tmp_path):
    f = tmp_path / "gurobi.lic"
    f.write_text("# made up\nTYPE=TOKEN\nVERSION=13\nHOSTNAME=kimhost\nEXPIRATION=2027-08-20\nKEY=ABCD\n")
    rd = L.read(L.Item("Gurobi", "gurobi", file=str(f)), TODAY)
    assert rd.ends == "2027-08-20" and rd.detail == "Gurobi token license, version 13"
    f.write_text("TOKENSERVER=kimhost\n")
    assert "token server kimhost" in L.read(L.Item("G", "gurobi", file=str(f)), TODAY).problem
    f.write_text("WLSACCESSID=abc\nWLSSECRET=def\nLICENSEID=1\n")
    assert "WLS" in L.read(L.Item("G", "gurobi", file=str(f)), TODAY).problem


def test_certificates_end_read_from_the_server(tmp_path):
    assert L.not_after(LATE) == datetime(2059, 9, 26, 19, 57, 22)
    (tmp_path / "c.pem").write_text(CERT)
    (tmp_path / "k.pem").write_text(KEY)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(tmp_path / "c.pem"), str(tmp_path / "k.pem"))
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def serve():
        for _ in range(2):
            try:
                c, _ = srv.accept()
                with ctx.wrap_socket(c, server_side=True) as t:
                    t.recv(1)
            except (OSError, ssl.SSLError):
                pass
    th = threading.Thread(target=serve, daemon=True)
    th.start()
    rd = L.read(L.Item("Site", "cert", host="127.0.0.1", port=port), TODAY)
    srv.close()
    assert rd.ends == "2036-11-15T19:57", rd
    assert "not trusted here" in rd.detail
    shut = socket.socket()
    shut.bind(("127.0.0.1", 0))
    free = shut.getsockname()[1]
    shut.close()
    assert "didn't answer" in L.read(L.Item("Off", "cert", host="127.0.0.1", port=free), TODAY).problem


def test_a_command_only_its_date_is_kept():
    item = L.Item("Mathematica", "command", command=("/opt/m/math", "-run", "x"))
    rd = L.read(item, TODAY, run=_ran('DateObject[{2027, 6, 30, 0, 0, 0.}, "Instant", "Gregorian", -5.]\n'))
    assert rd.ends == "2027-06-30" and rd.detail == "from math"
    rd = L.read(item, TODAY, run=_ran("No valid password found.\nkey ABCD-1234\n", rc=1))
    assert rd.problem == "no date in what it printed (exit 1)"
    rd = L.read(L.Item("V", "command", command=("/opt/v/tool",), pattern=r"until (\S+)"), TODAY,
                run=_ran("build 2024-01-01, licensed until 2027-03-04\n"))
    assert rd.ends == "2027-03-04"
    assert L.read(item, TODAY, run=_ran("Infinity\n")).never


def _items():
    return [L.Item("Workbench", "posit"), L.Item("Chem", "date", ends="2027-06-30"),
            L.Item("Old", "date", ends="never")]


def test_kept_a_row_a_license_and_a_failed_reading_keeps_the_last_end(tmp_path):
    conn = sqlite3.connect(tmp_path / "nomad.db")
    items = _items()
    L.store(conn, items, {"Workbench": L.Reading(ends="2026-11-23T08:00", detail="Activated"),
                          "Chem": L.Reading(ends="2027-06-30"), "Old": L.Reading(never=True)},
            now=datetime(2026, 11, 17, 8, 0), host="kimhost")
    L.store(conn, items, {"Workbench": L.Reading(problem="sudo asks for a password")},
            now=datetime(2026, 11, 18, 8, 0))
    L.store(conn, items, {"Workbench": L.Reading(problem="sudo asks for a password")},
            now=datetime(2026, 11, 18, 14, 0))
    rows = {r["name"]: r for r in L.listing(conn, NOW)}
    w = rows["Workbench"]
    (tmp_path / "x").mkdir()
    assert [r["name"] for r in L.listing(_hub_rows(tmp_path / "x", [
        {"site": "a", "name": "Lapsed long ago", "ends": "2025-01-01"}, {"site": "a", "name": "Soon", "ends": "2026-11-25"},
        {"site": "a", "name": "Lapsed lately", "ends": "2026-11-10"}]), NOW)] == ["Soon", "Lapsed lately", "Lapsed long ago"]
    assert w["ends"] == "2026-11-23T08:00" and w["days"] == 5 and w["state"] == "soon"
    assert w["problem"] == "sudo asks for a password" and w["problem_since"] == "2026-11-18T08:00:00"
    assert w["ok_at"] == "2026-11-17T08:00:00" and w["detail"] == "Activated"
    assert rows["Old"]["state"] == "no end" and rows["Chem"]["state"] == "ok"
    assert [r["name"] for r in L.listing(conn, NOW)] == ["Workbench", "Chem", "Old"]
    L.store(conn, items[:1], {"Workbench": L.Reading(ends="2027-11-23T08:00")}, now=datetime(2026, 11, 19))
    rows = L.listing(conn, NOW)
    assert [r["name"] for r in rows] == ["Workbench"]
    assert rows[0]["problem"] is None and rows[0]["problem_since"] is None and rows[0]["state"] == "ok"
    assert L.last_read(conn, ["Workbench"]) == datetime(2026, 11, 19)
    assert L.last_read(conn, ["Workbench", "Chem"]) is None


def _hub_rows(tmp_path, rows):
    """A hub's license_status, as sync leaves it (source_site)."""
    conn = sqlite3.connect(tmp_path / "combined.db")
    conn.execute("CREATE TABLE license_status (id INTEGER PRIMARY KEY, name TEXT, kind TEXT, about TEXT, renew INTEGER, "
                 "remind_days TEXT, ends TEXT, never INTEGER, detail TEXT, source TEXT, problem TEXT, "
                 "problem_since TEXT, read_at TEXT, ok_at TEXT, host TEXT, source_site TEXT, _sync_site TEXT, "
                 "_src_rowid INTEGER)")
    for r in rows:
        conn.execute("INSERT INTO license_status (name, kind, about, renew, remind_days, ends, never, detail, problem, "
                     "problem_since, read_at, ok_at, source_site) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (r["name"], r.get("kind", "date"), r.get("about"), r.get("renew", 1), r.get("remind_days"),
                      r.get("ends"), r.get("never", 0), r.get("detail"), r.get("problem"), r.get("problem_since"),
                      r.get("read_at", "2026-11-18T06:00:00"), r.get("ok_at", "2026-11-18T06:00:00"), r["site"]))
    conn.commit()
    return conn


def test_reminders_once_each_and_a_renewal_starts_them_over(tmp_path):
    conn = _hub_rows(tmp_path, [
        {"site": "kimweb", "name": "Workbench", "ends": "2026-11-23T08:00", "about": "RStudio in the browser"},
        {"site": "kimweb", "name": "Connect", "ends": "2025-11-23", "renew": 0},
        {"site": "patlab", "name": "Solver", "ends": "2027-08-20"},
        {"site": "patlab", "name": "Chem", "ends": "2026-11-08"},
        {"site": "patlab", "name": "Older", "ends": "2026-01-01"},
        {"site": "patlab", "name": "Math", "problem": "no date in what it printed (exit 1)",
         "problem_since": "2026-11-15T06:00:00", "ok_at": None},
        {"site": "patlab", "name": "Fresh", "problem": "sudo asks for a password", "problem_since": "2026-11-18T06:00:00",
         "ends": "2027-03-01"},
        {"site": "olive", "name": "Cert", "ends": "2027-02-13T23:59", "read_at": "2026-11-10T06:00:00"},
    ])
    s = L.Settings(to=("rc@example.edu",), link="https://nomad.example.edu/licenses", team="RC")
    rec = L.open_record(tmp_path / "licenses.db", write=True)
    sent = []
    out = L.remind(conn, s, rec, NOW, dry=False, sender=lambda m, cfg: sent.append(m), config={"mail": {"from": "n@x.edu"}})
    got = {(n.row["name"], n.why) for n in out["notes"]}
    assert got == {("Workbench", "ends"), ("Chem", "ended"), ("Math", "unread"), ("Cert", "stale")}
    w = next(n for n in out["notes"] if n.row["name"] == "Workbench")
    assert w.steps == ("60", "30", "14", "7") and w.days == 5
    assert out["status"] == "sent" and len(sent) == 1
    msg = sent[0]
    assert msg["To"] == "rc@example.edu" and msg["Subject"] == "NOMAD: Workbench (kimweb) ends in 5 days; 1 license ended"
    body = msg.get_content()
    assert "Workbench (kimweb) ends Mon 23 Nov 2026, in 5 days." in body
    assert "Chem (patlab) ended Sun 8 Nov 2026, 10 days ago." in body
    assert "Math (patlab): nomad can't read it since 15 Nov 2026" in body
    assert "Cert (olive): not read since 10 Nov 2026" in body
    assert "https://nomad.example.edu/licenses" in body and "-- nomad, for RC" in body
    assert "Connect" not in body and "Older" not in body and "Fresh" not in body
    # the next morning: nothing again; four days later, the 1-day reminder
    assert L.remind(conn, s, rec, NOW + timedelta(days=1), dry=False, sender=lambda m, c: sent.append(m),
                    config={"mail": {"from": "n@x.edu"}})["notes"] == []
    out = L.remind(conn, s, rec, NOW + timedelta(days=4), dry=True)
    names = [(n.row["name"], n.steps) for n in out["notes"]]
    assert ("Workbench", ("60", "30", "14", "7", "1")) in names
    assert out["subject"].startswith("NOMAD: ")
    # renewed: the end moves, the reminders start over at its 60 days
    conn.execute("UPDATE license_status SET ends = '2027-11-23T08:00' WHERE name = 'Workbench'")
    assert [n for n in L.remind(conn, s, rec, NOW + timedelta(days=1))["notes"] if n.row["name"] == "Workbench"] == []
    out = L.remind(conn, s, rec, datetime(2027, 9, 25, 8, 0))
    assert [(n.row["name"], n.steps) for n in out["notes"]
            if n.row["name"] == "Workbench" and n.why == "ends"] == [("Workbench", ("60",))]


def test_an_end_and_a_failed_reading_in_one_note(tmp_path):
    conn = _hub_rows(tmp_path, [{"site": "kimweb", "name": "Workbench", "ends": "2026-11-23T08:00",
                                 "problem": "sudo asks for a password", "problem_since": "2026-11-15T06:00:00"}])
    s = L.Settings(to=("rc@example.edu",))
    rec = L.open_record(tmp_path / "licenses.db", write=True)
    sent = []
    out = L.remind(conn, s, rec, NOW, dry=False, sender=lambda m, c: sent.append(m), config={"mail": {"from": "n@x.edu"}})
    assert [n.why for n in out["notes"]] == ["ends"]
    assert "can't read it since 15 Nov 2026: sudo asks for a password; this is the end it read before" in out["text"]
    assert L.remind(conn, s, rec, NOW + timedelta(days=1))["notes"] == []


def test_one_license_one_subject_and_its_own_days(tmp_path):
    conn = _hub_rows(tmp_path, [{"site": "kimweb", "name": "Workbench", "ends": "2026-12-28", "remind_days": "[45]"}])
    s = L.Settings(to=("rc@example.edu",))
    out = L.remind(conn, s, None, NOW)
    assert out["subject"] == "NOMAD: Workbench (kimweb) ends in 40 days" and out["notes"][0].steps == ("45",)
    assert L.remind(conn, L.Settings(to=("rc@example.edu",)), None, NOW - timedelta(days=10))["notes"] == []


def test_settings_come_from_the_center_s_own():
    s = L.settings({"support": {"email": "hpc@example.edu", "team": "ARC"}, "mail": {"from": "nomad@example.edu"},
                    "alerts": {"link": "https://nomad.example.edu/"}})
    assert s.to == ("hpc@example.edu",) and s.send_from == "nomad@example.edu" and s.team == "ARC"
    assert s.link == "https://nomad.example.edu/licenses" and s.remind_days == L.REMIND_DAYS and not s.problems
    s = L.settings({"licenses": {"remind_days": [7, 30, "x"], "to": "kim", "colour": 1}})
    assert s.remind_days == L.REMIND_DAYS and s.to == ()
    assert any("colour" in p for p in s.problems) and any("remind_days" in p for p in s.problems)
    assert L.settings({"licenses": {"remind_days": [7, 90, 30]}}).remind_days == (90, 30, 7)
    old = {"collectors": {"licenses": {"interval": 300, "servers": [{"name": "x"}]}}}
    assert any("old example" in p for p in L.config_problems(old))


def test_the_collector(tmp_path):
    from nomad.collectors import plan as plan_mod
    from nomad.collectors.licenses import LicensesCollector
    spec = next(s for s in plan_mod.SPECS if s.name == "licenses")
    assert not spec.default_on and spec.tables == ("license_status",)
    planned = {p.name: p for p in plan_mod.plan({"collectors": {"licenses": {
        "enabled": True, "items": [{"name": "Chem", "kind": "date", "ends": "2099-01-01"}]}}})}
    assert planned["licenses"].enabled
    db = tmp_path / "nomad.db"
    c = LicensesCollector(planned["licenses"].config, db)
    res = c.run()
    assert res.success and res.records_collected == 1
    assert LicensesCollector(planned["licenses"].config, db).collect() == []
    rows = L.listing(sqlite3.connect(db))
    assert rows[0]["name"] == "Chem" and rows[0]["ends"] == "2099-01-01"
    # no items any more: nothing kept
    LicensesCollector({"enabled": True}, db).collect()
    assert L.listing(sqlite3.connect(db)) == []


def test_add_shows_then_writes_once_and_renew_no(tmp_path):
    from nomad.cli import cli
    cfg = tmp_path / "nomad.toml"
    cfg.write_text("[general]\ndata_dir = \"%s\"\n" % tmp_path)
    run = CliRunner()
    args = ["-c", str(cfg), "licenses", "add", "Chem suite", "date", "--ends", "2099-06-30", "--about", "site license"]
    out = run.invoke(cli, args)
    assert out.exit_code == 0, out.output
    assert "[[collectors.licenses.items]]" in out.output and "Chem suite" not in cfg.read_text()
    out = run.invoke(cli, args + ["--apply"])
    assert out.exit_code == 0, out.output
    assert "Read now: ends" in out.output
    text = cfg.read_text()
    assert "[collectors.licenses]\nenabled = true" in text and 'name = "Chem suite"' in text
    assert "already" in run.invoke(cli, args + ["--apply"]).output
    out = run.invoke(cli, ["-c", str(cfg), "licenses", "add", "Chem suite", "date", "--ends", "2099-07-01", "--apply"])
    assert out.exit_code != 0 and "--replace" in out.output
    out = run.invoke(cli, ["-c", str(cfg), "licenses", "renew", "chem suite", "no", "--apply"])
    assert out.exit_code == 0, out.output
    from nomad.config.edit import loads
    items, problems = L.items_of(loads(cfg.read_text())["collectors"]["licenses"])
    assert len(items) == 1 and not items[0].renew and items[0].about == "site license" and not problems
    out = run.invoke(cli, ["-c", str(cfg), "licenses", "read"])
    assert out.exit_code == 0, out.output
    assert "Chem suite" in out.output and "not renewing" in out.output
    out = run.invoke(cli, ["-c", str(cfg), "licenses"])
    assert "1 license: 0 ended" in out.output
    bad = run.invoke(cli, ["-c", str(cfg), "licenses", "add", "X", "cert"])
    assert bad.exit_code != 0 and "needs host" in bad.output
    gone = run.invoke(cli, ["-c", str(cfg), "licenses", "add", "Y", "flexlm", "--file", str(tmp_path / "no" / "*.lic")])
    assert gone.exit_code != 0 and "nothing written" in gone.output
    gone = run.invoke(cli, ["-c", str(cfg), "licenses", "add", "Z", "command", "--command", str(tmp_path / "math")])
    assert gone.exit_code != 0 and "isn't a program" in gone.output


def test_remind_on_the_hub(tmp_path):
    from nomad.cli import cli
    conn = _hub_rows(tmp_path, [{"site": "kimweb", "name": "Workbench", "ends":
                                 (date.today() + timedelta(days=5)).isoformat()}])
    conn.close()
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f'[hub]\noutput_db = "{tmp_path / "combined.db"}"\n[[hub.sites]]\nname = "kimweb"\nhost = "kimweb"\n'
                   '[support]\nemail = "rc@example.edu"\n')
    out = CliRunner().invoke(cli, ["-c", str(cfg), "licenses", "remind", "--text"])
    assert out.exit_code == 0, out.output
    assert "would send" in out.output and "kimweb: Workbench: ends" in out.output and "in 5 days" in out.output
    assert not (tmp_path / "licenses.db").exists()
    out = CliRunner().invoke(cli, ["-c", str(cfg), "licenses"])
    assert "kimweb  Workbench" in out.output


def test_find_names_where_not_what(tmp_path):
    lf = tmp_path / "sw" / "vendorx" / "shared" / "license_files"
    lf.mkdir(parents=True)
    (lf / "v_1.lic").write_text("SERVER h ANY 1055\nINCREMENT a vendorx 1.0 1-jun-2027 1 SIGN=ABCD\n")
    g = tmp_path / "sw" / "gurobi1201" / "linux64"
    g.mkdir(parents=True)
    (g / "gurobi.lic").write_text("TYPE=TOKEN\nEXPIRATION=2027-08-20\n")
    nm = tmp_path / "sw" / "app" / "node_modules" / "x"
    nm.mkdir(parents=True)
    (nm / "license.lic").write_text("INCREMENT b v 1 1-jun-2027 1\n")
    found = L.find(roots=(str(tmp_path / "sw"),), ports=())
    kinds = {(f["kind"], f.get("file")) for f in found if f["kind"] != "posit"}
    assert kinds == {("flexlm", str(lf / "*.lic")), ("gurobi", str(g / "gurobi.lic"))}
    names = {f["name"] for f in found}
    assert "FlexLM (shared)" in names and "Gurobi (gurobi1201)" in names
    assert L.add_line({"name": "FlexLM (shared)", "kind": "flexlm", "file": "/opt/a b/*.lic"}) == \
        "nomad licenses add 'FlexLM (shared)' flexlm --file '/opt/a b/*.lic'"


@pytest.mark.parametrize("ends,today,words", [
    ("2026-11-23T08:00", TODAY, "ends Mon 23 Nov 2026, in 5 days"),
    ("2026-11-18", TODAY, "ends today (Wed 18 Nov 2026)"),
    ("2026-11-17", TODAY, "ended Tue 17 Nov 2026, 1 day ago"),
    (None, TODAY, "end not known"),
])
def test_ends_in_words(ends, today, words):
    assert L.ends_words({"ends": ends}, today) == words

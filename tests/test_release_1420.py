"""1.42.0: a lab's PI asks ARC for accounts (nomad.directory, nomad.account_requests,
`nomad lab accounts`)."""
import base64
import io
import json
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from nomad import account_requests as A
from nomad import directory as D
from nomad import lab_check as L

PEOPLE = {
    "jdoe": {"uid": ["jdoe"], "displayName": ["Jane Doe"], "cn": ["Jane Doe"]},
    "asmith": {"uid": ["asmith"], "cn": ["Alex Smith"], "givenName": ["Alex"], "sn": ["Smith"]},
    "asmith2": {"uid": ["asmith2"], "cn": ["Alexandra Smith"], "givenName": ["Alexandra"], "sn": ["Smith"]},
    "bkim": {"uid": ["bkim"], "givenName": ["Bo"], "sn": ["Kim"]},
    "mlee": {"uid": ["mlee"], "displayName": ["María Lee"]},
}


def ldif(entries):
    out = []
    for e in entries:
        out.append(f"dn: uid={e['uid'][0]},ou=people,dc=example,dc=edu")
        for k, vs in e.items():
            for v in vs:
                if v.isascii():
                    out.append(f"{k}: {v}")
                else:
                    out.append(f"{k}:: {base64.b64encode(v.encode()).decode()}")
        out.append("")
    return "\n".join(out) + "\n"


class FakeLdap:
    """ldapsearch, answering from PEOPLE as a directory would; keeps each command and the password it got."""

    def __init__(self, rc=0, stderr=""):
        self.calls, self.passwords, self.rc, self.stderr = [], [], rc, stderr

    def __call__(self, cmd, capture_output, text, timeout):
        self.calls.append(cmd)
        if "-y" in cmd:
            self.passwords.append(Path(cmd[cmd.index("-y") + 1]).read_text())
        filt = cmd[-len(D.Directory.ATTRS) - 1]
        limit = int(cmd[cmd.index("-z") + 1])
        hits = [e for e in PEOPLE.values() if match(e, filt)]
        rc = self.rc
        if len(hits) > limit:
            hits, rc = hits[:limit], 4
        return subprocess.CompletedProcess(cmd, rc, ldif(hits) if rc in (0, 4) else "", self.stderr)


def match(e, filt):
    """Enough of an LDAP filter for these tests: uid= ORs, and the name filter."""
    import re
    uids = re.findall(r"\(uid=([^)*]+)\)", filt)
    if uids:
        return e["uid"][0] in uids
    sn = re.search(r"\(sn=([^)]+)\)\(givenName=([^)*]+)\*\)", filt)
    last, first = sn.group(1), sn.group(2)
    full = (e.get("displayName") or e.get("cn") or [""])[0]
    by_parts = e.get("sn", [""])[0] == last and e.get("givenName", [""])[0].startswith(first)
    by_full = last in full and (full.startswith(first) or f" {first}" in full)
    return by_parts or by_full


def directory(run=None, **kw):
    s = D.settings({"directory": {"uri": "ldaps://ldap.example.edu", "base": "ou=people,dc=example,dc=edu", **kw}})
    return D.Directory(s, run=run or FakeLdap())


@pytest.fixture
def pwfile(tmp_path):
    p = tmp_path / "directory_password"
    p.write_text("s3cret-pass\n")
    p.chmod(0o600)
    return p


# --- nomad.directory --------------------------------------------------------------------

def test_settings_refuse_a_clear_password_and_need_a_base():
    s = D.settings({"directory": {"uri": "ldap://ldap.example.edu", "base": "dc=x", "bind_dn": "uid=svc,dc=x",
                                  "password_file": "/p"}})
    assert not s.on and any("clear" in p for p in s.problems)
    s = D.settings({"directory": {"uri": "ldaps://ldap.example.edu"}})
    assert not s.on and any("base" in p for p in s.problems)
    s = D.settings({"directory": {"uri": "ldaps://ldap.example.edu", "base": "dc=x", "bind_dn": "uid=svc,dc=x"}})
    assert any("password_file" in p for p in s.problems)
    assert not D.settings({}).on and D.settings({}).problems == ()
    with pytest.raises(D.DirectoryError, match="not set"):
        D.Directory(D.settings({}))


def test_escape_and_ldif():
    assert D.escape("a*(b)\\") == "a\\2a\\28b\\29\\5c"
    text = "dn: uid=x,dc=y\nuid: x\ndisplayName:: " + base64.b64encode("José Núñez".encode()).decode() + \
        "\ncn: a long\n  name\n\ndn: uid=z,dc=y\nuid: z\n"
    got = D.parse_ldif(text)
    assert got[0]["displayname"] == ["José Núñez"] and got[0]["cn"] == ["a long name"] and got[1]["uid"] == ["z"]
    assert D.name_of({"givenname": ["Bo"], "sn": ["Kim"]}) == "Bo Kim"


def test_the_password_goes_in_a_file_never_on_the_command_line(pwfile):
    fake = FakeLdap()
    d = directory(fake, bind_dn="uid=svc,ou=special,dc=example,dc=edu", password_file=str(pwfile))
    assert d.by_netids(["jdoe", "nobody"]) == {"jdoe": "Jane Doe", "nobody": None}
    cmd = fake.calls[0]
    assert "s3cret-pass" not in " ".join(cmd) and fake.passwords == ["s3cret-pass"]
    assert not Path(cmd[cmd.index("-y") + 1]).exists()          # the file is gone after
    assert cmd[cmd.index("-H") + 1] == "ldaps://ldap.example.edu" and "-D" in cmd


def test_a_refused_search_says_so_without_a_name(pwfile):
    fake = FakeLdap(rc=49, stderr="ldap_bind: Invalid credentials (49) for uid=svc,ou=special")
    d = directory(fake, bind_dn="uid=svc,dc=x", password_file=str(pwfile))
    with pytest.raises(D.DirectoryError) as e:
        d.check()
    assert "Invalid credentials" in str(e.value) and "svc" not in str(e.value)
    d = directory(FakeLdap(), bind_dn="uid=svc,dc=x", password_file=str(pwfile.parent / "missing"))
    with pytest.raises(D.DirectoryError, match="can't be read"):
        d.check()


def test_netids_are_asked_in_chunks():
    fake = FakeLdap()
    d = directory(fake)
    got = d.by_netids([f"user{i}" for i in range(95)] + ["jdoe", "Not A NetID!"])
    assert len(fake.calls) == 3 and got["jdoe"] == "Jane Doe" and "Not A NetID!" not in got


def test_by_name_in_parts_or_full_names():
    d = directory()
    assert d.by_name("Jane", "Doe") == ([{"netid": "jdoe", "name": "Jane Doe"}], False)
    found, more = d.by_name("Alex", "Smith")
    assert [f["netid"] for f in found] == ["asmith", "asmith2"] and not more
    assert d.by_name("Bo", "Kim")[0] == [{"netid": "bkim", "name": "Bo Kim"}]
    assert d.by_name("", "Kim") == ([], False)


def test_too_many_of_a_name(monkeypatch):
    monkeypatch.setattr(D, "MOST_MATCHES", 1)
    assert directory().by_name("Alex", "Smith")[1] is True


# --- what the PI typed ------------------------------------------------------------------

def test_parse_text():
    got = A.parse_text("jdoe\nJane Doe\nDoe, Jane; bkim, mlee, asmith\nJane Doe <jdoe@example.edu>\n"
                       "x@gmail.com\nDoe\tJane\tjdoe\nO'Neil\n\n", "example.edu")
    assert [g["text"] for g in got] == ["jdoe", "Jane Doe", "Doe, Jane", "bkim", "mlee", "asmith",
                                        "jdoe@example.edu", "x@gmail.com", "Doe, Jane, jdoe", "O'Neil"]
    assert got[0]["netids"] == ["jdoe"] and got[0]["alone"]
    assert got[1]["names"][0] == ("Jane", "Doe") and got[2]["names"][0] == ("Jane", "Doe")
    assert got[6]["netids"] == ["jdoe"] and "outside example.edu" in got[7]["why"]
    assert got[8]["netids"] == ["jdoe"] and got[9] == {"text": "O'Neil", "netids": [], "names": [], "alone": True}


def test_table_text_csv_tsv_and_headers():
    csv = "First Name,Last Name,NetID\nJane,Doe,jdoe\nBo,Kim,\n,,\n"
    assert A.table_text("s.csv", csv.encode()) == "jdoe\nKim, Bo"
    tsv = "Name\tEmail\nJane Doe\tjdoe@example.edu\nAlex Smith\t\n"
    assert A.table_text("s.tsv", tsv.encode("utf-8-sig")) == "jdoe@example.edu\nAlex Smith"
    assert A.table_text("s.txt", b"jdoe\nasmith\n") == "jdoe\nasmith"
    with pytest.raises(A.RequestError, match=".xls"):
        A.table_text("old.xls", b"\xd0\xcf")
    with pytest.raises(A.RequestError, match="can't be read"):
        A.table_text("bad.xlsx", b"not a zip")


def test_table_text_xlsx():
    openpyxl = pytest.importorskip("openpyxl")      # only to write the file; nomad reads it without
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Students"
    ws.append(["Last name", "First name", "NetID"])
    ws.append(["Doe", "Jane", "jdoe"])
    ws.append(["Kim", "Bo", None])
    buf = io.BytesIO()
    wb.save(buf)
    assert A.table_text("s.xlsx", buf.getvalue()) == "jdoe\nKim, Bo"


# --- who each one is --------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "combined.db")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE jobs (job_id TEXT, user_name TEXT, cluster TEXT, source_site TEXT);
        CREATE TABLE group_membership (username TEXT, group_name TEXT, cluster TEXT, source_site TEXT,
            has_account INTEGER);
        CREATE TABLE per_user_daily (day TEXT, hostname TEXT, username TEXT, source_site TEXT);
        CREATE TABLE node_state (timestamp TEXT, node_name TEXT, source_site TEXT);
        INSERT INTO jobs VALUES ('1', 'jdoe', 'spydur', 'spydur');
        INSERT INTO group_membership VALUES ('bkim', 'kimlab', 'arachne', 'arachne', 1),
            ('mlee', 'leelab', 'spydur', 'spydur', 0);
        INSERT INTO per_user_daily VALUES ('2026-11-01', 'jonimitchell', 'asmith', 'jonimitchell');
    """)
    c.execute("INSERT INTO node_state VALUES (datetime('now', 'localtime'), 'n1', 'spydur')")
    c.execute("INSERT INTO node_state VALUES (datetime('now', 'localtime', '-30 days'), 'n1', 'old')")
    return c


@pytest.fixture
def rec(tmp_path, monkeypatch):
    monkeypatch.setattr(L.os, "geteuid", lambda: 1000)
    return A.open_records(tmp_path / "lab_check.db", write=True)


def test_has_account_and_sites(conn):
    got = A.has_account(conn, ["jdoe", "bkim", "mlee", "asmith", "zed"])
    assert got == {"jdoe": ["spydur"], "bkim": ["arachne"], "mlee": [], "asmith": ["jonimitchell"], "zed": []}
    assert A.has_account(conn, ["jdoe", "bkim"], sites=["arachne"]) == {"jdoe": [], "bkim": ["arachne"]}
    assert A.sites_with_nodes(conn) == ["spydur"]


def test_resolve(conn, rec):
    rows = A.resolve(A.parse_text("jdoe\nAlex Smith\nNo Body\nSmith\nkim\nzzz qqq\njdoe asmith\n???"), directory(),
                     conn, rec)
    by = {r["text"]: r for r in rows}
    assert by["jdoe"]["status"] == "found" and by["jdoe"]["has"] == ["spydur"]
    assert by["Alex Smith"]["status"] == "choose" and [c["netid"] for c in by["Alex Smith"]["choices"]] == [
        "asmith", "asmith2"]
    assert by["Alex Smith"]["choices"][0]["has"] == ["jonimitchell"]
    assert by["No Body"]["status"] == "not_found"
    assert by["Smith"]["status"] == "not_found" and "surname" in by["Smith"]["why"]
    assert by["zzz qqq"]["status"] == "not_found"
    assert by["asmith"]["status"] == "found"                    # "jdoe asmith": two NetIDs, a row each
    assert by["???"]["status"] == "not_understood"


def test_resolve_partial_netids_keep_the_missing_one(conn, rec):
    rows = A.resolve(A.parse_text("jdoe nobody"), directory(), conn, rec)
    assert [(r["text"], r["status"]) for r in rows] == [("jdoe", "found"), ("nobody", "not_found")]


# --- the requests -----------------------------------------------------------------------

S = A.Settings(enabled=True, sites=("spydur", "arachne"), notify=("hpc@example.edu",), most=3)


def test_ask_close_and_list(rec):
    with pytest.raises(A.RequestError, match="pick the clusters"):
        A.ask(rec, "doelab", [{"netid": "jdoe"}], [], S, by="pi1")
    with pytest.raises(A.RequestError, match="not a cluster"):
        A.ask(rec, "doelab", [{"netid": "jdoe"}], ["vadur"], S, by="pi1")
    with pytest.raises(A.RequestError, match="at most"):
        A.ask(rec, "doelab", [{"netid": n} for n in ("a1", "a2", "a3", "a4")], ["spydur"], S, by="pi1")
    req = A.ask(rec, "doelab", [{"netid": "JDoe", "name": "Jane Doe"}, {"netid": "bkim", "name": "Bo Kim"}],
                ["spydur", "arachne"], S, by="PI1", note="  new  students\x07 ")
    assert len(req["rows"]) == 4 and req["by"] == "pi1" and req["note"] == "new students"
    again = A.ask(rec, "doelab", [{"netid": "jdoe"}, {"netid": "mlee"}], ["spydur"], S, by="pi1")
    assert again["skipped"] == [{"netid": "jdoe", "site": "spydur"}] and [r["netid"] for r in again["rows"]] == ["mlee"]
    with pytest.raises(A.RequestError, match="already asked"):
        A.ask(rec, "doelab", [{"netid": "jdoe"}], ["spydur"], S, by="pi1")
    assert A.asked(rec, ["jdoe", "zed"]) == {"jdoe": ["arachne", "spydur"]}

    with pytest.raises(A.RequestError, match="say why"):
        A.close(rec, req["id"], "jdoe", "spydur", "not_made", by="arc1")
    done = A.close(rec, req["id"], "jdoe", "spydur", "made", by="arc1")
    assert done["state"] == "made" and done["closed_by"] == "arc1"
    with pytest.raises(A.RequestError, match="already closed"):
        A.close(rec, req["id"], "jdoe", "spydur", "made", by="arc1")
    with pytest.raises(A.RequestError, match="no such"):
        A.close(rec, req["id"], "zed", "spydur", "made", by="arc1")
    A.close(rec, req["id"], "bkim", "arachne", "withdrawn", by="pi1")
    got = A.requests(rec, lab="doelab")
    assert [r["id"] for r in got] == [again["id"], req["id"]] and got[1]["open"] == 2
    assert [r["id"] for r in A.requests(rec, open_only=True)] == [again["id"], req["id"]]
    log = [json.loads(r[0]) for r in rec.execute("SELECT what FROM lab_check_log ORDER BY rowid")]
    assert "account_request" in log[0] and log[-1]["account_closed"]["state"] == "withdrawn"


def test_reading_before_anything_is_asked(tmp_path):
    rec = sqlite3.connect(tmp_path / "empty.db")
    rec.row_factory = sqlite3.Row
    assert A.requests(rec) == [] and A.asked(rec, ["jdoe"]) == {} and A.requests(None) == []


def test_settings():
    on = {"directory": {"uri": "ldaps://ldap.example.edu", "base": "dc=x"}}
    s = A.settings(on)
    assert s.enabled and s.notify == () and s.most == 100
    s = A.settings({**on, "console": {"labs": {"accounts": {"notify": "hpc@example.edu", "sites": ["spydur"],
                                                            "link": "https://n.example.edu/x", "most": 900}}}})
    assert s.notify == ("hpc@example.edu",) and s.sites == ("spydur",) and s.most == 500 and s.link
    s = A.settings({"console": {"labs": {"accounts": {"enabled": True}}}})
    assert not s.enabled and any("[directory]" in p for p in s.problems)
    assert not A.settings({**on, "console": {"labs": {"accounts": {"enabled": False}}}}).enabled


# --- email ------------------------------------------------------------------------------

class Sent(list):
    def __call__(self, msg, cfg, timeout=15):
        self.append(msg)


@pytest.fixture
def sent(monkeypatch):
    from nomad import mail
    box = Sent()
    monkeypatch.setattr(mail, "send", box)
    return box


CONFIG = {"console": {"labs": {"check": {"mail_domain": "example.edu", "send_from": "RC <rc@example.edu>",
                                         "names": {"pi1": "Carol Parish"}, "signature": ["Research Computing"]}}}}


def test_emails_to_arc_and_to_the_pi(rec, sent):
    cs = L.settings(CONFIG)
    req = A.ask(rec, "doelab", [{"netid": "jdoe", "name": "Jane Doe"}], ["spydur"], S, by="pi1", note="REU")
    assert A.tell_arc(CONFIG, cs, S, req) == "sent"
    m = sent[-1]
    assert m["To"] == "hpc@example.edu" and m["Reply-To"] == "pi1@example.edu"
    assert m["Subject"].startswith(f"Account request #{req['id']}: doelab, 1 person on spydur")
    assert "jdoe" in m.get_content() and "Their note: REU" in m.get_content()

    done = A.close(rec, req["id"], "jdoe", "spydur", "made", by="arc1", note="home on /scratch too")
    assert A.tell_pi(CONFIG, cs, S, req, [done]) == "sent"
    m = sent[-1]
    assert m["To"] == "pi1@example.edu" and m["Reply-To"] == "hpc@example.edu"
    assert m["Subject"] == "The account for jdoe on spydur is ready"
    body = m.get_content()
    assert body.startswith("Dear Dr. Parish,") and "Jane Doe (jdoe) on spydur" in body and "home on /scratch" in body


def test_review_to_holds_the_pis_email(rec, sent):
    cfg = json.loads(json.dumps(CONFIG))
    cfg["console"]["labs"]["check"]["review_to"] = ["rc@example.edu"]
    cs = L.settings(cfg)
    req = A.ask(rec, "doelab", [{"netid": "jdoe"}, {"netid": "bkim"}], ["spydur"], S, by="pi1")
    a = A.close(rec, req["id"], "jdoe", "spydur", "made", by="arc1")
    b = A.close(rec, req["id"], "bkim", "spydur", "not_made", by="arc1", note="not a student this term")
    assert A.tell_pi(cfg, cs, S, req, [a, b]) == "sent"
    m = sent[-1]
    assert m["To"] == "rc@example.edu" and m["Subject"].startswith("[review] Your account request")
    assert "would have gone to pi1@example.edu" in m.get_content() and "not a student this term" in m.get_content()


def test_no_sender_no_email(rec, sent):
    cs = L.settings({"console": {"labs": {"check": {"mail_domain": "example.edu"}}}})
    req = A.ask(rec, "doelab", [{"netid": "jdoe"}], ["spydur"], S, by="pi1")
    assert A.tell_arc({}, cs, S, req).startswith("not sent: no sender") and not sent
    assert A.tell_arc({}, cs, A.Settings(), req).startswith("not sent: no one to tell")


# --- `nomad lab accounts` ---------------------------------------------------------------

def test_cli_list_and_close(tmp_path, rec, sent, monkeypatch):
    from nomad import lab_accounts_cli as C
    from nomad.cli import cli
    req = A.ask(rec, "doelab", [{"netid": "jdoe", "name": "Jane Doe"}], ["spydur"], S, by="pi1")
    monkeypatch.setattr(C, "_ctx", lambda ctx: (CONFIG, None, L.settings(CONFIG), None, tmp_path / "lab_check.db"))
    r = CliRunner().invoke(cli, ["lab", "accounts", "list", "--open"])
    assert r.exit_code == 0, r.output
    assert f"#{req['id']}" in r.output and "jdoe" in r.output and "asked for" in r.output
    r = CliRunner().invoke(cli, ["lab", "accounts", "close", str(req["id"]), "jdoe", "spydur", "made", "--by", "arc1"])
    assert r.exit_code == 0, r.output
    assert "made" in r.output and "Email to pi1: sent" in r.output
    r = CliRunner().invoke(cli, ["lab", "accounts", "list", "--open"])
    assert "No requests with accounts still to make." in r.output


def test_cli_lookup(tmp_path, rec, conn, monkeypatch):
    from nomad import lab_accounts_cli as C
    from nomad.cli import cli
    monkeypatch.setattr(C, "_ctx", lambda ctx: (CONFIG, None, L.settings(CONFIG), conn, tmp_path / "lab_check.db"))
    monkeypatch.setattr(C, "_directory", lambda config: directory())
    r = CliRunner().invoke(cli, ["lab", "accounts", "lookup", "jdoe", "Alex Smith", "nobody"])
    assert r.exit_code == 0, r.output
    assert "Jane Doe; has an account on spydur" in r.output and "several: asmith Alex Smith" in r.output
    assert "not found" in r.output
    csv = tmp_path / "s.csv"
    csv.write_text("NetID\nbkim\n")
    r = CliRunner().invoke(cli, ["lab", "accounts", "lookup", "--file", str(csv), "--json"])
    assert json.loads(r.output)[0]["name"] == "Bo Kim"


def test_zip_reader_takes_the_first_sheet_by_the_workbook():
    """An .xlsx whose first sheet isn't sheet1.xml (renamed or reordered in Excel)."""
    buf = io.BytesIO()
    m = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{m}" xmlns:r="{r}"><sheets>'
                   '<sheet name="B" sheetId="2" r:id="rId2"/><sheet name="A" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                   '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{m}"><sheetData><row r="1">'
                   '<c r="A1" t="inlineStr"><is><t>wrong</t></is></c></row></sheetData></worksheet>')
        z.writestr("xl/worksheets/sheet2.xml", f'<worksheet xmlns="{m}"><sheetData>'
                   '<row r="1"><c r="B1" t="inlineStr"><is><t>NetID</t></is></c></row>'
                   '<row r="2"><c r="B2" t="inlineStr"><is><t>mlee</t></is></c></row></sheetData></worksheet>')
    assert A.table_text("s.xlsx", buf.getvalue()) == "mlee"

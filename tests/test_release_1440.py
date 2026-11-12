"""1.44.0: request numbers, as a ticket system gives them, and the account-requests report.

Every account request has a number, ARC-0012 ([console.tickets] prefix and
digits): on the PI's card, first in both emails' subjects, on the Admin page,
in the log. Requests made before keep theirs (request #12 is ARC-0012), and
the numbers are one sequence other kinds of request will share. A request is
found by its number, a NetID, a name, a lab or its PI; `nomad lab accounts
report` says what was asked for in a period and what became of it; the usage
report counts it (section 15), naming no one.
"""
import csv
import io
import json
import sqlite3
from datetime import datetime

import pytest
from click.testing import CliRunner

from nomad import account_requests as A
from nomad import lab_check as L
from nomad import tickets as T

S = A.Settings(enabled=True, sites=("spydur", "arachne"), notify=("hpc@example.edu",), most=20)
CONFIG = {"console": {"labs": {"check": {"mail_domain": "example.edu", "send_from": "RC <rc@example.edu>",
                                         "names": {"pi1": "Carol Parish"}, "signature": ["Research Computing"]}}}}


@pytest.fixture
def rec(tmp_path, monkeypatch):
    monkeypatch.setattr(L.os, "geteuid", lambda: 1000)
    return A.open_records(tmp_path / "lab_check.db", write=True)


def people(*netids):
    return [{"netid": n, "name": n.title()} for n in netids]


# --- the numbers ------------------------------------------------------------------------

def test_settings_ref_and_reading_a_number():
    s = T.settings({})
    assert (s.prefix, s.digits, s.problems) == ("ARC", 4, ())
    assert T.ref(12, s) == "ARC-0012" and T.ref(123456, s) == "ARC-123456"
    s = T.settings({"console": {"tickets": {"prefix": "urc", "digits": 5}}})
    assert T.ref(7, s) == "URC-00007"
    bad = T.settings({"console": {"tickets": {"prefix": "A-B", "digits": 2}}})
    assert bad.prefix == "ARC" and bad.digits == 4 and len(bad.problems) == 2
    assert T.settings({"console": {"tickets": {"digits": True}}}).problems
    d = T.Settings()
    for text, n in (("ARC-0012", 12), ("arc-12", 12), ("ARC 12", 12), ("ARC0012", 12), ("#12", 12), (" 12 ", 12),
                    ("URC-0012", None), ("ARC-", None), ("0", None), ("12a", None), ("", None), (None, None)):
        assert T.number_of(text, d) == n, text
    assert T.number_of("URC-0012", s) == 12
    # the prefix setting goes through the requests' settings, its problems with theirs
    on = {"directory": {"uri": "ldaps://ldap.example.edu", "base": "dc=x"}, "console": {"tickets": {"prefix": "9x"}}}
    got = A.settings(on)
    assert got.tickets.prefix == "ARC" and any("[console.tickets] prefix" in p for p in got.problems)


def test_requests_made_before_keep_their_numbers(tmp_path, monkeypatch):
    """A lab_check.db written by 1.42/1.43: its requests are carried into the
    sequence, and the next number follows the last of them."""
    monkeypatch.setattr(L.os, "geteuid", lambda: 1000)
    path = tmp_path / "lab_check.db"
    old = L.open_records(path, write=True)
    old.executescript(A.SCHEMA)
    for i in (1, 2, 5):
        old.execute("INSERT INTO account_requests (id, lab, by, at) VALUES (?, 'doelab', 'pi1', ?)",
                    (i, f"2026-11-0{i}T10:00:00"))
        old.execute("INSERT INTO account_request_people (request, netid, site) VALUES (?, ?, 'spydur')", (i, f"s{i}"))
    old.commit()
    old.close()
    rec = A.open_records(path, write=True)
    assert [tuple(r) for r in rec.execute("SELECT number, kind FROM tickets ORDER BY number")] == [
        (1, "accounts"), (2, "accounts"), (5, "accounts")]
    req = A.ask(rec, "doelab", people("jdoe"), ["spydur"], S, by="pi1")
    assert (req["id"], req["ref"]) == (6, "ARC-0006")
    assert T.kind_of(rec, 6) == "accounts" and T.kind_of(rec, 99) is None
    # an older nomad writing one more request meanwhile: carried over when next opened, never given twice
    rec.execute("INSERT INTO account_requests (id, lab, by, at) VALUES (7, 'doelab', 'pi1', '2026-11-10T10:00:00')")
    rec.commit()
    rec = A.open_records(path, write=True)
    assert A.ask(rec, "doelab", people("bkim"), ["spydur"], S, by="pi1")["id"] == 8
    # opened again and again: nothing doubled
    rec = A.open_records(path, write=True)
    assert rec.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 6


def test_a_request_not_kept_gives_its_number_back(rec):
    A.ask(rec, "doelab", people("jdoe"), ["spydur"], S, by="pi1")
    with pytest.raises(A.RequestError, match="already asked"):
        A.ask(rec, "doelab", people("jdoe"), ["spydur"], S, by="pi1")
    assert A.ask(rec, "doelab", people("bkim"), ["spydur"], S, by="pi1")["ref"] == "ARC-0002"
    assert [r[0] for r in rec.execute("SELECT number FROM tickets")] == [1, 2]
    with pytest.raises(ValueError):
        T.take(rec, "coffee")


def test_the_number_is_in_the_requests_and_the_log(rec):
    s = A.Settings(enabled=True, sites=("spydur",), most=5, tickets=T.Settings(prefix="URC", digits=3))
    req = A.ask(rec, "doelab", people("jdoe", "bkim"), ["spydur"], s, by="pi1")
    assert req["ref"] == "URC-001"
    A.close(rec, req["id"], "jdoe", "spydur", "made", by="arc1", s=s)
    assert A.requests(rec, s=s)[0]["ref"] == "URC-001"
    assert A.requests(rec)[0]["ref"] == "ARC-0001"                       # without settings: the default
    assert A.request_of(rec, req["id"], s)["ref"] == "URC-001" and A.request_of(rec, 99) is None
    log = [json.loads(r[0]) for r in rec.execute("SELECT what FROM lab_check_log ORDER BY rowid")]
    assert log[0]["account_request"]["ref"] == "URC-001" and log[1]["account_closed"]["ref"] == "URC-001"


# --- finding one ------------------------------------------------------------------------

def test_find_by_number_netid_name_lab_or_pi(rec):
    a = A.ask(rec, "doelab", [{"netid": "jdoe", "name": "Jane Doe"}], ["spydur"], S, by="pi1")
    b = A.ask(rec, "kimlab", [{"netid": "bkim", "name": "Bo Kim"}, {"netid": "u12x", "name": "Ann_Lee"}],
              ["arachne"], S, by="pi2")
    ids = lambda got: [r["id"] for r in got]  # noqa: E731
    assert ids(A.find(rec, "ARC-0001", S)) == [a["id"]] and A.find(rec, "ARC-0001", S)[0]["people"]
    assert ids(A.find(rec, "#2", S)) == [b["id"]] and ids(A.find(rec, "2", S)) == [b["id"]]
    assert A.find(rec, "ARC-0099", S) == []                              # a number: that one or none
    assert ids(A.find(rec, "12", S)) == [b["id"]]                        # no request 12: part of a NetID
    assert ids(A.find(rec, "JDOE", S)) == [a["id"]]
    assert ids(A.find(rec, "jane d", S)) == [a["id"]]
    assert ids(A.find(rec, "lab", S)) == [b["id"], a["id"]]              # newest first
    assert ids(A.find(rec, "pi2", S)) == [b["id"]]
    assert ids(A.find(rec, "lab", S, lab="doelab")) == [a["id"]]
    assert ids(A.find(rec, "ARC-0002", S, lab="doelab")) == []
    assert A.find(rec, "%", S) == []                                    # a percent sign is a percent sign
    assert ids(A.find(rec, "n_l", S)) == [b["id"]]                       # "_" is a letter here, not any letter
    assert A.find(rec, "o%e", S) == []
    assert A.find(rec, "   ", S) == [] and A.find(None, "jdoe", S) == []
    many = [A.ask(rec, "biglab", people(f"p{i:03d}"), ["spydur"], S, by="pi3")["id"] for i in range(5)]
    assert ids(A.find(rec, "biglab", S, most=3)) == many[::-1][:3]


# --- the emails -------------------------------------------------------------------------

class Sent(list):
    def __call__(self, msg, cfg, timeout=15):
        self.append(msg)


@pytest.fixture
def sent(monkeypatch):
    from nomad import mail
    box = Sent()
    monkeypatch.setattr(mail, "send", box)
    return box


def test_the_number_leads_both_subjects(rec, sent):
    cs = L.settings(CONFIG)
    s = A.Settings(enabled=True, sites=("spydur",), notify=("hpc@example.edu",), most=5,
                   tickets=T.Settings(prefix="URC", digits=4))
    req = A.ask(rec, "doelab", [{"netid": "jdoe", "name": "Jane Doe"}, {"netid": "bkim", "name": "Bo Kim"}],
                ["spydur"], s, by="pi1")
    assert A.tell_arc(CONFIG, cs, s, req) == "sent"
    m = sent[-1]
    assert m["Subject"] == "[URC-0001] Account request: doelab, 2 people on spydur"
    assert m.get_content().startswith("URC-0001: pi1 asked for accounts for the lab doelab")
    a = A.close(rec, req["id"], "jdoe", "spydur", "made", by="arc1", s=s)
    assert A.tell_pi(CONFIG, cs, s, A.request_of(rec, req["id"]), [a]) == "sent"
    assert sent[-1]["Subject"] == "[URC-0001] The account for jdoe on spydur is ready"   # the setting's prefix
    b = A.close(rec, req["id"], "bkim", "spydur", "not_made", by="arc1", note="left", s=s)
    A.tell_pi(CONFIG, cs, s, req, [a, b])
    m = sent[-1]
    assert m["Subject"] == "[URC-0001] Your account request: 1 ready, 1 not made"
    assert "(request URC-0001, doelab) are ready" in m.get_content() and "(request URC-0001) were not made" in \
        m.get_content()


# --- the report -------------------------------------------------------------------------

NOW = datetime(2026, 11, 12, 12, 0)


def fill(rec):
    """Three requests in October and November, one in last year's June."""
    def at(req, when):
        rec.execute("UPDATE account_requests SET at = ? WHERE id = ?", (when, req["id"]))

    def closed(req, netid, site, state, when, note=None):
        A.close(rec, req["id"], netid, site, state, by="arc1", note=note)
        rec.execute("UPDATE account_request_people SET closed_at = ? WHERE request = ? AND netid = ? AND site = ?",
                    (when, req["id"], netid, site))

    old = A.ask(rec, "oldlab", people("zed"), ["spydur"], S, by="pi9")
    at(old, "2025-06-01T09:00:00")
    closed(old, "zed", "spydur", "made", "2025-06-02T09:00:00")
    a = A.ask(rec, "doelab", people("jdoe", "bkim"), ["spydur", "arachne"], S, by="pi1")
    at(a, "2026-10-01T09:00:00")
    closed(a, "jdoe", "spydur", "made", "2026-10-02T09:00:00")          # 1 day
    closed(a, "jdoe", "arachne", "made", "2026-10-04T09:00:00")         # 3 days
    closed(a, "bkim", "spydur", "not_made", "2026-10-03T09:00:00", note="=not enrolled")
    A.close(rec, a["id"], "bkim", "arachne", "withdrawn", by="pi1")
    b = A.ask(rec, "kimlab", people("mlee", "jdoe"), ["spydur"], S, by="pi2")
    at(b, "2026-10-20T09:00:00")
    closed(b, "mlee", "spydur", "made", "2026-10-30T09:00:00")          # 10 days
    c = A.ask(rec, "doelab", people("asmith"), ["arachne"], S, by="pi1")
    at(c, "2026-11-05T12:00:00")                                         # still to make: 7 days old
    rec.commit()
    return old, a, b, c


def test_report_in_all_by_cluster_lab_and_month(rec):
    old, a, b, c = fill(rec)
    r = A.report(rec, datetime(2026, 9, 1), datetime(2026, 11, 13), s=S, now=NOW)
    t = r["totals"]
    assert (t["requests"], t["labs"], t["pis"], t["people"], t["accounts"]) == (3, 2, 2, 4, 7)
    assert (t["made"], t["not_made"], t["withdrawn"], t["open"]) == (3, 1, 1, 2)
    assert t["days_to_make"] == {"made": 3, "median": 3.0, "p90": 10.0, "longest": 10.0}
    assert r["slowest"] == {"ref": b["ref"], "lab": "kimlab", "site": "spydur", "days": 10.0}
    assert r["first_asked"] == "2025-06-01T09:00:00"
    sites = {x["site"]: x for x in r["by_cluster"]}
    assert (sites["spydur"]["accounts"], sites["spydur"]["made"], sites["spydur"]["open"]) == (4, 2, 1)
    assert sites["arachne"]["days_to_make"]["median"] == 3.0
    labs = {x["lab"]: x for x in r["by_lab"]}
    assert labs["doelab"]["pis"] == ["pi1"] and labs["doelab"]["requests"] == 2 and "oldlab" not in labs
    assert [(m["month"], m["requests"], m["accounts"]) for m in r["by_month"]] == [("2026-10", 2, 6), ("2026-11", 1, 1)]
    assert r["open_now"] == {"accounts": 2, "requests": 2, "oldest": {"ref": b["ref"], "days": 23.1}}
    one = A.report(rec, datetime(2026, 9, 1), datetime(2026, 11, 13), site="arachne", s=S, now=NOW)
    assert (one["totals"]["accounts"], one["totals"]["open"]) == (3, 1)
    assert one["open_now"]["oldest"] == {"ref": c["ref"], "days": 7.0}
    none = A.report(rec, datetime(2024, 1, 1), datetime(2024, 2, 1), s=S, now=NOW)
    assert none["totals"]["requests"] == 0 and none["totals"]["days_to_make"] is None and none["slowest"] is None
    assert A.report(None, datetime(2026, 1, 1), datetime(2026, 2, 1))["totals"]["accounts"] == 0


def test_records_as_a_spreadsheet_keep_text_as_text(rec):
    fill(rec)
    rows = A.records(rec, datetime(2026, 9, 1), datetime(2026, 11, 13), s=S)
    assert len(rows) == 7 and list(rows[0]) == list(A.RECORD_COLUMNS)
    bkim = next(r for r in rows if r["netid"] == "bkim" and r["cluster"] == "spydur")
    assert (bkim["ref"], bkim["state"], bkim["days"], bkim["closed_note"]) == ("ARC-0002", "not made", 2.0,
                                                                                "=not enrolled")
    waiting = next(r for r in rows if r["netid"] == "asmith")
    assert waiting["state"] == "still to make" and waiting["days"] == "" and waiting["closed_at"] == ""
    text = A.csv_text(rows)
    got = list(csv.reader(io.StringIO(text)))
    assert got[0] == list(A.RECORD_COLUMNS) and len(got) == 8
    assert "'=not enrolled" in text and ",=not" not in text                # never a formula in a spreadsheet


# --- `nomad lab accounts` ---------------------------------------------------------------

@pytest.fixture
def cli(tmp_path, rec, monkeypatch):
    from nomad import lab_accounts_cli as C
    from nomad.cli import cli as main
    cfg = {**CONFIG, "console": {**CONFIG["console"], "tickets": {"prefix": "ARC"}}}
    monkeypatch.setattr(C, "_ctx", lambda ctx: (cfg, None, L.settings(cfg), None, tmp_path / "lab_check.db"))
    return lambda *args: CliRunner().invoke(main, ["lab", "accounts", *args])


def test_cli_list_find_show_and_close_by_number(rec, cli, sent):
    old, a, b, c = fill(rec)
    r = cli("list", "--find", "jdoe")
    assert r.exit_code == 0, r.output
    assert "ARC-0003" in r.output and "ARC-0002" in r.output and "ARC-0004" not in r.output
    r = cli("show", "ARC-0004")
    assert r.exit_code == 0 and "asmith" in r.output and r.output.startswith("ARC-0004 ")
    assert json.loads(cli("show", "4", "--json").output)["ref"] == "ARC-0004"
    r = cli("show", "XYZ-0004")
    assert r.exit_code != 0 and "not a request's number" in r.output
    assert "no request ARC-0099" in cli("show", "ARC-0099").output
    r = cli("close", "ARC-0004", "asmith", "arachne", "made", "--by", "arc1")
    assert r.exit_code == 0, r.output
    assert "ARC-0004 asmith on arachne: made." in r.output and "Email to pi1: sent" in r.output
    assert sent[-1]["Subject"] == "[ARC-0004] The account for asmith on arachne is ready"
    assert "No requests found by 'nobody'." in cli("list", "--find", "nobody").output


def test_cli_report(rec, cli):
    fill(rec)
    r = cli("report", "--since", "2026-09-01", "--until", "2026-11-12")
    assert r.exit_code == 0, r.output
    assert "Account requests asked 2026-09-01 to 2026-11-12" in r.output
    assert "3 requests from 2 labs (2 PIs): 7 accounts for 4 people: made 3, not made 1, withdrawn 1, " \
           "still to make 2" in r.output
    assert "median 3 days, 9 in 10 within 10, longest 10 (ARC-0003, kimlab on spydur)" in r.output
    assert "By cluster:" in r.output and "By lab:" in r.output and "2026-10" in r.output
    assert "Still to make now: 2 accounts in 2 requests; the oldest ARC-0003" in r.output
    js = json.loads(cli("report", "--since", "2026-09-01", "--until", "2026-11-12", "--cluster", "arachne",
                        "--json").output)
    assert js["site"] == "arachne" and js["totals"]["accounts"] == 3
    out = cli("report", "--since", "2026-09-01", "--until", "2026-11-12", "--csv").output
    assert out.startswith(",".join(A.RECORD_COLUMNS)) and out.count("\n") == 8
    assert cli("report", "--json", "--csv").exit_code != 0
    assert cli("report", "--since", "2026-12-01", "--until", "2026-11-01").exit_code != 0
    assert cli("report", "--since", "soon").exit_code != 0
    assert "none." in cli("report", "--since", "2020-01-01", "--until", "2020-02-01").output


# --- the usage report: section 15, counted ----------------------------------------------

def test_usage_report_counts_the_requests_naming_no_one(tmp_path, monkeypatch):
    from tests.test_usage_report import CONFIG as REPORT, make_site

    from nomad.usage import config as ucfg, run
    site = make_site(tmp_path / "c1.db")
    (tmp_path / "report.toml").write_text(REPORT)
    cfg = ucfg.load(tmp_path / "report.toml", "c1")
    report, data = run.build(cfg, datetime(2026, 1, 1), datetime(2026, 3, 1), db=site, site=None)
    s15 = next(s for s in report.sections if s.number == 15)
    assert s15.status == "not measured" and "no account requests kept (lab_check.db not found" in s15.finding

    monkeypatch.setattr(L.os, "geteuid", lambda: 1000)
    rec = A.open_records(tmp_path / "lab_check.db", write=True)
    s = A.Settings(enabled=True, sites=("c1", "other"), most=20)
    for i, (lab, pi, who, when, made) in enumerate((
            ("zlab", "pi_frank", ["alice2", "bob2"], "2026-01-10T09:00:00", "2026-01-12T09:00:00"),
            ("qlab", "pi_gina", ["carol2"], "2026-02-03T09:00:00", None),
            ("qlab", "pi_gina", ["dave2"], "2025-12-03T09:00:00", "2025-12-04T09:00:00"))):
        req = A.ask(rec, lab, people(*who), ["c1"], s, by=pi)
        rec.execute("UPDATE account_requests SET at = ? WHERE id = ?", (when, req["id"]))
        if made:
            for n in who:
                A.close(rec, req["id"], n, "c1", "made", by="arc1")
            rec.execute("UPDATE account_request_people SET closed_at = ? WHERE request = ?", (made, req["id"]))
    A.ask(rec, "zlab", people("erin2"), ["other"], s, by="pi_frank")      # another cluster's: not counted
    rec.commit()
    report, data = run.build(cfg, datetime(2026, 1, 1), datetime(2026, 3, 1), db=site, site=None)
    s15 = next(x for x in report.sections if x.number == 15)
    get = lambda k: s15.get(k)  # noqa: E731
    assert (get("requests"), get("labs"), get("pis"), get("people"), get("accounts")) == (2, 2, 2, 3, 3)
    assert (get("made"), get("open"), get("days_to_make.median")) == (2, 1, 2.0)
    assert s15.finding == ("PIs asked for 3 accounts on c1 in 2 requests from 2 labs; 2 were made, a median 2.0 "
                           "days after they were asked; 1 is still to make.")
    assert [r[0] for r in s15.tables[0].rows] == ["Jan 2026", "Feb 2026"]
    written = run.write(report, data, tmp_path / "out", ["md", "json"])
    text = "\n".join(p.read_text() for p in written)
    for name in ("zlab", "qlab", "pi_frank", "pi_gina", "alice2", "carol2", "Carol2", "ARC-0"):
        assert name not in text, name
    assert "Accounts asked for by PIs" in text
    cov = [c for c in report.coverage if c.section.startswith("15.")]
    assert cov and cov[0].covered == "requests from 2025-12-03"


def test_usage_report_says_when_the_requests_cant_be_read(tmp_path):
    from tests.test_usage_report import CONFIG as REPORT, make_site

    from nomad.usage import config as ucfg, run
    site = make_site(tmp_path / "c1.db")
    (tmp_path / "report.toml").write_text(REPORT)
    (tmp_path / "lab_check.db").write_bytes(b"not a database at all, just text" * 10)
    cfg = ucfg.load(tmp_path / "report.toml", "c1")
    report, _ = run.build(cfg, datetime(2026, 1, 1), datetime(2026, 3, 1), db=site, site=None)
    s15 = next(s for s in report.sections if s.number == 15)
    assert s15.status == "not measured" and "can't be read by this account" in s15.finding


def test_report_old_requests_stay_out_of_the_period():
    rec = sqlite3.connect(":memory:")
    rec.row_factory = sqlite3.Row
    rec.executescript(A.SCHEMA)
    rec.execute("INSERT INTO account_requests (id, lab, by, at) VALUES (1, 'l', 'p', '2026-03-01T00:00:00')")
    rec.execute("INSERT INTO account_request_people (request, netid, site) VALUES (1, 'x', 's')")
    r = A.report(rec, datetime(2026, 1, 1), datetime(2026, 3, 1), now=datetime(2026, 3, 2))
    assert r["totals"]["accounts"] == 0 and r["open_now"]["accounts"] == 1   # the end is not in it; open is now
    assert r["open_now"]["oldest"]["days"] == 1.0


# --- data unchanged for a year (#119) ---------------------------------------------------

from nomad.storage import cold as C  # noqa: E402

TBYTES = int(1e12)
WHEN = "2026-11-10T03:00:00"


def storage_db(path):
    from nomad.collectors.storage_use import SCHEMA as STORAGE
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript(STORAGE)
    c.executescript("""
        CREATE TABLE jobs (job_id TEXT, user_name TEXT, submit_time TEXT);
        CREATE TABLE group_membership (username TEXT, group_name TEXT, gid INTEGER, cluster TEXT,
            collected_at TEXT, has_account INTEGER, source_site TEXT);""")
    for server, copy in (("fs1", None), ("fs2", None), ("backup", "fs1")):
        c.execute("INSERT INTO storage_reports (server, mode, started, finished, copy_of) VALUES (?, 'usage', ?, ?, ?)",
                  (server, WHEN, WHEN, copy))
    for server, p, total, used in (("fs1", "/home", 100, 90), ("fs1", "/scratch", 500, 100), ("fs2", "/data", 50, 10),
                                   ("backup", "/home", 200, 90)):
        c.execute("INSERT INTO storage_paths (timestamp, server, path, total_bytes, used_bytes) VALUES (?, ?, ?, ?, ?)",
                  (WHEN, server, p, total * TBYTES, used * TBYTES))
    for server, p, owner, size, old1, old2, newest in (
            ("fs1", "/home", "ann", 5, 4, 3, "2025-06-01T00:00:00"),
            ("fs1", "/home", "bob", 2, 1, 0.5, "2026-11-01T00:00:00"),
            ("fs1", "/scratch", "ann", 2, 1.8, 1.5, "2025-01-01T00:00:00"),
            ("fs1", "/scratch", "cparish", 10, 9, 8, "2025-09-01T00:00:00"),
            ("backup", "/home", "ann", 5, 4, 3, "2025-06-01T00:00:00")):
        c.execute("INSERT INTO storage_tops (scanned_at, server, path, top, top_owner, bytes, files, newest, "
                  "older1_bytes, older2_bytes, age1_days, age2_days, complete) VALUES (?, ?, ?, ?, ?, ?, 10, ?, ?, ?, "
                  "180, 365, 1)", (WHEN, server, p, f"{p}/{owner}", owner, int(size * TBYTES), newest,
                                   int(old1 * TBYTES), int(old2 * TBYTES)))
    t = datetime.now().isoformat(timespec="seconds")
    for user, group in (("cparish", "cparish$"), ("ann", "cparish$")):
        c.execute("INSERT INTO group_membership VALUES (?, ?, 1, 'spydur', ?, 1, 'spydur')", (user, group, t))
    c.execute("INSERT INTO jobs VALUES ('1', 'ann', '2025-03-01T10:00:00')")
    c.commit()
    return c


COLD = {"console": {"labs": {"group_pattern": "{netid}$", "accounts": {"notify": ["hpc@example.edu"]},
                             "check": {"mail_domain": "example.edu", "send_from": "RC <rc@example.edu>"}},
                    "cold_data": {"min_tb": 1, "link": "https://nomad.example.edu/diagnostics?tab=storage"}}}


@pytest.fixture
def cold(tmp_path, monkeypatch):
    monkeypatch.setattr(L.os, "geteuid", lambda: 1000)
    conn = storage_db(tmp_path / "combined.db")
    rec = C.open_records(tmp_path / "lab_check.db", write=True)
    rec.execute("INSERT INTO lab_check_answers VALUES ('2026-09', 'cparish$', 'ann', 'member', 'left', 'remove', NULL, "
                "'cparish', 'pi', '2026-09-20T10:00:00')")
    rec.commit()
    return conn, rec, C.settings(COLD)


def test_cold_settings():
    s = C.settings({})
    assert (s.min_tb, s.days, s.full_share, s.quiet_days, s.notify, s.problems) == (1.0, 365, 0.85, 30, (), ())
    assert C.settings(COLD).notify == ("hpc@example.edu",)                 # the account requests' by default
    bad = C.settings({"console": {"cold_data": {"min_tb": -1, "days": 7, "full_share": 2, "quiet_days": 1.5,
                                                "notify": "x", "link": "ftp://x"}}})
    assert (bad.min_tb, bad.days, bad.full_share, bad.quiet_days, bad.notify, bad.link) == (1.0, 365, 0.85, 30, (), "")
    assert len(bad.problems) == 5
    assert C.settings({"console": {"cold_data": {"notify": ["a@x.edu"], "min_tb": 0.5}}}).min_bytes == TBYTES // 2


def test_the_list(cold):
    conn, rec, s = cold
    got = C.listing(conn, rec, COLD, s)
    assert [(r["person"], r["server"], r["path"], r["unchanged_bytes"] // TBYTES) for r in got["rows"]] == [
        ("cparish", "fs1", "/scratch", 8), ("ann", "fs1", "/home", 3), ("ann", "fs1", "/scratch", 1)]
    assert got["people"] == 2 and got["total_bytes"] == 12.5 * TBYTES
    ann = got["rows"][1]
    assert ann["unchanged_days"] == 365 and ann["share"] == 0.6 and ann["folders"] == ["/home/ann"]
    assert ann["labs"] == [{"lab": "cparish$", "name": "cparish$", "pis": ["cparish"], "pi": False}]
    assert ann["lab_check"]["said"] == "has left the lab; their data can be removed"
    assert ann["last_job"] == "2025-03-01T10:00:00" and ann["case"] is None and ann["path_full_share"] == 0.9
    assert got["rows"][0]["labs"][0]["pi"] is True
    home = next(p for p in got["paths"] if p["path"] == "/home")
    assert home["nearly_full"] is True and home["listed_bytes"] == 3 * TBYTES
    assert not any(p["server"] == "backup" for p in got["paths"])           # copies are not anyone's data
    assert got["not_measured"] == ["fs2:/data: no scan of its folders' ages yet"]
    more = C.listing(conn, rec, COLD, C.settings({"console": {"cold_data": {"min_tb": 2, "days": 180}}}))
    assert [(r["person"], r["unchanged_bytes"] // TBYTES, r["unchanged_days"]) for r in more["rows"]] == [
        ("cparish", 9, 180), ("ann", 4, 180)]
    longer = C.listing(conn, rec, COLD, C.settings({"console": {"cold_data": {"days": 730}}}))
    assert longer["rows"][0]["unchanged_days"] == 365                       # the scan's oldest age, said
    assert any("ages up to 365 days, not 730" in m for m in longer["not_measured"])
    assert C.listing(None)["not_measured"] and C.listing(None)["rows"] == []


def test_cases_share_the_numbers_and_move_on(cold):
    conn, rec, s = cold
    A.ask(rec, "doelab", people("jdoe"), ["spydur"], S, by="pi1")             # ARC-0001: an account request
    row = C.listing(conn, rec, COLD, s)["rows"][1]
    c = C.open_case(rec, "ann", "fs1", "/home", by="arc1", note="emailed her", row=row, s=s)
    assert (c["ref"], c["state"], c["said"], c["open"]) == ("ARC-0002", "to_contact", "to contact", True)
    assert c["unchanged_at_open"] == 3 * TBYTES and c["notes"][0]["note"] == "emailed her"
    assert T.kind_of(rec, 2) == "data"
    with pytest.raises(C.CaseError, match="ARC-0002 is already open"):
        C.open_case(rec, "ann", "fs1", "/home", by="arc1", s=s)
    with pytest.raises(C.CaseError, match="which server"):
        C.open_case(rec, "ann", "fs1", "home", by="arc1", s=s)
    c = C.update_case(rec, 2, by="arc2", state="contacted", note="she'll move it to the archive", s=s)
    assert c["state"] == "contacted" and c["updated_by"] == "arc2" and len(c["notes"]) == 2
    with pytest.raises(C.CaseError, match="why it is kept"):
        C.update_case(rec, 2, by="arc2", state="keep", s=s)
    with pytest.raises(C.CaseError, match="nothing to change"):
        C.update_case(rec, 2, by="arc2", state="contacted", s=s)
    with pytest.raises(C.CaseError, match="no case ARC-0099"):
        C.update_case(rec, 99, by="arc2", state="moved", s=s)
    with pytest.raises(C.CaseError, match="one of"):
        C.update_case(rec, 2, by="arc2", state="lost", s=s)
    C.update_case(rec, 2, by="arc2", note="a reminder sent", s=s)
    c = C.update_case(rec, 2, by="arc2", state="keep", note="  the lab's  reference data ", s=s)
    assert (c["state"], c["reason"], c["open"]) == ("keep", "the lab's reference data", False)
    assert [n["state"] for n in c["notes"]] == ["to_contact", "contacted", None, "keep"]
    again = C.open_case(rec, "ann", "fs1", "/home", by="arc1", s=s)          # closed: another may open
    assert again["ref"] == "ARC-0003"
    assert [x["ref"] for x in C.cases(rec, s, open_only=True)] == ["ARC-0003"]
    assert C.listing(conn, rec, COLD, s)["rows"][1]["case"]["ref"] == "ARC-0003"   # the newest stands
    log = [json.loads(r[0]) for r in rec.execute("SELECT what FROM lab_check_log")]
    assert sum(1 for x in log if "data_case" in x) == 5
    assert A.request_of(rec, 1) and C.case_of(rec, 1, s) is None              # a request's number is no case


def test_csv_and_the_weekly_email(cold):
    conn, rec, s = cold
    C.open_case(rec, "=cmd", "fs1", "/home", by="arc1", s=s)
    rec.execute("UPDATE data_cases SET updated_at = '2026-01-01T00:00:00'")
    rec.commit()
    C.open_case(rec, "ann", "fs1", "/home", by="arc1", s=s)
    got = C.listing(conn, rec, COLD, s)
    rows = list(csv.reader(io.StringIO(C.csv_text(got))))
    assert rows[0] == list(C.CSV_COLUMNS) and len(rows) == 4
    ann = next(r for r in rows if r[2] == "ann" and r[4] == "/home")
    assert ann[0] == "ARC-0002" and ann[1] == "to contact" and ann[5] == "3.0" and ann[13].startswith("has left")
    new = C.note_seen(rec, got)
    assert len(new) == 3 and C.note_seen(rec, C.listing(conn, rec, COLD, s)) == []
    subject, text = C.digest(got, new, C.cases(rec, s, open_only=True), s, now=datetime(2026, 11, 12))
    assert subject == "Data unchanged for a year: 2 people, 12.5 TB; 1 nearly full"
    assert text.startswith("NEARLY FULL: fs1:/home is 90% full; 3.0 TB of it is on the list.")
    assert "New on the list (3):" in text and "Open cases: 2 to contact." in text
    assert "Not updated in 30 days:\n  ARC-0001 =cmd fs1:/home" in text
    assert "Not a case yet: 2, 9.5 TB" in text
    assert "ann          fs1:/scratch  1.5 TB  (has left the lab; their data can be removed)" in text
    assert "https://nomad.example.edu/diagnostics?tab=storage" in text and "fs2:/data" in text


def test_cold_cli(cold, tmp_path, monkeypatch):
    from nomad import lab_check_cli, mail
    from nomad.cli import cli as main
    conn, rec, s = cold
    monkeypatch.setattr(lab_check_cli, "_ctx", lambda ctx: (COLD, None, L.settings(COLD), conn,
                                                            tmp_path / "lab_check.db"))
    sent = []
    monkeypatch.setattr(mail, "send", lambda msg, cfg, timeout=15: sent.append(msg))
    run = lambda *a: CliRunner().invoke(main, ["storage", "cold", *a])  # noqa: E731
    r = run()
    assert r.exit_code == 0, r.output
    assert "NEARLY FULL: fs1:/home is 90% full" in r.output and "2 people hold 12.5 TB" in r.output
    assert "lab check: has left the lab; their data can be removed (cparish$, 2026-09-20)" in r.output
    assert "cparish$ (PI)" in r.output and "not measured: fs2:/data" in r.output
    r = run("open", "ann", "fs1:/home", "--note", "asked her", "--by", "arc1")
    assert r.exit_code == 0 and "ARC-0001: ann on fs1:/home, to contact." in r.output
    assert run("open", "ann", "fs1:/home").exit_code != 0
    assert run("open", "ann", "fs1/home").exit_code != 0
    r = run("open", "zed", "fs1:/home", "--by", "arc1")
    assert r.exit_code == 0 and "isn't on the list now" in r.output
    r = run("case", "ARC-0001", "keep", "--note", "teaching data", "--by", "arc2")
    assert r.exit_code == 0 and "ARC-0001: kept (teaching data)." in r.output
    assert run("case", "ARC-0001").exit_code != 0                            # nothing to change
    r = run("show", "1")
    assert "ARC-0001 ann fs1:/home: kept (teaching data)" in r.output and "arc1: to contact: asked her" in r.output
    assert "not a case's number" in run("show", "XYZ-1").output
    assert "ARC-0001,kept,ann,fs1,/home" in run("list", "--csv").output
    assert json.loads(run("list", "--json", "--server", "fs1").output)["people"] == 2
    assert run("list", "--server", "fs2").output.startswith("0 people hold 0.0 TB")
    r = run("digest")
    assert r.exit_code == 0 and r.output.startswith("Subject: Data unchanged for a year") and not sent
    r = run("digest", "--send")
    assert r.exit_code == 0 and sent[-1]["To"] == "hpc@example.edu" and "New on the list (3)" in sent[-1].get_content()
    run("digest", "--send")
    assert "New on the list" not in sent[-1].get_content()                  # once each
    r = run("timer", "--user", "root")
    assert r.exit_code != 0


def test_usage_report_reads_the_requests_where_the_lab_check_keeps_them(tmp_path, monkeypatch):
    """[console.labs.check] db: the requests aren't beside the database (mingus keeps them in the nomad
    account's home); the usage report reads them there."""
    from tests.test_usage_report import CONFIG as REPORT, make_site

    from nomad.cli import cli as main
    site = make_site(tmp_path / "c1.db")
    (tmp_path / "report.toml").write_text(REPORT)
    monkeypatch.setattr(L.os, "geteuid", lambda: 1000)
    elsewhere = tmp_path / "home" / "lab_check.db"
    rec = A.open_records(elsewhere, write=True)
    req = A.ask(rec, "zlab", people("alice2"), ["c1"], A.Settings(enabled=True, sites=("c1",), most=5), by="pi_x")
    rec.execute("UPDATE account_requests SET at = '2026-01-15T10:00:00' WHERE id = ?", (req["id"],))
    rec.commit()
    toml = tmp_path / "nomad.toml"
    toml.write_text(f'[console.labs.check]\ndb = "{elsewhere}"\n')
    out = tmp_path / "out"
    r = CliRunner().invoke(main, ["-c", str(toml), "usage-report", "--db", str(site), "--config",
                                  str(tmp_path / "report.toml"), "--cluster", "c1", "--from", "2026-01-01",
                                  "--to", "2026-03-01", "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "15. Accounts asked for by PIs: PIs asked for 1 account on c1 in 1 request from 1 lab" in r.output

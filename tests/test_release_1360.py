# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.36.0: `nomad lab check names --set` finds the PIs' names on the system
(read-only, no login), asks for the rest, and writes them to
[console.labs.check.names] with nomad's careful edit; `nomad lab check name
NETID First Last` for one."""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from nomad import lab_check as L
from nomad import lab_check_cli as C
from nomad import lab_check_names as N
from nomad.config.edit import TomlEdit, loads, value
from tests.test_release_1330 import NOW, REPORT, hub

REAL_DIRECTORY_OF = N.directory_of
TOML = """# nomad.toml (a test)
[hub]
output_db = "/var/lib/nomad/combined.db"

[console.labs]
group_pattern = "{netid}$"
not_labs = ["installer$"]   # service accounts

[console.labs.check]
opens = ["09-15", "02-01", "06-15"]
mail_domain = "example.edu"
link = "https://nomad.example.edu/lab"

# PIs' names for the lab check email ("Dear Dr. LAST,").
[console.labs.check.names]
cparish = "Carol Parish"

[alerts]
email = false
"""


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(L, "_system_name", lambda netid: None)
    monkeypatch.setattr(L.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(N, "directory_of", lambda s, paths=None: (None, "off (a test)"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    """nomad.toml on disk, the hub's database, and `nomad lab check` reading both."""
    toml = tmp_path / "nomad.toml"
    toml.write_text(TOML)
    os.chmod(toml, 0o640)
    conn = hub(tmp_path / "combined.db", now=NOW)

    def ctx(c):
        config = loads(toml.read_text())
        return config, REPORT, L.settings(config, REPORT), conn, tmp_path / "lab_check.db"
    monkeypatch.setattr(C, "_ctx", ctx)
    monkeypatch.setattr(L, "datetime", type("D", (L.datetime,), {"now": classmethod(lambda c, tz=None: NOW)}))
    yield toml
    conn.close()


def run(toml, *args, input=None, tty=True, monkeypatch=None):
    from nomad.cli import cli
    if monkeypatch is not None:
        monkeypatch.setattr(C, "_interactive", lambda: tty)
    return CliRunner().invoke(cli, ["-c", str(toml), "lab", "check", *args], input=input)


# -- finding ------------------------------------------------------------------------------------

def test_ldap_conf_and_the_setting(tmp_path):
    conf = tmp_path / "ldap.conf"
    conf.write_text("# a comment\nBASE   ou=people,dc=example,dc=edu\nURI ldap://ldap.example.edu ldap://b\n")
    assert N._ldap_conf([str(tmp_path / "none"), str(conf)]) == ("ldap://ldap.example.edu", "ou=people,dc=example,dc=edu")
    (tmp_path / "half.conf").write_text("URI ldap://x.edu\n")
    assert N._ldap_conf([str(tmp_path / "half.conf")]) is None
    auto = L.settings({}, None)
    assert auto.directory == "auto"
    assert REAL_DIRECTORY_OF(auto, [str(conf)]) == (("ldap://ldap.example.edu", "ou=people,dc=example,dc=edu", "uid"),
                                                    "from ldap.conf")
    assert REAL_DIRECTORY_OF(auto, [str(tmp_path / "none")])[0] is None
    off = L.settings({"console": {"labs": {"check": {"directory": False}}}}, None)
    assert REAL_DIRECTORY_OF(off)[0] is None and "off" in REAL_DIRECTORY_OF(off)[1]
    given = L.settings({"console": {"labs": {"check": {"directory": {"uri": "ldaps://l.example.edu", "base": "dc=example,dc=edu"}}}}}, None)
    assert REAL_DIRECTORY_OF(given) == (("ldaps://l.example.edu", "dc=example,dc=edu", "uid"),
                                        "from [console.labs.check] directory")
    bad = L.settings({"console": {"labs": {"check": {"directory": {"uri": "http://x", "base": "b"}}}}}, None)
    assert bad.directory is None and any("directory" in p for p in bad.problems)


def test_the_directory_without_a_login():
    ran = []
    ldif = ("dn: uid=cparish,ou=people,dc=example,dc=edu\nuid: cparish\ngivenName: Carol\nsn: Parish\n\n"
            "dn: uid=mdc,ou=people,dc=example,dc=edu\nuid: mdc\ngivenName: Maria\nsn:: ZGUgbGEgQ3J1eg==\n\n"
            "dn: uid=kd,ou=people\n dc=example\nuid: kd\ncn: Kay Donald\n\n"
            "dn: uid=x\nuid: x\ncn: x\n\n")

    def fake(cmd, **kw):
        ran.append((cmd, kw))
        return SimpleNamespace(returncode=0, stdout=ldif, stderr="")
    got, why = N.from_directory(["cparish", "mdc", "kd", "x", "BAD!"], ("ldap://ldap.example.edu", "dc=example,dc=edu", "uid"),
                                run=fake, which=lambda x: "/usr/bin/ldapsearch")
    assert why is None
    assert got == {"cparish": ("Carol", "Parish"), "mdc": ("Maria", "de la Cruz"), "kd": ("Kay", "Donald")}
    cmd, kw = ran[0]
    assert cmd[:3] == ["/usr/bin/ldapsearch", "-x", "-LLL"] and "-D" not in cmd and "-w" not in cmd and "-W" not in cmd
    assert "(|(uid=cparish)(uid=mdc)(uid=kd)(uid=x))" in cmd
    assert kw["env"]["LDAPRC"] == ".nomad-reads-no-ldaprc" and kw["stdin"] == subprocess.DEVNULL
    # UR's: the base answers, its people don't without a login
    no = N.from_directory(["cparish"], ("ldap://l.edu", "dc=e", "uid"),
                          run=lambda c, **k: SimpleNamespace(returncode=32, stdout="", stderr="No such object"),
                          which=lambda x: "/bin/ldapsearch")
    assert no == ({}, "shows no one without a login")
    assert N.from_directory(["cparish"], ("ldap://l.edu", "dc=e", "uid"), which=lambda x: None)[1].startswith(
        "ldapsearch is not installed")
    assert N._escape("a*(b)\\") == "a\\2a\\28b\\29\\5c"


def test_what_each_source_gave():
    s = L.settings({}, None)
    found, said = N.find(["cparish", "myang", "kd"], s, account=lambda n: ("Carol", "Parish") if n == "cparish" else None,
                         directory=(("ldap://l.edu", "dc=e", "uid"), "from ldap.conf"),
                         search=lambda left, d: ({"myang": ("Mei", "Yang")}, None))
    assert found == {"cparish": (("Carol", "Parish"), "account"), "myang": (("Mei", "Yang"), "directory")}
    assert said == ["the account's full name (getent passwd): 1 of 3",
                    "the directory ldap://l.edu (from ldap.conf, no login): 1 of 2"]
    _, said = N.find(["kd"], s, directory=(("ldap://l.edu", "dc=e", "uid"), "from ldap.conf"),
                     search=lambda left, d: ({}, "shows no one without a login"))
    assert said[-1] == "the directory ldap://l.edu (from ldap.conf, no login): shows no one without a login"
    assert N.find(["kd"], s, lookup=False) == ({}, [])


def test_what_a_person_types():
    assert N.parse("") == ("skip", None) and N.parse("   ") == ("skip", None)
    assert N.parse("-") == ("not a lab", None)
    assert N.parse("Tom Zylkin") == ("name", "Tom Zylkin")
    assert N.parse("Maria | de la Cruz") == ("name", {"first": "Maria", "last": "de la Cruz"})
    assert N.parse("tzylkin@richmond.edu")[0] == "bad" and N.parse("x = 3")[0] == "bad"
    assert N.parse("Maria |")[0] == "bad"
    assert N.as_value("Maria", "de la Cruz") == {"first": "Maria", "last": "de la Cruz"}
    assert N.as_value("Carol", "Parish") == "Carol Parish"


def test_names_from_a_file(tmp_path):
    t = tmp_path / "pi_names.toml"
    t.write_text('[console.labs.check.names]\ncparish = "Carol Parish"\npkvam = ""\nmdc = { first = "Maria", last = "de la Cruz" }\n')
    assert N.load(t) == {"cparish": "Carol Parish", "pkvam": "", "mdc": {"first": "Maria", "last": "de la Cruz"}}
    c = tmp_path / "names.csv"
    c.write_text("# NETID,Name\ncparish,Carol Parish\nmyang\tMei Yang\nrcargill: -\n")
    assert N.load(c) == {"cparish": "Carol Parish", "myang": "Mei Yang", "rcargill": "-"}
    (tmp_path / "bad.txt").write_text("just words here\n")
    with pytest.raises(ValueError):
        N.load(tmp_path / "bad.txt")


# -- writing --------------------------------------------------------------------------------------

def test_the_names_table_where_it_is(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text(TOML)
    edit = TomlEdit(p)
    before = edit.data
    ch = N.set_names(edit, {"myang": "Mei Yang", "mdc": {"first": "Maria", "last": "de la Cruz"}, "cparish": "Carol Parish"})
    assert ch == ["myang = Mei Yang", "mdc = Maria | de la Cruz"]
    want = {"cparish": "Carol Parish", "myang": "Mei Yang", "mdc": {"first": "Maria", "last": "de la Cruz"}}
    assert N.check(before, want)(edit.data)
    assert '# PIs\' names for the lab check email ("Dear Dr. LAST,").\n[console.labs.check.names]\ncparish' in edit.text
    assert 'mdc = { first = "Maria", last = "de la Cruz" }' in edit.text
    assert "[alerts]\nemail = false" in edit.text and "not_labs = [\"installer$\"]   # service accounts" in edit.text
    assert not N.check(before, {**want, "x": "Y Z"})(edit.data)
    # a new table goes after [console.labs.check]
    q = tmp_path / "plain.toml"
    q.write_text(TOML.split("# PIs'")[0] + "[alerts]\nemail = false\n")
    e2 = TomlEdit(q)
    N.set_names(e2, {"cparish": "Carol Parish"})
    assert e2.text.index("[console.labs.check.names]") < e2.text.index("[alerts]")
    # names written inline: left for a person
    r = tmp_path / "inline.toml"
    r.write_text('[console.labs.check]\nopens = []\nnames = { cparish = "Carol Parish" }\n')
    with pytest.raises(ValueError, match="written another way"):
        N.set_names(TomlEdit(r), {"myang": "Mei Yang"})
    assert value({"first": "A", "last": "B C"}) == '{ first = "A", last = "B C" }'


def test_a_settings_file_anyone_can_read(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text("")
    os.chmod(p, 0o644)
    assert "can be read by anyone" in N.open_to_all(p)
    os.chmod(p, 0o640)
    assert N.open_to_all(p) is None


# -- the commands -----------------------------------------------------------------------------------

def test_set_asks_for_the_rest_and_writes_once_told(world, monkeypatch):
    monkeypatch.setattr(N, "find", lambda netids, s, lookup=True: (
        ({"myang": (("Mei", "Yang"), "directory")} if "myang" in netids else {}),
        ["the directory ldap://l.edu (no login): 1 of 4"]) if lookup else ({}, []))
    # missing: cbukach, kdonald, knolin, myang (cparish has a name); found myang, kept;
    # then cbukach -> a name, kdonald -> later, knolin -> not a lab; then yes, write it
    r = run(world, "names", "--set", input="y\nCatherine Bukach\n\n-\ny\n", monkeypatch=monkeypatch)
    assert r.exit_code == 0, r.output
    assert "4 PIs with no name." in r.output and "Found 1:" in r.output
    assert "  + cbukach = Catherine Bukach" in r.output and "  + myang = Mei Yang" in r.output
    assert "  + knolin$: not a lab" in r.output and "Written. Backup:" in r.output
    data = loads(world.read_text())
    assert data["console"]["labs"]["check"]["names"] == {"cparish": "Carol Parish", "cbukach": "Catherine Bukach",
                                                         "myang": "Mei Yang"}
    assert data["console"]["labs"]["not_labs"] == ["installer$", "knolin$"]
    assert data["alerts"] == {"email": False} and data["hub"]["output_db"] == "/var/lib/nomad/combined.db"
    assert oct(os.stat(world).st_mode & 0o777) == "0o640"
    assert list(world.parent.glob("nomad.toml.bak-names-*"))
    # again: only kdonald is left (knolin$ is no lab now); Enter leaves it, nothing to write
    r = run(world, "names", "--set", "--no-lookup", input="\n", monkeypatch=monkeypatch)
    assert "1 PI with no name." in r.output and "Nothing to change" in r.output


def test_set_writes_nothing_unless_told(world, monkeypatch):
    monkeypatch.setattr(N, "find", lambda netids, s, lookup=True: ({}, []))
    before = world.read_text()
    r = run(world, "names", "--set", input="Catherine Bukach\nKay Donald\nKim Nolin\nMei Yang\nn\n",
            monkeypatch=monkeypatch)
    assert r.exit_code == 0, r.output
    assert "Nothing changed." in r.output and world.read_text() == before
    # a name that isn't one is asked again
    r = run(world, "names", "--set", input="bukach@example.edu\nCatherine Bukach\n\n\n\nn\n", monkeypatch=monkeypatch)
    assert "a person's name" in r.output
    # not at a terminal, without --from
    r = run(world, "names", "--set", tty=False, monkeypatch=monkeypatch)
    assert r.exit_code != 0 and "run it in a terminal" in r.output


def test_set_from_a_file(world, tmp_path, monkeypatch):
    f = tmp_path / "pi_names.toml"
    f.write_text('[console.labs.check.names]\ncbukach = "Catherine Bukach"\nkdonald = ""\nknolin = "-"\nmyang = "m@x"\n')
    r = run(world, "names", "--set", "--from", str(f), tty=False, monkeypatch=monkeypatch)
    assert r.exit_code == 0, r.output
    assert "+ cbukach = Catherine Bukach" in r.output and "+ knolin$: not a lab" in r.output
    assert "myang = 'm@x'" in r.output and "Run again with --apply" in r.output
    assert "cbukach" not in world.read_text()
    r = run(world, "names", "--set", "--from", str(f), "--apply", tty=False, monkeypatch=monkeypatch)
    assert r.exit_code == 0 and "Written." in r.output
    assert loads(world.read_text())["console"]["labs"]["check"]["names"]["cbukach"] == "Catherine Bukach"
    r = run(world, "names", "--from", str(f), monkeypatch=monkeypatch)
    assert r.exit_code != 0 and "go with --set" in r.output


def test_one_pi(world, monkeypatch):
    r = run(world, "name", "tzylkin", "Tom", "Zylkin", monkeypatch=monkeypatch)
    assert r.exit_code == 0 and "+ tzylkin = Tom Zylkin" in r.output and "--apply" in r.output
    r = run(world, "name", "tzylkin", "Tom", "Zylkin", "--apply", monkeypatch=monkeypatch)
    assert r.exit_code == 0, r.output
    assert loads(world.read_text())["console"]["labs"]["check"]["names"]["tzylkin"] == "Tom Zylkin"
    r = run(world, "name", "mdc", "Maria", "|", "de", "la", "Cruz", "--apply", monkeypatch=monkeypatch)
    assert loads(world.read_text())["console"]["labs"]["check"]["names"]["mdc"] == {"first": "Maria", "last": "de la Cruz"}
    r = run(world, "name", "tzylkin", "--remove", "--apply", monkeypatch=monkeypatch)
    assert "tzylkin" not in loads(world.read_text())["console"]["labs"]["check"]["names"]
    assert run(world, "name", "tzylkin", "--apply", monkeypatch=monkeypatch).exit_code != 0
    assert "is not a NetID" in run(world, "name", "Bad!", "X", "Y", monkeypatch=monkeypatch).output
    r = run(world, "names", monkeypatch=monkeypatch)
    assert "Add them: nomad lab check names --set" in r.output

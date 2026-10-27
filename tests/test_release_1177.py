# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.17.7: what each machine is ([console.machines]), beside its name."""
import sqlite3
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

TOML = """[console.labs]
group_pattern = "{netid}$"

[console.labs.resources."pi$"]
name = "Pi Lab"
workstations = ["hn1", "ws1"]
storage = ["10.0.0.9", "labnas:/export/pi"]

[console.storage."10.0.0.9"]
name = "bignas"
note = "community $HOME"

[console.storage."labnas"]
note = "old note"

[console.machines."HN1"]
note = "the lab's  head node:   collects its workstations"

[console.machines."labnas"]
note = "the Pi Lab's NAS"

[console.machines."hpc1"]
note = "the research cluster"
"""


def _acc(text=TOML):
    from nomad.config.edit import loads as _loads
    from nomad.config.access import access_from
    return access_from(_loads(text), log=False)


def test_a_machine_is_described_by_name_without_case():
    acc = _acc()
    assert acc.note_of("hn1") == acc.note_of(" Hn1 ") == "the lab's head node: collects its workstations"
    # a storage server: [console.storage]'s note, under its address or its name
    assert acc.note_of("10.0.0.9") == acc.note_of("BigNas") == "community $HOME"
    # [console.machines] says it when both do
    assert acc.note_of("labnas") == "the Pi Lab's NAS"
    assert acc.note_of("ws1") == acc.note_of("") == acc.note_of(None) == ""
    assert acc.notes() == {"10.0.0.9": "community $HOME", "bignas": "community $HOME",
                           "labnas": "the Pi Lab's NAS", "hn1": "the lab's head node: collects its workstations",
                           "hpc1": "the research cluster"}
    assert not acc.problems


def test_what_cant_be_read_is_said():
    acc = _acc('[console.machines."bad name"]\nnote = "x"\n\n[console.machines.ok]\nnote = 5\ncolour = "red"\n\n'
               '[console.machines.long]\nnote = "' + "x" * 250 + '"\n\n[console.machines.Twice]\nnote = "a"\n\n'
               '[console.machines.twice]\nnote = "b"\n')
    p = " | ".join(acc.problems)
    assert "not a machine's name" in p and "note should be text" in p and "colour: unknown" in p
    assert "250 characters" in p and "described twice" in p
    assert len(acc.machines["long"]) == 200 and acc.machines["twice"] == "b" and "ok" not in acc.machines
    assert "machines" in _acc('[console]\nmachines = 3\n').problems[0]


@pytest.fixture
def hub(tmp_path):
    path = tmp_path / "combined.db"
    c = sqlite3.connect(path)
    now = datetime.now().isoformat(timespec="seconds")
    c.executescript("""
    CREATE TABLE group_membership (username TEXT, group_name TEXT, gid INTEGER, cluster TEXT,
        collected_at TEXT, has_account INTEGER, source_site TEXT);
    CREATE TABLE workstation_state (id INTEGER PRIMARY KEY, timestamp TEXT, hostname TEXT,
        department TEXT, status TEXT, source_site TEXT);
    CREATE TABLE node_state (id INTEGER PRIMARY KEY, timestamp TEXT, node_name TEXT, state TEXT,
        cpus_total INTEGER, cpus_alloc INTEGER, memory_total_mb INTEGER, memory_alloc_mb INTEGER,
        cluster TEXT, partitions TEXT, reason TEXT, gres TEXT, source_site TEXT);
    """)
    for u in ("pi", "a1"):
        c.execute("INSERT INTO group_membership VALUES (?, 'pi$', 1, 'hpc1', ?, 1, 'hpc1')", (u, now))
    for h in ("hn1", "ws1"):
        c.execute("INSERT INTO workstation_state (timestamp, hostname, department, status, source_site) "
                  "VALUES (?, ?, 'pi$', 'online', 'hpc1')", (now, h))
    c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, memory_total_mb, "
              "memory_alloc_mb, cluster, source_site) VALUES (?, 'n1', 'IDLE', 8, 0, 1000, 0, 'hpc1', 'hpc1')",
              (now,))
    c.commit()
    c.close()
    return path


def test_the_facts_carry_what_each_machine_is(hub):
    from nomad.config.edit import loads as _loads
    from nomad import facts
    config = _loads(TOML)
    conn = facts.open_readonly(hub)
    try:
        lab, = facts.labs(conn, config, "pi")["labs"]
        site, = facts.sites(conn, config=config)["sites"]
    finally:
        conn.close()
    assert {w["name"]: w.get("note") for w in lab["workstations"]} == {
        "hn1": "the lab's head node: collects its workstations", "ws1": None}
    assert {s["entry"]: s.get("note") for s in lab["storage"]} == {
        "10.0.0.9": "community $HOME", "labnas:/export/pi": "the Pi Lab's NAS"}
    assert site["name"] == "hpc1" and site["note"] == "the research cluster"


def test_the_commands_show_it_and_masking_drops_it(hub, tmp_path, monkeypatch):
    from nomad.cli import cli
    import nomad.facts.cli as fcli
    import nomad.facts as facts
    monkeypatch.setattr(fcli, "_open", lambda ctx, db: ("x.db", facts.open_readonly(hub)))
    p = tmp_path / "nomad.toml"
    p.write_text(TOML)
    out = CliRunner().invoke(cli, ["-c", str(p), "facts", "lab", "pi"]).output
    assert "hn1: the lab's head node" in out and "storage 10.0.0.9 (community $HOME)" in out, out
    out = CliRunner().invoke(cli, ["-c", str(p), "facts", "sites"]).output
    assert "hpc1 (the research cluster)" in out, out
    for args in (["facts", "lab", "pi", "--mask"], ["facts", "sites", "--mask"]):
        masked = CliRunner().invoke(cli, ["-c", str(p), *args]).output
        assert "head node" not in masked and "research cluster" not in masked and "community" not in masked
    show = CliRunner().invoke(cli, ["-c", str(p), "lab", "show", "pi"]).output
    assert "hn1: the lab's head node" in show, show


def test_nomad_machine_note_writes_one_table_and_takes_it_away(tmp_path):
    from nomad.cli import cli
    p = tmp_path / "nomad.toml"
    p.write_text(TOML)
    run = lambda *a: CliRunner().invoke(cli, ["-c", str(p), "machine", *a])   # noqa: E731
    r = run("note", "hn1")
    assert "hn1: the lab's head node" in r.output
    r = run("note", "hn1", "the Pi Lab's head node", "--apply")       # the HN1 table, in its case
    assert r.exit_code == 0 and "Written" in r.output, r.output
    from nomad.config.edit import loads as _loads
    text = p.read_text()
    assert _loads(text)["console"]["machines"]["HN1"] == {"note": "the Pi Lab's head node"}
    assert text.count("HN1") == 1 and "hn1" not in _loads(text)["console"]["machines"]
    r = run("note", "bignas", "everyone's $HOME")                      # shown, not written
    assert 'note, "community $HOME", is no longer shown' in r.output and "Nothing changed" in r.output
    r = run("note", "bignas", "--remove")
    assert "no [console.machines] note to take away" in r.output
    r = run("note", "newbox", "a  spare   machine", "--apply")
    assert _loads(p.read_text())["console"]["machines"]["newbox"] == {"note": "a spare machine"}
    r = run("note", "newbox", "--remove", "--apply")
    assert "newbox" not in p.read_text()
    r = run("note", "x y", "z")
    assert r.exit_code != 0 and "not a machine's name" in r.output
    r = run("note", "newbox", "x" * 201)
    assert r.exit_code != 0 and "200 at most" in r.output
    r = run("note", "newbox", "  ")
    assert r.exit_code != 0 and "the note is empty" in r.output
    r = run("list")
    assert "hpc1" in r.output and "bignas" in r.output and "the Pi Lab's head node" in r.output


def test_a_machine_written_inline_is_left_to_be_changed_by_hand(tmp_path):
    from nomad.cli import cli
    p = tmp_path / "nomad.toml"
    p.write_text('[console.machines]\nhn1 = { note = "x" }\n')
    r = CliRunner().invoke(cli, ["-c", str(p), "machine", "note", "hn1", "y", "--apply"])
    assert r.exit_code != 0 and "inline" in r.output and p.read_text().count("hn1") == 1


def test_the_demo_describes_its_machines(tmp_path):
    from nomad.config.edit import loads as _loads
    from nomad.config.access import access_from
    from nomad.demo_cast import demo_toml
    acc = access_from(_loads(demo_toml()), log=False)
    assert acc.note_of("nas-02") == "the Grace Lab's NAS" and acc.note_of("nas-01") == "everyone's $HOME"
    assert acc.note_of("demo-cluster") and not acc.problems


def test_one_server_one_description_under_either_name():
    acc = _acc(TOML + '\n[console.machines."bignas"]\nnote = "the big NAS"\n')
    assert acc.note_of("10.0.0.9") == acc.note_of("bignas") == "the big NAS"
    assert acc.notes()["10.0.0.9"] == acc.notes()["bignas"] == "the big NAS"


def test_whose_each_description_is(hub):
    """Sites, storage and nodes are everyone's; a workstation its lab's."""
    from nomad import facts
    from nomad.facts.machines import kinds, visible
    text = TOML + ('\n[console.machines."ws1"]\nnote = "the head node site"\n'
                   '[console.machines."nas9"]\nnote = "a NAS once collected as a workstation"\n'
                   '[console.machines."n7"]\nnote = "the GPU node"\n'
                   '[console.machines."ghost"]\nnote = "nobody knows"\n'
                   '[console.machines."otherpc"]\nnote = "Bob\'s office desktop"\n'
                   '[console.labs.resources."bob$"]\nworkstations = ["otherpc"]\nstorage = ["nas9"]\n')
    c = sqlite3.connect(hub)
    now = datetime.now().isoformat(timespec="seconds")
    c.execute("CREATE TABLE sync_sites (name TEXT PRIMARY KEY)")
    c.executemany("INSERT INTO sync_sites VALUES (?)", [("hpc1",), ("ws1",)])
    c.execute("INSERT INTO workstation_state (timestamp, hostname, department, status, source_site) "
              "VALUES ('2025-01-01T00:00:00', 'NAS9', NULL, 'online', 'hpc1')")
    c.execute("INSERT INTO workstation_state (timestamp, hostname, department, status, source_site) "
              "VALUES (?, 'ws1', 'pi$', 'online', 'hpc1')", (now,))
    c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, memory_total_mb, "
              "memory_alloc_mb, cluster, source_site) VALUES (?, 'n7', 'IDLE', 8, 0, 1000, 0, 'hpc1', 'hpc1')", (now,))
    c.commit()
    c.close()
    acc = _acc(text)
    conn = facts.open_readonly(hub)
    try:
        k = kinds(conn, acc)
        assert k["ws1"] == "site" and k["nas9"] == "storage" and k["n7"] == "node" and k["hpc1"] == "site"
        assert k["hn1"] == k["otherpc"] == "workstation" and k["ghost"] == "unknown"
        everyone = visible(conn, acc, mine=set())
        assert {"ws1", "nas9", "n7", "hpc1", "bignas", "labnas"} <= set(everyone)
        assert not {"hn1", "otherpc", "ghost"} & set(everyone)
        assert "hn1" in visible(conn, acc, mine={"HN1"}) and "ghost" in visible(conn, acc)
    finally:
        conn.close()


def test_needs_attention_never_names_another_labs_workstation(tmp_path):
    """A member's data on another lab's workstation: where, not what it is."""
    from nomad.collectors import storage_use as su
    from nomad.facts.attention import _HOLDINGS
    from nomad import facts
    path = tmp_path / "h.db"
    c = sqlite3.connect(path)
    c.executescript(su.SCHEMA)
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, gid INTEGER, cluster TEXT, "
              "collected_at TEXT, has_account INTEGER, source_site TEXT)")
    now = datetime.now().isoformat(timespec="seconds")
    for u in ("pi", "a1"):
        c.execute("INSERT INTO group_membership VALUES (?, 'pi$', 1, 'hpc1', ?, 1, 'hpc1')", (u, now))
    c.execute("INSERT INTO storage_paths (timestamp, server, path, fs, total_bytes, used_bytes, available_bytes, "
              "accounting) VALUES (?, 'bobpc', '/home', 'xfs', ?, ?, 0, 1)", (now, 10**13, 10**13))
    c.execute("INSERT INTO storage_owners (timestamp, server, path, kind, name, used_bytes) "
              "VALUES (?, 'bobpc', '/home', 'user', 'a1', ?)", (now, 3 * 10**12))
    c.commit()
    c.close()
    from nomad.config.edit import loads as _loads
    cfg = _loads('[console.labs]\ngroup_pattern = "{netid}$"\n[console.labs.resources."bob$"]\n'
                 'workstations = ["bobpc"]\n[console.machines."bobpc"]\nnote = "Bob Smith\'s office desktop"\n')
    _HOLDINGS.clear()
    conn = facts.open_readonly(path)
    try:
        pi, = facts.labs(conn, cfg, "pi", names=True)["labs"]
        staff, = facts.labs(conn, cfg, groups=["pi$"], names=True)["labs"]
    finally:
        conn.close()
    a1 = next(i["says"] for i in pi["attention"] if i["kind"] == "data")
    assert a1 == "a1 holds 3.0 TB on bobpc:/home." and "office" not in str(pi["attention"])
    assert "Bob Smith's office desktop" in str(staff["attention"])


def test_writing_a_note_keeps_the_rest_of_the_table(tmp_path):
    from nomad.cli import cli
    from nomad.config.edit import loads as _loads
    p = tmp_path / "nomad.toml"
    p.write_text('[console.storage."s1"]\nnote = "x"\n\n[console.machines."labhead1"]\n'
                 '# what it is\nnote = "old"   # keep short\nsince = 2024-01-01\n\n'
                 '[console.machines."labhead1".extra]\nroom = "B12"\n')
    r = CliRunner().invoke(cli, ["-c", str(p), "machine", "note", "labhead1", "the head\x7f node", "--apply"])
    assert r.exit_code == 0, r.output
    text = p.read_text()
    assert '# what it is\nnote = "the head node"   # keep short\nsince = 2024-01-01' in text
    assert '[console.machines."labhead1"]' in text and 'room = "B12"' in text
    r = CliRunner().invoke(cli, ["-c", str(p), "machine", "note", "labhead1", "--remove", "--apply"])
    d = _loads(p.read_text())["console"]["machines"]["labhead1"]
    assert "note" not in d and d["extra"] == {"room": "B12"}
    # a new machine goes after the last machine's table, not at the end of the file
    p.write_text('[console.machines."a"]\nnote = "x"\n\n[alerts]\nenabled = true\n')
    CliRunner().invoke(cli, ["-c", str(p), "machine", "note", "b", "y", "--apply"])
    text = p.read_text()
    assert text.index('machines.b]') < text.index("[alerts]"), text


def test_what_cant_be_written_is_refused_cleanly(tmp_path):
    from nomad.cli import cli
    p = tmp_path / "nomad.toml"
    for body, says in (('[console]\nmachines = {}\n', "inline"),
                       ('[console.machines.HN]\nnote = "a"\n\n[console.machines.hn]\nnote = "b"\n',
                        "described 2 times")):
        p.write_text(body)
        r = CliRunner().invoke(cli, ["-c", str(p), "machine", "note", "hn", "z", "--apply"])
        assert r.exit_code != 0 and says in r.output and "Traceback" not in r.output, r.output
        assert p.read_text() == body
    p.write_text("[console\n")
    for args in (["note", "hn"], ["list"]):
        r = CliRunner().invoke(cli, ["-c", str(p), "machine", *args])
        assert r.exit_code != 0 and "fix that first" in r.output, r.output


def test_a_storage_server_described_by_name_and_address_is_one_table(tmp_path):
    from nomad.cli import cli
    from nomad.config.edit import loads as _loads
    p = tmp_path / "nomad.toml"
    p.write_text('[console.storage."10.0.0.5"]\nname = "bigstore"\nnote = "old"\n\n'
                 '[console.machines."10.0.0.5"]\nnote = "by address"\n')
    run = lambda *a: CliRunner().invoke(cli, ["-c", str(p), "machine", *a])   # noqa: E731
    r = run("note", "bigstore", "by name", "--apply")                 # the address's table changes
    assert r.exit_code == 0, r.output
    m = _loads(p.read_text())["console"]["machines"]
    assert m == {"10.0.0.5": {"note": "by name"}}
    assert "by name" in run("note", "bigstore").output and "by name" in run("note", "10.0.0.5").output
    r = run("note", "bigstore", "--remove", "--apply")
    assert 'bigstore will read: "old" (from [console.storage]' in r.output, r.output
    assert "machines" not in _loads(p.read_text())["console"]


def test_taking_the_last_note_takes_the_tables_comments(tmp_path):
    from nomad.cli import cli
    p = tmp_path / "nomad.toml"
    p.write_text('[console.machines."w1"]\n# bought 2021, room 101\nnote = "old"\n\n'
                 '[console.machines."w2"]\nnote = "x"\n')
    r = CliRunner().invoke(cli, ["-c", str(p), "machine", "note", "w1", "--remove", "--apply"])
    assert r.exit_code == 0, r.output
    assert p.read_text() == '[console.machines."w2"]\nnote = "x"\n'


def test_an_unreadable_table_is_never_taken_as_nobodys(hub, monkeypatch):
    """A read that fails: only nomad.toml's word, and nothing kept."""
    import nomad.facts.machines as M
    from nomad import facts
    text = TOML + '\n[console.machines."bobpc"]\nnote = "Bob\'s office desktop"\n'
    c = sqlite3.connect(hub)
    c.execute("CREATE TABLE storage_paths (timestamp TEXT, server TEXT, path TEXT)")
    c.execute("INSERT INTO storage_paths VALUES ('2026-10-27', 'bobpc', '/home')")
    c.execute("INSERT INTO workstation_state (timestamp, hostname, status, source_site) "
              "VALUES ('2026-10-27', 'bobpc', 'online', 'hpc1')")
    c.commit()
    c.close()
    acc = _acc(text)
    orig = M._distinct
    M._KEPT.clear()

    def failing(conn, table, col, since=None):
        if table == "workstation_state":
            raise M._Unread(table)
        return orig(conn, table, col, since)
    monkeypatch.setattr(M, "_distinct", failing)
    conn = facts.open_readonly(hub)
    try:
        assert "bobpc" not in M.visible(conn, acc, mine=set())
        monkeypatch.setattr(M, "_distinct", orig)
        assert M.kinds(conn, acc)["bobpc"] == "workstation"       # nothing was kept
    finally:
        conn.close()


def test_machine_notes_keep_their_newest_backups(tmp_path):
    from nomad.cli import cli
    p = tmp_path / "nomad.toml"
    p.write_text('[console.machines."a"]\nnote = "0"\n')
    for i in range(1, 24):
        r = CliRunner().invoke(cli, ["-c", str(p), "machine", "note", "a", f"note {i}", "--apply"])
        assert r.exit_code == 0, r.output
    backups = sorted(tmp_path.glob("nomad.toml.bak-machine-*"))
    assert len(backups) == 20 and not list(tmp_path.glob("nomad.toml.bak-lab-*"))

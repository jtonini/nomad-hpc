# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""`nomad lab`: a lab's machines and storage in nomad.toml; a NAS's pools in
`nomad console roles`; machines that stopped reporting said so."""
import json
import sqlite3
import subprocess
from datetime import datetime

import pytest
from click.testing import CliRunner

import nomad.cli as cli_mod
from nomad.cli import cli
from nomad.config import labs
from nomad.config.edit import TomlEdit, key_path, loads

TB = 10 ** 12

MINGUS = '''# the hub
[general]
data_dir = "/home/zeus/.local/share/nomad"

[console.labs]
group_pattern = "{netid}$"

[console.labs.resources."pi1$"]
storage = ["10.0.0.28:/mnt/pool/home"]

# Labs without a head node
[collectors.workstation]
enabled = true

[[collectors.workstation.workstations]]
hostname = "labws"
department = "pi2$"

# Lab three
[[collectors.workstation.workstations]]
hostname = "nas2"
department = "pi3$"
'''


@pytest.fixture
def toml(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text(MINGUS)
    return p


# --- the editor --------------------------------------------------------------

def test_key_paths():
    assert key_path('console.labs.resources."pi1$"') == ("console", "labs", "resources",
                                                             "pi1$")
    assert key_path("a.'b c'.d") == ("a", "b c", "d")
    assert key_path('console.storage."10.0.0.28"') == ("console", "storage", "10.0.0.28")


def test_edits_keep_comments_and_place_entries_beside_their_kind(toml):
    e = TomlEdit(toml)
    e.add_entry(("collectors", "workstation", "workstations"),
                {"hostname": "new", "department": "x$"})
    e.set_table(("console", "labs", "resources", "pi1$"),
                {"storage": ["10.0.0.28:/mnt/pool/home", "srv:/x"]})
    text = e.text
    assert text.startswith("# the hub\n") and "# Lab three\n" in text
    # The new entry follows the last one; the replaced table stays where it was.
    assert text.index('hostname = "new"') > text.index('hostname = "nas2"')
    assert text.index("[console.labs.resources") < text.index("[collectors.workstation]")
    assert loads(text)["console"]["labs"]["resources"]["pi1$"]["storage"][1] == "srv:/x"


def test_nothing_is_written_unless_it_reads_back(toml):
    e = TomlEdit(toml)
    e.append("[broken\n")
    with pytest.raises(ValueError, match="not be valid TOML"):
        e.save(lambda d: True)
    e = TomlEdit(toml)
    e.add_entry(("x",), {"a": "b"})
    with pytest.raises(ValueError, match="read back"):
        e.save(lambda d: False)
    assert toml.read_text() == MINGUS
    assert not list(toml.parent.glob("*.bak-lab-*"))


def test_backups_of_one_second_do_not_overwrite_each_other(toml):
    for n in range(2):
        e = TomlEdit(toml)
        e.add_entry(("x",), {"n": n})
        e.save(lambda d: True, stamp="20261006-100000")
    assert sorted(p.name for p in toml.parent.glob("*.bak-lab-*")) == [
        "nomad.toml.bak-lab-20261006-100000", "nomad.toml.bak-lab-20261006-100000-2"]


# --- the plans ---------------------------------------------------------------

def test_lab_by_netid_or_group():
    cfg = {"console": {"labs": {"group_pattern": "{netid}$"}}}
    assert labs.lab_group(cfg, "PI1") == "pi1$"
    assert labs.lab_group(cfg, "chemlab$") == "chemlab$"
    assert labs.lab_group({}, "pi1") == "pi1"        # no pattern: as given


def test_add_machine_new_retag_and_already(toml):
    e = TomlEdit(toml)
    assert labs.add_machine(e, "pi1$", "ws9") == ["ws9: a workstation collected here, "
                                                       "tagged pi1$"]
    assert labs.add_machine(e, "pi1$", "labws") == ["labws: tagged pi1$ (was pi2$)"]
    assert labs.add_machine(e, "pi1$", "labws") == []
    ws = e.data["collectors"]["workstation"]["workstations"]
    assert {w["hostname"]: w["department"] for w in ws} == {
        "labws": "pi1$", "nas2": "pi3$", "ws9": "pi1$"}


def test_add_machine_turns_the_collector_on(tmp_path):
    p = tmp_path / "n.toml"
    p.write_text('[collectors.workstation]\ninterval = 300\n')
    e = TomlEdit(p)
    assert "[collectors.workstation] enabled = true" in labs.add_machine(e, "x$", "ws1")
    assert e.data["collectors"]["workstation"] == {
        "enabled": True, "interval": 300, "workstations": [{"hostname": "ws1",
                                                            "department": "x$"}]}
    p.write_text('[collectors.workstation]\nenabled = false\n')
    with pytest.raises(labs.LabError, match="enabled = false"):
        labs.add_machine(TomlEdit(p), "x$", "ws1")


def test_a_nas_moves_out_of_the_workstations(toml):
    e = TomlEdit(toml)
    changes = labs.add_nas(e, "pi3$", "nas2", name="nas2", note="the lab NAS")
    assert changes[0] == "nas2: no longer collected as a workstation (a NAS is storage)"
    d = e.data
    assert [w["hostname"] for w in d["collectors"]["workstation"]["workstations"]] == ["labws"]
    assert d["collectors"]["storage"] == {"enabled": True, "storage_devices": [
        {"hostname": "nas2", "type": "zfs"}]}
    assert d["console"]["labs"]["resources"]["pi3$"] == {"storage": ["nas2"]}
    assert d["console"]["storage"]["nas2"] == {"name": "nas2", "note": "the lab NAS"}
    with pytest.raises(labs.LabError, match="collected here as a NAS"):
        labs.add_machine(e, "pi3$", "nas2")
    assert labs.add_nas(e, "pi3$", "nas2", name="nas2", note="the lab NAS") == []


def test_add_storage_lists_it_and_names_the_server(toml):
    e = TomlEdit(toml)
    assert labs.add_storage(e, "pi1$", "10.0.0.43:/mnt/pool/cold", name="coldstore",
                            note="cold storage") == [
        "pi1$: storage 10.0.0.43:/mnt/pool/cold",
        '10.0.0.43: name "coldstore", note "cold storage"']
    assert e.data["console"]["labs"]["resources"]["pi1$"]["storage"] == [
        "10.0.0.28:/mnt/pool/home", "10.0.0.43:/mnt/pool/cold"]
    with pytest.raises(labs.LabError, match="server:/export"):
        labs.add_storage(e, "pi1$", "srv:relative")


def test_remove(toml):
    e = TomlEdit(toml)
    labs.add_nas(e, "pi2$", "nas9")
    with pytest.raises(labs.LabError, match="tagged pi2"):
        labs.remove(e, "pi1$", "labws")
    assert labs.remove(e, "pi2$", "nas9") == ["nas9: no longer collected as a NAS",
                                                   "pi2$: no longer lists nas9"]
    assert labs.remove(e, "pi2$", "labws") == ["labws: no longer collected as a workstation"]
    assert labs.remove(e, "pi1$", "10.0.0.28") == [
        "pi1$: no longer lists 10.0.0.28:/mnt/pool/home"]
    d = e.data
    assert "resources" not in d["console"]["labs"] or "pi1$" not in d["console"]["labs"]["resources"]


def test_old_top_level_lists(tmp_path):
    # The storage collector still reads a top-level [[storage_devices]] when
    # the nested list is absent: that's the one to add to. The workstation
    # collector never read a top-level [[workstations]].
    p = tmp_path / "n.toml"
    p.write_text('[[storage_devices]]\nhostname = "old"\ntype = "zfs"\n\n'
                 '[[workstations]]\nhostname = "oldws"\n')
    e = TomlEdit(p)
    labs.add_nas(e, "x$", "new")
    assert [s["hostname"] for s in e.data["storage_devices"]] == ["old", "new"]
    labs.add_machine(e, "x$", "ws1")
    assert e.data["collectors"]["workstation"]["workstations"] == [
        {"hostname": "ws1", "department": "x$"}]


def test_a_single_string_list_stays_one_name(tmp_path):
    p = tmp_path / "n.toml"
    p.write_text('[console.labs.resources."g$"]\nworkstations = "adam"\nstorage = "nas1"\n')
    e = TomlEdit(p)
    labs.add_storage(e, "g$", "nas2")
    assert e.data["console"]["labs"]["resources"]["g$"] == {
        "workstations": ["adam"], "storage": ["nas1", "nas2"]}


def test_saving_keeps_permissions_and_symlinks(tmp_path):
    import os
    real = tmp_path / "real.toml"
    real.write_text('[x]\na = 1\n')
    os.chmod(real, 0o600)
    link = tmp_path / "nomad.toml"
    link.symlink_to(real)
    e = TomlEdit(link)
    e.add_entry(("y",), {"b": "c"})
    e.save(lambda d: True)
    assert link.is_symlink() and "[[y]]" in real.read_text()
    assert os.stat(real).st_mode & 0o777 == 0o600


def test_a_nas_two_labs_list_stays_collected(toml):
    e = TomlEdit(toml)
    labs.add_nas(e, "a$", "nas")
    labs.add_storage(e, "b$", "nas:/mnt/pool/b")
    assert labs.remove(e, "a$", "nas") == ["nas: still collected as a NAS (listed for b$ too)",
                                           "a$: no longer lists nas"]
    assert [s["hostname"] for s in e.data["collectors"]["storage"]["storage_devices"]] == ["nas"]


@pytest.mark.parametrize("host, problem", [
    ("root@nas2", "~/.ssh/config: Host nas2 / User root"),
    ("nas2;reboot", "not a host name"),
    ("", "not a host name"),
])
def test_hosts_nomad_can_reach(toml, host, problem):
    with pytest.raises(labs.LabError, match=problem.replace("/", ".").replace("~", ".")):
        labs.add_nas(TomlEdit(toml), "x$", host)


def test_inline_lists_are_refused_plainly(tmp_path):
    p = tmp_path / "n.toml"
    p.write_text('[collectors.workstation]\nenabled = true\nworkstations = []\n')
    with pytest.raises(labs.LabError, match="written inline"):
        labs.add_machine(TomlEdit(p), "x$", "ws1")
    p.write_text('[console.labs.resources]\n"g$" = { storage = ["a"] }\n')
    with pytest.raises(labs.LabError, match="written inline"):
        labs.add_storage(TomlEdit(p), "g$", "b")


def test_a_file_without_a_final_newline(tmp_path):
    p = tmp_path / "n.toml"
    p.write_text('[collectors.workstation]')
    e = TomlEdit(p)
    labs.add_machine(e, "x$", "ws1")
    assert e.data["collectors"]["workstation"]["enabled"] is True


def test_the_next_tables_comments_stay_with_it(toml):
    e = TomlEdit(toml)
    labs.add_machine(e, "x$", "ws9")
    text = e.text
    # "# Lab three" heads the nas2 entry, before which ws9 must not land.
    assert text.index("# Lab three") < text.index('hostname = "nas2"') < text.index('"ws9"')
    toml2 = toml.parent / "m.toml"
    toml2.write_text('[[collectors.workstation.workstations]]\nhostname = "a"\n\n'
                     '# ---- Console ----\n[console.labs]\ngroup_pattern = ""\n')
    e = TomlEdit(toml2)
    e.add_entry(("collectors", "workstation", "workstations"), {"hostname": "b"})
    assert e.text.index('"b"') < e.text.index("# ---- Console ----")


def test_key_path_with_an_escape_json_lacks():
    assert key_path('a."\\U0001F600"') == ()


def test_the_packaged_defaults_are_never_edited(tmp_path, monkeypatch):
    from nomad.config import get_default_config_path
    monkeypatch.setattr(cli_mod, "_lab_reach", lambda host, nas=False: "reachable over ssh")
    r = CliRunner().invoke(cli, ["-c", str(get_default_config_path()), "lab", "add-machine",
                                 "x", "ws1", "--apply"])
    assert r.exit_code != 0 and "packaged defaults" in r.output


def test_a_config_that_does_not_exist_is_not_made_from_a_lab_alone(tmp_path, monkeypatch):
    # A file of only the lab's tables would replace the packaged defaults.
    monkeypatch.setattr(cli_mod, "_lab_reach", lambda host, nas=False: "reachable over ssh")
    p = tmp_path / "new.toml"
    r = CliRunner().invoke(cli, ["-c", str(p), "lab", "add-machine", "x$", "ws1", "--apply"])
    assert r.exit_code != 0 and "doesn't exist; create it first" in r.output
    assert not p.exists()


# --- the commands ------------------------------------------------------------

def test_commands_show_then_write(toml, monkeypatch):
    monkeypatch.setattr(cli_mod, "_lab_reach", lambda host, nas=False: "reachable over ssh")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    run = CliRunner().invoke
    r = run(cli, ["-c", str(toml), "lab", "add-machine", "pi1", "ws9"])
    assert r.exit_code == 0, r.output
    assert "+ ws9: a workstation collected here, tagged pi1$" in r.output
    assert "Nothing changed" in r.output and toml.read_text() == MINGUS
    r = run(cli, ["-c", str(toml), "lab", "add-machine", "pi1", "ws9", "--apply"])
    assert "Written. Backup:" in r.output
    assert any(w["hostname"] == "ws9" for w in
               loads(toml.read_text())["collectors"]["workstation"]["workstations"])
    r = run(cli, ["-c", str(toml), "lab", "add-nas", "pi3", "nas2", "--note",
                  "the lab NAS", "--apply"])
    assert r.exit_code == 0, r.output
    assert "no `nomad collect` in this host's crontab" in r.output
    r = run(cli, ["-c", str(toml), "lab", "show", "pi3"])
    assert "storage: nas2, the lab NAS; a NAS collected here (zfs)" in r.output
    r = run(cli, ["-c", str(toml), "lab", "add-machine", "pi3", "nas2"])
    assert r.exit_code != 0 and "collected here as a NAS" in r.output


def test_add_nas_says_when_cron_leaves_the_storage_collector_out(toml, monkeypatch):
    monkeypatch.setattr(cli_mod, "_lab_reach", lambda host, nas=False: "reachable over ssh")
    cron = "*/5 * * * * flock -n x nomad collect -C workstation --once > log 2>&1\n"
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, cron, ""))
    r = CliRunner().invoke(cli, ["-c", str(toml), "lab", "add-nas", "pi3", "nas2"])
    assert "without the storage collector" in r.output and "-C storage" in r.output


# --- what a PI sees ----------------------------------------------------------

def _hub(path, now_rows=True):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, cluster TEXT)")
    c.execute("INSERT INTO group_membership VALUES ('s1', 'pi3$', 'spydur')")
    c.execute("CREATE TABLE workstation_state (timestamp TEXT, hostname TEXT, department TEXT, "
              "status TEXT)")
    c.executemany("INSERT INTO workstation_state VALUES (?, ?, ?, ?)", [
        ("2026-10-06T09:40:15", "labws", "pi3$", "online"),
        ("2026-10-06T07:00:00", "quiet", "pi3$", "online"),      # stopped reporting
        ("2026-10-01T09:00:00", "gone", "pi3$", "online"),       # long removed
        ("2026-10-06T09:30:00", "nas2", "pi3$", "online"),       # a NAS, moved since
    ])
    c.execute("CREATE TABLE storage_state (timestamp TEXT, hostname TEXT, status TEXT, "
              "total_bytes INTEGER, used_bytes INTEGER, free_bytes INTEGER, pools_json TEXT)")
    pools = json.dumps([{"name": "boot-pool", "health": "ONLINE"},
                        {"name": "tank", "health": "ONLINE"}])
    c.executemany("INSERT INTO storage_state VALUES (?, ?, ?, ?, ?, ?, ?)", [
        ("2026-10-06T09:30:00", "nas2", "online", 40 * TB, 30 * TB, 10 * TB, pools),
        ("2026-10-06T09:40:00", "nas2", "online", 40 * TB, 31 * TB, 9 * TB, pools),
        ("2026-10-06T09:00:00", "nas3", "online", 40 * TB, 1 * TB, 39 * TB, "[]"),
        ("2026-10-06T09:40:00", "nas3", "offline", None, None, None, "[]"),
    ])
    c.commit()
    c.close()


def test_a_pi_sees_the_nas_and_which_machines_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "_now", lambda: datetime(2026, 10, 6, 9, 45))
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n\n'
                   '[console.labs.resources."pi3$"]\nstorage = ["nas2", "nas3"]\n\n'
                   '[console.storage.nas2]\nname = "nas2"\nnote = "the lab NAS"\n')
    db = tmp_path / "combined.db"
    _hub(db)
    r = CliRunner().invoke(cli, ["-c", str(cfg), "console", "roles", "pi3", "--db", str(db)])
    out = r.output
    assert r.exit_code == 0, out
    assert "    labws [tagged]: online, 2026-10-06T09:40" in out
    assert "    quiet [tagged]: no report since 2026-10-06T07:00 (it was online then)" in out
    assert "gone" not in out                     # no record in two days: not the lab's now
    # nas2 was collected as a workstation before it was listed as storage.
    assert "nas2 [tagged]" not in out
    # Usable space, pool health, the boot pool left out.
    assert ("    nas2, the lab NAS: online, 2026-10-06T09:40; 78% used "
            "(31.0 TB of 40.0 TB), 9.0 TB free; pool tank ONLINE") in out
    assert "boot-pool" not in out
    assert "    nas3: offline at 2026-10-06T09:40; last online 2026-10-06T09:00" in out


# --- the storage collector ---------------------------------------------------

def test_usable_space_from_the_pools_root_datasets(monkeypatch, tmp_path):
    from nomad.collectors import storage as st
    from nomad.collectors.workstation import OUTPUT_MARK
    zpool = ("boot-pool\t30000000000\t3000000000\t27000000000\t-\t-\t1%\t10%\t1.00x\tONLINE\t-\n"
             "tank\t60000000000000\t20000000000000\t40000000000000\t-\t-\t3%\t33%\t1.00x\t"
             "ONLINE\t-\n")
    zfs = "boot-pool\t3000000000\t26000000000\ntank\t13000000000000\t26000000000000\n"
    answers = {"true": "", "zpool list -Hp": zpool, "zfs list -Hp -o name,used,avail -d 0": zfs}

    def fake(argv, **k):
        cmd = argv[-1].split("; ", 1)[1]
        banner = "This computer is nas2.\n"           # printed by the login, first
        if cmd in answers:
            return subprocess.CompletedProcess(argv, 0, f"{banner}{OUTPUT_MARK}\n{answers[cmd]}",
                                               "")
        return subprocess.CompletedProcess(argv, 1, banner, "not here")
    monkeypatch.setattr(st.subprocess, "run", fake)
    stats = st.StorageCollector({}, str(tmp_path / "x.db"))._collect_storage("nas2", "zfs")
    # RAIDZ: 60 TB of disks, 39 TB usable; the boot pool isn't storage.
    assert (stats.total_bytes, stats.used_bytes, stats.free_bytes) == (
        39 * TB, 13 * TB, 26 * TB)
    assert [p.name for p in stats.pools] == ["boot-pool", "tank"]


def test_parse_zfs_roots():
    from nomad.collectors.storage import parse_zfs_roots
    assert parse_zfs_roots("tank\t5\t7\ntank/home\t1\t7\nbad\tx\t1\n") == {"tank": (5, 7)}


# --- lab names and shared storage --------------------------------------------

def test_a_lab_has_a_name(toml):
    from nomad.config.access import access_from
    e = TomlEdit(toml)
    assert labs.set_name(e, "pi1$", "Chem  Lab") == ['pi1$: shown as "Chem Lab"']
    # The lab's lists stay; adding to them keeps the name.
    labs.add_storage(e, "pi1$", "srv:/x")
    t = e.data["console"]["labs"]["resources"]["pi1$"]
    assert t == {"name": "Chem Lab", "storage": ["10.0.0.28:/mnt/pool/home", "srv:/x"]}
    a = access_from(e.data, log=False)
    assert a.lab_label("pi1$") == "Chem Lab (pi1$)" and a.lab_label("pi9$") == "pi9$"
    assert labs.set_name(e, "pi1$", "Chem Lab") == []
    assert labs.set_name(e, "pi1$", "") == ["pi1$: shown by its group"]
    assert "name" not in e.data["console"]["labs"]["resources"]["pi1$"]
    with pytest.raises(labs.LabError, match="not a lab"):
        labs.set_name(e, "shared", "Everyone")


def test_a_wrong_lab_name_is_said_and_left_out():
    from nomad.config.access import access_from
    a = access_from({"console": {"labs": {"resources": {"g$": {"name": 3}}}}}, log=False)
    assert a.lab_names == {} and any("name should be text" in p for p in a.problems)


def test_a_shared_nas_is_collected_named_and_listed_for_no_lab(toml):
    e = TomlEdit(toml)
    assert labs.lab_group(e.data, "Shared") == "shared"
    changes = labs.add_nas(e, "shared", "bignas", note="community $HOME, all users")
    assert changes[-1] == 'bignas: note "community $HOME, all users"'
    d = e.data
    assert {"hostname": "bignas", "type": "zfs", "shared": True} in \
        d["collectors"]["storage"]["storage_devices"]
    assert "bignas" not in str(d["console"]["labs"]["resources"])
    assert "shared NAS collected here (no lab): bignas, community $HOME, all users" in \
        labs.summary(d)
    with pytest.raises(labs.LabError, match="add-nas shared"):
        labs.add_storage(e, "shared", "bignas:/x")
    # Listed for a lab too, it can't simply be dropped as shared.
    labs.add_storage(e, "pi1$", "bignas")
    with pytest.raises(labs.LabError, match="listed for pi1"):
        labs.remove(e, "shared", "bignas")
    assert labs.remove(e, "pi1$", "bignas") == ["bignas: still collected as a NAS (shared)",
                                                "pi1$: no longer lists bignas"]
    # Added for a lab again later, it stays everyone's.
    labs.add_nas(e, "pi1$", "bignas")
    assert {"hostname": "bignas", "type": "zfs", "shared": True} in \
        e.data["collectors"]["storage"]["storage_devices"]
    labs.remove(e, "pi1$", "bignas")
    assert labs.remove(e, "shared", "bignas") == ["bignas: no longer collected as a NAS"]


def test_name_and_shared_commands(toml, monkeypatch):
    monkeypatch.setattr(cli_mod, "_lab_reach", lambda host, nas=False: "reachable over ssh")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    run = CliRunner().invoke
    r = run(cli, ["-c", str(toml), "lab", "name", "pi1", "Chem Lab", "--apply"])
    assert r.exit_code == 0 and '+ pi1$: shown as "Chem Lab"' in r.output, r.output
    r = run(cli, ["-c", str(toml), "lab", "add-nas", "shared", "bignas", "--name", "big",
                  "--note", "everyone's homes", "--apply"])
    assert r.exit_code == 0, r.output
    r = run(cli, ["-c", str(toml), "lab", "show"])
    assert "Chem Lab (pi1$):" in r.output
    assert "shared NAS collected here (no lab): bignas, everyone's homes" in r.output
    r = run(cli, ["-c", str(toml), "lab", "remove", "shared", "bignas", "--apply"])
    assert r.exit_code == 0 and "no longer collected as a NAS" in r.output, r.output


def test_roles_show_the_labs_name(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_mod, "_now", lambda: datetime(2026, 10, 6, 9, 45))
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n\n'
                   '[console.labs.resources."pi3$"]\nname = "Chem Lab"\nstorage = ["nas2"]\n')
    db = tmp_path / "combined.db"
    _hub(db)
    r = CliRunner().invoke(cli, ["-c", str(cfg), "console", "roles", "pi3", "--db", str(db)])
    assert "    Chem Lab (pi3$): 1 members" in r.output
    r = CliRunner().invoke(cli, ["-c", str(cfg), "console", "roles", "pi3", "--db", str(db),
                                 "--mask"])
    assert "Chem Lab" not in r.output and "lab #1: 1 members" in r.output


def test_a_nas_read_by_name_is_the_server_its_mounts_name_by_address(tmp_path, monkeypatch):
    # The lab's machines mount 10.0.0.43:/pool/cold; the storage collector
    # reads the same box as "coldnas", the name [console.storage] gives it.
    monkeypatch.setattr(cli_mod, "_now", lambda: datetime(2026, 10, 6, 9, 45))
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n\n'
                   '[console.labs.resources."pi3$"]\nstorage = ["10.0.0.43:/pool/cold"]\n\n'
                   '[console.storage."10.0.0.43"]\nname = "coldnas"\nnote = "cold storage"\n')
    db = tmp_path / "combined.db"
    _hub(db)
    c = sqlite3.connect(db)
    c.execute("INSERT INTO storage_state VALUES ('2026-10-06T09:40:00', 'coldnas', 'online', "
              "?, ?, ?, ?)", (176 * TB, 60 * TB, 116 * TB,
                              json.dumps([{"name": "everything", "health": "ONLINE"}])))
    c.commit()
    c.close()
    r = CliRunner().invoke(cli, ["-c", str(cfg), "console", "roles", "pi3", "--db", str(db)])
    out = r.output
    assert ("    coldnas (10.0.0.43), cold storage: online, 2026-10-06T09:40; 35% used "
            "(60.0 TB of 176 TB), 116 TB free; pool everything ONLINE\n"
            "      /pool/cold: not in the data") in out
    assert out.split("\npi3:\n")[1].count("coldnas") == 1      # once in the lab's view


# --- nomad lab lead -------------------------------------------------------------

HUB = '''[general]
x = 1

[console.labs]
group_pattern = "{netid}$"
leads = {}                     # e.g. { NETID = ["chemlab$"] }

[console.labs.resources."pi1$"]
name = "Smith Lab"
workstations = ["labws1"]
'''


def lead(path, *args):
    r = CliRunner().invoke(cli, ["-c", str(path), "lab", "lead", *args])
    return r, r.output


def test_a_lead_replaces_the_empty_inline_line_with_a_table(tmp_path):
    """nomad init writes `leads = {}`; a second `leads` beside it once made the
    whole file unreadable. The command turns it into [console.labs.leads]."""
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    r, out = lead(p, "pi1", "jdoe")
    assert r.exit_code == 0 and "+ jdoe leads pi1$" in out and "Smith Lab (pi1$)" in out
    assert p.read_text() == HUB                              # shown, not written
    r, out = lead(p, "pi1", "JDoe", "--apply")
    assert r.exit_code == 0 and "Written." in out
    text = p.read_text()
    assert "leads = {}" not in text
    assert '[console.labs]\ngroup_pattern = "{netid}$"\n\n[console.labs.leads]\njdoe = ["pi1$"]\n' in text
    assert labs.leads_of(loads(text)) == {"jdoe": ["pi1$"]}
    from nomad.config.access import access_from
    assert "pi1$" in access_from(loads(text), log=False).lab_groups("jdoe")
    # Another lab for the same person, and another person: the table grows in place.
    lead(p, "pi2$", "jdoe", "--apply", "--new-group")
    lead(p, "pi1", "asmith", "--apply")
    assert labs.leads_of(loads(p.read_text())) == {"jdoe": ["pi1$", "pi2$"], "asmith": ["pi1$"]}
    assert p.read_text().count("[console.labs.leads]") == 1
    out = CliRunner().invoke(cli, ["-c", str(p), "lab", "show", "pi1"]).output
    assert "also led by: asmith, jdoe" in out


def test_an_inline_table_with_leads_is_merged(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB.replace('leads = {}                     # e.g. { NETID = ["chemlab$"] }',
                             'leads = { asmith = ["pi3$"] }'))
    lead(p, "pi1", "jdoe", "--apply")
    d = loads(p.read_text())
    assert labs.leads_of(d) == {"asmith": ["pi3$"], "jdoe": ["pi1$"]}
    assert "leads = {" not in p.read_text()


def test_remove_and_nothing_to_change(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    lead(p, "pi1", "jdoe", "--apply")
    r, out = lead(p, "pi1", "jdoe")
    assert "Nothing to change" in out
    r, out = lead(p, "pi1", "pi1")                       # the PI by group_pattern
    assert "through group_pattern" in out and r.exit_code == 0
    r, out = lead(p, "pi1", "pi1", "--remove")
    assert "through group_pattern, not leads" in out
    r, out = lead(p, "pi1", "jdoe", "--remove", "--apply")
    assert "+ jdoe no longer leads pi1$" in out
    assert "[console.labs.leads]" not in p.read_text()
    assert labs.leads_of(loads(p.read_text())) == {}
    r, out = lead(p, "pi1", "jdoe", "--remove")
    assert "Nothing to change" in out


def test_a_new_lead_without_a_labs_table_and_with_none_at_all(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text('[general]\nx = 1\n\n[console.labs.resources."pi1$"]\nname = "Smith Lab"\n')
    lead(p, "pi1$", "jdoe", "--apply")
    assert labs.leads_of(loads(p.read_text())) == {"jdoe": ["pi1$"]}


@pytest.mark.parametrize("text", [
    '[console.labs]\ngroup_pattern = "{netid}$"\nleads.asmith = ["pi3$"]\n',
    '[console]\nlabs = { group_pattern = "{netid}$" }\n',
    'console.labs.leads.asmith = ["pi3$"]\n[general]\nx = 1\n',
    '[console.labs]\nleads = { asmith = [\n  "pi3$",\n] }\n',
])
def test_other_ways_of_writing_it_are_left_to_a_person(tmp_path, text):
    p = tmp_path / "nomad.toml"
    p.write_text(text)
    r, out = lead(p, "pi1$", "jdoe", "--apply", "--new-group")
    assert r.exit_code != 0 and "by hand" in out
    assert p.read_text() == text


@pytest.mark.parametrize("netid", ["jdoe@example.edu", "j doe", "-rf", ""])
def test_only_netids(tmp_path, netid):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    r, out = lead(p, "pi1", netid, "--apply")
    assert r.exit_code != 0 and p.read_text() == HUB


def test_shared_is_not_a_lab(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    r, out = lead(p, "shared", "jdoe")
    assert r.exit_code != 0 and '"shared" is not a lab' in out


def test_a_lead_who_is_staff_is_told(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text('[console.roles]\noperator = ["jdoe"]\n\n' + HUB)
    r, out = lead(p, "pi1", "jdoe")
    assert "jdoe is an operator here" in out


def test_nothing_but_leads_may_change(tmp_path):
    """A line that only looks like `leads = {}` -- inside a multi-line string
    under [console.labs] -- is not removed into a changed file."""
    p = tmp_path / "nomad.toml"
    text = ('[console.labs]\ngroup_pattern = "{netid}$"\nnote = """\nleads = {}\n"""\n'
            '[console.labs.resources."pi1$"]\nname = "Smith Lab"\n')
    p.write_text(text)
    r, out = lead(p, "pi1", "jdoe", "--apply")
    assert r.exit_code != 0 and p.read_text() == text


# --- what the review found ------------------------------------------------------

def test_a_lab_nomad_does_not_know_is_refused(tmp_path):
    """LAB=smith with group_pattern is smith$: if another PI's NetID is smith,
    a typo would show their lab. Only known labs, or --new-group."""
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    r, out = lead(p, "smith", "jdoe", "--apply")
    assert r.exit_code != 0 and "smith$ is not a lab nomad knows here" in out
    assert p.read_text() == HUB
    r, out = lead(p, "smith", "jdoe", "--apply", "--new-group")
    assert r.exit_code == 0 and labs.leads_of(loads(p.read_text())) == {"jdoe": ["smith$"]}


def test_a_lab_known_from_the_groups_data(tmp_path):
    db = tmp_path / "nomad.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT)")
    c.execute("INSERT INTO group_membership VALUES ('student1', 'pi5$')")
    c.commit()
    c.close()
    p = tmp_path / "nomad.toml"
    p.write_text(f'[database]\npath = "{db}"\n\n' + HUB)
    r, out = lead(p, "pi5", "jdoe", "--apply")
    assert r.exit_code == 0, out
    assert labs.leads_of(loads(p.read_text())) == {"jdoe": ["pi5$"]}


def test_a_lab_by_its_name_and_names_that_are_no_lab(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    r, out = lead(p, "smith  lab", "jdoe", "--apply")
    assert r.exit_code == 0 and labs.leads_of(loads(p.read_text())) == {"jdoe": ["pi1$"]}
    for bad in ("", "a lab", "$"):
        r, out = lead(p, bad, "asmith", "--apply", "--new-group")
        assert r.exit_code != 0 and "not a lab here" in out, bad


def test_files_with_nothing_else_under_console(tmp_path):
    """Comparing what else changed once refused these: console.labs = {} is
    no change from no [console] at all."""
    for text in ('[general]\nx = 1\n', '[console.roles]\nadmin = ["root1"]\n'):
        p = tmp_path / "nomad.toml"
        p.write_text(text)
        r, out = lead(p, "pi1$", "jdoe", "--apply", "--new-group")
        assert r.exit_code == 0, out
        r, out = lead(p, "pi1$", "jdoe", "--apply", "--remove")
        assert r.exit_code == 0 and "no longer leads" in out, out
        assert labs.leads_of(loads(p.read_text())) == {}


def test_a_removed_lead_who_still_leads_is_told(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB.replace("leads = {}                     # e.g. { NETID = [\"chemlab$\"] }",
                             'leads = { pi1 = ["pi1$"] }'))
    r, out = lead(p, "pi1", "pi1", "--remove", "--apply")
    assert "pi1 still leads pi1$ through group_pattern" in out


def test_the_examples_comment_stays_with_its_line(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text('[console.labs]\ngroup_pattern = ""           # e.g. "{netid}$" where a group\n'
                 '                             # is their NetID followed by $\n'
                 'leads = {}\n\n# The lab workstations\n[console.labs.resources."pi1$"]\n'
                 'name = "Smith Lab"\n')
    lead(p, "pi1$", "jdoe", "--apply")
    text = p.read_text()
    assert ("# is their NetID followed by $\n\n[console.labs.leads]\njdoe = [\"pi1$\"]\n\n"
            "# The lab workstations\n[console.labs.resources") in text, text


@pytest.mark.skipif(not hasattr(__import__("os"), "geteuid") or __import__("os").geteuid() != 0,
                    reason="only root can give a file away")
def test_written_as_root_the_file_keeps_its_owner(tmp_path):
    import os
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    os.chown(p, 1000, 1000)
    os.chmod(p, 0o640)
    lead(p, "pi1", "jdoe", "--apply")
    st = p.stat()
    assert (st.st_uid, st.st_gid, st.st_mode & 0o777) == (1000, 1000, 0o640)


def test_a_file_that_cannot_be_written_is_said_plainly(tmp_path, monkeypatch):
    import shutil
    p = tmp_path / "nomad.toml"
    p.write_text(HUB)
    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(shutil, "copy2", denied)
    r, out = lead(p, "pi1", "jdoe", "--apply")
    assert r.exit_code != 0 and "can't write" in out and "nothing changed" in out
    assert p.read_text() == HUB


def test_a_name_that_could_mean_two_labs_is_refused(tmp_path):
    p = tmp_path / "nomad.toml"
    p.write_text(HUB + '\n[console.labs.resources."pi9$"]\nname = "Smith Lab"\n'
                 'workstations = ["labws9"]\n')
    r, out = lead(p, "Smith Lab", "jdoe", "--apply")
    assert r.exit_code != 0 and "several labs are named" in out and "pi1$, pi9$" in out
    # A lab named like a PI whose own group nomad knows.
    p.write_text(HUB + '\n[console.labs.resources."chem$"]\nname = "Pi1"\n'
                 'workstations = ["chemws"]\n')
    r, out = lead(p, "pi1", "jdoe", "--apply")
    assert r.exit_code != 0 and "could be the lab named so (chem$) or pi1$" in out
    assert labs.leads_of(loads(p.read_text())) == {}


def test_odd_databases_and_settings_do_not_crash_the_check(tmp_path):
    weird = tmp_path / "a?b#c%d"
    weird.mkdir()
    db = weird / "nomad.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT)")
    c.execute("INSERT INTO group_membership VALUES ('student1', 'pi7$')")
    c.commit()
    c.close()
    p = tmp_path / "nomad.toml"
    p.write_text(f'[database]\npath = "{db}"\n\n' + HUB)
    r, out = lead(p, "pi7", "jdoe", "--apply")
    assert r.exit_code == 0, out                      # found despite ? # % in the path
    p.write_text(HUB.replace("[general]\nx = 1\n", "[general]\ndata_dir = 5\n"))
    r, out = lead(p, "pi8", "jdoe")
    assert r.exit_code != 0 and "not a lab nomad knows" in out   # said, not a traceback

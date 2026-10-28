"""1.17.9: a lab's condo nodes, as report.toml's condo reports name them."""
import sqlite3
from datetime import datetime, timedelta

from nomad.condo import config as ccfg
from nomad.facts.condos import lab_condos, owns

NOW = datetime(2026, 10, 27, 21, 0, 0)
P = "{netid}$"


def _db(tmp_path, when=NOW):
    db = tmp_path / "n.db"
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE node_state (node_name TEXT, state TEXT, timestamp TEXT, cpus_total INTEGER, "
              "cpus_alloc INTEGER, memory_total_mb REAL, gres TEXT, partitions TEXT, source_site TEXT)")
    t = (when - timedelta(minutes=10)).isoformat()
    old = (when - timedelta(hours=5)).isoformat()
    rows = [("c01", "MIXED", t, 64, 32, 256000, "", "smith,condos", "hpc1"),
            ("c02", "IDLE", t, 64, 0, 256000, "gpu:a100:4", "smith,condos", "hpc1"),
            ("c03", "DRAINED", t, 64, 0, 256000, "", "smith,condos", "hpc1"),
            ("c01", "IDLE", old, 64, 0, 256000, "", "smith,condos", "hpc1"),          # older reading
            ("d01", "ALLOCATED", t, 32, 32, 128000, "", "jones,condos", "hpc1"),
            ("c01", "DOWN", t, 8, 0, 1000, "", "smith", "other")]                      # another site
    c.executemany("INSERT INTO node_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    c.commit()
    c.close()
    return sqlite3.connect(db)


def _settings(**condo):
    return ccfg.from_dict({"report": {"clusters": {"hpc1": {"condos": {
        "smith": {"owner": "jsmith", **condo},
        "jones": {"owner": "rjones", "confirmed": False}}}}}})


def test_a_labs_condo_and_its_nodes_now(tmp_path):
    conn = _db(tmp_path)
    got, = lab_condos(conn, "jsmith$", {"jsmith"}, _settings(), now=NOW, pattern=P)
    assert got["cluster"] == "hpc1" and got["partition"] == "smith" and got["owners"] == ["jsmith"]
    assert [n["node"] for n in got["nodes"]] == ["c01", "c02", "c03"]
    t = got["total"]
    assert (t["nodes"], t["cores"], t["gpus"], t["up"], t["drained"], t["down"]) == (3, 192, 4, 2, 1, 0)
    assert (t["cores_allocated"], t["cores_up"], t["cores_allocated_pct"]) == (32, 128, 25.0)
    assert got["stale"] is False
    # the other site's node of the same name is not this one
    assert got["nodes"][0]["condition"] == "up"


def test_only_confirmed_condos_the_lab_owns(tmp_path):
    conn = _db(tmp_path)
    assert lab_condos(conn, "rjones$", {"rjones"}, _settings(), now=NOW, pattern=P) == []     # a draft
    assert lab_condos(conn, "other$", {"someone"}, _settings(), now=NOW, pattern=P) == []
    # by the condo's members: the lab's group, and that group only
    got = lab_condos(conn, "smith$", set(), _settings(members="group:smith$"), now=NOW, pattern=P)
    assert [g["condo"] for g in got] == ["smith"]
    assert lab_condos(conn, "jsmith$", {"jsmith"}, _settings(members="group:smith$"), now=NOW, pattern=P) == []
    condo = _settings().clusters["hpc1"].condos["smith"]
    assert owns(condo, "jsmith$", {"JSmith"}, P)                                       # any case


def test_an_owner_with_several_labs(tmp_path):
    condo = _settings().clusters["hpc1"].condos["smith"]
    # their own group's lab, not another they lead too
    assert owns(condo, "jsmith$", {"jsmith"}, P, lambda n: ["jsmith$", "chemcore"])
    assert not owns(condo, "chemcore", {"jsmith", "bwho"}, P, lambda n: ["jsmith$", "chemcore"])
    # no group_pattern: the one lab they lead, else none (report.toml's members say which)
    assert owns(condo, "smithlab", {"jsmith"}, None, lambda n: ["smithlab"])
    assert not owns(condo, "smithlab", {"jsmith"}, None, lambda n: ["smithlab", "chemcore"])


def test_listed_nodes_and_old_readings(tmp_path):
    conn = _db(tmp_path)
    got, = lab_condos(conn, "jsmith$", {"jsmith"}, _settings(nodes="c[01-02],c09"), now=NOW, pattern=P)
    assert [n["node"] for n in got["nodes"]] == ["c01", "c02", "c09"]
    assert got["total"]["not_read"] == 1
    later = NOW + timedelta(hours=3)
    got, = lab_condos(conn, "jsmith$", {"jsmith"}, _settings(), now=later, pattern=P)
    assert got["stale"] is True and got["total"]["cores_allocated_pct"] is None
    assert got["total"]["up"] == 2                          # as last reported
    # quiet for days: still as last reported
    got, = lab_condos(conn, "jsmith$", {"jsmith"}, _settings(), now=NOW + timedelta(days=5), pattern=P)
    assert got["stale"] is True and got["total"]["cores"] == 192 and got["total"]["not_read"] == 0


def test_a_node_slurm_no_longer_lists(tmp_path):
    conn = _db(tmp_path)
    t = (NOW - timedelta(hours=30)).isoformat()
    conn.execute("INSERT INTO node_state VALUES ('c04', 'ALLOCATED', ?, 64, 64, 256000, '', 'smith,condos', 'hpc1')", (t,))
    conn.commit()
    got, = lab_condos(conn, "jsmith$", {"jsmith"}, _settings(nodes="c[01-02],c04"), now=NOW, pattern=P)
    gone = next(n for n in got["nodes"] if n["node"] == "c04")
    assert gone["condition"] == "gone" and got["total"]["gone"] == 1
    assert (got["total"]["up"], got["total"]["cores_allocated_pct"]) == (2, 25.0)      # not its 64


def test_no_node_matched_and_this_cluster_on_a_hub(tmp_path):
    conn = _db(tmp_path)
    got, = lab_condos(conn, "jsmith$", {"jsmith"}, _settings(partition="typo"), now=NOW, pattern=P)
    assert got["total"]["nodes"] == 0 and got["stale"] is False                 # the cluster reports
    local = ccfg.from_dict({"report": {"clusters": {"this-cluster": {"condos": {
        "smith": {"owner": "jsmith", "nodes": "c[01-02]"}}}}}})
    got, = lab_condos(conn, "jsmith$", {"jsmith"}, local, now=NOW, pattern=P)
    assert got["total"]["not_read"] == 2 and got["total"]["up"] == 0             # no readings on a hub


def test_facts_lab_says_the_labs_condo(tmp_path):
    from click.testing import CliRunner
    from nomad.cli import cli
    now = datetime.now()
    conn = _db(tmp_path, when=now)
    conn.execute("CREATE TABLE group_membership (username TEXT, group_name TEXT, source_site TEXT, "
                 "collected_at TEXT, has_account INTEGER)")
    conn.executemany("INSERT INTO group_membership VALUES (?, 'jsmith$', 'hpc1', ?, 1)",
                     [("jsmith", now.isoformat()), ("st1", now.isoformat())])
    conn.commit()
    conn.close()
    toml = tmp_path / "nomad.toml"
    toml.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n')
    (tmp_path / "report.toml").write_text(
        '[report.clusters.hpc1.condos.smith]\nowner = "jsmith"\nlabel = "Smith condo"\n')
    r = CliRunner().invoke(cli, ["-c", str(toml), "facts", "lab", "jsmith", "--db", str(tmp_path / "n.db")])
    assert r.exit_code == 0, r.output
    assert ("condo Smith condo on hpc1 (smith): 3 nodes, 192 cores, 4 GPUs; 2 up, 1 drained; 32 of the "
            "128 cores on nodes up allocated to jobs now (guests' included)") in r.output
    masked = CliRunner().invoke(cli, ["-c", str(toml), "facts", "lab", "jsmith", "--db", str(tmp_path / "n.db"),
                                      "--mask", "--json"])
    assert "smith" not in masked.output.lower().replace("jsmith", "") and "condo #1" in masked.output
    (tmp_path / "report.toml").write_text('[report.clusters.hpc1.condos.smith]\nowner = "jsmith"\nconfirmd = 1\n')
    masked = CliRunner().invoke(cli, ["-c", str(toml), "facts", "lab", "jsmith", "--db", str(tmp_path / "n.db"),
                                      "--mask", "--json"])
    assert "smith" not in masked.output.lower().replace("jsmith", "") and "report.toml doesn't read" in masked.output

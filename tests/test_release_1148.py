"""nomad 1.14.8: a lab's members by NetID, for those allowed to see them
(the Console: staff, and the lab's own PI)."""
import json

from nomad import facts
from tests.test_facts import CONFIG, NOW, hub  # noqa: F401  (hub is a fixture)


def test_each_member_by_netid_when_names_are_allowed(hub):  # noqa: F811
    lab = facts.labs(hub, CONFIG, "alice", now=NOW, names=True)["labs"][0]
    each = {m["user"]: m for m in lab["each"]}
    assert [m["user"] for m in lab["each"]] == ["bob", "alice"]          # most core-hours first
    assert each["bob"]["finished"] == 2 and each["bob"]["core_hours"] == 20.0
    assert each["bob"]["gpu_hours"] == 2.0 and each["bob"]["running"] == 1 and each["bob"]["pending"] == 0
    assert each["alice"]["finished"] == 3 and each["alice"]["core_hours"] == 8.7
    assert each["alice"]["running"] == 1 and each["alice"]["pending"] == 1
    # The lab's totals are still there, and nothing is held back: the rows
    # show each member anyway.
    assert lab["jobs"]["finished"] == 5 and lab["members"]["ran_jobs"] == 2
    lab = facts.labs(hub, CONFIG, "alice", now=NOW, names=True, min_others=3)["labs"][0]
    assert lab["jobs"]["finished"] == 5 and len(lab["each"]) == 2


def test_no_member_rows_without_names(hub):  # noqa: F811
    data = facts.labs(hub, CONFIG, "alice", now=NOW)
    assert "each" not in data["labs"][0]
    assert '"bob"' not in json.dumps(data)


def test_staff_asking_for_a_lab_by_group(hub):  # noqa: F811
    lab = facts.labs(hub, CONFIG, groups=["alice$"], now=NOW, names=True)["labs"][0]
    assert {m["user"] for m in lab["each"]} == {"alice", "bob"}


def test_the_lab_command_lists_each_member(tmp_path):
    from click.testing import CliRunner

    from nomad.cli import cli
    from tests.test_facts import build_hub
    path = tmp_path / "combined.db"
    build_hub(path)
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n'
                   '[console.labs.resources."alice$"]\nname = "Alice Lab"\nstorage = ["nas1"]\n')
    r = CliRunner().invoke(cli, ["-c", str(cfg), "facts", "lab", "alice", "--db", str(path), "--each"])
    assert r.exit_code == 0, r.output
    assert "bob" in r.output and "core-hours held" in r.output
    r = CliRunner().invoke(cli, ["-c", str(cfg), "facts", "lab", "alice", "--db", str(path), "--each", "--mask"])
    assert r.exit_code == 0 and "bob" not in r.output


# ── workstations by lab (review of 1.14.8) ──────────────────────────────

def _ws_db(path, readings):
    """readings: (site, host, minutes before NOW, cores, people's CPU seconds so far)."""
    import sqlite3
    from datetime import datetime, timedelta
    now = datetime(2026, 10, 19, 23, 0)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE workstation_state (timestamp TEXT, hostname TEXT, cpu_count INTEGER, "
              "memory_total_mb INTEGER, memory_used_mb INTEGER, source_site TEXT)")
    c.execute("CREATE TABLE workstation_user_snapshot (timestamp TEXT, hostname TEXT, uid INTEGER, "
              "cpu_usage_usec INTEGER, source_site TEXT)")
    for site, host, ago, cores, cpu_s in readings:
        t = (now - timedelta(minutes=ago)).isoformat()
        c.execute("INSERT INTO workstation_state VALUES (?, ?, ?, 32768, 2048, ?)", (t, host, cores, site))
        if cpu_s is not None:
            c.execute("INSERT INTO workstation_user_snapshot VALUES (?, ?, 1001, ?, ?)",
                      (t, host, int(cpu_s * 1e6), site))
    c.commit()
    return c, now


def test_a_machine_two_sites_read_counts_once(tmp_path):
    from nomad.facts.busy import busy
    # Both sites read adam at the same times; one person keeps 6 of 8 CPUs busy.
    rows = [(s, "adam", 5 * i, 8, (60 - i) * 300 * 6) for i in range(60) for s in ("s1", "s2")]
    c, now = _ws_db(tmp_path / "two.db", rows)
    config = {"console": {"labs": {"resources": {"smith$": {"workstations": ["adam"]}}}}}
    (w,) = busy(c, days=1, now=now, config=config)["workstations"]
    assert w["machines"] == 1 and w["cores"] == 8
    assert w["busy_cores"]["peak"]["value"] == 6.0 and w["machines_busy"]["peak"]["value"] == 1
    assert sorted(w["sites"]) == ["s1", "s2"]


def test_a_machine_whose_collection_moved_keeps_all_its_readings(tmp_path):
    from nomad.facts.busy import busy
    # 4 of 8 CPUs busy all along; read by s1, then by s2 from halfway.
    rows = [("s1" if i >= 30 else "s2", "adam", 5 * i, 8, (60 - i) * 300 * 4) for i in range(60)]
    c, now = _ws_db(tmp_path / "moved.db", rows)
    config = {"console": {"labs": {"resources": {"smith$": {"workstations": ["adam"]}}}}}
    (w,) = busy(c, days=1, now=now, config=config)["workstations"]
    # 50%, but for the ten minutes where the reading moved (each site's
    # counter starts its own run): nothing like the 25% of one site alone.
    assert 47 <= w["busy_cores"]["mean_pct"] <= 51 and w["each"][0]["cpu_measured"]


def test_a_machine_two_labs_list_is_in_both(tmp_path):
    from nomad.facts.busy import busy
    rows = [("s1", "adam", 5 * i, 8, (60 - i) * 300) for i in range(30)]
    c, now = _ws_db(tmp_path / "shared.db", rows)
    config = {"console": {"labs": {"resources": {"smith$": {"workstations": ["adam"]},
                                                 "jones$": {"workstations": ["adam"]}}}}}
    labs = sorted(w["lab"] for w in busy(c, days=1, now=now, config=config)["workstations"])
    assert labs == ["jones$", "smith$"]


def test_storage_is_matched_whatever_the_domain_or_case(tmp_path):
    from nomad.facts.busy import busy
    rows = [("s1", h, 5 * i, n, None) for i in range(30) for h, n in (("NAS1.example.edu", 4), ("ws1", 8))]
    c, now = _ws_db(tmp_path / "nas.db", rows)
    config = {"console": {"storage": {"10.0.0.9": {"name": "nas1"}}}}
    (w,) = busy(c, days=1, now=now, config=config)["workstations"]
    assert [m["name"] for m in w["each"]] == ["ws1"]


def test_without_names_a_lab_says_no_site(tmp_path):
    from nomad.facts.busy import busy
    rows = [("parishhead", "adam", 5 * i, 8, (60 - i) * 300) for i in range(30)]
    c, now = _ws_db(tmp_path / "site.db", rows)
    config = {"console": {"labs": {"resources": {"smith$": {"name": "Smith Lab", "workstations": ["adam"]}}}}}
    (w,) = busy(c, days=1, now=now, config=config, names=False)["workstations"]
    assert w["label"] == "lab 1" and w["site"] is None and w["sites"] == []
    assert "parishhead" not in json.dumps(w) and "Smith" not in json.dumps(w)

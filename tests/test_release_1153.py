"""nomad 1.15.3: a workstation nobody used reads 0 people's CPUs, not "not
measured"."""
from tests.test_release_1148 import _ws_db


def test_a_machine_only_its_own_accounts_used_reads_zero(tmp_path):
    from nomad.facts.busy import busy
    rows = [("s1", "idle-ws", 5 * i, 8, (60 - i) * 300) for i in range(30)]
    c, now = _ws_db(tmp_path / "idle.db", rows)
    # Only the collector's root login and the login screen were read.
    c.execute("UPDATE workstation_user_snapshot SET uid = 0")
    c.execute("INSERT INTO workstation_user_snapshot SELECT timestamp, hostname, 42, 1000, source_site "
              "FROM workstation_user_snapshot")
    c.commit()
    (w,) = busy(c, days=1, now=now)["workstations"]
    m = w["each"][0]
    assert m["cpu_measured"] is True and w["cpu_measured"] is True
    assert w["busy_cores"]["peak"]["value"] == 0 and w["busy_cores"]["mean_pct"] == 0


def test_a_machine_whose_probe_never_ran_stays_not_measured(tmp_path):
    from nomad.facts.busy import busy
    rows = [("s1", "dark-ws", 5 * i, 8, None) for i in range(30)]
    c, now = _ws_db(tmp_path / "dark.db", rows)
    (w,) = busy(c, days=1, now=now)["workstations"]
    assert w["each"][0]["cpu_measured"] is False and w["busy_cores"] is None


def test_people_and_system_accounts_on_one_machine(tmp_path):
    from nomad.facts.busy import busy
    rows = [("s1", "ws", 5 * i, 8, (60 - i) * 300 * 2) for i in range(30)]
    c, now = _ws_db(tmp_path / "mixed.db", rows)
    c.execute("INSERT INTO workstation_user_snapshot SELECT timestamp, hostname, 0, cpu_usage_usec * 3, "
              "source_site FROM workstation_user_snapshot")
    c.commit()
    (w,) = busy(c, days=1, now=now)["workstations"]
    assert w["busy_cores"]["peak"]["value"] == 2.0      # the person's 2, not root's 6

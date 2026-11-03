"""1.25.5: Slurm's estimate read as spydur's sbatch (Slurm 25.11) says it."""
import subprocess

from tests.test_release_1182 import scratch  # noqa: F401  (fixture)


def test_the_estimate_with_a_word_after_the_time(scratch, monkeypatch):  # noqa: F811
    # spydur, 3 Nov 2026: every row came back as an error, Slurm's whole line in its place
    from datetime import datetime
    import nomad.submit.cli as sc
    tmp, w, sent, slurm = scratch
    pl = sc.plan("job.sh")
    monkeypatch.setattr(sc, "_node_states", lambda names: {})
    said = {"yang2": "sbatch: Job 87237 to start at 2026-11-03T09:49:39 a using 1 processors on nodes spdr60 in "
                     "partition yang2",
            "basic": "sbatch: Job 87240 to start at 2026-11-03T11:00:00 a using 1 processors on nodes spdr01 in "
                     "partition basic"}

    def run(argv, cwd, data=None):
        part = next(a.split("=", 1)[1] for a in argv if a.startswith("--partition="))
        return subprocess.CompletedProcess(argv, 0, "", said[part] + "\n")
    doc = sc.start_times(pl, [], "yang2,basic", run=run, now=datetime(2026, 11, 3, 9, 49, 30))
    by = {s["partition"]: s for s in doc["starts"]}
    assert by["yang2"] == {"partition": "yang2", "start": "2026-11-03T09:49:39", "soon": True, "nodes": "spdr60",
                           "error": None}
    assert by["basic"]["start"] == "2026-11-03T11:00:00" and not by["basic"]["soon"] and by["basic"]["error"] is None
    # another partition than the one asked is still caught, the extra word or not
    said["basic"] = said["basic"].replace("partition basic", "partition medium")
    doc = sc.start_times(pl, [], "basic", run=run, now=datetime(2026, 11, 3, 9, 49, 30))
    assert doc["starts"][0]["error"] == "Slurm would run it in partition medium, not basic"


def test_without_members_a_condo_is_only_its_owners_own():
    # 1.25.6: spydur's myang$ holds some 170 people (staff, other PIs, a demo account): all of them saw
    # yang1, yang2 and yangnolin as "your lab's condo" -- no guest limit, and Fastest start could take it
    import sqlite3
    from datetime import datetime
    from nomad.condo import config as ccfg
    from nomad.submit import where
    from tests.test_release_1254 import _rows
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE group_membership (group_name TEXT, username TEXT, source_site TEXT, timestamp TEXT)")
    report = ccfg.from_dict({"report": {"clusters": {"spydur": {"condos": {
        "yang1": {"owner": "myang", "partition": "yang1"},
        "yangnolin": {"owner": ["myang", "knolin"], "partition": "yangnolin", "members": ["mo3ct"]}}}}}})
    def big_group(c):           # as resolve_members: the list when given, else the owners' (big) lab group
        return set(c.members) if isinstance(c.members, list) else {"jtonini", "gflanagi", "perickso", "mo3ct"}
    mine = lambda who: {c: m for c, _n, m in where.my_condos(report, "spydur", who, _rows(), big_group)}  # noqa: E731
    assert mine("jtonini") == {"yang1": False, "yangnolin": False}
    assert mine("myang") == {"yang1": True, "yangnolin": True} and mine("knolin")["yangnolin"]
    assert mine("mo3ct") == {"yang1": False, "yangnolin": True}          # listed in members
    assert where.unlisted_members(report, "spydur") == ["yang1"]
    people_of = where.people_lookup(conn, {}, "spydur", now=datetime(2026, 11, 3))
    assert where.my_condos(report, "spydur", "mo3ct", _rows(), people_of)[1][2] is True

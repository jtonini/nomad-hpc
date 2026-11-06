"""nomad 1.30.1: a lab's quiet condo while its jobs wait elsewhere (condo_notes); the CLI's wording."""
import json

from click.testing import CliRunner

from nomad.condo import config as ccfg
from nomad.facts.share import share
from tests.test_release_1300 import CONFIG, JOBS, NOW, REPORT, build, t


def _share(tmp_path, jobs, **kw):
    p = tmp_path / "r.toml"
    p.write_text(REPORT)
    c = build(tmp_path / f"x{len(list(tmp_path.glob('x*.db')))}.db", jobs)
    try:
        return share(c, CONFIG, "jsmith", days=30, now=NOW, condos=ccfg.load(p), **kw)
    finally:
        c.close()


# s1's CPU jobs wait an hour or more on hpc2 while the lab's condo (c01-c02, 64 cores, no GPUs) sits quiet
WAITING = [(f"w{i}", "s1", "standard", "a01", t(10 + i), t(10 + i, 3), t(10 + i, 4), "COMPLETED", 4, "Resources",
            "hpc2") for i in range(6)]


def test_its_quiet_condo_while_its_jobs_wait_elsewhere(tmp_path):
    d = _share(tmp_path, WAITING)
    note, = d["labs"][0]["condo_notes"]
    assert note["cluster"] == "hpc1" and note["partitions"] == ["smith"] and note["cores"] == 64 and note["gpus"] == 0
    assert note["own_condo_pct"] == 0.0
    assert note["elsewhere"] == [{"cluster": "hpc2", "label": "hpc2", "jobs": 6, "median_minutes": 180}]


def test_no_note_for_jobs_its_condo_couldnt_run_or_a_busy_condo(tmp_path):
    # GPU jobs (its condo has none), or more cores than it has: no note
    gpu = [w[:11] + (1,) for w in WAITING]
    assert _share(tmp_path, gpu)["labs"][0]["condo_notes"] == []
    big = [w[:8] + (128,) + w[9:] for w in WAITING]
    assert _share(tmp_path, big)["labs"][0]["condo_notes"] == []
    # waits for its own condo are not elsewhere
    own = [(f"o{i}", "s1", "smith", "c01", t(10 + i), t(10 + i, 3), t(10 + i, 4), "COMPLETED", 4, "Resources")
           for i in range(6)]
    assert _share(tmp_path, own)["labs"][0]["condo_notes"] == []
    # a condo a quarter busy or more: no note
    busy = WAITING + [("b", "jsmith", "smith", "c[01-02]", t(2), t(2), t(29), "COMPLETED", 64)]
    assert _share(tmp_path, busy)["labs"][0]["condo_notes"] == []
    # too few, or short, waits: no note
    assert _share(tmp_path, WAITING[:4])["labs"][0]["condo_notes"] == []


def test_the_cli_says_so_and_folds_clusters_it_didnt_use(tmp_path):
    from nomad.cli import cli
    build(tmp_path / "c.db", JOBS + WAITING).close()
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n')
    (tmp_path / "report.toml").write_text(REPORT)
    args = ["-c", str(cfg), "facts", "share", "jsmith", "--db", str(tmp_path / "c.db"), "--report",
            str(tmp_path / "report.toml"), "--now", NOW.isoformat()]
    r = CliRunner().invoke(cli, args)
    assert r.exit_code == 0, r.output
    assert "its own condo on Cluster One (smith: 64 cores, no GPUs) was" in r.output
    assert "6 jobs on hpc2 (median 3.0 h)" in r.output and "-p smith" in r.output
    # hpc2 has no condo of the lab's: no "off its condo" there
    hpc2 = r.output[r.output.index("  hpc2:"):]
    assert "off its condo" not in hpc2
    r = CliRunner().invoke(cli, args + ["--mask"])
    assert "smith" not in r.output and "condo #1" in r.output
    r = CliRunner().invoke(cli, args + ["--json"])
    assert json.loads(r.output)["labs"][0]["condo_notes"][0]["partitions"] == ["smith"]


def test_a_cluster_it_didnt_use_is_one_line(tmp_path):
    from nomad.cli import cli
    jobs = [j for j in JOBS if (j[10] if len(j) > 10 else "hpc1") == "hpc1"]
    build(tmp_path / "c.db", jobs).close()
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n')
    (tmp_path / "report.toml").write_text(REPORT)
    r = CliRunner().invoke(cli, ["-c", str(cfg), "facts", "share", "jsmith", "--db", str(tmp_path / "c.db"),
                                 "--report", str(tmp_path / "report.toml"), "--now", NOW.isoformat()])
    assert "didn't run on: hpc2" in r.output and "  hpc2:" not in r.output

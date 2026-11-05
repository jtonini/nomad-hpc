"""nomad 1.30.0: a lab's share of each cluster (nomad.facts.share, `nomad facts share`)."""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad.condo import config as ccfg, figures
from nomad.facts.share import share

NOW = datetime(2026, 10, 1, 7, 0)
CONFIG = {"console": {"labs": {"group_pattern": "{netid}$"}}}
REPORT = """
[report.clusters.hpc1]
label = "Cluster One"

[report.clusters.hpc1.condos.smith]
owner = "jsmith"

[report.clusters.hpc1.condos.jones_lab]
owner = "rjones"
"""
LAYOUT = {"c01": "smith,condos", "c02": "smith,condos", "c03": "jones_lab,condos", "c04": "jones_lab,condos",
          "n01": "basic", "n02": "basic", "n03": "basic", "n04": "basic"}


def t(day, hour=0, minute=0, month=9):
    return datetime(2026, month, day, hour, minute).isoformat()


def build(path, jobs, extra_nodes=()):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE node_state (id INTEGER PRIMARY KEY, timestamp TEXT, node_name TEXT, state TEXT,
        cpus_total INTEGER, cpus_alloc INTEGER, memory_total_mb INTEGER, partitions TEXT, gres TEXT,
        source_site TEXT);
    CREATE TABLE jobs (job_id TEXT, user_name TEXT, partition TEXT, node_list TEXT, submit_time TEXT,
        eligible_time TEXT, start_time TEXT, end_time TEXT, state TEXT, req_cpus INTEGER,
        req_mem_mb INTEGER, req_gpus INTEGER, runtime_seconds INTEGER, account TEXT, alloc_tres TEXT,
        pending_reason TEXT, ready_since TEXT, source_site TEXT);
    CREATE TABLE group_membership (username TEXT, group_name TEXT, collected_at TEXT,
        has_account INTEGER, source_site TEXT, cluster TEXT, first_seen TEXT);
    """)
    rows = []
    ts = datetime(2026, 8, 25)
    while ts < NOW:
        for node, parts in LAYOUT.items():
            down = node == "n04" and datetime(2026, 9, 10) <= ts < datetime(2026, 9, 12)
            rows.append((ts.isoformat(), node, "DOWN*" if down else "MIXED", 32, 0, 256000, parts,
                         "gpu:a40:2" if node == "n03" else None, "hpc1"))
        for node in ("a01", "a02") + tuple(extra_nodes):
            rows.append((ts.isoformat(), node, "IDLE", 16, 0, 64000, "standard", None, "hpc2"))
        ts += timedelta(hours=1)
    c.executemany("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, "
                  "memory_total_mb, partitions, gres, source_site) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    for j in jobs:
        jid, user, part, nodes, sub, start, end, state, cpus = j[:9]
        reason = j[9] if len(j) > 9 else None
        site = j[10] if len(j) > 10 else "hpc1"
        gpus = j[11] if len(j) > 11 else 0
        rt = 0
        if start and end:
            rt = int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
        c.execute("INSERT INTO jobs (job_id, user_name, partition, node_list, submit_time, start_time, "
                  "end_time, state, req_cpus, req_mem_mb, req_gpus, runtime_seconds, account, alloc_tres, "
                  "pending_reason, source_site) VALUES (?,?,?,?,?,?,?,?,?,4000,?,?,'default',?,?,?)",
                  (jid, user, part, nodes, sub, start, end, state, cpus, gpus, rt,
                   f"cpu={cpus},mem=4000M,node=1" + (f",gres/gpu={gpus}" if gpus else ""), reason, site))
    for user, group in (("jsmith", "jsmith$"), ("s1", "jsmith$"), ("rjones", "rjones$"), ("r2", "rjones$")):
        c.execute("INSERT INTO group_membership VALUES (?, ?, ?, 1, 'hpc1', 'hpc1', NULL)",
                  (user, group, NOW.isoformat()))
    c.commit()
    return c


JOBS = [
    # id, user, partition, nodes, submit, start, end, state, cpus, reason, site, gpus
    ("1", "jsmith", "smith", "c01", t(2), t(2), t(2, 10), "COMPLETED", 16),            # own condo 160
    ("2", "g1", "condos", "c02", t(3), t(3), t(3, 4), "COMPLETED", 32),                # guest on it 128
    ("3", "x9", "smith", "c02", t(4), t(4), t(4, 5), "COMPLETED", 8),                  # its partition: the lab's 40
    ("4", "s1", "condos", "c03", t(5), t(5), t(5, 10), "COMPLETED", 8),                # jones' condo 80
    ("5", "s1", "basic", "n01", t(13), t(13), t(13, 10), "COMPLETED", 8),              # shared 80
    ("6", "g2", "basic", "n01", t(13, 2), t(13, 8), t(13, 18), "COMPLETED", 16, "Resources"),  # waited 6 h
    ("7", "g3", "basic", "n[01-02]", t(20), t(20), t(20, 5), "COMPLETED", 32),         # shared 160
    ("8", "s1", "basic", "n01", t(20), t(20, 3), t(20, 4), "COMPLETED", 8, "Priority"),  # the lab waited 3 h
    ("9", "g4", "basic", "n02", t(21), t(22), t(22, 2), "COMPLETED", 4, "Dependency"),  # not for room
    ("10", "g5", "basic", "n03", t(25), t(25), t(25, 10), "COMPLETED", 4, None, "hpc1", 1),  # 40, 10 GPU-h
    ("11", "s1", "standard", "a01", t(14), t(14), t(14, 10), "COMPLETED", 4, None, "hpc2"),  # hpc2 40
    ("12", "rjones", "jones_lab", "c03", t(6), t(6), t(6, 2), "COMPLETED", 16),       # jones' own 32
    ("13", "g6", "basic", None, t(29), None, None, "PENDING", 4, "Resources"),        # waiting now
    ("14", "g7", "basic", None, t(28), None, t(28, 5), "CANCELLED", 4, "Resources"),  # cancelled after 5 h
]


@pytest.fixture
def conn(tmp_path):
    c = build(tmp_path / "combined.db", JOBS)
    yield c
    c.close()


@pytest.fixture
def settings(tmp_path):
    p = tmp_path / "report.toml"
    p.write_text(REPORT)
    return ccfg.load(p)


def lab_of(data, group="jsmith$"):
    return next(x for x in data["labs"] if x["group"] == group)


def cluster(lab, name):
    return next(c for c in lab["clusters"] if c["cluster"] == name)


def row(c, where):
    return next(r for r in c["rows"] if r["where"] == where)


def test_where_the_lab_ran_against_everyone_and_the_capacity(conn, settings):
    data = share(conn, CONFIG, "jsmith", days=30, now=NOW, condos=settings)
    assert data["kind"] == "lab_share" and data["window"]["since"] == "2026-09-01T00:00:00"
    lab = lab_of(data)
    assert lab["as"] == "pi" and lab["members"] == 2
    c = cluster(lab, "hpc1")
    assert c["label"] == "Cluster One" and [o["condo"] for o in c["own_condos"]] == ["smith"]
    own, shared, other = row(c, "own_condo"), row(c, "shared"), row(c, "other_condos")
    assert own["nodes"] == 2 and own["cores"] == 64
    # jsmith's 160 and x9's 40 in the condo's partition; g1 is a guest
    assert own["lab_core_hours"] == 200 and own["others_core_hours"] == 128
    # the window: 1 Sep 00:00 to 1 Oct 07:00 = 727 h; 64 cores in service throughout
    hours = (NOW - datetime(2026, 9, 1)).total_seconds() / 3600
    assert abs(own["capacity_core_hours"] - 64 * hours) < 1
    assert own["lab_pct"] == round(100 * 200 / (64 * hours), 1)
    assert shared["lab_core_hours"] == 80 + 8 and shared["others_core_hours"] == 160 + 160 + 8 + 40
    assert shared["others_people"] == 4                     # g2, g3, g4, g5
    # n04 down for two days: out of service
    assert abs(shared["capacity_core_hours"] - (4 * 32 * hours - 32 * 48)) < 2
    assert shared["gpus"] == 2 and shared["others_gpu_hours"] == 10 and shared["lab_gpu_hours"] == 0
    assert other["lab_core_hours"] == 80 and other["others_core_hours"] == 32
    tot = c["total"]
    assert tot["lab_core_hours"] == 200 + 88 + 80 and tot["lab_people"] == 3     # jsmith, s1 and x9
    assert tot["lab_of_used_pct"] == round(100 * 368 / (368 + 128 + 368 + 32), 1)
    assert sum(x["lab_core_hours"] for x in c["trend"]) == pytest.approx(368, abs=0.5)
    assert len(c["trend"]) == 31 and c["trend"][0]["from"] == "2026-09-01T00:00:00"
    # hpc2: no condos, the lab's 40 on shared nodes; no own-condo row
    c2 = cluster(lab, "hpc2")
    assert [r["where"] for r in c2["rows"]] == ["shared"] and c2["rows"][0]["lab_core_hours"] == 40
    assert c2["total"]["others_core_hours"] == 0


def test_who_waited_for_room_while_the_lab_ran(conn, settings):
    c = cluster(lab_of(share(conn, CONFIG, "jsmith", days=30, now=NOW, condos=settings)), "hpc1")
    w = c["waits"]
    # g2 (6 h) and g7 (cancelled after 5 h); g6, waiting still, counts the day it stops; g4's
    # dependency doesn't count
    assert w["others"] == {"jobs": 2, "people": 2, "mean_minutes": 330, "left_out_days": 0}
    # g2 waited 02:00-08:00 on basic, all of it while s1's job ran on n01 (until 10:00)
    b = w["behind_lab"]
    assert b["jobs"] == 1 and b["people"] == 1 and b["hours"] == 6.0
    # s1's 8 cores of the 8 in use on basic then (g2's own started at 08:00)
    assert b["lab_cores_avg"] == 8.0 and b["lab_of_busy_pct"] == 100.0 and b["cores_there_avg"] == 128
    # the lab's own wait: s1, 3 h, while g3 held 32 of the 32 cores in use
    assert w["lab"]["jobs"] == 1 and w["lab"]["median_minutes"] == 180
    assert w["lab"]["others_of_busy_pct"] == 100.0 and w["lab"]["while_others_ran_pct"] == 100.0


def test_the_lab_on_its_own_condo_is_not_others_waiting_behind_it(tmp_path, settings):
    jobs = [("1", "jsmith", "smith", "c01", t(2), t(2), t(2, 10), "COMPLETED", 16),
            ("2", "g1", "condos", "c01", t(2, 1), t(2, 10), t(2, 12), "COMPLETED", 32, "Resources")]
    c = build(tmp_path / "x.db", jobs)
    w = cluster(lab_of(share(c, CONFIG, "jsmith", days=30, now=NOW, condos=settings)), "hpc1")["waits"]
    assert w["others"]["jobs"] == 1 and w["behind_lab"]["jobs"] == 0


def test_others_days_resting_on_too_few_are_left_out(conn, settings):
    c = cluster(lab_of(share(conn, CONFIG, "jsmith", days=30, now=NOW, condos=settings, min_others=3)), "hpc1")
    # four people on the shared nodes, but each alone that day: every day left out
    sh = row(c, "shared")
    assert sh["others_core_hours"] == 0 and sh["others_people"] == 0 and sh["others_left_out_days"] == 4
    assert sh["idle_pct"] is None and sh["lab_core_hours"] == 88
    other = row(c, "other_condos")                                       # rjones alone
    assert other["others_core_hours"] == 0 and other["others_left_out_days"] == 1 and other["idle_pct"] is None
    # the guest on the lab's own condo: as its condo report gives it
    assert row(c, "own_condo")["others_core_hours"] == 128
    assert c["total"]["others_core_hours"] == 128 and c["total"]["others_left_out_days"] == 5   # 4 shared, 1 jones
    assert c["total"]["idle_pct"] is None
    assert sum(x["others_core_hours"] for x in c["trend"]) == 128
    w = c["waits"]
    # g2 and g7 each alone the day their waits ended
    assert w["others"]["jobs"] == 0 and w["others"]["left_out_days"] == 2 and w["behind_lab"]["jobs"] == 0
    assert w["lab"]["others_of_busy_pct"] is None and w["lab"]["busy_left_out_days"] == 1    # g3 alone held them


def test_any_two_windows_differ_by_whole_days_of_three_or_more(tmp_path):
    """A victim alone on 12 Sep; three others on 25 Sep: the 30-day figure less the 7-day one
    is not the victim's (review 2)."""
    jobs = [(f"o{i}", u, "basic", "n01", t(25, i), t(25, i), t(25, i + 1), "COMPLETED", 4) for i, u in
            enumerate(["a", "b", "c"])]
    jobs += [("v", "victim", "basic", "n02", t(12), t(12), t(12, 5), "COMPLETED", 8)]
    jobs += [(f"w{i}", u, "basic", "n03", t(26), t(26, i + 1), t(26, i + 2), "COMPLETED", 4, "Resources")
             for i, u in enumerate(["a", "b", "c"])]
    jobs += [("vw", "victim", "basic", "n03", t(12), t(12, 2), t(12, 3), "COMPLETED", 4, "Resources")]
    figs = {}
    for days in (7, 30):
        p = tmp_path / f"r{days}.toml"
        p.write_text(REPORT)
        c = build(tmp_path / f"d{days}.db", jobs)
        d = share(c, CONFIG, "jsmith", days=days, now=NOW, condos=ccfg.load(p), min_others=3)
        c.close()
        figs[days] = cluster(lab_of(d), "hpc1")
    s7, s30 = row(figs[7], "shared"), row(figs[30], "shared")
    assert s7["others_core_hours"] == s30["others_core_hours"] == 24       # 25 and 26 Sep, three each
    assert s30["others_left_out_days"] == 1 and s7["others_left_out_days"] == 0
    assert figs[7]["waits"]["others"]["jobs"] == figs[30]["waits"]["others"]["jobs"] == 3


def test_a_condo_two_labs_share(tmp_path):
    p = tmp_path / "report.toml"
    p.write_text(REPORT.replace('owner = "jsmith"', 'owner = "jsmith"\nmembers = "group:jsmith$,rjones$"'))
    s = ccfg.load(p)
    jobs = [("1", "jsmith", "smith", "c01", t(2), t(2), t(2, 10), "COMPLETED", 16),
            ("2", "r2", "smith", "c02", t(3), t(3), t(3, 10), "COMPLETED", 8),
            ("3", "g1", "condos", "c02", t(4), t(4), t(4, 1), "COMPLETED", 8)]
    c = build(tmp_path / "x.db", jobs)
    data = share(c, CONFIG, "jsmith", days=30, now=NOW, condos=s)
    hp = cluster(lab_of(data), "hpc1")
    assert hp["own_condos"][0]["shared_with"] == ["rjones$"]
    own = row(hp, "own_condo")
    assert own["lab_core_hours"] == 160 and own["others_core_hours"] == 88
    assert own["partners_core_hours"] == 80 and own["guests_core_hours"] == 8
    # and the other lab sees it as its own too
    other = share(c, CONFIG, "rjones", days=30, now=NOW, condos=s)
    hp2 = cluster(lab_of(other, "rjones$"), "hpc1")
    assert {o["condo"] for o in hp2["own_condos"]} == {"smith", "jones_lab"}


def test_without_report_toml_the_nodes_say_which_are_condos(conn):
    c = cluster(lab_of(share(conn, CONFIG, "jsmith", days=30, now=NOW)), "hpc1")
    assert c["own_condos"] == [] and [r["where"] for r in c["rows"]] == ["shared", "other_condos"]
    assert row(c, "other_condos")["lab_core_hours"] == 160 + 80          # the smith condo is not its own here


def test_staff_name_a_lab_and_someone_who_leads_none_gets_nothing(conn, settings):
    data = share(conn, CONFIG, groups=["rjones$"], days=7, now=NOW, condos=settings)
    lab = lab_of(data, "rjones$")
    assert "as" not in lab and data["window"]["bucket_days"] == 1
    assert share(conn, CONFIG, "nobody", now=NOW, condos=settings)["labs"] == []
    assert share(None, CONFIG, "jsmith", now=NOW)["labs"] == []


def test_a_research_assistant_sees_the_lab_without_its_pi(tmp_path, settings):
    cfg = {"console": {"labs": {"group_pattern": "{netid}$", "assistants": {"jsmith$": ["s1"]}}}}
    c = build(tmp_path / "x.db", JOBS)
    data = share(c, cfg, "s1", days=30, now=NOW, condos=settings)
    lab = lab_of(data)
    assert lab["as"] == "assistant" and lab["members"] == 1
    own = row(cluster(lab, "hpc1"), "own_condo")
    # jsmith's own 160 count with everyone else's; x9 ran in the condo's partition: the lab's
    assert own["lab_core_hours"] == 40 and own["others_core_hours"] == 128 + 160


def test_windows_and_trend_steps():
    from nomad.facts.share import _window, bucket_days
    assert [bucket_days(d) for d in (7, 30, 31, 90, 365)] == [1, 1, 1, 7, 28]
    days, lo, hi, spans = _window(90, NOW)
    assert lo == datetime(2026, 7, 3) and hi == NOW and all(a.time() == datetime.min.time() for _k, a, _b in spans)
    assert spans[-1][2] > NOW >= spans[-1][1]
    # whole days: to the last midnight, 90 of them
    days, lo, hi, spans = _window(90, NOW, whole_days=True)
    assert hi == datetime(2026, 10, 1) and lo == datetime(2026, 7, 3) and spans[-1][1] < hi <= spans[-1][2]


def test_node_hours_over_days_match_node_months(conn):
    months = figures.node_months(conn, "hpc1", ["n04"], ["2026-09"])
    days = figures.node_hours(conn, "hpc1", ["n04"], [(f"d{i}", datetime(2026, 9, 1) + timedelta(days=i),
                                                      datetime(2026, 9, 1) + timedelta(days=i + 1))
                                                     for i in range(30)])
    m = months[("n04", "2026-09")]
    assert abs(sum(nm.in_service for nm in days.values()) - m.in_service) < 0.01
    assert abs(sum(nm.classes["down"] for nm in days.values()) - 48) < 0.5
    with pytest.raises(ValueError):
        figures.node_hours(conn, "hpc1", ["n04"], [("x", datetime(2026, 9, 1, 3), datetime(2026, 9, 2))])


def test_the_cli(tmp_path, settings):
    from nomad.cli import cli
    build(tmp_path / "c.db", JOBS).close()
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[console.labs]\ngroup_pattern = "{netid}$"\n')
    (tmp_path / "report.toml").write_text(REPORT)
    args = ["-c", str(cfg), "facts", "share", "jsmith", "--db", str(tmp_path / "c.db"), "--report",
            str(tmp_path / "report.toml"), "--now", NOW.isoformat()]
    r = CliRunner().invoke(cli, args)
    assert r.exit_code == 0, r.output
    assert "Cluster One" in r.output and "own condo" in r.output and "waited for room" in r.output
    r = CliRunner().invoke(cli, args + ["--json"])
    assert json.loads(r.output)["kind"] == "lab_share"
    r = CliRunner().invoke(cli, args + ["--mask"])
    assert r.exit_code == 0 and "jsmith" not in r.output and "smith" not in r.output
    r = CliRunner().invoke(cli, ["-c", str(cfg), "facts", "share", "--group", "rjones$", "--db",
                                 str(tmp_path / "c.db"), "--report", str(tmp_path / "report.toml"),
                                 "--now", NOW.isoformat()])
    assert r.exit_code == 0 and "rjones$" in r.output


# --- 1.30.0 review: what others' figures could give away, and exactness -------------------

def _run(tmp_path, jobs, netid="jsmith", cfg=CONFIG, min_others=3, settings_text=REPORT, extra=(), **kw):
    p = tmp_path / f"r{len(list(tmp_path.glob('*.toml')))}.toml"
    p.write_text(settings_text)
    c = build(tmp_path / (p.stem + ".db"), jobs, extra)
    try:
        d = share(c, cfg, netid, days=30, now=NOW, condos=ccfg.load(p), min_others=min_others, **kw)
    finally:
        c.close()
    return d, {cl["cluster"]: cl for cl in d["labs"][0]["clusters"]}


FOUR = [(f"o{i}", u, "basic", "n01", t(3 + i), t(3 + i), t(3 + i, 2), "COMPLETED", 4)
        for i, u in enumerate(["a", "b", "c"])]


def test_a_research_assistant_is_not_shown_the_pi_alone_on_its_condo(tmp_path):
    cfg = {"console": {"labs": {"group_pattern": "{netid}$", "assistants": {"jsmith$": ["s1"]}}}}
    jobs = [("1", "jsmith", "smith", "c01", t(2), t(2), t(2, 10), "COMPLETED", 16)]
    _d, c = _run(tmp_path, jobs, netid="s1", cfg=cfg)
    own = row(c["hpc1"], "own_condo")
    assert own["others_core_hours"] == 0 and own["others_left_out_days"] == 1 and own["idle_pct"] is None
    # its PI sees its condo's guests as the condo report gives them
    _d, c = _run(tmp_path, [("2", "g1", "condos", "c01", t(2), t(2), t(2, 1), "COMPLETED", 8)])
    assert row(c["hpc1"], "own_condo")["others_core_hours"] == 8


def test_a_research_assistant_gets_no_partner_and_guest_split(tmp_path):
    cfg = {"console": {"labs": {"group_pattern": "{netid}$", "assistants": {"jsmith$": ["s1"]}}}}
    text = REPORT.replace('owner = "jsmith"', 'owner = "jsmith"\nmembers = "group:jsmith$,rjones$"')
    jobs = [("1", "jsmith", "smith", "c01", t(2), t(2), t(2, 10), "COMPLETED", 13)]
    jobs += [(f"p{i}", u, "smith", "c02", t(2), t(2, i), t(2, i + 1), "COMPLETED", 8) for i, u in
             enumerate(["r2", "rjones", "r2"])]
    _d, c = _run(tmp_path, jobs, netid="s1", cfg=cfg, settings_text=text, min_others=0)
    own = row(c["hpc1"], "own_condo")
    assert "partners_core_hours" not in own and "guests_core_hours" not in own


def test_a_trend_step_is_a_sum_of_whole_days(tmp_path):
    jobs = [(f"o{i}", u, "basic", "n01", t(3), t(3, i), t(3, i + 2), "COMPLETED", 4)
            for i, u in enumerate(["a", "b", "c"])]
    jobs += [("solo", "victim", "basic", "n02", t(20), t(20), t(20, 7), "COMPLETED", 32)]
    _d, c = _run(tmp_path, jobs)
    hp = c["hpc1"]
    assert hp["total"]["others_core_hours"] == 24 and hp["total"]["others_left_out_days"] == 1
    sep20 = next(x for x in hp["trend"] if x["from"].startswith("2026-09-20"))
    assert sep20["others_core_hours"] == 0 and sep20["others_left_out_days"] == 1


def test_gpu_figures_need_enough_gpu_users(tmp_path):
    jobs = [(f"o{i}", u, "basic", "n01", t(10), t(10, i), t(10, i + 2), "COMPLETED", 4)
            for i, u in enumerate(["a", "b", "c"])]
    jobs += [("g", "gpuuser", "basic", "n03", t(10), t(10), t(10, 9), "COMPLETED", 4, None, "hpc1", 2)]
    _d, c = _run(tmp_path, jobs)
    sh = row(c["hpc1"], "shared")
    assert sh["others_people"] == 4 and sh["others_core_hours"] == 60
    assert sh["others_gpu_hours"] == 0 and sh["others_gpu_left_out_days"] == 1 and sh["idle_gpu_pct"] is None
    assert c["hpc1"]["total"]["others_gpu_hours"] == 0


def test_behind_the_lab_is_exact_and_leaves_its_own_condo_out(tmp_path):
    # the lab's job ends at 10:02; g2 waits from 10:10: not behind it
    jobs = [("1", "s1", "basic", "n01", t(13), t(13, 9), t(13, 10, 2), "COMPLETED", 8),
            ("w", "g2", "basic", "n02", t(13, 10, 10), t(13, 11), t(13, 12), "COMPLETED", 4, "Resources")]
    _d, c = _run(tmp_path, jobs, min_others=0)
    assert c["hpc1"]["waits"]["behind_lab"]["jobs"] == 0
    # its own condo's cores are its own: of the busy cores off its condo, all the lab's
    jobs = [("1", "jsmith", "smith", "c01", t(13), t(13), t(13, 8), "COMPLETED", 32),
            ("2", "s1", "condos", "c03", t(13), t(13), t(13, 8), "COMPLETED", 8),
            ("w", "g1", "condos", None, t(13, 2), None, t(13, 6), "CANCELLED", 4, "Resources")]
    _d, c = _run(tmp_path, jobs, min_others=0)
    b = c["hpc1"]["waits"]["behind_lab"]
    assert b["jobs"] == 1 and b["hours"] == 4.0 and b["lab_cores_avg"] == 8.0
    assert b["cores_there_avg"] == 64 and b["lab_of_busy_pct"] == 100.0


def test_what_others_held_while_the_lab_waited_needs_enough_of_them(tmp_path):
    # four people outside the lab ran, but one held the nodes while s1 waited
    jobs = FOUR + [("h", "holder", "basic", "n[01-02]", t(20), t(20), t(20, 5), "COMPLETED", 32),
                   ("8", "s1", "basic", "n01", t(20), t(20, 3), t(20, 4), "COMPLETED", 8, "Priority")]
    _d, c = _run(tmp_path, jobs)
    lab = c["hpc1"]["waits"]["lab"]
    assert lab["jobs"] == 1 and lab["others_of_busy_pct"] is None and lab["while_others_ran_pct"] is None
    _d, c = _run(tmp_path, jobs, min_others=0)
    assert c["hpc1"]["waits"]["lab"]["others_of_busy_pct"] == 100.0


def test_partners_only_on_the_condo_they_share(tmp_path):
    text = REPORT + '\n[report.clusters.hpc1.condos.smith2]\nowner = "jsmith"\nnodes = "c02"\n'
    text = text.replace('[report.clusters.hpc1.condos.smith]\nowner = "jsmith"',
                        '[report.clusters.hpc1.condos.smith]\nowner = "jsmith"\nnodes = "c01"\n'
                        'members = "group:jsmith$,rjones$"')
    jobs = [("1", "r2", "condos", "c02", t(3), t(3), t(3, 10), "COMPLETED", 8),
            ("2", "r2", "condos", "c01", t(4), t(4), t(4, 1), "COMPLETED", 8)]
    _d, c = _run(tmp_path, jobs, settings_text=text, min_others=0)
    own = row(c["hpc1"], "own_condo")
    assert own["partners_core_hours"] == 8 and own["guests_core_hours"] == 80


def test_a_node_no_longer_read_keeps_its_cores_and_gpus(tmp_path):
    # hpc2's a09 (a GPU node) is read until 5 Sep only
    c = build(tmp_path / "x.db", [("1", "s1", "standard", "a09", t(3), t(3), t(3, 10), "COMPLETED", 4,
                                   None, "hpc2", 2)])
    c.execute("DELETE FROM node_state WHERE node_name = 'a09' AND timestamp >= ?", (t(5),))
    c.execute("INSERT INTO node_state (timestamp, node_name, state, cpus_total, cpus_alloc, memory_total_mb, "
              "partitions, gres, source_site) SELECT timestamp, 'a09', 'IDLE', 16, 0, 64000, 'standard', "
              "'gpu:a40:2', 'hpc2' FROM node_state WHERE node_name = 'a01' AND timestamp < ?", (t(5),))
    c.commit()
    d = share(c, CONFIG, "jsmith", days=30, now=NOW)
    sh = row(next(x for x in d["labs"][0]["clusters"] if x["cluster"] == "hpc2"), "shared")
    assert sh["nodes"] == 3 and sh["cores"] == 48 and sh["gpus"] == 2
    assert sh["capacity_gpu_hours"] > 0 and sh["lab_gpu_hours"] == 20 and sh["lab_gpu_pct"] is not None


def test_the_consoles_answer_is_the_same_all_day(tmp_path):
    jobs = FOUR + [("x", "late", "basic", "n02", t(30, 7), t(30, 7), t(30, 9), "COMPLETED", 8)]
    later = NOW + timedelta(hours=1)
    c = build(tmp_path / "x.db", jobs + [("y", "later", "basic", "n03", t(1, 7, 0, 10), t(1, 7, 0, 10),
                                          t(1, 7, 30, 10), "COMPLETED", 8)])
    p = tmp_path / "r.toml"
    p.write_text(REPORT)
    a = share(c, CONFIG, "jsmith", days=30, now=NOW, condos=ccfg.load(p), min_others=3)
    b = share(c, CONFIG, "jsmith", days=30, now=later, condos=ccfg.load(p), min_others=3)
    assert a["window"]["until"] == "2026-10-01T00:00:00" and a["window"]["whole_days"]
    assert a["labs"] == b["labs"]
    # staff: to now
    s = share(c, CONFIG, groups=["jsmith$"], days=30, now=NOW, condos=ccfg.load(p))
    assert s["window"]["until"] == NOW.isoformat() and not s["window"]["whole_days"]


def test_a_job_waiting_in_its_condos_partition_is_the_labs(tmp_path):
    jobs = [("p", "x9", "smith", None, t(29), None, t(29, 5), "CANCELLED", 4, "Resources")]
    _d, c = _run(tmp_path, jobs, min_others=0)
    w = c["hpc1"]["waits"]
    assert w["others"]["jobs"] == 0 and w["lab"]["jobs"] == 1


def test_over_capacity_is_not_said_where_days_are_left_out(tmp_path):
    jobs = [("1", "rjones", "jones_lab", "c[03-04]", t(2), t(2), t(29), "COMPLETED", 200)]
    _d, c = _run(tmp_path, jobs)
    other = row(c["hpc1"], "other_condos")
    assert other["others_left_out_days"] > 0 and "over_capacity" not in other
    assert not any("more core-hours" in x for x in c["hpc1"]["coverage"])


def test_a_wait_counts_once_it_stops_and_the_same_in_every_window(tmp_path):
    # waiting since 20 Sep, still waiting: not counted; started 30 Sep 06:00 after 10 days: counted
    # whole in a 7-day window too (its last 30 days at most)
    jobs = [("p", "g6", "basic", None, t(25), None, None, "PENDING", 4, "Resources"),
            ("q", "g7", "basic", "n02", t(20, 6), t(30, 6), t(30, 7), "COMPLETED", 4, "Resources")]
    for days in (7, 30):
        _d, c = _run(tmp_path, jobs, min_others=0)
        o = c["hpc1"]["waits"]["others"]
        assert o["jobs"] == 1 and o["mean_minutes"] == 10 * 24 * 60


def test_its_own_condo_how_full_and_the_trend_against_it(conn, settings):
    hp = cluster(lab_of(share(conn, CONFIG, "jsmith", days=30, now=NOW, condos=settings)), "hpc1")
    assert hp["own_condo_pct"] == row(hp, "own_condo")["lab_pct"]
    assert sum(x["lab_own_core_hours"] for x in hp["trend"]) == pytest.approx(200, abs=0.5)
    assert all(x["own_capacity_core_hours"] == pytest.approx(64 * 24, abs=1) for x in hp["trend"][1:-1])
    hp2 = cluster(lab_of(share(conn, CONFIG, "jsmith", days=30, now=NOW, condos=settings)), "hpc2")
    assert hp2["own_condo_pct"] is None and hp2["trend"][0]["own_capacity_core_hours"] is None


# --- 1.30.0 review 3: two labs in one answer, membership by day, days that have settled -------

def test_a_lab_the_viewer_also_sees_is_no_outsider_to_the_other(tmp_path):
    """jsmith leads jsmith$ and is a research assistant of rjones$ (r2, r3; its PI rjones): r2 and
    r3 don't make up the three people the PI's own use would hide behind."""
    cfg = {"console": {"labs": {"group_pattern": "{netid}$", "assistants": {"rjones$": ["jsmith"]}}}}
    jobs = [("1", "r2", "basic", "n01", t(10), t(10), t(10, 10), "COMPLETED", 4),
            ("2", "r3", "basic", "n01", t(10), t(10), t(10, 5), "COMPLETED", 4),
            ("3", "rjones", "basic", "n02", t(10), t(10), t(10, 3), "COMPLETED", 8)]
    c = build(tmp_path / "x.db", jobs)
    c.execute("INSERT INTO group_membership VALUES ('jsmith', 'rjones$', ?, 1, 'hpc1', 'hpc1', NULL)",
              (NOW.isoformat(),))
    c.execute("INSERT INTO group_membership VALUES ('r3', 'rjones$', ?, 1, 'hpc1', 'hpc1', NULL)", (NOW.isoformat(),))
    c.commit()
    p = tmp_path / "r.toml"
    p.write_text(REPORT)
    d = share(c, cfg, "jsmith", days=30, now=NOW, condos=ccfg.load(p), min_others=3)
    mine = row(cluster(lab_of(d, "jsmith$"), "hpc1"), "shared")
    theirs = row(cluster(lab_of(d, "rjones$"), "hpc1"), "shared")
    assert lab_of(d, "rjones$")["as"] == "assistant" and theirs["lab_core_hours"] == 60
    # jsmith$'s others that day: r2, r3 and rjones -- but r2 and r3 are rjones$'s, in this answer too
    assert mine["others_core_hours"] == 0 and mine["others_left_out_days"] == 1


def test_a_new_member_does_not_move_days_already_given(tmp_path):
    jobs = [("j1", "new1", "basic", "n01", t(15), t(15), t(15, 10), "COMPLETED", 4),
            ("j2", "new2", "basic", "n01", t(15), t(15), t(15, 5), "COMPLETED", 4),
            ("v", "victim", "basic", "n02", t(15), t(15), t(15, 3), "COMPLETED", 8)]
    c = build(tmp_path / "x.db", jobs)
    p = tmp_path / "r.toml"
    p.write_text(REPORT)

    def day15(now):
        hp = cluster(lab_of(share(c, CONFIG, "jsmith", days=30, now=now, condos=ccfg.load(p), min_others=3)), "hpc1")
        x = next(x for x in hp["trend"] if x["from"].startswith("2026-09-15"))
        return x["lab_core_hours"], x["others_core_hours"], x["others_left_out_days"]
    before = day15(NOW)
    # two new students join (the groups collector first sees them on 1 Oct)
    for u in ("new1", "new2"):
        c.execute("INSERT INTO group_membership VALUES (?, 'jsmith$', ?, 1, 'hpc1', 'hpc1', ?)",
                  (u, NOW.isoformat(), NOW.isoformat()))
    c.commit()
    # their days stay others' (never the lab's), and now they are the lab's people, they don't make
    # up the three either: the day is left out -- an answer shows less, never something new
    assert before == (0.0, 84.0, 0) and day15(NOW + timedelta(days=1)) == (0.0, 0.0, 1)
    # someone who joined on 20 Sep: their jobs from then are the lab's, before then not
    c.execute("INSERT INTO group_membership VALUES ('new3', 'jsmith$', ?, 1, 'hpc1', 'hpc1', ?)",
              (NOW.isoformat(), t(20, 9)))
    for jid, day in (("j3", 18), ("j4", 25)):
        c.execute("INSERT INTO jobs (job_id, user_name, partition, node_list, submit_time, start_time, end_time, "
                  "state, req_cpus, runtime_seconds, alloc_tres, source_site) VALUES (?, 'new3', 'basic', 'n03', "
                  "?, ?, ?, 'COMPLETED', 4, 3600, 'cpu=4,node=1', 'hpc1')", (jid, t(day, 8), t(day, 8), t(day, 9)))
    c.commit()
    hp = cluster(lab_of(share(c, CONFIG, "jsmith", days=30, now=NOW, condos=ccfg.load(p), min_others=0)), "hpc1")
    step = {x["from"][:10]: x for x in hp["trend"]}
    assert step["2026-09-25"]["lab_core_hours"] == 4 and step["2026-09-18"]["lab_core_hours"] == 0
    assert step["2026-09-18"]["others_core_hours"] == 4


def test_a_day_is_given_once_it_has_settled(tmp_path):
    from nomad.facts.share import SETTLE_HOURS
    c = build(tmp_path / "x.db", JOBS)
    p = tmp_path / "r.toml"
    p.write_text(REPORT)
    early = share(c, CONFIG, "jsmith", days=7, now=datetime(2026, 10, 1, SETTLE_HOURS - 1, 50),
                  condos=ccfg.load(p), min_others=3)
    late = share(c, CONFIG, "jsmith", days=7, now=datetime(2026, 10, 1, SETTLE_HOURS, 10),
                 condos=ccfg.load(p), min_others=3)
    assert early["window"]["until"] == "2026-09-30T00:00:00" and late["window"]["until"] == "2026-10-01T00:00:00"
    # a cluster whose readings stopped at 20:00 on 29 Sep ends at the midnight before them
    c.execute("DELETE FROM node_state WHERE source_site = 'hpc2' AND timestamp >= ?", (t(29, 20),))
    c.commit()
    d = share(c, CONFIG, "jsmith", days=7, now=NOW, condos=ccfg.load(p), min_others=3)
    hp2 = cluster(lab_of(d), "hpc2")
    assert hp2["until"] == "2026-09-29T00:00:00" and hp2["trend"][-1]["to"] == "2026-09-29T00:00:00"
    assert "until" not in cluster(lab_of(d), "hpc1")


# --- 1.30.0 review 4: leavers, memberships on several clusters, two labs' pieces, My Lab --------

def test_a_member_who_left_is_the_labs_to_their_last_sighting_only(tmp_path):
    jobs = [("x", "leaver", "basic", "n01", t(30, 1), t(30, 1), t(30, 5), "COMPLETED", 10),
            ("o1", "o1", "basic", "n01", t(30, 1), t(30, 1), t(30, 3), "COMPLETED", 4),
            ("o2", "o2", "basic", "n02", t(30, 1), t(30, 1), t(30, 2), "COMPLETED", 4)]
    c = build(tmp_path / "x.db", jobs)
    # listed until 29 Sep (a run since, 1 Oct, no longer lists them): within nomad's two days' grace
    c.execute("INSERT INTO group_membership VALUES ('leaver', 'jsmith$', ?, 1, 'hpc1', 'hpc1', NULL)", (t(29, 23),))
    c.commit()
    p = tmp_path / "r.toml"
    p.write_text(REPORT)

    def day30(now):
        hp = cluster(lab_of(share(c, CONFIG, "jsmith", days=30, now=now, condos=ccfg.load(p), min_others=3)), "hpc1")
        x = next(x for x in hp["trend"] if x["from"].startswith("2026-09-30"))
        return x["lab_core_hours"], x["others_core_hours"], x["others_left_out_days"]
    # not the lab's on 30 Sep, and no outsider to the count (a lab's own, once): o1 and o2 alone
    assert day30(NOW) == day30(NOW + timedelta(days=1)) == (0.0, 0.0, 1)


def test_two_labs_count_people_on_the_pieces_they_share(tmp_path):
    """jsmith (PI of jsmith$) is also rjones$'s research assistant: a guest alone on rjones's condo,
    left out of rjones$'s own row, is not jsmith$'s other condos less the rest (review 4)."""
    cfg = {"console": {"labs": {"group_pattern": "{netid}$", "assistants": {"rjones$": ["jsmith"]}}}}
    text = REPORT + '\n[report.clusters.hpc1.condos.white]\nowner = "kwhite"\nnodes = "n04"\n'
    jobs = [("p", "pguest", "condos", "c03", t(10), t(10), t(10, 1), "COMPLETED", 7)]
    jobs += [(f"q{i}", f"q{i}", "basic", "n04", t(10), t(10), t(10, 1), "COMPLETED", 2) for i in range(3)]
    c = build(tmp_path / "x.db", jobs)
    c.execute("INSERT INTO group_membership VALUES ('jsmith', 'rjones$', ?, 1, 'hpc1', 'hpc1', NULL)",
              (NOW.isoformat(),))
    c.commit()
    p = tmp_path / "r.toml"
    p.write_text(text)
    d = share(c, cfg, "jsmith", days=30, now=NOW, condos=ccfg.load(p), min_others=3)
    mine = row(cluster(lab_of(d, "jsmith$"), "hpc1"), "other_condos")
    theirs = row(cluster(lab_of(d, "rjones$"), "hpc1"), "own_condo")
    assert theirs["others_core_hours"] == 0 and theirs["others_left_out_days"] == 1
    # jsmith$'s other condos that day hold the guest's piece too: left out whole
    assert mine["others_core_hours"] == 0 and mine["others_left_out_days"] == 1


def test_my_lab_counts_a_members_jobs_from_the_day_they_joined(tmp_path):
    from nomad.facts import labs
    jobs = [("a", "new1", "basic", "n01", t(15), t(15), t(15, 10), "COMPLETED", 4),
            ("b", "new1", "basic", "n01", t(25), t(25), t(25, 10), "COMPLETED", 4)]
    c = build(tmp_path / "x.db", jobs)
    c.execute("INSERT INTO group_membership VALUES ('new1', 'jsmith$', ?, 1, 'hpc1', 'hpc1', ?)",
              (NOW.isoformat(), t(20)))
    c.commit()
    c.row_factory = sqlite3.Row
    d = labs(c, CONFIG, "jsmith", days=30, now=NOW, names=True)
    each = {e["user"]: e for e in d["labs"][0]["each"]}
    assert each["new1"]["finished"] == 1 and each["new1"]["core_hours"] == 40

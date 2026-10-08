"""1.8.0: the definitions behind the usage report (nomad.usage.calcs), with the
reference figures of the October 2026 replacement plan where they are
generic (capacity, growth, storage); node and family names are invented."""
import math
import re
from collections import Counter
from datetime import datetime
from types import SimpleNamespace

from nomad.usage import calcs
from nomad.usage.config import Family, from_dict


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol * max(1.0, abs(b))


def job(user, nodes, cpus, start, elapsed_h, submit=None, end=None, gpus=0):
    s = datetime.fromisoformat(start)
    e = datetime.fromisoformat(end) if end else None
    from datetime import timedelta
    return SimpleNamespace(user=user, nodes=tuple(nodes), cpus=cpus, start=s,
                           end=e or s + timedelta(hours=elapsed_h), elapsed=elapsed_h * 3600,
                           submit=datetime.fromisoformat(submit) if submit else s, gpus=gpus)


def test_time():
    assert calcs.month_hours(2026, 2) == 672 and calcs.month_hours(2024, 2) == 696
    assert calcs.month_hours(2026, 1) == 744 and calcs.month_hours(2026, 4) == 720
    t0, t1 = datetime(2025, 10, 1), datetime(2026, 10, 7)
    assert calcs.period_hours(t0, t1) == 371 * 24
    assert calcs.overlap_hours(datetime(2025, 9, 30), datetime(2025, 10, 2), t0, t1) == 24
    assert approx(calcs.annualize(5.021e6, 371), 4.9395e6, 1e-4)
    assert calcs.months_between(t0, t1)[0] == "2025-10" and calcs.months_between(t0, t1)[-1] == "2026-10"
    assert len(calcs.full_months(t0, t1)) == 12 and "2026-10" not in calcs.full_months(t0, t1)
    assert calcs.months_between(datetime(2026, 1, 1), datetime(2026, 3, 1)) == ["2026-01", "2026-02"]


def test_core_hours_split_and_clipped():
    jobs = [job("a", ["cn01", "cn02"], 104, "2026-01-01T00:00:00", 10),
            job("b", ["cn01"], 52, "2026-01-31T20:00:00", 8)]
    ch, people = calcs.per_node_core_hours(jobs, clip=False)
    assert ch["cn01"] == 52 * 10 + 52 * 8 and ch["cn02"] == 520
    assert people["cn01"] == {"a", "b"}
    # Clipped to January: job b runs 4 h in January, 4 in February.
    ch2, _ = calcs.per_node_core_hours(jobs, datetime(2026, 1, 1), datetime(2026, 2, 1), clip=True)
    assert ch2["cn01"] == 520 + 52 * 4
    assert approx(calcs.utilization(ch2["cn01"], 52, calcs.month_hours(2026, 1)), (520 + 208) / (52 * 744))
    assert math.isnan(calcs.utilization(10, 0, 5))


def test_waiting_share_weighted_by_core_hours():
    scope = [f"cn{i:02d}" for i in range(1, 16)]
    jobs = [
        job("a", ["cn01"], 52, "2026-04-03T00:00:00", 100, submit="2026-04-01T00:00:00"),   # 48 h wait
        job("b", ["cn02"], 52, "2026-04-02T01:00:00", 100, submit="2026-04-02T00:00:00"),   # 1 h
        job("c", ["cn50"], 52, "2026-04-09T00:00:00", 100, submit="2026-04-02T00:00:00"),   # out of scope
        job("d", ["cn08", "cn09"], 104, "2026-04-02T00:00:00", 1, submit="2026-04-02T00:00:00"),
    ]
    jobs.append(SimpleNamespace(user="e", nodes=(), cpus=0, start=None, end=None, elapsed=0,
                                submit=datetime(2026, 4, 5)))                                 # never started
    w = calcs.waiting_share(jobs, scope)["2026-04"]
    assert w["core_hours"] == 5200 + 5200 + 104
    assert w["waiting_core_hours"] == 5200
    assert w["users"] == 3 and w["users_waited"] == 1 and w["jobs_waited"] == 1


def test_cpu_efficiency():
    assert approx(calcs.cpu_efficiency([(50.0, 10, 3600), (100.0, 10, 3600), (None, 10, 3600)]), 0.75)
    assert math.isnan(calcs.cpu_efficiency([(None, 1, 1)]))


def test_gpu_hours_and_families():
    j = job("a", ["g17"], 4, "2026-08-31T20:00:00", 10, gpus=2)
    assert calcs.gpu_hours(j) == 20
    assert calcs.gpu_hours(j, datetime(2026, 9, 1), datetime(2026, 10, 1)) == 12
    fams = [Family("inference pipeline", re.compile(r"audio|^vid")),
            Family("molecular dynamics", re.compile(r"gmx|gromacs")),
            Family("tests", re.compile(r"test"))]
    assert calcs.classify(fams, "Audio_batch3") == ("inference pipeline", "name")
    assert calcs.classify(fams, "job1", "proj/gromacs") == ("molecular dynamics", "workdir")
    assert calcs.classify(fams, "job1", "proj/x") == ("unclassified", None)
    # First match wins: order matters.
    assert calcs.classify(fams, "gmx_test")[0] == "molecular dynamics"
    assert calcs.primary_family(Counter({"molecular dynamics": 5.0, "tests": 0.1}), Counter()) == "molecular dynamics"
    assert calcs.primary_family(Counter(), Counter({"unclassified": 9, "tests": 2})) == "tests"
    assert calcs.primary_family(Counter(), Counter({"unclassified": 9})) == "unclassified"
    assert calcs.gres_gpus("gpu:a40:8(S:0-1)") == 8 and calcs.gres_gpus("gpu:2,shard:4") == 2
    assert calcs.gres_gpus("(null)") == 0 and calcs.gres_gpus(None) == 0


def test_sreport_annual_filters_tres():
    rows = [
        {"month": "2026-08", "tres": "cpu", "allocated_h": 517240, "down_h": 77376, "planned_down_h": 0,
         "idle_h": 0, "planned_h": 566024, "reported_h": 1160640},
        {"month": "2026-08", "tres": "gres/gpu", "allocated_h": 5654, "down_h": 0, "planned_down_h": 0,
         "idle_h": 2360, "planned_h": 0, "reported_h": 8013},
    ]
    a = calcs.sreport_annual(rows)["2026"]
    assert a["allocated_h"] == 517240 and a["months"] == 1          # the GPU row is not added in
    assert approx(a["allocated_share"], 517240 / (1160640 - 77376))
    assert approx(calcs.sreport_annual(rows, tres="gres/gpu")["2026"]["allocated_share"], 5654 / 8013)


def test_growth():
    g = calcs.growth_rates({"2022": 2.55e6, "2023": 3.41e6, "2024": 4.10e6, "2025": 5.07e6})
    assert [round(100 * v) for v in g.values()] == [34, 20, 24]
    assert round(100 * calcs.cagr(2.55e6, 5.07e6, 3)) == 26
    # Only consecutive years.
    assert calcs.growth_rates({"2022": 1.0, "2024": 2.0}) == {}


def test_capacity_and_projection_reference_figures():
    today = calcs.capacity_core_hours(936)
    new9 = calcs.capacity_core_hours(2 * 128 + 3 * 128 + 4 * 64, weight=1.45)
    assert round(today / 1e6, 2) == 6.15
    assert round(new9 / 1e6, 2) == 8.54
    assert round((today + new9) / 1e6, 2) == 14.69
    base = calcs.annualize(5.021e6, 371)
    assert calcs.year_capacity_crossed(base, 0.26, today, 2026) == 2027
    assert calcs.year_capacity_crossed(base, 0.26, new9, 2026) == 2029
    assert calcs.year_capacity_crossed(base, 0.26, today + new9, 2026) == 2031
    assert calcs.year_capacity_crossed(base, 0.34, today + new9, 2026) == 2030
    # Clipped to the period (4.87 M), today's nodes last a year longer at 26%.
    assert calcs.year_capacity_crossed(calcs.annualize(4.87e6, 371), 0.26, today, 2026) == 2028
    assert calcs.floor_new_cores(936, 1.45) == 646


def test_storage_growth_after_cleanup():
    scratch = [("2026-04", 306.2), ("2026-05", 233.9), ("2026-06", 244.8), ("2026-07", 254.6),
               ("2026-08", 259.2), ("2026-09", 270.5), ("2026-10", 276.0)]
    g, first, cleanup = calcs.storage_growth(scratch)
    assert cleanup == "2026-05" and first == "2026-05"
    assert round(g, 1) == 8.4
    assert round(calcs.months_to_full(313.4, 276.0, g), 1) == 4.4
    assert math.isinf(calcs.months_to_full(10, 5, 0)) and math.isinf(calcs.months_to_full(10, 5, float("nan")))
    g1, first1, c1 = calcs.storage_growth([("2026-04", 1.0)])
    assert math.isnan(g1) and first1 == "2026-04" and c1 is None


def test_config_tiers_and_classes():
    cfg = from_dict({"report": {"exclude_users": ["root"], "clusters": {"c1": {
        "tiers": {"basic": "cn[01-08]", "medium": "cn[09-13]", "gpu": "g[16-18]", "condo": "lab[50-61]"},
        "institutional_tiers": ["basic", "medium", "gpu"],
        "exclude_users": ["installer"],
        "tier_partitions": {"short": "basic", "single13": "medium"},
        "gpu_partitions": ["gpus"], "overlay_partitions": ["all"],
        "unlisted_partitions": "condo",
        "cores_per_node": {"cn[01-13],g[16-18],lab[50-61]": 52},
        "gpus_per_node": {"g16": 2, "g[17-18]": 8},
    }}}}, "c1")
    assert cfg.tier_of("cn08") == "basic" and cfg.tier_of("cn09") == "medium" and cfg.tier_of("login1") is None
    assert sum(cfg.cores_per_node[n] for t in cfg.institutional for n in cfg.tiers[t]) == 16 * 52
    assert cfg.exclude_users == ["installer", "root"]
    assert cfg.partition_classes == {"short": "basic", "single13": "medium", "gpus": "gpu", "all": "overlay"}
    assert sum(cfg.gpus_per_node.values()) == 18


def test_config_errors():
    import pytest
    from nomad.usage.config import ReportConfigError
    bad = [
        {"tiers": {"a": "n[01-02]", "b": "n02"}},                       # node in two tiers
        {"tiers": {"a": "n01"}, "institutional_tiers": ["z"]},          # unknown tier
        {"tiers": {"a": "n01"}, "partition_classes": {"p": "nonsense"}},
        {"gpu_families": [{"name": "x", "regex": "("}]},                # bad regex
        {"capacity": {"practical": 3}},
        {"gpu_accounting_start": "yesterday"},
    ]
    for c in bad:
        with pytest.raises(ReportConfigError):
            from_dict({"report": {"clusters": {"c1": c}}}, "c1")


def test_example_config_reads():
    from pathlib import Path
    from nomad.usage.config import load
    cfg = load(Path(__file__).parent.parent / "docs" / "report.example.toml", "c1")
    assert cfg.has_tier_map and cfg.institutional_tiers == ["basic", "medium", "large", "gpu"]
    assert cfg.exclude_users == ["installer", "root"]
    assert cfg.partition_classes["short"] == "basic" and cfg.unlisted_partitions == "condo"
    assert [f.name for f in cfg.gpu_families][0] == "molecular dynamics"
    assert cfg.families[0].name == "quantum chemistry"
    assert cfg.capacity.planned[0].cores == 896
    assert sum(cfg.gpus_per_node.values()) == 18

"""1.28.1: a condo two labs share. spydur's yangnolin is Yang's and Nolin's
(owners myang, knolin): its users are in myang$, its co-owner only in knolin$.
report.toml names both, members = "group:myang$,knolin$"; Slurm's AllowGroups
takes the same list and admits anyone in either."""
import pytest

from nomad import partitions as P
from nomad import preemption as pre
from nomad.condo import config as ccfg

REPORT = """[report.labs]
mail_domain = "richmond.edu"

[report.clusters.spydur.condos.yangnolin]
owner = ["myang", "knolin"]
members = "group:myang$, knolin$"

[report.clusters.spydur.condos.parish]
owner = "cparish"
members = "group:cparish$"
"""


def _rows(yangnolin_groups="ALL"):
    text = [f"PartitionName=yangnolin AllowGroups={yangnolin_groups} AllowAccounts=ALL Nodes=spdr60 Default=NO "
            "MaxTime=UNLIMITED PriorityTier=1 PreemptMode=OFF State=UP GraceTime=0",
            "PartitionName=parish AllowGroups=ALL AllowAccounts=ALL Nodes=spdr[55-58] Default=NO MaxTime=UNLIMITED "
            "PriorityTier=1 PreemptMode=OFF State=UP GraceTime=0",
            "PartitionName=condos AllowGroups=ALL AllowAccounts=ALL Nodes=spdr[50-61] Default=NO MaxTime=UNLIMITED "
            "PriorityTier=1 PreemptMode=OFF State=UP GraceTime=0"]
    rows = P.parse("\n".join(text))
    for r in rows:
        r["site"] = None
    return rows


def _report(tmp_path, text=REPORT):
    path = tmp_path / "report.toml"
    path.write_text(text)
    return ccfg.load(path)


def test_members_names_one_group_or_several(tmp_path):
    rep = _report(tmp_path).cluster("spydur")
    assert rep.condos["yangnolin"].members == "group:myang$,knolin$"            # spaces dropped
    assert ccfg.member_groups(rep.condos["yangnolin"].members) == ["myang$", "knolin$"]
    assert ccfg.member_groups("group:cparish$") == ["cparish$"]
    assert ccfg.member_groups("account:parish") == [] and ccfg.member_groups(["myang"]) == []
    for bad in ("group:myang$,,knolin$", "group:myang$,myang$", "group:,"):
        with pytest.raises(ccfg.CondoConfigError):
            _report(tmp_path, REPORT.replace('"group:myang$, knolin$"', f'"{bad}"'))


def test_the_plan_admits_both_groups(tmp_path):
    rep = _report(tmp_path).cluster("spydur")
    off = {"PreemptType": "(null)", "PreemptMode": "OFF", "JobRequeue": "1"}
    res = pre.check(_rows(), off, rep, pre.Settings())
    yn = next(c for c in res["condos"] if c.name == "yangnolin")
    assert yn.groups == ["myang$", "knolin$"]
    assert yn.changes == [("yangnolin", "AllowGroups=myang$,knolin$ PriorityTier=2 PreemptMode=OFF")]
    assert "yangnolin admits anyone, not only myang$,knolin$ (report.toml's members)" in yn.problems
    plan = pre.plan_text(res, pre.Settings(), "spydur")
    assert "  PartitionName=yangnolin ...  AllowGroups=myang$,knolin$ PriorityTier=2 PreemptMode=OFF" in plan
    assert any(x.endswith("(the rest as on the condos line)") for x in plan)
    # Slurm lists them in another order: the same groups, nothing to change
    res = pre.check(_rows("knolin$,myang$"), off, rep, pre.Settings())
    yn = next(c for c in res["condos"] if c.name == "yangnolin")
    assert not any(ch.startswith("AllowGroups") for _p, ch in yn.changes)
    assert not any("admits" in x for x in yn.problems)


def test_the_lab_is_both_groups_people_and_on_both_labs_pages(tmp_path):
    from nomad.condo.figures import resolve_members
    from nomad.facts.condos import owns
    c = _report(tmp_path).cluster("spydur").condos["yangnolin"]
    groups = {"myang$": {"myang", "am8ht"}, "knolin$": {"knolin"}}
    people, how = resolve_members(None, c, None, lambda g: groups.get(g, set()), lambda g: True)
    assert people == {"myang", "am8ht", "knolin"} and how == "groups myang$, knolin$"
    one = _report(tmp_path).cluster("spydur").condos["parish"]
    assert resolve_members(None, one, None, lambda g: {"cparish", "s1"}, lambda g: True) == \
        ({"cparish", "s1"}, "group cparish$")
    assert owns(c, "myang$", set()) and owns(c, "knolin$", set()) and not owns(c, "cparish$", set())

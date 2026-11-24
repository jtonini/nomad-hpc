"""1.28.1: a condo two labs share. hpc1's chennovak is Chen's and Novak's
(owners tchen, lnovak): its users are in tchen$, its co-owner only in lnovak$.
report.toml names both, members = "group:tchen$,lnovak$"; Slurm's AllowGroups
takes the same list and admits anyone in either."""
import pytest

from nomad import partitions as P
from nomad import preemption as pre
from nomad.condo import config as ccfg

REPORT = """[report.labs]
mail_domain = "example.edu"

[report.clusters.hpc1.condos.chennovak]
owner = ["tchen", "lnovak"]
members = "group:tchen$, lnovak$"

[report.clusters.hpc1.condos.ames]
owner = "rames"
members = "group:rames$"
"""


def _rows(chennovak_groups="ALL"):
    text = [f"PartitionName=chennovak AllowGroups={chennovak_groups} AllowAccounts=ALL Nodes=nd60 Default=NO "
            "MaxTime=UNLIMITED PriorityTier=1 PreemptMode=OFF State=UP GraceTime=0",
            "PartitionName=ames AllowGroups=ALL AllowAccounts=ALL Nodes=nd[55-58] Default=NO MaxTime=UNLIMITED "
            "PriorityTier=1 PreemptMode=OFF State=UP GraceTime=0",
            "PartitionName=condos AllowGroups=ALL AllowAccounts=ALL Nodes=nd[50-61] Default=NO MaxTime=UNLIMITED "
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
    rep = _report(tmp_path).cluster("hpc1")
    assert rep.condos["chennovak"].members == "group:tchen$,lnovak$"            # spaces dropped
    assert ccfg.member_groups(rep.condos["chennovak"].members) == ["tchen$", "lnovak$"]
    assert ccfg.member_groups("group:rames$") == ["rames$"]
    assert ccfg.member_groups("account:ames") == [] and ccfg.member_groups(["tchen"]) == []
    for bad in ("group:tchen$,,lnovak$", "group:tchen$,tchen$", "group:,"):
        with pytest.raises(ccfg.CondoConfigError):
            _report(tmp_path, REPORT.replace('"group:tchen$, lnovak$"', f'"{bad}"'))


def test_the_plan_admits_both_groups(tmp_path):
    rep = _report(tmp_path).cluster("hpc1")
    off = {"PreemptType": "(null)", "PreemptMode": "OFF", "JobRequeue": "1"}
    res = pre.check(_rows(), off, rep, pre.Settings())
    yn = next(c for c in res["condos"] if c.name == "chennovak")
    assert yn.groups == ["tchen$", "lnovak$"]
    assert yn.changes == [("chennovak", "AllowGroups=tchen$,lnovak$ PriorityTier=2 PreemptMode=OFF")]
    assert "chennovak admits anyone, not only tchen$,lnovak$ (report.toml's members)" in yn.problems
    plan = pre.plan_text(res, pre.Settings(), "hpc1")
    assert "  PartitionName=chennovak ...  AllowGroups=tchen$,lnovak$ PriorityTier=2 PreemptMode=OFF" in plan
    assert any(x.endswith("(the rest as on the condos line)") for x in plan)
    # Slurm lists them in another order: the same groups, nothing to change
    res = pre.check(_rows("lnovak$,tchen$"), off, rep, pre.Settings())
    yn = next(c for c in res["condos"] if c.name == "chennovak")
    assert not any(ch.startswith("AllowGroups") for _p, ch in yn.changes)
    assert not any("admits" in x for x in yn.problems)


def test_the_lab_is_both_groups_people_and_on_both_labs_pages(tmp_path):
    from nomad.condo.figures import resolve_members
    from nomad.facts.condos import owns
    c = _report(tmp_path).cluster("hpc1").condos["chennovak"]
    groups = {"tchen$": {"tchen", "am8ht"}, "lnovak$": {"lnovak"}}
    people, how = resolve_members(None, c, None, lambda g: groups.get(g, set()), lambda g: True)
    assert people == {"tchen", "am8ht", "lnovak"} and how == "groups tchen$, lnovak$"
    one = _report(tmp_path).cluster("hpc1").condos["ames"]
    assert resolve_members(None, one, None, lambda g: {"rames", "s1"}, lambda g: True) == \
        ({"rames", "s1"}, "group rames$")
    assert owns(c, "tchen$", set()) and owns(c, "lnovak$", set()) and not owns(c, "rames$", set())

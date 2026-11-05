"""1.28.0: guests on condos, and the owners' priority there. spydur's
partitions as Slurm 25.11.5 lists them on 5 Nov 2026 (every one AllowGroups=ALL,
PriorityTier 1, PreemptMode OFF; condos spdr[50-61]); the plan João chose:
Slurm preempts guests in condo_overflow (REQUEUE, 30 min's grace), the owners'
partitions at a higher tier admitting their lab's group (cparish$)."""
import sqlite3
from datetime import datetime, timedelta

from click.testing import CliRunner

from nomad import partitions as P
from nomad import preemption as pre
from nomad.condo import config as ccfg

LINES = {
    "all": "Nodes=spdr[01-15] Default=NO",
    "basic": "Nodes=spdr[01-08] Default=YES",
    "bukach": "Nodes=spdr50 Default=NO",
    "dias": "Nodes=spdr[51-52] Default=NO",
    "parish": "Nodes=spdr[55-58] Default=NO",
    "yang1": "Nodes=spdr59 Default=NO",
    "condos": "Nodes=spdr[50-61] Default=NO",
}
REPORT = """[report.labs]
mail_domain = "richmond.edu"

[report.clusters.spydur]
guest_partitions = ["condos"]

[report.clusters.spydur.condos.parish]
owner = "cparish"
members = "group:cparish$"

[report.clusters.spydur.condos.dias]
owner = "adias"

[report.clusters.spydur.condos.yang1]
owner = "myang"
members = "group:myang$"
"""
OFF = {"PreemptType": "(null)", "PreemptMode": "OFF", "JobRequeue": "1", "PreemptExemptTime": "00:00:00"}
ON = {"PreemptType": "preempt/partition_prio", "PreemptMode": "REQUEUE", "JobRequeue": "1"}


def _rows(changes=None):
    text = []
    for name, rest in LINES.items():
        line = f"PartitionName={name} AllowGroups=ALL AllowAccounts=ALL {rest} MaxTime=UNLIMITED PriorityTier=1 " \
               "PreemptMode=OFF State=UP GraceTime=0"
        for a, b in (changes or {}).get(name, ()):
            line = line.replace(a, b)
        text.append(line)
    rows = P.parse("\n".join(text))
    for r in rows:
        r["site"] = None
    return rows


ON_CHANGES = {"parish": [("AllowGroups=ALL", "AllowGroups=cparish$"), ("PriorityTier=1", "PriorityTier=2")],
              "dias": [("PriorityTier=1", "PriorityTier=2")],
              "yang1": [("AllowGroups=ALL", "AllowGroups=myang$"), ("PriorityTier=1", "PriorityTier=2")],
              "condos": [("PartitionName=condos", "PartitionName=condo_overflow"),
                         ("PreemptMode=OFF", "PreemptMode=REQUEUE"), ("GraceTime=0", "GraceTime=1800")]}


def _report(tmp_path):
    path = tmp_path / "report.toml"
    path.write_text(REPORT)
    return ccfg.load(path)


def test_slurms_own_settings_are_read_and_kept():
    text = ("Configuration data as of 2026-11-05T10:00:00\nPreemptMode             = OFF\n"
            "PreemptParameters       = (null)\nPreemptType             = (null)\nPreemptExemptTime       = 00:00:00\n"
            "JobRequeue              = 1\nSLURM_VERSION           = 25.11.5\n")
    assert pre.parse_config(text) == OFF
    assert not pre.is_on(OFF) and pre.is_on(ON) and not pre.is_on({"PreemptType": "preempt/none"})
    conn = sqlite3.connect(":memory:")
    now = datetime(2026, 11, 5, 10)
    assert pre.store_config(conn, "spydur", OFF, now) == 4
    assert pre.store_config(conn, "spydur", OFF, now + timedelta(minutes=5)) == 0       # nothing new
    assert pre.store_config(conn, "spydur", dict(OFF, PreemptType="preempt/partition_prio"),
                            now + timedelta(minutes=10)) == 1
    assert pre.config_of(conn)["PreemptType"] == "preempt/partition_prio"


def test_spydur_today_the_owners_have_no_first_claim(tmp_path):
    rep = _report(tmp_path).cluster("spydur")
    res = pre.check(_rows(), OFF, rep, pre.Settings())
    by = {c.name: c for c in res["condos"]}
    assert not res["on"] and res["global_changes"] == ["PreemptType=preempt/partition_prio", "PreemptMode=REQUEUE"]
    assert by["parish"].problems == [
        "parish admits anyone, not only cparish$ (report.toml's members)",
        "parish is at PriorityTier 1: nothing ranks the owners first"]
    assert by["parish"].changes == [("parish", "AllowGroups=cparish$ PriorityTier=2 PreemptMode=OFF")]
    assert [w for _q, _t, w in by["parish"].others] == ["the guests' partition: preempted once the plan is on"]
    # dias: no group to keep it to the lab: not ranked first (anyone could preempt the guests from it)
    assert any("name no group or account" in p for p in by["dias"].problems)
    assert by["dias"].changes == [("dias", "PreemptMode=OFF")]
    assert set(res["others_off"]) == {"all", "basic", "bukach"}              # Slurm's REQUEUE: not on them
    g = res["guest"]
    assert not g["exists"] and g["now"] == ["condos"] and g["set"]["Nodes"] == "spdr[50-61]"
    plan = pre.plan_text(res, pre.Settings(), "spydur")
    assert "  PartitionName=parish ...  AllowGroups=cparish$ PriorityTier=2 PreemptMode=OFF" in plan
    assert "  PartitionName=all ...  PreemptMode=OFF" in plan
    assert ("  add PartitionName=condo_overflow Nodes=spdr[50-61] AllowGroups=ALL PriorityTier=1 PreemptMode=REQUEUE "
            "GraceTime=1800 ...   (the rest as on the condos line)") in plan
    assert any(x.startswith("  PartitionName=condos ...  State=DRAIN PreemptMode=REQUEUE GraceTime=1800") for x in plan)
    off = plan[plan.index("To turn it back off (as scontrol shows them now; a setting the line didn't have: remove it):"):]
    assert "  PartitionName=parish ...  AllowGroups=ALL PriorityTier=1 PreemptMode=OFF   (as now)" in off
    assert "  remove the PreemptType and PreemptMode lines (as now: no preemption)" in off
    assert "  PartitionName=condos ...  State=UP PreemptMode=OFF GraceTime=0   (as now; add the line back if it was " \
           "removed)" in off
    assert not any("PriorityTier=2" in x for x in plan if x.startswith("  PartitionName=dias"))
    # the condo reports find the guests through report.toml's guest_partitions: both while condos drains
    assert any(x.startswith('  report.toml, [report.clusters.spydur]: guest_partitions = ["condos", "condo_overflow"]'
                            "   (while condos drains") for x in plan)
    # GraceTime 30 min against the reports' 5 min guarantee to owners: said
    res = pre.check(_rows(), OFF, rep, pre.Settings(), owner_start_minutes=5)
    assert any("owner_start_minutes is 5" in n for n in res["notes"])
    assert not any("owner_start_minutes" in n for n in pre.check(_rows(), OFF, rep, pre.Settings(grace_minutes=5),
                                                                  owner_start_minutes=5)["notes"])


def test_condos_stays_a_guests_door_while_condo_overflow_takes_over(tmp_path):
    rep = _report(tmp_path).cluster("spydur")
    lines = dict(LINES, condo_overflow="Nodes=spdr[50-61] Default=NO")
    changes = {k: v for k, v in ON_CHANGES.items() if k != "condos"}
    changes["condo_overflow"] = [("PreemptMode=OFF", "PreemptMode=REQUEUE"), ("GraceTime=0", "GraceTime=1800")]

    def rows(extra):
        text = []
        for name, rest in lines.items():
            line = f"PartitionName={name} AllowGroups=ALL AllowAccounts=ALL {rest} MaxTime=UNLIMITED PriorityTier=1 " \
                   "PreemptMode=OFF State=UP GraceTime=0"
            for a, b in (changes.get(name, []) + extra.get(name, [])):
                line = line.replace(a, b)
            text.append(line)
        out = P.parse("\n".join(text))
        for r in out:
            r["site"] = None
        return out
    # both there, condos still up: a guests' door (no "isn't a guests' partition"), drained in the plan
    res = pre.check(rows({}), ON, rep, pre.Settings())
    by = {c.name: c for c in res["condos"]}
    assert not any("isn't a guests'" in x for x in by["parish"].problems)
    assert res["guest"]["exists"] and res["guest"]["now"] == ["condos"] and "condos" not in res["others_off"]
    plan = pre.plan_text(res, pre.Settings(), "spydur")
    assert any(x.startswith("  PartitionName=condos ...  State=DRAIN PreemptMode=REQUEUE GraceTime=1800") for x in plan)
    # condos draining, preempted as condo_overflow is: only the note to remove it once empty
    res = pre.check(rows({"condos": [("State=UP", "State=DRAIN"), ("PreemptMode=OFF", "PreemptMode=REQUEUE"),
                                     ("GraceTime=0", "GraceTime=1800")]}), ON, rep, pre.Settings())
    assert res["guest"]["now"] == [] and res["guest"]["draining"] == ["condos"]
    assert any(n.startswith("condos is draining: remove its line once squeue -p condos is empty") for n in res["notes"])
    assert not any("PartitionName=condos" in x for x in pre.plan_text(res, pre.Settings(), "spydur"))


def test_an_account_keeps_a_condo_to_its_lab_only_where_slurm_enforces_it(tmp_path):
    path = tmp_path / "report.toml"
    path.write_text(REPORT.replace('[report.clusters.spydur.condos.dias]\nowner = "adias"',
                                   '[report.clusters.spydur.condos.dias]\nowner = "adias"\naccount = "dias"'))
    rep = ccfg.load(path).cluster("spydur")
    assert pre.member_account(rep.condos["dias"]) == "dias"           # the condo's own account
    res = pre.check(_rows(), OFF, rep, pre.Settings())
    dias = next(c for c in res["condos"] if c.name == "dias")
    assert any("doesn't enforce associations" in x for x in dias.problems)
    assert not any("PriorityTier=2" in ch for _p, ch in dias.changes)
    res = pre.check(_rows(), dict(OFF, AccountingStorageEnforce="associations,limits"), rep, pre.Settings())
    dias = next(c for c in res["condos"] if c.name == "dias")
    assert dias.changes == [("dias", "AllowAccounts=dias PriorityTier=2 PreemptMode=OFF")]


def test_once_it_is_on_parish_is_ok_and_guests_are_told(tmp_path):
    rep = _report(tmp_path).cluster("spydur")
    rows = _rows(ON_CHANGES)
    res = pre.check(rows, ON, rep, pre.Settings())
    by = {c.name: c for c in res["condos"]}
    assert by["parish"].problems == [] and by["yang1"].problems == []
    assert [w for _q, _t, w in by["parish"].others] == ["preempted there: requeue, after up to 30 min"]
    assert res["guest"]["exists"] and res["guest"]["set"] == {}
    g = next(r for r in rows if r["partition"] == "condo_overflow")
    assert pre.preempts(g, rows, ON) == {"mode": "REQUEUE", "grace_s": 1800, "requeue": True}
    assert pre.preempts(g, rows, OFF) is None                  # Slurm's switch off: nobody is preempted
    parish = next(r for r in rows if r["partition"] == "parish")
    assert pre.preempts(parish, rows, ON) is None              # the owners' own jobs: never
    from nomad.submit import where as W
    cands = [{"partition": "condo_overflow", "role": "guest", "allowed": True, "why": "", "nodes": 12}]
    got = W.rank(cands, {"starts": [{"partition": "condo_overflow", "soon": True}]}, W.Settings(),
                 preempted={"condo_overflow": pre.preempts(g, rows, ON)})
    row = got["rows"][0]
    assert row["preempt"]["grace_s"] == 1800
    assert row["why"] == ("the owners can take its nodes back: Slurm then stops the job after up to 30 min and puts "
                          "it back in the queue (it starts over unless it resumes from a checkpoint)")
    # a guest row with slow nodes says both
    got = W.rank(cands, {"starts": [{"partition": "condo_overflow", "soon": True}]},
                 W.Settings(), preempted={"condo_overflow": pre.preempts(g, rows, ON)},
                 slow={"condo_overflow": [("spdr55", {"mhz": 400, "ratio": 0.1})]})
    assert "stops the job after up to 30 min" in got["rows"][0]["why"] and "1 of its 12 nodes runs throttled" in \
        got["rows"][0]["why"]


def test_whether_a_script_survives_a_requeue():
    from nomad.submit import requeue as R
    gmx = "#!/bin/bash\n#SBATCH --partition=condo_overflow\nmodule load gromacs\ngmx mdrun -deffnm md\n"
    kinds = [(n["kind"], n["text"][:30]) for n in R.notes(gmx, "GROMACS")]
    assert ("loses", "GROMACS starts over from step ") in kinds and ("loses", "its Slurm output file is writt") in kinds
    assert R.summary(R.notes(gmx)) == "it starts over if it is requeued"
    ok = gmx.replace("-deffnm md", "-deffnm md -cpi md.cpt").replace("#!/bin/bash\n",
                                                                       "#!/bin/bash\n#SBATCH --open-mode=append\n")
    assert R.summary(R.notes(ok)) == "it survives being requeued"
    bad = "#!/bin/bash\n#SBATCH --no-requeue\nset -euo pipefail\nmkdir run\ncd run\nmv ../in.dat .\n./prog\n"
    texts = " ".join(n["text"] for n in R.notes(bad))
    assert "--no-requeue" in texts and "mkdir (no -p)" in texts and "rm, mv" in texts
    assert R.summary(R.notes(bad)) == "it may not survive being requeued"
    # cleanup after the program, a guarded mkdir, mkdir --parents: fine; errexit spelled otherwise: seen
    tidy = ("#!/bin/bash\n#SBATCH --open-mode=append\nset -Eeuo pipefail\n[ -d out ] || mkdir out\n"
            "mkdir --parents logs\ngmx mdrun -deffnm md -cpi md.cpt\nrm -f tmp*\nmv md.log out/\n")
    assert R.summary(R.notes(tidy)) == "it survives being requeued" and not [n for n in R.notes(tidy)
                                                                           if n["kind"] in ("check", "stops")]
    for errexit in ("set -o errexit", "set -Eeu"):
        assert any("mkdir (no -p)" in n["text"] for n in R.notes(f"#!/bin/bash\n{errexit}\nmkdir out\n./prog\n"))
    assert any("mkdir (no -p)" in n["text"] for n in R.notes("#!/bin/bash -e\nmkdir out\n./prog\n"))
    # --open-mode truncate (with a space) is not append; an echo of SLURM_RESTART_COUNT doesn't resume
    assert any("written anew" in n["text"] for n in R.notes("#!/bin/bash\n#SBATCH --open-mode truncate\n./x\n"))
    echo = R.notes("#!/bin/bash\n#SBATCH --open-mode=append\necho restarts: $SLURM_RESTART_COUNT\npython3 t.py\n")
    assert R.summary(echo) == "it starts over if it is requeued"
    assert "local scratch" in " ".join(n["text"] for n in R.notes("#!/bin/bash\ncd /localscratch/$USER\n./prog\n"))
    vasp = "#!/bin/bash\n[ -s CONTCAR ] && cp CONTCAR POSCAR\nmpirun vasp_std\n"
    assert not [n for n in R.notes(vasp) if "VASP" in n["text"]]
    generic = R.notes("#!/bin/bash\npython3 train.py\n")
    assert any("SLURM_RESTART_COUNT" in n["text"] for n in generic)
    aware = R.notes("#!/bin/bash\n#SBATCH --open-mode=append\nif [ \"${SLURM_RESTART_COUNT:-0}\" -gt 0 ]; then "
                    "python3 train.py --resume; else python3 train.py; fi\n")
    assert R.summary(aware) == "it survives being requeued"
    head = "#!/bin/bash\n#SBATCH --open-mode=append\n"
    # a test of something else that then echoes the count: not a restart; a copy of it tested: one
    assert R.summary(R.notes(head + "if [ -f x ]; then echo $SLURM_RESTART_COUNT; fi\npython3 t.py\n")) == \
        "it starts over if it is requeued"
    assert R.summary(R.notes(head + "N=${SLURM_RESTART_COUNT:-0}\nif [ $N -gt 0 ]; then python3 t.py -r; fi\n")) == \
        "it survives being requeued"
    assert R.summary(R.notes(head + "if (( SLURM_RESTART_COUNT > 0 )); then python3 t.py -r; fi\n")) == \
        "it survives being requeued"
    # module load python3 isn't the program: an rm after it and before python3 is still before it
    kinds = [n["kind"] for n in R.notes("#!/bin/bash\nmodule load python3\nrm -f in.dat\npython3 run.py\n")]
    assert "check" in kinds
    assert "check" not in [n["kind"] for n in R.notes("#!/bin/bash\nmodule load python3\npython3 run.py\nrm t\n")]
    # mkdir -m 700 -p, if ! mkdir: fine under set -e
    for ok_mkdir in ("mkdir -m 700 -p run", "if ! mkdir run; then exit 1; fi", "mkdir -vp run"):
        assert not any("mkdir (no -p)" in n["text"] for n in R.notes(f"#!/bin/bash\nset -e\n{ok_mkdir}\n./prog\n"))


def test_the_jobs_slurm_preempted_are_kept_once(tmp_path, monkeypatch):
    import subprocess

    from nomad.collectors import preemptions as C
    sep = C.SEP
    out = "\n".join(sep.join(x) for x in (
        ("4815162", "md-run", "amy", "condo_overflow", "PREEMPTED", "2026-11-04T09:00:00", "2026-11-04T12:30:00",
         "03:30:00", "spdr55"),
        ("777", "sim", "amy", "condo_overflow", "PREEMPTED", "2026-11-04T08:00:00", "2026-11-04T12:30:00",
         "04:30:00", "spdr56"),
        ("4815999", "x", "bob", "condo_overflow", "REQUEUED", "2026-11-05T09:00:00", "2026-11-05T09:10:00",
         "00:10:00", "spdr57"),
        ("bad;id", "x", "eve", "condo_overflow", "PREEMPTED", "t", "t", "00:01:00", "spdr50")))
    # its instances: 4815162 came back (pending again), 777 didn't (--no-requeue: cancelled)
    runs = "\n".join(sep.join(x) for x in (("4815162", "PREEMPTED", "2026-11-04T09:00:00"),
                                              ("4815162", "PENDING", "Unknown"),
                                              ("777", "PREEMPTED", "2026-11-04T04:00:00"),     # an earlier run's
                                              ("777", "PREEMPTED", "2026-11-04T08:00:00")))
    asked = []

    def run(argv, **kw):
        asked.append(argv)
        return subprocess.CompletedProcess(argv, 0, (runs if "-j" in argv else out) + "\n", "")
    monkeypatch.setattr(C.subprocess, "run", run)
    monkeypatch.setattr(C, "find_tool", lambda name: f"/usr/bin/{name}")
    c = C.PreemptionsCollector({}, tmp_path / "db")
    data = c.collect()
    assert [(d["job_id"], d["state"], d["requeued"]) for d in data] == [("4815162", "PREEMPTED", 1),
                                                                          ("777", "PREEMPTED", 0)]
    # ended a minute ago, nothing back yet: not known (Slurm may not have requeued it yet)
    fresh = [dict(data[1], requeued=None)]
    c._came_back("/usr/bin/sacct", fresh, now=datetime(2026, 11, 4, 12, 31))
    assert fresh[0]["requeued"] is None
    c._came_back("/usr/bin/sacct", fresh, now=datetime(2026, 11, 4, 13, 0))
    assert fresh[0]["requeued"] == 0
    assert "-D" in asked[0] and "PR" in asked[0] and "now-2hours" in asked[0]
    assert asked[1][asked[1].index("-j") + 1] == "4815162,777"
    c.store(data)
    c.store(data)
    assert sqlite3.connect(tmp_path / "db").execute("SELECT COUNT(*) FROM job_preemptions").fetchone()[0] == 2


def _hub(tmp_path, cfg, changes):
    db = sqlite3.connect(tmp_path / "combined.db")
    P.ensure(db)
    db.execute("ALTER TABLE partition_settings ADD COLUMN source_site TEXT")
    P.store(db, "spydur", _rows(changes))
    db.execute("UPDATE partition_settings SET source_site = 'spydur'")
    pre.store_config(db, "spydur", cfg)
    db.execute("ALTER TABLE slurm_settings ADD COLUMN source_site TEXT")
    db.execute("UPDATE slurm_settings SET source_site = 'spydur'")
    from nomad.collectors.preemptions import PreemptionsCollector
    db.executescript(PreemptionsCollector.SCHEMA.replace("PRIMARY KEY (job_id, start)",
                                                         "source_site TEXT, PRIMARY KEY (job_id, start)"))
    db.commit()
    return tmp_path / "combined.db"


def _preempted(hub, job, user, part, state="PREEMPTED", requeued=1, start="2026-11-04T09:00:00"):
    db = sqlite3.connect(hub)
    db.execute("INSERT INTO job_preemptions (job_id, job_name, user, partition, state, start, end, elapsed, nodes, "
               "first_seen, requeued, source_site) VALUES (?, 'md-run', ?, ?, ?, ?, '2026-11-04T12:30:00', "
               "'03:30:00', 'spdr55', ?, ?, 'spydur')",
               (job, user, part, state, start, datetime.now().isoformat(timespec="seconds"), requeued))
    db.commit()


def test_the_person_is_told_once_where_slurm_preempts(tmp_path):
    from nomad.alerts import hub_preemptions as H
    hub = _hub(tmp_path, ON, ON_CHANGES)
    _preempted(hub, "100", "amy", "condo_overflow", start="2026-11-03T09:00:00")          # before notices were on
    sent = []
    cfg = {"submit": {"preemption": {"notices": True}}}
    assert H.hub_preemption_notices(hub, tmp_path, config={}, send=sent.append, domain="richmond.edu") == []
    # the first run only notes what is there
    assert H.hub_preemption_notices(hub, tmp_path, config=cfg, send=sent.append, domain="richmond.edu") == []
    assert sent == []
    _preempted(hub, "4815162", "amy", "condo_overflow")
    _preempted(hub, "500", "bob", "basic")                                   # nobody preempts in basic
    _preempted(hub, "501", "bob", "condo_overflow", state="REQUEUED")        # an admin's requeue: not this
    keys = H.hub_preemption_notices(hub, tmp_path, config=cfg, send=sent.append, domain="richmond.edu")
    assert keys == ["spydur:4815162:2026-11-04T09:00:00"] and len(sent) == 1
    m = sent[0]
    assert m["To"] == "amy@richmond.edu"
    assert m["Subject"] == "Your job 4815162 (md-run) on spydur was stopped for a condo's owners"
    body = m.get_content()
    assert "ran in condo_overflow on spydur, on another lab's condo nodes (spdr55), for 3 h 30 min" in body
    assert "after a grace of up to 30 minutes and put it back in the queue" in body
    assert "nomad size --requeue job.sh" in body
    assert H.hub_preemption_notices(hub, tmp_path, config=cfg, send=sent.append, domain="richmond.edu") == []
    # an array's 30 tasks, the last cancelled: one message, the cancelled one first and counted
    for i in range(30):
        _preempted(hub, f"900_{i}", "amy", "condo_overflow", requeued=0 if i == 29 else 1)
    keys = H.hub_preemption_notices(hub, tmp_path, config=cfg, send=sent.append, domain="richmond.edu")
    assert len(keys) == 30 and len(sent) == 2
    assert sent[1]["Subject"] == "30 of your jobs on spydur were stopped for a condo's owners"
    body = sent[1].get_content()
    assert "1 cancelled (send it again when you are ready); 29 put back in the queue." in body
    assert body.index("900_29") < body.index("900_0 ") and "cancelled it" in body and "... and 10 more" in body
    # ended moments ago, not known to be back: it waits a sync
    now = datetime.now()
    db = sqlite3.connect(hub)
    db.execute("INSERT INTO job_preemptions (job_id, user, partition, state, start, end, first_seen, requeued, "
               "source_site) VALUES ('950', 'amy', 'condo_overflow', 'PREEMPTED', '2026-11-05T09:00:00', ?, ?, NULL, "
               "'spydur')", ((now - timedelta(minutes=2)).isoformat(timespec="seconds"),
                             now.isoformat(timespec="seconds")))
    db.commit()
    assert H.hub_preemption_notices(hub, tmp_path, config=cfg, send=sent.append, domain="richmond.edu") == []
    assert H.hub_preemption_notices(hub, tmp_path, config=cfg, send=sent.append, domain="richmond.edu",
                                    now=now + timedelta(minutes=30)) == ["spydur:950:2026-11-05T09:00:00"]
    # notices off for three days (the ledger last written then): the next run only notes what is there
    import json
    ledger = tmp_path / H.LEDGER
    old = json.loads(ledger.read_text())
    ledger.write_text(json.dumps(dict(old, _last_run=(now - timedelta(days=3)).isoformat(timespec="seconds"))))
    _preempted(hub, "960", "amy", "condo_overflow")
    n = len(sent)
    assert H.hub_preemption_notices(hub, tmp_path, config=cfg, send=sent.append, domain="richmond.edu") == []
    assert len(sent) == n and "spydur:960:2026-11-04T09:00:00" in json.loads(ledger.read_text())
    _preempted(hub, "961", "amy", "condo_overflow")
    assert H.hub_preemption_notices(hub, tmp_path, config=cfg, send=sent.append,
                                    domain="richmond.edu") == ["spydur:961:2026-11-04T09:00:00"]
    # Slurm's switch off: nothing is preempted there, nothing said
    (tmp_path / "x").mkdir()
    hub2 = _hub(tmp_path / "x", OFF, {})
    assert H.hub_preemption_notices(hub2, tmp_path / "x", config=cfg, send=sent.append, domain="richmond.edu") == []


def test_nomad_partitions_condos_says_it(tmp_path):
    from nomad.cli import cli
    hub = _hub(tmp_path, OFF, {})
    toml = tmp_path / "nomad.toml"
    toml.write_text("")
    (tmp_path / "report.toml").write_text(REPORT)
    r = CliRunner().invoke(cli, ["-c", str(toml), "partitions", "--site", "spydur", "--condos", "--db", str(hub)])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "spydur: Slurm preempts nothing (PreemptType (null), PreemptMode OFF, JobRequeue 1)" in out
    assert "parish" in out and "! parish admits anyone, not only cparish$" in out
    assert "condo_overflow: not one of Slurm's partitions yet (today the guests' door is condos)" in out
    assert "To turn it on at spydur" in out and "PartitionName=parish ...  AllowGroups=cparish$ PriorityTier=2" in out
    r = CliRunner().invoke(cli, ["-c", str(toml), "partitions", "--site", "spydur", "--condos", "--db", str(hub),
                                 "--mask"])
    assert r.exit_code != 0 and "no --mask" in r.output

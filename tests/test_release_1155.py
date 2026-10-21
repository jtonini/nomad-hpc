"""nomad 1.15.5: what is left to set up for the condo reports
(`nomad usage-report condos status`, `nomad config check`), the monthly
timer, and the questions `condos init --apply` asks in a terminal."""
import sqlite3
from datetime import datetime

import pytest
from click.testing import CliRunner

from nomad.condo import config as ccfg, status as cstatus
from tests.test_condo import NOW, REPORT_TOML, _every, _mini, build  # noqa: F401

NOMAD_TOML = '[console.labs]\ngroup_pattern = "{netid}$"\n'


@pytest.fixture
def hub(tmp_path):
    db = tmp_path / "combined.db"
    build(db).close()
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(NOMAD_TOML)
    rt = tmp_path / "report.toml"
    rt.write_text(REPORT_TOML)
    return db, cfg, rt


def _check(db, rt, **kw):
    conn = sqlite3.connect(db)
    try:
        kw.setdefault("timer", "enabled")
        kw.setdefault("pdf", True)
        return cstatus.check(conn, ccfg.load(rt), {}, **kw)
    finally:
        conn.close()


def kinds(items):
    return {i.kind for i in items}


def test_what_is_left_names_each_thing_and_says_how(hub):
    db, _, rt = hub
    items = _check(db, rt)
    by = {i.kind: i for i in items}
    assert by["unconfirmed"].names == ["jones_lab"] and by["unconfirmed"].level == "todo"
    assert "--confirm jones_lab" in by["unconfirmed"].how
    assert by["reviewing"].level == "note" and "rc@example.edu" in by["reviewing"].text
    assert "new" not in by and "gone" not in by and "timer" not in by
    # The masked words carry no condo or owner names.
    for i in items:
        for name in ("jones_lab", "smith", "jsmith", "rjones", "rc@example.edu"):
            assert name not in i.masked


def test_a_condo_on_the_nodes_not_listed_and_one_listed_that_is_gone(hub):
    db, _, rt = hub
    rt.write_text(REPORT_TOML.replace("[report.clusters.hpc1.condos.jones_lab]",
                                      "[report.clusters.hpc1.condos.lee]\npartition = \"lee\"\n"
                                      "[report.clusters.hpc1.condos.old_jones]"))
    items = _check(db, rt)
    by = {i.kind: i for i in items}
    assert by["new"].names == ["jones_lab"]
    assert sorted(by["gone"].names) == ["lee", "old_jones"]
    # old_jones isn't confirmed, but it's gone: said once, as gone.
    assert "unconfirmed" not in by


def test_a_cluster_whose_condos_are_not_drafted_yet(hub):
    db, _, rt = hub
    rt.write_text('[report.labs]\nmail_domain = "example.edu"\n')
    by = {i.kind: i for i in _check(db, rt)}
    assert by["not_configured"].cluster == "hpc1"
    assert sorted(by["not_configured"].names) == ["jones_lab", "smith"]
    assert by["not_configured"].how == "nomad usage-report condos init --apply"


def test_reports_that_could_not_go_out(hub):
    db, _, rt = hub
    rt.write_text(REPORT_TOML.replace('mail_domain = "example.edu"\n', "")
                  .replace('send_from = "Research Computing <rc@example.edu>"\n', "")
                  .replace('formats = ["html", "json", "md"]', 'formats = ["html", "pdf"]'))
    by = {i.kind: i for i in _check(db, rt, timer="absent", pdf=False)}
    assert by["no_address"].names == ["hpc1/smith"] and by["no_address"].level == "todo"
    assert by["no_sender"].level == "todo"
    assert by["timer"].level == "todo" and "isn't installed" in by["timer"].text
    assert by["no_pdf"].level == "note"
    conn = sqlite3.connect(db)
    try:
        items = cstatus.check(conn, ccfg.load(rt), {"mail": {"from": "rc@example.edu"}},
                              timer="enabled", pdf=True)
    finally:
        conn.close()
    assert "no_sender" not in kinds(items)


def test_a_site_without_condos_has_nothing_to_set_up(tmp_path):
    c = _mini(":memory:", _every(datetime(2026, 9, 1), datetime(2026, 9, 2), nodes=("n1", "n2")))
    items = cstatus.check(c, ccfg.Settings(), {}, timer="absent", pdf=False)
    assert items == []
    # A report.toml that doesn't read is said even then.
    items = cstatus.check(c, ccfg.Settings(), {}, problem="reveiw_to: no such setting", timer=None)
    assert kinds(items) == {"report_toml"} and "reveiw_to" not in items[0].masked


def test_the_timer_state_from_systemctl(monkeypatch):
    import subprocess
    monkeypatch.setattr(cstatus.shutil, "which", lambda name: "/bin/" + name)

    def answer(word, code=0, err=""):
        return lambda *a, **k: subprocess.CompletedProcess(a, code, word + "\n", err)
    for word, state in (("enabled", "enabled"), ("disabled", "disabled"), ("not-found", "absent")):
        monkeypatch.setattr(cstatus.subprocess, "run", answer(word))
        assert cstatus.timer_state() == state
    monkeypatch.setattr(cstatus.subprocess, "run", answer("", 1, "Failed to get unit file state: No such file"))
    assert cstatus.timer_state() == "absent"
    monkeypatch.setattr(cstatus.shutil, "which", lambda name: None)
    assert cstatus.timer_state() is None


def test_status_and_config_check(hub, monkeypatch):
    from nomad.cli import cli
    db, cfg, rt = hub
    monkeypatch.setattr(cstatus, "timer_state", lambda: "absent")
    args = ["-c", str(cfg), "usage-report", "condos", "status", "--db", str(db), "--config", str(rt)]
    r = CliRunner().invoke(cli, args)
    assert r.exit_code == 0, r.output
    assert "owner not confirmed: jones_lab" in r.output and "--confirm jones_lab" in r.output
    assert "the monthly timer isn't installed" in r.output and "to do" in r.output
    r = CliRunner().invoke(cli, args + ["--mask"])
    assert r.exit_code == 0 and "jones_lab" not in r.output and "rc@example.edu" not in r.output
    # config check: counts only, and where the details are. report.toml
    # beside nomad.toml, the hub's database where [hub] says.
    cfg.write_text(NOMAD_TOML + f'[hub]\noutput_db = "{db}"\n'
                   '[[hub.sites]]\nname = "hpc1"\nhost = "head1"\ndb_path = "/x/hpc1.db"\n')
    (cfg.parent / "report.toml").write_text(REPORT_TOML)
    r = CliRunner().invoke(cli, ["-c", str(cfg), "config", "check"])
    assert r.exit_code == 0, r.output
    assert "condo reports: 2 to do (details and what to run: nomad usage-report condos status)" in r.output
    assert "jones_lab" not in r.output and "smith" not in r.output


def test_the_timer_matches_the_units_set_up_by_hand():
    from nomad.cli import _timer_units
    service, timer = _timer_units("/opt/nomad/venv/bin/nomad", "/etc/nomad/nomad.toml", "nomad", "*-*-01 07:00")
    assert service == ("[Unit]\nDescription=NOMAD condo reports\nAfter=network-online.target\n\n"
                       "[Service]\nType=oneshot\nUser=nomad\n"
                       "ExecStart=/opt/nomad/venv/bin/nomad -c /etc/nomad/nomad.toml usage-report condos --send\n")
    assert timer == ("[Unit]\nDescription=NOMAD condo reports, on the 1st\n\n"
                     "[Timer]\nOnCalendar=*-*-01 07:00\nPersistent=true\n\n"
                     "[Install]\nWantedBy=timers.target\n")


def test_timer_prints_without_apply_and_needs_root_to_install(hub, monkeypatch):
    import os
    from nomad.cli import cli
    _, cfg, _ = hub
    monkeypatch.setattr("shutil.which", lambda name: None)      # no systemd-analyze, no systemctl
    r = CliRunner().invoke(cli, ["-c", str(cfg), "usage-report", "condos", "timer", "--user", "root"])
    assert r.exit_code == 0, r.output
    assert "# /etc/systemd/system/nomad-condo-reports.timer" in r.output
    assert f"-c {cfg.resolve()} usage-report condos --send" in r.output and "User=root" in r.output
    r = CliRunner().invoke(cli, ["-c", str(cfg), "usage-report", "condos", "timer", "--user", "nosuchaccount"])
    assert r.exit_code != 0 and "no account nosuchaccount" in r.output
    r = CliRunner().invoke(cli, ["-c", str(cfg), "usage-report", "condos", "timer", "--at", "x\nUser=root"])
    assert r.exit_code != 0 and "one line" in r.output
    monkeypatch.setattr("shutil.which", lambda name: "/bin/" + name if name == "systemctl" else None)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    r = CliRunner().invoke(cli, ["-c", str(cfg), "usage-report", "condos", "timer", "--user", "root", "--apply"])
    assert r.exit_code != 0 and "as root" in r.output


def test_init_asks_how_reports_go_out_in_a_terminal(hub, monkeypatch):
    from nomad import cli as ncli
    db, cfg, rt = hub
    rt.write_text("")
    answers = iter(["example.edu", "rc@example.edu, help@example.edu", "", "Questions: rc@example.edu"])
    monkeypatch.setattr(ncli.click, "prompt", lambda *a, **k: next(answers))
    monkeypatch.setattr(ncli.click, "confirm", lambda *a, **k: True)
    ncli._ask_delivery(rt)
    d = ccfg.load(rt).delivery
    assert d.mail_domain == "example.edu" and d.review_to == ["rc@example.edu", "help@example.edu"]
    assert d.contact == "Questions: rc@example.edu" and d.send_from is None
    # Asked again, nothing is missing but the sender: one question.
    asked = []
    monkeypatch.setattr(ncli.click, "prompt", lambda words, **k: asked.append(words) or "")
    ncli._ask_delivery(rt)
    assert len(asked) == 1 and asked[0].strip().startswith("Sender")
    # Not a terminal (CliRunner): no questions, and the next steps.
    r = CliRunner().invoke(ncli.cli, ["-c", str(cfg), "usage-report", "condos", "init", "--db", str(db),
                                      "--config", str(rt), "--apply"])
    assert r.exit_code == 0, r.output
    assert "condos timer --apply" in r.output and "condos status" in r.output


def test_an_owners_report_says_which_jobs_ended_there(hub):
    from nomad.condo import content, figures
    db, _, rt = hub
    conn = sqlite3.connect(db)
    try:
        data = figures.build(conn, ccfg.load(rt), "2026-09", nomad_config={}, now=NOW)
    finally:
        conn.close()
    r = next(x for x in data["reports"] if x["condo"]["name"] == "smith")
    words = " ".join(getattr(b, "text", "") or "" for b in content.owner_doc(r, ccfg.load(rt).delivery))
    assert "sent to smith or to any other partition that includes them" in words


# --- what the review of 1.15.5 found ----------------------------------------------

def test_config_check_survives_a_database_it_cannot_read(hub, monkeypatch):
    from nomad.cli import cli
    db, cfg, _ = hub
    cfg.write_text(NOMAD_TOML + f'[hub]\noutput_db = "{db}"\n'
                   '[[hub.sites]]\nname = "hpc1"\nhost = "head1"\ndb_path = "/x/hpc1.db"\n')
    (cfg.parent / "report.toml").write_text(REPORT_TOML)
    import nomad.usage.sources as sources

    def denied(path):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(sources, "open_db", denied)
    r = CliRunner().invoke(cli, ["-c", str(cfg), "config", "check"])
    assert r.exit_code == 0, r.output
    assert "condo reports not checked" in r.output and "sudo -u nomad" in r.output
    r = CliRunner().invoke(cli, ["-c", str(cfg), "usage-report", "condos", "status"])
    assert r.exit_code == 1 and "can't be read here" in r.output and "Traceback" not in r.output


def test_config_check_on_a_site_says_nothing_of_condos(tmp_path):
    from nomad.cli import cli
    c = _mini(str(tmp_path / "site.db"), _every(datetime(2026, 9, 1), datetime(2026, 9, 2)))
    c.close()
    cfg = tmp_path / "nomad.toml"
    cfg.write_text(f'[database]\npath = "{tmp_path / "site.db"}"\n')
    r = CliRunner().invoke(cli, ["-c", str(cfg), "config", "check"])
    assert r.exit_code == 0, r.output
    assert "condo" not in r.output


def test_a_sender_under_another_name_in_mail(hub):
    db, _, rt = hub
    rt.write_text(REPORT_TOML.replace('send_from = "Research Computing <rc@example.edu>"\n', ""))
    conn = sqlite3.connect(db)
    try:
        items = cstatus.check(conn, ccfg.load(rt), {"mail": {"from_address": "rc@example.edu"}},
                              timer="enabled", pdf=True)
    finally:
        conn.close()
    assert "no_sender" not in kinds(items)


def test_a_condo_without_an_owner_is_not_called_unconfirmed(hub):
    db, _, rt = hub
    rt.write_text(REPORT_TOML.replace('owner = "rjones"\n', ""))
    by = {i.kind: i for i in _check(db, rt)}
    assert by["no_owner"].names == ["jones_lab"] and "--owner jones_lab=NETID" in by["no_owner"].how
    assert "unconfirmed" not in by


def test_no_node_readings_is_said_once(tmp_path, hub):
    _, _, rt = hub
    empty = sqlite3.connect(tmp_path / "empty.db")
    items = cstatus.check(empty, ccfg.load(rt), {}, timer="enabled", pdf=True)
    assert [i.kind for i in items if i.level == "todo"] == ["no_readings"]
    assert "jones_lab" not in items[0].masked


def test_the_timer_refuses_paths_a_unit_would_read_otherwise(hub, monkeypatch):
    from nomad.cli import cli
    _, cfg, _ = hub
    spaced = cfg.parent / "my settings"
    spaced.mkdir()
    (spaced / "nomad.toml").write_text(NOMAD_TOML)
    monkeypatch.setattr("shutil.which", lambda name: None)
    r = CliRunner().invoke(cli, ["-c", str(spaced / "nomad.toml"), "usage-report", "condos", "timer",
                                 "--user", "root"])
    assert r.exit_code != 0 and "read otherwise" in r.output
    r = CliRunner().invoke(cli, ["-c", str(cfg), "usage-report", "condos", "timer", "--user", "root;x"])
    assert r.exit_code != 0


def test_a_timer_set_up_by_hand_with_the_same_settings_is_the_same():
    from nomad.cli import _timer_units, _unit_settings
    service, timer = _timer_units("/opt/nomad/venv/bin/nomad", "/etc/nomad/nomad.toml", "nomad", "*-*-01 07:00")
    by_hand = timer.replace("Description=NOMAD condo reports, on the 1st", "Description=NØMAÐ condo reports")
    assert _unit_settings(by_hand) == _unit_settings(timer)
    other = timer.replace("07:00", "06:00")
    assert _unit_settings(other) != _unit_settings(timer)
    assert _unit_settings(service.replace("After=network-online.target\n", "")) == _unit_settings(service)


def test_one_wrong_answer_is_asked_again(hub, monkeypatch):
    from nomad import cli as ncli
    _, _, rt = hub
    rt.write_text("")
    answers = iter(["example.edu", "rc@example.edu help@example.edu", "rc@example.edu", "", ""])
    said = []
    monkeypatch.setattr(ncli.click, "prompt", lambda *a, **k: next(answers))
    monkeypatch.setattr(ncli.click, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ncli.click, "echo", lambda *a, **k: said.append(" ".join(map(str, a))))
    ncli._ask_delivery(rt)
    d = ccfg.load(rt).delivery
    assert d.mail_domain == "example.edu" and d.review_to == ["rc@example.edu"]
    assert any("again, or Enter" in x for x in said)


# --- nomad init's condo step --------------------------------------------------------

PARTS = {"basic": {"nodes": ["n01", "n02"]}, "condos": {"nodes": ["c01", "c02", "c03", "c04"]},
         "smith": {"nodes": ["c01", "c02"]}, "jones_lab": {"nodes": ["c03", "c04"]}}


def _scripted(answers, yes=True):
    it = iter(answers)
    return (lambda *a, **k: next(it)), (lambda *a, **k: yes), (lambda *a, **k: None)


def test_the_condos_from_the_wizards_partitions():
    from nomad.condo import wizard
    assert wizard.guest_guess(PARTS) == "condos"
    assert wizard.find_condos(PARTS, "condos") == {"jones_lab": {"c03", "c04"}, "smith": {"c01", "c02"}}
    assert wizard.find_condos(PARTS, "basic") == {}


def test_declining_or_no_slurm_cluster_registers_nothing():
    from nomad.condo import wizard
    p, c, e = _scripted([], yes=False)
    assert wizard.ask([{"name": "c1", "type": "hpc", "partitions": PARTS}], p, c, e) is None
    p, c, e = _scripted([])
    assert wizard.ask([{"name": "ws", "type": "workstations", "partitions": {"bio": {"nodes": ["w1"]}}}],
                      p, c, e) is None


def test_the_answers_become_report_toml(tmp_path):
    from nomad.condo import wizard
    p, c, e = _scripted(["nosuch", "condos", "j smith;", "jsmith, rjones", "", "example", "example.edu",
                         "rc@example.edu"])
    plan = wizard.ask([{"name": "c1", "type": "hpc", "partitions": PARTS}], p, c, e)
    (cp,) = plan.clusters
    assert cp.guest == "condos" and cp.condos == {"jones_lab": ["jsmith", "rjones"], "smith": []}
    assert plan.labs == {"mail_domain": "example.edu", "review_to": ["rc@example.edu"]}
    path = tmp_path / "report.toml"
    said = []
    wizard.write(path, plan, True, said.append)
    assert not path.exists() and any("[report.clusters.c1.condos.smith]" in x for x in said)
    wizard.write(path, plan, False, said.append)
    s = ccfg.load(path)
    jl, sm = s.clusters["c1"].condos["jones_lab"], s.clusters["c1"].condos["smith"]
    assert jl.owners == ["jsmith", "rjones"] and jl.confirmed and not sm.owner and not sm.confirmed
    assert s.clusters["c1"].guest_partitions == ["condos"] and s.delivery.mail_domain == "example.edu"
    assert path.stat().st_mode & 0o777 == 0o600


def test_report_toml_already_there_keeps_what_it_has(tmp_path):
    from nomad.condo import wizard
    path = tmp_path / "report.toml"
    path.write_text('[report.labs]\ncontact = "Questions: rc@example.edu"\n\n'
                    '[report.clusters.c1]\nlabel = "Cluster One"\n')
    plan = wizard.Plan(clusters=[wizard.ClusterPlan("c1", "condos", {"smith": ["jsmith"]})],
                       labs={"mail_domain": "example.edu", "contact": "something else"})
    said = []
    wizard.write(path, plan, True, said.append)                 # dry run: says the labs too
    assert any("mail_domain = example.edu" in x for x in said) and "smith" not in path.read_text()
    wizard.write(path, plan, False, said.append)
    s = ccfg.load(path)
    assert s.delivery.mail_domain == "example.edu" and s.delivery.contact == "Questions: rc@example.edu"
    assert s.clusters["c1"].label == "Cluster One" and s.clusters["c1"].condos["smith"].confirmed
    assert any("c1's table" in x for x in said)
    assert list(tmp_path.glob("report.toml.bak-*"))


def test_condos_already_there_are_left(tmp_path):
    from nomad.condo import wizard
    path = tmp_path / "report.toml"
    path.write_text('[report.clusters.c2.condos.lee]\nowner = "klee"\n')
    said = []
    wizard.write(path, wizard.Plan(clusters=[wizard.ClusterPlan("c2", "condos", {"other": []})]),
                 False, said.append)
    assert list(ccfg.load(path).clusters["c2"].condos) == ["lee"]
    assert any("c2's condos" in x for x in said)


def test_two_clusters_on_one_machine_go_together(tmp_path):
    """One site database is one cluster to the reports."""
    from nomad.condo import discover, wizard
    path = tmp_path / "report.toml"
    plan = wizard.Plan(clusters=[wizard.ClusterPlan("c1", "condos", {"smith": ["jsmith"]}),
                                 wizard.ClusterPlan("c2", "condo2", {"jones_lab": [], "smith": ["other"]})])
    said = []
    wizard.write(path, plan, False, said.append)
    s = ccfg.load(path)
    (name,) = s.clusters
    assert name == discover.LOCAL and discover.local_name(s, {"clusters": {"c1": {}, "c2": {}}}) == name
    cc = s.clusters[name]
    assert cc.guest_partitions == ["condos", "condo2"] and sorted(cc.condos) == ["jones_lab", "smith"]
    assert cc.condos["smith"].owners == ["jsmith"]
    assert any("two clusters" in x for x in said) and any("go together" in x for x in said)


def test_a_report_toml_using_this_cluster_keeps_that_name(tmp_path):
    from nomad.condo import discover, wizard
    path = tmp_path / "report.toml"
    path.write_text('[report.clusters.this-cluster]\nlabel = "Here"\n')
    wizard.write(path, wizard.Plan(clusters=[wizard.ClusterPlan("c1", "condos", {"smith": ["jsmith"]})]),
                 False, lambda *a: None)
    s = ccfg.load(path)
    assert list(s.clusters) == [discover.LOCAL] and s.clusters[discover.LOCAL].condos["smith"].confirmed


def test_an_empty_report_toml_is_written_to(tmp_path):
    from nomad.condo import wizard
    path = tmp_path / "report.toml"
    path.write_text("")
    wizard.write(path, wizard.Plan(clusters=[wizard.ClusterPlan("c1", "condos", {"smith": []})]),
                 False, lambda *a: None)
    assert "smith" in ccfg.load(path).clusters["c1"].condos


def test_owners_typed_twice_count_once():
    from nomad.condo import wizard
    p, c, e = _scripted(["condos", "jsmith,jsmith", "", "", ""])
    plan = wizard.ask([{"name": "c1", "type": "hpc", "partitions": PARTS}], p, c, e)
    assert plan.clusters[0].condos["jones_lab"] == ["jsmith"]


def test_written_as_root_the_nomad_account_can_read_it(tmp_path, monkeypatch):
    import grp
    import os
    import pwd
    from nomad.condo import wizard
    if os.geteuid() != 0:
        pytest.skip("needs root")
    gid = os.getgid()
    real = pwd.getpwnam
    monkeypatch.setattr(pwd, "getpwnam", lambda n: (type("P", (), {"pw_gid": gid, "pw_uid": 4242})()
                                                    if n == "nomad" else real(n)))
    path = tmp_path / "report.toml"
    said = []
    wizard.write(path, wizard.Plan(clusters=[wizard.ClusterPlan("c1", "condos", {"smith": []})]),
                 False, said.append)
    st = path.stat()
    assert st.st_mode & 0o777 == 0o640 and st.st_gid == gid
    assert any(grp.getgrgid(gid).gr_name in x for x in said)


def test_the_timer_account_must_reach_the_settings(tmp_path):
    import os
    from nomad.cli import _readable_by
    if os.geteuid() != 0:
        pytest.skip("needs root to make folders for another account")
    closed = tmp_path / "closed"
    closed.mkdir()
    f = closed / "nomad.toml"
    f.write_text("x = 1\n")
    f.chmod(0o644)
    closed.chmod(0o700)
    tmp_path.chmod(0o755)
    assert _readable_by(f, "root") is True
    assert _readable_by(f, "nobody") is False
    closed.chmod(0o755)
    assert _readable_by(f, "nobody") is (all(p.stat().st_mode & 0o001 for p in f.resolve().parents))


def test_nomad_init_asks_about_condos(tmp_path, monkeypatch):
    """The whole wizard (--dry-run), on a cluster whose Slurm is a script."""
    import os
    import nomad.condo.wizard as wizard
    from nomad.cli import cli
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "sinfo").write_text(
        "#!/bin/sh\ncase \"$*\" in\n"
        "  \"-a -h -o %P\") printf 'basic*\\ncondos\\nsmith\\njones_lab\\n' ;;\n"
        "  \"-h -p basic -o %n\") printf 'n01\\nn02\\n' ;;\n"
        "  \"-h -p condos -o %n\") printf 'c01\\nc02\\nc03\\nc04\\n' ;;\n"
        "  \"-h -p smith -o %n\") printf 'c01\\nc02\\n' ;;\n"
        "  \"-h -p jones_lab -o %n\") printf 'c03\\nc04\\n' ;;\n"
        "  *) exit 1 ;;\nesac\n")
    (bin_dir / "sinfo").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("HOME", str(tmp_path))
    real = wizard.ask
    asked = {}

    def scripted(clusters, prompt, confirm, echo):
        asked["clusters"] = clusters
        p, c, _ = _scripted(["condos", "rjones", "jsmith", "example.edu", ""])
        return real(clusters, p, c, echo)
    monkeypatch.setattr(wizard, "ask", scripted)
    from click.testing import CliRunner
    r = CliRunner().invoke(cli, ["init", "--dry-run", "--force"], input="\n" * 200)
    assert r.exit_code == 0, r.output[-2000:]
    assert "Step 5: Condo reports" in r.output
    assert sorted(asked["clusters"][0]["partitions"]) == ["basic", "condos", "jones_lab", "smith"]
    assert "[report.clusters.cluster-1.condos.smith]" in r.output and 'owner = "jsmith"' in r.output


def test_a_sites_own_database_goes_by_its_nomad_toml_name(tmp_path):
    """What `nomad init` writes on a site is what its own reports read."""
    from nomad.condo import discover, figures, wizard
    c = _mini(str(tmp_path / "site.db"), _every(datetime(2026, 9, 1), datetime(2026, 10, 1), minutes=60),
              site=False)
    plan = wizard.Plan(clusters=[wizard.ClusterPlan("cluster-1", "condos", {"own": ["jsmith"]})])
    rt = tmp_path / "report.toml"
    wizard.write(rt, plan, False, lambda *a: None)
    s = ccfg.load(rt)
    cfg = {"clusters": {"cluster-1": {"name": "Cluster 1"}}}
    assert discover.local_name(s, cfg) == "cluster-1"
    assert discover.local_name(s, {}) == discover.LOCAL
    data = figures.build(c, s, "2026-09", nomad_config=cfg, now=NOW)
    (cl,) = data["clusters"]
    assert cl["name"] == "cluster-1" and [r["label"] for r in cl["condos"]] == ["own"]
    items = cstatus.check(c, s, cfg, timer="enabled", pdf=True)
    assert "gone" not in kinds(items) and "not_configured" not in kinds(items)

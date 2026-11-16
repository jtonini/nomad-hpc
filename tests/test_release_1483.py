"""nomad 1.48.3: policy documents for the Console's Help > Policies; the term
reports' files and words."""
import sqlite3
import sys

import pytest
from click.testing import CliRunner

from nomad.config import policies as pol
from nomad.config.edit import TomlEdit, loads
from nomad.condo import config as ccfg, term

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from test_term_reports import FALL, NOMAD_CONFIG, NOW, _cli, build  # noqa: E402


@pytest.fixture
def docs(tmp_path):
    d = tmp_path / "policies"
    d.mkdir()
    (d / "usage.pdf").write_bytes(b"%PDF-1.4 a policy\n")
    (d / "storage.md").write_text("# Storage\n\nKeep what you need.\n")
    return d


def test_the_list_as_the_console_reads_it(docs):
    cfg = {"console": {"policies": [
        {"title": "Usage policy", "file": str(docs / "usage.pdf"), "about": "Who may use the clusters",
         "updated": "2026-09-01"},
        {"title": "Usage policy", "file": str(docs / "storage.md")},           # the same title: its own id
        {"title": "Storage policy", "url": "https://example.edu/storage"},
        {"title": "Gone", "file": str(docs / "gone.pdf")},
        {"title": "", "url": "https://example.edu/x"},
        {"title": "Both", "file": str(docs / "usage.pdf"), "url": "https://example.edu/y"},
        {"title": "Word", "file": str(docs / "usage.docx")},
        {"title": "Script", "url": "javascript:alert(1)"},
        {"title": "Odd", "url": "https://example.edu/z", "colour": "red", "updated": "soon"},
    ]}}
    items, problems = pol.read(cfg)
    assert [(p.id, p.kind) for p in items] == [("usage-policy", "pdf"), ("usage-policy-2", "markdown"),
                                                ("storage-policy", "link"), ("gone", "pdf"), ("odd", "link")]
    usage, md, link, gone, odd = items
    assert usage.updated == "2026-09-01" and usage.media_type == "application/pdf" and usage.problem is None
    assert md.updated and len(md.updated) == 10                 # the file's date
    assert gone.problem == "no such file"
    assert odd.updated is None
    assert usage.public() == {"id": "usage-policy", "title": "Usage policy", "about": "Who may use the clusters",
                              "kind": "pdf", "url": None, "updated": "2026-09-01"}
    said = "\n".join(problems)
    for bit in ("policies]] 5: no title; left out", "'Both': give a file or a url", "usage.docx: a .pdf, .html, .md or .txt",
                "'Script': 'javascript:alert(1)'", "'Odd': colour: unknown; ignored", "'Odd': updated = 'soon'"):
        assert bit in said
    assert pol.read({}) == ([], [])
    assert pol.read({"console": {"policies": "x"}})[1]


def test_a_file_too_big_or_unreadable(docs, monkeypatch):
    monkeypatch.setattr(pol, "MAX_BYTES", 4)
    assert pol.file_problem(str(docs / "usage.pdf")).startswith("larger than")
    monkeypatch.setattr(pol, "MAX_BYTES", 25 * 1024 * 1024)
    assert pol.file_problem(str(docs)) == "not a file"
    assert pol.slug("Política de uso — 2026!") == "politica-de-uso-2026"


def _toml(tmp_path, text='[general]\ncluster_name = "demo"\n\n[console]\nsubmit = "staff"\n'):
    p = tmp_path / "nomad.toml"
    p.write_text(text)
    return p


def test_set_policy_adds_changes_and_takes_off(tmp_path, docs):
    path = _toml(tmp_path)
    body = pol.entry("Usage policy", file=str(docs / "usage.pdf"), about="Who  may\nuse it", updated="2026-09-01")
    assert body["about"] == "Who may use it"
    edit = TomlEdit(path)
    assert pol.set_policy(edit, body, "Usage policy")
    edit.save(pol.check_for("Usage policy", body), tag="policy", keep=pol.BACKUPS)
    data = loads(path.read_text())
    assert data["console"]["submit"] == "staff" and data["console"]["policies"] == [body]
    assert pol.set_policy(TomlEdit(path), body, "Usage policy") == []         # the same again: nothing
    other = pol.entry("Usage policy", url="https://example.edu/usage")
    edit = TomlEdit(path)
    assert pol.set_policy(edit, other, "Usage policy")[0].startswith("changed:")
    edit.save(pol.check_for("Usage policy", other), tag="policy")
    assert loads(path.read_text())["console"]["policies"] == [other]
    edit = TomlEdit(path)
    assert pol.set_policy(edit, None, "Usage policy")
    edit.save(pol.check_for("Usage policy", None), tag="policy")
    assert "policies" not in loads(path.read_text())["console"]
    assert pol.set_policy(TomlEdit(path), None, "Usage policy") == []
    for bad in ({"file": "rel.pdf"}, {"url": "ftp://x"}, {}, {"file": "/a.pdf", "url": "https://x.edu"}):
        with pytest.raises(pol.PolicyError):
            pol.entry("T", **bad)
    inline = _toml(tmp_path, '[console]\npolicies = [{ title = "A", url = "https://x.edu" }]\n')
    with pytest.raises(pol.PolicyError, match="inline"):
        pol.set_policy(TomlEdit(inline), other, "Usage policy")


def test_the_commands(tmp_path, docs):
    from nomad.cli import cli
    path = _toml(tmp_path)
    run = lambda *a: CliRunner().invoke(cli, ["-c", str(path), "console", *a], catch_exceptions=False)  # noqa: E731
    r = run("policies")
    assert "no [[console.policies]]" in r.output
    r = run("policy", "Usage policy", "--file", str(docs / "usage.pdf"))
    assert "title = \"Usage policy\"" in r.output and "Nothing changed" in r.output
    assert "policies" not in loads(path.read_text())["console"]
    r = run("policy", "Usage policy", "--file", str(docs / "usage.pdf"), "--updated", "2026-09-01", "--apply")
    assert "Written. Backup:" in r.output and "no restart" in r.output
    r = run("policy", "Usage policy", "--file", str(docs / "usage.pdf"), "--updated", "2026-09-01", "--apply")
    assert "there already; nothing to change" in r.output
    r = run("policy", "Storage policy", "--url", "https://example.edu/storage", "--apply")
    r = run("policies")
    assert "2 in Help > Policies" in r.output and "Usage policy: pdf," in r.output
    assert "Storage policy: a link, https://example.edu/storage" in r.output
    r = run("policy", "Gone", "--file", str(docs / "gone.pdf"))
    assert "no such file; the Console will say so" in r.output
    r = run("policy", "Usage policy", "--remove", "--about", "x")
    assert r.exit_code != 0 and "--remove takes only the title" in r.output
    r = run("policy", "Usage policy", "--remove", "--apply")
    assert "taken off" in r.output
    assert [e["title"] for e in loads(path.read_text())["console"]["policies"]] == ["Storage policy"]
    assert len(list(tmp_path.glob("nomad.toml.bak-policy-*"))) == 3


def test_hours_with_their_unit():
    assert term.hours_with(0.04, "core-hour") == "under 1 core-hour"
    assert term.hours_with(1.2, "GPU-hour") == "1 GPU-hour"
    assert term.hours_with(0, "GPU-hour") == "0 GPU-hours"
    assert term.hours_with(208806, "core-hour") == "208,806 core-hours"


def test_a_quiet_labs_earlier_files_go(tmp_path, monkeypatch):
    db = tmp_path / "combined.db"
    c = build(db)
    # quinn$ ran a job: it gets a report in the first run, none once the job is gone.
    c.execute("INSERT INTO jobs (job_id, user_name, partition, node_list, submit_time, start_time, end_time, "
              "state, req_cpus, req_mem_mb, req_gpus, runtime_seconds, account, alloc_tres, source_site) "
              "VALUES ('9', 'quinn', 'basic', 'n01', '2026-10-05T00:00:00', '2026-10-05T00:00:00', "
              "'2026-10-05T02:00:00', 'COMPLETED', 1, 1000, 0, 7200, 'default', 'cpu=1,mem=1000M,node=1', 'hpc1')")
    c.commit()
    c.close()
    out = tmp_path / "o"
    r = _cli(db, tmp_path, "--out", str(out), monkeypatch=monkeypatch)
    assert (out / "quinn" / "2026-08-25_2027-01-10.html").exists() and "removed" not in r.output
    other_term = out / "quinn" / "2026-05-10_2026-08-25.html"
    other_term.write_text("the summer's")
    c = sqlite3.connect(db)
    c.execute("DELETE FROM jobs WHERE job_id = '9'")
    c.commit()
    c.close()
    # A run for one lab leaves the others' files alone.
    r = _cli(db, tmp_path, "--out", str(out), "--lab", "kim$", monkeypatch=monkeypatch)
    assert (out / "quinn" / "2026-08-25_2027-01-10.html").exists()
    r = _cli(db, tmp_path, "--out", str(out), monkeypatch=monkeypatch)
    assert "removed this term's earlier files of 1 lab with no report now" in r.output
    assert not list((out / "quinn").glob("2026-08-25_2027-01-10.*")) and other_term.exists()
    assert (out / "kim" / "2026-08-25_2027-01-10.html").exists()
    assert (out / "summary" / "2026-08-25_2027-01-10.html").exists()
    s = ccfg.load(tmp_path / "report.toml")
    conn = sqlite3.connect(db)
    try:
        d = term.build(conn, s, FALL[0], FALL[1], name=FALL[2], nomad_config=NOMAD_CONFIG, now=NOW)
    finally:
        conn.close()
    assert "quinn$" not in {f["lab"]["group"] for f in d["reports"]}
    assert term.clear_stale(tmp_path / "nowhere", "k", []) == []

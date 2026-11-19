"""1.57.1: `nomad licenses add` without --no-renew lists a license as being
renewed, whatever click reads a flag whose value is False as (click 8.3
read it as False when it wasn't given, so every license was added as not
being renewed, and none was ever reminded). Made-up names only."""
from click.testing import CliRunner

from nomad import licenses as L
from nomad.config.edit import loads


def _items(cfg):
    return {it.name: it for it in L.items_of(loads(cfg.read_text())["collectors"]["licenses"])[0]}


def test_added_without_no_renew_is_renewed(tmp_path):
    from nomad.cli import cli
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[general]\ndata_dir = "%s"\n' % tmp_path)
    prog = tmp_path / "license-manager"
    prog.write_text("")
    run = CliRunner()
    for args in (["Chem", "date", "--ends", "2099-06-30"],
                 ["Old", "date", "--ends", "never", "--no-renew"],
                 ["Posit", "posit", "--program", str(prog)],
                 ["Posit here", "posit", "--program", str(prog), "--no-sudo"]):
        out = run.invoke(cli, ["-c", str(cfg), "licenses", "add", *args, "--apply"])
        assert out.exit_code == 0, out.output
    items = _items(cfg)
    assert items["Chem"].renew and not items["Old"].renew
    assert items["Posit"].sudo and not items["Posit here"].sudo
    text = cfg.read_text()
    assert text.count("renew = false") == 1 and text.count("sudo = false") == 1


def test_an_entry_added_as_not_renewing_by_mistake_is_put_right_with_replace(tmp_path):
    from nomad.cli import cli
    cfg = tmp_path / "nomad.toml"
    cfg.write_text('[general]\ndata_dir = "%s"\n[collectors.licenses]\nenabled = true\n\n'
                   '[[collectors.licenses.items]]\nname = "Chem"\nkind = "date"\nends = "2099-06-30"\n'
                   'renew = false\n' % tmp_path)
    run = CliRunner()
    args = ["-c", str(cfg), "licenses", "add", "Chem", "date", "--ends", "2099-06-30", "--apply"]
    assert "--replace" in run.invoke(cli, args).output
    out = run.invoke(cli, args + ["--replace"])
    assert out.exit_code == 0 and "Replaced Chem" in out.output, out.output
    assert _items(cfg)["Chem"].renew and "renew = false" not in cfg.read_text()
    assert "listed already" in run.invoke(cli, args + ["--replace"]).output

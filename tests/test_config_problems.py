# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A nomad.toml that can't be read is said so, by file and line, wherever it
matters -- not skipped in silence. A key set twice in [console.labs] once
made the whole file unreadable, and with it every lab and PI, while every
command and the Console carried on as if there were none."""

import pytest
from click.testing import CliRunner

import nomad.config as nc
from nomad.cli import cli
from nomad.config.problems import ConfigError, describe, excerpt_lines

@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """A home of its own: the hourly warning stamp lives under ~/.cache."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    return h


TWICE = ('[general]\nx = 1\n\n[console.labs]\nleads = { jdoe = ["pi1$"] }\n'
         'group_pattern = "{netid}$"\nleads = {}\n')


def write(tmp_path, text, name="nomad.toml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def problem(tmp_path, text) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        nc.read_toml(write(tmp_path, text))
    return info.value


def test_a_key_set_twice_names_both_lines_and_the_table(tmp_path):
    e = problem(tmp_path, TWICE)
    assert (e.line, e.earlier) == (7, 5)
    assert e.short == ("line 7: `leads` is set twice in [console.labs] (first on line 5); "
                       "keep one of them")
    assert str(e).startswith(str(tmp_path / "nomad.toml") + ", line 7: ")
    assert excerpt_lines(e) == ['    5 | leads = { jdoe = ["pi1$"] }', '  > 7 | leads = {}']
    assert isinstance(e, ValueError)          # callers catching ValueError still do


def test_a_table_declared_twice(tmp_path):
    e = problem(tmp_path, '[console.labs]\nx = 1\n[other]\ny = 2\n[console.labs]\nz = 3\n')
    assert e.line == 5 and e.earlier == 1
    assert "[console.labs] appears twice (first on line 1)" in e.reason


def test_twice_at_the_top_and_a_value_that_becomes_a_table(tmp_path):
    e = problem(tmp_path, 'a = 1\nb = 2\na = 3\n')
    assert e.reason.startswith("`a` is set twice in the top of the file") and e.earlier == 1
    e = problem(tmp_path, 'a = 1\n[a]\nb = 2\n')
    assert e.line == 2 and "[a] is already defined above" in e.reason


def test_an_array_of_tables_may_repeat(tmp_path):
    assert nc.read_toml(write(tmp_path, '[[sites]]\nname = "a"\n[[sites]]\nname = "b"\n'))


def test_other_mistakes_keep_the_parsers_words_and_the_line(tmp_path):
    e = problem(tmp_path, '[console.labs]\ngroup_pattern = {netid}$\n')
    assert e.line == 2 and e.reason and "{netid}" not in e.reason
    assert excerpt_lines(e) == ['  > 2 | group_pattern = {netid}$']


def test_no_value_from_the_file_is_repeated_in_the_reason(tmp_path):
    e = problem(tmp_path, '[mail]\napi_token = "s3cret-value"\napi_token = "other-secret"\n')
    assert "s3cret" not in str(e) and "other-secret" not in str(e)
    # The excerpt is for the file's owner, and hides secret-looking values too.
    assert excerpt_lines(e) == ['    2 | api_token = …', '  > 3 | api_token = …']


def test_the_older_parsers_quoted_contents_are_dropped():
    class Old(Exception):              # the toml package's error, as on Python 3.10
        msg = "What? mail already exists?{'mail': {'password': 'hunter2'}}"
        lineno, colno = 2, 1
    e = describe("n.toml", "[x]\nmail = 1\n", Old())
    assert "hunter2" not in str(e) and e.line == 2


def test_not_text_and_not_openable(tmp_path):
    p = tmp_path / "n.toml"
    p.write_bytes(b'x = "\xff\xfe"\n')
    with pytest.raises(ConfigError, match="is not UTF-8 text"):
        nc.read_toml(p)
    d = tmp_path / "dir.toml"
    d.mkdir()
    e = nc.check_config(d)
    assert isinstance(e, ConfigError) and "can't be opened" in e.reason


def test_check_config(tmp_path, monkeypatch):
    user = tmp_path / "user.toml"
    monkeypatch.setattr(nc, "DEFAULT_CONFIG_PATHS", [user, tmp_path / "etc.toml"])
    assert nc.check_config() is None                     # no file: nothing wrong
    user.write_text('[general]\nx = 1\n')
    assert nc.check_config() is None
    user.write_text(TWICE)
    e = nc.check_config()
    assert e.path == str(user) and e.line == 7
    assert nc.check_config(write(tmp_path, "y = 1\n", "other.toml")) is None


# -- the CLI ------------------------------------------------------------------

def run(*args):
    r = CliRunner().invoke(cli, list(args))
    return r, r.stdout, r.stderr


def test_config_check_says_what_and_where(tmp_path):
    bad = write(tmp_path, TWICE)
    r, out, _ = run("-c", str(bad), "config", "check")
    assert r.exit_code == 1
    assert "can't be read" in out and "line 7: `leads` is set twice" in out
    assert "  > 7 | leads = {}" in out and "none of its settings apply" in out
    good = write(tmp_path, '[console.labs]\ngroup_pattern = "{netid}$"\n', "good.toml")
    r, out, _ = run("-c", str(good), "config", "check")
    assert r.exit_code == 0 and "reads" in out and "sections: console" in out
    r, out, _ = run("-c", str(tmp_path / "none.toml"), "config", "check")
    assert r.exit_code == 0 and "No nomad.toml" in out


def test_every_command_says_so_on_stderr(tmp_path):
    bad = write(tmp_path, TWICE)
    r, out, err = run("-c", str(bad), "sync", "--help")
    assert r.exit_code == 0
    assert "can't read" in err and "line 7" in err and "Running WITHOUT it" in err
    assert "can't read" not in out              # never into captured output
    good = write(tmp_path, "[general]\nx = 1\n", "good.toml")
    assert "can't read" not in run("-c", str(good), "sync", "--help")[2]


def test_lab_commands_refuse_and_change_nothing(tmp_path):
    bad = write(tmp_path, TWICE)
    r, out, err = run("-c", str(bad), "lab", "add-machine", "pi1", "ws1", "--apply")
    assert r.exit_code != 0 and "nothing changed" in err and "line 7" in err
    assert bad.read_text() == TWICE
    r, out, err = run("-c", str(bad), "lab", "show")
    assert r.exit_code != 0 and "no labs can be shown" in err


def test_console_roles_says_the_console_has_no_labs(tmp_path):
    bad = write(tmp_path, TWICE)
    r, out, err = run("-c", str(bad), "console", "roles")
    assert r.exit_code == 0
    assert "can't be read" in out and "nobody is a PI there" in out
    assert "Console access, from nothing" in out
    assert "can't read" not in err             # said once, in its own words


def test_the_same_warning_once_an_hour_off_a_terminal(tmp_path):
    """cron mails every run's output: one mail an hour per problem, not 288 a day."""
    bad = write(tmp_path, TWICE)
    assert "can't read" in run("-c", str(bad), "sync", "--help")[2]
    assert "can't read" not in run("-c", str(bad), "sync", "--help")[2]
    bad.write_text("a = 1\na = 2\n")                 # a different problem: at once
    assert "line 2" in run("-c", str(bad), "sync", "--help")[2]


# -- what the review found -----------------------------------------------------

def test_a_line_that_is_not_toml_is_described_at_once(tmp_path):
    """A nested pattern once took exponential time on a bare word with no =:
    a missing value, or a comment without its #, hung every command."""
    import time
    from nomad.config.problems import _key
    started = time.monotonic()
    assert _key("allowed_groups_for_console_access_by_department_and_lab") is None
    assert _key("TODO add the chemistry lab before friday, and the physics one") is None
    assert _key("x" * 50000) is None
    e = problem(tmp_path, "[console]\nallowed_groups_for_console_access_by_department\n")
    assert e.line == 2
    # Bracketed lines with no closing bracket, many of them, above a duplicate.
    junk = "".join("[" + " " * 990 + "x\n" for _ in range(30))
    e = problem(tmp_path, f'[a]\nnote = """\n{junk}"""\nk = 1\nk = 2\n')
    assert "`k` is set twice in [a]" in e.reason
    assert time.monotonic() - started < 2


def test_values_never_reach_the_reason_on_either_python(tmp_path):
    for text in ('[a]\nbind_password = 4ever-Secret\n', '[a]\nadmins = ["jdoe", 1abc2]\n',
                 '[a]\nurl = http://u:hunter2@host\n'):
        e = problem(tmp_path, text)
        for secret in ("4ever", "1abc2", "hunter2"):
            assert secret not in str(e), (text, str(e))
    class Old(Exception):              # the toml package passes Python's own words on
        msg = "could not convert string to float: '4ever-Secret'"
        lineno, colno = 2, 1
    e = describe("n.toml", "[a]\nx = 4ever-Secret\n", Old())
    assert "4ever" not in str(e) and "text needs quotes" in e.reason


def test_secrets_hidden_in_inline_tables_and_urls(tmp_path):
    e = problem(tmp_path, '[db]\nconn = { user = "u", password = "pw1" }\n'
                          'conn = { user = "v", password = "pw2" }\n'
                          'url = "https://u:pw3@h"\nurl = "x"\n')
    shown = "\n".join(excerpt_lines(e))
    assert "pw1" not in shown and "pw2" not in shown and "conn = …" in shown
    for line in ('webhook_url = "https://hooks.example.edu/services/T0/B0/abc"',
                 'bind_pw = "pw4"'):
        e = problem(tmp_path, f'[x]\n{line}\n{line}\n')
        assert "abc" not in "\n".join(excerpt_lines(e)) and "pw4" not in "\n".join(excerpt_lines(e))


def test_lines_are_counted_as_the_parsers_count_them(tmp_path):
    e = problem(tmp_path, 'x = "a\u2028b"\n[a]\nk = 1\nk = 2\n')
    assert (e.line, e.earlier) == (4, 3) and "set twice in [a]" in e.reason


def test_describing_can_fail_without_hiding_the_error(tmp_path, monkeypatch):
    import nomad.config.problems as pr
    def broken(*a, **k):
        raise IndexError("a bug in describe")
    monkeypatch.setattr(pr, "describe", broken)
    e = problem(tmp_path, TWICE)
    assert e.line == 7 and e.reason

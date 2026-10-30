# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.23: failed sudo/su and new setuid files, read unprivileged. The probe
reads the auth log itself where the collector's account may (e.g. /var/log/secure
group-readable to it), and scans the configured roots for a person's setuid
files -- no root helper, no cron. Same auth_failures/setuid_new findings as the
root path (1.20); the kinds, folding, severities and Console labels already
exist, so this only adds an unprivileged source."""
import os
import tempfile
from datetime import datetime

import pytest

from nomad.collectors import watch_probe as probe
from nomad.collectors.policy_watch import PolicyWatchCollector, config_problems


NOW = datetime(2026, 10, 30, 16, 0, 0)


@pytest.fixture(autouse=True)
def _known_uids():
    probe._UIDS.update({"alice": 1000, "bob": 1001, "carol": 1002, "evil": 1003,
                        "dave": 1004, "root": 0})
    yield


def _auth_log(lines):
    p = tempfile.mktemp()
    with open(p, "w") as f:
        f.write("".join(lines))
    return p


def test_failed_sudo_su_counted_per_account():
    log = _auth_log([
        "Oct 30 15:59:01 spydur sudo:    alice : 1 incorrect password attempt ; USER=root ; COMMAND=/bin/su\n",
        "Oct 30 15:58:00 spydur su: pam_unix(su-l:auth): authentication failure; logname=bob ruser=bob user=root\n",
        "Oct 30 15:57:00 spydur su: FAILED SU (to root) carol on pts/2\n",
    ])
    rows = probe.auth_failures(log, window_hours=24, min_uid=1000, now=NOW)
    got = {r["user"]: int(r["detail"]) for r in rows}
    assert got == {"alice": 1, "bob": 1, "carol": 1}
    assert all(r["kind"] == "auth_failures" and r["processes"] == int(r["detail"]) for r in rows)


def test_su_to_root_counts_the_actor_not_the_target():
    # user=root is the TARGET; the actor (ruser/by) is who tried, and must be counted.
    log = _auth_log(["Oct 30 15:57:00 spydur su: FAILED SU (to root) carol on pts/2\n"])
    rows = probe.auth_failures(log, 24, 1000, NOW)
    assert [r["user"] for r in rows] == ["carol"]


def test_a_command_string_cannot_inject_an_account():
    # a *successful* sudo running echo, whose argument spells a fake failure after COMMAND=
    log = _auth_log([
        "Oct 30 15:56:00 spydur sudo:    alice : USER=root ; COMMAND=/bin/echo evil : authentication failure\n"])
    assert probe.auth_failures(log, 24, 1000, NOW) == []


def test_old_attempts_fall_outside_the_window():
    log = _auth_log([
        "Oct 28 10:00:00 spydur sudo:    dave : 1 incorrect password attempt ; COMMAND=/x\n",
        "Oct 30 15:59:00 spydur sudo:    alice : 1 incorrect password attempt ; COMMAND=/x\n"])
    got = {r["user"] for r in probe.auth_failures(log, 24, 1000, NOW)}
    assert got == {"alice"}


def test_roots_own_failures_are_excluded():
    log = _auth_log(["Oct 30 15:55:00 spydur su: FAILED SU (to x) root on pts/9\n"])
    assert probe.auth_failures(log, 24, 1000, NOW) == []


def test_an_unreadable_or_missing_log_is_silent():
    assert probe.auth_failures("/nonexistent/secure", 24, 1000, NOW) == []
    assert probe.auth_failures("", 24, 1000, NOW) == []


def test_iso_timestamps_are_understood():
    log = _auth_log([
        "2026-10-30T15:59:00.123456-04:00 spydur sudo:    alice : 1 incorrect password attempt ; COMMAND=/x\n"])
    assert [r["user"] for r in probe.auth_failures(log, 24, 1000, NOW)] == ["alice"]


def test_setuid_scan_finds_a_persons_setuid_file():
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "sub"))
    suid = os.path.join(d, "rootkit")
    open(suid, "w").write("x")
    os.chmod(suid, 0o4755)
    plain = os.path.join(d, "sub", "normal")
    open(plain, "w").write("x")
    os.chmod(plain, 0o644)
    rows = probe.setuid_files([d], min_uid=0)            # min_uid 0 so the sandbox owner counts
    names = {r["program"] for r in rows}
    assert "rootkit" in names and "normal" not in names
    assert all(r["kind"] == "setuid_new" for r in rows)


def test_setuid_scan_of_a_missing_root_is_empty():
    assert probe.setuid_files(["/nonexistent"], min_uid=0) == []


# ── the collector: config and probe args ────────────────────────────────────

def test_probe_args_carry_auth_and_setuid():
    c = PolicyWatchCollector({"enabled": True}, ":memory:")
    assert "--auth" in c._args() and "--setuid-roots" not in c._args()   # auth on by default, setuid off
    c2 = PolicyWatchCollector({"enabled": True, "setuid_roots": ["/localscratch", "/scratch"],
                               "auth_window_hours": 48, "auth_log": "/var/log/secure"}, ":memory:")
    a = c2._args()
    assert a[a.index("--auth-log") + 1] == "/var/log/secure"
    assert a[a.index("--auth-window") + 1] == "48"
    assert a[a.index("--setuid-roots") + 1] == "/localscratch,/scratch"
    off = PolicyWatchCollector({"enabled": True, "watch_auth": False}, ":memory:")
    assert "--auth" not in off._args() and "--auth-log" not in off._args()


def test_config_validates_the_privilege_keys():
    ok, info = config_problems({"enabled": True, "watch_auth": True, "auth_window_hours": 24,
                                "setuid_roots": ["/localscratch"], "auth_log": "/var/log/secure"})
    assert ok == []
    assert any(line.startswith("privilege") for line in info)
    bad, _ = config_problems({"auth_window_hours": 0, "setuid_roots": "not a list",
                              "watch_auth": "yes", "auth_log": "/etc/x; rm -rf /"})
    assert any("auth_window_hours" in p for p in bad) and any("setuid_roots" in p for p in bad)
    assert any("watch_auth" in p for p in bad) and any("auth_log" in p for p in bad)
    # a relative setuid root is rejected
    bad2, _ = config_problems({"setuid_roots": ["localscratch"]})
    assert any("setuid_roots" in p and "absolute" in p for p in bad2)

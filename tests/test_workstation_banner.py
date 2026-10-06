# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A workstation whose shell startup file prints a banner on every login,
interactive or not, must not have the banner read as a command's output."""
import socket
import subprocess

import pytest

from nomad.collectors import workstation as ws
from nomad.collectors.workstation import OUTPUT_MARK, _after_mark

BANNER = (" ______\n/ This computer is labws. \\\n ------\n        \\   ^__^\n"
          " GPU INFO\n24564 MiB, 482 MiB, 78.89 W, 32\n")


@pytest.mark.parametrize("out, expected", [
    (f"{BANNER}{OUTPUT_MARK}\n12\n", "12\n"),
    (f"{OUTPUT_MARK}\n12\n", "12\n"),                 # no banner
    (f"{BANNER}{OUTPUT_MARK}\n", ""),                 # a command with no output
    (f"{BANNER}{OUTPUT_MARK}", ""),
    ("12\n", "12\n"),                                 # no mark at all: as it was
    (f"{OUTPUT_MARK}\na\n{OUTPUT_MARK}\nb\n", f"a\n{OUTPUT_MARK}\nb\n"),   # the first mark
])
def test_after_mark(out, expected):
    assert _after_mark(out) == expected


def _fake_run(calls, stdout):
    def run(cmd, *a, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout, "")
    return run


def test_a_remote_command_skips_the_banner(monkeypatch):
    calls = []
    monkeypatch.setattr(ws.subprocess, "run", _fake_run(calls, f"{BANNER}{OUTPUT_MARK}\n24\n"))
    assert ws.run_command("nproc", "labws") == "24"
    assert f"'echo {OUTPUT_MARK}; nproc'" in calls[0]


def test_a_local_command_is_run_as_it_is(monkeypatch):
    calls = []
    monkeypatch.setattr(ws.subprocess, "run", _fake_run(calls, "24\n"))
    assert ws.run_command("nproc", socket.gethostname()) == "24"
    assert calls == ["nproc"]


def test_a_remote_probe_skips_the_banner(monkeypatch):
    calls = []
    rows = '{"mountpoint": "/home"}\n{"mountpoint": "/scratch"}\n'
    monkeypatch.setattr(ws.subprocess, "run", _fake_run(calls, f"{BANNER}{OUTPUT_MARK}\n{rows}"))
    assert ws.run_python_probe("mount_probe", "labws") == rows.strip()
    assert calls[0][-1] == f"echo {OUTPUT_MARK}; python3 -"


def test_a_local_probe_is_run_as_it_is(monkeypatch):
    calls = []
    monkeypatch.setattr(ws.subprocess, "run", _fake_run(calls, '{"a": 1}\n'))
    assert ws.run_python_probe("mount_probe", None) == '{"a": 1}'
    assert calls[0] == ["python3", "-"]


def test_the_mark_reaches_a_real_shell_intact():
    # What the remote shell runs, run here: the mark comes out on its own line
    # before the command's output, after whatever the shell printed first.
    out = subprocess.run(["bash", "-c", f"echo banner; echo {OUTPUT_MARK}; echo 24"],
                         capture_output=True, text=True).stdout
    assert _after_mark(out).strip() == "24"

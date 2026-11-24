# SPDX-License-Identifier: AGPL-3.0-or-later
"""1.58.1: the gate's jobs answer says each job's partition and what it
asked for (cores, GPUs), so the Console's My jobs now can show where
a job runs and what it asked for before nomad's next reading."""
from __future__ import annotations

from nomad.submit import gate
from tests.test_release_1250 import _out, person  # noqa: F401  (person is a fixture)


def test_jobs_say_partition_and_what_was_asked(person, capsys):
    call, said, answers, w = person
    answers["out"] = ("\x1f".join(("93034_6", "dd_sdm", "RUNNING", "3:02", "12:00:00", "1", "nd55",
                                   "/scratch/kim/run", "condos", "8", "gres/gpu:1"))
                      + "\n" + "\x1f".join(("93034_14", "dd_sdm", "PENDING", "0:00", "12:00:00", "1",
                                            "(Priority)", "/scratch/kim/run", "condos", "8", "N/A"))
                      ).encode() + b"\n"
    assert call("jobs") == 0
    jobs = _out(capsys)["jobs"]
    assert jobs[0] == {"job": "93034_6", "name": "dd_sdm", "state": "RUNNING", "time": "3:02", "limit": "12:00:00",
                       "nodes": "1", "where": "nd55", "workdir": "/scratch/kim/run", "partition": "condos",
                       "cpus": "8", "gres": "gres/gpu:1"}
    assert jobs[1]["where"] == "(Priority)" and jobs[1]["gres"] == "N/A"
    # squeue is asked for all of it
    fmt = said[-1][said[-1].index("-o") + 1]
    assert fmt.split("\x1f")[-3:] == ["%P", "%C", "%b"]
    # a line of the first eight only (as before) is still read
    answers["out"] = "\x1f".join(("4242", "my job", "RUNNING", "1:02", "4:00:00", "1", "nd02", "/w")).encode()
    assert call("jobs") == 0
    assert _out(capsys)["jobs"][0]["where"] == "nd02"
    assert gate.JOB_FIELDS[:8] == ("job", "name", "state", "time", "limit", "nodes", "where", "workdir")

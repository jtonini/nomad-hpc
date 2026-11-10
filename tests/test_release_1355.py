# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.35.5: the lab check's email says nothing happens automatically, in two
plain sentences; a PI's name left empty is as if not there, without a note."""
from __future__ import annotations

import pytest

from nomad import lab_check as L
from tests.test_release_1330 import REPORT
from tests.test_release_1354 import check, entry


@pytest.fixture(autouse=True)
def no_accounts(monkeypatch):
    monkeypatch.setattr(L, "_system_name", lambda netid: None)


def test_nothing_happens_automatically():
    s = L.settings(check(names={"cparish": "Carol Parish"}), REPORT)
    for e in (entry(), entry(kind="remind14", claims=2, left=40)):
        body = L._body(e, s)
        assert "\nNothing happens automatically. Our team reviews your answers before making any change.\n" in body
        assert "Nothing changes" not in body and "changes your answers" not in body
        para = next(p for p in body.split("\n\n") if p.startswith("Nothing happens"))
        assert ":" not in para and para.lower().count("change") == 1


def test_a_name_not_filled_in_yet():
    s = L.settings(check(names={"cparish": "Carol Parish", "myang": "", "knolin": "   "}), REPORT)
    assert s.names == (("cparish", "Carol", "Parish"),)
    assert not [p for p in s.problems if "names" in p]
    assert L.name_of("myang", s) == (None, "none") and L.greeting(["myang"], s) == "Dear colleague,"
    # a value that isn't a name is still said
    bad = L.settings(check(names={"cparish": "Carol Parish", "x1": 3, "BAD NETID!": "Ann Lee"}), REPORT)
    assert len([p for p in bad.problems if "names:" in p]) == 2

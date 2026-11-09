# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""1.35.3: the Columbus practice run's recorded run (spydur, 9 November: job
89551), and its log entry reading what runc 7.2.2 really writes."""
from nomad.demos import kits as K


def test_the_columbus_kit_keeps_its_spydur_run():
    k = K.find({}, "columbus-water")
    assert K.recorded_text(k) == ("A run of it on spydur (2026-11-09, columbus/722, partition condos): 1 CPU(s); "
                                  "1 s from start to end; at most 121 MB of memory.")
    got = {x["file"]: x["found"] for x in K.recorded_look(k)}
    assert got["LISTINGS/scfls.sp"][0]["lines"] == ["     total energy =        -76.0253553484"]
    assert got["LISTINGS/ciudgsm.sp"][0]["lines"][-1].startswith(" eci       =    -76.230540087951")
    assert "1.98007125" in got["LISTINGS/propls.ci.drt1.state1.sp"][0]["lines"][0]
    assert any("runc start:" in x for x in got["runls"][0]["lines"])
    assert any("-76.0253553484" in x for x in got["runls"][0]["lines"])
    for f in k.recorded["files"]:
        data = (k.path / f).read_bytes()
        assert b"jtonini" not in data and b"/home/" not in data, f

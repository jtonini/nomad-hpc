#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Write the NAMD practice kits' input (nomad/demos/kits/namd-water and
namd-gpu-water): a box of 1,000 TIP3P water molecules, its PSF, PDB and
CHARMM-form parameters, made here so the kit needs no VMD or psfgen.

    python3 scripts/demos/make_namd_water.py            write them
    python3 scripts/demos/make_namd_water.py --check    and check them with OpenMM (pip install openmm)

The same seed gives the same files.
"""
import argparse
import math
import random
from pathlib import Path

KITS = Path(__file__).resolve().parents[2] / "nomad" / "demos" / "kits"
N = 10                     # waters a side: 1,000
SPACING = 3.104            # angstrom: about 1 g/cm3
BOX = N * SPACING
OH, HOH = 0.9572, math.radians(104.52)
ATOMS = (("OH2", "OT", -0.834, 15.9994), ("H1", "HT", 0.417, 1.0080), ("H2", "HT", 0.417, 1.0080))

PRM = """* TIP3P water in CHARMM form, for a practice run (nomad demos): the CHARMM22 TIP3P model.
*

ATOMS
MASS     1 HT     1.00800
MASS     2 OT    15.99940

BONDS
OT   HT    450.000   0.9572
HT   HT      0.000   1.5139

ANGLES
HT   OT   HT     55.000   104.52

DIHEDRALS

IMPROPER

NONBONDED nbxmod  5 atom cdiel shift vatom vdistance vswitch -
cutnb 14.0 ctofnb 12.0 ctonnb 10.0 eps 1.0 e14fac 1.0 wmin 1.5
OT     0.000000  -0.152100     1.768200
HT     0.000000  -0.046000     0.224500

END
"""


def conf(box: float) -> str:
    c = box / 2
    return f"""# A box of 1,000 water molecules: a practice run (nomad demos).
# Its results mean nothing for research. Minimised, then 100 ps at 300 K.
structure          water.psf
coordinates        water.pdb
paraTypeCharmm     on
parameters         par_water.prm
temperature        300

exclude            scaled1-4
1-4scaling         1.0
cutoff             12.0
switching          on
switchdist         10.0
pairlistdist       14.0

timestep           2.0
rigidBonds         all
nonbondedFreq      1
fullElectFrequency 2
stepspercycle      10

cellBasisVector1   {box:.3f} 0.0 0.0
cellBasisVector2   0.0 {box:.3f} 0.0
cellBasisVector3   0.0 0.0 {box:.3f}
cellOrigin         {c:.3f} {c:.3f} {c:.3f}
wrapAll            on
PME                yes
PMEGridSpacing     1.0

langevin           on
langevinDamping    1
langevinTemp       300
langevinHydrogen   off

outputName         water-md
dcdfreq            5000
xstFreq            5000
outputEnergies     1000
outputTiming       5000

minimize           500
reinitvels         300
run                50000
"""


def rotate(v, m):
    return tuple(sum(m[i][j] * v[j] for j in range(3)) for i in range(3))


def random_rotation(rng):
    # a uniform random rotation, from a random unit quaternion
    u1, u2, u3 = rng.random(), rng.random(), rng.random()
    q = (math.sqrt(1 - u1) * math.sin(2 * math.pi * u2), math.sqrt(1 - u1) * math.cos(2 * math.pi * u2),
         math.sqrt(u1) * math.sin(2 * math.pi * u3), math.sqrt(u1) * math.cos(2 * math.pi * u3))
    x, y, z, w = q
    return ((1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
            (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
            (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)))


def waters(seed=7):
    rng = random.Random(seed)
    h1 = (OH * math.sin(HOH / 2), 0.0, OH * math.cos(HOH / 2))
    h2 = (-OH * math.sin(HOH / 2), 0.0, OH * math.cos(HOH / 2))
    out = []
    for i in range(N):
        for j in range(N):
            for k in range(N):
                o = ((i + 0.5) * SPACING, (j + 0.5) * SPACING, (k + 0.5) * SPACING)
                m = random_rotation(rng)
                out.append([o] + [tuple(o[a] + d[a] for a in range(3)) for d in (rotate(h1, m), rotate(h2, m))])
    return out


def psf(n_waters: int) -> str:
    lines = ["PSF", "", "       1 !NTITLE", " REMARKS A box of TIP3P water for a practice run (nomad demos)", ""]
    n = 3 * n_waters
    lines.append(f"{n:8d} !NATOM")
    serial = 0
    for r in range(1, n_waters + 1):
        for name, typ, q, mass in ATOMS:
            serial += 1
            lines.append(f"{serial:8d} WAT  {r:<4d} TIP3 {name:<4s} {typ:<4s} {q:10.6f} {mass:13.4f} {0:11d}")
    lines.append("")

    def section(items, per, tag):
        flat = [x for it in items for x in it]
        lines.append(f"{len(items):8d} !{tag}")
        step = per * len(items[0]) if items else 1
        for i in range(0, len(flat), step):
            lines.append("".join(f"{x:8d}" for x in flat[i:i + step]))
        lines.append("")
    bonds, angles = [], []
    for w in range(n_waters):
        o, a, b = 3 * w + 1, 3 * w + 2, 3 * w + 3
        bonds += [(o, a), (o, b), (a, b)]
        angles.append((a, o, b))
    section(bonds, 4, "NBOND: bonds")
    section(angles, 3, "NTHETA: angles")
    for tag in ("NPHI: dihedrals", "NIMPHI: impropers", "NDON: donors", "NACC: acceptors"):
        lines += [f"{0:8d} !{tag}", "", ""]
    lines += [f"{0:8d} !NNB", ""]
    zeros = [0] * n
    for i in range(0, n, 8):
        lines.append("".join(f"{x:8d}" for x in zeros[i:i + 8]))
    lines += ["", f"{1:8d}{0:8d} !NGRP", f"{0:8d}{0:8d}{0:8d}", "", f"{0:8d} !NCRTERM: cross-terms"]
    return "\n".join(lines) + "\n"


def pdb(ws) -> str:
    lines = [f"CRYST1{BOX:9.3f}{BOX:9.3f}{BOX:9.3f}  90.00  90.00  90.00 P 1           1"]
    serial = 0
    for r, w in enumerate(ws, 1):
        for (name, _, _, _), (x, y, z) in zip(ATOMS, w):
            serial += 1
            element = name[0]
            lines.append(f"ATOM  {serial:5d} {' ' + name:<4s} TIP3 {r:4d}    {x:8.3f}{y:8.3f}{z:8.3f}"
                         f"{1.0:6.2f}{0.0:6.2f}      WAT {element:>2s}")
    lines.append("END")
    return "\n".join(lines) + "\n"


def write(folder: Path, ws):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "water.psf").write_text(psf(len(ws)))
    (folder / "water.pdb").write_text(pdb(ws))
    (folder / "par_water.prm").write_text(PRM)
    (folder / "water.namd").write_text(conf(BOX))


def check(folder: Path):
    import openmm as mm
    from openmm import app, unit
    p = app.CharmmPsfFile(str(folder / "water.psf"))
    p.setBox(BOX * unit.angstrom, BOX * unit.angstrom, BOX * unit.angstrom)
    params = app.CharmmParameterSet(str(folder / "par_water.prm"))
    crd = app.PDBFile(str(folder / "water.pdb"))
    system = p.createSystem(params, nonbondedMethod=app.PME, nonbondedCutoff=1.2 * unit.nanometer,
                            switchDistance=1.0 * unit.nanometer, constraints=app.HBonds, rigidWater=True)
    integ = mm.LangevinMiddleIntegrator(300 * unit.kelvin, 1 / unit.picosecond, 0.002 * unit.picoseconds)
    sim = app.Simulation(p.topology, system, integ, mm.Platform.getPlatformByName("CPU"))
    sim.context.setPositions(crd.positions)
    e0 = sim.context.getState(getEnergy=True).getPotentialEnergy()
    sim.minimizeEnergy(maxIterations=200)
    e1 = sim.context.getState(getEnergy=True).getPotentialEnergy()
    sim.context.setVelocitiesToTemperature(300 * unit.kelvin)
    sim.step(500)
    st = sim.context.getState(getEnergy=True)
    ke = st.getKineticEnergy().value_in_unit(unit.kilojoule_per_mole)
    dof = 3 * system.getNumParticles() - system.getNumConstraints() - 3
    temp = 2 * ke / (dof * 0.0083144626)
    per = e1.value_in_unit(unit.kilocalorie_per_mole) / len(p.residue_list)
    print(f"{system.getNumParticles()} atoms, {system.getNumConstraints()} constraints; "
          f"energy {e0.value_in_unit(unit.kilocalorie_per_mole):.0f} -> {e1.value_in_unit(unit.kilocalorie_per_mole):.0f} "
          f"kcal/mol after minimising ({per:.2f} a molecule); {temp:.0f} K after 1 ps")
    assert -12 < per < -8, "TIP3P liquid water should be near -10 kcal/mol a molecule"
    assert 250 < temp < 350


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    ws = waters()
    for name in ("namd-water", "namd-gpu-water"):
        write(KITS / name, ws)
    if a.check:
        check(KITS / "namd-water")

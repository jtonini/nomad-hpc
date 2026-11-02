# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Slurm's programs, as nomad runs them (1.25.2).

By hand, nomad runs them by name, as the person's PATH finds them: their
own choice, as when they type them. From the Console (nomad-gate), each is
found once on the person's login PATH, before any is run, and then run by
its full path -- nothing put earlier on PATH meanwhile runs instead -- and
nothing is run when one of them, or a folder above it, could be changed by
someone else (whoever owns it: root, Slurm's own account, the person): they
could have put it there, and it would act as the person. One PATH doesn't
find isn't run at all (as when it isn't there).
"""
from __future__ import annotations

import errno
import os
import shutil

SLURM = ("sbatch", "scontrol", "sinfo", "sacct", "sacctmgr", "squeue", "scancel")
_pinned: dict = {}               # name -> its full path (None: not on PATH), from the Console


class Unsafe(Exception):
    """A program someone else could have put where PATH finds it."""


def check(name: str, env=None, private: set = frozenset()) -> str | None:
    """``name`` as PATH finds it, by its full path (None: not there);
    Unsafe when someone else could have put it there."""
    from nomad.submit import gate
    env = os.environ if env is None else env
    found = shutil.which(name, path=env.get("PATH", os.defpath))
    if not found:
        return None
    found = os.path.abspath(found)
    why = gate.program_open(found, private)
    if why:
        raise Unsafe(f"the {name} nomad would run is {found}, and {why}: someone else could have put it there")
    return found


def pin(env=None, private: set = frozenset(), names=SLURM) -> None:
    """From the Console: each of ``names``, found now, run by its full path
    from here on (Unsafe, and none pinned, when one could have been put
    there by someone else)."""
    got = {n: check(n, env, private) for n in names}
    _pinned.clear()
    _pinned.update(got)


def unpin() -> None:
    """By hand: by name again."""
    _pinned.clear()


def pinned() -> bool:
    return bool(_pinned)


def where(name: str) -> str | None:
    """A pinned program's full path (None: not pinned, or not on PATH)."""
    return _pinned.get(name)


def argv(a: list) -> list:
    """``a`` with its program by its full path, when it is pinned; a pinned
    program PATH didn't find is a missing program (FileNotFoundError, as
    when it isn't there)."""
    a = list(a)
    if a and a[0] in _pinned:
        full = _pinned[a[0]]
        if full is None:
            raise FileNotFoundError(errno.ENOENT, f"{a[0]}: not on your PATH", a[0])
        a[0] = full
    return a

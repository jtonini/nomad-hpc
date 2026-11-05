# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Whether a job survives being put back in the queue (1.28.0).

A guest's job on a condo can be preempted when the owners need their nodes:
Slurm stops it, after up to the partition's GraceTime, and requeues it -- the
same job, run again from the top of its script, on whichever node is free. Whether that costs the person anything depends on the script: one
that resumes from its checkpoint loses minutes; one that starts over loses
everything since it started; a few break on a second run. ``notes`` reads
the script's text (nothing is run) and says what applies, each with what to
change: [{kind, text}], kind "stops" (it is cancelled rather than requeued,
or fails the second time), "loses" (it starts over, or loses its log),
"check" (it might: what nomad can't tell from the text) or "ok" (it
resumes).
"""
from __future__ import annotations

import re

_SBATCH = re.compile(r"^\s*#SBATCH\s+(.*)$")
_CMD_SPLIT = re.compile(r"\s*(?:;|&&|\|\|)\s*")


def _sbatch(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        m = _SBATCH.match(line)
        if m:
            out += m.group(1).split("#", 1)[0].split()
    return out


def _commands(text: str) -> list[str]:
    """The script's command lines (comments and #SBATCH lines left out)."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out += [c for c in _CMD_SPLIT.split(s) if c]
    return out


def _has(cmds: list[str], pattern: str) -> bool:
    rx = re.compile(pattern)
    return any(rx.search(c) for c in cmds)


def _open_mode(opts: list[str]) -> str | None:
    for i, o in enumerate(opts):
        if o.startswith("--open-mode="):
            return o.split("=", 1)[1].lower()
        if o == "--open-mode" and i + 1 < len(opts):
            return opts[i + 1].lower()
    return None


# a test of $SLURM_RESTART_COUNT (in a [ ], [[ ]], test or (( )), or a case on it), not an echo of it
_TESTS = r"(\[\[?|\btest\b)[^;\]\n]*\$\{?%s\b|\(\([^)\n]*\b%s\b|\bcase\s+\S*\$\{?%s\b"
_RESTART_TEST = re.compile(_TESTS % (("SLURM_RESTART_COUNT",) * 3))
_RESTART_COPY = re.compile(r"\b([A-Za-z_]\w*)=[\"']?\$\{?SLURM_RESTART_COUNT\b")
_ERREXIT = re.compile(r"^\s*set\s+(-[A-Za-z]*e[A-Za-z]*\b|-o\s+errexit\b)|^#!.*\s-[A-Za-z]*e[A-Za-z]*\s*$", re.M)
# the program itself, as a command's own word (not module load python3, not an echo)
_MAIN = re.compile(r"(gmx(_mpi|_d)?|mdrun\w*|vasp_(std|gam|ncl)|cp2k(\.\w+)?|g(09|16)|orca|qchem|pw\.x|lmp\w*|"
                   r"namd\d?|pmemd\S*|python[23]?(\.\d+)?|Rscript|julia|matlab|mpirun|mpiexec|srun|apptainer|"
                   r"singularity)")
_PREFIX = {"time", "exec", "nohup", "env", "command", "then", "else", "do", "if", "while", "until", "!"}


def _word(cmd: str) -> str:
    """A command's own word: past VAR=value, time, exec, then, ... ."""
    for t in cmd.split():
        if t in _PREFIX or re.fullmatch(r"[A-Za-z_]\w*=\S*", t):
            continue
        return t
    return ""


def _is_main(cmd: str) -> bool:
    w = _word(cmd)
    return w.startswith("./") or bool(_MAIN.fullmatch(w.rsplit("/", 1)[-1]))


def _restarts(text: str) -> bool:
    """Whether the script tests $SLURM_RESTART_COUNT (or a copy of it)."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    body = "\n".join(lines)
    if _RESTART_TEST.search(body):
        return True
    for name in set(_RESTART_COPY.findall(body)):
        if re.search(_TESTS % ((re.escape(name),) * 3), body):
            return True
    return False


def _mkdir_breaks(line: str) -> bool:
    """A mkdir that fails when the folder is there: no -p (in any of its
    options), not a condition's (if ! mkdir, ... || mkdir, mkdir ... || x)."""
    ln = line.split("#", 1)[0]
    m = re.search(r"(^|[;&|(]\s*|\s)mkdir\b(.*)$", ln)
    if not m or "||" in ln:
        return False
    before, opts = ln[:m.start(2)], m.group(2)
    if re.search(r"\b(if|while|until)\s+(!\s*)?mkdir$", before):
        return False
    return not re.search(r"(^|\s)(-[A-Za-z]*p[A-Za-z]*|--parents)(\s|$)", opts)


def notes(text: str, family: str | None = None) -> list[dict]:
    """What a requeue does to this script, each with what to change."""
    out = []
    text = text or ""
    opts = _sbatch(text)
    cmds = _commands(text)
    if "--no-requeue" in opts:
        out.append({"kind": "stops", "text": "it asks not to be requeued (#SBATCH --no-requeue): when the owners "
                                             "need the nodes it is cancelled, not put back in the queue"})
    if _open_mode(opts) != "append":
        out.append({"kind": "loses", "text": "its Slurm output file is written anew when it starts again: what "
                                             "the first run printed is lost (#SBATCH --open-mode=append keeps it)"})
    restarts = _restarts(text)
    fam = (family or "").lower()
    resumes = restarts
    if "gromacs" in fam or _has(cmds, r"\b(gmx(_mpi|_d)?|mdrun)\b"):
        if _has(cmds, r"\s-cpi(\s|$)"):
            resumes = True
            out.append({"kind": "ok", "text": "GROMACS resumes from its checkpoint (mdrun -cpi)"})
        else:
            out.append({"kind": "loses", "text": "GROMACS starts over from step 0: add -cpi state.cpt (or "
                                                 "-cpi NAME.cpt with -deffnm NAME) to mdrun, and it continues "
                                                 "from its checkpoint (it writes one every 15 min: -cpt)"})
    elif "vasp" in fam or _has(cmds, r"\bvasp_(std|gam|ncl)\b"):
        if _has(cmds, r"\bcp\s+(-\S+\s+)*CONTCAR\s+POSCAR\b") or restarts:
            resumes = True
        else:
            out.append({"kind": "loses", "text": "VASP starts over from POSCAR: before vasp_std, "
                                                 "[ -s CONTCAR ] && cp CONTCAR POSCAR continues from the last "
                                                 "positions (ISTART=1 also reads WAVECAR)"})
    elif "cp2k" in fam or _has(cmds, r"\bcp2k(\.\w+)?\b"):
        if not restarts:
            out.append({"kind": "loses", "text": "CP2K starts over unless its input reads its restart file: "
                                                 "&EXT_RESTART with RESTART_FILE_NAME NAME-1.restart, used when "
                                                 "that file is there"})
    elif "gaussian" in fam or _has(cmds, r"\bg(09|16)\b"):
        if not restarts:
            out.append({"kind": "loses", "text": "Gaussian starts over: with %chk in the input, an optimization "
                                                 "can go on from it (Opt=Restart in a second input, run when the "
                                                 ".chk is there)"})
    elif "orca" in fam or _has(cmds, r"(\$ORCA/|/)orca\b|^orca\b"):
        if not restarts:
            out.append({"kind": "loses", "text": "ORCA starts over; its SCF can start from the .gbw a first run "
                                                 "left (! MOREAD with %moinp)"})
    elif not restarts:
        out.append({"kind": "loses", "text": "it starts again from the top of the script: whatever it computed "
                                             "is lost unless it saves its work and the script picks it up "
                                             "($SLURM_RESTART_COUNT says how many times it was requeued)"})
    if resumes and not any(n["kind"] == "ok" for n in out):
        out.append({"kind": "ok", "text": "it picks up where it stopped"})
    # a second run that breaks: mkdir with no -p under errexit (not one guarded by [ -d ] || or || true)
    if _ERREXIT.search(text):
        for line in text.splitlines():
            if not line.lstrip().startswith("#") and _mkdir_breaks(line):
                out.append({"kind": "stops", "text": "set -e with mkdir (no -p): the second run fails at mkdir, "
                                                     "the folder being there already (mkdir -p)"})
                break
    # moving or removing before the program runs: maybe its inputs (after it: its own cleanup, fine)
    main = next((i for i, c in enumerate(cmds) if _is_main(c)), len(cmds))
    if any(_word(c) in ("rm", "mv") for c in cmds[:main]):
        out.append({"kind": "check", "text": "it removes or moves files before the program runs (rm, mv): if "
                                             "they are its inputs, the second run won't find them"})
    if _has(cmds, r"(/localscratch|\$TMPDIR|\$\{TMPDIR\}|/tmp/)"):
        out.append({"kind": "loses", "text": "what it keeps on the node's own disk (local scratch, /tmp) is gone "
                                             "when it starts again, likely on another node"})
    return out


def summary(found: list[dict]) -> str:
    """One line for a page or a message."""
    if any(n["kind"] == "stops" for n in found):
        return "it may not survive being requeued"
    if any(n["kind"] == "loses" for n in found):
        return "it starts over if it is requeued"
    return "it survives being requeued"


__all__ = ["notes", "summary"]

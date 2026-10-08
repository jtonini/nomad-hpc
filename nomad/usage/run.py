# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Read, build, check and write a usage report."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from nomad.usage import guard, render, sections, sources
from nomad.usage.config import ClusterConfig
from nomad.usage.facts import Report, apply_min_cell


def build(cfg: ClusterConfig, t0: datetime, t1: datetime, *, db: Path | None, site: str | None,
          sacct: Path | None = None, exclude: set[str] | None = None, user_map: Path | None = None,
          teaching_site: str | None = None, min_cell: int | None = None) -> tuple[Report, sources.Data]:
    from nomad import __version__
    if min_cell is not None:
        cfg.min_cell = min_cell
    data = sources.load(cfg, t0, t1, db=db, site=site, sacct=sacct, exclude=exclude,
                        user_map=user_map, teaching_site=teaching_site)
    report = sections.build(data, produced=datetime.now().strftime("%Y-%m-%d %H:%M"),
                            nomad_version=__version__)
    apply_min_cell(report, cfg.min_cell)
    return report, data


def allowed_labels(report: Report, data: sources.Data) -> tuple[set[str], set[str]]:
    """Words the report prints that aren't its own: (report.toml's tier,
    class, family and capacity labels and the cluster; the labels from the
    data: filesystems, departments, schools, session types). The second
    never excuses a username: a department named like a user still stops
    the report, unless --allow lets that word through."""
    cfg = data.cfg
    config = guard.labels_of(report.cluster, data.site, data.teaching_site,
                             *cfg.tiers, *cfg.partition_classes.values(), cfg.unlisted_partitions,
                             *(f.name for f in cfg.families), *(f.name for f in cfg.gpu_families),
                             *(p.label for p in cfg.capacity.planned))
    texts = list(data.fs_labels.values())
    for j in data.jobs:
        if j.dept:
            texts.append(j.dept)
        if j.school:
            texts.append(j.school)
    if data.teaching:
        texts += [r["type"] for r in data.teaching["by_type"]]
    return config, guard.labels_of(*texts)


def write(report: Report, data: sources.Data, out: Path, formats: list[str],
          allow: set[str] | None = None) -> list[Path]:
    """Check the report for names, then write it. Nothing is written when the
    check fails (GuardError). ``allow``: words that may appear even if a
    name in the data is spelled the same (a job called "rescale", say)."""
    texts = {}
    md = render.markdown(report)
    if "md" in formats:
        texts["md"] = md
    if "json" in formats:
        texts["json"] = render.to_json(report)
    checked = [md] + ([guard.json_text(texts["json"])] if "json" in texts else [])
    config, labels = allowed_labels(report, data)
    words = {a.lower() for a in (allow or ())}
    guard.check(checked, data.names, config | words, data_labels=labels)
    out = Path(out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    stem = f"usage-{report.cluster}-{report.period_from[:10]}-to-{report.period_to[:10]}"
    written = []
    for ext, text in texts.items():
        path = out / f"{stem}.{ext}"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        written.append(path)
    return written


def guard_details(err: guard.GuardError, path: Path) -> None:
    """The names the guard found, for the person running the report only
    (a file readable by them alone)."""
    path = Path(path).expanduser()
    with private_file(path, overwrite=True) as f:
        for kind, names in err.found.items():
            for n in sorted(names):
                f.write(f"{kind}\t{n}\n")


def private_file(path: Path, overwrite: bool = False):
    """A new text file only its owner can read. It must not exist unless
    ``overwrite``, and is never reached through a symbolic link."""
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    flags |= os.O_TRUNC if overwrite else os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "w", encoding="utf-8", newline="")
    except Exception:
        os.close(fd)
        raise

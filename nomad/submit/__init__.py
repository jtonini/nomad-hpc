# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Sizing a job, and submitting it through nomad (1.18).

- ``spec``: what a job asks for (its #SBATCH lines, its submit line);
- ``apps``: which program it runs, and what its input file says it needs;
- ``history``: the person's past runs (Slurm's accounting, or nomad's database);
- ``cluster``: the partitions it could go to, and whether the person may use them;
- ``sizing``: each value nomad suggests, where it comes from and why;
- ``cli``: ``nomad size`` (and ``nomad submit``).
"""

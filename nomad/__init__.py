# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("nomad-hpc")
except PackageNotFoundError:  # running from source without install
    __version__ = "unknown"

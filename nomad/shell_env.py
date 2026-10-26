# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""A shell for ssh's ProxyJump and ProxyCommand (1.17.3).

ssh runs a ProxyCommand -- and a ProxyJump, which it turns into one --
through $SHELL. A service account usually has no login shell
(/sbin/nologin or /bin/false), and so does $SHELL in what it runs (su,
sudo -u, a systemd unit with User=): every ssh and scp nomad starts to
reach a site through a jump host then fails, with nothing in nomad's own
output but "connection closed". nomad's subprocesses (the hub's sync and
pulls, actions, the storage, workstation and GPU collectors) inherit the
environment, so the CLI sets SHELL=/bin/sh for itself and them when $SHELL
is missing, a no-login shell, or not a program that runs. A shell that
works is left as it is. The Console calls fix_shell() when it starts.
"""
from __future__ import annotations

import os

FALLBACK = "/bin/sh"
NO_LOGIN = frozenset({"nologin", "false", "true"})


def shell_ok(shell: str | None) -> bool:
    """Whether ssh can run a ProxyCommand through this shell."""
    if not shell:
        return False
    if os.path.basename(shell) in NO_LOGIN:
        return False
    return os.path.isabs(shell) and os.path.isfile(shell) and os.access(shell, os.X_OK)


def fix_shell(environ=None) -> str | None:
    """Set SHELL to /bin/sh in environ (os.environ by default) when the one
    there can't run a ProxyCommand. Returns the shell replaced ("" when
    there was none), or None when it was left as it was."""
    env = os.environ if environ is None else environ
    shell = env.get("SHELL")
    if shell_ok(shell):
        return None
    env["SHELL"] = FALLBACK
    return shell or ""


__all__ = ["FALLBACK", "NO_LOGIN", "fix_shell", "shell_ok"]

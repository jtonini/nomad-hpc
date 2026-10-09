# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Actions: nomad commands the Console (or `nomad actions run`) may start,
on the hub or on a site, from a fixed catalog. See docs/actions.md.

    from nomad import actions
    actions.catalog()                       # what there is, with each parameter's kind
    actions.targets()                       # {"hub": bool, "sites": [...]}
    actions.run("collectors", {"days": 7}, site="c1")
    actions.report_file("usage-c1-....md")  # a file an action wrote, by name
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from nomad.actions import spec as _catalog
from nomad.actions.spec import ActionError
from nomad.actions.runner import open_report_file, report_file, reports_dir

__all__ = ["ActionError", "catalog", "targets", "run", "report_file", "open_report_file", "reports_dir"]


def catalog() -> list[dict]:
    return _catalog.catalog()


def targets() -> dict:
    """Where actions can run from here: this host as the hub (when it has the
    combined database), and the hub's sites."""
    from nomad.actions.remote import hub_settings
    h = hub_settings()
    return {"hub": h["combined_db"].exists(), "sites": [s["name"] for s in h["sites"]]}


def run(name: str, params: dict | None = None, *, site: str | None = None, here: bool = False,
        cancel: threading.Event | None = None, on_output: Callable[[str], None] | None = None) -> dict:
    """Run an action on the hub (site=None), on a site, or on this host as a
    site would (here=True), and return its result: ok, exit_code, output,
    errors, seconds, and files for an action that writes them. ActionError
    when the request itself is refused."""
    from nomad.actions import remote, runner
    action = _catalog.get(name)
    if here:
        if _catalog.SITE not in action.where:
            raise ActionError(f"{action.name} runs on the hub only")
        res = runner.run_action(action, _catalog.check(action, params), cancel=cancel, on_output=on_output)
        res["site"] = None
        return res
    h = remote.hub_settings()
    sites = [s["name"] for s in h["sites"]]
    values = _catalog.check(action, params, sites=sites)
    if site is None:
        if _catalog.HUB not in action.where:
            raise ActionError(f"{action.name} runs on a site: choose one")
        if action.hub_db and not h["combined_db"].exists():
            raise ActionError(f"no combined database at {h['combined_db']}: this host isn't the hub")
        res = runner.run_action(action, values, hub_db=h["combined_db"], cancel=cancel, on_output=on_output)
        res["site"] = None
        return res
    if _catalog.SITE not in action.where:
        raise ActionError(f"{action.name} runs on the hub, not on a site")
    if site not in sites:
        raise ActionError(f"{site!r} is not one of the hub's sites")
    return remote.run_remote(site, action.name, values, timeout=action.timeout, cancel=cancel)

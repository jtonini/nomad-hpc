#!/usr/bin/python3 -I
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""nomad policy watch: the root helper (1.20). It reads what the unprivileged
probe (watch_probe.py) cannot, and writes a small, counts-only JSON that the
probe reads back (collectors.policy_watch root_report). Nothing else of
nomad is needed; standard library only, Python 3.6 or later.

It is OFF until a site turns it on, because it needs root. Run it as root by
cron on each node (login and compute), a few minutes apart from the sweep:

    */15 * * * * /usr/bin/python3 -I /path/to/watch_root_probe.py \\
        --out /var/lib/nomad-watch/root_signals.json \\
        --roots /home,/scratch,/localscratch

What it writes, for an administrator to review (never a command line, never
a file's contents):

- auth_failures: failed sudo and su attempts since the last run, counted per
  account (from the system auth log: /var/log/secure or /var/log/auth.log);
- setuid_new: setuid or setgid files owned by a regular account (uid >=
  --min-uid) under --roots that were not there at the last run -- a common
  step after a process reaches root. Their path's basename only.

It keeps a state file beside --out (``.state``) so each run reports only what
is new: the auth log read on from where it stopped, and setuid files not seen
before. The output and state are written atomically and world-readable
(the probe runs as the collector, not root); the state holds no secrets.

    --out PATH          where to write the JSON (required)
    --roots A,B,C       directories to scan for setuid files (default none)
    --min-uid N         the lowest uid that is a person (default 1000)
    --max-files N       stop a scan after this many entries (default 400000)
    --max-report N      at most this many new setuid files reported (default 200)
    --auth-log PATH     the auth log (default: whichever of the usual two exists)
    --stale-hours H     mark the report stale if the last run was over H ago (default 1)
"""
import argparse
import json
import os
import pwd
import re
import sys
import tempfile
import time

AUTH_LOGS = ("/var/log/secure", "/var/log/auth.log")
# A failed privilege attempt. Matched on the line's head (before COMMAND=), so a
# user can't inject one through the command string a sudo failure logs.
FAIL = re.compile(r"pam_unix\((?:sudo|su|su-l):auth\): authentication failure|"
                  r"FAILED SU|FAILED su|incorrect password attempt")
NAME = r"([A-Za-z0-9._-]{1,64})"
# The account that made the attempt (the actor), from the structured fields,
# NOT from user= (which names the target -- so su to root is counted, not
# dropped as root). In order of trust.
ACTORS = (re.compile(r"\bsudo:\s+" + NAME + r"\s+:"),          # sudo:   alice : ...
          re.compile(r"\b(?:ruser|logname)=" + NAME),           # pam
          re.compile(r"FAILED SU \(to \S+\) " + NAME),         # su: FAILED SU (to root) alice
          re.compile(r"\bby " + NAME + r"\b"))                   # su: FAILED su for root by alice


def _name(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OverflowError):
        return None


def _uid(name):
    try:
        return pwd.getpwnam(name).pw_uid
    except (KeyError, TypeError):
        return None


def load_state(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_atomic(path, text, mode=0o644):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".nomad-watch.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def auth_failures(log, state, min_uid):
    """Failed sudo/su attempts since the last run, counted per account. Reads
    the auth log on from where it stopped (by inode and offset, kept in
    state), so each run counts only new lines. Only accounts from min_uid up."""
    if not log:
        return [], state
    try:
        st = os.stat(log)
    except OSError:
        return [], state
    prev = state if isinstance(state, dict) else {}
    offset = prev.get("offset", 0) if prev.get("inode") == st.st_ino else 0
    if offset > st.st_size:                 # rotated or truncated: start over
        offset = 0
    counts = {}
    try:
        with open(log, errors="replace") as f:
            f.seek(offset)
            for line in f:
                head = line.split("COMMAND=", 1)[0]     # a command string can't inject an actor/target
                if not FAIL.search(head):
                    continue
                who = next((m.group(1) for m in (rx.search(head) for rx in ACTORS) if m), None)
                if not who:
                    continue
                uid = _uid(who)
                if uid is None or uid < min_uid:        # only a person's attempts (root's own excluded)
                    continue
                counts[who] = counts.get(who, 0) + 1
            end = f.tell()
    except OSError:
        return [], state
    out = [{"user": u, "uid": _uid(u), "count": n} for u, n in sorted(counts.items())]
    return out, {"inode": st.st_ino, "offset": end}


def _iter(roots, min_uid, max_files):
    """setuid/setgid regular files under roots owned by a person, bounded.
    Stays on each root's own filesystem (os.scandir, no symlink following)."""
    seen = 0
    for root in roots:
        try:
            dev = os.stat(root).st_dev
        except OSError:
            continue
        stack = [root]
        while stack and seen < max_files:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            with it:
                for e in it:
                    if seen >= max_files:
                        break
                    seen += 1
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            if e.stat(follow_symlinks=False).st_dev == dev:
                                stack.append(e.path)
                            continue
                        if not e.is_file(follow_symlinks=False):
                            continue
                        stt = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stt.st_mode & 0o6000 and stt.st_uid >= min_uid:
                        yield e.path, stt.st_uid


def setuid_new(roots, state, min_uid, max_files, max_report):
    """setuid/setgid files under roots that weren't there at the last run.
    State keeps the paths already seen, so only new ones are reported."""
    known = set(state.get("paths", [])) if isinstance(state, dict) else set()
    now, new = [], []
    for path, uid in _iter(roots, min_uid, max_files):
        now.append(path)
        if path not in known:
            new.append((path, uid))
    rows = []
    for path, uid in new[:max_report]:
        rows.append({"user": _name(uid), "uid": uid, "name": os.path.basename(path)})
    # keep the full current set (bounded) so a file is reported once, not every run
    return rows, {"paths": sorted(set(now))[:50000]}


def main():
    ap = argparse.ArgumentParser(description="nomad policy watch root helper (writes a counts-only JSON).")
    ap.add_argument("--out", required=True)
    ap.add_argument("--roots", default="")
    ap.add_argument("--min-uid", type=int, default=1000)
    ap.add_argument("--max-files", type=int, default=400000)
    ap.add_argument("--max-report", type=int, default=200)
    ap.add_argument("--auth-log", default="")
    ap.add_argument("--stale-hours", type=float, default=1.0)
    a = ap.parse_args()
    if os.geteuid() != 0:
        sys.stderr.write("watch_root_probe: not root; it reads root-only logs and files, so run it as root.\n")
    roots = [r for r in a.roots.split(",") if r]
    log = a.auth_log or next((p for p in AUTH_LOGS if os.path.exists(p)), "")
    state_path = a.out + ".state"
    state = load_state(state_path)

    fails, auth_state = auth_failures(log, state.get("auth", {}), a.min_uid)
    suid, suid_state = setuid_new(roots, state.get("setuid", {}), a.min_uid, a.max_files, a.max_report)

    now = time.time()
    report = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
        "host": os.uname()[1].split(".")[0],
        "auth_log": bool(log),
        "roots": roots,
        "auth_failures": fails,
        "setuid_new": suid,
    }
    write_atomic(a.out, json.dumps(report) + "\n")
    write_atomic(state_path, json.dumps({"auth": auth_state, "setuid": suid_state,
                                         "at": now}) + "\n", mode=0o600)


if __name__ == "__main__":
    main()

# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Whether licensed programs run on the lab machines (1.59.0).

A license file can be in force and still not cover a machine: a program
licensed node by node (Q-Chem's node-locked license) refuses to start on a
workstation added after its license was made, and the file says nothing a
script can read about which machines it covers. So nomad tries: twice a day,
on each machine a site collects from, a tiny job with each install of each
program listed (for Q-Chem: two hydrogen atoms, Hartree-Fock, the smallest
basis -- a few seconds), as an unprivileged account (the gate's, ``nomad``,
else ``nobody``), in a folder under /tmp that is removed after. What it keeps,
per machine and install, is one of:

- ``runs``: the job finished;
- ``license``: the program refused its license (why, in a few words: "the
  license expired on 12 Sep 2026", "this machine isn't in the license");
- ``nostart``: it didn't start ("its setup says the program is in X, which
  isn't on this machine");
- ``timeout`` and ``failed``: still running after the limit, or ended
  otherwise (its last line).

Nothing the program prints is kept beyond those words: its license keys and
machine ids never leave the machine's answer, which is read in memory and
dropped.

The machines are reached the way the site's workstation collector reaches
them, over ssh (as ``ssh_user``, default root, so that the job can be run as
the unprivileged account). On the site that reaches them (its nomad.toml)::

    [collectors.license_tries]
    enabled = true
    # min_hours = 11              # try at most this often (twice a day)
    # machines = []               # default: [collectors.workstation] workstations
    # ssh_user = "root"           # the account ssh logs in as on each machine
    # run_as = "nomad"            # the account each tiny job runs as (else nobody)
    # timeout = 120               # seconds one tiny job may take
    # at_once = 6                 # machines tried at the same time

    [[collectors.license_tries.programs]]
    name = "Q-Chem"
    recipe = "qchem"              # how to try it (nomad knows: qchem)
    min_version = "6"             # older installs are left out
    # roots = ["/opt", "/usr/local", "/software", "/apps", "/usr/local/sw", "/share/apps"]

The hub gets the rows with everything else (``nomad sync``), lists them
(``nomad licenses tries``; the Console's Licenses page for staff, each lab's
page for its own machines), and the daily license email (``nomad licenses
remind``) says each change once: a program that stops running on a machine,
and one that runs there again. Emails name machines and programs, never
people.
"""
from __future__ import annotations

import re
import shlex
import socket
import sqlite3
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

TABLE = "license_tries"
HOSTS = "license_try_hosts"
MIN_HOURS = 11
TIMEOUT = 120
AT_ONCE = 6
ROOTS = ("/opt", "/usr/local", "/software", "/apps", "/usr/local/sw", "/share/apps")
RECIPES = ("qchem",)
RESULTS = ("runs", "license", "nostart", "timeout", "failed")
NOT_RUNNING = ("license", "nostart", "timeout", "failed")
MARK = "__NOMAD_TRIES_BEGIN__"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,40}$")
_ACCOUNT = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")


class TriesError(ValueError):
    pass


@dataclass
class Program:
    name: str
    recipe: str
    min_major: int = 0
    roots: tuple = ROOTS


@dataclass
class Settings:
    programs: list = field(default_factory=list)
    machines: list = field(default_factory=list)
    ssh_user: str = "root"
    run_as: str = "nomad"
    timeout: int = TIMEOUT
    at_once: int = AT_ONCE
    min_hours: float = MIN_HOURS
    problems: list = field(default_factory=list)


def _major(value, where: str, problems: list) -> int:
    if value in (None, ""):
        return 0
    m = re.match(r"^\s*(\d+)", str(value))
    if not m:
        problems.append(f"{where} min_version {value!r} is not a version (6, 6.4, ...)")
        return 0
    return int(m.group(1))


def settings(section: dict | None) -> Settings:
    """[collectors.license_tries], as the collector gets it (``workstation_hosts``
    added from the workstation collector's list)."""
    sec = section if isinstance(section, dict) else {}
    s = Settings()
    p = s.problems
    for i, raw in enumerate(sec.get("programs") or []):
        where = f"[[collectors.license_tries.programs]] #{i + 1}"
        if not isinstance(raw, dict):
            p.append(f"{where} is not a table")
            continue
        name = str(raw.get("name") or "").strip()
        recipe = str(raw.get("recipe") or "").strip().lower()
        if not _NAME.match(name):
            p.append(f"{where} needs a name (letters, digits, spaces, . _ + -)")
            continue
        if recipe not in RECIPES:
            p.append(f"{where} ({name}): recipe {recipe!r} is not one nomad knows ({', '.join(RECIPES)})")
            continue
        roots = raw.get("roots")
        if roots is None:
            roots = ROOTS
        elif not (isinstance(roots, list) and all(isinstance(r, str) and r.startswith("/") for r in roots)):
            p.append(f"{where} ({name}): roots should be a list of folders (\"/opt\", ...)")
            roots = ROOTS
        s.programs.append(Program(name, recipe, _major(raw.get("min_version"), where, p),
                                  tuple(r.rstrip("/") or "/" for r in roots)))
    listed = sec.get("machines")
    if listed:
        hosts = listed if isinstance(listed, list) else []
        if not hosts:
            p.append("[collectors.license_tries] machines should be a list of host names")
    else:
        hosts = sec.get("workstation_hosts") or []
    s.machines = []
    for h in hosts:
        if isinstance(h, str) and _HOST.match(h.strip()):
            if h.strip() not in s.machines:
                s.machines.append(h.strip())
        else:
            p.append(f"[collectors.license_tries] machine {h!r} is not a host name")
    for key, default in (("ssh_user", "root"), ("run_as", "nomad")):
        v = str(sec.get(key) or default).strip()
        if not _ACCOUNT.match(v):
            p.append(f"[collectors.license_tries] {key} {v!r} is not an account name")
            v = default
        setattr(s, key, v)
    for key, default, lo, hi in (("timeout", TIMEOUT, 10, 1800), ("at_once", AT_ONCE, 1, 32)):
        try:
            v = int(sec.get(key, default))
        except (TypeError, ValueError):
            p.append(f"[collectors.license_tries] {key} should be a number")
            v = default
        setattr(s, key, min(max(v, lo), hi))
    try:
        s.min_hours = float(sec.get("min_hours", MIN_HOURS))
    except (TypeError, ValueError):
        p.append("[collectors.license_tries] min_hours should be a number")
    return s


# -- what each machine runs ---------------------------------------------------------------------

# One script for every recipe: it finds the installs and tries each. Run as
# root (bash -s -- RUN_AS LIMIT), it runs the tiny job as RUN_AS; otherwise
# as whoever runs it. It prints, for nomad to read:
#   @host  NAME  ACCOUNT
#   @try   PROGRAM  RECIPE  INSTALL  STATUS  SECONDS
#   @said  a line the program printed (what nomad reads its reason from)
#   @setup  QC-AFTER-ITS-SETUP  0|1 (is it on this machine)
# Nothing it prints is kept: nomad keeps its own words for the reason.
_SCRIPT_HEAD = r'''set -u
export LC_ALL=C
RUNAS=${1:-nomad} LIMIT=${2:-120}
if [ "$(id -u)" = 0 ]; then
    getent passwd "$RUNAS" >/dev/null 2>&1 || RUNAS=nobody
else
    RUNAS=""
fi
echo "@host	$(hostname -s 2>/dev/null || hostname)	${RUNAS:-$(id -un)}"
'''

_QCHEM = r'''
# ---- QCHEM: PROGRAM, MINMAJOR, ROOTS
try_qchem() {
    local prog=$1 minmajor=$2; shift 2
    local roots=() d q lic
    for d in "$@"; do [ -d "$d" ] && roots+=("$d"); done
    [ ${#roots[@]} -gt 0 ] || return 0
    local found=()
    while IFS= read -r d; do
        while IFS= read -r lic; do
            q=$(readlink -f "${lic%/qcaux/license/qchem.license.dat}") && [ -n "$q" ] && found+=("$q")
        done < <(find -H "$d" -maxdepth 4 -path '*/qcaux/license/qchem.license.dat' -type f 2>/dev/null)
    done < <(find "${roots[@]}" -mindepth 1 -maxdepth 2 \( -iname 'qchem*' -o -iname 'q-chem*' \) -xtype d 2>/dev/null)
    [ ${#found[@]} -gt 0 ] || return 0
    local input='$molecule
0 1
H  0.000  0.000  0.000
H  0.000  0.000  0.740
$end

$rem
JOBTYPE  sp
METHOD   hf
BASIS    sto-3g
$end
'
    local t run rc t0 secs st digits
    while IFS= read -r q; do
        digits=$(basename "$q" | grep -o '[0-9][0-9]*' | head -1)
        if [ -n "$digits" ] && [ "${digits:0:1}" -lt "$minmajor" ]; then continue; fi
        t=$(mktemp -d /tmp/nomad-tries.XXXXXX) || { echo "@try	$prog	qchem	$q	failed	0"; echo "@said	no folder could be made in /tmp"; continue; }
        printf '%s' "$input" > "$t/h2.in"
        [ -n "$RUNAS" ] && chown -R "$RUNAS" "$t" 2>/dev/null
        chmod 700 "$t"
        run='cd "$QCSCRATCH" || exit 9
             if [ -f "$QC/qcenv.sh" ]; then . "$QC/qcenv.sh" >/dev/null 2>&1; else export PATH="$QC/bin:$PATH"; fi
             if [ -d "$QC" ]; then echo "@setup	$QC	1"; else echo "@setup	$QC	0"; fi
             command -v qchem >/dev/null 2>&1 || { echo "no qchem command in $QC/bin"; exit 8; }
             timeout '"$LIMIT"' qchem h2.in h2.out'
        t0=$SECONDS
        if [ -n "$RUNAS" ]; then
            runuser -u "$RUNAS" -- env -i HOME="$t" PATH=/usr/local/bin:/usr/bin:/bin QC="$q" QCAUX="$q/qcaux" \
                QCSCRATCH="$t" QCLOCALSCR="$t" TMPDIR="$t" USER="$RUNAS" LOGNAME="$RUNAS" bash -c "$run" > "$t/run.log" 2>&1
        else
            env -i HOME="$t" PATH=/usr/local/bin:/usr/bin:/bin QC="$q" QCAUX="$q/qcaux" QCSCRATCH="$t" \
                QCLOCALSCR="$t" TMPDIR="$t" USER="$(id -un)" LOGNAME="$(id -un)" bash -c "$run" > "$t/run.log" 2>&1
        fi
        rc=$?
        secs=$((SECONDS - t0))
        cat "$t/run.log" "$t/h2.out" > "$t/both" 2>/dev/null
        if grep -q 'Thank you very much for using Q-Chem\|Total energy in the final basis set' "$t/both"; then st=runs
        elif grep -qi 'licen[cs]e' "$t/both"; then st=license
        elif [ "$rc" = 8 ]; then st=nostart
        elif [ "$rc" = 124 ]; then st=timeout
        else st=failed
        fi
        echo "@try	$prog	qchem	$q	$st	$secs"
        grep '^@setup	' "$t/run.log" | head -1
        if [ "$st" != runs ]; then
            { grep -aiE 'licen[cs]e|expire|invalid|unable|fail|error|no qchem command|not found|denied' "$t/both";
              grep -av '^@setup' "$t/both" | grep -v '^[[:space:]]*$' | tail -2; } \
                | grep -v '^@' | awk '!seen[$0]++' | head -8 | cut -c1-200 | sed 's/^/@said	/'
        fi
        rm -rf "$t"
    done < <(printf '%s\n' "${found[@]}" | sort -u)
}
'''


def script(s: Settings) -> str:
    """The script each machine runs (``bash -s -- RUN_AS LIMIT``)."""
    calls = []
    for p in s.programs:
        if p.recipe == "qchem":
            calls.append("try_qchem " + " ".join(shlex.quote(x) for x in (p.name, str(p.min_major), *p.roots)))
    return _SCRIPT_HEAD + _QCHEM + "\n".join(calls) + "\nexit 0\n"


# -- what it said, in nomad's own words ---------------------------------------------------------

_ID = re.compile(r"\b(SID|sid|host ?id|hostid)[ :=]*\S+")
_HEX = re.compile(r"\b[0-9A-Fa-f]{10,}\b")
_KEY = re.compile(r"(?<![A-Za-z0-9+=._-])(?=[A-Za-z0-9+=._-]*\d)(?=[A-Za-z0-9+=._-]*[A-Za-z])[A-Za-z0-9+=._-]{16,}")
_EXPIRED = re.compile(r"expired licen[cs]e\s+(\d{1,2})-(\d{1,2})-(\d{2,4})", re.I)
_NOT_COVERED = re.compile(r"failed standard licen[cs]e check|unable to validate licen[cs]e", re.I)
_NO_CMD = re.compile(r"no qchem command in (\S+)/bin")


def masked(line: str) -> str:
    """A line with no ids and no license keys in it."""
    line = _ID.sub(lambda m: m.group(1) + " (id)", line)
    line = _HEX.sub("(id)", line)
    return _KEY.sub("(key)", line).strip()


def _expiry(m) -> date | None:
    a, b, y = (int(x) for x in m.groups())
    y = y + 2000 if y < 100 else y
    for mm, dd in ((a, b), (b, a)):           # Q-Chem writes MM-DD-YY; DD-MM-YY tried if that can't be
        try:
            return date(y, mm, dd)
        except ValueError:
            continue
    return None


def reason(status: str, said: list[str], setup: tuple | None, limit: int) -> str:
    """Why it doesn't run, in a few words ('' when it runs)."""
    if status == "runs":
        return ""
    text = "\n".join(said)
    if status == "nostart":
        if setup and setup[1] == "0":
            return f"its setup says it is in {setup[0]}, which isn't on this machine"
        m = _NO_CMD.search(text)
        return f"no qchem command in {m.group(1)}/bin" if m else "it didn't start"
    if status == "timeout":
        return f"still running after {limit} s"
    if status == "license":
        m = _EXPIRED.search(text)
        if m:
            d = _expiry(m)
            return f"the license expired on {d.day} {d.strftime('%b %Y')}" if d else "the license expired"
        if _NOT_COVERED.search(text):
            return "this machine isn't in the license"
        for line in said:
            if re.search(r"licen[cs]e", line, re.I) and not re.match(r"\s*license file:", line, re.I):
                return "license refused: " + masked(line)[:120]
        return "license refused"
    last = [masked(x) for x in said if x.strip()]
    return (last[-1][:120] if last else "it ended without saying why")


def version_of(install: str) -> str:
    """'6.4' from qchem64, '6.1.1' from qchem611, '7.0' from qchem70 ('' if none)."""
    base = install.rstrip("/").rsplit("/", 1)[-1]
    m = re.search(r"(\d+)", base)
    if not m:
        return ""
    d = m.group(1)
    return d if len(d) == 1 else ".".join([d[0]] + list(d[1:]))


def parse(out: str, limit: int) -> tuple[str | None, list[dict]]:
    """(the machine's own name, [{program, recipe, install, version, result, reason, seconds}])."""
    head, mark, rest = out.partition(MARK + "\n")
    body = rest if mark else out
    name, rows, cur, said, setup = None, [], None, [], None

    def close():
        if cur is not None:
            cur["reason"] = reason(cur["result"], said, setup, limit)
            rows.append(cur)

    for line in body.splitlines():
        if line.startswith("@host\t"):
            f = line.split("\t")
            name = f[1].strip() if len(f) > 1 else None
        elif line.startswith("@try\t"):
            close()
            f = line.split("\t")
            if len(f) < 6 or f[4] not in RESULTS:
                cur = None
                continue
            try:
                secs = int(f[5])
            except ValueError:
                secs = None
            cur = {"program": f[1], "recipe": f[2], "install": f[3], "version": version_of(f[3]),
                   "result": f[4], "seconds": secs}
            said, setup = [], None
        elif line.startswith("@setup\t") and cur is not None:
            f = line.split("\t")
            if len(f) >= 3:
                setup = (f[1], f[2])
        elif line.startswith("@said\t") and cur is not None:
            said.append(line[6:])
    close()
    return name, rows


def is_local(host: str) -> bool:
    h = host.split(".")[0].lower()
    return h in ("localhost", socket.gethostname().split(".")[0].lower()) or host in ("127.0.0.1", "::1")


def try_machine(host: str, s: Settings, run: Callable = subprocess.run) -> dict:
    """{machine, reached, note, rows} for one machine."""
    code = script(s)
    args = [s.run_as, str(s.timeout)]
    budget = 60 + s.timeout * max(1, 12 * len(s.programs))
    if is_local(host):
        argv = ["bash", "-s", "--", *args]
    else:
        remote = f"echo {MARK}; bash -s -- {' '.join(shlex.quote(a) for a in args)}"
        argv = ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=15",
                "-o", "LogLevel=ERROR", f"{s.ssh_user}@{host}", remote]
    try:
        r = run(argv, input=code, capture_output=True, text=True, timeout=budget)
    except subprocess.TimeoutExpired:
        return {"machine": host, "reached": False, "note": f"no answer in {budget} s", "rows": []}
    except OSError as e:
        return {"machine": host, "reached": False, "note": f"couldn't run ssh: {e.strerror or e}", "rows": []}
    name, rows = parse(r.stdout or "", s.timeout)
    if name is None:
        why = (r.stderr or "").strip().splitlines()
        note = masked(why[-1])[:160] if why else f"no answer (exit {r.returncode})"
        return {"machine": host, "reached": False, "note": note, "rows": []}
    return {"machine": host, "reached": True, "note": "" if rows else "none of the programs here", "rows": rows}


def try_all(s: Settings, run: Callable = subprocess.run) -> list[dict]:
    if not s.programs or not s.machines:
        return []
    with ThreadPoolExecutor(max_workers=min(s.at_once, len(s.machines))) as pool:
        return list(pool.map(lambda h: try_machine(h, s, run), s.machines))


# -- kept on the site ---------------------------------------------------------------------------

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    machine   TEXT NOT NULL,
    program   TEXT NOT NULL,
    install   TEXT NOT NULL,
    version   TEXT,
    result    TEXT NOT NULL,
    reason    TEXT,
    seconds   INTEGER,
    tried_at  TEXT NOT NULL,
    since     TEXT NOT NULL,
    PRIMARY KEY (machine, program, install)
);
CREATE TABLE IF NOT EXISTS {HOSTS} (
    machine   TEXT PRIMARY KEY,
    tried_at  TEXT NOT NULL,
    reached   INTEGER NOT NULL,
    note      TEXT,
    reached_at TEXT
);
"""


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def store(conn: sqlite3.Connection, results: list[dict], programs, now: datetime | None = None,
          keep_others: bool = False) -> None:
    """Each machine's answer: its rows replace its last ones (an install no
    longer there goes; ``since`` stays while the result does). A machine that
    didn't answer keeps its last rows, and its line says since when. Machines
    not in ``results`` go (no longer listed), unless ``keep_others`` (some
    machines tried by hand)."""
    now = (now or datetime.now()).replace(microsecond=0)
    at = now.isoformat()
    names = {p.name for p in programs}
    ensure(conn)
    with conn:
        for res in results:
            m = res["machine"]
            if res["reached"]:
                conn.execute(f"INSERT INTO {HOSTS} (machine, tried_at, reached, note, reached_at) VALUES (?, ?, 1, ?, ?) "
                             "ON CONFLICT(machine) DO UPDATE SET tried_at = excluded.tried_at, reached = 1, "
                             "note = excluded.note, reached_at = excluded.reached_at", (m, at, res["note"], at))
                old = {(r[0], r[1]): (r[2], r[3]) for r in conn.execute(
                    f"SELECT program, install, result, since FROM {TABLE} WHERE machine = ?", (m,))}
                seen = set()
                for row in res["rows"]:
                    key = (row["program"], row["install"])
                    seen.add(key)
                    since = old[key][1] if key in old and old[key][0] == row["result"] else at
                    conn.execute(
                        f"INSERT OR REPLACE INTO {TABLE} (machine, program, install, version, result, reason, seconds, "
                        "tried_at, since) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (m, row["program"], row["install"], row["version"], row["result"], row["reason"],
                         row["seconds"], at, since))
                for key in set(old) - seen:
                    conn.execute(f"DELETE FROM {TABLE} WHERE machine = ? AND program = ? AND install = ?", (m,) + key)
            else:
                conn.execute(f"INSERT INTO {HOSTS} (machine, tried_at, reached, note) VALUES (?, ?, 0, ?) "
                             "ON CONFLICT(machine) DO UPDATE SET tried_at = excluded.tried_at, reached = 0, "
                             "note = excluded.note", (m, at, res["note"]))
        # programs no longer listed, machines no longer listed: gone
        if names:
            marks = ",".join("?" * len(names))
            conn.execute(f"DELETE FROM {TABLE} WHERE program NOT IN ({marks})", tuple(names))
        listed = {r["machine"] for r in results}
        if listed and not keep_others:
            marks = ",".join("?" * len(listed))
            conn.execute(f"DELETE FROM {TABLE} WHERE machine NOT IN ({marks})", tuple(listed))
            conn.execute(f"DELETE FROM {HOSTS} WHERE machine NOT IN ({marks})", tuple(listed))


class Busy(RuntimeError):
    """Another round of tries is going on (the collector's, or `tries now`)."""


class one_round:
    """Only one round of tries at a time for a database: a lock file beside it,
    held from the first try to the last row kept (cron starts nomad collect
    every few minutes; a round can take longer)."""

    def __init__(self, db_path):
        self.path = f"{db_path}.license_tries.lock"
        self.fh = None

    def __enter__(self):
        import fcntl
        self.fh = open(self.path, "a")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
            raise Busy("another round of tries is going on") from None
        return self

    def __exit__(self, *exc):
        import fcntl
        if self.fh is not None:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
        return False


def last_tried(conn: sqlite3.Connection) -> datetime | None:
    try:
        row = conn.execute(f"SELECT MAX(tried_at) FROM {HOSTS}").fetchone()
    except sqlite3.Error:
        return None
    try:
        return datetime.fromisoformat(row[0]) if row and row[0] else None
    except ValueError:
        return None


# -- read on the hub ----------------------------------------------------------------------------

def _cols(conn, table: str) -> set:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def listing(conn: sqlite3.Connection | None, site: str | None = None, machines=None) -> dict:
    """{"tries": [rows], "hosts": [rows]} as kept (on the hub: every site's,
    with ``site``). ``machines``: only these (a lab's)."""
    out: dict[str, Any] = {"tries": [], "hosts": []}
    if conn is None:
        return out
    for table, key in ((TABLE, "tries"), (HOSTS, "hosts")):
        cols = _cols(conn, table)
        if not cols:
            continue
        sel = "source_site" if "source_site" in cols else "NULL"
        where, args = "", []
        if site and "source_site" in cols:
            where, args = " WHERE source_site = ?", [site]
        names = [c for c in ("machine", "program", "install", "version", "result", "reason", "seconds",
                             "tried_at", "since", "reached", "note", "reached_at") if c in cols]
        for r in conn.execute(f"SELECT {sel}, {', '.join(names)} FROM {table}{where}", args):
            d = dict(zip(["site"] + names, tuple(r)))
            if machines is not None and str(d.get("machine", "")).split(".")[0].lower() not in machines:
                continue
            out[key].append(d)
    return out


def label(row: dict) -> str:
    return f"{row['program']} {row['version']}".strip() if row.get("version") else row["program"]


def summary(data: dict) -> list[dict]:
    """Per program and install: [{program, version, install, label, site, runs: [machines],
    not: [(machine, result, reason, since)], tried_at}], busiest first."""
    groups: dict = {}
    for r in data.get("tries", []):
        key = (r.get("site"), r["program"], r["install"])
        g = groups.setdefault(key, {"site": r.get("site"), "program": r["program"], "install": r["install"],
                                    "version": r.get("version") or "", "label": label(r), "runs": [], "not": [],
                                    "tried_at": r.get("tried_at")})
        if r["result"] == "runs":
            g["runs"].append(r["machine"])
        else:
            g["not"].append((r["machine"], r["result"], r.get("reason") or "", r.get("since")))
        if (r.get("tried_at") or "") > (g["tried_at"] or ""):
            g["tried_at"] = r.get("tried_at")

    def vkey(v: str):
        return tuple(int(x) for x in v.split(".") if x.isdigit())
    out = sorted(groups.values(), key=lambda g: (g["program"], tuple(-x for x in vkey(g["version"])), g["install"]))
    for g in out:
        g["runs"].sort()
        g["not"].sort()
    return out


# -- what the daily license email says, each once -----------------------------------------------

def told_name(row: dict) -> str:
    return f"try:{row['machine']}:{row['program']}:{row['install']}"


def due(data: dict, done: set) -> list[dict]:
    """[{row, why: "notrun"|"again", what}] not told yet: a machine and install
    whose result isn't "runs" (once each time it stops), and one that runs
    again after a stop that was told (once)."""
    told: dict = {}
    for (st, nm, w, step) in done:
        if step == "once" and nm.startswith("try:"):
            told.setdefault((st, nm), []).append(tuple(w.split(":", 1)) if ":" in w else (w, ""))
    out = []
    for r in data.get("tries", []):
        key = (r.get("site") or "", told_name(r))
        since = str(r.get("since") or "")
        what = f"{r['result']}:{since}"
        said = told.get(key, [])
        if r["result"] in NOT_RUNNING:
            if (r["result"], since) not in said:
                out.append({"row": r, "why": "notrun", "what": what})
        elif r["result"] == "runs":
            stops = [s for res, s in said if res in NOT_RUNNING and s < since]
            again = [s for res, s in said if res == "runs"]
            if stops and not any(s >= max(stops) for s in again):
                out.append({"row": r, "why": "again", "what": what})
    return out


def section(notes: list[dict]) -> tuple[str, list[str]]:
    """(the subject's words, the email's lines) for these notes."""
    if not notes:
        return "", []
    by: dict = {}
    for n in notes:
        r = n["row"]
        key = (r.get("site") or "", label(r), r["install"])
        g = by.setdefault(key, {"notrun": {}, "again": [], "program": r["program"]})
        if n["why"] == "notrun":
            g["notrun"].setdefault(r.get("reason") or r["result"], []).append(r["machine"])
        else:
            g["again"].append(r["machine"])
    lines = ["Licensed programs on the lab machines (nomad tries each twice a day):", ""]
    stopped: dict = {}
    for (site, lab, install), g in sorted(by.items()):
        where = f" ({site})" if site else ""
        for why, ms in sorted(g["notrun"].items()):
            ms = sorted(set(ms))
            stopped.setdefault(g["program"], set()).add(lab)
            lines.append(f"- {lab}{where} doesn't run on {len(ms)} machine{'s' if len(ms) != 1 else ''}: "
                         f"{', '.join(ms)}. Why: {why}. ({install})")
        if g["again"]:
            ms = sorted(set(g["again"]))
            lines.append(f"- {lab}{where} runs again on {', '.join(ms)}. ({install})")
    lines.append("")
    if stopped:
        progs = sorted(stopped)
        if len(progs) == 1:
            n = len(stopped[progs[0]])
            subj = f"{progs[0]} doesn't run on some lab machines" + (f" ({n} versions)" if n > 1 else "")
        else:
            subj = f"{len(progs)} programs don't run on some lab machines"
    else:
        subj = "licensed programs run again on lab machines"
    return subj, lines

#!/usr/bin/python3 -I
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""nomad power: the root helper (1.25.8). It reads what only root can -- the
board's own power sensors and event log, through ipmitool -- and writes them
to a JSON file the power collector reads (collectors.power files). Nothing
else of nomad is needed; standard library only, Python 3.6 or later.

It is OFF until a site turns it on, because it needs root. Run it as root by
cron on the head node, for itself and every node Slurm lists (over root's
ssh), or on one host for itself only:

    */5 * * * * /usr/bin/python3 -I /usr/local/libexec/nomad/power_root_probe.py \\
        --out /var/lib/nomad-power/power.json --nodes slurm --self

What it reads, and only reads (ipmitool's read commands, nothing that sets
or powers anything):

- ``ipmitool sdr type T`` for the Power Supply, Power Unit, Voltage, Current
  and Fan sensors: each one's status and reading, or its states (a supply's
  "Presence detected", "Power Supply AC lost", "Failure detected" ...);
- ``ipmitool sensor get`` for a sensor whose status isn't ok: its thresholds,
  so the collector can tell a real threshold from a 0 W one;
- ``ipmitool sel info``, and ``ipmitool sel elist`` when the event log
  changed: the entries not seen before (a state file beside --out keeps what
  was seen). A host's first run sends what its log holds as history.

The collector judges what is wrong; this only reports. The output is written
atomically and world-readable (the collector runs as nomad's account, not
root); the state file is root's only. Two runs never overlap (a lock beside
--out): one still running makes the next exit at once.

    --out PATH        where to write the JSON (required; its folder must be
                      root's and not writable by others)
    --nodes LIST      hosts to read over root's ssh, comma-separated; "slurm"
                      is every node sinfo lists
    --self            this host too (the default without --nodes)
    --parallel N      hosts read at once (default 8)
    --sinfo PATH      Slurm's sinfo, where it isn't in /usr/bin or /usr/local/bin
    --timeout S       seconds a host may take for each of its two reads (default 240;
                      each ipmitool command at most 30)
    --max-events N    at most this many log entries sent per host per run (default 200)
"""
import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

VERSION = "1.25.8"
MARK = "__NOMAD_POWER__"
TYPES = ("Power Supply", "Power Unit", "Voltage", "Current", "Fan")
NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# A sensor's name as ipmitool prints it (at most 16 characters in the record). Never
# starting with "-": ipmitool would take it for an option (-H, -S, -O ...).
SENSOR_OK = re.compile(r"^[A-Za-z0-9+][A-Za-z0-9 _.+#/():-]{0,39}$")
PATH = "/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/sbin:/usr/local/bin"
MAX_OUTPUT = 4 * 1024 * 1024
MAX_ERRORS = 64 * 1024
THRESHOLDS = (("Lower non-recoverable", "lnr"), ("Lower critical", "lcr"), ("Lower non-critical", "lnc"),
              ("Upper non-critical", "unc"), ("Upper critical", "ucr"), ("Upper non-recoverable", "unr"))
# ipmitool under timeout where there is one: a board that stops answering doesn't hold the run
# (and killed if it ignores the signal); each command's exit status follows it.
PREFIX = ('T=""; timeout --foreground -k 5 30 true >/dev/null 2>&1 && T="timeout --foreground -k 5 30"; ')
SSH = ["ssh", "-n", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
       "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
       "-o", "ClearAllForwardings=yes"]


def stamp():
    """Now, with this host's offset from UTC (2026-11-03T12:30:00-05:00): the
    collector reading the file may run in another time zone."""
    t = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return t[:-2] + ":" + t[-2:] if re.search(r"[+-]\d{4}$", t) else t


def first_script():
    lines = [PREFIX,
             'command -v ipmitool >/dev/null 2>&1 || { echo "%s none"; exit 0; }; ' % MARK]
    for t in TYPES:
        lines.append('echo "%s sdr %s"; $T ipmitool sdr type %s 2>&1; echo "%s rc $?"; '
                     % (MARK, t, shlex.quote(t), MARK))
    lines.append('echo "%s selinfo"; $T ipmitool sel info 2>&1; echo "%s rc $?"; echo "%s end"'
                 % (MARK, MARK, MARK))
    return "".join(lines)


def second_script(names, sel):
    parts = [PREFIX]
    if names:
        parts.append('echo "%s get"; $T ipmitool sensor get %s 2>&1; echo "%s rc $?"; '
                     % (MARK, " ".join(shlex.quote(n) for n in names), MARK))
    if sel:
        parts.append('echo "%s sel"; $T ipmitool sel elist 2>&1; echo "%s rc $?"; ' % (MARK, MARK))
    parts.append('echo "%s end"' % MARK)
    return "".join(parts)


def _bounded(argv, env, timeout):
    """(stdout, stderr, exit status) of argv, at most MAX_OUTPUT and MAX_ERRORS
    bytes kept: a host that prints without end is stopped there, not held in
    memory. Its own process group, so a timeout kills what it started too
    (ipmitool's timeout runs in the foreground: in the same group)."""
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         env=env, start_new_session=True)
    bufs, too_much = {"out": bytearray(), "err": bytearray()}, threading.Event()

    def pump(stream, key, cap, stop):
        buf = bufs[key]
        fd = stream.fileno()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            if len(buf) + len(chunk) > cap:
                buf += chunk[:max(0, cap - len(buf))]
                if stop:
                    too_much.set()
                    return
                continue                # errors past the cap: read on, and let go
            buf += chunk

    readers = [threading.Thread(target=pump, args=(p.stdout, "out", MAX_OUTPUT, True)),
               threading.Thread(target=pump, args=(p.stderr, "err", MAX_ERRORS, False))]
    for t in readers:
        t.daemon = True
        t.start()
    deadline = time.monotonic() + timeout
    late = False
    while p.poll() is None:
        if too_much.is_set() or time.monotonic() > deadline:
            late = not too_much.is_set()
            try:
                os.killpg(p.pid, 9)
            except OSError:
                pass
            break
        time.sleep(0.05)
    p.wait()
    for t in readers:
        t.join(2)
    if any(t.is_alive() for t in readers):
        # something it started still holds a pipe: its group is still there (so the number is
        # nobody else's), and it goes too
        try:
            os.killpg(p.pid, 9)
        except OSError:
            pass
        for t in readers:
            t.join(5)
    for t, f in zip(readers, (p.stdout, p.stderr)):
        if not t.is_alive():
            f.close()           # a reader still waiting keeps its pipe: never a number reused under it
    if late:
        raise RuntimeError("no answer in %d s" % timeout)
    if too_much.is_set():
        raise RuntimeError("more than %d MB of output: stopped" % (MAX_OUTPUT // 1024 // 1024))
    return bytes(bufs["out"]), bytes(bufs["err"]), p.returncode


def run_on(host, script, timeout, local):
    """The script's output on host (this host's sh, or root's ssh), or raises
    RuntimeError with why not."""
    env = {"PATH": PATH, "LC_ALL": "C", "LANG": "C"}
    argv = ["/bin/sh", "-c", script] if local else SSH + ["--", host, script]
    try:
        out, err, rc = _bounded(argv, env, timeout)
    except OSError as e:
        raise RuntimeError("couldn't run %s (%s)" % (argv[0], e.strerror or e))
    out = out.decode("utf-8", "replace")
    if MARK not in out:
        lines = err[-2000:].decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(lines[-1][:300] if lines else "no answer (exit %d)" % rc)
    # what a login prints before the command (a banner) is dropped
    return out[out.index(MARK):]


def sections(out):
    """({name: [lines]}, {name: exit status}) from the output's MARK lines
    (name = what follows MARK; "rc N" gives the section before it its status)."""
    got, rcs, cur = {}, {}, None
    for line in out.splitlines():
        if line.startswith(MARK):
            name = line[len(MARK):].strip()
            if name.startswith("rc ") and cur is not None:
                try:
                    rcs[cur] = int(name[3:])
                except ValueError:
                    rcs[cur] = -1
                continue
            cur = name
            got.setdefault(cur, [])
        elif cur is not None:
            got[cur].append(line.rstrip())
    return got, rcs


def parse_sdr(lines, kind):
    """``ipmitool sdr type`` lines: name | number | status | entity | reading or states."""
    rows, errors = [], []
    for line in lines:
        f = [x.strip() for x in line.split("|")]
        if len(f) != 5 or not f[0]:
            if line.strip():
                errors.append(line.strip()[:200])
            continue
        rows.append({"name": f[0][:40], "type": kind, "number": f[1][:8], "status": f[2][:8],
                     "entity": f[3][:8], "value": f[4][:200]})
    return rows, errors


def parse_selinfo(lines):
    info = {}
    for line in lines:
        k, sep, v = line.partition(":")
        if not sep:
            continue
        k, v = k.strip().lower(), v.strip()
        if k == "entries" and v.isdigit():
            info["entries"] = int(v)
        elif k == "percent used":
            m = re.match(r"(\d+)", v)
            if m:
                info["used_pct"] = int(m.group(1))
        elif k == "last add time":
            info["last_add"] = v[:40]
        elif k == "last del time":
            info["last_del"] = v[:40]
        elif k == "overflow":
            info["overflow"] = v.lower() == "true"
    return info


def parse_get(lines):
    """``ipmitool sensor get A B``: each sensor's thresholds, by its Sensor ID."""
    got, cur = {}, None
    for line in lines:
        k, sep, v = line.partition(":")
        if not sep:
            continue
        k, v = k.strip(), v.strip()
        if k == "Sensor ID":
            cur = re.sub(r"\s*\(0x[0-9a-fA-F]+\)\s*$", "", v)
            got[cur] = {}
        elif cur is not None:
            for label, key in THRESHOLDS:
                # "Lower Non-Recoverable" (sensor get) or "Lower non-recoverable" (sdr get)
                if k.lower() == label.lower():
                    try:
                        got[cur][key] = float(v)
                    except ValueError:
                        pass
    return got


def parse_sel(lines):
    """``ipmitool sel elist``: id | date | time | sensor | event [| direction [| detail]]."""
    rows = []
    for line in lines:
        f = [x.strip() for x in line.split("|")]
        if len(f) < 5 or not f[0]:
            continue
        # an entry is its record id and when it was logged: the same after an ipmitool or
        # firmware update words it otherwise; a cleared log starts its ids again, at other times.
        # Logged before the board's clock was set ("Pre-Init", seconds since it started), an id
        # and a count can come again after a clear: what it says is part of it then.
        part = f[:3] if f[1].lower() != "pre-init" else f[:5]
        raw = "|".join(re.sub(r"\s+", " ", x).lower() for x in part)
        rows.append({"key": hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16],
                     "id": f[0][:8], "date": f[1][:16], "time": f[2][:16], "sensor": f[3][:80],
                     "event": f[4][:120], "direction": (f[5] if len(f) > 5 else "")[:16],
                     "detail": " | ".join(f[6:])[:200]})
    return rows


def read_host(host, local, seen, timeout, max_events):
    """One host's readings, and what its event log holds now (for the state)."""
    now = stamp()
    try:
        sec, rcs = sections(run_on(host, first_script(), timeout, local))
    except RuntimeError as e:
        return {"read_at": now, "error": str(e)}, None
    if "none" in sec:
        return {"read_at": now, "error": "no ipmitool here"}, None
    sensors, errors, unread = [], [], []
    for t in TYPES:
        name = "sdr " + t
        rows, errs = parse_sdr(sec.get(name, []), t)
        if name not in sec or rcs.get(name) != 0 or errs and not rows:
            # failed, cut short, or only an error said ("Unable to open SDR for reading": another
            # tool had the board): this type isn't known this time (its rows kept as they were)
            unread.append(t)
            if rcs.get(name) in (124, 137):
                errors.append("ipmitool sdr type %s: no answer in 30 s" % t)
            errors += errs[:2]
            continue
        sensors += rows
        errors += errs
    if not sensors and errors:
        # "Could not open device at /dev/ipmi0 ...": no board controller this host can reach
        return {"read_at": now, "error": errors[-1]}, None
    info = parse_selinfo(sec.get("selinfo", [])) if rcs.get("selinfo") == 0 else {}
    flagged = [s["name"] for s in sensors
               if s["status"] not in ("ok", "ns", "") and SENSOR_OK.match(s["name"])][:40]
    prev = seen if isinstance(seen, dict) else None
    sig = [info.get("entries"), info.get("last_add"), info.get("last_del")]
    want_sel = bool(info) and (prev is None or prev.get("sig") != sig)
    sel_rows = None
    if flagged or want_sel:
        try:
            sec2, rcs2 = sections(run_on(host, second_script(flagged, want_sel), timeout, local))
        except RuntimeError as e:
            sec2, rcs2 = {}, {}
            errors.append("second read: %s" % e)
        limits = parse_get(sec2.get("get", []))
        for s in sensors:
            if s["name"] in limits:
                s["thresholds"] = limits[s["name"]]
        if want_sel and "sel" in sec2:
            if rcs2.get("sel") == 0:
                sel_rows = parse_sel(sec2["sel"])
            else:
                errors.append("ipmitool sel elist: exit %s (the log is read again next time)" % rcs2.get("sel"))
    sel = dict(info)
    state = prev
    if sel_rows is not None:
        known = set(prev.get("keys", [])) if prev else set()
        new = [r for r in sel_rows if r["key"] not in known]
        sel["baseline"] = prev is None
        # not one entry known: the log was cleared, and what it holds is new
        sel["cleared"] = bool(known) and not (known & {r["key"] for r in sel_rows})
        sel["more"] = max(0, len(new) - max_events)
        sel["new"] = new[-max_events:]
        if sel["more"]:
            errors.append("%d older new log entries not sent (at most %d a run)" % (sel["more"], max_events))
        state = {"sig": sig, "keys": [r["key"] for r in sel_rows][-5000:]}
    else:
        sel["new"] = []
    out = {"read_at": now, "error": None, "sensors": sensors, "sel": sel}
    if unread:
        out["unread_types"] = unread
    if errors:
        out["notes"] = errors[:5]
    return out, state


def slurm_nodes(timeout, sinfo):
    """(the nodes Slurm lists, None) or (None, why not)."""
    try:
        out, err, rc = _bounded([sinfo, "-h", "-N", "-o", "%n"], {"PATH": PATH, "LC_ALL": "C"}, timeout)
    except (OSError, RuntimeError) as e:
        return None, "sinfo: %s" % e
    if rc != 0:
        return None, "sinfo: %s" % (err.decode("utf-8", "replace").strip()[-200:] or "exit %d" % rc)
    names = sorted({x.strip() for x in out.decode("utf-8", "replace").split()})
    return (names, None) if names else (None, "sinfo listed no nodes")


def check_folder(path):
    """--out's folder: root's, and nobody else may write in it (the output is
    replaced there as root)."""
    d = os.path.dirname(os.path.abspath(path))
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise SystemExit("power_root_probe: %s isn't a folder" % d)
    if os.geteuid() == 0 and (st.st_uid != 0 or st.st_mode & 0o022):
        raise SystemExit("power_root_probe: %s must be root's and not writable by others "
                         "(mkdir -m 755 %s; chown root: %s)" % (d, d, d))
    if os.path.islink(path):
        raise SystemExit("power_root_probe: %s is a symbolic link" % path)


def write_atomic(path, text, mode):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".nomad-power.")
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


def load_state(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def main(argv=None):
    ap = argparse.ArgumentParser(description="nomad power root helper: board power sensors and event log, read only.")
    ap.add_argument("--out", required=True)
    ap.add_argument("--nodes", default="")
    ap.add_argument("--self", dest="me", action="store_true")
    ap.add_argument("--sinfo", default="sinfo")
    ap.add_argument("--parallel", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--max-events", type=int, default=200)
    a = ap.parse_args(argv)
    if os.geteuid() != 0:
        sys.stderr.write("power_root_probe: not root; ipmitool needs root, so run it as root.\n")
    check_folder(a.out)
    lock = open(a.out + ".lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.stderr.write("power_root_probe: the last run is still going; this one stops.\n")
        return 0
    me = socket.gethostname().split(".")[0]
    state_path = a.out + ".state"
    state = load_state(state_path)
    seen = state.get("hosts") if isinstance(state.get("hosts"), dict) else {}
    notes, names, partial = [], [], False
    for n in [x.strip() for x in a.nodes.split(",") if x.strip()]:
        if n != "slurm":
            names.append(n)
            continue
        listed, why = slurm_nodes(min(a.timeout, 60), a.sinfo)
        if listed is None:
            # slurmctld down, say: the hosts read last time are read again, and the file says it
            # isn't the whole list (the collector forgets no host for it)
            before = state.get("hosts_read") if isinstance(state.get("hosts_read"), list) else []
            listed = sorted({str(h) for h in list(seen) + before} - {me})
            notes.append("couldn't list Slurm's nodes (%s): read the %d read before" % (why[:200], len(listed)))
            partial = True
        names += listed
    hosts = []
    for n in names:
        if not NAME_OK.match(n):
            sys.stderr.write("power_root_probe: %r isn't a host name; left out\n" % n[:80])
        elif n != me and n not in hosts:
            hosts.append(n)
    jobs = [(h, False) for h in hosts]
    if a.me or not a.nodes.strip():
        jobs.insert(0, (me, True))
    with ThreadPoolExecutor(max_workers=max(1, min(a.parallel, 32))) as pool:
        futures = [(h, pool.submit(read_host, h, local, seen.get(h), a.timeout, max(1, a.max_events)))
                   for h, local in jobs]
        results = [(h, f.result()) for h, f in futures]
    report = {"nomad_power": 1, "helper": VERSION, "written": stamp(),
              "from": me, "hosts": {}}
    if notes:
        report["notes"] = notes
    if partial:
        report["partial"] = True
    new_seen = {}
    for h, (out, st) in results:
        report["hosts"][h] = out
        if st is not None:
            new_seen[h] = st
        elif h in seen:
            new_seen[h] = seen[h]          # its log unread this time: what was seen stays seen
    read_ok = sorted(h for h, (out, _st) in results if not out.get("error"))
    write_atomic(state_path, json.dumps({"hosts": new_seen, "hosts_read": sorted(
        set(read_ok) | set(h for h, _r in results if h in (state.get("hosts_read") or []))),
        "at": time.time()}) + "\n", 0o600)
    write_atomic(a.out, json.dumps(report, sort_keys=True) + "\n", 0o644)
    return 0


if __name__ == "__main__":
    sys.exit(main())

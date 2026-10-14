# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
NØMAÐ Interactive Session Collector
Monitors RStudio and Jupyter sessions via process inspection.
No root or API tokens required.

Works standalone on Python 3.6+ or integrates with NØMAÐ framework.
"""

import logging
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# Default thresholds
DEFAULT_IDLE_SESSION_HOURS = 24
DEFAULT_MEMORY_HOG_MB = 4096
DEFAULT_MAX_IDLE_SESSIONS = 5

# Try to import framework (may fail on Python 3.6)
try:
    from nomad.collectors.base import BaseCollector, registry
    HAS_FRAMEWORK = True
except (ImportError, SyntaxError):
    HAS_FRAMEWORK = False
    BaseCollector = object
    registry = None


def get_process_memory(pid):
    """Get memory info from /proc/[pid]/status."""
    try:
        with open(f'/proc/{pid}/status') as f:
            content = f.read()
        rss = vms = 0
        for line in content.split('\n'):
            if line.startswith('VmRSS:'):
                rss = int(line.split()[1])
            elif line.startswith('VmSize:'):
                vms = int(line.split()[1])
        return {'rss_mb': round(rss / 1024, 1), 'vms_mb': round(vms / 1024, 1)}
    except:
        return {'rss_mb': 0, 'vms_mb': 0}


def _stat_fields(pid):
    """/proc/<pid>/stat after "(comm)" (a name may hold spaces and ")"):
    index 0 is field 3 (state), 11-14 utime, stime, cutime, cstime, 19
    starttime."""
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().rsplit(')', 1)[1].split()
    except Exception:
        return None


def get_process_cpu_ticks(pid, children=True):
    """CPU used so far by the process, in clock ticks, and (``children``)
    by the children it has waited for: forked workers that ended."""
    fields = _stat_fields(pid)
    try:
        return float(sum(int(fields[i]) for i in ((11, 12, 13, 14) if children else (11, 12))))
    except Exception:
        return None


_NAMES = {}


def user_name(uid):
    """The account name for a uid. ps aux cuts names longer than 8
    characters ("abcdefgh+"), which then match no group or roster."""
    if uid not in _NAMES:
        try:
            import pwd
            _NAMES[uid] = pwd.getpwuid(int(uid)).pw_name
        except Exception:
            _NAMES[uid] = str(uid)
    return _NAMES[uid]


_SERVERS = ('jupyter-lab', 'jupyter-notebook', 'jupyterhub-singleuser', 'jupyter-labhub',
            'jupyter-server', 'jupyter-singleuser')
_SERVER_MODULES = ('jupyterhub.singleuser', 'jupyter_server', 'jupyterlab', 'notebook',
                   'jupyterlab.labhubapp')


_PYTHON = re.compile(r'^(python|pypy)[0-9.]*$')

# Where a session's software comes from: a conda environment's programs are
# under .../envs/<name>/ (bin/python; R's lib/R/bin/exec/R). A course often
# has one of its own (a Jupyter kernel named for it), so the name says which
# class a session is for. Sites with another layout set environment_patterns
# ([interactive], or [collectors.interactive]): regular expressions matched
# against the program a session runs, the name in a group called "env"
# (else the first group). A session from the account's own environment (it
# owns it, or keeps it in its home) says so, and each says where its
# environment is: anyone can name one like a course, so on the hub one's own
# counts only for a faculty member whose environment others used too (an
# instructor who built their course's).
DEFAULT_ENVIRONMENT_PATTERNS = (r'/envs/(?P<env>[^/]+)/',)
_ENV_NAME = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.+-]{0,63}$')


def environment_patterns(values=None):
    """The patterns, compiled; one that isn't a regular expression is
    skipped (logged), so a typo costs the environments, not the collector."""
    out = []
    for text in (DEFAULT_ENVIRONMENT_PATTERNS if values is None else values):
        try:
            out.append(re.compile(str(text)))
        except re.error as e:
            logger.warning("interactive collector: environment_patterns: %r is not a "
                           "regular expression (%s); skipped", text, e)
    return out


def environment_of(args, patterns, pid=None):
    """The environment a command line runs from (``patterns`` against the
    program, its first word; a bare name, from ``pid``'s /proc/PID/exe when
    it can be read), or None: the base installation, or a path no pattern
    knows."""
    return environment_info(args, patterns, pid)[0]


def environment_info(args, patterns, pid=None, uid=None, seen=None):
    """(environment_of's name, own, where): own when the environment is
    account ``uid``'s -- it owns it, or keeps it in its home (``seen``: a
    cache for one run); where, the program's path up to the name (the
    environment's directory)."""
    words = (args or '').split()
    if not words:
        return None, False, None
    program = words[0]
    if '/' not in program and pid is not None:
        try:
            program = os.readlink(f'/proc/{pid}/exe')
        except OSError:
            return None, False, None
    for p in patterns:
        m = p.search(program)
        if not m:
            continue
        key = 'env' if 'env' in m.groupdict() else (1 if m.groups() else None)
        name = m.group(key) if key is not None else None
        if not name or not _ENV_NAME.match(name):
            continue
        root = program[:m.end(key)]
        return name, uid is not None and _own(root, uid, seen), root
    return None, False, None


def _own(root, uid, seen=None):
    """Whether the environment at ``root`` is the account's own: it owns
    it, or it is in its home. Unreadable: not known to be (shared ones are
    readable by all)."""
    key = (root, str(uid))
    if seen is not None and key in seen:
        return seen[key]
    own = False
    try:
        n = int(uid)
        try:
            own = os.stat(root).st_uid == n
        except OSError:
            pass
        if not own:
            try:
                import pwd
                home = pwd.getpwuid(n).pw_dir.rstrip('/')       # "/" (system accounts): ""
                own = bool(home) and (root + '/').startswith(home + '/')
            except (KeyError, ImportError):
                pass
    except (TypeError, ValueError):
        pass
    if seen is not None:
        seen[key] = own
    return own


def _python_runs(words):
    """What a Python command line runs: ('module', name), ('script', path),
    or None (-c, or nothing). Options before it are skipped, -X and -W with
    their value (python -Xfrozen_modules=off -m ipykernel_launcher, which
    ipykernel writes into kernel specs on Python 3.11 and later; python3 -s
    /usr/bin/jupyter-notebook, a distribution's script)."""
    i = 1
    while i < len(words):
        w = words[i]
        if w == '--':
            return ('script', words[i + 1]) if i + 1 < len(words) else None
        if w.startswith('--'):
            i += 1
            continue
        if w.startswith('-') and len(w) > 1:
            for j in range(1, len(w)):
                c = w[j]
                if c in 'mcXW':
                    value = w[j + 1:]
                    if not value:
                        i += 1
                        value = words[i] if i < len(words) else ''
                    if c == 'm':
                        return ('module', value)
                    if c == 'c':
                        return None
                    break
            i += 1
            continue
        return ('script', w)
    return None


def session_type(cmdline):
    """Which kind of interactive session a command line is, or None: by the
    program it runs (or the script or module Python runs), not by a word
    anywhere in it -- "pip install ipykernel" or "tail rsession.log" is not
    a session."""
    words = cmdline.split()
    if not words:
        return None
    names = [os.path.basename(words[0]).lower()]
    module = None
    if _PYTHON.match(names[0]):
        runs = _python_runs(words)
        if runs and runs[0] == 'module':
            module = runs[1].lower()
        elif runs:
            names.append(os.path.basename(runs[1]).lower())
    if 'rsession' in names:
        return 'RStudio'
    if (module or '').startswith('ipykernel') or any(n.startswith('ipykernel') for n in names):
        return 'Jupyter (Python)'
    if names[0] in ('r', 'r.exe') and 'irkernel' in cmdline.lower():
        return 'Jupyter (R)'
    if any(n in _SERVERS for n in names) or module in _SERVER_MODULES:
        return 'Jupyter Server'
    return None


def _start(fields):
    """When the process started: (ISO local time, unix seconds)."""
    try:
        with open('/proc/stat') as f:
            for line in f:
                if line.startswith('btime'):
                    boot_time = int(line.split()[1])
                    break
        clk_tck = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
        start_seconds = boot_time + (int(fields[19]) / clk_tck)
        return datetime.fromtimestamp(start_seconds).isoformat(), start_seconds
    except Exception:
        return None, None


def get_process_start_time(pid):
    """Get process start time from /proc/[pid]/stat."""
    return _start(_stat_fields(pid) or [])[0]


def calc_age_hours(start_time):
    """Calculate age in hours from start time."""
    if not start_time:
        return None
    try:
        start_dt = datetime.fromisoformat(start_time)
        age = datetime.now() - start_dt
        return round(age.total_seconds() / 3600, 1)
    except:
        return None


# Set when the last call could not list processes (so no "nobody is on"
# is concluded from it).
LAST_FAILED = False


def collect_sessions(server_id='local', env_patterns=None):
    """RStudio and Jupyter sessions, one per session: a process a session
    forked (a multiprocessing pool, R's mclapply) keeps its parent's command
    line and is folded into it -- its memory and CPU added to the session's --
    rather than counted as a session of its own. A kernel is its own session,
    apart from the server that started it. Each says the environment it runs
    from (``environment``, see environment_of; ``env_patterns``: compiled
    patterns, default the conda layout)."""
    global LAST_FAILED
    if env_patterns is None:
        env_patterns = environment_patterns()
    owned: dict = {}
    LAST_FAILED = False
    procs = {}
    try:
        ps_output = subprocess.check_output(
            ['ps', '-eo', 'uid=,pid=,ppid=,pcpu=,pmem=,args='],
            universal_newlines=True,
            stderr=subprocess.DEVNULL
        )
    except Exception as e:
        LAST_FAILED = True
        logger.warning(f"Failed to collect sessions: {e}")
        return []
    now = datetime.now()
    for line in ps_output.strip().split('\n'):
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        kind = session_type(parts[5])
        if kind is None:
            continue
        try:
            pid, ppid = int(parts[1]), int(parts[2])
            cpu_pct, mem_pct = float(parts[3]), float(parts[4])
        except ValueError:
            continue
        fields = _stat_fields(pid) or []
        start_time, start_epoch = _start(fields)
        mem_info = get_process_memory(pid)
        procs[pid] = {
            'uid': parts[0], 'ppid': ppid, 'kind': kind, 'cpu_percent': cpu_pct,
            # A Jupyter server runs from the hub's installation, whatever
            # its kernels use.
            'environment': (None, False, None) if kind == 'Jupyter Server' else
                           environment_info(parts[5], env_patterns, pid, parts[0], owned),
            'mem_percent': mem_pct, 'mem_mb': mem_info['rss_mb'],
            'mem_virtual_mb': mem_info['vms_mb'], 'start_time': start_time,
            'start_epoch': start_epoch,
            # A Jupyter server's children are kernels, sessions of their own:
            # the CPU of one it has shut down (and waited for) is that
            # kernel's, already counted, not the server's.
            'cpu_ticks': get_process_cpu_ticks(pid, children=(kind != 'Jupyter Server')),
        }

    def root(pid):
        seen = set()
        while True:
            p = procs[pid]
            parent = procs.get(p['ppid'])
            if (parent is None or parent['uid'] != p['uid'] or parent['kind'] != p['kind']
                    or p['ppid'] in seen):
                return pid
            seen.add(pid)
            pid = p['ppid']

    sessions = {}
    for pid in sorted(procs):
        r = root(pid)
        p = procs[pid]
        s = sessions.get(r)
        if s is None:
            top = procs[r]
            age = (time.time() - top['start_epoch']) if top['start_epoch'] else None
            s = sessions[r] = {
                'timestamp': now.isoformat(),
                'server_id': server_id,
                'session_type': top['kind'],
                'environment': top['environment'][0],
                'environment_own': top['environment'][1],
                'environment_path': top['environment'][2],
                'user': user_name(top['uid']),
                'pid': r,
                'cpu_percent': 0.0, 'mem_percent': 0.0, 'mem_mb': 0.0, 'mem_virtual_mb': 0.0,
                'start_time': top['start_time'],
                'age_hours': round(age / 3600, 1) if age is not None else None,
                'age_seconds': age,
                'cpu_ticks': 0.0,
                'processes': 0,
            }
        s['processes'] += 1
        s['cpu_percent'] += p['cpu_percent']
        s['mem_percent'] += p['mem_percent']
        s['mem_mb'] = round(s['mem_mb'] + p['mem_mb'], 1)
        s['mem_virtual_mb'] = round(s['mem_virtual_mb'] + p['mem_virtual_mb'], 1)
        if s['cpu_ticks'] is not None:
            s['cpu_ticks'] = None if p['cpu_ticks'] is None else s['cpu_ticks'] + p['cpu_ticks']
    out = []
    for s in sessions.values():
        # ps's %CPU is each process's average over its whole life; the daily
        # totals judge idleness from the CPU counters between runs.
        s['is_idle'] = s['cpu_percent'] < 1.0
        out.append(s)
    return out


def build_summary(sessions, idle_hours=24, memory_hog_mb=4096):
    """Build summary statistics from collected sessions."""
    users = set()
    total_memory = idle_count = 0
    rstudio_count = jupyter_python_count = jupyter_r_count = 0
    stale_count = memory_hog_count = 0

    for s in sessions:
        users.add(s['user'])
        total_memory += s['mem_mb']
        if s['is_idle']:
            idle_count += 1
        if s['session_type'] == 'RStudio':
            rstudio_count += 1
        elif s['session_type'] == 'Jupyter (Python)':
            jupyter_python_count += 1
        elif s['session_type'] == 'Jupyter (R)':
            jupyter_r_count += 1
        age = s.get('age_hours', 0) or 0
        if s['is_idle'] and age >= idle_hours:
            stale_count += 1
        if s['mem_mb'] >= memory_hog_mb:
            memory_hog_count += 1

    return {
        'total_sessions': len(sessions),
        'idle_sessions': idle_count,
        'total_memory_mb': round(total_memory, 1),
        'unique_users': len(users),
        'rstudio_sessions': rstudio_count,
        'jupyter_python_sessions': jupyter_python_count,
        'jupyter_r_sessions': jupyter_r_count,
        'stale_sessions': stale_count,
        'memory_hog_sessions': memory_hog_count
    }


def get_report(server_id='local', idle_hours=24, memory_hog_mb=4096, max_idle=5):
    """Get a full report with alerts."""
    sessions = collect_sessions(server_id)
    summary = build_summary(sessions, idle_hours, memory_hog_mb)

    users = {}
    for s in sessions:
        user = s['user']
        if user not in users:
            users[user] = {'sessions': 0, 'memory_mb': 0, 'idle': 0, 'rstudio': 0, 'jupyter': 0}
        users[user]['sessions'] += 1
        users[user]['memory_mb'] += s['mem_mb']
        if s['session_type'] == 'RStudio':
            users[user]['rstudio'] += 1
        else:
            users[user]['jupyter'] += 1
        if s['is_idle']:
            users[user]['idle'] += 1

    user_list = [{'user': u, **v} for u, v in sorted(users.items(), key=lambda x: -x[1]['memory_mb'])]

    stale_sessions = [s for s in sessions if s['is_idle'] and s.get('age_hours', 0) and s['age_hours'] >= idle_hours]
    memory_hogs = [s for s in sessions if s['mem_mb'] >= memory_hog_mb]
    idle_session_hogs = [u for u in user_list if u['idle'] > max_idle]

    by_type = {
        'RStudio': {'total': 0, 'idle': 0, 'memory_mb': 0},
        'Jupyter (Python)': {'total': 0, 'idle': 0, 'memory_mb': 0},
        'Jupyter (R)': {'total': 0, 'idle': 0, 'memory_mb': 0},
        'Jupyter Server': {'total': 0, 'idle': 0, 'memory_mb': 0}
    }
    for s in sessions:
        t = s['session_type']
        if t in by_type:
            by_type[t]['total'] += 1
            by_type[t]['memory_mb'] += s['mem_mb']
            if s['is_idle']:
                by_type[t]['idle'] += 1

    return {
        'timestamp': datetime.now().isoformat(),
        'server_id': server_id,
        'summary': {
            'total_sessions': summary['total_sessions'],
            'idle_sessions': summary['idle_sessions'],
            'total_memory_mb': summary['total_memory_mb'],
            'total_memory_gb': round(summary['total_memory_mb'] / 1024, 2),
            'unique_users': summary['unique_users']
        },
        'by_type': by_type,
        'users': user_list,
        'sessions': sorted(sessions, key=lambda x: -x['mem_mb']),
        'alerts': {
            'stale_sessions': sorted(stale_sessions, key=lambda x: -x.get('age_hours', 0)),
            'memory_hogs': sorted(memory_hogs, key=lambda x: -x['mem_mb']),
            'idle_session_hogs': idle_session_hogs
        },
        'thresholds': {
            'idle_session_hours': idle_hours,
            'memory_hog_mb': memory_hog_mb,
            'max_idle_sessions': max_idle
        }
    }


def print_report(data):
    """Print a human-readable report."""
    summary = data['summary']
    by_type = data['by_type']
    thresholds = data['thresholds']

    print("=" * 70)
    print("              Interactive Sessions Report")
    print("=" * 70)
    print("  Timestamp:      {}".format(data['timestamp']))
    print("  Server:         {}".format(data['server_id']))
    print("  Total Sessions: {}".format(summary['total_sessions']))
    print("  Idle Sessions:  {}".format(summary['idle_sessions']))
    print("  Total Memory:   {} GB".format(summary['total_memory_gb']))
    print("  Unique Users:   {}".format(summary['unique_users']))
    print("-" * 70)

    print("\n  SESSIONS BY TYPE:")
    print("  {:<20} {:>8} {:>8} {:>12}".format('Type', 'Total', 'Idle', 'Memory (MB)'))
    print("  {:<20} {:>8} {:>8} {:>12}".format('-'*20, '-'*8, '-'*8, '-'*12))
    for stype, stats in by_type.items():
        if stats['total'] > 0:
            print("  {:<20} {:>8} {:>8} {:>12.0f}".format(
                stype, stats['total'], stats['idle'], stats['memory_mb']))

    if data['users']:
        print("\n  TOP USERS BY MEMORY:")
        print("  {:<12} {:>8} {:>8} {:>8} {:>10} {:>6}".format(
            'User', 'Sessions', 'RStudio', 'Jupyter', 'Mem (MB)', 'Idle'))
        print("  {:<12} {:>8} {:>8} {:>8} {:>10} {:>6}".format(
            '-'*12, '-'*8, '-'*8, '-'*8, '-'*10, '-'*6))
        for u in data['users'][:10]:
            print("  {:<12} {:>8} {:>8} {:>8} {:>10.0f} {:>6}".format(
                u['user'][:12], u['sessions'], u['rstudio'], u['jupyter'],
                u['memory_mb'], u['idle']))

    alerts = data['alerts']
    if alerts['idle_session_hogs']:
        print("\n  [!] USERS WITH >{} IDLE SESSIONS:".format(thresholds['max_idle_sessions']))
        for u in alerts['idle_session_hogs']:
            print("    - {}: {} idle ({} RStudio, {} Jupyter), {:.0f} MB".format(
                u['user'], u['idle'], u['rstudio'], u['jupyter'], u['memory_mb']))

    if alerts['stale_sessions']:
        print("\n  [!] STALE SESSIONS (idle >{}h): {}".format(
            thresholds['idle_session_hours'], len(alerts['stale_sessions'])))
        for s in alerts['stale_sessions'][:5]:
            print("    - {}: {}, {:.0f}h old, {:.0f} MB".format(
                s['user'], s['session_type'], s['age_hours'], s['mem_mb']))

    if alerts['memory_hogs']:
        print("\n  [!] MEMORY HOGS (>{}GB): {}".format(
            thresholds['memory_hog_mb']/1024, len(alerts['memory_hogs'])))
        for s in alerts['memory_hogs'][:5]:
            print("    - {}: {}, {:.1f} GB".format(
                s['user'], s['session_type'], s['mem_mb']/1024))

    print("=" * 70)


# Framework integration (Python 3.7+)
if HAS_FRAMEWORK:
    @registry.register
    class InteractiveCollector(BaseCollector):
        """Collector for interactive computing sessions."""
        name = "interactive"
        description = "RStudio and Jupyter session monitor"
        default_interval = 300

        def __init__(self, config, db_path):
            super().__init__(config, db_path)
            self.idle_session_hours = config.get('idle_session_hours', DEFAULT_IDLE_SESSION_HOURS)
            self.memory_hog_mb = config.get('memory_hog_mb', DEFAULT_MEMORY_HOG_MB)
            self.max_idle_sessions = config.get('max_idle_sessions', DEFAULT_MAX_IDLE_SESSIONS)
            self.server_id = config.get('server_id', 'local')
            # Raw snapshots kept this many days (0: all); the daily totals
            # (interactive_daily) are kept regardless.
            self.sample_retention_days = int(config.get('sample_retention_days', 0) or 0)
            patterns = config.get('environment_patterns')
            if patterns is not None and not isinstance(patterns, (list, tuple)):
                patterns = [patterns]
            self.env_patterns = environment_patterns(patterns)

        def collect(self):
            sessions = collect_sessions(self.server_id, self.env_patterns)
            if LAST_FAILED:
                self.note = "could not list processes (ps failed): this run is not counted"
            elif not sessions:
                # Nobody on: nothing to store, but the time between runs must
                # stay known (else the next session's first interval looks
                # like a gap). Not when ps itself failed: that is not nobody.
                self._daily([])
            return sessions

        def _daily(self, sessions):
            from nomad.collectors import interactive_daily
            try:
                clk = float(os.sysconf(os.sysconf_names['SC_CLK_TCK']))
            except (ValueError, KeyError, OSError):
                clk = 100.0
            with self.get_db_connection() as conn:
                interactive_daily.update(conn, sessions, datetime.now(), self.server_id, clk)
                interactive_daily.prune(conn, self.sample_retention_days, datetime.now())
                conn.commit()

        def store(self, data):
            if not data:
                return
            with self.get_db_connection() as conn:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(interactive_sessions)")}
                if cols and 'environment' not in cols:
                    # Databases from before 1.12.5: the column, empty until now
                    # (another writer may have just added it).
                    try:
                        conn.execute("ALTER TABLE interactive_sessions ADD COLUMN environment TEXT")
                    except sqlite3.OperationalError as e:
                        if 'duplicate column' not in str(e):
                            raise
                for s in data:
                    conn.execute("""
                        INSERT INTO interactive_sessions
                        (timestamp, server_id, user, session_type, pid, cpu_percent,
                         mem_percent, mem_mb, mem_virtual_mb, start_time, age_hours, is_idle,
                         environment)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (s['timestamp'], s['server_id'], s['user'], s['session_type'],
                          s['pid'], s['cpu_percent'], s['mem_percent'], s['mem_mb'],
                          s['mem_virtual_mb'], s['start_time'], s['age_hours'], s['is_idle'],
                          s.get('environment')))
                summary = build_summary(data, self.idle_session_hours, self.memory_hog_mb)
                conn.execute("""
                    INSERT INTO interactive_summary
                    (timestamp, server_id, total_sessions, idle_sessions, total_memory_mb,
                     unique_users, rstudio_sessions, jupyter_python_sessions, jupyter_r_sessions,
                     stale_sessions, memory_hog_sessions)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (datetime.now().isoformat(), self.server_id, summary['total_sessions'],
                      summary['idle_sessions'], summary['total_memory_mb'], summary['unique_users'],
                      summary['rstudio_sessions'], summary['jupyter_python_sessions'],
                      summary['jupyter_r_sessions'], summary['stale_sessions'],
                      summary['memory_hog_sessions']))
                conn.commit()
            self._daily(data)

        def get_report(self):
            return get_report(self.server_id, self.idle_session_hours,
                            self.memory_hog_mb, self.max_idle_sessions)


if __name__ == '__main__':
    report = get_report()
    print_report(report)

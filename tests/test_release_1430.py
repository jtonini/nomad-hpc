"""1.43.0: acting on an interactive server's sessions (nomad interactive sessions|stop|clear|restart).

Nothing here signals a real process or runs sudo: /proc, sudo and systemctl
are stood in for, and every command nomad would run is checked as words.
"""
import json
import sqlite3
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from nomad import interactive_admin as ia
from nomad.actions import runner, spec
from nomad.collectors import interactive_daily as D

ON = {"actions": {"interactive": True}}
T0 = datetime(2026, 11, 12, 9, 0)


# --- the collector keeps when each session was last busy -------------------------------

def _s(user, kind, pid, start, ticks, age_seconds=None):
    return {"user": user, "session_type": kind, "pid": pid, "start_time": start, "mem_mb": 100.0,
            "cpu_ticks": ticks, "age_hours": 5.0, "age_seconds": age_seconds}


def _busy(conn):
    return dict(conn.execute("SELECT key, busy_at FROM interactive_state WHERE key != 'last_run'"))


def _known(conn):
    return dict(conn.execute("SELECT key, busy_known FROM interactive_state WHERE key != 'last_run'"))


def test_the_collector_keeps_when_a_session_was_last_busy():
    conn = sqlite3.connect(":memory:")
    D.ensure(conn)
    old = _s("ann", "RStudio", 101, "2026-11-10T08:00:00", 1000.0, age_seconds=2 * 86400)
    run = lambda when, sessions: D.update(conn, sessions, when, "web1", epoch=when.timestamp())  # noqa: E731
    run(T0, [old])
    assert _busy(conn) == {"101:2026-11-10T08:00:00": T0.isoformat()}      # its past isn't known: from now
    assert _known(conn) == {"101:2026-11-10T08:00:00": 0}                    # idle at least since then
    t1 = T0 + timedelta(minutes=5)
    run(t1, [{**old, "cpu_ticks": 1000.0 + 300 * 100 * 0.5}])               # half a core for 5 minutes
    assert _busy(conn)["101:2026-11-10T08:00:00"] == t1.isoformat()
    t2 = t1 + timedelta(minutes=5)
    new = _s("bob", "Jupyter Server", 202, (t2 - timedelta(minutes=2)).isoformat(), 5.0, age_seconds=120)
    run(t2, [{**old, "cpu_ticks": 1000.0 + 300 * 100 * 0.5 + 1}, new])        # idle now; bob's new
    got = _busy(conn)
    assert got["101:2026-11-10T08:00:00"] == t1.isoformat()                 # kept: idle since t1
    assert got[f"202:{new['start_time']}"] == new["start_time"]              # new: as busy when it started
    assert set(_known(conn).values()) == {1}


def test_the_hub_gets_how_long_each_open_session_has_been_idle():
    conn = sqlite3.connect(":memory:")
    D.ensure(conn)
    a = _s("ann", "RStudio", 101, "2026-11-10T08:00:00", 1000.0, age_seconds=2 * 86400)
    b = _s("bob", "Jupyter Server", 202, "2026-11-11T08:00:00", 50.0, age_seconds=86400)
    rows = lambda: sorted(conn.execute("SELECT server_id, pid, start_time, busy_at, busy_known "  # noqa: E731
                                       "FROM interactive_idle"))
    D.update(conn, [a, b], T0, "web1", epoch=T0.timestamp())
    assert rows() == [("web1", 101, "2026-11-10T08:00:00", T0.isoformat(), 0),
                      ("web1", 202, "2026-11-11T08:00:00", T0.isoformat(), 0)]
    changes = []
    conn.set_trace_callback(lambda sql: changes.append(sql) if sql.lstrip().startswith(
        ("INSERT INTO interactive_idle", "UPDATE interactive_idle", "DELETE FROM interactive_idle")) else None)
    t1 = T0 + timedelta(minutes=5)
    D.update(conn, [{**a, "cpu_ticks": 1000.0 + 300 * 100}, b], t1, "web1", epoch=t1.timestamp())
    assert rows()[0][3:] == (t1.isoformat(), 1) and rows()[1][3:] == (T0.isoformat(), 0)
    assert len([c for c in changes if c.lstrip().startswith("UPDATE")]) == 1      # bob's row: unchanged
    t2 = t1 + timedelta(minutes=5)
    D.update(conn, [b], t2, "web1", epoch=t2.timestamp())                          # ann's ended
    assert [r[1] for r in rows()] == [202]
    D.update(conn, [], t2 + timedelta(minutes=5), "web1", epoch=(t2 + timedelta(minutes=5)).timestamp())
    assert rows() == []


def test_a_state_from_before_gets_the_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE interactive_state (key TEXT PRIMARY KEY, cpu_ticks REAL, day TEXT, seen_at TEXT)")
    conn.execute("INSERT INTO interactive_state VALUES ('1:x', 5, '2026-11-12', 'x')")
    D.ensure(conn)
    D.ensure(conn)
    assert {"busy_at", "busy_known"} <= {r[1] for r in conn.execute("PRAGMA table_info(interactive_state)")}


# --- settings ------------------------------------------------------------------------

def test_settings():
    s = ia.settings({})
    assert s["on"] is False and s["rstudio_unit"] == "rstudio-server" and s["jupyter_unit"] == "jupyterhub"
    assert s["stale_hours"] == 24 and not s["problems"]
    s = ia.settings({"actions": {"interactive": True, "stale_hours": 12}, "collectors": {"interactive": {}}})
    assert s["on"] and s["stale_hours"] == 12
    assert ia.settings({"collectors": {"interactive": {"idle_session_hours": 48}}})["stale_hours"] == 48
    bad = ia.settings({"actions": {"interactive": "yes", "rstudio_server": "rstudio-server",
                                   "jupyter_unit": "jupyterhub; rm", "stale_hours": "soon"}})
    assert bad["on"] is False and len(bad["problems"]) == 4 and bad["jupyter_unit"] == "jupyterhub"


# --- a stand-in server ---------------------------------------------------------------

class World:
    """Processes, accounts, sudo's rules and systemd, as a server has them."""

    def __init__(self):
        self.procs = {
            4101: {"pid": 4101, "uid": 5001, "user": "ann", "kind": "RStudio", "started": "2026-11-10T08:00:00",
                   "ppid": 1, "cmd": "/usr/lib/rstudio-server/bin/rsession -u ann"},
            4202: {"pid": 4202, "uid": 5002, "user": "bob", "kind": "Jupyter Server",
                   "started": "2026-11-11T07:00:00.250000", "ppid": 1, "cmd": "python3 /opt/jh/bin/jupyterhub-singleuser"},
            4203: {"pid": 4203, "uid": 5002, "user": "bob", "kind": "Jupyter (Python)",
                   "started": "2026-11-11T07:05:00", "ppid": 4202, "cmd": "python -m ipykernel_launcher -f k.json"},
            4300: {"pid": 4300, "uid": 5003, "user": "cat", "kind": None, "started": "2026-11-12T01:00:00",
                   "ppid": 1, "cmd": "bash"},
            900: {"pid": 900, "uid": 0, "user": "root", "kind": "Jupyter Server", "started": "2026-11-01T00:00:00",
                  "ppid": 1, "cmd": "jupyterhub-singleuser"},
        }
        self.accounts = {"ann": 5001, "bob": 5002, "cat": 5003, "root": 0, "daemon": 2}
        self.may_as = {"bob"}                  # members of the group sudo lets the account act as
        self.may_root = {"suspend", "restart rstudio-server", "restart jupyterhub"}
        self.units = {"rstudio-server": "active", "jupyterhub": "active"}
        self.ran = []
        self.dies = True                       # a signalled process ends

    def run(self, argv, timeout=30):
        self.ran.append(list(argv))
        if argv[:3] == ["/usr/bin/sudo", "-n", "-l"]:
            rest = argv[3:]
            if rest[0] == "-u":
                return (0, "", "") if rest[1] in self.may_as else (1, "", "Sorry, user installer is not allowed")
            words = " ".join(rest)
            ok = (rest[0] == "/usr/sbin/rstudio-server" and "suspend" in self.may_root) or \
                any(words == f"/usr/bin/systemctl {r}" for r in self.may_root)
            return (0, "", "") if ok else (1, "", "Sorry, user installer is not allowed")
        if argv[:2] == ["/usr/bin/systemctl", "show"]:
            unit = argv[-1]
            if unit not in self.units:
                return 0, "LoadState=not-found\nActiveState=inactive\nActiveEnterTimestamp=\n", ""
            return 0, (f"LoadState=loaded\nActiveState={self.units[unit]}\n"
                       "ActiveEnterTimestamp=Thu 2026-11-12 06:00:00 EST\n"
                       "ExecStart={ path=/opt/jh/bin/jupyterhub ; argv[]=/opt/jh/bin/jupyterhub -f "
                       "/etc/jupyterhub/jupyterhub_config.py ; }\nWorkingDirectory=/etc/jupyterhub\n"), ""
        if argv[:2] == ["/usr/bin/systemctl", "is-active"]:
            return 0, self.units.get(argv[2], "inactive") + "\n", ""
        if argv[:2] == ["/usr/bin/sudo", "-n"]:
            pid = int(argv[-1]) if argv[-1].isdigit() else None
            if pid and self.dies:
                self.procs.pop(pid, None)
                if self.procs.get(4203) and pid == 4202:
                    self.procs.pop(4203)
            return 0, "", ""
        return 127, "", "not stood in for"

    def sessions(self):
        out = []
        for p in self.procs.values():
            if p["kind"] and p["uid"] >= 1000:
                age = (T0 - datetime.fromisoformat(p["started"])).total_seconds()
                out.append({"user": p["user"], "pid": p["pid"], "session_type": p["kind"], "start_time": p["started"],
                            "age_hours": round(age / 3600, 1), "mem_mb": 200.0, "processes": 1,
                            "environment": None})
        return out


class _AtT0(datetime):
    """The clock these sessions were written for (1.47.0: the tests had read
    the real one, and bob's kernel, idle an hour at T0, passed 24 hours a day
    later)."""
    @classmethod
    def now(cls, tz=None):
        return T0


@pytest.fixture
def w(monkeypatch, tmp_path):
    world = World()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ia, "datetime", _AtT0)
    monkeypatch.setattr(ia, "proc", lambda pid: dict(world.procs[pid]) if pid in world.procs else None)
    monkeypatch.setattr(ia, "_which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(ia, "rstudio_server", lambda s: "/usr/sbin/rstudio-server")
    monkeypatch.setattr(ia, "_account", lambda: "installer")
    monkeypatch.setattr(ia, "_install", lambda: None)
    monkeypatch.setattr(ia, "jupyter_config", lambda unit, run=None: str(tmp_path / "jupyterhub_config.py"))
    monkeypatch.setattr(ia.time, "sleep", lambda s: None)
    monkeypatch.setattr(ia, "STOP_WAIT", {"RStudio": 0.05, "Jupyter Server": 0.05})
    monkeypatch.setattr(ia, "KERNEL_WAIT", 0.05)

    class PW:
        def __init__(self, name):
            self.pw_name, self.pw_uid = name, world.accounts[name]

    def getpwnam(name):
        if name not in world.accounts:
            raise KeyError(name)
        return PW(name)
    monkeypatch.setattr(ia.pwd, "getpwnam", getpwnam)
    db = tmp_path / "site.db"
    conn = sqlite3.connect(db)
    D.ensure(conn)
    conn.executemany("INSERT INTO interactive_state (key, cpu_ticks, day, seen_at, busy_at) VALUES (?, 0, '', '', ?)",
                     [("4101:2026-11-10T08:00:00", "2026-11-11T05:00:00"),          # ann idle 28 h
                      ("4202:2026-11-11T07:00:00.250000", "2026-11-11T07:00:00"),   # bob's server: 26 h
                      ("4203:2026-11-11T07:05:00", "2026-11-12T08:00:00")])         # bob's kernel: 1 h
    conn.commit()
    conn.close()
    (tmp_path / "jupyterhub_config.py").write_text("c.JupyterHub.admin_users = set()\n")
    world.db, world.home = db, tmp_path
    return world


def _log(w):
    p = w.home / ".local" / "share" / "nomad" / "interactive.log"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# --- looking -------------------------------------------------------------------------

def test_sessions_and_how_long_idle(w):
    found = {x["pid"]: x for x in ia.sessions(w.db, now=T0, collect=w.sessions)}
    assert set(found) == {4101, 4202, 4203}                                 # root's isn't a session listed
    assert found[4101]["idle_hours"] == 28.0
    assert found[4203]["idle_hours"] == 1.0
    assert found[4202]["idle_hours"] == 1.0                                 # as idle as bob's kernel
    assert [x["pid"] for x in ia.stale(list(found.values()), 24)] == [4101]


def test_idle_at_least_since_nomad_began_watching(w):
    con = sqlite3.connect(w.db)
    con.execute("UPDATE interactive_state SET busy_known = 0 WHERE key LIKE '4101:%' OR key LIKE '4202:%'")
    con.commit()
    con.close()
    found = {x["pid"]: x for x in ia.sessions(w.db, now=T0, collect=w.sessions)}
    assert found[4101]["idle_hours"] == 28.0 and found[4101]["idle_at_least"] is True
    assert found[4202]["idle_hours"] == 1.0 and found[4202]["idle_at_least"] is False    # its kernel: known
    assert [x["pid"] for x in ia.stale(list(found.values()), 24)] == [4101]           # a lower bound is enough
    assert "idle 28.0 h or more" in "\n".join(ia.lines({"what": "sessions", "sessions": list(found.values())}))


def test_a_server_whose_kernels_arent_watched_yet_isnt_called_idle(w):
    sqlite3.connect(w.db).execute("DELETE FROM interactive_state WHERE key LIKE '4203:%'").connection.commit()
    found = {x["pid"]: x for x in ia.sessions(w.db, now=T0, collect=w.sessions)}
    assert found[4203]["idle_hours"] is None and found[4202]["idle_hours"] is None


def test_look_says_what_may_be_done(w):
    res = ia.look(config=ON, db=w.db, run=w.run, collect=w.sessions)
    assert res["ok"] and not res["problems"] and res["stale"] == 1 and res["watched"] == 3
    assert res["services"]["rstudio"]["can_restart"] and res["services"]["jupyter"]["stops_servers"] is True
    assert res["can"]["suspend_rstudio"] is True
    w.may_root = set()
    res = ia.look(config={}, db=w.db, run=w.run, collect=w.sessions)
    assert res["off"] and res["problems"] and not res["services"]["jupyter"]["can_restart"]
    assert "not allowed" in res["can"]["why_not_rstudio"]
    assert not any(a[:2] == ["/usr/bin/sudo", "-n"] and a[2] != "-l" for a in w.ran)   # nothing was run


# --- stopping one --------------------------------------------------------------------

def test_an_rstudio_session_is_suspended_with_workbenchs_own_command(w):
    res = ia.stop("ann", 4101, started="2026-11-10T08:00:00", by="jtonini", config=ON, run=w.run)
    assert res["ok"] and not res["applied"] and res["runs_as"] == "root"
    assert res["command"] == ["/usr/bin/sudo", "-n", "/usr/sbin/rstudio-server", "suspend-session", "4101"]
    assert 4101 in w.procs and "comes back" in res["does"]
    res = ia.stop("ann", 4101, started="2026-11-10T08:00:00", by="jtonini", apply=True, config=ON, run=w.run)
    assert res["ok"] and res["applied"] and res["said"].startswith("suspended") and 4101 not in w.procs
    assert _log(w)[-1]["what"] == "stop" and _log(w)[-1]["by"] == "jtonini" and _log(w)[-1]["ok"]


def test_a_jupyter_server_is_stopped_as_its_person(w):
    res = ia.stop("bob", 4202, started="2026-11-11T07:00:00.250000", apply=True, config=ON, run=w.run)
    assert res["ok"] and res["runs_as"] == "bob"
    assert res["command"] == ["/usr/bin/sudo", "-n", "-u", "bob", "/usr/bin/kill", "-TERM", "4202"]
    assert 4202 not in w.procs


def test_someone_sudo_cant_act_as_is_left_alone(w):
    w.procs[4404] = {**w.procs[4202], "pid": 4404, "uid": 5003, "user": "cat"}
    res = ia.stop("cat", 4404, config=ON, run=w.run)
    assert not res["ok"] and "act as cat" in res["problems"][0]
    res = ia.stop("cat", 4404, apply=True, config=ON, run=w.run)
    assert not res["ok"] and not res["applied"] and 4404 in w.procs


@pytest.mark.parametrize("person,pid,started,why", [
    ("ann", 4101, "2026-11-10T08:00:01", "isn't the session that was looked at"),
    ("bob", 4101, None, "isn't bob's"),
    ("cat", 4300, None, "isn't an RStudio or Jupyter session"),
    ("root", 900, None, "system account"),
    ("daemon", 900, None, "system account"),
    ("nobody", 4101, None, "no account called"),
    ("ann", 9999, None, "isn't running"),
    ("ann; id", 4101, None, "NetID"),
    ("ann", 4101, "yesterday", "is a time"),
    ("ann", True, None, "process number"),
])
def test_what_isnt_a_persons_session_is_refused(w, person, pid, started, why):
    with pytest.raises(ia.SessionError, match=why):
        ia.stop(person, pid, started=started, apply=True, config=ON, run=w.run)
    assert not any(a[:2] == ["/usr/bin/sudo", "-n"] and a[2] != "-l" for a in w.ran)


def test_off_or_an_open_install_stops_it(w, monkeypatch):
    res = ia.stop("ann", 4101, apply=True, config={}, run=w.run)
    assert not res["ok"] and not res["applied"] and "off here" in res["said"] and 4101 in w.procs
    monkeypatch.setattr(ia, "_install", lambda: "/opt/nomad/venv is writable by its group")
    res = ia.stop("ann", 4101, apply=True, config=ON, run=w.run)
    assert not res["ok"] and "someone else could change" in res["said"] and 4101 in w.procs


def test_the_process_ending_between_the_look_and_the_signal(w, monkeypatch):
    seen = []
    real = ia.proc

    def going(pid):                                   # there when checked, gone just before the signal
        seen.append(pid)
        return real(pid) if len(seen) < 2 else None
    monkeypatch.setattr(ia, "proc", going)
    res = ia.stop("ann", 4101, apply=True, config=ON, run=w.run)
    assert res["ok"] and res["said"] == "it had already ended"
    assert not any(a[:2] == ["/usr/bin/sudo", "-n"] and a[2] != "-l" for a in w.ran)


def test_a_session_that_wont_end(w):
    w.dies = False
    res = ia.stop("bob", 4202, apply=True, config=ON, run=w.run)
    assert not res["ok"] and "still running" in res["said"]
    res = ia.stop("ann", 4101, apply=True, config=ON, run=w.run)
    assert res["ok"] and "still saving" in res["said"]          # R saving a big workspace: asked, it goes


# --- clearing the stale ones ---------------------------------------------------------

def test_clear_suspends_and_stops_the_stale_ones(w):
    w.procs[4501] = {**w.procs[4202], "pid": 4501, "started": "2026-11-09T07:00:00"}     # bob's second server
    con = sqlite3.connect(w.db)
    con.execute("INSERT INTO interactive_state (key, cpu_ticks, day, seen_at, busy_at) VALUES "
                "('4501:2026-11-09T07:00:00', 0, '', '', '2026-11-09T07:00:00')")
    con.execute("UPDATE interactive_state SET busy_at = '2026-11-10T07:00:00' WHERE key LIKE '4203:%'")
    con.commit()
    con.close()
    res = ia.clear(config=ON, db=w.db, run=w.run, collect=w.sessions)
    assert res["ok"] and not res["applied"] and [x["pid"] for x in res["stale"]] == [4501, 4101, 4202]
    assert res["hours"] == 24 and 4101 in w.procs
    res = ia.clear(hours=27, kind="jupyter", config=ON, db=w.db, run=w.run, collect=w.sessions)
    assert [x["pid"] for x in res["stale"]] == [4501]
    res = ia.clear(by="jtonini", apply=True, config=ON, db=w.db, run=w.run, collect=w.sessions)
    assert res["ok"] and res["applied"] and res["said"] == "3 of 3 stale sessions cleared"
    assert {4101, 4202, 4501}.isdisjoint(w.procs)
    assert _log(w)[-1]["what"] == "clear" and len(_log(w)[-1]["sessions"]) == 3


def test_clear_leaves_alone_whom_sudo_cant_act_as(w):
    w.procs[4404] = {**w.procs[4202], "pid": 4404, "uid": 5003, "user": "cat", "started": "2026-11-09T07:00:00"}
    con = sqlite3.connect(w.db)
    con.execute("INSERT INTO interactive_state (key, cpu_ticks, day, seen_at, busy_at) VALUES "
                "('4404:2026-11-09T07:00:00', 0, '', '', '2026-11-09T07:00:00')")
    con.commit()
    con.close()
    res = ia.clear(config=ON, db=w.db, run=w.run, collect=w.sessions)
    assert res["ok"] and [x["pid"] for x in res["stale"]] == [4404, 4101]
    assert res["stale"][0]["why_not"] and res["stale"][1]["why_not"] is None
    assert any("1 of them can't be stopped here" in n and "(cat)" in n for n in res["notes"])
    res = ia.clear(apply=True, config=ON, db=w.db, run=w.run, collect=w.sessions)
    assert res["ok"] and res["said"] == "1 of 1 stale sessions cleared; 1 left alone (sudo can't act as their people here)"
    assert 4404 in w.procs and 4101 not in w.procs
    w.procs[4101] = {**w.procs[4404], "pid": 4101}
    w.may_root = set()
    res = ia.clear(config=ON, db=w.db, run=w.run, collect=w.sessions)
    assert not res["ok"] and "none of them can be stopped here" in res["problems"]


def test_clear_with_nothing_stale_and_bad_requests(w):
    res = ia.clear(hours=100, config=ON, db=w.db, run=w.run, collect=w.sessions)
    assert res["ok"] and not res["stale"] and "no session has been idle 100 hours" in res["notes"]
    for bad in ({"hours": 0}, {"kind": "everything"}, {"by": "a b"}):
        with pytest.raises(ia.SessionError):
            ia.clear(config=ON, db=w.db, run=w.run, collect=w.sessions, **bad)


# --- restarting a service ------------------------------------------------------------

def test_restarting_rstudio(w):
    res = ia.restart("rstudio", config=ON, run=w.run, collect=w.sessions)
    assert res["ok"] and res["sessions"] == 1 and res["command"] == [
        "/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "rstudio-server"]
    res = ia.restart("rstudio", apply=True, by="jtonini", config=ON, run=w.run, collect=w.sessions)
    assert res["ok"] and res["applied"] and res["after"] == "active"
    assert ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "rstudio-server"] in w.ran


def test_restarting_jupyterhub_needs_the_number_of_servers_it_stops(w):
    res = ia.restart("jupyter", config=ON, run=w.run, collect=w.sessions)
    assert res["ok"] and res["stops_servers"] is True and res["asks"] == 1 and "stops every" in res["does"]
    assert any("--servers 1" in n for n in res["notes"])
    res = ia.restart("jupyter", apply=True, config=ON, run=w.run, collect=w.sessions)
    assert not res["ok"] and not res["applied"] and "servers = 1" in res["said"]
    res = ia.restart("jupyter", servers=3, apply=True, config=ON, run=w.run, collect=w.sessions)
    assert not res["ok"] and "not 3: the count moved on" in res["said"]
    assert not ia.restart("jupyter", servers=3, config=ON, run=w.run, collect=w.sessions)["ok"]   # the dry run too
    assert ia.restart("jupyter", servers=1, config=ON, run=w.run, collect=w.sessions)["ok"]
    assert not any("restart" in a for a in w.ran if a[:2] == ["/usr/bin/sudo", "-n"] and a[2] != "-l")
    res = ia.restart("jupyter", servers=1, apply=True, config=ON, run=w.run, collect=w.sessions)
    assert res["ok"] and res["applied"]
    w.procs.pop(4202)
    w.procs.pop(4203)
    res = ia.restart("jupyter", apply=True, config=ON, run=w.run, collect=w.sessions)   # no servers: nothing to say
    assert res["ok"] and res["applied"] and res["asks"] == 0


def test_a_hub_that_keeps_its_servers(w):
    (w.home / "jupyterhub_config.py").write_text("c.JupyterHub.cleanup_servers = True\n"
                                                 "c.JupyterHub.cleanup_servers = False  # later wins\n")
    res = ia.restart("jupyter", apply=True, config=ON, run=w.run, collect=w.sessions)
    assert res["ok"] and res["stops_servers"] is False and "keep running" in res["does"]


def test_cleanup_servers_and_the_hubs_config():
    assert ia.cleanup_servers(None) is None
    assert ia.cleanup_servers("/nonexistent/x.py") is None


def test_restart_refusals(w):
    w.may_root = {"suspend"}
    res = ia.restart("rstudio", apply=True, config=ON, run=w.run, collect=w.sessions)
    assert not res["ok"] and "sudoers" in res["said"]
    del w.units["jupyterhub"]
    res = ia.restart("jupyter", config=ON, run=w.run, collect=w.sessions)
    assert not res["ok"] and any("no unit called jupyterhub" in p for p in res["problems"])
    with pytest.raises(ia.SessionError):
        ia.restart("httpd", config=ON, run=w.run, collect=w.sessions)


# --- the catalog and the command line ------------------------------------------------

def test_the_catalog():
    look = spec.get("interactive.sessions")
    assert look.where == frozenset({spec.SITE}) and look.names and not look.changes
    stop = spec.get("interactive.stop")
    v = spec.check(stop, {"person": "ann", "pid": "4101", "started": "2026-11-10T08:00:00", "by": "jt"})
    assert runner.build_argv(stop, v)[3:] == ["interactive", "stop", "--json", "--started=2026-11-10T08:00:00",
                                              "--by", "jt", "--", "ann", "4101"]
    for bad in ({"person": "ann", "pid": 1, "started": "x"}, {"person": "-ann", "pid": 5, "started": "x"},
                {"person": "ann", "pid": 5}, {"person": "ann", "pid": 5, "started": 'a"b'}):
        with pytest.raises(spec.ActionError):
            spec.check(stop, bad)
    clear = spec.get("interactive.clear")
    assert runner.build_argv(clear, spec.check(clear, {"hours": 48, "apply": True}))[3:] == [
        "interactive", "clear", "--json", "--hours", "48", "--kind", "all", "--apply"]
    restart = spec.get("interactive.restart")
    assert runner.build_argv(restart, spec.check(restart, {"service": "jupyter", "servers": 4}))[3:] == [
        "interactive", "restart", "--json", "--servers", "4", "--", "jupyter"]
    with pytest.raises(spec.ActionError):
        spec.check(restart, {"service": "httpd"})


def test_the_command_line(w, monkeypatch):
    from nomad.cli import cli
    orig = ia._run
    monkeypatch.setattr(ia, "_collect", w.sessions)
    for f in (ia.may, ia._plan_stop, ia._stop_one, ia.unit_show):
        monkeypatch.setattr(f, "__defaults__", tuple(w.run if d is orig else d for d in f.__defaults__))
    for f in (ia.stop, ia.clear, ia.restart, ia.look):
        monkeypatch.setattr(f, "__kwdefaults__", {k: (w.run if v is orig else v) for k, v in f.__kwdefaults__.items()})
    toml = w.home / "nomad.toml"
    toml.write_text(f'[general]\ndata_dir = "{w.home}"\n[database]\npath = "{w.db}"\n[actions]\ninteractive = true\n')
    go = lambda *a: CliRunner().invoke(cli, ["-c", str(toml), "interactive", *a])  # noqa: E731
    r = go("stop", "ann", "4101", "--json")
    d = json.loads(r.output)
    assert r.exit_code == 0 and d["ok"] and d["command"][-2:] == ["suspend-session", "4101"], r.output
    r = go("stop", "root", "900", "--json")
    assert r.exit_code == 1 and "system account" in json.loads(r.output)["error"]
    r = go("restart", "jupyter")
    assert "stops every person's server" in r.output and "--servers 1" in r.output, r.output
    r = go("sessions")
    assert r.exit_code == 0 and "1 idle 24 hours or more" in r.output, r.output
    r = go("clear", "--apply", "--by", "jtonini")
    assert r.exit_code == 0 and "1 of 1 stale sessions cleared" in r.output and 4101 not in w.procs, r.output


def test_proc_reads_this_process():
    import os
    me = ia.proc(os.getpid())
    assert me["uid"] == os.getuid() and me["kind"] is None and ia._STARTED.fullmatch(me["started"])
    assert ia.proc(4194999) is None

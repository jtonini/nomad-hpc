# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""Guests on condos, and the owners' priority there (1.28.0).

A condo's owners paid for its nodes; others may use them while they are
free (a guest partition, ``condo_overflow``), and give them back when the
owners need them: Slurm preempts the guest's job (``preempt/partition_prio``:
the owners' partition at a higher ``PriorityTier``; the guest partition's
``PreemptMode=REQUEUE``): it signals the job's steps (SIGTERM to programs
started with srun; a program the script runs itself hears nothing until the
end), waits up to its ``GraceTime``, then stops the job and puts it back in
the queue. That is Slurm's to do; this module says how far a
site's Slurm is from it and what would change, from what nomad already reads:
each partition's settings (nomad.partitions), Slurm's own preemption settings
(``scontrol show config``: read with them, ``slurm_settings``) and the condo
reports' report.toml (each condo's owners, nodes and members).

Nothing here changes Slurm. The switch is Slurm's configuration, applied by
the site's staff (with a committee's approval, at Richmond): nomad follows
what Slurm does -- the Submit page's warning, the requeue checks and the
messages after a preemption appear only where Slurm really preempts.

    [submit.preemption]            # the hub's nomad.toml
    guest_partition = "condo_overflow"   # the guests' partition the plan names
    grace_minutes = 30             # GraceTime the plan proposes
    owner_tier = 2                 # the owners' PriorityTier the plan proposes
    notices = false                # email a person when a job of theirs is preempted
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

TABLE = "slurm_settings"
KEYS = ("PreemptType", "PreemptMode", "PreemptExemptTime", "JobRequeue", "AccountingStorageEnforce")
CHECK_EVERY = timedelta(hours=1)


# ── Slurm's own preemption settings ──────────────────────────────────────

def parse_config(text: str) -> dict:
    """``scontrol show config``'s "Key = value" lines, the preemption keys."""
    out = {}
    for line in (text or "").splitlines():
        key, eq, value = line.partition("=")
        if eq and key.strip() in KEYS:
            out[key.strip()] = value.strip()
    return out


def store_config(conn: sqlite3.Connection, cluster: str, values: dict, now: datetime | None = None) -> int:
    """Keep the values; a row is written when it changed or wasn't checked
    for CHECK_EVERY (each write is a row the hub's sync sends). Returns rows
    written."""
    now = (now or datetime.now()).replace(microsecond=0)
    conn.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (cluster TEXT NOT NULL, key TEXT NOT NULL, value TEXT, "
                 "changed_at TEXT, checked_at TEXT, PRIMARY KEY (cluster, key))")
    have = {k: (v, c) for k, v, c in conn.execute(
        f"SELECT key, value, checked_at FROM {TABLE} WHERE cluster = ?", (cluster,))}
    n = 0
    for key, value in values.items():
        old = have.get(key)
        if old and old[0] == value:
            try:
                if now - datetime.fromisoformat(old[1]) < CHECK_EVERY:
                    continue
            except (TypeError, ValueError):
                pass
            conn.execute(f"UPDATE {TABLE} SET checked_at = ? WHERE cluster = ? AND key = ?",
                         (now.isoformat(), cluster, key))
        else:
            conn.execute(f"INSERT OR REPLACE INTO {TABLE} (cluster, key, value, changed_at, checked_at) "
                         "VALUES (?, ?, ?, ?, ?)", (cluster, key, value, now.isoformat(), now.isoformat()))
        n += 1
    return n


def config_of(conn: sqlite3.Connection, site: str | None = None) -> dict:
    """{key: value} as last read (on a hub: one site's)."""
    try:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({TABLE})")}
        if not cols:
            return {}
        if site is not None and "source_site" in cols:
            rows = conn.execute(f"SELECT key, value FROM {TABLE} WHERE source_site = ?", (site,)).fetchall()
        else:
            rows = conn.execute(f"SELECT key, value FROM {TABLE}").fetchall()
    except sqlite3.Error:
        return {}
    return dict(rows)


def is_on(cfg: dict) -> bool:
    """Whether Slurm preempts at all: a PreemptType other than none."""
    t = str(cfg.get("PreemptType") or "").strip().lower()
    return bool(t) and t not in ("(null)", "none", "preempt/none")


def preempts(p: dict, partitions: list[dict], cfg: dict) -> dict | None:
    """How Slurm treats a job in partition ``p`` when a partition above it
    needs its nodes: {mode, grace_s, requeue} (requeue: put back in the
    queue, else cancelled), or None when it never does (preemption off, or
    nothing outranks it on its nodes)."""
    from nomad.partitions import is_guest
    if not is_on(cfg) or not is_guest(p, partitions):
        return None
    mode = str(p.get("preempt_mode") or "").upper()
    requeue = "REQUEUE" in mode and str(cfg.get("JobRequeue") or "1").strip() != "0"
    return {"mode": mode, "grace_s": int(p.get("grace_time_s") or 0), "requeue": requeue}


# ── the condos against Slurm ─────────────────────────────────────────────

@dataclass
class Settings:
    guest_partition: str = "condo_overflow"
    grace_minutes: int = 30
    owner_tier: int = 2
    notices: bool = False


def settings(config: dict | None) -> Settings:
    s = Settings()
    sub = (config or {}).get("submit") if isinstance(config, dict) else None
    raw = sub.get("preemption") if isinstance(sub, dict) and isinstance(sub.get("preemption"), dict) else {}
    if isinstance(raw.get("guest_partition"), str) and raw["guest_partition"].strip():
        s.guest_partition = raw["guest_partition"].strip()
    for key, low, high in (("grace_minutes", 0, 24 * 60), ("owner_tier", 2, 65533)):
        v = raw.get(key)
        if isinstance(v, int) and not isinstance(v, bool) and low <= v <= high:
            setattr(s, key, v)
    if isinstance(raw.get("notices"), bool):
        s.notices = raw["notices"]
    return s


def member_groups(c) -> list[str]:
    """The groups report.toml's ``members`` names: the string form,
    "group:cparish$", or several, "group:myang$,knolin$" (Slurm's AllowGroups
    admits anyone in any of them; a list is NetIDs, as the condo reports read
    it)."""
    from nomad.condo.config import member_groups as groups
    return groups(c.members)


def member_account(c) -> str | None:
    """The account report.toml's ``members`` names ("account:parish"), else
    the condo's own ``account`` (as the condo reports read it)."""
    m = c.members
    if isinstance(m, str) and m.startswith("account:") and len(m) > 8:
        return m[8:]
    return getattr(c, "account", None) or None


def accounts_enforced(cfg: dict) -> bool:
    """AllowAccounts keeps others out only where Slurm enforces associations
    (AccountingStorageEnforce: associations, or limits, qos or safe, which
    imply it); else anyone can name the account."""
    v = str(cfg.get("AccountingStorageEnforce") or "").lower()
    return any(w in v for w in ("associations", "limits", "qos", "safe"))


def _set(value) -> set:
    return {x.strip() for x in str(value or "").split(",") if x.strip()}


@dataclass
class CondoCheck:
    name: str
    nodes: list
    owners: list
    groups: list                      # report.toml members' group
    partition: dict | None            # the owners' partition, as Slurm has it
    others: list = field(default_factory=list)       # [(partition row, its treatment, words)]
    problems: list = field(default_factory=list)     # what keeps the owners from first claim, in words
    changes: list = field(default_factory=list)      # (partition, "Key=value ...") the plan would set
    account: str | None = None


def _doors(rows: list[dict], report_cluster, owners: set) -> list[str]:
    """Today's guests' partitions: report.toml's guest_partitions, else
    those named like "condo" that are no condo's own (as Where it starts
    soonest reads them)."""
    names = {r["partition"] for r in rows}
    listed = [g for g in (getattr(report_cluster, "guest_partitions", None) or []) if g in names]
    return listed or sorted(n for n in names if "condo" in n.lower() and n not in owners)


def check(rows: list[dict], cfg: dict, report_cluster, s: Settings,
          owner_start_minutes: float | None = None) -> dict:
    """{on, config, condos: [CondoCheck], guest, others_off, global_changes,
    notes}: how each confirmed condo of report.toml stands in Slurm now --
    the owners first on their nodes, only their lab in their partition, every
    other partition on those nodes a guests' one -- and what the plan
    changes. The plan never ranks a partition first that it can't keep to the
    lab (a members group or account), and names PreemptMode=OFF on every
    partition but the guests' (Slurm's PreemptMode=REQUEUE, needed with
    partition_prio, would otherwise apply to every partition that sets none)."""
    from nomad.hostlist import expand_hostlist
    by_name = {r["partition"]: r for r in rows}
    on = is_on(cfg)
    global_off = str(cfg.get("PreemptMode") or "").upper() in ("", "OFF")
    out = {"on": on, "config": dict(cfg), "condos": [], "guest": None, "others_off": [], "global_changes": [],
           "notes": []}
    if not on:
        out["global_changes"].append("PreemptType=preempt/partition_prio")
    if global_off:
        out["global_changes"].append("PreemptMode=REQUEUE")
    if str(cfg.get("JobRequeue") or "").strip() == "0":
        out["notes"].append("JobRequeue=0: a preempted job is cancelled unless it asked for --requeue")
    if owner_start_minutes is not None and s.grace_minutes > owner_start_minutes:
        out["notes"].append(f"the guests' grace is {s.grace_minutes} min ([submit.preemption] grace_minutes) but "
                            f"report.toml's [report.policy] owner_start_minutes is {owner_start_minutes:g}: an "
                            "owner's job can wait the whole grace for a guest's to stop, and the condo reports "
                            "would count it late; make them agree (the committee's call)")
    condos = list(report_cluster.condos.values())
    for c in condos:
        if not c.confirmed:
            out["notes"].append(f"{c.name} isn't confirmed in report.toml: left out of the plan until it is")
    condos = [c for c in condos if c.confirmed]
    owner_parts = {c.part for c in condos}
    doors = _doors(rows, report_cluster, owner_parts)
    door_now = by_name.get(s.guest_partition)
    # every guests' door is one: the plan's and today's (draining once the plan's is there)
    door_names = sorted({s.guest_partition, *doors} - owner_parts)
    condo_nodes = set()
    for c in condos:
        p = by_name.get(c.part)
        nodes = list(c.nodes) or (expand_hostlist(p["nodes"]) if p and p.get("nodes") else [])
        condo_nodes |= set(nodes)
        ck = CondoCheck(c.name, nodes, list(c.owners), member_groups(c), p, account=member_account(c))
        out["condos"].append(ck)
        if p is None:
            ck.problems.append(f"Slurm has no partition {c.part} (report.toml's for {c.name})")
            continue
        tier = p.get("priority_tier") or 0
        want = {}
        allowed, accounts = _set(p.get("allow_groups")), _set(p.get("allow_accounts"))
        if ck.groups:
            if allowed != set(ck.groups):
                want["AllowGroups"] = ",".join(ck.groups)
                ck.problems.append(f"{c.part} admits {'anyone' if not allowed or 'ALL' in allowed else ','.join(sorted(allowed))}"
                                   f", not only {','.join(ck.groups)} (report.toml's members)")
        elif ck.account and not accounts_enforced(cfg):
            ck.problems.append(f"{c.name}'s members are an account ({ck.account}), but Slurm doesn't enforce "
                               "associations (AccountingStorageEnforce): anyone could name it, so AllowAccounts "
                               f"wouldn't keep {c.part} to the lab; give a group (members = \"group:NAME\")")
            ck.account = None
        elif ck.account:
            if accounts != {ck.account}:
                want["AllowAccounts"] = ck.account
                ck.problems.append(f"{c.part} admits {'any account' if not accounts or 'ALL' in accounts else ','.join(sorted(accounts))}"
                                   f", not only {ck.account} (report.toml's members)")
        else:
            ck.problems.append(f"report.toml's members for {c.name} name no group or account "
                               f"(members = \"group:NAME\"): Slurm can't keep {c.part} to the lab, so the plan "
                               "doesn't rank it first (anyone could send jobs there and preempt the guests)")
        restricted = bool(ck.groups or ck.account)
        if tier < s.owner_tier:
            ck.problems.append(f"{c.part} is at PriorityTier {tier}: nothing ranks the owners first")
            if restricted:
                want["PriorityTier"] = str(s.owner_tier)
        if str(p.get("preempt_mode") or "OFF").upper() != "OFF" or global_off:
            if str(p.get("preempt_mode") or "OFF").upper() != "OFF":
                ck.problems.append(f"{c.part}'s own jobs can be preempted (PreemptMode {p.get('preempt_mode')})")
            want["PreemptMode"] = "OFF"          # explicit: not Slurm's REQUEUE by default
        if want:
            ck.changes.append((c.part, " ".join(f"{k}={v}" for k, v in want.items())))
        owner_tier = s.owner_tier if (restricted and tier < s.owner_tier) else tier
        mine = set(expand_hostlist(p.get("nodes"))) | set(nodes)      # all the owners' partition holds
        for q in rows:
            if q is p or q.get("partition") == c.part or not q.get("nodes"):
                continue
            shared = mine & set(expand_hostlist(q["nodes"]))
            if not shared:
                continue
            qtier = q.get("priority_tier") or 0
            if q["partition"] in door_names:
                treated = preempts(q, rows, cfg)
                words = ("preempted there: " + treated["mode"].lower()
                         + (f", after up to {treated['grace_s'] // 60} min" if treated["grace_s"] else ", at once")
                         if treated and qtier < tier else "the guests' partition: preempted once the plan is on")
                ck.others.append((q, treated, words))
                continue
            if q["partition"] in owner_parts:
                ck.problems.append(f"{q['partition']}, another condo's partition, holds {len(shared)} of its nodes")
                ck.others.append((q, None, "another condo's partition"))
                continue
            words = f"a shared partition holding {len(shared)} of its nodes"
            ck.problems.append(f"{q['partition']} holds {len(shared)} of {c.part}'s nodes and isn't a guests' "
                               "partition: its jobs there either keep them from the owners (PreemptMode OFF) or "
                               "are preempted at once with no grace: take those nodes out of it, or make it a "
                               "guests' partition")
            ck.others.append((q, preempts(q, rows, cfg), words))
            if qtier >= owner_tier:
                ck.problems.append(f"{q['partition']} is at PriorityTier {qtier}, as high as the owners'")
    # the guests' partition the plan names
    if door_now is not None:
        treated = preempts(door_now, rows, cfg)
        want = {}
        if str(door_now.get("preempt_mode") or "").upper() != "REQUEUE":
            want["PreemptMode"] = "REQUEUE"
        if int(door_now.get("grace_time_s") or 0) != s.grace_minutes * 60:
            want["GraceTime"] = str(s.grace_minutes * 60)
        if (door_now.get("priority_tier") or 0) >= s.owner_tier:
            want["PriorityTier"] = "1"
        # today's other doors: drained, then removed once empty
        olds = [d for d in doors if d != s.guest_partition and d in by_name]
        draining = [d for d in olds if "DRAIN" in str(by_name[d].get("state") or "").upper()
                    and str(by_name[d].get("preempt_mode") or "").upper() == "REQUEUE"
                    and int(by_name[d].get("grace_time_s") or 0) == s.grace_minutes * 60]
        out["guest"] = {"partition": s.guest_partition, "exists": True, "treated": treated, "set": want,
                        "now": [d for d in olds if d not in draining], "draining": draining}
        for d in draining:
            out["notes"].append(f"{d} is draining: remove its line once squeue -p {d} is empty (and from "
                                "report.toml's guest_partitions)")
    else:
        nodes = sorted(set().union(*(set(expand_hostlist(by_name[g]["nodes"])) for g in doors
                                     if by_name[g].get("nodes")))) if doors else sorted(condo_nodes)
        if not nodes:
            out["notes"].append("no guests' partition and no condo nodes found: nothing to propose for guests")
            out["guest"] = {"partition": s.guest_partition, "exists": False, "now": doors, "set": {}}
        else:
            out["guest"] = {"partition": s.guest_partition, "exists": False, "now": doors,
                            "set": {"Nodes": _hostlist(nodes), "AllowGroups": "ALL", "PriorityTier": "1",
                                    "PreemptMode": "REQUEUE", "GraceTime": str(s.grace_minutes * 60)}}
    # every other partition: PreemptMode=OFF named, so the global REQUEUE doesn't make it preemptible
    if global_off or not on:
        done = owner_parts | set(door_names) | {s.guest_partition}
        out["others_off"] = [r["partition"] for r in rows if r["partition"] not in done]
    out["rows_by_name"] = by_name
    out["guest_partitions"] = list(getattr(report_cluster, "guest_partitions", None) or [])
    return out


def _hostlist(nodes: list) -> str:
    """spdr50 ... spdr61 -> spdr[50-61] (Slurm's form), runs of one width."""
    import re
    groups: dict = {}
    loose = []
    for n in nodes:
        m = re.fullmatch(r"(.*?)(\d+)", n)
        if m:
            groups.setdefault((m.group(1), len(m.group(2))), []).append(int(m.group(2)))
        else:
            loose.append(n)
    out = []
    for (prefix, width), nums in sorted(groups.items()):
        nums = sorted(set(nums))
        runs, start = [], nums[0]
        for a, b in zip(nums, nums[1:] + [None]):
            if b != a + 1:
                runs.append(f"{start:0{width}d}" + (f"-{a:0{width}d}" if a != start else ""))
                start = b
        out.append(prefix + (runs[0] if len(runs) == 1 and "-" not in runs[0] else f"[{','.join(runs)}]"))
    return ",".join(out + loose)


def plan_text(result: dict, s: Settings, site: str) -> list[str]:
    """The slurm.conf changes that would turn it on, and those that turn it
    back off: what to set on each line, never a whole line rewritten (a
    site's slurm.conf has more than scontrol shows). "As now" is what
    scontrol shows, which may be Slurm's default rather than the line's own:
    where the line had no such setting, remove it instead."""
    on, off = [], []
    cfg = result["config"]
    on += result["global_changes"]
    if result["global_changes"]:
        if is_on(cfg):
            off.append(f"PreemptMode={cfg.get('PreemptMode') or 'OFF'}   (as now)")
        else:
            off.append("remove the PreemptType and PreemptMode lines (as now: no preemption)")
    for ck in result["condos"]:
        for part, change in ck.changes:
            p = ck.partition or {}
            on.append(f"PartitionName={part} ...  {change}")
            was = []
            for kv in change.split():
                k = kv.split("=", 1)[0]
                cur = (p.get("settings") or {}).get(k)
                was.append(f"{k}={cur if cur not in (None, '') else '(unset)'}")
            off.append(f"PartitionName={part} ...  {' '.join(was)}   (as now)")
    for part in result.get("others_off") or []:
        on.append(f"PartitionName={part} ...  PreemptMode=OFF")
        off.append(f"PartitionName={part} ...  remove PreemptMode=OFF (if the line had none before)")
    g = result.get("guest") or {}
    if g:
        sets = " ".join(f"{k}={v}" for k, v in (g.get("set") or {}).items())
        if g.get("exists") and sets:
            on.append(f"PartitionName={g['partition']} ...  {sets}")
            off.append(f"PartitionName={g['partition']} ...  as now")
        elif not g.get("exists") and sets:
            on.append(f"add PartitionName={g['partition']} {sets} ..."
                      + (f"   (the rest as on the {', '.join(g['now'])} line)" if g.get("now") else ""))
            off.append(f"PartitionName={g['partition']} ...  State=DRAIN; remove the line once squeue -p "
                       f"{g['partition']} is empty")
        for old in g.get("now") or []:
            was = ((result.get("rows_by_name") or {}).get(old) or {}).get("settings") or {}
            on.append(f"PartitionName={old} ...  State=DRAIN PreemptMode=REQUEUE "
                      f"GraceTime={s.grace_minutes * 60}   (takes no new jobs; remove the line once "
                      f"squeue -p {old} is empty: jobs in a partition that is gone fail)")
            now_was = " ".join(f"{k}={was.get(k) if was.get(k) not in (None, '') else '(unset)'}"
                               for k in ("State", "PreemptMode", "GraceTime"))
            off.append(f"PartitionName={old} ...  {now_was}   (as now; add the line back if it was removed)")
        listed = result.get("guest_partitions") or []
        olds = [o for o in (g.get("now") or []) + (g.get("draining") or []) if o in listed]
        if olds and g["partition"] not in listed:
            on.append(f"report.toml, [report.clusters.{site}]: guest_partitions = "
                      f"{[*listed, g['partition']]!r}".replace("'", '"')
                      + f"   (while {', '.join(olds)} drains; then without {', '.join(olds)}: the condo reports "
                        "find the guests' partitions through it)")
            off.append(f"report.toml, [report.clusters.{site}]: guest_partitions = {listed!r}".replace("'", '"')
                       + "   (as now)")
        gone = [o for o in (g.get("draining") or []) if o in listed]
        if gone and g["partition"] in listed:
            on.append(f"report.toml, [report.clusters.{site}]: once {', '.join(gone)} is gone, guest_partitions = "
                      f"{[x for x in listed if x not in gone]!r}".replace("'", '"'))
    out = [f"To turn it on at {site} (slurm.conf, then scontrol reconfigure; '...' is the rest of that line as it is):"]
    out += [f"  {x}" for x in on] or ["  nothing: it is on already"]
    out.append("To turn it back off (as scontrol shows them now; a setting the line didn't have: remove it):")
    out += [f"  {x}" for x in off] or ["  nothing to undo"]
    return out


__all__ = ["TABLE", "KEYS", "parse_config", "store_config", "config_of", "is_on", "preempts", "Settings",
           "settings", "member_groups", "member_account", "accounts_enforced", "CondoCheck", "check",
           "plan_text"]

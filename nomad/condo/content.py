# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""What each report says, once, as blocks every format draws its own way
(an email's HTML, a PDF, plain text)."""
from __future__ import annotations

from dataclasses import dataclass, field

from nomad.condo.figures import month_name

OWNER = "#2563eb"
GUEST = "#d97706"
IDLE = "#cbd5e1"
OUT = "#475569"


@dataclass
class Title:
    institution: str
    title: str
    subtitle: str
    badge: str
    lines: list = field(default_factory=list)   # the institution and its units ([console.branding])
    logo: bool = False                          # the institution's logo goes at the top

    @property
    def masthead(self) -> list:
        return self.lines or ([self.institution] if self.institution else [])


@dataclass
class H:
    text: str


@dataclass
class P:
    text: str
    muted: bool = False


@dataclass
class Bar:
    segments: list          # (label, pct, color)
    caption: str = ""


@dataclass
class Tiles:
    tiles: list             # (label, value, sub, color)


@dataclass
class Table:
    headers: list
    rows: list
    align: str = ""         # one letter a column: l or r
    note: str = ""


@dataclass
class Bars:
    """One labelled bar a row: the months of a trend, the condos of a cluster."""
    rows: list              # (label, segments, text)
    legend: list = field(default_factory=list)   # (label, color)


@dataclass
class Foot:
    lines: list


# --- numbers in words ------------------------------------------------------------

def n(x, d=0) -> str:
    if x is None:
        return "-"
    return f"{x:,.{d}f}"


def pct(x) -> str:
    return "-" if x is None else (f"{x:.0f}%" if x >= 10 or x == 0 else f"{x:.1f}%")


def minutes(x) -> str:
    if x is None:
        return "-"
    if x < 1:
        return "under a minute"
    if x < 90:
        return f"{x:.0f} min"
    if x < 48 * 60:
        return f"{x / 60:.1f} h"
    return f"{x / 1440:.1f} days"


def plural(k, word, words=None) -> str:
    return f"{n(k)} {word if k == 1 else (words or word + 's')}"


def short_month(key: str) -> str:
    name = month_name(key)
    return name[:3] + " " + name.split()[-1]


def _segments(u: dict) -> list:
    return [("lab", u.get("owner_pct") or 0, OWNER), ("guests", u.get("guest_pct") or 0, GUEST),
            ("idle", u.get("idle_pct") or 0, IDLE)]


LEGEND = [("owner's lab", OWNER), ("guests", GUEST), ("idle", IDLE)]
_WHY = {"guests": "guest jobs were on the nodes", "own": "the lab's own jobs filled the nodes",
        "other": "neither guests nor the lab's jobs held the nodes"}


def owner_doc(fig: dict, delivery, brand=None) -> list:
    c, u, o, g, nd = fig["condo"], fig["use"], fig["owner_jobs"], fig["guests"], fig["nodes"]
    cl = fig["cluster"]["label"]
    month = month_name(fig["month"])
    mon = month.split()[0]
    doc = [Title(delivery.institution, f"{c['label']} condo on {cl}", f"Monthly report: {month}",
                 f"Cluster: {cl}", **_brand(brand))]
    hw = f"{plural(len(c['nodes']), 'node')} ({c['node_list']}): {n(c['cores'])} cores, " \
         f"{n(c['memory_gb'])} GB of memory" + (f", {plural(c['gpus'], 'GPU')}" if c["gpus"] else "")
    owners = c.get("owners") or ([c["owner"]] if c.get("owner") else [])
    who = (f". Owner: {owners[0]}." if len(owners) == 1 else
           f". Owners: {', '.join(owners[:-1])} and {owners[-1]}." if owners else ".")
    doc.append(P(hw + who
                 + f" Partition {c['partition']} on {cl}."))

    doc.append(H(f"Your nodes in {mon}"))
    if u["capacity_core_hours"]:
        doc.append(Bar(_segments(u), f"Share of the {n(u['capacity_core_hours'])} core-hours the nodes "
                                      "were in service"))
        doc.append(Tiles([
            ("Your lab", pct(u["owner_pct"]), f"{n(u['owner_core_hours'])} core-hours", OWNER),
            ("Guests", pct(u["guest_pct"]), f"{n(u['guest_core_hours'])} core-hours, "
                                            f"{plural(g['people'], 'person', 'people')}", GUEST),
            ("Idle", pct(u["idle_pct"]), f"{n(u['idle_core_hours'])} core-hours", IDLE)]))
    else:
        doc.append(P("No node readings this month, so the shares can't be worked out.", muted=True))
    if u.get("gpu"):
        gp = u["gpu"]
        doc.append(P(f"GPUs: your lab used {pct(gp['owner_pct'])} of the {n(gp['capacity_gpu_hours'])} "
                     f"GPU-hours in service ({n(gp['owner_gpu_hours'])}), guests {pct(gp['guest_pct'])} "
                     f"({n(gp['guest_gpu_hours'])})."))
    doc.append(Table(["", "In service", "Down", "Drained", "Maintenance", "Not read"],
                     [["Node-hours", n(nd["in_service"]), n(nd["down"]), n(nd["drained"]),
                       n(nd["maintenance"]), n(nd["not_read"])]], "lrrrrr",
                     note="Not read: the collector didn't report; those hours count as in service."))
    guest = f"{plural(g['jobs'], 'guest job')} ran on your nodes"
    if g["preempted"]:
        guest += f"; {plural(g['preempted'], 'was', 'were')} preempted to make room for your lab"
    else:
        guest += "; none was preempted"
    guest += "."
    if g["over_time_limit"]:
        guest += (f" {plural(g['over_time_limit'], 'guest job')} ran longer than the "
                  f"{g['time_limit_days']:g}-day guest limit.")
    doc.append(P(guest))

    doc.append(H("Your lab's jobs on your nodes"))
    gm = o["guarantee_minutes"]
    if o["tracked"]:
        t = (f"{n(o['within'])} of {plural(o['tracked'], 'job')} submitted to {c['partition']} "
             f"({pct(o['within_pct'])}) started within {gm:g} minutes of when they could start.")
        late = o["late"]
        k = late["guests"] + late["own"] + late["other"]
        if k:
            parts = [f"{n(late['guests'])} while guest jobs were on the nodes" if late["guests"] else "",
                     f"{n(late['own'])} while your lab's own jobs filled them" if late["own"] else "",
                     f"{n(late['other'])} for other reasons (dependencies, a request larger than the "
                     "nodes, nodes out of service)" if late["other"] else ""]
            t += f" Of the {n(k)} that waited longer: " + "; ".join(p for p in parts if p) + "."
            t += (f" The longest wait was {minutes(o['longest_wait_minutes'])}"
                  + (" (still waiting)" if o.get("longest_still_waiting") else "")
                  + f", while {_WHY.get(o['longest_reason'], '')}.")
        doc.append(P(t))
    elif not o["still_waiting"]:
        doc.append(P(f"No jobs submitted to {c['partition']} started in {mon}."))
    if o["still_waiting"]:
        k = o["still_waiting"]
        t = (f"{plural(k, 'job')} submitted in {mon} {'was' if k == 1 else 'were'} still waiting when this "
             "report was made")
        lw = o.get("still_waiting_late", 0)
        if lw:
            t += (f"; {'it has' if k == 1 else (n(lw) + ' of them have' if lw != k else 'they have')} waited "
                  f"longer than {gm:g} minutes and {'is' if lw == 1 else 'are'} counted above")
        doc.append(P(t + "."))
    st = o.get("summary") or {}
    if st.get("finished"):
        oc = st["outcomes"]
        # Not only the jobs above: a member's job can reach these nodes
        # through another partition that includes them.
        doc.append(P(f"Your lab's jobs that ended on these nodes in {mon}, sent to {c['partition']} or "
                     "to any other partition that includes them:"))
        doc.append(Table(["Ended in " + mon, "Completed", "Failed", "Out of memory", "Timed out",
                          "Cancelled", "Node failure"],
                         [[n(st["finished"]), n(oc["completed"]), n(oc["failed"]), n(oc["out_of_memory"]),
                           n(oc["timeout"]), n(oc["cancelled"]), n(oc["node_fail"])]], "rrrrrrr"))
        cpu = (st.get("core_hours") or {}).get("used_pct")
        mem = (st.get("memory") or {}).get("used_pct")
        if cpu is not None or mem is not None:
            bits = []
            if cpu is not None:
                bits.append(f"kept {pct(cpu)} of the cores they held busy")
            if mem is not None:
                bits.append(f"used {pct(mem)} of the memory they asked for, at their peak")
            line = "These jobs " + " and ".join(bits) + "."
            low = fig["policy"]["use_below"] * 100
            if (cpu is not None and cpu < low) or (mem is not None and mem < low):
                line += (" Asking for nearer what jobs use leaves more of your nodes free for your "
                         "lab's next jobs.")
            doc.append(P(line))

    if fig["elsewhere"]:
        doc.append(H("Your lab elsewhere"))
        rows = []
        for e in fig["elsewhere"]:
            where = (f"{fig['cluster']['label']}, {e['where']}" if e["cluster"] == fig["cluster"]["name"]
                     else e.get("label") or e["cluster"])
            rows.append([where, n(e["core_hours"]), n(e["gpu_hours"]) if e["gpu_hours"] else "-",
                         n(e["people"]), n(e["jobs_ended"]), pct(e["failed_pct"]), pct(e["cpu_used_pct"]),
                         pct(e["memory_used_pct"]), minutes(e["wait_median_minutes"])])
        doc.append(Table(["Where", "Core-hours", "GPU-hours", "People", "Jobs ended", "Failed",
                          "CPU used", "Memory used", "Median wait"], rows, "lrrrrrrrr",
                         note="Other condos: your lab as guests on other owners' nodes."))

    m = fig["members"]
    doc.append(H("Members"))
    if m["named"] and m["each"]:
        used = [r for r in m["each"] if r["condo_core_hours"] or r["shared_core_hours"]
                or r.get("other_condo_core_hours") or r["elsewhere_core_hours"] or r["jobs_ended"]]
        idle_members = len(m["each"]) - len(used)
        rows = [[r["user"] + ("" if r["member"] else " *"), n(r["condo_core_hours"]),
                 n(r["shared_core_hours"]), n(r.get("other_condo_core_hours", 0)), n(r["elsewhere_core_hours"]),
                 n(r["gpu_hours"]) if r["gpu_hours"] else "-", n(r["jobs_ended"]), n(r["failed"])]
                for r in used]
        note = f"Members from {m['how']}. Core-hours this month."
        if idle_members:
            note += f" {plural(idle_members, 'other member')} ran nothing this month."
        if any(not r["member"] for r in m["each"]):
            note += " * ran in your partition without being a member (their jobs there only)."
        if rows:
            doc.append(Table(["NetID", "Your nodes", "Shared nodes", "Other condos", "Other clusters",
                              "GPU-hours", "Jobs ended", "Failed"], rows, "lrrrrrrr", note=note))
        else:
            doc.append(P(f"No member of the lab ran anything this month ({plural(len(m['each']), 'member')}, "
                         f"from {m['how']})."))
    else:
        doc.append(P(f"{plural(m['count'], 'member')} (from {m['how']})."))

    s = fig["storage"]
    q, h = s["lab_scratch_quota_tb"], s["home_quota_gb"]
    doc.append(H("Storage"))
    doc.append(P(f"{s['scratch_path']}: quota {n(q['soft'])} TB, up to {n(q['hard'])} TB for a short "
                 f"time. Home directories: {n(h['soft'])} GB each, up to {n(h['hard'])} GB. "
                 "Not measured yet: once NØMAÐ reads the quotas, this shows what is used."))

    doc.append(H("The past 12 months"))
    rows = []
    for t in fig["trend"]:
        if not t.get("measured"):
            rows.append((short_month(t["month"]), [], "no readings"))
        else:
            rows.append((short_month(t["month"]), _segments(t),
                         f"lab {pct(t['owner_pct'])}, guests {pct(t['guest_pct'])}, idle {pct(t['idle_pct'])}"))
    doc.append(Bars(rows, LEGEND))
    doc.append(Foot(fig["coverage"] + ([delivery.contact] if delivery.contact else [])
                    + [f"Figures from NØMAÐ (nomad-hpc.com), {fig['generated'][:10]}."]))
    return doc


def _brand(brand) -> dict:
    if brand is None:
        return {}
    return {"lines": list(brand.lines), "logo": brand.logo is not None}


def summary_doc(data: dict, delivery, policy, brand=None) -> list:
    month = data["month_name"]
    doc = [Title(delivery.institution, "Condo summary", f"Monthly summary: {month}",
                 f"{plural(len(data['clusters']), 'cluster')}", **_brand(brand))]
    gm = policy.owner_start_minutes
    for cl in data["clusters"]:
        doc.append(H(cl["label"]))
        if not cl["condos"]:
            doc.append(P("No condos." + (" Guest partition: " + ", ".join(cl["guest_partitions"])
                                         if cl["guest_partitions"] else ""), muted=True))
        else:
            rows = []
            for r in cl["condos"]:
                rows.append([r["label"], (", ".join(r.get("owners") or []) or r["owner"] or "?")
                             + ("" if r["confirmed"] else " (unconfirmed)"),
                             n(r["nodes"]), pct(r["owner_pct"]), pct(r["guest_pct"]), pct(r["idle_pct"]),
                             n(r["guest_core_hours"]), n(r["preempted"]),
                             f"{pct(r['within_pct'])} of {n(r['owner_jobs'])}" if r["owner_jobs"] else "-",
                             n(r["out_node_hours"])])
            doc.append(Table(["Condo", "Owner", "Nodes", "Owner's lab", "Guests", "Idle", "Guest core-hours",
                              "Preempted", f"Started in {gm:g} min", "Node-hours out"], rows, "llrrrrrrrr",
                             note="Shares of the in-service core-hours. Node-hours out: down, drained or in "
                                  "maintenance."))
            doc.append(Bars([(r["label"], _segments({"owner_pct": r["owner_pct"], "guest_pct": r["guest_pct"],
                                                     "idle_pct": r["idle_pct"]}),
                              f"owner {pct(r['owner_pct'])}, guests {pct(r['guest_pct'])}") for r in cl["condos"]],
                            LEGEND))
        for f in cl["flags"]:
            if f["kind"] == "new":
                doc.append(P("Found on the nodes but not in report.toml: " + ", ".join(f["partitions"])
                             + ". `nomad usage-report condos init` drafts them."))
            elif f["kind"] == "not_configured":
                doc.append(P("Not in report.toml yet (shown from the nodes, owners unknown): "
                             + ", ".join(f["partitions"]) + ". `nomad usage-report condos init` drafts them."))
            elif f["kind"] == "partition_gone":
                doc.append(P("In report.toml but no longer on the nodes: " + ", ".join(f["partitions"]) + "."))
            elif f["kind"] == "no_readings":
                doc.append(P("In report.toml, but the cluster has no node readings for the month: "
                             + ", ".join(f["condos"]) + "."))
            elif f["kind"] == "unconfirmed":
                doc.append(P("Owner not confirmed (no report goes to an owner): " + ", ".join(f["condos"]) + "."))
    doc.append(Foot(([delivery.contact] if delivery.contact else [])
                    + [f"Figures from NØMAÐ (nomad-hpc.com), {data['generated'][:10]}."]))
    return doc

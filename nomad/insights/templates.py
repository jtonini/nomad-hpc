# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 João Tonini
"""
Narrative templates for the NØMAÐ Insight Engine.

Each template is a callable that receives a Signal and returns a
human-readable narrative string. Templates are selected based on
signal type and severity, with conditional logic for context-dependent
phrasing.
"""
from __future__ import annotations

from .signals import Signal, SignalType, Severity


def _fmt_hours(h: float) -> str:
    """Format hours into human-readable duration."""
    if h < 1:
        return f"{h * 60:.0f} minutes"
    if h <= 24:
        return f"{h:.0f} hours"
    days = h / 24
    if float(days).is_integer():
        return f"{days:.0f} days"
    if days < 7:
        return f"{days:.1f} days"
    return f"{days / 7:.1f} weeks"


def _severity_word(sev: Severity) -> str:
    """Opening tone word for severity."""
    return {
        Severity.INFO: "",
        Severity.NOTICE: "Note:",
        Severity.WARNING: "Warning:",
        Severity.CRITICAL: "CRITICAL:",
    }[sev]


# ── Template functions ───────────────────────────────────────────────────

def narrate_job_success_rate(sig: Signal) -> str:
    m = sig.metrics
    hours = m.get("hours", 24)
    judged = m.get("judged", m.get("total", 0))
    rate = m.get("problem_rate")
    if rate is None and m.get("success_rate") is not None:
        rate = 100 - m["success_rate"]
    cancelled = m.get("cancelled", 0)

    head = f"{judged:,} jobs ended in the last {_fmt_hours(hours)}"
    if cancelled:
        head += f" (and {cancelled:,} were cancelled, which is not counted as a failure)"
    if rate is None:
        return head + "."
    if not m.get("problems"):
        text = f"{head}; none failed or hit a limit."
    else:
        text = f"{head}; {rate:.1f}% failed or hit a limit."
        kinds = []
        for key, word in (("failed", "failed"), ("timed_out", "ran out of time"),
                          ("oom", "ran out of memory"), ("node_fail", "lost to a node failure"),
                          ("other_limit", "hit another limit")):
            if m.get(key):
                kinds.append(f"{m[key]:,} {word}")
        if kinds:
            text += " " + ", ".join(kinds) + "."
    if judged < m.get("min_jobs", 50):
        text += " Too few jobs to judge a rate."
    return text


def narrate_partition_failures(sig: Signal) -> str:
    m = sig.metrics
    partition = m["partition"]
    failures = m["failures"]
    pct = m["pct"]
    jobs = m.get("jobs")
    elsewhere = m.get("elsewhere_pct")

    text = f"In the '{partition}' partition {failures:,}"
    text += f" of {jobs:,} jobs" if jobs else " jobs"
    text += f" failed or hit a limit ({pct:.1f}%)"
    if elsewhere is not None:
        text += f", against {elsewhere:.1f}% in the other partitions"
    return text + "."


def narrate_oom(sig: Signal) -> str:
    m = sig.metrics
    count = m["oom_count"]
    users = m.get("top_users", [])

    text = f"{count} jobs were stopped for using more memory than they asked for."
    if users:
        text += f" Most of them belong to {', '.join(users)}."
    text += " Compare what these jobs requested with their peak memory (nomad edu explain <job_id>)."
    return text


def narrate_timeout(sig: Signal) -> str:
    m = sig.metrics
    count = m["timeout_count"]
    share = m.get("share_pct")
    text = f"{count} jobs ran out of time"
    if share is not None:
        text += f" ({share:.1f}% of the jobs that ended)"
    return text + (". 'nomad edu explain <job_id>' shows how long a job ran against "
                   "the time it asked for.")


def narrate_job_rate_trend(sig: Signal) -> str:
    m = sig.metrics
    prev = m.get("previous_problem_rate")
    curr = m.get("current_problem_rate")
    if prev is None or curr is None:
        prev, curr = 100 - m["previous_rate"], 100 - m["current_rate"]
    word = "fewer" if curr < prev else "more"
    period = _fmt_hours(m.get("hours", 24))
    return (
        f"{word.capitalize()} jobs are failing or hitting a limit: {curr:.1f}% in the last "
        f"{period}, against {prev:.1f}% in the {period} before."
    )


def _fmt_size_gb(gb: float) -> str:
    return _fmt_bytes(gb * 1073741824)


def _fmt_bytes(b: float) -> str:
    """Decimal units, as the Console's Dashboard shows them (1 TB = 10^12 bytes)."""
    if abs(b) >= 1e12:
        return f"{b / 1e12:.1f} TB"
    if abs(b) >= 1e9:
        return f"{b / 1e9:.0f} GB"
    return f"{b / 1e6:.0f} MB"


def narrate_filesystem_usage(sig: Signal) -> str:
    m = sig.metrics
    server = m.get("server") or m.get("hostname", "unknown")
    usage = m.get("usage_pct") or m.get("usage_percent", 0)
    avail = m.get("avail_gb") or m.get("free_gb", 0)
    paths = m.get("paths") or []

    free_b = m.get("free_bytes")
    free_text = _fmt_bytes(free_b) if free_b is not None else _fmt_size_gb(avail)
    text = f"{server} is {usage:.0f}% full ({free_text} free)"
    if len(paths) > 1:
        text += f"; {' and '.join(paths)} are one filesystem"
    text += "."
    growth = m.get("growth_gb_per_day")
    growth_b = m.get("growth_bytes_per_day")
    days_full = m.get("days_until_full")
    if growth is not None and m.get("trend_days"):
        if growth > 0.5:
            month = (_fmt_bytes(growth_b * 30) if growth_b is not None
                     else _fmt_size_gb(growth * 30))
            text += f" Growing about {month} a month"
            if days_full is not None:
                if days_full < 60:
                    text += f"; full in about {days_full:.0f} days at that rate"
                else:
                    text += f"; full in about {days_full / 30:.0f} months at that rate"
            text += "."
        elif growth < -0.5:
            text += " Use has been falling."
        else:
            text += " Use is steady."
    if m.get("stale"):
        text += f" (Last reading {str(m.get('as_of', ''))[:16].replace('T', ' ')}.)"
    return text


def narrate_disk_fill_projection(sig: Signal) -> str:
    m = sig.metrics
    server = m.get("server") or m.get("hostname", "unknown")
    rate = m["fill_rate_gb_hr"]
    hours = m["hours_to_full"]

    if hours < 12:
        urgency = "Urgent"
    elif hours < 24:
        urgency = "Important"
    else:
        urgency = "Note"

    return (
        f"{urgency}: {server} is filling at {rate:.1f} GB/hr and will reach capacity "
        f"in approximately {_fmt_hours(hours)}. "
        f"Recommendation: identify large writers and consider purge or quota adjustments."
    )


def narrate_gpu_util_gap(sig: Signal) -> str:
    m = sig.metrics
    node = m["node"]
    smi = m["avg_smi_util"]
    real = m["avg_real_util"]
    gap = m["avg_gap"]
    pattern_hint = ""
    if gap > 35:
        pattern_hint = (
            " The pipeline stages show significant idle time despite kernel "
            "activity — consider larger batch sizes, kernel fusion, or "
            "data prefetching."
        )
    return (
        f"{node} shows a {gap:.0f}-point gap between nvidia-smi utilization "
        f"({smi:.0f}%) and Real Utilization ({real:.0f}%). "
        f"The GPU appears busy but the compute pipeline is underused."
        f"{pattern_hint}"
    )


def narrate_gpu_workload_pattern(sig: Signal) -> str:
    m = sig.metrics
    node = m["node"]
    wclass = m["workload_class"]
    pct = m["dominant_pct"]
    ptype = m.get("pattern_type", "")

    if ptype == "memory-bound":
        return (
            f"{node} has been running memory-bound workloads {pct:.0f}% of the time. "
            f"GPU compute pipeline is underutilized relative to memory bandwidth. "
            f"Possible improvements: increase batch size, optimize data layout, "
            f"or use prefetching to overlap compute and data transfer."
        )
    if ptype == "idle":
        return (
            f"{node} GPU has been idle {pct:.0f}% of the sampled window. "
            f"Consider whether allocated jobs are actually using the GPU, "
            f"or whether this node could serve additional workloads."
        )
    # productive
    return (
        f"{node} is running {wclass} workloads {pct:.0f}% of the time — "
        f"GPU resources are being used effectively."
    )


def narrate_gpu_hardware_health(sig: Signal) -> str:
    m = sig.metrics
    node = m["node"]
    gpu_id = m["gpu_id"]
    status = m["health_status"]

    if status == "CRIT":
        remap = m.get("row_remap_failure", 0)
        ecc = m.get("ecc_uncorrectable", 0)
        if remap:
            return (
                f"{node} GPU {gpu_id} has a row remap failure — HBM memory is "
                f"permanently degraded. This GPU should be removed from production "
                f"and scheduled for replacement."
            )
        if ecc:
            return (
                f"{node} GPU {gpu_id} has {ecc} uncorrectable ECC error(s). "
                f"Memory integrity cannot be guaranteed. Remove from production "
                f"and investigate hardware."
            )
        return f"{node} GPU {gpu_id} is in a critical hardware state. Investigate immediately."

    if status == "HOT":
        return (
            f"{node} GPU {gpu_id} temperature is at or above the warning threshold. "
            f"Check cooling, airflow, and workload intensity. Sustained high "
            f"temperatures accelerate hardware degradation."
        )

    # WARN — PCIe
    rate = m.get("pcie_replay_rate", 0)
    return (
        f"{node} GPU {gpu_id} is logging PCIe replay errors ({rate:.3f}/s). "
        f"This indicates link instability — check the PCIe slot, riser card, "
        f"or cable. Left unaddressed, this typically escalates to link failure."
    )


def narrate_gpu_failure_rate(sig: Signal) -> str:
    m = sig.metrics
    rate = m["fail_rate"]
    failed = m["failed"]
    total = m["total_gpu_jobs"]
    text = f"{rate:.0f}% of GPU jobs failed or hit a limit ({failed:,} of {total:,})."
    people = m.get("people")
    top = m.get("top_person_share")
    if people == 1:
        text += " All of them are one person's."
    elif people and top is not None and top >= 80:
        text += f" {top:.0f}% of them are one person's (of {people} people)."
    elif people:
        text += f" They come from {people} people."
    return text


def narrate_gpu_oom(sig: Signal) -> str:
    m = sig.metrics
    count = m["gpu_oom_count"]
    total = m["total_gpu_jobs"]
    return (
        f"{count} GPU jobs (of {total:,}) were stopped for using more memory than they "
        f"asked for. This is the job's memory on the node (--mem), not GPU memory."
    )


def narrate_queue_pressure(sig: Signal) -> str:
    m = sig.metrics
    partition = m["partition"]
    pending = m["pending"]
    running = m["running"]
    ratio = m["ratio"]

    if running == 0:
        return f"'{partition}': {pending} jobs waiting and none running."
    return (
        f"'{partition}': {pending} jobs waiting and {running} running "
        f"({ratio:.1f}× as many waiting as running)."
    )


def narrate_high_wait_time(sig: Signal) -> str:
    m = sig.metrics
    partition = m["partition"]
    med = m.get("median_wait_sec", m["avg_wait_sec"]) / 3600
    mx = m["max_wait_sec"] / 3600
    jobs = m.get("jobs")
    text = f"Jobs in '{partition}' waited a median {med:.1f} hours to start (longest {mx:.1f} hours"
    text += f", {jobs:,} jobs)." if jobs else ")."
    return text


def narrate_network_latency(sig: Signal) -> str:
    m = sig.metrics
    path = m["path"]
    avg = m["avg_latency"]
    peak = m["max_latency"]
    return (
        f"Network path '{path}' showing elevated latency: {avg:.1f}ms average, "
        f"{peak:.1f}ms peak. This can impact NFS-dependent jobs and parallel workloads."
    )


def narrate_packet_loss(sig: Signal) -> str:
    m = sig.metrics
    path = m["path"]
    avg = m["avg_loss"]
    return (
        f"Packet loss detected on '{path}': {avg:.2f}% average. "
        f"Even small packet loss degrades MPI and distributed training performance significantly."
    )


def narrate_active_alerts(sig: Signal) -> str:
    m = sig.metrics
    messages = m.get("messages", [])
    if messages:
        text = "; ".join(messages[:5])
        if len(messages) > 5:
            text += f" (+{len(messages)-5} more)"
        return text
    return f"{m.get('total_active', 0)} alerts."


def narrate_alerts_raised(sig: Signal) -> str:
    m = sig.metrics
    total = m["total"]
    conditions = m["conditions"]
    period = _fmt_hours(m.get("hours", 24))
    text = (f"{total:,} alert{'s' if total != 1 else ''} raised in the last {period}, "
            f"about {conditions} condition{'s' if conditions != 1 else ''}")
    top = m.get("top") or []
    if top:
        parts = []
        for c in top[:3]:
            when = str(c.get("last") or "")[:16].replace("T", " ")
            parts.append(f"{c['message']} ({c['count']}×, last {when})")
        text += ": " + "; ".join(parts)
    text += "."
    if m.get("unplaced"):
        text += (f" {m['unplaced']} older alerts don't record their site and "
                 f"are not counted here.")
    return text


def narrate_flapping_alert(sig: Signal) -> str:
    m = sig.metrics
    metric = m["metric"]
    count = m["trigger_count"]
    return (
        f"The '{metric}' alert has triggered {count} times recently — "
        f"this suggests an oscillating condition rather than a one-time event. "
        f"Review the threshold or investigate the underlying cause."
    )


def narrate_cloud_cost(sig: Signal) -> str:
    m = sig.metrics
    cost = m["total_cost_usd"]
    hours = m["hours"]
    daily = cost * (24 / hours) if hours > 0 else cost
    return f"Cloud compute spending: ${cost:.2f} over the last {_fmt_hours(hours)} (projected ${daily:.2f}/day)."


def narrate_underutilized_instance(sig: Signal) -> str:
    m = sig.metrics
    instance = m["instance"]
    cpu = m["avg_cpu"]
    return (
        f"Cloud instance '{instance}' is averaging only {cpu:.1f}% CPU utilization. "
        f"Consider downsizing to a smaller instance type or consolidating workloads to reduce cost."
    )


def narrate_workstation_cpu(sig: Signal) -> str:
    m = sig.metrics
    host = m["hostname"]
    ratio = m['load'] / max(m['cpus'], 1)
    text = (f"'{host}' has a load of {m['load']:.1f} on {m['cpus']} cores "
            f"({ratio:.1f}× its cores).")
    if ratio > 1:
        text += " More work is waiting to run than it has cores, so it will feel slow."
    return text


def narrate_workstation_memory(sig: Signal) -> str:
    m = sig.metrics
    host = m["hostname"]
    pct = m.get("mem_pct", m.get("avg_mem", 0))
    return (
        f"'{host}' is using {pct:.0f}% of its memory. Processes on it risk being "
        f"killed for memory; check for runaway processes."
    )



# ── Template dispatch ────────────────────────────────────────────────────

# ── Dynamics templates ───────────────────────────────────────────────

def narrate_diversity_fragility(sig: Signal) -> str:
    m = sig.metrics
    share = m["dominant_proportion"]
    if m.get("dimension", "group") == "user":
        jobs = m.get("jobs")
        text = (f"One person ran {share:.0%} of the "
                + (f"{jobs:,} " if jobs else "")
                + "jobs submitted in this window. Figures counted over jobs here "
                  "mostly describe that person's work.")
        return text
    return (
        f"'{m['dominant']}' accounts for {share:.0%} of all jobs "
        f"(H'={m['shannon_h']:.3f}): the workload rests on one group."
    )


def narrate_diversity_declining(sig: Signal) -> str:
    m = sig.metrics
    return (
        f"Workload diversity is declining (H'={m['shannon_h']:.3f}, "
        f"slope: {m['slope']:.4f}/window). Investigate whether user "
        f"communities are being lost or workload is consolidating."
    )


def narrate_capacity_binding(sig: Signal) -> str:
    m = sig.metrics
    sat = ""
    if m.get("hours_to_saturation"):
        sat = f" At the current rise it would be full in about {m['hours_to_saturation']:.0f} hours."
    return (
        f"{m['label']} is at {m['utilization']:.0%}, the resource closest to its "
        f"limit.{sat}"
    )


def narrate_capacity_saturation(sig: Signal) -> str:
    m = sig.metrics
    return (
        f"Saturation imminent: {m['dimension']} projected to reach "
        f"full capacity in {m['hours_to_saturation']:.0f} hours "
        f"at current growth rate. Immediate action recommended."
    )


def narrate_niche_contention(sig: Signal) -> str:
    m = sig.metrics
    return (
        f"{m['high_overlap_count']} group pair(s) request similar mixes of "
        f"resources (Pianka overlap of their average requests). Highest: "
        f"{m['top_pair_a']} and {m['top_pair_b']} (O={m['top_overlap']:.2f}). "
        f"Similar requests are not the same as running at the same time."
    )


def narrate_resilience_low(sig: Signal) -> str:
    m = sig.metrics
    text = f"Resilience score {m['score']:.0f}/100."
    if m.get("summary"):
        text += f" {m['summary']}"
    elif m.get("mean_recovery_hours"):
        text += f" Mean recovery time: {m['mean_recovery_hours']:.1f} hours."
    return text


def narrate_resilience_degrading(sig: Signal) -> str:
    m = sig.metrics
    return (
        f"Recovery from node failures and failure spikes is taking longer than "
        f"earlier in the window. Resilience score {m['score']:.0f}/100."
    )


def narrate_externality_detected(sig: Signal) -> str:
    m = sig.metrics
    imposers = ", ".join(m.get("top_imposers", []))
    return (
        f"{m['edge_count']} pair(s) of groups where one group's resource use rises "
        f"and falls with the other's failure rate (strongest from: {imposers}). "
        f"A correlation, not proof that one causes the other."
    )


_TEMPLATE_MAP: dict[str, callable] = {

    "job_success_rate": narrate_job_success_rate,
    "partition_failure_concentration": narrate_partition_failures,
    "oom_failures": narrate_oom,
    "timeout_failures": narrate_timeout,
    "job_rate_trend": narrate_job_rate_trend,
    "filesystem_usage": narrate_filesystem_usage,
    "disk_fill_projection": narrate_disk_fill_projection,
    "gpu_job_failure_rate": narrate_gpu_failure_rate,
    "gpu_oom": narrate_gpu_oom,
    "gpu_util_gap": narrate_gpu_util_gap,
    "gpu_workload_pattern": narrate_gpu_workload_pattern,
    "gpu_hardware_health": narrate_gpu_hardware_health,
    "queue_pressure": narrate_queue_pressure,
    "high_wait_time": narrate_high_wait_time,
    "high_network_latency": narrate_network_latency,
    "packet_loss": narrate_packet_loss,
    "active_alerts": narrate_active_alerts,
    "alerts_raised": narrate_alerts_raised,
    "flapping_alert": narrate_flapping_alert,
    "cloud_cost_summary": narrate_cloud_cost,
    "underutilized_cloud_instance": narrate_underutilized_instance,
    "workstation_high_cpu": narrate_workstation_cpu,
    "workstation_high_memory": narrate_workstation_memory,
    "diversity_fragility": narrate_diversity_fragility,
    "diversity_declining": narrate_diversity_declining,
    "capacity_binding_constraint": narrate_capacity_binding,
    "capacity_saturation_imminent": narrate_capacity_saturation,
    "niche_contention_risk": narrate_niche_contention,
    "resilience_low": narrate_resilience_low,
    "resilience_degrading": narrate_resilience_degrading,
    "externality_detected": narrate_externality_detected,
}


def narrate(signal: Signal) -> str:
    """Convert a signal into a narrative string using the appropriate template.

    A template that fails (a metric it expects is missing) falls back to the
    signal's own detail: one malformed signal must not take down the brief
    or the Console page with it.
    """
    template = _TEMPLATE_MAP.get(signal.title)
    if template:
        try:
            return template(signal)
        except Exception as e:  # noqa: BLE001 - fall back, and say so in the log
            import logging
            logging.getLogger(__name__).warning(
                "Narration failed for %s (%s: %s); using its detail",
                signal.title, type(e).__name__, e)
    # Fallback: use the signal's detail field directly
    return signal.detail

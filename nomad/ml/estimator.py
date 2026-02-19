# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Joao Tonini
"""
Data Readiness Estimator for NOMAD-HPC

Estimates how much data is needed for reliable ML predictions based on:
- Sample size requirements for statistical power
- Class balance (success/failure ratio)
- Feature coverage and variance
- Network density for GNN predictions

Key thresholds based on literature and empirical testing:
- Minimum viable: 100 jobs (basic pattern detection)
- Recommended: 500 jobs (reliable predictions)
- Optimal: 1000+ jobs (robust cross-validation)
"""

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


@dataclass
class ReadinessReport:
    """Data readiness assessment report."""
    total_jobs: int
    success_jobs: int
    failure_jobs: int
    sample_score: int
    balance_score: int
    feature_score: int
    recency_score: int
    overall_score: int
    minimum_jobs: int = 100
    recommended_jobs: int = 500
    optimal_jobs: int = 1000
    status: str = "insufficient"
    ready_for_training: bool = False
    feature_coverage: dict = field(default_factory=dict)
    recommendations: list = field(default_factory=list)
    estimated_accuracy: float = 0.0
    confidence_interval: tuple = (0.0, 0.0)
    time_estimate: dict = field(default_factory=dict)


def estimate_required_sample_size(
    effect_size: float = 0.3,
    power: float = 0.80,
    alpha: float = 0.05,
    n_features: int = 17
) -> dict:
    """Estimate required sample size using power analysis principles."""
    z_alpha = 1.96 if alpha == 0.05 else 2.58
    z_power = 0.84 if power == 0.80 else 1.28
    base_n = 2 * ((z_alpha + z_power) / effect_size) ** 2
    feature_adjusted_n = max(base_n, n_features * 15)
    ml_adjusted_n = feature_adjusted_n * 1.4
    return {
        "minimum": max(100, int(ml_adjusted_n * 0.5)),
        "recommended": max(500, int(ml_adjusted_n)),
        "optimal": max(1000, int(ml_adjusted_n * 2)),
        "per_feature": 15,
        "effect_size": effect_size,
        "power": power,
    }


def compute_class_balance_score(n_success: int, n_failure: int) -> tuple:
    """Score class balance from 0-100."""
    total = n_success + n_failure
    if total == 0:
        return 0, "No data"
    failure_rate = n_failure / total
    if 0.15 <= failure_rate <= 0.35:
        return 100, "Excellent"
    elif 0.10 <= failure_rate < 0.15 or 0.35 < failure_rate <= 0.45:
        return 80, "Good"
    elif 0.05 <= failure_rate < 0.10 or 0.45 < failure_rate <= 0.55:
        return 60, "Acceptable"
    elif 0.02 <= failure_rate < 0.05 or 0.55 < failure_rate <= 0.70:
        return 40, "Poor"
    else:
        return 20, "Critical"


def compute_feature_coverage(jobs: list, feature_names: list = None) -> dict:
    """Analyze feature coverage and variance."""
    if feature_names is None:
        feature_names = [
            'runtime_sec', 'req_cpus', 'req_mem_mb', 'req_gpus',
            'avg_cpu_percent', 'peak_cpu_percent', 'avg_memory_gb', 'peak_memory_gb',
            'avg_io_wait_percent', 'total_nfs_read_gb', 'total_nfs_write_gb',
            'total_local_read_gb', 'total_local_write_gb', 'nfs_ratio',
            'exit_code', 'exit_signal', 'failure_reason'
        ]
    coverage = {}
    for feature in feature_names:
        values = []
        for job in jobs:
            val = job.get(feature)
            if val is not None:
                try:
                    values.append(float(val))
                except (ValueError, TypeError):
                    pass
        n_present = len(values)
        n_total = len(jobs)
        pct_coverage = (n_present / n_total * 100) if n_total > 0 else 0
        if values:
            mean_val = sum(values) / len(values)
            variance = sum((v - mean_val) ** 2 for v in values) / len(values) if len(values) > 1 else 0
            std_val = math.sqrt(variance)
            cv = (std_val / mean_val * 100) if mean_val != 0 else 0
        else:
            mean_val = std_val = cv = 0
        coverage[feature] = {
            'n_present': n_present,
            'pct_coverage': round(pct_coverage, 1),
            'mean': round(mean_val, 2),
            'std': round(std_val, 2),
            'cv': round(cv, 1),
            'has_variance': cv > 5,
        }
    return coverage


def compute_feature_score(coverage: dict) -> tuple:
    """Score overall feature quality from 0-100."""
    issues = []
    scores = []
    critical_features = ['runtime_sec', 'avg_cpu_percent', 'avg_memory_gb', 'nfs_ratio', 'exit_code']
    for feature, stats in coverage.items():
        if stats['pct_coverage'] >= 90:
            cov_score = 100
        elif stats['pct_coverage'] >= 70:
            cov_score = 80
        elif stats['pct_coverage'] >= 50:
            cov_score = 60
        else:
            cov_score = 40
        var_score = 100 if stats['has_variance'] else 50
        weight = 2.0 if feature in critical_features else 1.0
        scores.append((cov_score * 0.6 + var_score * 0.4) * weight)
    if scores:
        total_weight = sum(2.0 if f in critical_features else 1.0 for f in coverage.keys())
        avg_score = sum(scores) / total_weight
    else:
        avg_score = 0
    return int(avg_score), issues


def compute_recency_score(jobs: list, max_age_days: int = 90) -> tuple:
    """Score data recency from 0-100."""
    if not jobs:
        return 0, "No data"
    now = datetime.now()
    recent_count = 0
    for job in jobs:
        end_time = job.get('end_time')
        if end_time:
            try:
                if isinstance(end_time, str):
                    dt = None
                    for fmt in ['%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S']:
                        try:
                            dt = datetime.strptime(end_time[:26], fmt)
                            break
                        except ValueError:
                            continue
                    if dt is None:
                        continue
                else:
                    dt = end_time
                age_days = (now - dt).days
                if age_days <= max_age_days:
                    recent_count += 1
            except (ValueError, TypeError):
                continue
    recent_pct = (recent_count / len(jobs) * 100) if jobs else 0
    if recent_pct >= 80:
        return 100, "Excellent"
    elif recent_pct >= 60:
        return 80, "Good"
    elif recent_pct >= 40:
        return 60, "Acceptable"
    elif recent_pct >= 20:
        return 40, "Stale"
    else:
        return 20, "Very stale"


def estimate_accuracy(n_jobs: int, balance_score: int, feature_score: int) -> tuple:
    """Estimate expected model accuracy and confidence interval."""
    if n_jobs < 50:
        base_accuracy = 0.50 + 0.10 * math.log10(max(1, n_jobs))
    elif n_jobs < 500:
        base_accuracy = 0.60 + 0.05 * math.log10(n_jobs)
    else:
        base_accuracy = 0.70 + 0.03 * math.log10(n_jobs)
    balance_adjustment = (balance_score - 50) / 500
    feature_adjustment = (feature_score - 50) / 1000
    estimated = min(0.95, max(0.50, base_accuracy + balance_adjustment + feature_adjustment))
    ci_width = 0.20 / math.sqrt(max(1, n_jobs / 100))
    ci_lower = max(0.40, estimated - ci_width)
    ci_upper = min(0.99, estimated + ci_width)
    return round(estimated, 2), (round(ci_lower, 2), round(ci_upper, 2))


def estimate_time_to_readiness(report: ReadinessReport, jobs: list) -> dict:
    """Estimate how long until data is ready for reliable predictions."""
    result = {
        'collection_rate': 0.0,
        'failure_rate': 0.0,
        'days_to_minimum': 0,
        'days_to_recommended': 0,
        'days_to_optimal': 0,
        'days_to_balance': None,
        'limiting_factor': None,
        'estimate_confidence': 'low',
    }
    if not jobs:
        result['limiting_factor'] = 'No data collected yet'
        return result
    now = datetime.now()
    dates = []
    failure_dates = []
    for job in jobs:
        end_time = job.get('end_time') or job.get('submit_time')
        if end_time:
            try:
                if isinstance(end_time, str):
                    dt = None
                    for fmt in ['%Y-%m-%dT%H:%M:%S.%f', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S']:
                        try:
                            dt = datetime.strptime(end_time[:26], fmt)
                            break
                        except ValueError:
                            continue
                    if dt is None:
                        continue
                else:
                    dt = end_time
                dates.append(dt)
                if job.get('state') in ('FAILED', 'TIMEOUT', 'OUT_OF_MEMORY', 'CANCELLED'):
                    failure_dates.append(dt)
            except (ValueError, TypeError):
                continue
    if len(dates) < 2:
        result['limiting_factor'] = 'Not enough data to estimate collection rate'
        return result
    oldest = min(dates)
    newest = max(dates)
    date_range_days = max(1, (newest - oldest).days)
    collection_rate = len(dates) / date_range_days
    result['collection_rate'] = round(collection_rate, 1)
    failure_rate = len(failure_dates) / date_range_days if failure_dates else 0
    result['failure_rate'] = round(failure_rate, 2)
    n_jobs = report.total_jobs
    n_failures = report.failure_jobs
    jobs_to_minimum = max(0, report.minimum_jobs - n_jobs)
    jobs_to_recommended = max(0, report.recommended_jobs - n_jobs)
    jobs_to_optimal = max(0, report.optimal_jobs - n_jobs)
    if collection_rate > 0:
        result['days_to_minimum'] = math.ceil(jobs_to_minimum / collection_rate) if jobs_to_minimum > 0 else 0
        result['days_to_recommended'] = math.ceil(jobs_to_recommended / collection_rate) if jobs_to_recommended > 0 else 0
        result['days_to_optimal'] = math.ceil(jobs_to_optimal / collection_rate) if jobs_to_optimal > 0 else 0
    min_failures_needed = max(10, int(report.minimum_jobs * 0.05))
    failures_needed = max(0, min_failures_needed - n_failures)
    if failures_needed > 0 and failure_rate > 0:
        result['days_to_balance'] = math.ceil(failures_needed / failure_rate)
    elif failures_needed > 0 and failure_rate == 0:
        result['days_to_balance'] = -1
    else:
        result['days_to_balance'] = 0
    if n_jobs < report.minimum_jobs:
        result['limiting_factor'] = 'sample_size'
    elif report.balance_score < 40:
        result['limiting_factor'] = 'class_balance'
    elif report.feature_score < 60:
        result['limiting_factor'] = 'feature_coverage'
    elif report.recency_score < 40:
        result['limiting_factor'] = 'data_recency'
    else:
        result['limiting_factor'] = None
    if date_range_days >= 14 and len(dates) >= 100:
        result['estimate_confidence'] = 'high'
    elif date_range_days >= 7 and len(dates) >= 50:
        result['estimate_confidence'] = 'medium'
    return result


def generate_recommendations(report: ReadinessReport) -> list:
    """Generate actionable recommendations based on the report."""
    recs = []
    if report.total_jobs < report.minimum_jobs:
        needed = report.minimum_jobs - report.total_jobs
        recs.append("[!] Collect {} more jobs to reach minimum viable sample size".format(needed))
    elif report.total_jobs < report.recommended_jobs:
        needed = report.recommended_jobs - report.total_jobs
        recs.append("[*] Collect {} more jobs for recommended sample size".format(needed))
    elif report.total_jobs < report.optimal_jobs:
        needed = report.optimal_jobs - report.total_jobs
        recs.append("[i] Collect {} more jobs for optimal prediction accuracy".format(needed))
    if report.failure_jobs == 0:
        recs.append("[!] No failure data - predictions will be unreliable")
    elif report.balance_score < 40:
        failure_rate = report.failure_jobs / report.total_jobs if report.total_jobs > 0 else 0
        if failure_rate < 0.05:
            recs.append("[*] Very few failures - consider using class weights during training")
    if report.feature_score < 60:
        recs.append("[*] Feature coverage is low - ensure job_summary data is being collected")
    if report.recency_score < 60:
        recs.append("[*] Data is stale - recent patterns may differ from historical data")
    if report.ready_for_training:
        recs.append("[ok] Data is ready for ML training - run: nomad train")
    else:
        recs.append("[..] Continue collecting data before training ML models")
    return recs


def assess_readiness(db_path: str) -> ReadinessReport:
    """Assess data readiness for ML predictions."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    tables = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]
    jobs = []
    if 'jobs' in tables:
        c.execute("SELECT * FROM jobs")
        for row in c.fetchall():
            job = dict(row)
            if 'job_summary' in tables:
                c.execute("SELECT * FROM job_summary WHERE job_id = ?", (job['job_id'],))
                summary = c.fetchone()
                if summary:
                    job.update(dict(summary))
            jobs.append(job)
    conn.close()
    total = len(jobs)
    failures = sum(1 for j in jobs if j.get('state') in ('FAILED', 'TIMEOUT', 'OUT_OF_MEMORY', 'CANCELLED'))
    successes = total - failures
    requirements = estimate_required_sample_size()
    if total >= requirements['optimal']:
        sample_score = 100
        status = "optimal"
    elif total >= requirements['recommended']:
        sample_score = 80
        status = "recommended"
    elif total >= requirements['minimum']:
        sample_score = 60
        status = "minimum"
    else:
        sample_score = int(total / requirements['minimum'] * 60) if requirements['minimum'] > 0 else 0
        status = "insufficient"
    balance_score, _ = compute_class_balance_score(successes, failures)
    feature_coverage = compute_feature_coverage(jobs)
    feature_score, _ = compute_feature_score(feature_coverage)
    recency_score, _ = compute_recency_score(jobs)
    overall_score = int(
        sample_score * 0.35 +
        balance_score * 0.25 +
        feature_score * 0.25 +
        recency_score * 0.15
    )
    estimated_accuracy, confidence_interval = estimate_accuracy(total, balance_score, feature_score)
    report = ReadinessReport(
        total_jobs=total,
        success_jobs=successes,
        failure_jobs=failures,
        sample_score=sample_score,
        balance_score=balance_score,
        feature_score=feature_score,
        recency_score=recency_score,
        overall_score=overall_score,
        minimum_jobs=requirements['minimum'],
        recommended_jobs=requirements['recommended'],
        optimal_jobs=requirements['optimal'],
        status=status,
        ready_for_training=total >= requirements['minimum'] and failures >= 10 and balance_score >= 40,
        feature_coverage=feature_coverage,
        estimated_accuracy=estimated_accuracy,
        confidence_interval=confidence_interval,
    )
    report.recommendations = generate_recommendations(report)
    report.time_estimate = estimate_time_to_readiness(report, jobs)
    return report


def format_readiness_report(report: ReadinessReport, verbose: bool = False) -> str:
    """Format readiness report for terminal output (edu-style with colors)."""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    CYAN = "\033[36m"

    def score_color(score):
        if score >= 85:
            return GREEN
        if score >= 65:
            return CYAN
        if score >= 40:
            return YELLOW
        return RED

    def bar(score, width=10):
        filled = round(score / 100 * width)
        return chr(9608) * filled + chr(9617) * (width - filled)

    def level(score):
        if score >= 85:
            return "Excellent"
        if score >= 65:
            return "Good"
        if score >= 40:
            return "Developing"
        return "Needs Work"

    lines = []
    lines.append("")
    lines.append("  {}NOMAD-HPC Data Readiness{}".format(BOLD, RESET))
    lines.append("  " + chr(9472) * 56)
    oc = score_color(report.overall_score)
    lines.append("  Status: {}{}  {}%   {}{}".format(
        oc, bar(report.overall_score), report.overall_score, report.status.upper(), RESET))
    lines.append("")
    lines.append("  {}Sample Size{}".format(BOLD, RESET))
    lines.append("  " + chr(9472) * 56)
    progress = min(100, int(report.total_jobs / report.optimal_jobs * 100)) if report.optimal_jobs > 0 else 0
    pc = score_color(progress)
    lines.append("    Total Jobs       {}{}{}  {:,}".format(pc, bar(progress), RESET, report.total_jobs))
    if report.total_jobs > 0:
        success_pct = report.success_jobs / report.total_jobs * 100
        failure_pct = report.failure_jobs / report.total_jobs * 100
        success_blocks = round(success_pct / 10)
        failure_blocks = 10 - success_blocks
        ratio_bar = "{}{}{}{}".format(GREEN, chr(9608) * success_blocks, RED, chr(9608) * failure_blocks)
        lines.append("    Success/Fail     {}{}  {:.0f}%/{:.0f}%".format(ratio_bar, RESET, success_pct, failure_pct))
    lines.append("    {}Thresholds       min:{} | rec:{} | opt:{}{}".format(
        DIM, report.minimum_jobs, report.recommended_jobs, report.optimal_jobs, RESET))
    lines.append("")
    lines.append("  {}Readiness Scores{}".format(BOLD, RESET))
    lines.append("  " + chr(9472) * 56)
    c = score_color(report.sample_score)
    lines.append("    Sample Size      {}{}{}  {:3.0f}%   {}".format(c, bar(report.sample_score), RESET, report.sample_score, level(report.sample_score)))
    c = score_color(report.balance_score)
    lines.append("    Class Balance    {}{}{}  {:3.0f}%   {}".format(c, bar(report.balance_score), RESET, report.balance_score, level(report.balance_score)))
    if report.total_jobs > 0:
        failure_rate = report.failure_jobs / report.total_jobs * 100
        lines.append("    {}                 failure rate: {:.1f}% (ideal: 15-35%){}".format(DIM, failure_rate, RESET))
    c = score_color(report.feature_score)
    lines.append("    Feature Quality  {}{}{}  {:3.0f}%   {}".format(c, bar(report.feature_score), RESET, report.feature_score, level(report.feature_score)))
    c = score_color(report.recency_score)
    lines.append("    Data Recency     {}{}{}  {:3.0f}%   {}".format(c, bar(report.recency_score), RESET, report.recency_score, level(report.recency_score)))
    lines.append("    " + chr(9472) * 52)
    oc = score_color(report.overall_score)
    lines.append("    {}Overall          {}{}{}  {:3.0f}%   {}{}".format(BOLD, oc, bar(report.overall_score), RESET, report.overall_score, level(report.overall_score), RESET))
    lines.append("")
    if verbose and report.feature_coverage:
        lines.append("  {}Feature Coverage{}".format(BOLD, RESET))
        lines.append("  " + chr(9472) * 56)
        for feature, stats in sorted(report.feature_coverage.items()):
            cov = stats['pct_coverage']
            c = score_color(cov)
            ok = "{}[ok]{}".format(GREEN, RESET) if stats['has_variance'] and cov > 70 else "{}[--]{}".format(DIM, RESET)
            lines.append("    {:18s} {}{}{}  {:5.1f}%  {}".format(feature[:18], c, bar(cov), RESET, cov, ok))
        lines.append("")
    lines.append("  {}Estimated Model Performance{}".format(BOLD, RESET))
    lines.append("  " + chr(9472) * 56)
    acc = report.estimated_accuracy * 100
    c = score_color(acc)
    lines.append("    Accuracy         {}{}{}  {:.0f}%".format(c, bar(acc), RESET, acc))
    lines.append("    {}95% CI           {:.0f}% - {:.0f}%{}".format(DIM, report.confidence_interval[0] * 100, report.confidence_interval[1] * 100, RESET))
    lines.append("")
    # Collection forecast
    te = report.time_estimate
    if te and te.get('collection_rate', 0) > 0:
        lines.append("  {}Collection Forecast{}".format(BOLD, RESET))
        lines.append("  " + chr(9472) * 56)
        lines.append("    Current rate     {}{:.1f} jobs/day{}".format(CYAN, te['collection_rate'], RESET))
        if te.get('failure_rate', 0) > 0:
            lines.append("    Failure rate     {}{:.2f} failures/day{}".format(DIM, te['failure_rate'], RESET))
        if report.ready_for_training:
            lines.append("    {}Data is ready for ML training!{}".format(GREEN, RESET))
        else:
            if te.get('days_to_minimum', 0) > 0:
                lines.append("    Minimum ready    ~{} days ({} more jobs)".format(te['days_to_minimum'], report.minimum_jobs - report.total_jobs))
            if te.get('days_to_recommended', 0) > 0:
                lines.append("    Recommended      ~{} days ({} more jobs)".format(te['days_to_recommended'], report.recommended_jobs - report.total_jobs))
            if te.get('days_to_optimal', 0) > 0:
                lines.append("    Optimal          ~{} days ({} more jobs)".format(te['days_to_optimal'], report.optimal_jobs - report.total_jobs))
        if te.get('estimate_confidence') == 'low':
            lines.append("    {}Note: Estimates based on limited data{}".format(DIM, RESET))
        lines.append("")
    if report.recommendations:
        lines.append("  {}Recommendations{}".format(BOLD, RESET))
        lines.append("  " + chr(9472) * 56)
        for rec in report.recommendations:
            if "[ok]" in rec:
                lines.append("    {}{}{}".format(GREEN, rec, RESET))
            elif "[!]" in rec:
                lines.append("    {}{}{}".format(RED, rec, RESET))
            elif "[*]" in rec:
                lines.append("    {}{}{}".format(YELLOW, rec, RESET))
            else:
                lines.append("    {}".format(rec))
        lines.append("")
    return "\n".join(lines)


def check_readiness(db_path: str, verbose: bool = False) -> str:
    """Check data readiness and return formatted report."""
    report = assess_readiness(db_path)
    return format_readiness_report(report, verbose)

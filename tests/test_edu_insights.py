# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for nomad.edu.insights."""

import pytest
from nomad.edu.insights import (
    DEFAULT_THRESHOLDS, KEY_TO_DISPLAY, Issue, UserInsights,
    _aggregate_dimension,
    _classify_severity, _compute_dimension_trajectory,
    _load_thresholds, format_user_insights,
)
from nomad.edu.scoring import DimensionScore, JobFingerprint, Suggestion


def make_fp(job_id: str, scores: dict) -> JobFingerprint:
    """Build a synthetic JobFingerprint.

    scores is a dict of {short_key: (score, suggestion, detail)} where:
      - score is a float 0-100
      - suggestion is None, or a Suggestion dataclass, or a dict with the
        Suggestion fields (we'll build it for you), or a directive shorthand
        tuple like ("mem", suggested_mb, current_mb, actual_mb)
      - detail is a string
    """
    fp = JobFingerprint(job_id=job_id, user="testuser")
    for key, value in scores.items():
        if len(value) == 3:
            score, suggestion, detail = value
        else:
            score, detail = value
            suggestion = None
        long_name = KEY_TO_DISPLAY[key]

        # Convert various suggestion shapes into a Suggestion or None
        if suggestion is None or suggestion == "":
            sug = None
        elif isinstance(suggestion, Suggestion):
            sug = suggestion
        elif isinstance(suggestion, tuple):
            # Shorthand: ("mem", suggested, current, actual)
            directive, suggested, current, actual = suggestion
            unit_map = {"mem": "MB", "time": "seconds",
                        "ntasks": "cores", "gres": "GPUs"}
            sug = Suggestion(
                directive=directive,
                suggested_value=suggested,
                current_value=current,
                actual_usage=actual,
                unit=unit_map.get(directive, ""),
                rationale="test",
            )
        elif isinstance(suggestion, dict):
            sug = Suggestion(**suggestion)
        else:
            # Plain string — backward compat for old tests that just want
            # *some* suggestion present. Build a minimal placeholder.
            sug = Suggestion(
                directive="mem",
                suggested_value=2048,
                current_value=65536,
                actual_usage=1024,
                unit="MB",
                rationale="test placeholder",
            )

        fp.dimensions[key] = DimensionScore(
            name=long_name,
            score=score,
            level="Needs Work" if score < 40 else "Good",
            detail=detail,
            suggestion=sug,
            applicable=True,
        )
    return fp


class TestSeverityClassification:
    def test_critical_requires_low_avg_and_high_ratio(self):
        assert _classify_severity(avg_score=5.0, affected_ratio=0.95) == "critical"

    def test_low_avg_low_ratio_is_high_not_critical(self):
        assert _classify_severity(avg_score=10.0, affected_ratio=0.5) == "high"

    def test_moderate_avg_is_high(self):
        assert _classify_severity(avg_score=20.0, affected_ratio=0.6) == "high"

    def test_near_threshold_avg_is_medium(self):
        assert _classify_severity(avg_score=35.0, affected_ratio=0.55) == "medium"


class TestThresholdConfig:
    def test_defaults_when_no_config(self):
        t = _load_thresholds(None)
        assert t == DEFAULT_THRESHOLDS

    def test_overrides_apply(self):
        t = _load_thresholds({"thresholds": {"cpu": 30.0, "memory": 50.0}})
        assert t["cpu"] == 30.0
        assert t["memory"] == 50.0
        assert t["time"] == DEFAULT_THRESHOLDS["time"]  # unchanged

    def test_invalid_threshold_falls_back(self):
        t = _load_thresholds({"thresholds": {"cpu": "not-a-number"}})
        assert t["cpu"] == DEFAULT_THRESHOLDS["cpu"]

    def test_unknown_dimension_ignored(self):
        t = _load_thresholds({"thresholds": {"unknown_dim": 99.0}})
        assert "unknown_dim" not in t


class TestAggregation:
    def test_systemic_dimension_surfaces(self):
        # Suggestion: peak 1GB current 64GB suggested 2GB (placeholder shape)
        sug = Suggestion(directive="mem", suggested_value=2048,
                         current_value=65536, actual_usage=1024,
                         unit="MB", rationale="test")
        fps = [
            make_fp(f"job_{i}", {"memory": (5.0, sug, "Used 1GB of 64GB")})
            for i in range(10)
        ]
        issue = _aggregate_dimension("memory", fps, 40.0)
        assert issue is not None
        assert issue.affected_jobs == 10
        assert issue.avg_score == 5.0
        assert issue.severity == "critical"
        assert issue.directive == "mem"
        # The aggregator uses p95×2 of actual_usage. For uniform 1024MB usage
        # rounded up to a SLURM-friendly value, expect 2G or 4G.
        assert issue.suggested_display in ("2G", "4G")

    def test_non_systemic_filtered_out(self):
        # 1 of 4 below threshold = 25% < 50% systemic ratio
        fps = [
            make_fp("g1", {"memory": (80.0, "", "")}),
            make_fp("g2", {"memory": (75.0, "", "")}),
            make_fp("b1", {"memory": (10.0, "Try: --mem=2G", "")}),
            make_fp("g3", {"memory": (85.0, "", "")}),
        ]
        assert _aggregate_dimension("memory", fps, 40.0) is None

    def test_modal_strategy_for_discrete(self):
        # CPU recommendations are integers (mode strategy applies).
        # 3 jobs say 1 core, 2 say 2 cores -> mode is 1.
        sug1 = Suggestion(directive="ntasks", suggested_value=1,
                          current_value=8, actual_usage=1,
                          unit="cores", rationale="test")
        sug2 = Suggestion(directive="ntasks", suggested_value=2,
                          current_value=8, actual_usage=2,
                          unit="cores", rationale="test")
        fps = [
            make_fp("j1", {"cpu": (5.0, sug1, "")}),
            make_fp("j2", {"cpu": (5.0, sug1, "")}),
            make_fp("j3", {"cpu": (5.0, sug1, "")}),
            make_fp("j4", {"cpu": (5.0, sug2, "")}),
            make_fp("j5", {"cpu": (5.0, sug2, "")}),
        ]
        issue = _aggregate_dimension("cpu", fps, 40.0)
        assert issue is not None
        assert issue.directive == "ntasks"
        assert issue.suggested_display == "1"  # mode of {1,1,1,2,2} = 1
        assert issue.strategy == "mode"

    def test_quantile_strategy_for_continuous(self):
        # Memory recommendations use quantile×buffer strategy.
        # Build jobs with usage 1000, 1100, 1200, 1300, 1400 MB (range 1-1.4G).
        # p95 = ~1380, × 2x buffer = ~2760, rounded up = 4G.
        fps = []
        for i, usage in enumerate([1000, 1100, 1200, 1300, 1400]):
            sug = Suggestion(directive="mem", suggested_value=usage * 2,
                             current_value=204800, actual_usage=usage,
                             unit="MB", rationale="test")
            fps.append(make_fp(f"j{i}", {"memory": (5.0, sug, "")}))
        issue = _aggregate_dimension("memory", fps, 40.0)
        assert issue is not None
        assert issue.directive == "mem"
        assert issue.suggested_display in ("4G",)
        assert "buffer" in issue.strategy or "p95" in issue.strategy

    def test_inapplicable_excluded_from_total(self):
        sug = Suggestion(directive="mem", suggested_value=2048,
                         current_value=65536, actual_usage=1024,
                         unit="MB", rationale="test")
        fps = [make_fp(f"j{i}", {"memory": (5.0, sug, "y")}) for i in range(5)]
        # Add a fingerprint with no Memory dimension at all
        no_mem = JobFingerprint(job_id="no_mem", user="testuser")
        fps.append(no_mem)
        issue = _aggregate_dimension("memory", fps, 40.0)
        assert issue is not None
        assert issue.total_applicable == 5  # the no_mem fp is excluded
        assert issue.affected_jobs == 5


class TestFramingFollowsTheSuggestion:
    """The line explaining an issue must not contradict its suggestion, and
    the suggestion must not contradict the jobs it is about."""

    def _issue(self, key, directive, jobs):
        """jobs: (suggested, current, actual) per flagged job."""
        fps = [make_fp(f"j{i}", {key: (15.0, (directive, sug, cur, act), "")})
               for i, (sug, cur, act) in enumerate(jobs)]
        return _aggregate_dimension(key, fps, threshold=40.0)

    def test_walltime_too_short_says_so(self):
        # 2 h requested, jobs timed out at 2 h: each job's own advice is 3 h
        issue = self._issue("time", "time", [(10800, 7200, 7100)] * 4)
        assert issue.direction == "raise"
        assert issue.suggested_value > issue.current_value_typical
        assert "ran out of walltime" in issue.rationale
        assert "Over-requesting" not in issue.rationale

    def test_walltime_too_long_says_so(self):
        issue = self._issue("time", "time", [(900, 36000, 600)] * 4)
        assert issue.direction == "lower"
        assert issue.suggested_value < issue.current_value_typical
        assert issue.rationale.startswith("Over-requesting walltime")

    def test_memory_too_little_says_so(self):
        issue = self._issue("memory", "mem", [(16384, 8192, 9000)] * 4)
        assert issue.suggested_value > issue.current_value_typical
        assert "ran out of memory" in issue.rationale

    def test_memory_too_much_says_so(self):
        issue = self._issue("memory", "mem", [(2048, 65536, 1024)] * 4)
        assert "unavailable to other jobs" in issue.rationale

    def test_text_output_uses_the_issue_framing(self):
        ui = UserInsights(username="u", job_count=4, window_days=90, total_job_count=4,
                          overall_score=30.0)
        ui.issues = [self._issue("time", "time", [(10800, 7200, 7100)] * 4)]
        text = format_user_insights(ui)
        assert "ran out of walltime" in text and "Over-requesting" not in text

    def test_mixed_requests_advise_for_the_usual_one(self):
        # Seen on spydur: most jobs ask 160G and use ~10G, a few ask 4G and
        # use 16G. One value can't fit both; it used to say "4G -> 128G".
        jobs = [(32768, 163840, 10240)] * 10 + [(24576, 4096, 16384)] * 3
        issue = self._issue("memory", "mem", jobs)
        assert issue.direction == "lower" and issue.other_direction_jobs == 3
        assert issue.current_value_typical == 163840 and issue.advice_jobs == 10
        assert issue.suggested_value < issue.current_value_typical
        assert issue.suggested_value <= 32768            # p95 of 10G x 2, rounded
        assert "3 other flagged jobs ran short" in issue.suggestion_rationale
        assert "unavailable to other jobs" in issue.rationale

    def test_advice_for_a_subset_says_which_jobs(self):
        jobs = [(8192, 102400, 2048)] * 6 + [(8192, 204800, 2048)] * 4
        issue = self._issue("memory", "mem", jobs)
        assert issue.advice_jobs == 6
        assert "from your 6 flagged jobs that request" in issue.suggestion_rationale

    def test_no_advice_that_changes_nothing(self):
        # rounded up, the advice lands on the request itself: "32G -> 32G"
        issue = self._issue("memory", "mem", [(16384, 32768, 16000)] * 4)
        assert issue is not None and issue.suggested_display == ""
        assert issue.directive_label == ""
        # every job's own suggestion is its request: no direction at all
        issue = self._issue("memory", "mem", [(1024, 1024, 10)] * 4)
        assert issue.suggested_display == "" and issue.direction == ""


class TestTrajectory:
    def test_worsening_detected(self):
        fps = (
            [make_fp(f"early_{i}", {"memory": (80.0, "", "")}) for i in range(4)] +
            [make_fp(f"late_{i}", {"memory": (10.0, "x", "")}) for i in range(4)]
        )
        assert _compute_dimension_trajectory(fps, "memory") == "worsening"

    def test_improving_detected(self):
        fps = (
            [make_fp(f"early_{i}", {"memory": (10.0, "x", "")}) for i in range(4)] +
            [make_fp(f"late_{i}", {"memory": (80.0, "", "")}) for i in range(4)]
        )
        assert _compute_dimension_trajectory(fps, "memory") == "improving"

    def test_stable_when_too_few_jobs(self):
        fps = [make_fp(f"j{i}", {"memory": (50.0, "", "")}) for i in range(2)]
        assert _compute_dimension_trajectory(fps, "memory") == "stable"

    def test_overall_trend_is_the_population_rule(self):
        # The overall direction is read from the change between the first and
        # last week with measured jobs, as on Trajectory and Group Reports.
        from nomad.edu.population import trend
        assert trend(None) == "too_few_weeks"
        assert trend(12.0) == "improving"
        assert trend(-6.0) == "declining"
        assert trend(5.0) == "steady" and trend(-5.0) == "steady"


class TestFormatting:
    def test_empty_user_renders_gracefully(self):
        ui = UserInsights(username="ghost", job_count=0, window_days=90)
        text = format_user_insights(ui)
        assert "No recent jobs found for ghost" in text

    def test_no_issues_renders_clean_message(self):
        ui = UserInsights(username="great_user", job_count=10, window_days=90,
                          overall_score=85.0, overall_trajectory="stable")
        text = format_user_insights(ui)
        assert "No systemic issues detected" in text

    def test_critical_issue_in_output(self):
        ui = UserInsights(username="testuser", job_count=30, window_days=90,
                          overall_score=35.0)
        ui.issues = [Issue(
            dimension="Memory Efficiency",
            dimension_key="memory",
            affected_jobs=30,
            total_applicable=30,
            avg_score=5.0,
            severity="critical",
            trajectory="stable",
            directive="mem",
            suggested_value=4096,
            suggested_display="4G",
            current_value_typical=204800,
            current_display="200G",
            strategy="p95_with_2x_buffer",
            rationale="covers 95% of jobs with 2x safety buffer",
        )]
        text = format_user_insights(ui)
        assert "[CRITICAL]" in text
        assert "Memory Efficiency" in text
        assert "--mem=4G" in text
        assert "Memory Efficiency" in text
        assert "30/30 jobs" in text

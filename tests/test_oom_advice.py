"""A job that ran out of memory is never told to ask for less.

The peak nomad reads for such a job is often below its request: the samples
miss the spike that stopped it. The advice has to start from the request.
"""
from nomad.edu.insights import MEMORY_BUFFER_FACTOR, _aggregate_quantile
from nomad.edu.scoring import round_memory_up, score_memory

GB = 1024


def _oom(req_gb, peak_gb):
    return score_memory({"state": "OUT_OF_MEMORY", "req_mem_mb": req_gb * GB},
                        {"peak_memory_gb": peak_gb})


def test_low_reading_still_asks_for_more():
    for peak in (0, 10, 20, 29, 51):
        s = _oom(52, peak).suggestion
        assert s.suggested_value > 52 * GB, peak
        assert s.suggested_value == round_memory_up(52 * GB * 1.5)
        assert "52G" in s.rationale


def test_reading_above_the_request_is_the_base():
    s = _oom(52, 70).suggestion
    assert s.suggested_value == round_memory_up(70 * GB * 1.5)
    assert "peak" in s.rationale


def test_nothing_known_no_suggestion():
    d = _oom(0, 0)
    assert d.suggestion is None
    assert d.level == "Needs Work"


def test_reading_past_the_node_is_ignored():
    d = score_memory({"state": "OUT_OF_MEMORY", "req_mem_mb": 52 * GB, "_node_memory_mb": 64 * GB},
                     {"peak_memory_gb": 400})
    assert d.suggestion.suggested_value == round_memory_up(52 * GB * 1.5)


def test_out_of_memory_jobs_all_point_up():
    sugs = [_oom(52, p).suggestion for p in (18, 20, 22, 19, 21)]
    assert all(s.suggested_value > s.current_value for s in sugs)
    value, _, _ = _aggregate_quantile(sugs, MEMORY_BUFFER_FACTOR, round_memory_up)
    assert value > 52 * GB

"""1.67.1: a job that asks for more than twice the memory it uses is never
"Good", and nomad's memory advice (the peak × 1.5, rounded up by the Submit
page's steps) lands in the score's best band."""
import pytest

from nomad.edu.insights import MEMORY_BUFFER_FACTOR
from nomad.edu.scoring import MEMORY_ROOM, round_memory_up, score_memory
from nomad.submit.sizing import round_mem

GB = 1024


def mem(peak_gb, req_gb, state="COMPLETED"):
    return score_memory({"req_mem_mb": req_gb * GB, "state": state}, {"peak_memory_gb": peak_gb})


@pytest.mark.parametrize("peak, req, level", [
    (3.1, 9, "Developing"),          # 34%: was 69, Good
    (4.4, 9, "Developing"),          # 49%
    (2.0, 9, "Needs Work"),          # 22%
    (6.3, 9, "Excellent"),           # 70%
    (4.5, 9, "Excellent"),           # 50%
    (8.5, 9, "Good"),                # 94%: tight
    (10, 9, "Needs Work"),           # above the request
])
def test_levels(peak, req, level):
    assert mem(peak, req).level == level


def test_under_half_is_the_share_times_1_3_and_never_good():
    assert mem(3.15, 9).score == pytest.approx(45.5)
    for pct in range(11, 50):
        d = mem(9 * pct / 100, 9)
        assert d.score < 65 and d.level != "Good", pct
    assert mem(3.1, 9).suggestion is not None                 # advice comes with it


def test_tight_is_said_so_without_advice():
    d = mem(8.5, 9)
    assert d.score == 80 and d.detail.startswith("Tight memory sizing") and d.suggestion is None
    d = mem(10, 9)                                            # above the request: its own words
    assert d.score == 30 and d.detail.startswith("Peaked at") and d.suggestion.suggested_value == 15 * GB


@pytest.mark.parametrize("peak_gb", [1.2, 2.5, 3.1, 5.5, 7.9, 11, 20, 40, 90, 200, 600])
def test_following_the_advice_lands_in_the_best_band(peak_gb):
    d = mem(peak_gb, peak_gb * 4)
    advice = d.suggestion.suggested_value
    assert advice == round_memory_up(peak_gb * GB * MEMORY_ROOM)
    assert 50 <= 100 * peak_gb * GB / advice <= 75
    again = mem(peak_gb, advice / GB)
    assert again.level == "Excellent" or not again.applicable          # small requests aren't judged


def test_one_rounding_rule():
    assert MEMORY_ROOM == 1.5 and MEMORY_BUFFER_FACTOR == MEMORY_ROOM
    assert [round_memory_up(x) for x in (100, 1500, 4097, 6348, 16385, 70000, 300000)] == \
        [1024, 1536, 5120, 7168, 18432, 73728, 327680]
    for x in (100, 1500, 4097, 6348, 16385, 70000, 300000):
        assert round_mem(x) == round_memory_up(x)


def test_out_of_memory_unchanged():
    d = mem(6, 8, state="OUT_OF_MEMORY")
    assert d.score == 15 and d.suggestion.suggested_value == round_memory_up(8 * GB * 1.5)

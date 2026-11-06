import pytest

from cbrd_dup.stats import nearest_rank, pct, wilson_interval


def test_wilson_interval_known_values():
    assert wilson_interval(5, 10) == pytest.approx((0.2366, 0.7634), abs=1e-4)
    assert wilson_interval(0, 10) == pytest.approx((0.0, 0.2775), abs=1e-4)
    assert wilson_interval(10, 10) == pytest.approx((0.7225, 1.0), abs=1e-4)


def test_wilson_interval_matches_published_baseline():
    # 18 of 202 pairs in the top 10: "5.7-13.6%" in docs/evidence.md.
    low, high = wilson_interval(18, 202)
    assert f"{100 * low:.1f}-{100 * high:.1f}%" == "5.7-13.6%"


def test_wilson_interval_needs_trials():
    with pytest.raises(ValueError, match="at least one trial"):
        wilson_interval(0, 0)


def test_nearest_rank():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert nearest_rank(values, 0.5) == 5
    assert nearest_rank(values, 0.9) == 9
    assert nearest_rank(values, 0.91) == 10
    assert nearest_rank(values, 0.0) == 1
    assert nearest_rank(values, 1.0) == 10
    assert nearest_rank([], 0.5) is None
    # 0.07 * 100 is 7.000000000000001 in floating point; it must not round up to rank 8.
    assert nearest_rank(list(range(1, 101)), 0.07) == 7


def test_pct():
    assert pct(1, 8) == "1/8 (12.5%)"
    assert pct(0, 0) == "0/0"

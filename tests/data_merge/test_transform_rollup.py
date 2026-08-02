"""Seats-by-front to local-body control, and the head-vs-largest-front check."""

from __future__ import annotations

from data_merge.transform.rollup import head_cross_front, rollup


def test_seven_of_thirteen_seats_is_a_majority() -> None:
    result = rollup({"UDF": 7, "LDF": 6})
    assert result.total_seats == 13
    assert result.majority_threshold == 7
    assert result.control_type == "majority"
    assert result.ruling_front == "UDF"
    assert result.largest_front == "UDF"


def test_six_six_one_is_hung_not_tied() -> None:
    # Two fronts tie for the top, but a third front holds the remaining
    # seat, so the split isn't clean two-way and control comes back hung.
    result = rollup({"UDF": 6, "LDF": 6, "OTH": 1})
    assert result.total_seats == 13
    assert result.control_type == "hung"
    assert result.ruling_front == ""


def test_an_equal_top_two_with_nothing_left_over_is_a_tie() -> None:
    result = rollup({"UDF": 6, "LDF": 6})
    assert result.total_seats == 12
    assert result.control_type == "tie"
    assert result.largest_front == "TIE"
    assert result.ruling_front == ""


def test_a_single_leading_front_below_the_threshold_is_hung() -> None:
    result = rollup({"UDF": 5, "LDF": 4, "NDA": 4})
    assert result.control_type == "hung"
    assert result.largest_front == "UDF"
    assert result.ruling_front == ""


def test_head_cross_front_flags_a_chair_outside_the_largest_front() -> None:
    assert head_cross_front("LDF", "UDF") == "Y"
    assert head_cross_front("UDF", "UDF") == "N"
    assert head_cross_front("", "UDF") == ""
    assert head_cross_front("UDF", "TIE") == ""

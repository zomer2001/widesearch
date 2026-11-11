"""Effective periods must not turn missing dates into asserted chronology."""

from datetime import date, datetime

import pytest
from pydantic import ValidationError

from searchos.socm.temporal import TemporalRelation, TemporalScope, compare_temporal_scopes


@pytest.mark.parametrize(
    ("left_dates", "right_dates", "expected", "reverse"),
    [
        (("2023-01-01", "2023-12-31"), ("2024-01-01", "2024-12-31"),
         TemporalRelation.BEFORE, TemporalRelation.AFTER),
        (("2024-01-01", "2024-06-01"), ("2024-06-01", "2024-12-31"),
         TemporalRelation.OVERLAPS, TemporalRelation.OVERLAPS),
        (("2024-01-01", "2024-07-01"), ("2024-06-01", "2024-12-31"),
         TemporalRelation.OVERLAPS, TemporalRelation.OVERLAPS),
        (("2024-01-01", "2024-12-31"), ("2024-03-01", "2024-06-01"),
         TemporalRelation.CONTAINS, TemporalRelation.DURING),
        (("2024-01-01", "2024-12-31"), ("2024-01-01", "2024-06-01"),
         TemporalRelation.CONTAINS, TemporalRelation.DURING),
        (("2024-01-01", "2024-12-31"), ("2024-06-01", "2024-12-31"),
         TemporalRelation.CONTAINS, TemporalRelation.DURING),
        (("2024-01-01", "2024-12-31"), ("2024-01-01", "2024-12-31"),
         TemporalRelation.EQUAL, TemporalRelation.EQUAL),
        (("2024-06-01", "2024-06-01"), ("2024-06-01", "2024-06-01"),
         TemporalRelation.EQUAL, TemporalRelation.EQUAL),
        (("2024-01-01", "2024-06-01"), ("2024-06-01", "2024-06-01"),
         TemporalRelation.CONTAINS, TemporalRelation.DURING),
    ],
)
def test_complete_effective_periods_and_reverse_relations(
    left_dates, right_dates, expected, reverse
):
    left = TemporalScope(start=left_dates[0], end=left_dates[1])
    right = TemporalScope(start=right_dates[0], end=right_dates[1])

    assert compare_temporal_scopes(left, right) == expected
    assert compare_temporal_scopes(right, left) == reverse


def test_two_known_boundaries_can_prove_strict_order_despite_unknown_others():
    previous = TemporalScope(end="2024-06-01")
    following = TemporalScope(start="2024-06-02")

    assert compare_temporal_scopes(previous, following) == TemporalRelation.BEFORE
    assert compare_temporal_scopes(following, previous) == TemporalRelation.AFTER


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (TemporalScope(), TemporalScope()),
        (TemporalScope(), TemporalScope(start="2024-01-01", end="2024-12-31")),
        (TemporalScope(start="2024-01-01"), TemporalScope(start="2024-01-01")),
        (TemporalScope(end="2024-12-31"), TemporalScope(end="2024-12-31")),
        (TemporalScope(end="2024-06-01"), TemporalScope(start="2024-06-01")),
        (TemporalScope(start="2024-01-01", end="2024-12-31"),
         TemporalScope(start="2024-06-01")),
        (TemporalScope(start="2024-01-01", end="2024-12-31"),
         TemporalScope(end="2024-06-01")),
        (TemporalScope(start="2024-01-01"),
         TemporalScope(start="2024-06-01", end="2024-12-31")),
    ],
)
def test_missing_boundaries_do_not_establish_equality_containment_or_partial_overlap(left, right):
    assert compare_temporal_scopes(left, right) == TemporalRelation.UNKNOWN
    assert compare_temporal_scopes(right, left) == TemporalRelation.UNKNOWN


def test_invalid_chronological_range_is_rejected():
    with pytest.raises(ValidationError, match="start must not be after end"):
        TemporalScope(start="2024-06-02", end="2024-06-01")


@pytest.mark.parametrize("value", ["2024", "2024-06", "2024-02-30", "last year",
                                  "20240601", "2024-W01-1", "2024-06-01T00:00:00",
                                  datetime(2024, 6, 1), 0, True])
def test_endpoints_require_explicit_calendar_dates(value):
    with pytest.raises(ValidationError, match="dates or full YYYY-MM-DD"):
        TemporalScope(start=value)


def test_date_and_basis_quote_survive_json_round_trip_without_filling_unknown_endpoint():
    scope = TemporalScope(start=date(2024, 2, 29), basis_quote="Effective from 29 February 2024.")

    restored = TemporalScope.model_validate_json(scope.model_dump_json())

    assert restored == scope
    assert restored.end is None
    assert restored.basis_quote == "Effective from 29 February 2024."


def test_quote_is_retained_without_being_treated_as_an_extracted_date():
    scope = TemporalScope(basis_quote="Published in 2024; the effective period is not specified.")

    assert scope.start is None
    assert scope.end is None
    assert compare_temporal_scopes(scope, TemporalScope()) == TemporalRelation.UNKNOWN

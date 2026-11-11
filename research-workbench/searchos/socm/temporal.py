"""Conservative date-interval reasoning for the P1 evidence prototype.

Scopes are closed intervals: both dates belong to the effective period, so
intervals that meet on a date intersect. A missing endpoint is unknown, not an
unbounded or ongoing period. This module does not infer dates from source
publication times or natural-language excerpts.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from typing import Self

from pydantic import field_validator, model_validator

from searchos.util.base_model import CamelModel


class TemporalRelation(str, Enum):
    BEFORE = "before"
    AFTER = "after"
    EQUAL = "equal"
    CONTAINS = "contains"
    DURING = "during"
    OVERLAPS = "overlaps"
    UNKNOWN = "unknown"


class TemporalScope(CamelModel):
    """An effective period with optional, explicitly known calendar boundaries.

    Inputs are date objects or full ``YYYY-MM-DD`` strings. ``basis_quote``
    preserves the supporting excerpt; its presence is not a support verdict.
    A single-day scope has equal start/end dates. Supplying only one endpoint
    does not imply that the other endpoint has the same value.
    """

    start: date | None = None
    end: date | None = None
    basis_quote: str = ""

    @field_validator("start", "end", mode="before")
    @classmethod
    def _calendar_date_only(cls, value: object) -> date | None:
        if value is None:
            return None
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError:
                pass
            else:
                if parsed.isoformat() == value:
                    return parsed
        raise ValueError("Temporal endpoints must be dates or full YYYY-MM-DD strings")

    @model_validator(mode="after")
    def _valid_range(self) -> Self:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("Temporal scope start must not be after end")
        return self


def compare_temporal_scopes(left: TemporalScope, right: TemporalScope) -> TemporalRelation:
    """Return a relationship justified by the supplied boundaries alone.

    BEFORE/AFTER can be proved with just the opposing end/start pair. Other
    relationships require complete intervals: containment includes a shared
    boundary, EQUAL takes priority, and OVERLAPS means intersecting intervals
    with neither interval containing the other. UNKNOWN does not mean disjoint;
    it means these inputs do not establish one of the more specific relations.
    """
    if left.end is not None and right.start is not None and left.end < right.start:
        return TemporalRelation.BEFORE
    if left.start is not None and right.end is not None and left.start > right.end:
        return TemporalRelation.AFTER

    if left.start is None or left.end is None or right.start is None or right.end is None:
        return TemporalRelation.UNKNOWN

    if left.start == right.start and left.end == right.end:
        return TemporalRelation.EQUAL
    if left.start <= right.start and left.end >= right.end:
        return TemporalRelation.CONTAINS
    if left.start >= right.start and left.end <= right.end:
        return TemporalRelation.DURING
    return TemporalRelation.OVERLAPS

"""Domain types shared by source (Baby Daybook) and sink (Huckleberry).

SQLite rows never cross into here and Firestore dicts never leave from here: source.py parses
into these types, and sink.py serializes from them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, NewType

type Side = Literal["left", "right"]

# Stable identity of one Baby Daybook row, e.g. "uid:3f2a..." or "breastfeeding@1419000000000".
# It is the only input to the Huckleberry document id, so the same row always maps to the same
# document, and fixing how fields are mapped updates documents in place.
SourceKey = NewType("SourceKey", str)


@dataclass(frozen=True, slots=True)
class Interval:
    """A completed span with positive length, in UTC. Build it with `Interval.of`."""

    start: datetime
    end: datetime

    @classmethod
    def of(cls, start_ms: int, end_ms: int) -> Interval:
        """Raises ValueError when there is no end or the span is not positive."""
        if not end_ms or end_ms <= start_ms:
            raise ValueError("no end time or non-positive duration")
        return cls(
            datetime.fromtimestamp(start_ms / 1000, UTC),
            datetime.fromtimestamp(end_ms / 1000, UTC),
        )

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass(frozen=True, slots=True)
class Sleep:
    key: SourceKey
    span: Interval
    notes: str | None


@dataclass(frozen=True, slots=True)
class Nursing:
    """left_s + right_s is the feeding time. It can be shorter than span.seconds because of pauses."""

    key: SourceKey
    span: Interval
    left_s: float
    right_s: float
    last_side: Side
    notes: str | None


type Event = Sleep | Nursing


@dataclass(frozen=True, slots=True)
class Skip:
    """A source row that is deliberately not imported, with the reason shown in the dry run."""

    key: SourceKey
    kind: str
    reason: str


@dataclass(frozen=True, slots=True)
class SourceReport:
    events: tuple[Event, ...]  # sorted by span.start
    skipped: tuple[Skip, ...]

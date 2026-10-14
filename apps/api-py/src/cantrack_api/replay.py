"""Walk replay: timeline read and GPS lookup over recorded walk data (FR-27)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .structures import DoublyLinkedList

FORWARD: str = "forward"
BACKWARD: str = "backward"


@dataclass(frozen=True)
class TimelineEvent:
    """One recorded moment on a walk's replay timeline.

    ``kind`` is the event name (``check_in``, ``photo``, ``check_out``),
    ``at`` is the ISO-8601 timestamp and ``payload`` the event details.
    """

    kind: str
    at: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class GpsPoint:
    """One recorded GPS fix of a walk, ordered by ``at_epoch``."""

    at_epoch: float
    lat: float
    lng: float


def build_timeline(walk_rows: list[dict[str, Any]]) -> DoublyLinkedList[TimelineEvent]:
    """Sort walk_timeline rows by ``at`` and append them as TimelineEvents.

    The sort is stable, so rows sharing a timestamp keep their input order.

    Big-O: O(n log n) for the sort plus O(n) for the appends, O(n log n)
    overall, where n is the number of walk_timeline rows.
    """
    walk_timeline: DoublyLinkedList[TimelineEvent] = DoublyLinkedList()
    for walk_row in sorted(walk_rows, key=lambda walk_row: walk_row["at"]):
        walk_timeline.append(
            TimelineEvent(
                kind=walk_row["kind"],
                at=walk_row["at"],
                payload=walk_row["payload"],
            )
        )
    return walk_timeline


def read_timeline(
    walk_timeline: DoublyLinkedList[TimelineEvent],
    start_index: int,
    steps: int,
    direction: str,
) -> list[TimelineEvent]:
    """Return ``steps`` events from ``start_index`` in ``direction``.

    ``direction`` must be ``"forward"`` (toward later events) or ``"backward"``
    (toward earlier events); anything else raises ValueError. ``start_index``
    must index the walk_timeline; anything out of range raises IndexError.

    Big-O: O(n), where n is the walk_timeline size (it is a slice of the
    forward or reverse array walk).
    """
    if direction not in (FORWARD, BACKWARD):
        raise ValueError("Direction must be 'forward' or 'backward'.")
    if start_index < 0 or start_index >= walk_timeline.size():
        raise IndexError("Start index is out of range.")
    if direction == FORWARD:
        return walk_timeline.to_array()[start_index : start_index + steps]
    reverse_start = walk_timeline.size() - 1 - start_index
    return walk_timeline.to_array_reverse()[reverse_start : reverse_start + steps]


def find_gps_at(gps_points: list[GpsPoint], moment: float) -> GpsPoint:
    """Return the latest GPS fix at or before ``moment``.

    Hand-written binary search over the gps_points sorted by ``at_epoch``; no
    ``bisect``. Raises ValueError when the walk has no gps_points, and
    IndexError when ``moment`` precedes the first recorded fix.

    Big-O: O(log n), where n is the number of gps_points.
    """
    if not gps_points:
        raise ValueError("Cannot search a walk without gps_points.")
    if moment < gps_points[0].at_epoch:
        raise IndexError("Moment is before the first recorded gps_point.")
    low = 0
    high = len(gps_points) - 1
    while low <= high:
        middle = (low + high) // 2
        if gps_points[middle].at_epoch == moment:
            return gps_points[middle]
        if gps_points[middle].at_epoch < moment:
            low = middle + 1
        else:
            high = middle - 1
    return gps_points[high]

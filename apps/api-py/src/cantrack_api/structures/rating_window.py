"""Circular buffer of the last N ratings of a walker (FR-26)."""

from __future__ import annotations

DEFAULT_CAPACITY = 25

MINIMUM_RATING = 1.0
MAXIMUM_RATING = 5.0


class RatingWindow:
    """Rolling window of a walker's most recent ratings, plus the all-time mean (FR-26).

    Backing store: a fixed-size list of length ``capacity``, a write index that
    wraps with modulo, a running sum of the buffered ratings, and two all-time
    counters kept separate from the window. When the buffer is full, the rating
    about to be overwritten is subtracted from the running sum, so no average is
    ever recomputed from scratch.

    Big-O:
    - add: O(1)
    - recent_average: O(1)
    - all_time_average: O(1)
    - recent_count: O(1)
    - all_time_count: O(1)
    """

    __slots__ = (
        "_capacity",
        "_ratings",
        "_write_index",
        "_window_count",
        "_window_sum",
        "_all_time_count",
        "_all_time_sum",
    )

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError(f"capacity must be at least 1, got {capacity}")
        self._capacity: int = capacity
        self._ratings: list[float] = [0.0] * capacity
        self._write_index: int = 0
        self._window_count: int = 0
        self._window_sum: float = 0.0
        self._all_time_count: int = 0
        self._all_time_sum: float = 0.0

    def add(self, rating: float) -> None:
        """Record ``rating``, evicting the oldest one when full. O(1).

        Raises ValueError unless ``1 <= rating <= 5``, leaving the window and
        the all-time totals untouched.
        """
        if rating < MINIMUM_RATING or rating > MAXIMUM_RATING:
            raise ValueError(
                f"rating must be between {MINIMUM_RATING} and {MAXIMUM_RATING}, got {rating}"
            )
        if self._window_count == self._capacity:
            self._window_sum -= self._ratings[self._write_index]
        else:
            self._window_count += 1
        self._ratings[self._write_index] = rating
        self._window_sum += rating
        self._all_time_sum += rating
        self._all_time_count += 1
        self._write_index = (self._write_index + 1) % self._capacity

    def recent_average(self) -> float | None:
        """Return the mean of the buffered ratings, or None when the window is empty. O(1)."""
        if self._window_count == 0:
            return None
        return self._window_sum / self._window_count

    def all_time_average(self) -> float | None:
        """Return the mean of every rating ever recorded, or None when there are none. O(1)."""
        if self._all_time_count == 0:
            return None
        return self._all_time_sum / self._all_time_count

    def recent_count(self) -> int:
        """Return how many ratings the window currently holds. O(1)."""
        return self._window_count

    def all_time_count(self) -> int:
        """Return how many ratings have been recorded in total. O(1)."""
        return self._all_time_count

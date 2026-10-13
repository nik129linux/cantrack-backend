"""Array-backed binary max-heap for offer ranking (FR-23)."""

from __future__ import annotations

import math
from typing import Generic, TypeVar

T = TypeVar("T")


class _HeapEntry(Generic[T]):
    """One entry of the heap's backing store.

    Holds the stored value, its priority, optional expiry and insertion
    sequence. No public methods.
    """

    __slots__ = ("value", "priority", "expires_at", "sequence")

    def __init__(
        self, value: T, priority: float, expires_at: float | None, sequence: int
    ) -> None:
        self.value: T = value
        self.priority: float = priority
        self.expires_at: float | None = expires_at
        self.sequence: int = sequence


def _entry_greater(left: _HeapEntry[T], right: _HeapEntry[T]) -> bool:
    if left.priority != right.priority:
        return left.priority > right.priority
    return left.sequence < right.sequence


class MaxHeap(Generic[T]):
    """Max-heap that returns the highest-priority offer first (FR-23).

    Ties are broken by insertion order: the earlier push wins. Expired
    entries are discarded lazily from the top on ``pop_max`` and
    ``peek_max``.

    Backing store: Python ``list`` used as the array of a binary heap,
    with hand-written sift-up and sift-down.

    Big-O:
    - push: O(log n)
    - pop_max: O(log n) amortized, each expired entry is discarded once
    - peek_max: O(1) plus discards of expired tops
    - size: O(1)
    - is_empty: O(1)
    """

    __slots__ = ("_entries", "_sequence")

    def __init__(self) -> None:
        self._entries: list[_HeapEntry[T]] = []
        self._sequence: int = 0

    def _sift_up(self, index: int) -> None:
        while index > 0:
            parent_index = (index - 1) // 2
            if _entry_greater(self._entries[index], self._entries[parent_index]):
                self._entries[index], self._entries[parent_index] = (
                    self._entries[parent_index],
                    self._entries[index],
                )
                index = parent_index
            else:
                break

    def _sift_down(self, index: int) -> None:
        count = len(self._entries)
        while True:
            left_index = 2 * index + 1
            right_index = 2 * index + 2
            largest_index = index
            if left_index < count and _entry_greater(
                self._entries[left_index], self._entries[largest_index]
            ):
                largest_index = left_index
            if right_index < count and _entry_greater(
                self._entries[right_index], self._entries[largest_index]
            ):
                largest_index = right_index
            if largest_index != index:
                self._entries[index], self._entries[largest_index] = (
                    self._entries[largest_index],
                    self._entries[index],
                )
                index = largest_index
            else:
                break

    def _discard_expired(self, now: float | None) -> None:
        if now is None:
            return
        while self._entries:
            top_entry = self._entries[0]
            if top_entry.expires_at is not None and top_entry.expires_at <= now:
                last_entry = self._entries.pop()
                if self._entries:
                    self._entries[0] = last_entry
                    self._sift_down(0)
            else:
                break

    def push(self, value: T, priority: float, expires_at: float | None = None) -> None:
        """Insert ``value`` with ``priority`` and optional ``expires_at``. O(log n).

        Raises ValueError if ``priority`` is NaN or infinite.
        """
        if not math.isfinite(priority):
            raise ValueError("priority must be finite")
        entry = _HeapEntry(value, priority, expires_at, self._sequence)
        self._sequence += 1
        self._entries.append(entry)
        self._sift_up(len(self._entries) - 1)

    def pop_max(self, now: float | None = None) -> T:
        """Remove and return the highest-priority live value. O(log n) amortized.

        Discards expired entries from the top until a live one is found.
        Each expired entry is discarded once. Raises IndexError if nothing
        live remains. With ``now`` as None, nothing counts as expired.
        """
        self._discard_expired(now)
        if not self._entries:
            raise IndexError("Cannot pop from an empty heap.")
        last_entry = self._entries.pop()
        if not self._entries:
            return last_entry.value
        top_entry = self._entries[0]
        self._entries[0] = last_entry
        self._sift_down(0)
        return top_entry.value

    def peek_max(self, now: float | None = None) -> T:
        """Return the highest-priority live value without removing it. O(1) plus discards.

        Discards expired entries from the top until a live one is at the
        top. Raises IndexError if nothing live remains. With ``now`` as
        None, nothing counts as expired.
        """
        self._discard_expired(now)
        if not self._entries:
            raise IndexError("Cannot peek at an empty heap.")
        return self._entries[0].value

    def size(self) -> int:
        """Return how many entries the heap stores, including not-yet-discarded expired ones. O(1)."""
        return len(self._entries)

    def is_empty(self) -> bool:
        """Return whether the heap stores no entries. O(1)."""
        return len(self._entries) == 0

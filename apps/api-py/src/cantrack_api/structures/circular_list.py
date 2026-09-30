"""Circular list rotating through recurring routes (FR-13)."""

from __future__ import annotations

from typing import Generic, TypeVar

T = TypeVar("T")


class _CircularNode(Generic[T]):
    """One link of the cycle's backing store.

    Holds a value and a pointer to the next node; on the tail that pointer
    wraps back to the first node. No public methods.
    """

    __slots__ = ("value", "next")

    def __init__(self, value: T) -> None:
        self.value: T = value
        self.next: _CircularNode[T] | None = None


class CircularList(Generic[T]):
    """Round-robin cursor over recurring routes (FR-13).

    Backing store: singly linked nodes with head, tail and current pointers,
    where the tail links back to the head to close the cycle.

    Big-O:
    - add: O(1)
    - current: O(1)
    - next: O(1)
    - size: O(1)
    """

    __slots__ = ("_head", "_tail", "_cursor", "_count")

    def __init__(self) -> None:
        self._head: _CircularNode[T] | None = None
        self._tail: _CircularNode[T] | None = None
        self._cursor: _CircularNode[T] | None = None
        self._count: int = 0

    def add(self, value: T) -> None:
        """Add ``value`` at the end of the cycle, leaving the cursor put. O(1)."""
        node = _CircularNode(value)
        if self._tail is None:
            node.next = node
            self._head = node
            self._tail = node
            self._cursor = node
        else:
            self._tail.next = node
            node.next = self._head
            self._tail = node
        self._count += 1

    def current(self) -> T:
        """Return the value under the cursor. O(1). Raises IndexError if empty."""
        if self._cursor is None:
            raise IndexError("Cannot read the current item of an empty circular list.")
        return self._cursor.value

    def next(self) -> T:
        """Advance the cursor and return the value it lands on. O(1).

        Wraps from the tail back to the head. Raises IndexError if empty.
        """
        if self._cursor is None:
            raise IndexError("Cannot advance an empty circular list.")
        node = self._cursor.next
        if node is None:
            raise IndexError("Cannot advance to a missing item in a circular list.")
        self._cursor = node
        return node.value

    def size(self) -> int:
        """Return how many values the cycle holds. O(1)."""
        return self._count
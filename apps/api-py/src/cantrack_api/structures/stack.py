"""Stack (LIFO) backing the walk check-in undo history (FR-11)."""

from __future__ import annotations

from typing import Generic, TypeVar

T = TypeVar("T")


class _StackNode(Generic[T]):
    """One link of the stack's backing store.

    Holds a value and a pointer to the node below it. No public methods.
    """

    __slots__ = ("value", "next")

    def __init__(self, value: T) -> None:
        self.value: T = value
        self.next: _StackNode[T] | None = None


class Stack(Generic[T]):
    """LIFO stack of check-in records, undone most recent first (FR-11).

    Backing store: singly linked nodes anchored at the top.

    Big-O:
    - push: O(1)
    - pop: O(1)
    - peek: O(1)
    - size: O(1)
    - is_empty: O(1)
    """

    __slots__ = ("_top", "_count")

    def __init__(self) -> None:
        self._top: _StackNode[T] | None = None
        self._count: int = 0

    def push(self, value: T) -> None:
        """Push ``value`` on top of the stack. O(1)."""
        node = _StackNode(value)
        node.next = self._top
        self._top = node
        self._count += 1

    def pop(self) -> T:
        """Remove and return the top value. O(1). Raises IndexError if empty."""
        if self._top is None:
            raise IndexError("Cannot pop from an empty stack.")
        node = self._top
        self._top = node.next
        self._count -= 1
        return node.value

    def peek(self) -> T:
        """Return the top value without removing it. O(1). Raises IndexError if empty."""
        if self._top is None:
            raise IndexError("Cannot peek at an empty stack.")
        return self._top.value

    def size(self) -> int:
        """Return how many values the stack holds. O(1)."""
        return self._count

    def is_empty(self) -> bool:
        """Return whether the stack holds no values. O(1)."""
        return self._count == 0
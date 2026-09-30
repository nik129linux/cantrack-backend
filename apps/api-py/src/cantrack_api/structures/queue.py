"""Queue (FIFO) buffering GPS points before persistence (FR-14)."""

from __future__ import annotations

from typing import Generic, TypeVar

T = TypeVar("T")


class _QueueNode(Generic[T]):
    """One link of the queue's backing store.

    Holds a value and a pointer to the node enqueued after it. No public
    methods.
    """

    __slots__ = ("value", "next")

    def __init__(self, value: T) -> None:
        self.value: T = value
        self.next: _QueueNode[T] | None = None


class Queue(Generic[T]):
    """FIFO queue of buffered GPS points (FR-14).

    Backing store: singly linked nodes with head and tail pointers.

    Big-O:
    - enqueue: O(1)
    - dequeue: O(1)
    - front: O(1)
    - size: O(1)
    - is_empty: O(1)
    """

    __slots__ = ("_head", "_tail", "_count")

    def __init__(self) -> None:
        self._head: _QueueNode[T] | None = None
        self._tail: _QueueNode[T] | None = None
        self._count: int = 0

    def enqueue(self, value: T) -> None:
        """Append ``value`` after the tail. O(1)."""
        node = _QueueNode(value)
        if self._tail is None:
            self._head = node
        else:
            self._tail.next = node
        self._tail = node
        self._count += 1

    def dequeue(self) -> T:
        """Remove and return the front value. O(1). Raises IndexError if empty."""
        if self._head is None:
            raise IndexError("Cannot dequeue from an empty queue.")
        node = self._head
        self._head = node.next
        if self._head is None:
            self._tail = None
        self._count -= 1
        return node.value

    def front(self) -> T:
        """Return the front value without removing it. O(1). Raises IndexError if empty."""
        if self._head is None:
            raise IndexError("Cannot read the front of an empty queue.")
        return self._head.value

    def size(self) -> int:
        """Return how many values the queue holds. O(1)."""
        return self._count

    def is_empty(self) -> bool:
        """Return whether the queue holds no values. O(1)."""
        return self._count == 0
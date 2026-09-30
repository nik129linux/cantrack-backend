"""Doubly linked list holding the walk timeline events (FR-12)."""

from __future__ import annotations

from typing import Generic, TypeVar

T = TypeVar("T")


class _DoublyNode(Generic[T]):
    """One link of the list's backing store.

    Holds a value plus pointers to the previous and next nodes. No public
    methods.
    """

    __slots__ = ("value", "previous", "next")

    def __init__(self, value: T) -> None:
        self.value: T = value
        self.previous: _DoublyNode[T] | None = None
        self.next: _DoublyNode[T] | None = None


class DoublyLinkedList(Generic[T]):
    """Walk timeline of check-ins, incidents and check-outs (FR-12).

    Backing store: doubly linked nodes with head and tail pointers, so the
    timeline can be read in either direction.

    Big-O:
    - append: O(1)
    - prepend: O(1)
    - remove_at: O(n)
    - to_array: O(n)
    - to_array_reverse: O(n)
    - size: O(1)
    """

    __slots__ = ("_head", "_tail", "_count")

    def __init__(self) -> None:
        self._head: _DoublyNode[T] | None = None
        self._tail: _DoublyNode[T] | None = None
        self._count: int = 0

    def append(self, value: T) -> None:
        """Add ``value`` after the tail. O(1)."""
        node = _DoublyNode(value)
        node.previous = self._tail
        if self._tail is None:
            self._head = node
        else:
            self._tail.next = node
        self._tail = node
        self._count += 1

    def prepend(self, value: T) -> None:
        """Add ``value`` before the head. O(1)."""
        node = _DoublyNode(value)
        node.next = self._head
        if self._head is None:
            self._tail = node
        else:
            self._head.previous = node
        self._head = node
        self._count += 1

    def remove_at(self, index: int) -> None:
        """Remove the value at ``index``, repairing both of its links. O(n).

        ``index`` must be an int in ``0..size - 1``. Raises IndexError
        otherwise.
        """
        if index < 0 or index >= self._count:
            raise IndexError("Removal index is out of range.")

        current = self._head
        for _ in range(index):
            current = current.next

        if current.previous is None:
            self._head = current.next
            if self._head is None:
                self._tail = None
            else:
                self._head.previous = None
        else:
            current.previous.next = current.next
            if current.next is None:
                self._tail = current.previous
            else:
                current.next.previous = current.previous

        current.previous = None
        current.next = None
        self._count -= 1

    def to_array(self) -> list[T]:
        """Return every value from head to tail. O(n)."""
        values: list[T] = []
        current = self._head
        while current is not None:
            values.append(current.value)
            current = current.next
        return values

    def to_array_reverse(self) -> list[T]:
        """Return every value from tail to head. O(n)."""
        values: list[T] = []
        current = self._tail
        while current is not None:
            values.append(current.value)
            current = current.previous
        return values

    def size(self) -> int:
        """Return how many values the list holds. O(1)."""
        return self._count
"""Singly linked list holding the ordered stops of a route (FR-06, FR-07)."""

from __future__ import annotations

from typing import Generic, TypeVar

T = TypeVar("T")


class _SinglyNode(Generic[T]):
    """One link of the list's backing store.

    Holds a value and a pointer to the following node. No public methods.
    """

    __slots__ = ("value", "next")

    def __init__(self, value: T) -> None:
        self.value: T = value
        self.next: _SinglyNode[T] | None = None


class SinglyLinkedList(Generic[T]):
    """Ordered list of route stops (FR-06 and FR-07).

    Backing store: singly linked nodes with head and tail pointers, so appends
    stay O(1) while indexed access walks the chain.

    Big-O:
    - append: O(1)
    - insert_at: O(n)
    - remove_at: O(n)
    - get: O(n)
    - to_array: O(n)
    - size: O(1)
    """

    __slots__ = ("_head", "_tail", "_count")

    def __init__(self) -> None:
        self._head: _SinglyNode[T] | None = None
        self._tail: _SinglyNode[T] | None = None
        self._count: int = 0

    def append(self, value: T) -> None:
        """Add ``value`` after the tail. O(1)."""
        node = _SinglyNode(value)
        if self._tail is None:
            self._head = node
        else:
            self._tail.next = node
        self._tail = node
        self._count += 1

    def insert_at(self, index: int, value: T) -> None:
        """Insert ``value`` so it ends up at ``index``. O(n).

        ``index`` must be an int in ``0..size``. Raises IndexError otherwise.
        """
        if index < 0 or index > self._count:
            raise IndexError("Insertion index is out of range.")

        if index == 0:
            node = _SinglyNode(value)
            node.next = self._head
            self._head = node
            if self._tail is None:
                self._tail = node
            self._count += 1
            return

        node = _SinglyNode(value)
        previous = self._node_at(index - 1)
        node.next = previous.next
        previous.next = node
        if index == self._count:
            self._tail = node
        self._count += 1

    def remove_at(self, index: int) -> None:
        """Remove the value at ``index``. O(n).

        ``index`` must be an int in ``0..size - 1``. Raises IndexError
        otherwise.
        """
        if index < 0 or index >= self._count:
            raise IndexError("Removal index is out of range.")

        if index == 0:
            self._head = self._head.next
            self._count -= 1
            if self._head is None:
                self._tail = None
            return

        previous = self._node_at(index - 1)
        removed = previous.next
        previous.next = removed.next
        if removed is self._tail:
            self._tail = previous
        self._count -= 1

    def get(self, index: int) -> T:
        """Return the value at ``index``. O(n).

        ``index`` must be an int in ``0..size - 1``. Raises IndexError
        otherwise.
        """
        if index < 0 or index >= self._count:
            raise IndexError("Access index is out of range.")
        return self._node_at(index).value

    def to_array(self) -> list[T]:
        """Return every value from head to tail. O(n)."""
        values: list[T] = []
        current = self._head
        while current is not None:
            values.append(current.value)
            current = current.next
        return values

    def size(self) -> int:
        """Return how many values the list holds. O(1)."""
        return self._count

    def _node_at(self, index: int) -> _SinglyNode[T]:
        current = self._head
        for _ in range(index):
            current = current.next
        return current
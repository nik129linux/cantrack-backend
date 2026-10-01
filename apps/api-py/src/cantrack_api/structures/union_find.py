"""UnionFind (disjoint sets) grouping compatible dogs into packs (FR-15).

S2 uses it for the pickup plan: inside a time cluster, every pair of dogs that
is compatible (neither is reactive and their pins are within the group radius)
is unioned, so a component connects dogs transitively by proximity; components
larger than the pack cap are then chunked in plan order.

Only ``typing``/``dataclasses``-level standard library is used — no sets, no
graphs, nothing that implements a structure for us (NFR-03).
"""

from __future__ import annotations

from typing import Generic, Hashable, TypeVar

T = TypeVar("T", bound=Hashable)


class UnionFind(Generic[T]):
    """Disjoint-set forest with union by size and ITERATIVE path compression.

    Backing store: two dicts (parent pointers and subtree sizes) plus a count
    of disjoint sets.

    Big-O (alpha = inverse Ackermann, effectively constant):
    - add: O(1) amortized
    - find: O(alpha(n)) amortized, worst case O(log n) per call before
      compression; iterative, never recursive (a 100 000-long chain must not
      need 100 000 stack frames)
    - union: O(alpha(n)) amortized (two finds + O(1) merge)
    - connected: O(alpha(n)) amortized (two finds)
    - count: O(1)
    """

    __slots__ = ("_parent", "_size", "_count")

    def __init__(self) -> None:
        self._parent: dict[T, T] = {}
        self._size: dict[T, int] = {}
        self._count: int = 0

    def add(self, element: T) -> None:
        """Register ``element`` as its own singleton set (idempotent).

        Big-O: O(1) amortized.
        """
        if element not in self._parent:
            self._parent[element] = element
            self._size[element] = 1
            self._count += 1

    def find(self, element: T) -> T:
        """Return the representative (root) of ``element``'s set.

        Two iterative passes: walk up to the root, then walk again compressing
        every visited node directly onto the root.

        Big-O: O(alpha(n)) amortized.

        Raises:
            KeyError: If ``element`` was never added.
        """
        if element not in self._parent:
            raise KeyError(element)

        root = element
        while self._parent[root] != root:
            root = self._parent[root]

        # Iterative path compression (no recursion proportional to n).
        current = element
        while self._parent[current] != root:
            self._parent[current], current = root, self._parent[current]

        return root

    def union(self, a: T, b: T) -> bool:
        """Merge the sets of ``a`` and ``b``; True when it actually merged.

        Union by size: the smaller tree hangs under the larger one, keeping
        find() shallow without needing recursion.

        Big-O: O(alpha(n)) amortized.

        Raises:
            KeyError: If either element was never added.
        """
        root_a = self.find(a)
        root_b = self.find(b)
        if root_a == root_b:
            return False

        if self._size[root_a] < self._size[root_b]:
            root_a, root_b = root_b, root_a
        self._parent[root_b] = root_a
        self._size[root_a] += self._size[root_b]
        self._count -= 1
        return True

    def connected(self, a: T, b: T) -> bool:
        """Whether ``a`` and ``b`` are in the same set.

        Big-O: O(alpha(n)) amortized.

        Raises:
            KeyError: If either element was never added.
        """
        return self.find(a) == self.find(b)

    def count(self) -> int:
        """The number of disjoint sets.

        Big-O: O(1).
        """
        return self._count

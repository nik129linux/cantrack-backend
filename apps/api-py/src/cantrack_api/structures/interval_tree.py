"""Hand-written interval tree for FR-17 (walk-request conflict detection).

An augmented AVL tree: every node stores a half-open interval ``[start, end)``
keyed by its start plus the maximum end of its whole subtree, so an overlap
query can prune entire branches. Keys are ISO-8601 timestamps in a single
uniform format (the routers normalise to UTC before inserting), where
lexicographic order equals chronological order; any totally-ordered comparable
key works.

The tree is self-balancing on purpose: 5000 ascending inserts degenerate a
plain BST into a 5000-deep chain and any recursive walk then dies with a
``RecursionError`` (default limit 1000). With AVL rotations the height stays
O(log n) (~13 for 5000 nodes), so the bounded recursion here is safe.

No data-structure library is used — nodes are hand-written linked objects
(only ``typing`` from the standard library).

Big-O (n = number of intervals, k = number reported by a query):
- insert: O(log n)
- search: O(log n + k) with max-end pruning; O(n) worst case when every
  interval overlaps the query
- size: O(1)
- is_empty: O(1)
"""

from __future__ import annotations

from typing import Generic, TypeVar

T = TypeVar("T")


class _IntervalNode(Generic[T]):
    """One node of the tree: an interval, its value and the AVL bookkeeping.

    ``max_end`` is the largest ``end`` in this subtree (this node included) —
    the augmentation that lets ``search`` skip a whole branch whose intervals
    all finish before the query starts. No public methods.
    """

    __slots__ = ("start", "end", "value", "height", "max_end", "left", "right")

    def __init__(self, start: str, end: str, value: T) -> None:
        self.start: str = start
        self.end: str = end
        self.value: T = value
        self.height: int = 1
        self.max_end: str = end
        self.left: _IntervalNode[T] | None = None
        self.right: _IntervalNode[T] | None = None


class IntervalTree(Generic[T]):
    """A balanced interval tree over half-open ``[start, end)`` intervals.

    Overlap is ``a.start < b.end and b.start < a.end``: intervals that merely
    touch (one ends exactly where the other starts) do NOT overlap, which is
    what makes back-to-back walks conflict-free.

    ``search`` returns the values of every overlapping interval sorted by
    interval start (an in-order walk of the BST), so callers get a
    deterministic, chronological answer.

    Big-O: see the module docstring (insert O(log n), search O(log n + k),
    size/is_empty O(1)).
    """

    __slots__ = ("_root", "_size")

    def __init__(self) -> None:
        self._root: _IntervalNode[T] | None = None
        self._size: int = 0

    def is_empty(self) -> bool:
        """Return whether the tree holds no intervals. O(1)."""
        return self._size == 0

    def size(self) -> int:
        """Return the number of inserted intervals. O(1)."""
        return self._size

    @staticmethod
    def _validate(start: str, end: str) -> None:
        """Reject a degenerate or inverted interval before anything mutates."""
        if not start < end:
            raise ValueError("Interval end must be after its start.")

    def insert(self, start: str, end: str, value: T) -> None:
        """Insert ``[start, end)`` carrying ``value``. O(log n).

        Raises:
            ValueError: If ``end <= start``; nothing is stored then.
        """
        self._validate(start, end)
        self._root = self._insert(self._root, start, end, value)
        self._size += 1

    def search(self, start: str, end: str) -> list[T]:
        """Return every value whose interval overlaps ``[start, end)``.

        Results come sorted by the stored intervals' start. O(log n + k).

        Raises:
            ValueError: If ``end <= start``.
        """
        self._validate(start, end)
        hits: list[T] = []
        self._search(self._root, start, end, hits)
        return hits

    # -- internals ----------------------------------------------------------

    def _insert(self, node: _IntervalNode[T] | None, start: str, end: str, value: T) -> _IntervalNode[T]:
        if node is None:
            return _IntervalNode(start, end, value)
        if start < node.start:
            node.left = self._insert(node.left, start, end, value)
        else:
            node.right = self._insert(node.right, start, end, value)
        self._refresh(node)
        return self._rebalance(node)

    @staticmethod
    def _height(node: _IntervalNode[T] | None) -> int:
        return node.height if node is not None else 0

    @staticmethod
    def _refresh(node: _IntervalNode[T]) -> None:
        """Recompute height and the subtree max-end after a child changed."""
        node.height = 1 + max(IntervalTree._height(node.left), IntervalTree._height(node.right))
        max_end = node.end
        if node.left is not None and node.left.max_end > max_end:
            max_end = node.left.max_end
        if node.right is not None and node.right.max_end > max_end:
            max_end = node.right.max_end
        node.max_end = max_end

    @staticmethod
    def _balance_factor(node: _IntervalNode[T]) -> int:
        return IntervalTree._height(node.left) - IntervalTree._height(node.right)

    @staticmethod
    def _rotate_right(node: _IntervalNode[T]) -> _IntervalNode[T]:
        pivot = node.left
        assert pivot is not None  # guaranteed by the balance factor
        node.left = pivot.right
        pivot.right = node
        IntervalTree._refresh(node)
        IntervalTree._refresh(pivot)
        return pivot

    @staticmethod
    def _rotate_left(node: _IntervalNode[T]) -> _IntervalNode[T]:
        pivot = node.right
        assert pivot is not None  # guaranteed by the balance factor
        node.right = pivot.left
        pivot.left = node
        IntervalTree._refresh(node)
        IntervalTree._refresh(pivot)
        return pivot

    def _rebalance(self, node: _IntervalNode[T]) -> _IntervalNode[T]:
        factor = self._balance_factor(node)
        if factor > 1:
            left = node.left
            if left is not None and self._balance_factor(left) < 0:
                node.left = self._rotate_left(left)
            return self._rotate_right(node)
        if factor < -1:
            right = node.right
            if right is not None and self._balance_factor(right) > 0:
                node.right = self._rotate_right(right)
            return self._rotate_left(node)
        return node

    def _search(self, node: _IntervalNode[T] | None, start: str, end: str, hits: list[T]) -> None:
        """In-order walk with two prunings, so hits come sorted by start.

        Recursion depth is the tree height, O(log n) thanks to the AVL
        balancing — never proportional to the number of intervals.
        """
        if node is None:
            return
        # The left branch can only overlap if some interval there ends after
        # the query starts.
        if node.left is not None and node.left.max_end > start:
            self._search(node.left, start, end, hits)
        if node.start < end and start < node.end:
            hits.append(node.value)
        # The right branch holds starts >= this node's start: if this start is
        # already at or past the query end, none of them can overlap; and the
        # branch is dead when every interval there ends before the query starts.
        if node.right is not None and node.start < end and node.right.max_end > start:
            self._search(node.right, start, end, hits)

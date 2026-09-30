"""AVL tree keyed by pickup time, for the next-pickup lookup (FR-08)."""

from __future__ import annotations

from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class _AvlNode(Generic[K, V]):
    """One key of the tree's backing store.

    Holds a key, its value, pointers to both subtrees and the cached height of
    the subtree rooted here. No public methods.
    """

    __slots__ = ("key", "value", "left", "right", "height")

    def __init__(self, key: K, value: V) -> None:
        self.key: K = key
        self.value: V = value
        self.left: _AvlNode[K, V] | None = None
        self.right: _AvlNode[K, V] | None = None
        self.height: int = 1


def _height(node: _AvlNode[K, V] | None) -> int:
    return 0 if node is None else node.height


def _update_height(node: _AvlNode[K, V]) -> None:
    node.height = 1 + max(_height(node.left), _height(node.right))


def _balance_factor(node: _AvlNode[K, V]) -> int:
    return _height(node.left) - _height(node.right)


def _rotate_right(node: _AvlNode[K, V]) -> _AvlNode[K, V]:
    new_root = node.left
    node.left = new_root.right
    new_root.right = node
    _update_height(node)
    _update_height(new_root)
    return new_root


def _rotate_left(node: _AvlNode[K, V]) -> _AvlNode[K, V]:
    new_root = node.right
    node.right = new_root.left
    new_root.left = node
    _update_height(node)
    _update_height(new_root)
    return new_root


def _rebalance(node: _AvlNode[K, V]) -> _AvlNode[K, V]:
    _update_height(node)
    balance = _balance_factor(node)

    if balance > 1:
        if _balance_factor(node.left) < 0:
            node.left = _rotate_left(node.left)
        return _rotate_right(node)

    if balance < -1:
        if _balance_factor(node.right) > 0:
            node.right = _rotate_right(node.right)
        return _rotate_left(node)

    return node


class AvlTree(Generic[K, V]):
    """Height-balanced map from pickup time to the stop that is due (FR-08).

    Keys are ordered with ``<``, so numbers, strings and tuples of them all
    work. Inserting an existing key replaces its value and keeps the size.

    Backing store: nodes caching their own subtree height, rebalanced on the
    way back up every insert and remove.

    Big-O:
    - insert: O(log n)
    - find: O(log n)
    - min: O(log n)
    - remove: O(log n)
    - inorder_keys: O(n)
    - size: O(1)
    - height: O(1)
    """

    __slots__ = ("_root", "_count")

    def __init__(self) -> None:
        self._root: _AvlNode[K, V] | None = None
        self._count: int = 0

    def insert(self, key: K, value: V) -> None:
        """Store ``value`` under ``key``, replacing any previous value. O(log n)."""
        inserted = False

        def insert_node(node: _AvlNode[K, V] | None) -> _AvlNode[K, V]:
            nonlocal inserted
            if node is None:
                inserted = True
                return _AvlNode(key, value)
            if key < node.key:
                node.left = insert_node(node.left)
            elif key > node.key:
                node.right = insert_node(node.right)
            else:
                node.value = value
            return _rebalance(node)

        self._root = insert_node(self._root)
        if inserted:
            self._count += 1

    def find(self, key: K) -> V | None:
        """Return the value stored under ``key``, or None when absent. O(log n)."""
        current = self._root
        while current is not None:
            if key < current.key:
                current = current.left
            elif key > current.key:
                current = current.right
            else:
                return current.value
        return None

    def min(self) -> tuple[K, V]:
        """Return the ``(key, value)`` pair with the smallest key. O(log n).

        Raises IndexError when the tree is empty.
        """
        current = self._root
        if current is None:
            raise IndexError("Cannot read the minimum of an empty tree.")
        while current.left is not None:
            current = current.left
        return (current.key, current.value)

    def remove(self, key: K) -> None:
        """Remove ``key`` and rebalance; does nothing when absent. O(log n)."""
        removed = False

        def remove_node(node: _AvlNode[K, V] | None, target: K) -> _AvlNode[K, V] | None:
            nonlocal removed
            if node is None:
                return None

            if target < node.key:
                node.left = remove_node(node.left, target)
            elif target > node.key:
                node.right = remove_node(node.right, target)
            else:
                removed = True
                if node.left is None:
                    return node.right
                if node.right is None:
                    return node.left
                successor = node.right
                while successor.left is not None:
                    successor = successor.left
                node.key = successor.key
                node.value = successor.value
                node.right = remove_node(node.right, successor.key)

            return _rebalance(node)

        self._root = remove_node(self._root, key)
        if removed:
            self._count -= 1

    def size(self) -> int:
        """Return how many keys the tree holds. O(1)."""
        return self._count

    def height(self) -> int:
        """Return the height of the tree: 0 when empty, 1 for a single key. O(1)."""
        return _height(self._root)

    def inorder_keys(self) -> list[K]:
        """Return every key in ascending order. O(n)."""
        keys: list[K] = []

        def collect(node: _AvlNode[K, V] | None) -> None:
            if node is None:
                return
            collect(node.left)
            keys.append(node.key)
            collect(node.right)

        collect(self._root)
        return keys
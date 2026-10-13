"""Point quadtree for nearby-walker radius queries (FR-16, FR-23)."""

from __future__ import annotations

import math
from typing import Generic, TypeVar

K = TypeVar("K")


class _QuadtreeNode(Generic[K]):
    """One quadrant of the quadtree's backing store.

    Holds the rectangle for this quadrant, the points that live directly
    in this node when it is a leaf, and four children when it has split.
    No public methods.
    """

    __slots__ = (
        "min_east",
        "min_north",
        "max_east",
        "max_north",
        "depth",
        "points",
        "children",
    )

    def __init__(
        self,
        min_east: float,
        min_north: float,
        max_east: float,
        max_north: float,
        depth: int,
    ) -> None:
        self.min_east: float = min_east
        self.min_north: float = min_north
        self.max_east: float = max_east
        self.max_north: float = max_north
        self.depth: int = depth
        self.points: list[tuple[K, float, float]] = []
        self.children: list[_QuadtreeNode[K]] | None = None


class Quadtree(Generic[K]):
    """Point quadtree over planar east/north coordinates (FR-16, FR-23).

    The walk API projects lat/lng to east/north before indexing, so this
    structure works in metres on a flat plane.

    Backing store: a recursive quadrant tree capped at ``max_depth`` plus a
    side index ``dict[K, tuple[float, float]]`` that lets ``remove`` and
    move-on-reinsert locate a key without scanning the tree. The dict is a
    lookup aid, not the structure itself.

    Big-O:
    - insert: O(depth)
    - remove: O(depth + node_capacity)
    - query_radius: O(n) worst case, typically O(depth + k) where k is the
      number of reported points
    - size: O(1)
    """

    __slots__ = (
        "_min_east",
        "_min_north",
        "_max_east",
        "_max_north",
        "_node_capacity",
        "_max_depth",
        "_root",
        "_index",
        "_count",
    )

    def __init__(
        self,
        min_east: float,
        min_north: float,
        max_east: float,
        max_north: float,
        node_capacity: int = 4,
        max_depth: int = 12,
    ) -> None:
        self._min_east: float = min_east
        self._min_north: float = min_north
        self._max_east: float = max_east
        self._max_north: float = max_north
        self._node_capacity: int = node_capacity
        self._max_depth: int = max_depth
        self._root: _QuadtreeNode[K] = _QuadtreeNode(
            min_east, min_north, max_east, max_north, 0
        )
        self._index: dict[K, tuple[float, float]] = {}
        self._count: int = 0

    # -- public -----------------------------------------------------------

    def insert(self, key: K, east: float, north: float) -> None:
        """Insert ``key`` at ``east``/``north``, moving it if already present. O(depth).

        Bounds are inclusive on both the min and the max edge.

        Raises:
            ValueError: If the point lies outside the tree bounds.
        """
        if east < self._min_east or east > self._max_east or north < self._min_north or north > self._max_north:
            raise ValueError("Point lies outside quadtree bounds.")
        if key in self._index:
            previous_east, previous_north = self._index[key]
            if previous_east == east and previous_north == north:
                return
            self._remove_point(self._root, key, previous_east, previous_north)
            self._insert_point(self._root, key, east, north)
            self._index[key] = (east, north)
            return
        self._insert_point(self._root, key, east, north)
        self._index[key] = (east, north)
        self._count += 1

    def remove(self, key: K) -> bool:
        """Remove ``key`` from the tree. O(depth + node_capacity).

        Returns:
            True when the key was present, False when it was unknown.
        """
        if key not in self._index:
            return False
        stored_east, stored_north = self._index.pop(key)
        self._remove_point(self._root, key, stored_east, stored_north)
        self._count -= 1
        return True

    def query_radius(self, east: float, north: float, radius: float) -> list[K]:
        """Return every key within ``radius`` of ``east``/``north``, nearest first. O(n) worst case, typically O(depth + k).

        Points at distance exactly ``radius`` are included. Whole quadrants
        whose rectangle does not intersect the query circle are pruned.

        Raises:
            ValueError: If ``radius`` is negative.
        """
        if radius < 0:
            raise ValueError("Radius must be non-negative.")
        candidates: list[tuple[float, K]] = []
        radius_squared = radius * radius
        self._query_radius(self._root, east, north, radius, radius_squared, candidates)
        candidates.sort(key=lambda pair: pair[0])
        return [found_key for _, found_key in candidates]

    def size(self) -> int:
        """Return how many points the tree holds. O(1)."""
        return self._count

    # -- internals --------------------------------------------------------

    def _insert_point(self, node: _QuadtreeNode[K], key: K, east: float, north: float) -> None:
        if node.children is None:
            node.points.append((key, east, north))
            if len(node.points) > self._node_capacity and node.depth < self._max_depth:
                self._subdivide(node)
            return
        child = self._choose_child(node, east, north)
        self._insert_point(child, key, east, north)

    def _subdivide(self, node: _QuadtreeNode[K]) -> None:
        mid_east = (node.min_east + node.max_east) / 2.0
        mid_north = (node.min_north + node.max_north) / 2.0
        next_depth = node.depth + 1
        node.children = [
            _QuadtreeNode(node.min_east, node.min_north, mid_east, mid_north, next_depth),
            _QuadtreeNode(mid_east, node.min_north, node.max_east, mid_north, next_depth),
            _QuadtreeNode(node.min_east, mid_north, mid_east, node.max_north, next_depth),
            _QuadtreeNode(mid_east, mid_north, node.max_east, node.max_north, next_depth),
        ]
        previous_points = node.points
        node.points = []
        for point_key, point_east, point_north in previous_points:
            child = self._choose_child(node, point_east, point_north)
            self._insert_point(child, point_key, point_east, point_north)

    def _choose_child(self, node: _QuadtreeNode[K], east: float, north: float) -> _QuadtreeNode[K]:
        assert node.children is not None
        mid_east = (node.min_east + node.max_east) / 2.0
        mid_north = (node.min_north + node.max_north) / 2.0
        west = east <= mid_east
        south = north <= mid_north
        if west and south:
            return node.children[0]
        if not west and south:
            return node.children[1]
        if west and not south:
            return node.children[2]
        return node.children[3]

    def _remove_point(self, node: _QuadtreeNode[K], key: K, east: float, north: float) -> bool:
        if node.children is None:
            for index, (stored_key, _, _) in enumerate(node.points):
                if stored_key == key:
                    node.points.pop(index)
                    return True
            return False
        child = self._choose_child(node, east, north)
        return self._remove_point(child, key, east, north)

    def _query_radius(
        self,
        node: _QuadtreeNode[K],
        east: float,
        north: float,
        radius: float,
        radius_squared: float,
        candidates: list[tuple[float, K]],
    ) -> None:
        if not self._rect_intersects_circle(node, east, north, radius_squared):
            return
        if node.children is None:
            for stored_key, point_east, point_north in node.points:
                delta_east = point_east - east
                delta_north = point_north - north
                distance_squared = delta_east * delta_east + delta_north * delta_north
                if distance_squared <= radius_squared:
                    distance = math.sqrt(distance_squared)
                    candidates.append((distance, stored_key))
            return
        for child in node.children:
            self._query_radius(child, east, north, radius, radius_squared, candidates)

    @staticmethod
    def _rect_intersects_circle(node: _QuadtreeNode[K], east: float, north: float, radius_squared: float) -> bool:
        closest_east = node.min_east if east < node.min_east else (node.max_east if east > node.max_east else east)
        closest_north = node.min_north if north < node.min_north else (node.max_north if north > node.max_north else north)
        delta_east = east - closest_east
        delta_north = north - closest_north
        return delta_east * delta_east + delta_north * delta_north <= radius_squared

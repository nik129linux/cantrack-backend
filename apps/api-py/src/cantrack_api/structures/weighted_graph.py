"""Weighted graph of walk legs, used to route pickups (FR-09)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")

_INFINITY = float("inf")


class _Edge(Generic[T]):
    """One directed leg of the graph's backing store.

    Holds the destination node and the cost of walking to it. No public
    methods.
    """

    __slots__ = ("to", "weight")

    def __init__(self, to: T, weight: float) -> None:
        self.to: T = to
        self.weight: float = weight


@dataclass(frozen=True)
class ShortestPath(Generic[T]):
    """The cheapest walk between two nodes of a ``WeightedGraph``.

    ``distance`` is the total cost of ``path``, and ``path`` runs from the
    start node to the target node inclusive.
    """

    distance: float
    path: list[T]


class WeightedGraph(Generic[T]):
    """Directed weighted graph of walk legs between pickup stops (FR-09).

    Backing store: adjacency list mapping each known node to the legs leaving
    it. Edges point one way only, and costs must be finite and non-negative
    (zero is fine), so Dijkstra stays correct.

    Big-O:
    - add_node: O(1) average
    - add_edge: O(1) average
    - shortest_path: O(V^2 + E)
    """

    __slots__ = ("_adjacency",)

    def __init__(self) -> None:
        self._adjacency: dict[T, list[_Edge[T]]] = {}

    def add_node(self, node: T) -> None:
        """Register ``node`` with no outgoing legs. O(1) average."""
        if node not in self._adjacency:
            self._adjacency[node] = []

    def add_edge(self, from_node: T, to_node: T, weight: float) -> None:
        """Add a directed leg from ``from_node`` to ``to_node``. O(1) average.

        ``weight`` must be a finite non-negative number. Raises ValueError on a
        negative, infinite or NaN weight.
        """
        if not 0 <= weight < _INFINITY:
            raise ValueError("Edge weight must be a finite non-negative number.")

        self.add_node(from_node)
        self.add_node(to_node)
        self._adjacency[from_node].append(_Edge(to_node, weight))

    def shortest_path(self, start: T, target: T) -> ShortestPath[T]:
        """Return the cheapest walk from ``start`` to ``target``. O(V^2 + E).

        Raises ValueError when either node is unknown or when the target is
        unreachable from the start.
        """
        if start not in self._adjacency or target not in self._adjacency:
            raise ValueError("Cannot find a path between unknown nodes.")

        distances: dict[T, float] = {start: 0.0}
        previous: dict[T, T] = {}
        unvisited: set[T] = set(self._adjacency.keys())

        while unvisited:
            current: T | None = None
            current_distance = _INFINITY
            for node in unvisited:
                known = distances.get(node)
                if known is None:
                    continue
                if known < current_distance:
                    current = node
                    current_distance = known
            if current is None:
                break

            unvisited.discard(current)
            if current == target:
                path = [target]
                cursor = target
                while cursor != start:
                    predecessor = previous.get(cursor)
                    if predecessor is None:
                        raise ValueError("Cannot reconstruct the shortest path.")
                    cursor = predecessor
                    path.append(cursor)
                path.reverse()
                return ShortestPath(distance=current_distance, path=path)

            for edge in self._adjacency[current]:
                if edge.to not in unvisited:
                    continue
                candidate = current_distance + edge.weight
                known = distances.get(edge.to)
                if known is None or candidate < known:
                    distances[edge.to] = candidate
                    previous[edge.to] = current

        raise ValueError("No path exists between the requested nodes.")
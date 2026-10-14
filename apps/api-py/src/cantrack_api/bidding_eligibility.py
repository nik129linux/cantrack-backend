"""Geo projection and busy-walker eligibility for walk bidding (FR-25).

Pure helpers over the Plan 1 structures: equirectangular projection, free
window checks, pack compatibility, detour cost and the combined busy-walker
eligibility rule, plus the nearby-walker radius query. No I/O, no clock.

Big-O (n = intervals, d = current dogs, V/E = graph nodes/edges, m = indexed walkers):
- project_to_metres: O(1)
- is_free_in_window: O(log n + k)
- is_pack_compatible: O(d·alpha(n))
- detour_minutes: O(V^2 + E)
- is_busy_eligible: max of the three above
- nearby_walker_ids: O(m) worst case
"""

from __future__ import annotations

import math

from cantrack_api.structures import IntervalTree, Quadtree, UnionFind, WeightedGraph

WALKING_SPEED_KMH = 5.0

_EARTH_RADIUS_M = 6_371_000.0


def project_to_metres(lat: float, lng: float, ref_lat: float) -> tuple[float, float]:
    """Project ``lat``/``lng`` to planar east/north metres. O(1).

    Equirectangular projection: ``east = R·cos(ref_lat)·rad(lng)`` and
    ``north = R·rad(lat)``, with ``ref_lat`` the reference latitude.
    """
    east = _EARTH_RADIUS_M * math.cos(math.radians(ref_lat)) * math.radians(lng)
    north = _EARTH_RADIUS_M * math.radians(lat)
    return east, north


def is_free_in_window(busy_windows: IntervalTree[str], window_start: str, window_end: str) -> bool:
    """Whether ``[window_start, window_end)`` overlaps no busy interval. O(log n + k).

    Touching windows (one ends where the other starts) do not overlap.
    """
    return busy_windows.search(window_start, window_end) == []


def is_pack_compatible(pack: UnionFind[str], new_dog_id: str, current_dog_ids: list[str]) -> bool:
    """Whether ``new_dog_id`` shares a pack with every current dog. O(d·alpha(n)).

    Vacuously true when the route holds no dogs yet.
    """
    return all(pack.connected(new_dog_id, dog_id) for dog_id in current_dog_ids)


def detour_minutes(street_graph: WeightedGraph[str], route_stop: str, pickup_stop: str) -> float | None:
    """Cheapest detour from ``route_stop`` to ``pickup_stop`` in minutes. O(V^2 + E).

    Returns None when either stop is unknown or the pickup is unreachable.
    """
    try:
        return street_graph.shortest_path(route_stop, pickup_stop).distance
    except ValueError:
        return None


def is_busy_eligible(
    *,
    pack: UnionFind[str],
    new_dog_id: str,
    current_dog_ids: list[str],
    street_graph: WeightedGraph[str],
    route_stop: str,
    pickup_stop: str,
    detour_limit_min: float,
    dog_count: int,
    capacity: int,
) -> bool:
    """Whether a busy walker may take one more dog. O(V^2 + E).

    Needs all three: pack-compatible with every current dog, a detour within
    ``detour_limit_min`` (unknown/unreachable counts as not eligible), and
    room for one more dog under ``capacity``.
    """
    if not is_pack_compatible(pack, new_dog_id, current_dog_ids):
        return False
    detour = detour_minutes(street_graph, route_stop, pickup_stop)
    if detour is None or detour > detour_limit_min:
        return False
    return dog_count + 1 <= capacity


def nearby_walker_ids(nearby_walkers: Quadtree[str], pickup_lat: float, pickup_lng: float, radius_km: float) -> list[str]:
    """Return the walker ids within ``radius_km`` of the pickup, nearest first. O(m) worst case.

    The pickup is projected with ``pickup_lat`` as the reference latitude,
    matching the projection used when the walkers were indexed.
    """
    query_east, query_north = project_to_metres(pickup_lat, pickup_lng, pickup_lat)
    return nearby_walkers.query_radius(query_east, query_north, radius_km * 1000.0)

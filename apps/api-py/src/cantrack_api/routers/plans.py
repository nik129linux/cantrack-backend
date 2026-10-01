"""S2: ``POST /plans/suggest`` — the walker's pickup plan for one local day.

The plan is a SUGGESTION: it is computed per request and never persisted
("Accept plan" on the web calls the existing ``POST /routes``). Contract
(pinned by tests/py/api/test_plans.py):

- only the caller's ``accepted`` requests whose UTC-normalized requested time
  falls inside the local day ``[date 00:00, date+1 00:00)`` shifted by
  ``utcOffsetMinutes`` are planned;
- ordering: stops sorted by requested time go into an ``AvlTree`` keyed by
  ``(requested_time, id)`` — the earliest is the next due stop. Clusters use
  the ANCHOR rule: a stop joins the current cluster only while it is within
  ``CLUSTER_WINDOW_MINUTES`` (15, inclusive) of the cluster's FIRST stop.
  Inside a cluster the order is nearest neighbour (haversine from the previous
  stop, ``WALKING_SPEED_KMH`` = 5), ties by earlier requested time then id;
- ``eta``: the first stop's eta is its requested time; each next one is
  ``max(requested, previous eta + leg / 5 km/h)`` truncated to whole seconds.
  ``lateMinutes`` = ceil(max(0, eta - requested)) in minutes,
  ``late`` = lateMinutes > ``LATE_THRESHOLD_MINUTES`` (10) and top-level
  ``feasible`` = no late stop — an impossible schedule is never silent;
- ``legDistanceKm`` = round(haversine, 2) (0.0 for the first stop),
  ``totalDistanceKm`` = round(sum of RAW legs, 2);
- groups: inside each cluster a ``reactive`` dog is always alone; two other
  dogs are compatible within ``GROUP_RADIUS_KM`` (1.5, inclusive) — UnionFind
  unions every compatible pair, so components connect transitively — and each
  component is chunked into consecutive runs of at most ``MAX_GROUP_SIZE`` (4)
  in plan order. Group numbers are consecutive integers in order of first
  appearance along the whole plan; clusters never share a group.
"""

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client
from supabase_auth.types import User

from ..db import run_query
from ..deps import get_current_user, get_supabase
from ..geo import haversine_km
from ..schemas import SuggestPlanBody
from ..structures import AvlTree, UnionFind

router = APIRouter(prefix="/plans")

REQUESTS_TABLE = "walk_requests"
DOGS_TABLE = "dogs"

ROLE_MESSAGE = "Only walkers can suggest a plan."
DATE_MESSAGE = "date must be a valid YYYY-MM-DD date."

REQUEST_COLUMNS = (
    "id,owner_id,walker_id,dog_id,status,requested_time,"
    "pickup_lat,pickup_lng,price_cop,created_at,responded_at"
)

#: A stop joins the current cluster while within this many minutes (inclusive)
#: of the cluster's FIRST stop (anchor rule; a chain rule would let a whole
#: morning collapse into one reorderable cluster).
CLUSTER_WINDOW_MINUTES = 15

#: Walking speed used for legs and ETAs (spec S2).
WALKING_SPEED_KMH = 5.0

#: A stop more than this many minutes past its requested time is "late".
LATE_THRESHOLD_MINUTES = 10

#: Two non-reactive dogs may share a group when their pins are within this
#: distance (inclusive); components are then capped at MAX_GROUP_SIZE.
GROUP_RADIUS_KM = 1.5

#: No group walks more than this many dogs at once (spec S2).
MAX_GROUP_SIZE = 4


def _role(user: User) -> str | None:
    metadata = getattr(user, "user_metadata", None) or {}
    return metadata.get("role")


def _parse_utc(value: str) -> datetime:
    """Parse a stored ISO-8601 timestamp as an aware UTC datetime."""
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _day_window(date: str, offset_minutes: int) -> tuple[datetime, datetime]:
    """The UTC half-open window ``[date 00:00, date+1 00:00)`` in local time.

    Raises:
        HTTPException: 400 with the exact DATE_MESSAGE when ``date`` is not a
            real YYYY-MM-DD calendar date.
    """
    try:
        local_midnight = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=DATE_MESSAGE
        ) from None
    local_tz = timezone(timedelta(minutes=offset_minutes))
    start = local_midnight.replace(tzinfo=local_tz).astimezone(timezone.utc)
    return start, start + timedelta(days=1)


def _cluster(in_time_order: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split time-ordered stops into clusters with the 15-minute ANCHOR rule."""
    clusters: list[list[dict[str, Any]]] = []
    for stop in in_time_order:
        when = _parse_utc(stop["requested_time"])
        if clusters:
            anchor = _parse_utc(clusters[-1][0]["requested_time"])
            if when - anchor <= timedelta(minutes=CLUSTER_WINDOW_MINUTES):
                clusters[-1].append(stop)
                continue
        clusters.append([stop])
    return clusters


def _nearest_neighbour_order(cluster: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order one cluster: start at the earliest stop, then always the nearest
    unvisited stop (haversine), ties by earlier requested time then id."""
    remaining = list(cluster)  # already in (requested_time, id) order
    ordered = [remaining.pop(0)]
    while remaining:
        current = ordered[-1]
        best_index = min(
            range(len(remaining)),
            key=lambda index: (
                haversine_km(
                    float(current["pickup_lat"]),
                    float(current["pickup_lng"]),
                    float(remaining[index]["pickup_lat"]),
                    float(remaining[index]["pickup_lng"]),
                ),
                remaining[index]["requested_time"],
                remaining[index]["id"],
            ),
        )
        ordered.append(remaining.pop(best_index))
    return ordered


def _assign_groups(
    clusters_ordered: list[list[dict[str, Any]]],
    reactive_ids: set[str],
) -> dict[str, int]:
    """Group numbers per request id, consecutive by first appearance.

    Inside each cluster: reactive dogs are alone; the others are unioned in a
    UnionFind for every compatible pair (within GROUP_RADIUS_KM, transitive),
    and each component is chunked into consecutive runs of MAX_GROUP_SIZE in
    plan order.
    """
    slot_of: dict[str, tuple[Any, ...]] = {}
    for cluster in clusters_ordered:
        member_ids = [stop["id"] for stop in cluster]

        union_find: UnionFind[str] = UnionFind()
        for stop_id in member_ids:
            if stop_id not in reactive_ids:
                union_find.add(stop_id)

        by_id = {stop["id"]: stop for stop in cluster}
        social = [stop_id for stop_id in member_ids if stop_id not in reactive_ids]
        for i, first in enumerate(social):
            for second in social[i + 1 :]:
                distance = haversine_km(
                    float(by_id[first]["pickup_lat"]),
                    float(by_id[first]["pickup_lng"]),
                    float(by_id[second]["pickup_lat"]),
                    float(by_id[second]["pickup_lng"]),
                )
                if distance <= GROUP_RADIUS_KM:
                    union_find.union(first, second)

        # Components keep plan order, then chunk into runs of at most 4.
        component_order: dict[str, list[str]] = {}
        for stop_id in social:
            component_order.setdefault(union_find.find(stop_id), []).append(stop_id)
        for stop_id in member_ids:
            if stop_id in reactive_ids:
                slot_of[stop_id] = ("solo", stop_id)
            else:
                members = component_order[union_find.find(stop_id)]
                chunk = members.index(stop_id) // MAX_GROUP_SIZE
                slot_of[stop_id] = ("pack", union_find.find(stop_id), chunk)

    groups: dict[str, int] = {}
    for cluster in clusters_ordered:
        for stop in cluster:
            slot = slot_of[stop["id"]]
            if slot not in groups:
                groups[slot] = len(groups) + 1
    return {stop_id: groups[slot] for stop_id, slot in slot_of.items()}


@router.post("/suggest")
def suggest_plan(
    body: SuggestPlanBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Suggest the pickup order for one local day of accepted requests.

    Args:
        body: ``{"date": "YYYY-MM-DD", "utcOffsetMinutes": int = 0}``.
        user: The authenticated walker.
        supabase: The Supabase client (dependency-injected, faked in tests).

    Returns:
        ``{"date", "stops", "totalDistanceKm", "feasible"}`` — the suggestion,
        not persisted anywhere.

    Raises:
        HTTPException: 400 for a non-walker caller or an invalid date, 500 on
            database errors.
    """
    if _role(user) != "walker":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ROLE_MESSAGE)

    window_start, window_end = _day_window(body.date, body.utcOffsetMinutes)

    rows = run_query(
        supabase.table(REQUESTS_TABLE)
        .select(REQUEST_COLUMNS)
        .eq("walker_id", user.id)
        .eq("status", "accepted")
    )
    day_rows = [
        row
        for row in rows
        if window_start <= _parse_utc(row["requested_time"]) < window_end
    ]

    # The hand-written AVL keyed by requested time gives the next due stop and
    # the chronological order; the id in the key keeps equal times distinct.
    tree: AvlTree[tuple[str, str], dict[str, Any]] = AvlTree()
    for row in day_rows:
        tree.insert((row["requested_time"], row["id"]), row)
    in_time_order = [tree.find(key) for key in tree.inorder_keys()]

    clusters = [_nearest_neighbour_order(cluster) for cluster in _cluster(in_time_order)]

    dog_ids = sorted({row["dog_id"] for row in day_rows})
    dogs: dict[str, dict[str, Any]] = {}
    if dog_ids:
        dog_rows = run_query(
            supabase.table(DOGS_TABLE).select("id,name,profile").in_("id", dog_ids)
        )
        dogs = {dog["id"]: dog for dog in dog_rows}

    def is_reactive(row: dict[str, Any]) -> bool:
        profile = dogs.get(row["dog_id"], {}).get("profile") or {}
        return profile.get("temperament") == "reactive"

    groups = _assign_groups(clusters, {row["id"] for row in day_rows if is_reactive(row)})

    stops: list[dict[str, Any]] = []
    raw_legs: list[float] = []
    previous_eta: datetime | None = None
    previous_pin: tuple[float, float] | None = None
    for cluster in clusters:
        for row in cluster:
            pin = (float(row["pickup_lat"]), float(row["pickup_lng"]))
            requested = _parse_utc(row["requested_time"])

            if previous_pin is None or previous_eta is None:
                raw_leg = 0.0
                eta = requested
            else:
                raw_leg = haversine_km(previous_pin[0], previous_pin[1], pin[0], pin[1])
                walking = timedelta(hours=raw_leg / WALKING_SPEED_KMH)
                eta = max(requested, previous_eta + walking)
            eta = eta.replace(microsecond=0)

            late_seconds = max(0.0, (eta - requested).total_seconds())
            late_minutes = int(math.ceil(late_seconds / 60.0)) if late_seconds else 0

            raw_legs.append(raw_leg)
            stops.append(
                {
                    "requestId": row["id"],
                    "dogId": row["dog_id"],
                    "dogName": dogs.get(row["dog_id"], {}).get("name"),
                    "requestedTime": row["requested_time"],
                    "eta": eta.isoformat(),
                    "legDistanceKm": 0.0 if previous_pin is None else round(raw_leg, 2),
                    "flags": ["reactive"] if is_reactive(row) else [],
                    "group": groups[row["id"]],
                    "lateMinutes": late_minutes,
                    "late": late_minutes > LATE_THRESHOLD_MINUTES,
                }
            )
            previous_eta = eta
            previous_pin = pin

    return {
        "date": body.date,
        "stops": stops,
        "totalDistanceKm": round(math.fsum(raw_legs), 2),
        "feasible": not any(stop["late"] for stop in stops),
    }

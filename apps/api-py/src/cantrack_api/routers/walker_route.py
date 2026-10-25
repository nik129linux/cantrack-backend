"""W4 Rutas: today's won walks as one route.

The day's stops are the caller's ``assigned``, ``in_walk`` and ``done`` walks
whose pickup falls on one local Bogota date, ordered by pickup time through the
hand-written AVL tree. Each leg's minutes come from Dijkstra over
``walk_graph_edges`` when those edges connect the two pickup stops, and from the
straight-line walking speed otherwise; the kilometres are always the haversine
distance.
"""

import re
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from supabase import Client
from supabase_auth.types import User

from ..app import limiter
from ..db import run_query
from ..deps import get_now, get_supabase
from ..geo import haversine_km
from ..structures import AvlTree, WeightedGraph
from ..walker_access import walker_from_path

router = APIRouter()

REQUESTS_TABLE = "walk_requests"
DOGS_TABLE = "dogs"
POSITIONS_TABLE = "walker_positions"
GRAPH_EDGES_TABLE = "walk_graph_edges"

#: The statuses of a walk that belongs on the day's route.
ROUTE_STATUSES = ["assigned", "in_walk", "done"]

#: Bogota is a fixed UTC-5 offset (Colombia has no daylight saving time).
BOGOTA_TZ = timezone(timedelta(hours=-5))

#: Walking speed used when the graph does not connect two stops, in km/h.
STRAIGHT_SPEED_KMH = 4.5

BAD_DATE_MESSAGE = "date must be a YYYY-MM-DD day."

_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


class StartView(BaseModel):
    """The walker's current position, the start of the first leg."""

    lat: float
    lng: float


class StopView(BaseModel):
    """One pickup on the day's route, with its leg from the previous point."""

    requestId: str
    dogName: str | None
    status: str
    requestedTime: str
    pickupLat: float | None
    pickupLng: float | None
    legKm: float
    legMinutes: int
    legSource: str


class RouteView(BaseModel):
    """The walker's whole route for one local day."""

    date: str
    start: StartView | None
    stops: list[StopView]
    totalKm: float
    totalMinutes: int
    nextStopId: str | None


def _parse_day(value: str) -> str:
    """Validate a ``YYYY-MM-DD`` day and return it unchanged. O(1).

    Raises:
        HTTPException: 400 when the value is not a real calendar day in that
            exact format.
    """
    if _DATE_PATTERN.fullmatch(value) is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_DATE_MESSAGE
        )
    try:
        date_type.fromisoformat(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_DATE_MESSAGE
        ) from None
    return value


def _parse_stored_time(value: str) -> datetime:
    """Parse a stored timestamp as an aware UTC datetime. O(1).

    A missing or unparseable value falls back to the epoch, and a naive value is
    assumed to be UTC, so one bad row never breaks the whole route.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _local_day(value: str) -> str:
    """Return the Bogota calendar day of a stored timestamp. O(1)."""
    return _parse_stored_time(value).astimezone(BOGOTA_TZ).date().isoformat()


def _dogs_by_id(supabase: Client, dog_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Load dogs by id in one batched query. O(n)."""
    unique = sorted({dog_id for dog_id in dog_ids if dog_id is not None})
    if not unique:
        return {}
    rows = run_query(supabase.table(DOGS_TABLE).select("id,name").in_("id", unique))
    return {row["id"]: row for row in rows}


def _street_graph(supabase: Client) -> WeightedGraph[str]:
    """Build the walk-leg graph from every ``walk_graph_edges`` row. O(E)."""
    graph: WeightedGraph[str] = WeightedGraph()
    for edge in run_query(supabase.table(GRAPH_EDGES_TABLE).select("*")):
        graph.add_edge(edge["from_stop"], edge["to_stop"], edge["minutes"])
    return graph


def _leg(
    graph: WeightedGraph[str],
    previous: dict[str, Any] | None,
    stop: dict[str, Any],
    start: StartView | None,
) -> tuple[float, int, str]:
    """Return ``(km, minutes, source)`` for the leg arriving at ``stop``. O(V^2+E).

    The first stop starts at the walker's stored position, or at nothing (a
    zero leg whose source is ``start``). Every other leg goes from the previous
    stop's ``pickup:<id>`` node to this stop's, and Dijkstra's minutes win when
    the graph connects them. The kilometres are always the straight line.
    """
    stop_lat = stop.get("pickup_lat")
    stop_lng = stop.get("pickup_lng")

    if previous is None:
        if start is None or stop_lat is None or stop_lng is None:
            return 0.0, 0, "start"
        km = haversine_km(start.lat, start.lng, stop_lat, stop_lng)
        return round(km, 2), int(round(km / STRAIGHT_SPEED_KMH * 60)), "straight"

    previous_lat = previous.get("pickup_lat")
    previous_lng = previous.get("pickup_lng")
    if previous_lat is None or previous_lng is None or stop_lat is None or stop_lng is None:
        km = 0.0
    else:
        km = haversine_km(previous_lat, previous_lng, stop_lat, stop_lng)

    try:
        shortest = graph.shortest_path(
            f"pickup:{previous['id']}", f"pickup:{stop['id']}"
        )
    except ValueError:
        return round(km, 2), int(round(km / STRAIGHT_SPEED_KMH * 60)), "straight"
    return round(km, 2), int(round(shortest.distance)), "graph"


@router.get("/walkers/{walkerId}/route", response_model=RouteView)
@limiter.limit("30/minute")
def get_day_route(
    request: Request,
    date: str | None = Query(default=None),
    walker: User = Depends(walker_from_path),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Return the calling walker's route for one local Bogota day.

    Stops are the walker's ``assigned``, ``in_walk`` and ``done`` walks whose
    ``requested_time`` falls on ``date``, sorted by pickup time through an AVL
    tree. The start is the walker's stored ``walker_positions`` row, when there
    is one, and each stop's leg is measured from that start (the first stop) or
    the previous stop. Dog names come from one batched query.

    Args:
        request: The incoming request, read by the rate limiter.
        date: The local day as ``YYYY-MM-DD``; defaults to today in Bogota.
        walker: The authenticated walker named in the path.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        ``{date, start, stops, totalKm, totalMinutes, nextStopId}``.

    Raises:
        HTTPException: 400 for a malformed or impossible date, 403 for a
            caller who is not the walker in the path, 500 on database errors.

    Big-O: O(w log w + (V^2+E) + d), w the walker's walks of the day, V and E
    the walk graph's nodes and edges, and d the dogs in the batched lookup.
    """
    day = (
        _parse_day(date)
        if date is not None
        else now.astimezone(BOGOTA_TZ).date().isoformat()
    )

    rows = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .eq("walker_id", walker.id)
        .in_("status", ROUTE_STATUSES)
    )
    todays = [row for row in rows if _local_day(row["requested_time"]) == day]

    tree: AvlTree[tuple[datetime, str], dict[str, Any]] = AvlTree()
    for row in todays:
        tree.insert((_parse_stored_time(row["requested_time"]), row["id"]), row)
    ordered = [tree.find(key) for key in tree.inorder_keys()]

    dogs = _dogs_by_id(supabase, [row.get("dog_id") for row in ordered])
    graph = _street_graph(supabase)

    position_rows = run_query(
        supabase.table(POSITIONS_TABLE).select("*").eq("walker_id", walker.id)
    )
    start = None
    if position_rows:
        start = StartView(
            lat=position_rows[0]["lat"], lng=position_rows[0]["lng"]
        )

    stops: list[dict[str, Any]] = []
    previous = None
    for row in ordered:
        if row is None:
            continue
        leg_km, leg_minutes, leg_source = _leg(graph, previous, row, start)
        dog = dogs.get(row.get("dog_id")) or {}
        stops.append(
            {
                "requestId": row["id"],
                "dogName": dog.get("name"),
                "status": row["status"],
                "requestedTime": row["requested_time"],
                "pickupLat": row.get("pickup_lat"),
                "pickupLng": row.get("pickup_lng"),
                "legKm": leg_km,
                "legMinutes": leg_minutes,
                "legSource": leg_source,
            }
        )
        previous = row

    next_stop = next((stop for stop in stops if stop["status"] != "done"), None)
    return {
        "date": day,
        "start": start,
        "stops": stops,
        "totalKm": round(sum(stop["legKm"] for stop in stops), 2),
        "totalMinutes": sum(stop["legMinutes"] for stop in stops),
        "nextStopId": next_stop["requestId"] if next_stop is not None else None,
    }

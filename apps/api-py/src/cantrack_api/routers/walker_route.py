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
from ..navigation import (
    cache_get,
    cache_key,
    cache_put,
    get_route_source,
    inside_pasto,
    parse_route,
)
from ..structures import AvlTree, WeightedGraph
from ..walker_access import walker_from_path

router = APIRouter()

REQUESTS_TABLE = "walk_requests"
DOGS_TABLE = "dogs"
POSITIONS_TABLE = "walker_positions"
GRAPH_EDGES_TABLE = "walk_graph_edges"
PROFILES_TABLE = "owner_profiles"
OFFERS_TABLE = "walk_offers"
TIMELINE_TABLE = "walk_timeline"

#: Simulated commission, in percent (the same one Inicio and Ganancias use).
COMMISSION_PCT = 10

#: The owner's phone is only shared while the walk is still to happen or happening.
LIVE_STATUSES = {"assigned", "in_walk"}

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
    durationHours: float
    netCop: int
    ownerName: str | None
    ownerPhone: str | None


class OverdueView(BaseModel):
    """An assigned walk whose slot is already over, from any day."""

    requestId: str
    dogName: str | None
    requestedTime: str
    durationHours: float
    pickupLat: float | None
    pickupLng: float | None


class NavStepView(BaseModel):
    """One turn of the way to a pickup."""

    text: str
    distanceM: int
    #: Where this step begins on ``geometry``.
    startIndex: int


class WalkingRouteView(BaseModel):
    """The way to a pickup: the line to draw and the turns to follow."""

    #: ``osrm`` when the streets were routed, ``straight`` for the direct line.
    source: str
    distanceKm: float
    minutes: int
    #: ``[[lat, lng], ...]`` along the streets.
    geometry: list[list[float]]
    steps: list[NavStepView]


class ActiveView(BaseModel):
    """The walk in progress right now, from any day, with its check-in time."""

    requestId: str
    dogName: str | None
    requestedTime: str
    durationHours: float
    startedAt: str | None
    netCop: int
    ownerName: str | None
    ownerPhone: str | None
    pickupLat: float | None
    pickupLng: float | None


class RouteView(BaseModel):
    """The walker's whole route for one local day."""

    date: str
    start: StartView | None
    stops: list[StopView]
    totalKm: float
    totalMinutes: int
    nextStopId: str | None
    overdue: list[OverdueView]
    active: ActiveView | None


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


def _owners_by_id(supabase: Client, owner_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Load the owners' names and phones in one batched query. O(n)."""
    unique = sorted({owner_id for owner_id in owner_ids if owner_id is not None})
    if not unique:
        return {}
    rows = run_query(
        supabase.table(PROFILES_TABLE)
        .select("owner_id,display_name,phone")
        .in_("owner_id", unique)
    )
    return {row["owner_id"]: row for row in rows}


def _offer_prices(supabase: Client, walker_id: str, walk_ids: list[str]) -> dict[str, int]:
    """Map walk id to the caller's own offer price, in one query. O(n)."""
    if not walk_ids:
        return {}
    rows = run_query(
        supabase.table(OFFERS_TABLE)
        .select("walk_request_id,price_cop")
        .eq("walker_id", walker_id)
        .in_("walk_request_id", sorted(set(walk_ids)))
    )
    prices: dict[str, int] = {}
    for row in rows:
        price = row.get("price_cop")
        if isinstance(price, int):
            prices.setdefault(row["walk_request_id"], price)
    return prices


def _net(row: dict[str, Any], prices: dict[str, int]) -> int:
    """What the walker keeps from a walk: the agreed price minus commission. O(1)."""
    gross = prices.get(row["id"])
    if gross is None:
        fare = row.get("price_cop")
        gross = fare if isinstance(fare, int) else 0
    return gross - gross * COMMISSION_PCT // 100


def _hours(row: dict[str, Any]) -> float:
    """The walk's length in hours, 0 when missing. O(1)."""
    value = row.get("duration_hours")
    return float(value) if isinstance(value, (int, float)) else 0.0


def _is_overdue(row: dict[str, Any], now: datetime) -> bool:
    """Whether an assigned walk's whole slot already ended. O(1)."""
    if row.get("status") != "assigned" or not row.get("requested_time"):
        return False
    start = _parse_stored_time(row["requested_time"])
    return start + timedelta(hours=_hours(row)) < now


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
    late = sorted(
        (row for row in rows if _is_overdue(row, now)),
        key=lambda row: (_parse_stored_time(row["requested_time"]), row["id"]),
    )

    walking = max(
        (row for row in rows if row["status"] == "in_walk"),
        key=lambda row: (_parse_stored_time(row["requested_time"]), row["id"]),
        default=None,
    )

    tree: AvlTree[tuple[datetime, str], dict[str, Any]] = AvlTree()
    for row in todays:
        tree.insert((_parse_stored_time(row["requested_time"]), row["id"]), row)
    ordered = [row for row in (tree.find(key) for key in tree.inorder_keys()) if row is not None]
    extra = [walking] if walking is not None else []

    dogs = _dogs_by_id(supabase, [row.get("dog_id") for row in [*ordered, *late, *extra]])
    owners = _owners_by_id(
        supabase,
        [row.get("owner_id") for row in [*ordered, *extra] if row["status"] in LIVE_STATUSES],
    )
    prices = _offer_prices(supabase, walker.id, [row["id"] for row in [*ordered, *extra]])
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
        # A done walk never shows the phone, even when the same owner has a live one.
        owner = (owners.get(row.get("owner_id")) or {}) if row["status"] in LIVE_STATUSES else {}
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
                "durationHours": _hours(row),
                "netCop": _net(row, prices),
                "ownerName": owner.get("display_name"),
                "ownerPhone": owner.get("phone"),
            }
        )
        previous = row

    active = None
    if walking is not None:
        check_ins = run_query(
            supabase.table(TIMELINE_TABLE)
            .select("kind,at")
            .eq("walk_request_id", walking["id"])
            .eq("kind", "check_in")
        )
        started = max((row["at"] for row in check_ins if row.get("at")), default=None)
        owner = owners.get(walking.get("owner_id")) or {}
        active = {
            "requestId": walking["id"],
            "dogName": (dogs.get(walking.get("dog_id")) or {}).get("name"),
            "requestedTime": walking["requested_time"],
            "durationHours": _hours(walking),
            "startedAt": started,
            "netCop": _net(walking, prices),
            "ownerName": owner.get("display_name"),
            "ownerPhone": owner.get("phone"),
            "pickupLat": walking.get("pickup_lat"),
            "pickupLng": walking.get("pickup_lng"),
        }

    next_stop = next((stop for stop in stops if stop["status"] != "done"), None)
    return {
        "date": day,
        "start": start,
        "stops": stops,
        "totalKm": round(sum(stop["legKm"] for stop in stops), 2),
        "totalMinutes": sum(stop["legMinutes"] for stop in stops),
        "nextStopId": next_stop["requestId"] if next_stop is not None else None,
        "overdue": [
            {
                "requestId": row["id"],
                "dogName": (dogs.get(row.get("dog_id")) or {}).get("name"),
                "requestedTime": row["requested_time"],
                "durationHours": _hours(row),
                "pickupLat": row.get("pickup_lat"),
                "pickupLng": row.get("pickup_lng"),
            }
            for row in late
        ],
        "active": active,
    }


@router.get("/walkers/{walkerId}/walking-route", response_model=WalkingRouteView)
@limiter.limit("30/minute")
def get_walking_route(
    request: Request,
    fromLat: float = Query(...),
    fromLng: float = Query(...),
    toLat: float = Query(...),
    toLng: float = Query(...),
    walker: User = Depends(walker_from_path),
    source: Any = Depends(get_route_source),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Return the way from the walker to a pickup, street by street.

    The answer is what the app draws on its own map: the line along the
    streets and the turns in Spanish. Only the two points leave this server,
    never who asked. A routing failure is never an error for the caller: the
    straight line comes back instead, with ``source: "straight"``, so Cómo
    llegar always shows something. Routes are cached per ~11 m cell, which is
    what makes a walker's repeated calls cheap while he moves.

    Args:
        request: The incoming request, read by the rate limiter.
        fromLat: Latitude of the walker.
        fromLng: Longitude of the walker.
        toLat: Latitude of the pickup.
        toLng: Longitude of the pickup.
        walker: The authenticated walker named in the path.
        source: The routing source (dependency, faked in tests).
        now: The current time (dependency, frozen in tests).

    Returns:
        ``{source, distanceKm, minutes, geometry, steps}``.

    Raises:
        HTTPException: 400 when a point is outside Pasto, 403 for a caller who
            is not the walker in the path.

    Big-O: O(p) in the points of the route; O(1) on a cache hit.
    """
    if not inside_pasto(fromLat, fromLng) or not inside_pasto(toLat, toLng):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Both points must be inside Pasto.",
        )

    key = cache_key(fromLat, fromLng, toLat, toLng)
    cached = cache_get(key, now)
    if cached is not None:
        return cached

    route: dict[str, Any] | None = None
    try:
        route = parse_route(source.foot_route(fromLat, fromLng, toLat, toLng))
    except Exception:  # noqa: BLE001 - any upstream trouble falls back to the line
        route = None

    if route is None:
        km = haversine_km(fromLat, fromLng, toLat, toLng)
        return {
            "source": "straight",
            "distanceKm": round(km, 3),
            "minutes": max(1, int(round(km / STRAIGHT_SPEED_KMH * 60))),
            "geometry": [[fromLat, fromLng], [toLat, toLng]],
            "steps": [],
        }

    cache_put(key, route, now)
    return route

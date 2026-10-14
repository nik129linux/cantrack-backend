"""Bidding: owners post open walk requests, walkers list the ones they may bid on.

Roles (FR-23, FR-25): ``POST /bidding/requests`` is owner-only and the dog
must be the caller's (404 otherwise); ``GET /bidding/requests/open`` is
walker-only, and a walker with no stored position sees nothing. A request is
visible to a walker when its deadline has not passed, the walker is inside
the bidding radius of the pickup, and either the walker is free for the whole
window or — busy — the new dog is pack-compatible with the active route, the
detour through the street graph is within the limit and the walker's capacity
allows one more dog. Eligibility math lives in ``bidding_eligibility``; this
router only loads rows and keeps them or drops them.
"""

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client
from supabase_auth.types import User

from ..bidding import DEFAULT_CONFIG
from ..bidding_eligibility import (
    is_busy_eligible,
    is_free_in_window,
    nearby_walker_ids,
    project_to_metres,
)
from ..db import first_row, run_query
from ..deps import get_current_user, get_now, get_supabase
from ..schemas import CreateBiddingRequestBody
from ..structures import IntervalTree, Quadtree, UnionFind, WeightedGraph

router = APIRouter(prefix="/bidding")

REQUESTS_TABLE = "walk_requests"
DOGS_TABLE = "dogs"
POSITIONS_TABLE = "walker_positions"
PACK_LINKS_TABLE = "dog_pack_links"
GRAPH_EDGES_TABLE = "walk_graph_edges"
ROUTE_STOPS_TABLE = "walk_route_stops"

NOT_FOUND_MESSAGE = "Request not found."
DOG_NOT_FOUND_MESSAGE = "Dog not found."
OWNER_ROLE_MESSAGE = "Only owners can post a walk request."
WALKER_ROLE_MESSAGE = "Only walkers can list open walk requests."
WINDOW_TIME_MESSAGE = (
    "windowStart must be an ISO-8601 datetime with a timezone offset."
)
WINDOW_FUTURE_MESSAGE = "windowStart must be in the future."

#: The statuses whose windows keep a walker busy.
BUSY_STATUSES = ["assigned", "in_walk"]

#: Quadtree bounds in metres (the eligibility tests use the same).
QUADTREE_BOUNDS = (-20_000_000.0, -20_000_000.0, 20_000_000.0, 20_000_000.0)


def _role(user: User) -> str | None:
    """Read the role claim, mirroring the S1 routers. O(1)."""
    metadata = getattr(user, "user_metadata", None) or {}
    return metadata.get("role")


def _parse_window_start(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, refusing naive values (400, exact message). O(1)."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=WINDOW_TIME_MESSAGE
        ) from None
    if parsed.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=WINDOW_TIME_MESSAGE
        )
    return parsed


def _pickup_stop(walk_request_id: str) -> str:
    """The stop key a walk request's pickup has on the street graph. O(1)."""
    return f"pickup:{walk_request_id}"


def _open_view(row: dict[str, Any]) -> dict[str, Any]:
    """The request as the bidding marketplace shows it. O(1)."""
    return {
        "id": row["id"],
        "status": row["status"],
        "dogId": row["dog_id"],
        "requestedTime": row["requested_time"],
        "windowEnd": row["window_end"],
        "durationHours": row["duration_hours"],
        "priceCop": row["price_cop"],
        "pickupLat": row["pickup_lat"],
        "pickupLng": row["pickup_lng"],
        "deadlineAt": row["deadline_at"],
    }


@router.post("/requests", status_code=status.HTTP_201_CREATED)
def create_bidding_request(
    body: CreateBiddingRequestBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Post an open bidding request (owners only).

    The dog must be the caller's (404 otherwise), ``windowStart`` must carry
    an offset (400) and lie in the future (400, against the overridable
    clock). The new row starts ``open`` with ``walker_id`` NULL (broadcast):
    ``requested_time`` is the window start, ``window_end`` the start plus
    the duration, and ``deadline_at`` now plus ``auto_pick_after_min`` — the
    moment an auto-pick may fire (later tasks).

    Args:
        body: dog, window, duration, asking price and pickup pin.
        user: The authenticated owner.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        The stored request in the marketplace view (201).

    Raises:
        HTTPException: 400 (role, window format, past window), 404 (dog not
            the caller's), 500 (database).
    """
    if _role(user) != "owner":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=OWNER_ROLE_MESSAGE
        )

    first_row(
        run_query(
            supabase.table(DOGS_TABLE)
            .select("*")
            .eq("id", body.dogId)
            .eq("owner_id", user.id)
        ),
        DOG_NOT_FOUND_MESSAGE,
    )

    window_start = _parse_window_start(body.windowStart)
    if window_start < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=WINDOW_FUTURE_MESSAGE
        )

    rows = run_query(
        supabase.table(REQUESTS_TABLE).insert(
            {
                "owner_id": user.id,
                "walker_id": None,
                "dog_id": body.dogId,
                "status": "open",
                "requested_time": body.windowStart,
                "window_end": (
                    window_start + timedelta(hours=body.durationHours)
                ).isoformat(),
                "deadline_at": (
                    now + timedelta(minutes=DEFAULT_CONFIG.auto_pick_after_min)
                ).isoformat(),
                "duration_hours": body.durationHours,
                "pickup_lat": body.pickupLat,
                "pickup_lng": body.pickupLng,
                "price_cop": body.priceCop,
            }
        )
    )
    created = first_row(rows, NOT_FOUND_MESSAGE)
    return _open_view(created)


@router.get("/requests/open")
def list_open_requests(
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> list[dict[str, Any]]:
    """List the open requests this walker may bid on (walkers only).

    A walker with no stored position sees nothing. Otherwise every open
    request whose deadline has not passed is kept when the walker is inside
    the bidding radius of the pickup — projected into the hand-written
    Quadtree built from ``walker_positions`` — and either free for the whole
    window (IntervalTree over the walker's ``assigned``/``in_walk`` windows)
    or busy-eligible: pack-compatible with the active route's dogs
    (Union-Find over ``dog_pack_links``), detour within the limit
    (WeightedGraph over ``walk_graph_edges``, from the route's first stop to
    ``pickup:<request_id>``) and room for one more dog.

    Args:
        user: The authenticated walker.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        The visible open requests in marketplace view.

    Raises:
        HTTPException: 400 for a caller without the walker role; 500 on
            database errors.
    """
    if _role(user) != "walker":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=WALKER_ROLE_MESSAGE
        )

    my_positions = run_query(
        supabase.table(POSITIONS_TABLE).select("*").eq("walker_id", user.id)
    )
    if not my_positions:
        return []

    walk_requests = run_query(
        supabase.table(REQUESTS_TABLE).select("*").eq("status", "open")
    )
    if not walk_requests:
        return []

    active_rows = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .eq("walker_id", user.id)
        .in_("status", BUSY_STATUSES)
    )
    busy_windows: IntervalTree[str] = IntervalTree()
    for other in active_rows:
        busy_windows.insert(
            other["requested_time"], other["window_end"], other["id"]
        )

    pack: UnionFind[str] = UnionFind()
    street_graph: WeightedGraph[str] = WeightedGraph()
    current_dog_ids: list[str] = []
    route_stop = None
    if active_rows:
        for link in run_query(supabase.table(PACK_LINKS_TABLE).select("*")):
            pack.add(link["dog_a"])
            pack.add(link["dog_b"])
            pack.union(link["dog_a"], link["dog_b"])
        for edge in run_query(supabase.table(GRAPH_EDGES_TABLE).select("*")):
            street_graph.add_edge(
                edge["from_stop"], edge["to_stop"], edge["minutes"]
            )
        for other in active_rows:
            stops = run_query(
                supabase.table(ROUTE_STOPS_TABLE)
                .select("*")
                .eq("walk_request_id", other["id"])
                .order("position")
            )
            if stops and route_stop is None:
                route_stop = stops[0]["stop_key"]
            current_dog_ids.extend(stop["dog_id"] for stop in stops)

    position_rows = run_query(supabase.table(POSITIONS_TABLE).select("*"))
    visible: list[dict[str, Any]] = []
    for walk_request in walk_requests:
        if datetime.fromisoformat(walk_request["deadline_at"]) <= now:
            continue
        nearby_walkers: Quadtree[str] = Quadtree(*QUADTREE_BOUNDS)
        for position in position_rows:
            east, north = project_to_metres(
                position["lat"], position["lng"], walk_request["pickup_lat"]
            )
            nearby_walkers.insert(position["walker_id"], east, north)
        if (
            user.id
            not in nearby_walker_ids(
                nearby_walkers,
                walk_request["pickup_lat"],
                walk_request["pickup_lng"],
                DEFAULT_CONFIG.radius_km,
            )
        ):
            continue
        if is_free_in_window(
            busy_windows, walk_request["requested_time"], walk_request["window_end"]
        ):
            visible.append(_open_view(walk_request))
            continue
        if is_busy_eligible(
            pack=pack,
            new_dog_id=walk_request["dog_id"],
            current_dog_ids=current_dog_ids,
            street_graph=street_graph,
            route_stop=route_stop,
            pickup_stop=_pickup_stop(walk_request["id"]),
            detour_limit_min=DEFAULT_CONFIG.detour_limit_min,
            dog_count=len(current_dog_ids),
            capacity=DEFAULT_CONFIG.walker_capacity,
        ):
            visible.append(_open_view(walk_request))
    return visible

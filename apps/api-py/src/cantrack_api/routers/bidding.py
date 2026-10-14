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

from ..bidding import (
    DEFAULT_CONFIG,
    WEEK_SECONDS,
    WalkOffer,
    rank_offers,
    score_offers,
    spend_cancel_token,
)
from ..bidding_eligibility import (
    is_busy_eligible,
    is_free_in_window,
    nearby_walker_ids,
    project_to_metres,
)
from ..db import first_row, run_query
from ..deps import get_current_user, get_now, get_supabase
from ..geo import haversine_km
from ..schemas import CreateBiddingRequestBody, PickOfferBody, SendOfferBody
from ..structures import (
    AvlTree,
    IllegalTransition,
    IntervalTree,
    Quadtree,
    RatingWindow,
    SinglyLinkedList,
    TokenBucket,
    UnionFind,
    WalkEvent,
    WalkState,
    WalkStateMachine,
    WeightedGraph,
)

router = APIRouter(prefix="/bidding")

REQUESTS_TABLE = "walk_requests"
DOGS_TABLE = "dogs"
POSITIONS_TABLE = "walker_positions"
PACK_LINKS_TABLE = "dog_pack_links"
GRAPH_EDGES_TABLE = "walk_graph_edges"
ROUTE_STOPS_TABLE = "walk_route_stops"
OFFERS_TABLE = "walk_offers"
RATINGS_TABLE = "walk_ratings"
BUCKETS_TABLE = "walker_cancel_buckets"
TIMELINE_TABLE = "walk_timeline"

NOT_FOUND_MESSAGE = "Request not found."
DOG_NOT_FOUND_MESSAGE = "Dog not found."
OWNER_ROLE_MESSAGE = "Only owners can post a walk request."
WALKER_ROLE_MESSAGE = "Only walkers can list open walk requests."
WINDOW_TIME_MESSAGE = (
    "windowStart must be an ISO-8601 datetime with a timezone offset."
)
WINDOW_FUTURE_MESSAGE = "windowStart must be in the future."
NOT_YOUR_WALK_MESSAGE = "Not your walk request."
WALK_NOT_VISIBLE_MESSAGE = "Walk not visible to you."
WALKER_ALREADY_OFFERED_MESSAGE = "Walker already offered on this walk."
PICK_OWNER_MESSAGE = "Only owners can pick an offer."
AUTO_PICK_OWNER_MESSAGE = "Only owners can run an auto-pick."
WALK_NO_LONGER_OPEN_MESSAGE = "Walk no longer open."
DEADLINE_NOT_PASSED_MESSAGE = "Deadline has not passed yet."
OFFER_NOT_FOUND_MESSAGE = "Offer not found."
TRANSITION_MESSAGE = "That transition is not allowed from the current state."
NO_TOKENS_MESSAGE = "No cancellation tokens left."
NO_SHOW_TOO_EARLY_MESSAGE = "Too early to mark a no-show."

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


def _is_visible_to_walker(
    walk_request: dict[str, Any],
    walker_id: str,
    now: datetime,
    position_rows: list[dict[str, Any]],
    active_rows: list[dict[str, Any]],
    pack: UnionFind[str],
    street_graph: WeightedGraph[str],
    current_dog_ids: list[str],
    route_stop: str | None,
) -> bool:
    """Whether a walker passes the deadline, radius and free/busy checks. O(V^2 + E + m)."""
    if datetime.fromisoformat(walk_request["deadline_at"]) <= now:
        return False
    nearby_walkers: Quadtree[str] = Quadtree(*QUADTREE_BOUNDS)
    for position in position_rows:
        east, north = project_to_metres(
            position["lat"], position["lng"], walk_request["pickup_lat"]
        )
        nearby_walkers.insert(position["walker_id"], east, north)
    if walker_id not in nearby_walker_ids(
        nearby_walkers,
        walk_request["pickup_lat"],
        walk_request["pickup_lng"],
        DEFAULT_CONFIG.radius_km,
    ):
        return False
    busy_windows: IntervalTree[str] = IntervalTree()
    for other in active_rows:
        busy_windows.insert(
            other["requested_time"], other["window_end"], other["id"]
        )
    if is_free_in_window(
        busy_windows, walk_request["requested_time"], walk_request["window_end"]
    ):
        return True
    return is_busy_eligible(
        pack=pack,
        new_dog_id=walk_request["dog_id"],
        current_dog_ids=current_dog_ids,
        street_graph=street_graph,
        route_stop=route_stop,
        pickup_stop=_pickup_stop(walk_request["id"]),
        detour_limit_min=DEFAULT_CONFIG.detour_limit_min,
        dog_count=len(current_dog_ids),
        capacity=DEFAULT_CONFIG.walker_capacity,
    )


def _load_walk_offers(
    supabase: Client, request_id: str
) -> list[WalkOffer]:
    """Build WalkOffer objects for every offer on a request, with reputation data. O(o · r)."""
    offer_rows = run_query(
        supabase.table(OFFERS_TABLE).select("*").eq("walk_request_id", request_id)
    )
    walk_offers: list[WalkOffer] = []
    for offer_row in offer_rows:
        walker_id = offer_row["walker_id"]
        rating_rows = run_query(
            supabase.table(RATINGS_TABLE)
            .select("*")
            .eq("walker_id", walker_id)
            .order("created_at")
            .limit(DEFAULT_CONFIG.rating_window_size)
        )
        rating_window: RatingWindow = RatingWindow(DEFAULT_CONFIG.rating_window_size)
        for rating_row in rating_rows:
            rating_window.add(rating_row["rating"])
        bucket_rows = run_query(
            supabase.table(BUCKETS_TABLE).select("*").eq("walker_id", walker_id)
        )
        bucket_empty = bool(bucket_rows) and bucket_rows[0]["remaining"] == 0
        walk_offers.append(
            WalkOffer(
                offer_id=offer_row["id"],
                walker_id=walker_id,
                price_cop=offer_row["price_cop"],
                distance_km=offer_row["distance_km"],
                recent_rating=rating_window.recent_average(),
                bucket_empty=bucket_empty,
                created_at=datetime.fromisoformat(offer_row["created_at"]).timestamp(),
            )
        )
    return walk_offers


def _offer_ratings(
    supabase: Client, walker_id: str
) -> tuple[float | None, float | None]:
    """Return (recentRating, allTimeRating) for a walker. O(r)."""
    rating_rows = run_query(
        supabase.table(RATINGS_TABLE)
        .select("*")
        .eq("walker_id", walker_id)
        .order("created_at")
    )
    rating_window: RatingWindow = RatingWindow(DEFAULT_CONFIG.rating_window_size)
    for rating_row in rating_rows:
        rating_window.add(rating_row["rating"])
    return rating_window.recent_average(), rating_window.all_time_average()


@router.post("/requests/{request_id}/offers", status_code=status.HTTP_201_CREATED)
def send_offer(
    request_id: str,
    body: SendOfferBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Send an offer on an open walk request (walkers only).

    The request must be ``open`` and visible to the caller — within the bidding
    radius, deadline not passed, and either free or busy-eligible. One offer per
    walker per request. The distance is the haversine from the walker's stored
    position to the pickup.

    Args:
        request_id: The walk request to bid on.
        body: The bid's price and ETA.
        user: The authenticated walker.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        ``{id, score}`` — the new offer's id and its score among all offers.

    Raises:
        HTTPException: 400 (not a walker), 404 (unknown request), 403 (not open
            or not visible), 409 (already offered), 500 (database).
    """
    if _role(user) != "walker":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=WALKER_ROLE_MESSAGE
        )

    walk_request = first_row(
        run_query(
            supabase.table(REQUESTS_TABLE).select("*").eq("id", request_id)
        ),
        NOT_FOUND_MESSAGE,
    )
    if walk_request["status"] != "open":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=WALK_NOT_VISIBLE_MESSAGE
        )

    position_rows = run_query(supabase.table(POSITIONS_TABLE).select("*"))
    active_rows = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .eq("walker_id", user.id)
        .in_("status", BUSY_STATUSES)
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

    if not _is_visible_to_walker(
        walk_request,
        user.id,
        now,
        position_rows,
        active_rows,
        pack,
        street_graph,
        current_dog_ids,
        route_stop,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=WALK_NOT_VISIBLE_MESSAGE
        )

    existing_offers = run_query(
        supabase.table(OFFERS_TABLE)
        .select("*")
        .eq("walk_request_id", request_id)
        .eq("walker_id", user.id)
    )
    if existing_offers:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=WALKER_ALREADY_OFFERED_MESSAGE
        )

    walker_position = first_row(
        run_query(
            supabase.table(POSITIONS_TABLE)
            .select("*")
            .eq("walker_id", user.id)
        ),
        WALK_NOT_VISIBLE_MESSAGE,
    )
    distance_km = haversine_km(
        walker_position["lat"],
        walker_position["lng"],
        walk_request["pickup_lat"],
        walk_request["pickup_lng"],
    )

    rows = run_query(
        supabase.table(OFFERS_TABLE).insert(
            {
                "walk_request_id": request_id,
                "walker_id": user.id,
                "price_cop": body.priceCop,
                "eta_min": body.etaMin,
                "distance_km": distance_km,
                "created_at": now.isoformat(),
            }
        )
    )
    created = first_row(rows, NOT_FOUND_MESSAGE)

    walk_offers = _load_walk_offers(supabase, request_id)
    scores = score_offers(walk_offers)
    return {"id": created["id"], "score": scores[created["id"]]}


@router.get("/requests/{request_id}/offers")
def list_offers(
    request_id: str,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> list[dict[str, Any]]:
    """List the offers on a walk request, ranked best-first (owners only).

    Only the owning owner may read the offers; a foreign or unknown request
    is a 403. Each offer carries the walker's score, recent rating (last-25
    window) and all-time rating.

    Args:
        request_id: The walk request whose offers to list.
        user: The authenticated owner.
        supabase: The Supabase client.

    Returns:
        The ranked offers, each ``{offerId, walkerId, priceCop, etaMin, score,
        recentRating, allTimeRating}``.

    Raises:
        HTTPException: 400 (not an owner), 403 (not the owner), 500 (database).
    """
    if _role(user) != "owner":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=OWNER_ROLE_MESSAGE
        )

    walk_request_rows = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .eq("id", request_id)
        .eq("owner_id", user.id)
    )
    if not walk_request_rows:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=NOT_YOUR_WALK_MESSAGE
        )

    offer_rows = run_query(
        supabase.table(OFFERS_TABLE).select("*").eq("walk_request_id", request_id)
    )
    walk_offers = _load_walk_offers(supabase, request_id)
    scores = score_offers(walk_offers)
    ranked_ids = rank_offers(walk_offers)

    offer_map = {offer.offer_id: offer for offer in walk_offers}
    ranked_offers: list[dict[str, Any]] = []
    for offer_id in ranked_ids:
        offer = offer_map[offer_id]
        offer_row = next(row for row in offer_rows if row["id"] == offer_id)
        recent_rating, all_time_rating = _offer_ratings(supabase, offer.walker_id)
        ranked_offers.append(
            {
                "offerId": offer_id,
                "walkerId": offer.walker_id,
                "priceCop": offer_row["price_cop"],
                "etaMin": offer_row["eta_min"],
                "score": scores[offer_id],
                "recentRating": recent_rating,
                "allTimeRating": all_time_rating,
            }
        )
    return ranked_offers


def _join_active_route(
    supabase: Client, winner_id: str, walk_request: dict[str, Any]
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Append the picked dog to the winning walker's active route. O(a + s log s).

    The winner's busy walks (``assigned``/``in_walk``) are scanned in window
    order; the first one with stored stops owns the route. Its stops are loaded
    into a SinglyLinkedList, the new dog's stop is appended and persisted as a
    ``walk_route_stops`` row on that walk (``position`` = max + 1), and every
    stop's ``pickup_time`` is re-indexed into an AvlTree whose ``min()`` is
    the next pickup. A winner with no busy route gets an empty stop list and
    no next pickup.

    Args:
        supabase: The Supabase client.
        winner_id: The walker who won the pick.
        walk_request: The walk request that was just assigned.

    Returns:
        ``(route_stops, next_pickup)`` — the route in order, each stop
        ``{dogId, stopKey, pickupTime, position}``, and the next pickup
        ``{pickupTime, stopKey, dogId}``; ``next_pickup`` is None when the
        winner has no active route.
    """
    active_rows = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .eq("walker_id", winner_id)
        .in_("status", BUSY_STATUSES)
        .order("requested_time")
    )
    for active_row in active_rows:
        stop_rows = run_query(
            supabase.table(ROUTE_STOPS_TABLE)
            .select("*")
            .eq("walk_request_id", active_row["id"])
            .order("position")
        )
        if not stop_rows:
            continue
        route: SinglyLinkedList[dict[str, Any]] = SinglyLinkedList()
        for stop_row in stop_rows:
            route.append(stop_row)
        new_stop = {
            "walk_request_id": active_row["id"],
            "dog_id": walk_request["dog_id"],
            "stop_key": _pickup_stop(walk_request["id"]),
            "pickup_time": walk_request["requested_time"],
            "position": max(stop_row["position"] for stop_row in stop_rows) + 1,
        }
        route.append(new_stop)
        run_query(supabase.table(ROUTE_STOPS_TABLE).insert(new_stop))
        pickup_index: AvlTree[str, dict[str, Any]] = AvlTree()
        for stop in route.to_array():
            pickup_index.insert(stop["pickup_time"], stop)
        _, next_stop = pickup_index.min()
        route_stops = [
            {
                "dogId": stop["dog_id"],
                "stopKey": stop["stop_key"],
                "pickupTime": stop["pickup_time"],
                "position": stop["position"],
            }
            for stop in route.to_array()
        ]
        next_pickup = {
            "pickupTime": next_stop["pickup_time"],
            "stopKey": next_stop["stop_key"],
            "dogId": next_stop["dog_id"],
        }
        return route_stops, next_pickup
    return [], None


@router.post("/requests/{request_id}/pick")
def pick_offer(
    request_id: str,
    body: PickOfferBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Pick the winning offer on an open walk request (the owning owner only).

    The ``OWNER_PICK`` transition is validated through the WalkStateMachine,
    then applied with a compare-and-set on the request status: the first pick
    flips the walk to ``assigned`` and names the offer's walker; a later pick
    loses with a 409 because the ``status='open`` filter matches zero rows.
    An owner pick always wins over a later auto-pick, since auto-pick only
    fires once the deadline has passed.

    Args:
        request_id: The walk request to resolve.
        body: The offer the owner picks.
        user: The authenticated owner.
        supabase: The Supabase client.

    Returns:
        ``{id, status: "assigned", walkerId, routeStops}`` — ``routeStops`` is
        the winning walker's active route after the pick, each stop
        ``{dogId, stopKey, pickupTime, position}``; a busy winner's new dog is
        appended to the route and ``nextPickup`` (``{pickupTime, stopKey,
        dogId}``, the AvlTree minimum over every stop's pickup time) is
        included, while a free winner gets ``routeStops: []`` and no
        ``nextPickup``.

    Raises:
        HTTPException: 400 (not an owner), 403 (not the owner), 404 (unknown
            offer), 409 (walk no longer open), 500 (database).
    """
    if _role(user) != "owner":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=PICK_OWNER_MESSAGE
        )

    walk_request = _owned_request(supabase, request_id, user.id)

    _validate_transition(walk_request["status"], WalkEvent.OWNER_PICK)

    offer_row = first_row(
        run_query(
            supabase.table(OFFERS_TABLE)
            .select("*")
            .eq("walk_request_id", request_id)
            .eq("id", body.offerId)
        ),
        OFFER_NOT_FOUND_MESSAGE,
    )

    updated = run_query(
        supabase.table(REQUESTS_TABLE)
        .update({"status": "assigned", "walker_id": offer_row["walker_id"]})
        .eq("id", request_id)
        .eq("status", "open")
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=WALK_NO_LONGER_OPEN_MESSAGE
        )
    won = updated[0]
    route_stops, next_pickup = _join_active_route(
        supabase, won["walker_id"], walk_request
    )
    resolved: dict[str, Any] = {
        "id": won["id"],
        "status": won["status"],
        "walkerId": won["walker_id"],
        "routeStops": route_stops,
    }
    if next_pickup is not None:
        resolved["nextPickup"] = next_pickup
    return resolved


@router.post("/requests/{request_id}/auto-pick")
def auto_pick_offer(
    request_id: str,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Resolve an open walk request once its bidding deadline has passed.

    Before ``deadline_at`` the request is still answerable, so this is a 400.
    At or after the deadline the top-ranked offer wins (``AUTO_PICK``); with
    no offers at all the walk ``DEADLINE_EMPTY`` → ``expired``. Both go
    through the WalkStateMachine and the same compare-and-set on the request
    status, so a walk already resolved by an owner pick matches zero rows
    and answers 409.

    Args:
        request_id: The walk request to resolve.
        user: The authenticated owner.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        ``{id, status, walkerId}`` — ``walkerId`` is None for an expiry.

    Raises:
        HTTPException: 400 (not an owner, deadline not passed), 403 (not the
            owner), 409 (walk no longer open), 500 (database).
    """
    if _role(user) != "owner":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=AUTO_PICK_OWNER_MESSAGE
        )

    walk_request = _owned_request(supabase, request_id, user.id)

    deadline_at = datetime.fromisoformat(walk_request["deadline_at"])
    if now < deadline_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=DEADLINE_NOT_PASSED_MESSAGE
        )

    walk_offers = _load_walk_offers(supabase, request_id)
    event = WalkEvent.AUTO_PICK if walk_offers else WalkEvent.DEADLINE_EMPTY
    _validate_transition(walk_request["status"], event)

    patch: dict[str, Any]
    if walk_offers:
        ranked_ids = rank_offers(walk_offers)
        offer_map = {offer.offer_id: offer for offer in walk_offers}
        winner = offer_map[ranked_ids[0]]
        patch = {"status": "assigned", "walker_id": winner.walker_id}
    else:
        patch = {"status": "expired"}

    updated = run_query(
        supabase.table(REQUESTS_TABLE)
        .update(patch)
        .eq("id", request_id)
        .eq("status", "open")
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=WALK_NO_LONGER_OPEN_MESSAGE
        )
    row = updated[0]
    return {"id": row["id"], "status": row["status"], "walkerId": row["walker_id"]}


def _owned_request(supabase: Client, request_id: str, owner_id: str) -> dict[str, Any]:
    """Load a walk request scoped to its owner, or 403 for a foreign one. O(1)."""
    walk_request_rows = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .eq("id", request_id)
        .eq("owner_id", owner_id)
    )
    if not walk_request_rows:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=NOT_YOUR_WALK_MESSAGE
        )
    return walk_request_rows[0]


def _validate_transition(current_status: str, event: WalkEvent) -> None:
    """Validate an event against a status through the WalkStateMachine. O(1).

    Raises:
        HTTPException: 409 ``Walk no longer open.`` when the machine refuses
            the pair — the mapping every bidding endpoint uses for a walk that
            has already left ``open``.
    """
    try:
        WalkStateMachine.next_state(WalkState(current_status), event)
    except IllegalTransition:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=WALK_NO_LONGER_OPEN_MESSAGE
        ) from None


def _validate_lifecycle_transition(current_status: str, event: WalkEvent) -> None:
    """Validate an event for the cancels/check-in/check-out endpoints. O(1).

    Raises:
        HTTPException: 409 ``That transition is not allowed from the current
            state.`` when the machine refuses the pair — the IllegalTransition
            mapping every lifecycle endpoint uses.
    """
    try:
        WalkStateMachine.next_state(WalkState(current_status), event)
    except IllegalTransition:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=TRANSITION_MESSAGE
        ) from None


def _spend_walker_token(
    supabase: Client, walker_id: str, now: datetime
) -> tuple[bool, int]:
    """Spend one cancel token for a walker and persist the new count. O(1).

    A missing ``walker_cancel_buckets`` row starts the period full at
    ``cancel_tokens_per_week``; an existing row replays its stored state
    through :func:`spend_cancel_token` so the weekly refill applies. The row
    is written only when a token was actually spent.

    Args:
        supabase: The Supabase client.
        walker_id: The walker whose bucket to spend from.
        now: The current time (dependency, frozen in tests).

    Returns:
        ``(spent, new_remaining)``.
    """
    bucket_rows = run_query(
        supabase.table(BUCKETS_TABLE).select("*").eq("walker_id", walker_id)
    )
    if bucket_rows:
        period_start = datetime.fromisoformat(bucket_rows[0]["period_start"]).timestamp()
        cancel_bucket = TokenBucket(
            DEFAULT_CONFIG.cancel_tokens_per_week, WEEK_SECONDS, period_start
        )
        spent, new_remaining = spend_cancel_token(
            cancel_bucket, bucket_rows[0]["remaining"], period_start, now.timestamp()
        )
        if spent:
            run_query(
                supabase.table(BUCKETS_TABLE)
                .update({"remaining": new_remaining})
                .eq("walker_id", walker_id)
            )
        return spent, new_remaining
    period_start = now.timestamp()
    cancel_bucket = TokenBucket(
        DEFAULT_CONFIG.cancel_tokens_per_week, WEEK_SECONDS, period_start
    )
    spent, new_remaining = spend_cancel_token(
        cancel_bucket,
        DEFAULT_CONFIG.cancel_tokens_per_week,
        period_start,
        period_start,
    )
    run_query(
        supabase.table(BUCKETS_TABLE).insert(
            {
                "walker_id": walker_id,
                "remaining": new_remaining,
                "period_start": now.isoformat(),
            }
        )
    )
    return spent, new_remaining


def _assigned_request(supabase: Client, request_id: str, walker_id: str) -> dict[str, Any]:
    """Load a walk request assigned to ``walker_id``, or 403 for a foreign one. O(1)."""
    walk_request_rows = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .eq("id", request_id)
        .eq("walker_id", walker_id)
    )
    if not walk_request_rows:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=NOT_YOUR_WALK_MESSAGE
        )
    return walk_request_rows[0]


def _apply_transition(
    supabase: Client,
    request_id: str,
    walk_request: dict[str, Any],
    event: WalkEvent,
    patch: dict[str, Any],
    timeline_kind: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Validate, compare-and-set and append the timeline row for a transition. O(1).

    Args:
        supabase: The Supabase client.
        request_id: The walk request to move.
        walk_request: The stored request row (pre-transition status).
        event: The WalkStateMachine event to validate and apply.
        patch: The columns to set on ``walk_requests``.
        timeline_kind: The ``walk_timeline`` kind to append, or None.
        now: The current time (dependency, frozen in tests).

    Returns:
        ``{id, status, walkerId}`` of the updated row.

    Raises:
        HTTPException: 409 when the machine refuses the event or the
            compare-and-set matches zero rows.
    """
    _validate_lifecycle_transition(walk_request["status"], event)
    updated = run_query(
        supabase.table(REQUESTS_TABLE)
        .update(patch)
        .eq("id", request_id)
        .eq("status", walk_request["status"])
    )
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=TRANSITION_MESSAGE
        )
    if timeline_kind is not None:
        run_query(
            supabase.table(TIMELINE_TABLE).insert(
                {
                    "walk_request_id": request_id,
                    "kind": timeline_kind,
                    "at": now.isoformat(),
                    "payload": {},
                }
            )
        )
    row = updated[0]
    return {"id": row["id"], "status": row["status"], "walkerId": row["walker_id"]}


@router.post("/requests/{request_id}/walker-cancel")
def walker_cancel_request(
    request_id: str,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Cancel an assigned walk and reopen it for bidding (the assigned walker).

    Costs one cancel token: a walker with an empty bucket gets a 403 and the
    walk stays ``assigned``. A spent token applies ``WALKER_CANCEL`` through
    the WalkStateMachine and a compare-and-set flips the walk back to
    ``open``; the offers stay in the table so the reopened request keeps its
    heap.

    Args:
        request_id: The walk request to cancel.
        user: The authenticated walker.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        ``{id, status: "open", walkerId}``.

    Raises:
        HTTPException: 403 (not the assigned walker, no tokens left), 409
            (illegal transition or lost compare-and-set), 500 (database).
    """
    walk_request = _assigned_request(supabase, request_id, user.id)
    _validate_lifecycle_transition(walk_request["status"], WalkEvent.WALKER_CANCEL)
    spent, _ = _spend_walker_token(supabase, user.id, now)
    if not spent:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=NO_TOKENS_MESSAGE
        )
    return _apply_transition(
        supabase,
        request_id,
        walk_request,
        WalkEvent.WALKER_CANCEL,
        {"status": "open"},
        None,
        now,
    )


@router.post("/requests/{request_id}/owner-cancel")
def owner_cancel_request(
    request_id: str,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Cancel a walk the owner posted or won (the owning owner only).

    ``open`` withdraws (``OWNER_WITHDRAW``) and ``assigned`` cancels before
    pickup (``OWNER_CANCEL``); both end ``cancelled``. Any other state is a
    409. A foreign request is a 403.

    Args:
        request_id: The walk request to cancel.
        user: The authenticated owner.
        supabase: The Supabase client.

    Returns:
        ``{id, status: "cancelled", walkerId}``.

    Raises:
        HTTPException: 403 (not the owner), 409 (illegal transition), 500
            (database).
    """
    walk_request = _owned_request(supabase, request_id, user.id)
    current_status = walk_request["status"]
    if current_status == "open":
        event = WalkEvent.OWNER_WITHDRAW
    elif current_status == "assigned":
        event = WalkEvent.OWNER_CANCEL
    else:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=TRANSITION_MESSAGE
        )
    return _apply_transition(
        supabase,
        request_id,
        walk_request,
        event,
        {"status": "cancelled"},
        None,
        now,
    )


@router.post("/requests/{request_id}/no-show")
def mark_no_show(
    request_id: str,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Mark the assigned walker as a no-show and reopen the walk (owners only).

    Only legal from ``assigned`` and only at/after ``window_start`` plus the
    no-show grace period — earlier than that is a 400. The no-show counts as a
    walker cancellation: it spends one token via the same helper as
    ``walker-cancel``, but an empty bucket still reopens the walk (the
    penalty applies at ranking time).

    Args:
        request_id: The walk request whose walker failed to show.
        user: The authenticated owner.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        ``{id, status: "open", walkerId}``.

    Raises:
        HTTPException: 400 (too early), 403 (not the owner), 409 (illegal
            transition), 500 (database).
    """
    walk_request = _owned_request(supabase, request_id, user.id)
    _validate_lifecycle_transition(walk_request["status"], WalkEvent.NO_SHOW)
    window_start = datetime.fromisoformat(walk_request["requested_time"])
    if now < window_start + timedelta(minutes=DEFAULT_CONFIG.no_show_grace_min):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=NO_SHOW_TOO_EARLY_MESSAGE
        )
    _spend_walker_token(supabase, walk_request["walker_id"], now)
    return _apply_transition(
        supabase,
        request_id,
        walk_request,
        WalkEvent.NO_SHOW,
        {"status": "open"},
        None,
        now,
    )


@router.post("/requests/{request_id}/check-in")
def check_in_walk(
    request_id: str,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Start the walk (the assigned walker only).

    ``CHECK_IN`` moves the walk from ``assigned`` to ``in_walk`` and appends
    a ``check_in`` row to the walk's timeline; any other state is a 409.

    Args:
        request_id: The walk request to check in to.
        user: The authenticated walker.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        ``{id, status: "in_walk", walkerId}``.

    Raises:
        HTTPException: 403 (not the assigned walker), 409 (illegal transition),
            500 (database).
    """
    walk_request = _assigned_request(supabase, request_id, user.id)
    return _apply_transition(
        supabase,
        request_id,
        walk_request,
        WalkEvent.CHECK_IN,
        {"status": "in_walk"},
        "check_in",
        now,
    )


@router.post("/requests/{request_id}/check-out")
def check_out_walk(
    request_id: str,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Finish the walk (the assigned walker only).

    ``CHECK_OUT`` moves the walk from ``in_walk`` to ``done`` and appends a
    ``check_out`` row to the walk's timeline; any other state is a 409.

    Args:
        request_id: The walk request to check out of.
        user: The authenticated walker.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        ``{id, status: "done", walkerId}``.

    Raises:
        HTTPException: 403 (not the assigned walker), 409 (illegal transition),
            500 (database).
    """
    walk_request = _assigned_request(supabase, request_id, user.id)
    return _apply_transition(
        supabase,
        request_id,
        walk_request,
        WalkEvent.CHECK_OUT,
        {"status": "done"},
        "check_out",
        now,
    )

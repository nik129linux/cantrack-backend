"""Explorar: fare suggestion, owner request list, walker positions (PR2).

The owner asks what a walk is worth (a deterministic market median, not an
AI guess), pages through the requests they posted, and sees how many walkers
are currently around a pickup. Walkers have exactly one write here: their
position heartbeat, the only writer to ``walker_positions`` now that RLS is
deny-all.
"""

import base64
import binascii
import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from supabase import Client
from supabase_auth.types import User

from ..app import limiter
from ..bidding import DEFAULT_CONFIG
from ..bidding_eligibility import nearby_walker_ids, project_to_metres
from ..db import run_query
from ..deps import current_role, get_current_user, get_now, get_supabase
from ..geo import haversine_km
from ..owner_access import owner_from_path
from ..structures import Quadtree

from .bidding import QUADTREE_BOUNDS

router = APIRouter()

REQUESTS_TABLE = "walk_requests"
OFFERS_TABLE = "walk_offers"
DOGS_TABLE = "dogs"
POSITIONS_TABLE = "walker_positions"

#: The walk statuses an owner's request list may filter on.
WALK_STATUSES = ("open", "assigned", "in_walk", "done", "expired", "cancelled")

#: The only walk lengths an owner may ask a fare suggestion for.
OFFERED_DURATIONS = frozenset({30, 45, 60, 90, 120})

#: The statuses whose agreed offer price counts towards the fare market.
AGREED_STATUSES = ["assigned", "in_walk", "done"]

#: How far back agreed prices stay representative for the suggestion.
MARKET_WINDOW_DAYS = 60

#: Fallback fare: COP per minute, and the rounding/minimum in COP.
DEFAULT_COP_PER_MINUTE = 330
MIN_FARE_COP = 8000
FARE_STEP_COP = 1000

#: A walker position is fresh enough to count for this many minutes.
POSITION_TTL_MINUTES = 30

BAD_CURSOR_MESSAGE = "The cursor is not valid."
BAD_DURATION_MESSAGE = "durationMinutes must be one of 30, 45, 60, 90 or 120."
OWNERS_ONLY_MESSAGE = "Only owners can use this."
WALKERS_ONLY_MESSAGE = "Only walkers can report their position."


class FareSuggestionView(BaseModel):
    """Body of ``GET /bidding/fare-suggestion``."""

    suggestedCop: int
    basis: str
    sampleSize: int
    minCop: int
    stepCop: int


class OwnerRequestItem(BaseModel):
    """One row of the owner's request list."""

    id: str
    status: str
    dogId: str
    dogName: str | None
    requestedTime: str
    durationHours: float
    priceCop: int
    offerCount: int
    deadlineAt: str
    walkerId: str | None


class OwnerRequestPage(BaseModel):
    """One cursor page of the owner's requests."""

    items: list[OwnerRequestItem]
    nextCursor: str | None


class WalkerPositionBody(BaseModel):
    """Body of ``PUT /walker-position`` (WGS-84 degrees)."""

    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)


class WalkerPositionView(BaseModel):
    """The walker's position after the heartbeat was stored."""

    lat: float
    lng: float
    updatedAt: str


class NearbyWalkersView(BaseModel):
    """Body of ``GET /bidding/nearby-walkers`` (never who or where)."""

    count: int
    radiusKm: float


def _utc_key(value: str) -> str:
    """Normalise an ISO-8601 timestamp to UTC ISO for stable comparison. O(1).

    Raises:
        ValueError: If the value is not an ISO-8601 timestamp.
    """
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(timezone.utc).isoformat()


def _median(values: list[float]) -> float:
    """Return the median of a non-empty list. O(n log n) (sorting dominates).

    An even-sized list answers the mean of the two middle values.
    """
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _encode_cursor(created_at: str, request_id: str) -> str:
    """Encode ``[created_at, id]`` as urlsafe base64 JSON. O(1)."""
    payload = json.dumps([created_at, request_id], separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(value: str) -> tuple[str, str]:
    """Decode a cursor, answering 400 for anything undecodable. O(1).

    Args:
        value: The urlsafe base64 of a JSON ``[created_at, id]`` pair.

    Returns:
        The ``(created_at, id)`` pair.

    Raises:
        HTTPException: 400 when the value is not base64, not JSON, not a
            two-string list, or carries an unparseable timestamp.
    """
    try:
        raw = base64.b64decode(value.encode(), altchars=b"-_", validate=True)
        payload = json.loads(raw)
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_CURSOR_MESSAGE
        ) from None
    if (
        not isinstance(payload, list)
        or len(payload) != 2
        or not all(isinstance(part, str) for part in payload)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_CURSOR_MESSAGE
        )
    try:
        _utc_key(payload[0])
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_CURSOR_MESSAGE
        ) from None
    return payload[0], payload[1]


@router.get("/bidding/fare-suggestion", response_model=FareSuggestionView)
@limiter.limit("30/minute")
def suggest_fare(
    request: Request,
    durationMinutes: int = Query(..., ge=1, le=12 * 60),
    lat: float = Query(..., ge=-90, le=90, allow_inf_nan=False),
    lng: float = Query(..., ge=-180, le=180, allow_inf_nan=False),
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Suggest a fare from the owner's recent agreed prices (owners only).

    The samples are the winning offers on the walks that reached
    ``assigned``/``in_walk``/``done`` in the last 60 days with a pickup within
    ``DEFAULT_CONFIG.radius_km`` of the requested point. With at least five
    samples the suggestion is the median price per minute times the duration;
    otherwise it falls back to ``DEFAULT_COP_PER_MINUTE``. The result is
    rounded to the nearest ``FARE_STEP_COP`` and never below ``MIN_FARE_COP``.

    Args:
        durationMinutes: One of the five offered walk lengths.
        lat: Pickup latitude.
        lng: Pickup longitude.
        user: The authenticated owner.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        ``{suggestedCop, basis, sampleSize, minCop, stepCop}``.

    Raises:
        HTTPException: 403 for a non-owner, 500 on database errors.

    Big-O: O(w + o + b), where w is the recent walks, o the offers that could
        match them and b their logged rows (the median sort is O(s log s) in
        the sample count s).
    """
    if current_role(user) != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=OWNERS_ONLY_MESSAGE
        )
    if durationMinutes not in OFFERED_DURATIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_DURATION_MESSAGE
        )

    cutoff = now - timedelta(days=MARKET_WINDOW_DAYS)
    rows = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .in_("status", AGREED_STATUSES)
    )
    eligible = []
    for row in rows:
        if not row.get("walker_id"):
            continue
        try:
            created_at = datetime.fromisoformat(row["created_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if created_at < cutoff:
            continue
        try:
            distance = haversine_km(
                lat, lng, row["pickup_lat"], row["pickup_lng"]
            )
        except (KeyError, TypeError, ValueError):
            continue
        if distance <= DEFAULT_CONFIG.radius_km:
            eligible.append(row)

    prices_per_minute: list[float] = []
    if eligible:
        offers = run_query(
            supabase.table(OFFERS_TABLE)
            .select("*")
            .in_("walk_request_id", [row["id"] for row in eligible])
        )
        by_pair = {
            (offer["walk_request_id"], offer["walker_id"]): offer
            for offer in offers
        }
        for row in eligible:
            offer = by_pair.get((row["id"], row["walker_id"]))
            if offer is None:
                continue
            try:
                minutes = float(row["duration_hours"]) * 60.0
                price = float(offer["price_cop"])
            except (KeyError, TypeError, ValueError):
                continue
            if minutes <= 0:
                continue
            prices_per_minute.append(price / minutes)

    sample_size = len(prices_per_minute)
    if sample_size >= 5:
        basis = "market"
        raw = _median(prices_per_minute) * durationMinutes
    else:
        basis = "default"
        raw = DEFAULT_COP_PER_MINUTE * durationMinutes

    rounded = int(math.floor(raw / FARE_STEP_COP + 0.5)) * FARE_STEP_COP
    return {
        "suggestedCop": max(MIN_FARE_COP, rounded),
        "basis": basis,
        "sampleSize": sample_size,
        "minCop": MIN_FARE_COP,
        "stepCop": FARE_STEP_COP,
    }


@router.get("/owners/{ownerId}/requests", response_model=OwnerRequestPage)
def list_owner_requests(
    owner: User = Depends(owner_from_path),
    supabase: Client = Depends(get_supabase),
    limit: int = Query(20, ge=1, le=50),
    status_filter: Literal[
        "open", "assigned", "in_walk", "done", "expired", "cancelled"
    ]
    | None = Query(None, alias="status"),
    cursor: str | None = Query(None),
) -> dict[str, Any]:
    """List the owner's requests newest first, one cursor page at a time.

    Rows are ordered by ``(created_at, id)`` descending; the cursor is the
    urlsafe base64 of a JSON ``[created_at, id]`` pair and an undecodable one
    is a 400. Offers and dogs are loaded with one batched query each.

    Args:
        owner: The authenticated owner named in the path.
        supabase: The Supabase client.
        limit: Page size, 1..50 (default 20).
        status_filter: Optional walk status to keep.
        cursor: Optional cursor from a previous page.

    Returns:
        ``{items, nextCursor}``.

    Raises:
        HTTPException: 400 for a bad cursor/limit/status, 500 on database
            errors.

    Big-O: O(n log n + o + d), where n is the owner's requests (the sort
        dominates), o the offers and d the dogs of the returned page.
    """
    cursor_key = _decode_cursor(cursor) if cursor is not None else None

    query = supabase.table(REQUESTS_TABLE).select("*").eq("owner_id", owner.id)
    if status_filter is not None:
        query = query.eq("status", status_filter)
    rows = run_query(query)

    def sort_key(row: dict[str, Any]) -> tuple[str, str]:
        return _utc_key(row["created_at"]), row["id"]

    rows.sort(key=sort_key, reverse=True)
    if cursor_key is not None:
        cursor_pair = (_utc_key(cursor_key[0]), cursor_key[1])
        rows = [row for row in rows if sort_key(row) < cursor_pair]

    page = rows[:limit]
    has_more = len(rows) > limit

    offers_by_request: dict[str, int] = {}
    dogs_by_id: dict[str, dict[str, Any]] = {}
    if page:
        request_ids = [row["id"] for row in page]
        dog_ids = sorted({row["dog_id"] for row in page})
        for offer in run_query(
            supabase.table(OFFERS_TABLE)
            .select("id,walk_request_id")
            .in_("walk_request_id", request_ids)
        ):
            request_id = offer["walk_request_id"]
            offers_by_request[request_id] = offers_by_request.get(request_id, 0) + 1
        for dog in run_query(
            supabase.table(DOGS_TABLE).select("id,name").in_("id", dog_ids)
        ):
            dogs_by_id[dog["id"]] = dog

    items = [
        {
            "id": row["id"],
            "status": row["status"],
            "dogId": row["dog_id"],
            "dogName": (dogs_by_id.get(row["dog_id"]) or {}).get("name"),
            "requestedTime": row["requested_time"],
            "durationHours": row["duration_hours"],
            "priceCop": row["price_cop"],
            "offerCount": offers_by_request.get(row["id"], 0),
            "deadlineAt": row["deadline_at"],
            "walkerId": row.get("walker_id"),
        }
        for row in page
    ]
    next_cursor = (
        _encode_cursor(page[-1]["created_at"], page[-1]["id"])
        if has_more and page
        else None
    )
    return {"items": items, "nextCursor": next_cursor}


@router.put("/walker-position", response_model=WalkerPositionView)
@limiter.limit("4/minute")
def report_walker_position(
    request: Request,
    body: WalkerPositionBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Store the walker's current position, replacing their previous row.

    Exactly one row per walker is kept: the existing row is updated when there
    is one and inserted otherwise (the Supabase double has no upsert). The
    heartbeat is limited to four reports per minute.

    Args:
        request: The incoming request (used by the rate limiter).
        body: The WGS-84 coordinates to record.
        user: The authenticated walker.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        ``{lat, lng, updatedAt}``.

    Raises:
        HTTPException: 403 for a non-walker, 429 above the rate limit, 500 on
            database errors.

    Big-O: O(p), where p is the walker's position rows (at most one).
    """
    if current_role(user) != "walker":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=WALKERS_ONLY_MESSAGE
        )

    updated_at = now.isoformat()
    existing = run_query(
        supabase.table(POSITIONS_TABLE).select("*").eq("walker_id", user.id)
    )
    if existing:
        run_query(
            supabase.table(POSITIONS_TABLE)
            .update({"lat": body.lat, "lng": body.lng, "updated_at": updated_at})
            .eq("walker_id", user.id)
        )
    else:
        run_query(
            supabase.table(POSITIONS_TABLE).insert(
                {
                    "walker_id": user.id,
                    "lat": body.lat,
                    "lng": body.lng,
                    "updated_at": updated_at,
                }
            )
        )
    return {"lat": body.lat, "lng": body.lng, "updatedAt": updated_at}


@router.get("/bidding/nearby-walkers", response_model=NearbyWalkersView)
@limiter.limit("30/minute")
def count_nearby_walkers(
    request: Request,
    lat: float = Query(..., ge=-90, le=90, allow_inf_nan=False),
    lng: float = Query(..., ge=-180, le=180, allow_inf_nan=False),
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Count walkers with a fresh position inside the bidding radius.

    Fresh positions (``updated_at`` within ``POSITION_TTL_MINUTES``) are
    projected into the hand-written Quadtree the same way
    ``list_open_requests`` does, and the radius query answers how many are
    around the point. Only the count and the radius are returned — never who
    the walkers are or where exactly they are.

    Args:
        lat: Pickup latitude.
        lng: Pickup longitude.
        user: The authenticated owner.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        ``{count, radiusKm}``.

    Raises:
        HTTPException: 403 for a non-owner, 500 on database errors.

    Big-O: O(m) worst case for the radius query over the Quadtree, where m is
        the number of fresh positions.
    """
    if current_role(user) != "owner":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=OWNERS_ONLY_MESSAGE
        )

    cutoff = now - timedelta(minutes=POSITION_TTL_MINUTES)
    positions = run_query(supabase.table(POSITIONS_TABLE).select("*"))
    nearby_walkers: Quadtree[str] = Quadtree(*QUADTREE_BOUNDS)
    for position in positions:
        try:
            updated_at = datetime.fromisoformat(position["updated_at"])
        except (KeyError, TypeError, ValueError):
            continue
        if updated_at < cutoff:
            continue
        east, north = project_to_metres(position["lat"], position["lng"], lat)
        nearby_walkers.insert(position["walker_id"], east, north)

    found = nearby_walker_ids(
        nearby_walkers, lat, lng, DEFAULT_CONFIG.radius_km
    )
    return {"count": len(found), "radiusKm": DEFAULT_CONFIG.radius_km}

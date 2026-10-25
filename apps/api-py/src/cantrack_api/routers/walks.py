"""Mis paseos: walk lists, detail, live position, GPS ingest and repeat (PR3).

The owner's tabs read the walks they own, split by STATUS: ``assigned`` is
upcoming (soonest first), ``in_walk`` is active, and ``done``/``cancelled``/
``expired`` are past (newest first). The detail view answers both parties —
its owner and its assigned walker — with the same 404 for anybody else, and
lists in ``allowedActions`` only the buttons that would actually work.

Live tracking exists only while a walk is ``in_walk``. GPS is written only by
the assigned walker, in strictly increasing order, from the check-in onwards
and never in the future. Repeating a finished walk copies its dog, duration,
price and pickup into a brand-new open request for a future window.
"""

import base64
import binascii
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from supabase import Client
from supabase_auth.types import User

from ..app import limiter
from ..bidding import DEFAULT_CONFIG
from ..db import first_row, run_query
from ..deps import current_role, get_current_user, get_now, get_supabase
from ..owner_access import owner_from_path

router = APIRouter()

REQUESTS_TABLE = "walk_requests"
DOGS_TABLE = "dogs"
PROFILES_TABLE = "walker_profiles"
OFFERS_TABLE = "walk_offers"
RATINGS_TABLE = "walk_ratings"
TIMELINE_TABLE = "walk_timeline"
GPS_POINTS_TABLE = "walk_gps_points"

NOT_FOUND_MESSAGE = "Walk not found."
GPS_WALKER_ONLY_MESSAGE = "Only the assigned walker can send GPS points."
NOT_IN_PROGRESS_MESSAGE = "This walk is not in progress."
GPS_ONLY_DURING_MESSAGE = "GPS is only accepted during a walk."
GPS_ORDER_MESSAGE = "GPS points must be in order and within the walk."
REPEAT_STATE_MESSAGE = "Only a finished walk can be repeated."
WINDOW_TIME_MESSAGE = "windowStart must be an ISO-8601 datetime with a timezone offset."
WINDOW_FUTURE_MESSAGE = "windowStart must be in the future."
WINDOW_TOO_FAR_MESSAGE = "windowStart must be within the next 30 days."
BAD_TAB_MESSAGE = "tab must be 'upcoming', 'active' or 'past'."
BAD_CURSOR_MESSAGE = "Invalid cursor."

UPCOMING_STATUSES = ["assigned"]
ACTIVE_STATUSES = ["in_walk"]
PAST_STATUSES = ["done", "cancelled", "expired"]
REPEATABLE_STATUSES = ("done", "cancelled", "expired")

DEFAULT_LIMIT = 20
MAX_LIMIT = 50
MAX_WINDOW_AHEAD_DAYS = 30
GPS_FUTURE_GRACE_SECONDS = 60
MAX_GPS_POINTS = 20


class WalkListItem(BaseModel):
    """One walk as the owner's tab lists it."""

    id: str
    status: str
    dogId: str | None
    dogName: str | None
    walkerId: str | None
    walkerName: str | None
    requestedTime: str
    durationHours: float
    priceCop: int
    canRate: bool
    hasReport: bool


class WalkListResponse(BaseModel):
    """A page of walks plus the cursor of the next page, if any."""

    items: list[WalkListItem]
    nextCursor: str | None


class DogCard(BaseModel):
    """The dog on a walk detail."""

    id: str | None
    name: str | None


class WalkerCard(BaseModel):
    """The walker on a walk detail, with their all-time reputation."""

    id: str | None
    name: str | None
    allTimeRating: float | None
    ratingCount: int


class TimelineEvent(BaseModel):
    """One event of a walk's timeline."""

    kind: str
    at: str
    payload: dict[str, Any]


class WalkReport(BaseModel):
    """The stored report of a walk; only present once ``report_status`` is set."""

    status: str
    text: str | None
    distanceKm: float | None
    durationMin: int | None


class WalkDetail(BaseModel):
    """The full detail of one walk, as its owner or assigned walker sees it."""

    dog: DogCard
    walker: WalkerCard
    priceCop: int
    offerCount: int
    timeline: list[TimelineEvent]
    rating: float | None
    report: WalkReport | None
    allowedActions: list[str]


class GpsPointBody(BaseModel):
    """One GPS fix sent by the walker."""

    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)
    atEpoch: float = Field(allow_inf_nan=False)


class GpsBatchBody(BaseModel):
    """A GPS batch: 1 to 20 fixes."""

    points: list[GpsPointBody] = Field(min_length=1, max_length=MAX_GPS_POINTS)


class GpsStored(BaseModel):
    """The answer to a GPS batch."""

    stored: int


class LivePosition(BaseModel):
    """The latest known GPS fix of a live walk."""

    lat: float
    lng: float
    atEpoch: float


class LiveResponse(BaseModel):
    """The live tracking state of a walk."""

    position: LivePosition | None
    minutesElapsed: int


class RepeatBody(BaseModel):
    """Body of ``POST /bidding/requests/{id}/repeat``."""

    windowStart: str


class RepeatResponse(BaseModel):
    """The new open request repeated from a finished walk."""

    id: str
    status: str
    dogId: str
    priceCop: int
    durationHours: float


def _parse_time(value: str) -> datetime:
    """Parse an aware ISO-8601 timestamp from client input. O(1).

    Raises:
        HTTPException: 400 when the value is not a timezone-aware timestamp.
    """
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


def _parse_stored_time(value: str) -> datetime:
    """Parse a stored timestamp, falling back to an aware zero. O(1)."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _encode_cursor(row: dict[str, Any]) -> str:
    """Encode a walk row's ``[requested_time, id]`` cursor in urlsafe base64. O(1)."""
    raw = json.dumps([row["requested_time"], row["id"]]).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(value: str) -> tuple[datetime, str]:
    """Decode a urlsafe base64 JSON cursor into ``(requested_time, id)``. O(1).

    Raises:
        HTTPException: 400 when the cursor is not valid base64 JSON of a
            two-element ``[timestamp, id]`` list.
    """
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
        if (
            not isinstance(decoded, list)
            or len(decoded) != 2
            or not isinstance(decoded[0], str)
            or not isinstance(decoded[1], str)
        ):
            raise ValueError("bad cursor shape")
        return _parse_stored_time(decoded[0]), decoded[1]
    except (ValueError, TypeError, binascii.Error, UnicodeDecodeError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_CURSOR_MESSAGE
        ) from None


def _walk_key(row: dict[str, Any]) -> tuple[datetime, str]:
    """The sort key of a walk row: requested time then id. O(1)."""
    return _parse_stored_time(row["requested_time"]), row["id"]


def _rows_by_request(
    supabase: Client, table: str, request_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Load rows of ``table`` for many requests in one query, grouped by request.

    O(n) in the matched rows: one ``in_`` query replaces one query per row.
    """
    unique = sorted(set(request_ids))
    if not unique:
        return {}
    rows = run_query(supabase.table(table).select("*").in_("walk_request_id", unique))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["walk_request_id"], []).append(row)
    return grouped


def _dogs_by_id(
    supabase: Client, dog_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Load dogs by id in one query. O(n)."""
    unique = sorted(set(dog_ids))
    if not unique:
        return {}
    rows = run_query(supabase.table(DOGS_TABLE).select("*").in_("id", unique))
    return {row["id"]: row for row in rows}


def _profiles_by_walker_id(
    supabase: Client, walker_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Load walker profiles by walker id in one query. O(n)."""
    unique = sorted(set(walker_ids))
    if not unique:
        return {}
    rows = run_query(
        supabase.table(PROFILES_TABLE).select("*").in_("walker_id", unique)
    )
    return {row["walker_id"]: row for row in rows}


def _agreed_price(
    row: dict[str, Any], offers: list[dict[str, Any]]
) -> int:
    """The assigned walker's offer price, else the request's asking price. O(o)."""
    walker_id = row.get("walker_id")
    for offer in offers:
        if offer.get("walker_id") == walker_id:
            return offer["price_cop"]
    return row["price_cop"]


@router.get("/owners/{ownerId}/walks", response_model=WalkListResponse)
def list_walks(
    tab: str = Query(default="upcoming"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    owner: User = Depends(owner_from_path),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """List the calling owner's walks for one tab, paginated with a cursor.

    ``upcoming`` are the ``assigned`` walks soonest first, ``active`` the
    ``in_walk`` ones, and ``past`` the ``done``/``cancelled``/``expired`` ones
    newest first. Dogs, walker names, offers and ratings are loaded in batches
    with ``in_`` so a page costs a constant number of queries.

    Args:
        tab: ``upcoming``, ``active`` or ``past``.
        limit: Page size, 1 to 50.
        cursor: Opaque urlsafe base64 cursor from a previous page.
        owner: The authenticated owner named in the path.
        supabase: The Supabase client.

    Returns:
        ``{items, nextCursor}``; ``nextCursor`` is None on the last page.

    Raises:
        HTTPException: 400 for an unknown tab, a bad limit or a bad cursor;
            500 on database errors.

    Big-O: O(n log n + n·k), n the owner's rows in the tab and k the number of
    related rows per walk, because the page is sorted in Python and the
    related rows come from a constant number of batched queries.
    """
    if tab == "upcoming":
        statuses = UPCOMING_STATUSES
        descending = False
    elif tab == "active":
        statuses = ACTIVE_STATUSES
        descending = False
    elif tab == "past":
        statuses = PAST_STATUSES
        descending = True
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_TAB_MESSAGE
        )

    cursor_key = _decode_cursor(cursor) if cursor is not None else None

    rows = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .eq("owner_id", owner.id)
        .in_("status", statuses)
    )
    rows = sorted(rows, key=_walk_key, reverse=descending)
    if cursor_key is not None:
        rows = [
            row
            for row in rows
            if (
                _walk_key(row) < cursor_key
                if descending
                else _walk_key(row) > cursor_key
            )
        ]

    page = rows[:limit]
    has_more = len(rows) > limit
    next_cursor = _encode_cursor(page[-1]) if page and has_more else None

    request_ids = [row["id"] for row in page]
    offers_by_request = _rows_by_request(supabase, OFFERS_TABLE, request_ids)
    ratings_by_request = _rows_by_request(supabase, RATINGS_TABLE, request_ids)
    dogs = _dogs_by_id(supabase, [row["dog_id"] for row in page])
    profiles = _profiles_by_walker_id(
        supabase, [row["walker_id"] for row in page if row.get("walker_id")]
    )

    items = []
    for row in page:
        offers = offers_by_request.get(row["id"], [])
        profile = profiles.get(row.get("walker_id")) or {}
        dog = dogs.get(row.get("dog_id")) or {}
        items.append(
            {
                "id": row["id"],
                "status": row["status"],
                "dogId": row.get("dog_id"),
                "dogName": dog.get("name"),
                "walkerId": row.get("walker_id"),
                "walkerName": profile.get("display_name"),
                "requestedTime": row["requested_time"],
                "durationHours": row.get("duration_hours"),
                "priceCop": _agreed_price(row, offers),
                "canRate": row["status"] == "done"
                and not ratings_by_request.get(row["id"]),
                "hasReport": row.get("report_status") is not None,
            }
        )
    return {"items": items, "nextCursor": next_cursor}


def _load_visible_walk(
    supabase: Client, request_id: str, user: User
) -> dict[str, Any]:
    """Load a walk the caller owns or is assigned to, or 404. O(1).

    The same 404 is used for an unknown id and for a foreign walk, so the
    endpoint never reveals whether someone else's walk exists.
    """
    row = first_row(
        run_query(
            supabase.table(REQUESTS_TABLE).select("*").eq("id", request_id)
        ),
        NOT_FOUND_MESSAGE,
    )
    if row.get("owner_id") != user.id and row.get("walker_id") != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_MESSAGE
        )
    return row


def _allowed_actions(
    row: dict[str, Any], user: User, now: datetime, rated: bool
) -> list[str]:
    """The actions that would work for this viewer and walk state. O(1).

    The owner's buttons follow the status, with ``no_show`` appearing only
    after ``requested_time`` plus the no-show grace period and ``rate``
    disappearing once the walk was rated. The assigned walker's buttons are
    ``check_in``/``cancel`` while assigned and ``check_out``/``send_gps``
    while walking.
    """
    is_owner = row.get("owner_id") == user.id
    walk_status = row["status"]

    if is_owner:
        if walk_status == "open":
            return ["pick", "cancel"]
        if walk_status == "assigned":
            actions = ["cancel"]
            window_start = _parse_stored_time(row["requested_time"])
            if now >= window_start + timedelta(
                minutes=DEFAULT_CONFIG.no_show_grace_min
            ):
                actions.append("no_show")
            return actions
        if walk_status == "in_walk":
            return ["track"]
        if walk_status == "done":
            actions = ["replay", "repeat"]
            if not rated:
                actions.append("rate")
            return actions
        if walk_status in ("cancelled", "expired"):
            return ["repeat"]
        return []

    if walk_status == "assigned":
        return ["check_in", "cancel"]
    if walk_status == "in_walk":
        return ["check_out", "send_gps"]
    return []


@router.get("/bidding/requests/{request_id}", response_model=WalkDetail)
def get_walk_detail(
    request_id: str,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Read one walk as its owner or its assigned walker.

    Anybody else, and any unknown id, gets the same 404 ``Walk not found.``.
    The answer carries the dog, the walker's walker-profile card with the
    all-time rating and count rebuilt from ``walk_ratings``, the agreed price
    (the walker's offer, else the asking price), the offer count, the
    timeline sorted by ``at``, the owner's rating of the walk, the stored
    report when there is one, and the exact actions the viewer may take.

    Args:
        request_id: The walk to read.
        user: The authenticated caller.
        supabase: The Supabase client.
        now: The current time, used for the no-show grace period.

    Returns:
        The walk detail in the client's camelCase shape.

    Raises:
        HTTPException: 404 for a foreign or unknown walk; 500 on database
            errors.

    Big-O: O(o log o + t log t + r), where o is the walk's offers, t its
    timeline rows and r its walker's ratings: each related list is loaded
    once and sorted in memory.
    """
    row = _load_visible_walk(supabase, request_id, user)

    dog = first_row(
        run_query(
            supabase.table(DOGS_TABLE).select("*").eq("id", row["dog_id"])
        ),
        NOT_FOUND_MESSAGE,
    )
    offers = run_query(
        supabase.table(OFFERS_TABLE)
        .select("*")
        .eq("walk_request_id", request_id)
    )

    walker_id = row.get("walker_id")
    if walker_id is not None:
        profile_rows = run_query(
            supabase.table(PROFILES_TABLE)
            .select("*")
            .eq("walker_id", walker_id)
        )
        walker_ratings = run_query(
            supabase.table(RATINGS_TABLE)
            .select("*")
            .eq("walker_id", walker_id)
        )
    else:
        profile_rows = []
        walker_ratings = []
    profile = profile_rows[0] if profile_rows else {}
    rating_values = [rating_row["rating"] for rating_row in walker_ratings]
    all_time_rating = (
        sum(rating_values) / len(rating_values) if rating_values else None
    )

    timeline_rows = run_query(
        supabase.table(TIMELINE_TABLE)
        .select("*")
        .eq("walk_request_id", request_id)
    )
    timeline_rows = sorted(
        timeline_rows, key=lambda event: _parse_stored_time(event["at"])
    )

    walk_ratings = run_query(
        supabase.table(RATINGS_TABLE)
        .select("*")
        .eq("walk_request_id", request_id)
    )
    rating = walk_ratings[0]["rating"] if walk_ratings else None

    report_status = row.get("report_status")
    report = (
        None
        if report_status is None
        else {
            "status": report_status,
            "text": row.get("report_text"),
            "distanceKm": row.get("report_distance_km"),
            "durationMin": row.get("report_duration_min"),
        }
    )

    return {
        "dog": {"id": dog.get("id"), "name": dog.get("name")},
        "walker": {
            "id": walker_id,
            "name": profile.get("display_name"),
            "allTimeRating": all_time_rating,
            "ratingCount": len(rating_values),
        },
        "priceCop": _agreed_price(row, offers),
        "offerCount": len(offers),
        "timeline": [
            {
                "kind": event["kind"],
                "at": event["at"],
                "payload": event.get("payload") or {},
            }
            for event in timeline_rows
        ],
        "rating": rating,
        "report": report,
        "allowedActions": _allowed_actions(row, user, now, rating is not None),
    }


def _check_in_epoch(supabase: Client, request_id: str, row: dict[str, Any]) -> float:
    """The epoch of the walk's check-in, falling back to its requested time. O(t)."""
    events = run_query(
        supabase.table(TIMELINE_TABLE)
        .select("*")
        .eq("walk_request_id", request_id)
        .eq("kind", "check_in")
        .order("at")
    )
    if events:
        return _parse_stored_time(events[-1]["at"]).timestamp()
    return _parse_stored_time(row["requested_time"]).timestamp()


@router.post(
    "/bidding/requests/{request_id}/gps", response_model=GpsStored
)
@limiter.limit("6/minute")
def store_gps_points(
    request: Request,
    request_id: str,
    body: GpsBatchBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Append a batch of GPS fixes to an in-progress walk (assigned walker only).

    The caller must be the walk's assigned walker and the walk must be
    ``in_walk``. Every fix must come strictly after the check-in instant and
    after the last stored fix, the batch must be strictly increasing, and no
    fix may lie more than 60 seconds in the future, so a client can never
    rewrite history or land points outside the walk.

    Args:
        request: The incoming request, read by the rate limiter.
        request_id: The walk to record.
        body: One to twenty fixes.
        user: The authenticated caller.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        ``{stored}`` — how many fixes were written.

    Raises:
        HTTPException: 400 for malformed or out-of-order fixes, 403 for a
            caller who is not the assigned walker, 404 for an unknown walk,
            409 when the walk is not in progress, 429 when the per-minute
            budget is spent, 500 on database errors.

    Big-O: O(p + t + g), p the submitted fixes, t the walk's timeline rows and
    g its stored fixes: the batch is validated in one pass after one query
    for the check-in and one for the last stored point.
    """
    row = first_row(
        run_query(
            supabase.table(REQUESTS_TABLE).select("*").eq("id", request_id)
        ),
        NOT_FOUND_MESSAGE,
    )
    if row.get("walker_id") != user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=GPS_WALKER_ONLY_MESSAGE
        )
    if row["status"] != "in_walk":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=GPS_ONLY_DURING_MESSAGE
        )

    check_in_epoch = _check_in_epoch(supabase, request_id, row)
    stored_points = run_query(
        supabase.table(GPS_POINTS_TABLE)
        .select("*")
        .eq("walk_request_id", request_id)
        .order("at_epoch")
    )
    previous_epoch = check_in_epoch
    if stored_points:
        previous_epoch = max(previous_epoch, stored_points[-1]["at_epoch"])

    future_limit = now.timestamp() + GPS_FUTURE_GRACE_SECONDS
    for point in body.points:
        at_epoch = point.atEpoch
        if at_epoch <= previous_epoch or at_epoch > future_limit:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=GPS_ORDER_MESSAGE
            )
        previous_epoch = at_epoch

    for point in body.points:
        run_query(
            supabase.table(GPS_POINTS_TABLE).insert(
                {
                    "walk_request_id": request_id,
                    "at_epoch": point.atEpoch,
                    "lat": point.lat,
                    "lng": point.lng,
                }
            )
        )
    return {"stored": len(body.points)}


@router.get("/bidding/requests/{request_id}/live", response_model=LiveResponse)
def get_live_position(
    request_id: str,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Return the latest GPS fix of an in-progress walk and its elapsed minutes.

    Only the walk's two parties may read it (everybody else gets the same 404
    as an unknown id) and only while the walk is ``in_walk`` (otherwise 409).
    The elapsed minutes are measured from the ``check_in`` timeline event.

    Args:
        request_id: The walk to locate.
        user: The authenticated caller.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        ``{position: {lat, lng, atEpoch} | None, minutesElapsed}``.

    Raises:
        HTTPException: 404 for a foreign or unknown walk, 409 when the walk is
            not in progress, 500 on database errors.

    Big-O: O(g log g + t), g the walk's GPS fixes (ordered by the database)
    and t its timeline rows.
    """
    row = _load_visible_walk(supabase, request_id, user)
    if row["status"] != "in_walk":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=NOT_IN_PROGRESS_MESSAGE
        )

    points = run_query(
        supabase.table(GPS_POINTS_TABLE)
        .select("*")
        .eq("walk_request_id", request_id)
        .order("at_epoch")
    )
    position = None
    if points:
        latest = points[-1]
        position = {
            "lat": latest["lat"],
            "lng": latest["lng"],
            "atEpoch": latest["at_epoch"],
        }

    check_in_epoch = _check_in_epoch(supabase, request_id, row)
    elapsed_seconds = max(0.0, now.timestamp() - check_in_epoch)
    return {
        "position": position,
        "minutesElapsed": int(elapsed_seconds // 60),
    }


@router.post(
    "/bidding/requests/{request_id}/repeat",
    response_model=RepeatResponse,
    status_code=status.HTTP_201_CREATED,
)
@limiter.limit("10/minute")
def repeat_walk(
    request: Request,
    request_id: str,
    body: RepeatBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Repeat a finished walk as a brand-new open request (its owner only).

    Only ``done``, ``cancelled`` and ``expired`` walks can be repeated. The
    new request copies the dog, duration, price and pickup pin, starts
    ``open`` with no walker, and takes its ``windowStart`` from the body,
    which must be in the future and at most 30 days ahead. Its bidding
    deadline is now plus ``auto_pick_after_min``.

    Args:
        request: The incoming request, read by the rate limiter.
        request_id: The finished walk to repeat.
        body: The new window's start.
        user: The authenticated caller.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        The new open request ``{id, status, dogId, priceCop, durationHours}``.

    Raises:
        HTTPException: 400 for a bad or out-of-range window, 404 for a
            foreign or unknown walk, 409 for a walk that is still running,
            429 when the per-minute budget is spent, 500 on database errors.

    Big-O: O(1) database work besides the fixed row insert.
    """
    row = first_row(
        run_query(
            supabase.table(REQUESTS_TABLE)
            .select("*")
            .eq("id", request_id)
            .eq("owner_id", user.id)
        ),
        NOT_FOUND_MESSAGE,
    )
    if row["status"] not in REPEATABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=REPEAT_STATE_MESSAGE
        )

    window_start = _parse_time(body.windowStart)
    if window_start <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=WINDOW_FUTURE_MESSAGE
        )
    if window_start > now + timedelta(days=MAX_WINDOW_AHEAD_DAYS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=WINDOW_TOO_FAR_MESSAGE
        )

    duration_hours = row.get("duration_hours") or 0.0
    try:
        window_end = window_start + timedelta(hours=duration_hours)
    except OverflowError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=WINDOW_TIME_MESSAGE
        ) from None

    rows = run_query(
        supabase.table(REQUESTS_TABLE).insert(
            {
                "owner_id": row["owner_id"],
                "walker_id": None,
                "dog_id": row["dog_id"],
                "status": "open",
                "requested_time": body.windowStart,
                "window_end": window_end.isoformat(),
                "deadline_at": (
                    now + timedelta(minutes=DEFAULT_CONFIG.auto_pick_after_min)
                ).isoformat(),
                "duration_hours": duration_hours,
                "pickup_lat": row.get("pickup_lat"),
                "pickup_lng": row.get("pickup_lng"),
                "price_cop": row["price_cop"],
            }
        )
    )
    created = first_row(rows, NOT_FOUND_MESSAGE)
    return {
        "id": created["id"],
        "status": created["status"],
        "dogId": created["dog_id"],
        "priceCop": created["price_cop"],
        "durationHours": created["duration_hours"],
    }

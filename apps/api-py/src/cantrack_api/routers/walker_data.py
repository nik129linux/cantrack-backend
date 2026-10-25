"""W5 Perfil: walk history, data export and account deletion (Ley 1581).

Every endpoint hangs from ``walker_access.walker_from_path``, so the walker id
always comes from the path guard and never from the request body. Deleting a
walker detaches them from the owners' history instead of erasing it: finished
walks keep their row with ``walker_id`` null, and assigned walks go back to the
market as ``open`` so the owner can receive new offers.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel
from supabase import Client
from supabase_auth.types import User

from ..app import limiter
from ..db import run_query
from ..deps import get_now, get_supabase
from ..walker_access import walker_from_path

router = APIRouter()

REQUESTS_TABLE = "walk_requests"
OFFERS_TABLE = "walk_offers"
RATINGS_TABLE = "walk_ratings"
DOGS_TABLE = "dogs"
OWNER_PROFILES_TABLE = "owner_profiles"
WALKER_PROFILES_TABLE = "walker_profiles"
POSITIONS_TABLE = "walker_positions"
CHECKOUTS_TABLE = "checkouts"

AVATARS_BUCKET = "avatars"

#: Colombia has no daylight saving time, so a fixed offset is exact.
BOGOTA_TZ = timezone(timedelta(hours=-5))

UPCOMING_STATUSES = ["assigned"]
ACTIVE_STATUSES = ["in_walk"]
PAST_STATUSES = ["done"]
MAX_WALKS = 50

BAD_TAB_MESSAGE = "tab must be 'upcoming', 'active' or 'past'."
CONFIRM_REQUIRED_MESSAGE = 'Send {"confirm": "DELETE"} to delete the account.'
WALK_IN_PROGRESS_MESSAGE = (
    "Finish your walk in progress before deleting the account."
)


class WalkerWalk(BaseModel):
    """One walk as the walker's history tab lists it."""

    id: str
    dogName: str | None = None
    ownerName: str | None = None
    status: str
    requestedTime: str
    durationHours: float
    priceCop: int
    rating: float | None = None


class ExportResponse(BaseModel):
    """The walker's portable data snapshot (Ley 1581 export)."""

    exportedAt: str
    profile: dict[str, Any] | None = None
    position: dict[str, Any] | None = None
    offers: list[dict[str, Any]] = []
    walks: list[dict[str, Any]] = []
    ratingsReceived: list[dict[str, Any]] = []


class DeleteAccountBody(BaseModel):
    """Body of ``DELETE /walkers/{walkerId}``."""

    confirm: str | None = None


def _walk_key(row: dict[str, Any]) -> tuple[datetime, str]:
    """Return the sort key of a walk: requested time then id. O(1)."""
    try:
        parsed = datetime.fromisoformat(row["requested_time"])
    except (KeyError, TypeError, ValueError):
        parsed = datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BOGOTA_TZ)
    return parsed, row["id"]


def _dogs_by_id(supabase: Client, dog_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Load dogs by id in one query. O(n) in the matched dogs."""
    unique = sorted(set(dog_ids))
    if not unique:
        return {}
    rows = run_query(supabase.table(DOGS_TABLE).select("*").in_("id", unique))
    return {row["id"]: row for row in rows}


def _owners_by_id(
    supabase: Client, owner_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Load owner profiles by owner id in one query. O(n) in the matched rows."""
    unique = sorted(set(owner_ids))
    if not unique:
        return {}
    rows = run_query(
        supabase.table(OWNER_PROFILES_TABLE)
        .select("*")
        .in_("owner_id", unique)
    )
    return {row["owner_id"]: row for row in rows}


def _my_offers_by_request(
    supabase: Client, walker_id: str, request_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Load the caller's offers for many requests in one query. O(n)."""
    unique = sorted(set(request_ids))
    if not unique:
        return {}
    rows = run_query(
        supabase.table(OFFERS_TABLE)
        .select("*")
        .eq("walker_id", walker_id)
        .in_("walk_request_id", unique)
    )
    return {row["walk_request_id"]: row for row in rows}


def _ratings_by_request(
    supabase: Client, walker_id: str, request_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Load the caller's ratings for many requests in one query. O(n)."""
    unique = sorted(set(request_ids))
    if not unique:
        return {}
    rows = run_query(
        supabase.table(RATINGS_TABLE)
        .select("*")
        .eq("walker_id", walker_id)
        .in_("walk_request_id", unique)
    )
    return {row["walk_request_id"]: row for row in rows}


def _profile_view(
    walker_id: str, row: dict[str, Any] | None
) -> dict[str, Any]:
    """Translate a stored walker profile to the export shape. O(1)."""
    if row is None:
        return {"walkerId": walker_id, "displayName": None}
    version = row.get("consent_version")
    return {
        "walkerId": walker_id,
        "displayName": row.get("display_name"),
        "bio": row.get("bio"),
        "serviceArea": row.get("service_area"),
        "pricePerWalk": row.get("price_per_walk"),
        "phone": row.get("phone"),
        "avatarPath": row.get("avatar_path"),
        "consent": (
            {"version": version, "at": row.get("consent_at")}
            if version is not None
            else None
        ),
    }


def _position_view(
    row: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Translate a walker position row to the export shape. O(1)."""
    if row is None:
        return None
    return {
        "lat": row.get("lat"),
        "lng": row.get("lng"),
        "updatedAt": row.get("updated_at"),
    }


def _export_walk(row: dict[str, Any]) -> dict[str, Any]:
    """One walk as the walker's own record: no pickup point (the owner's home). O(1)."""
    return {
        "id": row["id"],
        "status": row.get("status"),
        "dogId": row.get("dog_id"),
        "requestedTime": row.get("requested_time"),
        "durationHours": row.get("duration_hours"),
        "priceCop": row.get("price_cop"),
    }


@router.get("/walkers/{walkerId}/walks", response_model=list[WalkerWalk])
def list_walker_walks(
    tab: str = Query(default="upcoming"),
    user: User = Depends(walker_from_path),
    supabase: Client = Depends(get_supabase),
) -> list[dict[str, Any]]:
    """List the calling walker's walks for one tab, at most 50.

    ``upcoming`` are the ``assigned`` walks soonest first, ``active`` the
    ``in_walk`` ones, and ``past`` the ``done`` ones newest first. Dogs, owner
    names, the caller's offers and ratings are loaded with batched ``in_``
    queries, and the agreed price is the caller's own offer price falling
    back to the request's asking price.

    Args:
        tab: ``upcoming``, ``active`` or ``past``.
        user: The authenticated walker named in the path.
        supabase: The Supabase client used to read the rows.

    Returns:
        The tab's walks in the client's camelCase shape.

    Raises:
        HTTPException: 400 for an unknown tab, 403 for another account, 500 on
            database errors.

    Big-O: O(n log n + n·k), n the caller's walks in the tab and k the related
    rows per walk; the page is sorted in Python and the related rows come from
    a constant number of batched queries.
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

    rows = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .eq("walker_id", user.id)
        .in_("status", statuses)
    )
    rows = sorted(rows, key=_walk_key, reverse=descending)[:MAX_WALKS]

    request_ids = [row["id"] for row in rows]
    dogs = _dogs_by_id(supabase, [row["dog_id"] for row in rows])
    owners = _owners_by_id(supabase, [row["owner_id"] for row in rows])
    offers = _my_offers_by_request(supabase, user.id, request_ids)
    ratings = _ratings_by_request(supabase, user.id, request_ids)

    items: list[dict[str, Any]] = []
    for row in rows:
        dog = dogs.get(row.get("dog_id")) or {}
        owner = owners.get(row.get("owner_id")) or {}
        offer = offers.get(row["id"]) or {}
        rating = ratings.get(row["id"]) or {}
        items.append(
            {
                "id": row["id"],
                "dogName": dog.get("name"),
                "ownerName": owner.get("display_name"),
                "status": row["status"],
                "requestedTime": row["requested_time"],
                "durationHours": row.get("duration_hours"),
                "priceCop": offer.get("price_cop", row["price_cop"]),
                "rating": rating.get("rating"),
            }
        )
    return items


@router.get("/walkers/{walkerId}/export", response_model=ExportResponse)
@limiter.limit("2/minute")
def export_walker_data(
    request: Request,
    response: Response,
    user: User = Depends(walker_from_path),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Export every row the caller owns as portable JSON. O(n).

    The snapshot contains only rows keyed by the caller's walker id: their
    profile, position, offers, walks and the ratings they received. Dog
    embeddings and owner emails are never part of it.

    Args:
        request: The incoming request, read by the rate limiter.
        response: The outgoing response, stamped with the download header.
        user: The authenticated walker named in the path.
        supabase: The Supabase client used to read the rows.
        now: The current time, stamped as ``exportedAt``.

    Returns:
        ``{exportedAt, profile, position, offers, walks, ratingsReceived}``
        scoped to the caller.

    Raises:
        HTTPException: 403 for another account, 429 when the per-minute budget
            is spent, 500 on database errors.

    Big-O: O(n) in the caller's rows across the five tables.
    """
    walker_id = user.id
    profile_rows = run_query(
        supabase.table(WALKER_PROFILES_TABLE)
        .select("*")
        .eq("walker_id", walker_id)
    )
    position_rows = run_query(
        supabase.table(POSITIONS_TABLE).select("*").eq("walker_id", walker_id)
    )
    offers = run_query(
        supabase.table(OFFERS_TABLE).select("*").eq("walker_id", walker_id)
    )
    walks = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .eq("walker_id", walker_id)
        .order("requested_time", desc=True)
    )
    ratings = run_query(
        supabase.table(RATINGS_TABLE)
        .select("*")
        .eq("walker_id", walker_id)
        .order("created_at", desc=True)
    )
    response.headers["Content-Disposition"] = (
        'attachment; filename="cantrack-export.json"'
    )
    return {
        "exportedAt": now.isoformat(),
        "profile": _profile_view(
            walker_id, profile_rows[0] if profile_rows else None
        ),
        "position": _position_view(position_rows[0] if position_rows else None),
        "offers": offers,
        "walks": [_export_walk(walk) for walk in walks],
        "ratingsReceived": ratings,
    }


@router.delete("/walkers/{walkerId}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("2/minute")
def delete_walker_account(
    request: Request,
    user: User = Depends(walker_from_path),
    supabase: Client = Depends(get_supabase),
    body: DeleteAccountBody | None = None,
) -> Response:
    """Erase the caller's account after detaching them from the owners' walks.

    The confirmation body must be exactly ``{"confirm": "DELETE"}``. Deletion
    is refused while one of the caller's walks is in progress. Otherwise the
    assigned walks go back to ``open`` with no walker, finished walks keep
    their history with ``walker_id`` null, and the caller's offers, position,
    profile row, avatar object and login are removed.

    Args:
        request: The incoming request, read by the rate limiter.
        user: The authenticated walker named in the path.
        supabase: The Supabase client used to read and delete the rows.
        body: ``{"confirm": "DELETE"}``, the explicit human gate.

    Returns:
        An empty 204 once everything the caller owns is gone.

    Raises:
        HTTPException: 400 without the exact confirmation, 403 for another
            account, 409 while a walk is in progress, 429 when the per-minute
            budget is spent, 500 on database errors.

    Big-O: O(w + o), w the caller's walks and o their offers.
    """
    if body is None or body.confirm != "DELETE":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=CONFIRM_REQUIRED_MESSAGE,
        )

    walker_id = user.id
    walks = run_query(
        supabase.table(REQUESTS_TABLE).select("*").eq("walker_id", walker_id)
    )
    if any(walk.get("status") == "in_walk" for walk in walks):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=WALK_IN_PROGRESS_MESSAGE,
        )

    profile_rows = run_query(
        supabase.table(WALKER_PROFILES_TABLE)
        .select("*")
        .eq("walker_id", walker_id)
    )
    avatar_path = (
        profile_rows[0].get("avatar_path") if profile_rows else None
    ) or None

    run_query(
        supabase.table(REQUESTS_TABLE)
        .update({"status": "open", "walker_id": None})
        .eq("walker_id", walker_id)
        .in_("status", ["assigned"])
    )
    # Every other walk (done, cancelled, expired, legacy) and every walk report stays with its
    # owner, detached from me. Migration 005 makes the FKs ON DELETE SET NULL as the DB-side guard.
    run_query(
        supabase.table(REQUESTS_TABLE)
        .update({"walker_id": None})
        .eq("walker_id", walker_id)
    )
    run_query(
        supabase.table(CHECKOUTS_TABLE)
        .update({"walker_id": None})
        .eq("walker_id", walker_id)
    )
    run_query(
        supabase.table(OFFERS_TABLE).delete().eq("walker_id", walker_id)
    )
    run_query(
        supabase.table(POSITIONS_TABLE).delete().eq("walker_id", walker_id)
    )
    run_query(
        supabase.table(WALKER_PROFILES_TABLE)
        .delete()
        .eq("walker_id", walker_id)
    )
    if avatar_path:
        supabase.storage.from_(AVATARS_BUCKET).remove([avatar_path])
    supabase.auth.admin.delete_user(walker_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)

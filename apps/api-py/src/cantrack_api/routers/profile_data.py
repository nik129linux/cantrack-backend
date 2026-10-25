"""Perfil: data export and account deletion, Ley 1581 (PR4)."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from supabase import Client
from supabase_auth.types import User

from ..app import limiter
from ..db import run_query
from ..deps import get_now, get_supabase
from ..owner_access import owner_from_path

router = APIRouter()

PROFILES_TABLE = "owner_profiles"
DOGS_TABLE = "dogs"
REQUESTS_TABLE = "walk_requests"
RATINGS_TABLE = "walk_ratings"

AVATARS_BUCKET = "avatars"
EMBEDDING_COLUMN = "embedding"

CONFIRM_REQUIRED_MESSAGE = 'Send {"confirm": "DELETE"} to delete the account.'
WALK_IN_PROGRESS_MESSAGE = "Finish your walk in progress before deleting the account."


class DeleteAccountBody(BaseModel):
    """Body of ``DELETE /owners/{ownerId}``."""

    confirm: str | None = None


class ExportResponse(BaseModel):
    """The owner's portable data snapshot (Ley 1581 export)."""

    exportedAt: str
    profile: dict[str, Any] | None = None
    dogs: list[dict[str, Any]] = []
    walkRequests: list[dict[str, Any]] = []
    ratingsGiven: list[dict[str, Any]] = []


def _strip_embedding(row: dict[str, Any]) -> dict[str, Any]:
    """Return a dog row without its embedding column. O(k)."""
    return {key: value for key, value in row.items() if key != EMBEDDING_COLUMN}


def _profile_view(owner_id: str, row: dict[str, Any] | None) -> dict[str, Any]:
    """Translate a stored owner profile row to the export shape. O(1)."""
    if row is None:
        return {"ownerId": owner_id, "displayName": None, "phone": None}
    return {
        "ownerId": owner_id,
        "displayName": row.get("display_name"),
        "phone": row.get("phone"),
        "avatarPath": row.get("avatar_path"),
        "home": (
            {"lat": row["home_lat"], "lng": row["home_lng"], "label": row.get("home_label")}
            if row.get("home_lat") is not None
            else None
        ),
        "consent": (
            {"version": row["consent_version"], "at": row.get("consent_at")}
            if row.get("consent_version")
            else None
        ),
    }


@router.get("/owners/{ownerId}/export", response_model=ExportResponse)
@limiter.limit("2/minute")
def export_owner_data(
    request: Request,
    response: Response,
    user: User = Depends(owner_from_path),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Export every row the caller owns as portable JSON. O(d + w + r).

    Args:
        request: The incoming request, read by the rate limiter.
        response: The outgoing response, stamped with the download header.
        user: The authenticated owner named in the path.
        supabase: The Supabase client used to read the rows.
        now: The current time, stamped as ``exportedAt``.

    Returns:
        ``{exportedAt, profile, dogs, walkRequests, ratingsGiven}`` scoped to
        the caller, with no embedding column anywhere.
    """
    owner_id = user.id
    profile_rows = run_query(
        supabase.table(PROFILES_TABLE).select("*").eq("owner_id", owner_id)
    )
    profile = _profile_view(owner_id, profile_rows[0] if profile_rows else None)
    dogs = run_query(supabase.table(DOGS_TABLE).select("*").eq("owner_id", owner_id))
    walks = run_query(
        supabase.table(REQUESTS_TABLE).select("*").eq("owner_id", owner_id)
    )
    walk_ids = [walk["id"] for walk in walks if "id" in walk]
    ratings: list[dict[str, Any]] = []
    if walk_ids:
        ratings = run_query(
            supabase.table(RATINGS_TABLE).select("*").in_("walk_request_id", walk_ids)
        )
    response.headers["Content-Disposition"] = 'attachment; filename="cantrack-export.json"'
    return {
        "exportedAt": now.isoformat(),
        "profile": profile,
        "dogs": [_strip_embedding(dog) for dog in dogs],
        "walkRequests": walks,
        "ratingsGiven": ratings,
    }


@router.delete("/owners/{ownerId}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("2/minute")
def delete_owner_account(
    request: Request,
    user: User = Depends(owner_from_path),
    supabase: Client = Depends(get_supabase),
    body: DeleteAccountBody | None = None,
) -> Response:
    """Erase the caller's account and login after cancelling open walks. O(w + d).

    Args:
        request: The incoming request, read by the rate limiter.
        user: The authenticated owner named in the path.
        supabase: The Supabase client used to read and delete the rows.
        body: ``{"confirm": "DELETE"}``, the explicit human gate.

    Returns:
        An empty 204 once the dogs, profile, avatar and login are gone.
    """
    if body is None or body.confirm != "DELETE":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=CONFIRM_REQUIRED_MESSAGE,
        )
    owner_id = user.id
    walks = run_query(
        supabase.table(REQUESTS_TABLE).select("*").eq("owner_id", owner_id)
    )
    if any(walk.get("status") == "in_walk" for walk in walks):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=WALK_IN_PROGRESS_MESSAGE,
        )
    profile_rows = run_query(
        supabase.table(PROFILES_TABLE).select("*").eq("owner_id", owner_id)
    )
    avatar_path = (profile_rows[0].get("avatar_path") if profile_rows else None) or None
    run_query(
        supabase.table(REQUESTS_TABLE)
        .update({"status": "cancelled"})
        .eq("owner_id", owner_id)
        .in_("status", ["open", "assigned"])
    )
    run_query(supabase.table(DOGS_TABLE).delete().eq("owner_id", owner_id))
    run_query(supabase.table(PROFILES_TABLE).delete().eq("owner_id", owner_id))
    if avatar_path:
        supabase.storage.from_(AVATARS_BUCKET).remove([avatar_path])
    supabase.auth.admin.delete_user(owner_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


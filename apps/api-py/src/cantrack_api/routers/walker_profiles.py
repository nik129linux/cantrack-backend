"""S1: walker profiles — the public card data owners pick a walker from.

``PUT /walker-profile`` upserts the caller's own profile and is reserved to
the walker role; ``GET /walker-profiles`` is the catalog any authenticated
user may browse (Trie search, Quadtree "near my pin" and ratings are S4).
The wire format is camelCase like routes; storage is snake_case like dogs.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client
from supabase_auth.types import User

from ..db import first_row, run_query
from ..deps import get_current_user, get_supabase
from ..schemas import SaveWalkerProfileBody

router = APIRouter()

PROFILES_TABLE = "walker_profiles"
ROLE_MESSAGE = "Only walkers can save a walker profile."
SAVED_MESSAGE = "Walker profile not found."


def _role(user: User) -> str | None:
    """Read the role out of the Supabase user metadata (walker | owner)."""
    metadata = getattr(user, "user_metadata", None) or {}
    return metadata.get("role")


def _view(row: dict[str, Any]) -> dict[str, Any]:
    """Translate a stored profile row to the camelCase API shape."""
    return {
        "walkerId": row["walker_id"],
        "displayName": row["display_name"],
        "bio": row.get("bio"),
        "serviceArea": row.get("service_area"),
        "pricePerWalk": row["price_per_walk"],
    }


@router.put("/walker-profile")
def save_walker_profile(
    body: SaveWalkerProfileBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Create or replace the caller's walker profile (upsert by walker_id).

    Args:
        body: The public card fields; the price is a non-negative COP integer.
        user: The authenticated walker.
        supabase: The Supabase client used to read and write the row.

    Returns:
        The saved profile in the camelCase API shape.

    Raises:
        HTTPException: 400 if the caller is not a walker; 500 if the database
            rejects a query.
    """
    if _role(user) != "walker":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ROLE_MESSAGE)

    row = {
        "display_name": body.displayName,
        "bio": body.bio,
        "service_area": body.serviceArea,
        "price_per_walk": body.pricePerWalk,
    }
    existing = run_query(
        supabase.table(PROFILES_TABLE).select("walker_id").eq("walker_id", user.id)
    )
    if existing:
        rows = run_query(
            supabase.table(PROFILES_TABLE).update(row).eq("walker_id", user.id)
        )
    else:
        rows = run_query(
            supabase.table(PROFILES_TABLE).insert({"walker_id": user.id, **row})
        )
    return _view(first_row(rows, SAVED_MESSAGE))


@router.get("/walker-profiles")
def list_walker_profiles(
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> list[dict[str, Any]]:
    """List every walker profile, oldest first (stable catalog order).

    Args:
        user: Any authenticated user (owners browse it to pick a walker).
        supabase: The Supabase client used to read the rows.

    Returns:
        The catalog in the camelCase API shape, possibly empty.

    Raises:
        HTTPException: 500 if the database cannot be read.
    """
    rows = run_query(supabase.table(PROFILES_TABLE).select("*").order("created_at"))
    return [_view(row) for row in rows]

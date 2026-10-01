"""S2: ``GET /clients`` — the walker's "My clients".

A client is a dog with an ACCEPTED request addressed to the caller, carrying
the pin, the time and the full questionnaire: accepting already opened the S1
privacy gate, so this endpoint reveals nothing new — it only ever reads rows
scoped to ``walker_id = caller`` and ``status = accepted``. Privacy follows the
status: pending, declined and cancelled requests (including a request
cancelled after having been accepted) are not clients.

One item per accepted REQUEST (the same dog with walks on two days appears
twice), ordered by requested time and then request id. Timestamps come from
the database, which normalizes every timestamptz to UTC — nothing here echoes
client-sent strings.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client
from supabase_auth.types import User

from ..db import run_query
from ..deps import get_current_user, get_supabase

router = APIRouter(prefix="/clients")

REQUESTS_TABLE = "walk_requests"
DOGS_TABLE = "dogs"

ROLE_MESSAGE = "Only walkers have clients."

REQUEST_COLUMNS = (
    "id,owner_id,walker_id,dog_id,status,requested_time,"
    "pickup_lat,pickup_lng,price_cop,created_at,responded_at"
)


def _role(user: User) -> str | None:
    metadata = getattr(user, "user_metadata", None) or {}
    return metadata.get("role")


def profile_to_api(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert a stored snake_case questionnaire to the camelCase wire form."""
    if profile is None:
        return None
    return {
        "size": profile.get("size"),
        "temperament": profile.get("temperament"),
        "energy": profile.get("energy"),
        "leashTrained": profile.get("leash_trained"),
        "allergies": profile.get("allergies"),
        "medicalNotes": profile.get("medical_notes"),
        "vetContact": profile.get("vet_contact"),
        "emergencyContact": profile.get("emergency_contact"),
    }


@router.get("")
def list_clients(
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> list[dict[str, Any]]:
    """Return the caller's clients: one item per accepted request.

    Args:
        user: The authenticated walker.
        supabase: The Supabase client (dependency-injected, faked in tests).

    Returns:
        The client list, ordered by requested time then request id.

    Raises:
        HTTPException: 400 for a non-walker caller, 500 on database errors.
    """
    if _role(user) != "walker":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ROLE_MESSAGE)

    rows = run_query(
        supabase.table(REQUESTS_TABLE)
        .select(REQUEST_COLUMNS)
        .eq("walker_id", user.id)
        .eq("status", "accepted")
    )

    dog_ids = sorted({row["dog_id"] for row in rows})
    dogs: dict[str, dict[str, Any]] = {}
    if dog_ids:
        dog_rows = run_query(
            supabase.table(DOGS_TABLE).select("id,name,profile").in_("id", dog_ids)
        )
        dogs = {dog["id"]: dog for dog in dog_rows}

    rows.sort(key=lambda row: (row["requested_time"], row["id"]))

    clients: list[dict[str, Any]] = []
    for row in rows:
        dog = dogs.get(row["dog_id"], {})
        clients.append(
            {
                "requestId": row["id"],
                "dogId": row["dog_id"],
                "dogName": dog.get("name"),
                "requestedTime": row["requested_time"],
                "pickupLat": row["pickup_lat"],
                "pickupLng": row["pickup_lng"],
                "profile": profile_to_api(dog.get("profile")),
            }
        )
    return clients

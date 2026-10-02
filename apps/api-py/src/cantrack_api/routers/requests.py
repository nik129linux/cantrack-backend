"""S1: walk requests — the marketplace flow from owner to walker.

Roles and privacy (docs/superpowers/specs/2026-09-30): the owner creates a
request (dog + walker + time + pin) priced from the walker's profile; the
walker's inbox lists the requests addressed to them, pending oldest first
through the hand-written Queue; accept/decline are walker-only, cancel is
owner-only, and every query is scoped by the caller's id, so a foreign row is
a 404 that reveals nothing.

The walker's view is derived from the request STATUS: only ``accepted`` opens
the full view (pin, whole questionnaire, contacts); ``pending``, ``declined``
and ``cancelled`` — including a request cancelled after having been accepted —
return the limited view (dog name, breed, size, temperament).

Two rules share one window definition: a walk occupies
``[requested_time, requested_time + 60 min)``, half-open. Creating a request
whose window overlaps another pending|accepted request for the SAME dog (with
any walker) is a 409 double-booking; accepting a request flags (does not
block) the overlaps against the walker's other accepted requests, found with
the hand-written IntervalTree (FR-17).
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client
from supabase_auth.types import User

from ..db import first_row, run_query
from ..deps import get_current_user, get_now, get_supabase
from ..schemas import CreateRequestBody
from ..structures import IntervalTree, Queue

router = APIRouter(prefix="/requests")

REQUESTS_TABLE = "walk_requests"
DOGS_TABLE = "dogs"
PROFILES_TABLE = "walker_profiles"
CHECKOUTS_TABLE = "checkouts"

NOT_FOUND_MESSAGE = "Request not found."
DOG_NOT_FOUND_MESSAGE = "Dog not found."
PROFILE_NOT_FOUND_MESSAGE = "Walker profile not found."
OWNER_ROLE_MESSAGE = "Only owners can create a walk request."
ROLE_MESSAGE = "Unsupported role."
TIME_MESSAGE = "requestedTime must be an ISO-8601 datetime with a timezone offset."
FUTURE_MESSAGE = "requestedTime must be in the future."
DOUBLE_BOOKED_MESSAGE = "This dog already has an active request for that time."
FLOOD_MESSAGE = "Too many pending requests."
ACCEPT_STATE_MESSAGE = "Only a pending request can be accepted."
DECLINE_STATE_MESSAGE = "Only a pending request can be declined."
CANCEL_STATE_MESSAGE = "Only a pending or accepted request can be cancelled."
COMPLETED_CANCEL_MESSAGE = "A completed request cannot be cancelled."

#: A walk occupies [requested_time, requested_time + WALK_MINUTES), half-open,
#: so back-to-back walks never conflict.
WALK_MINUTES = 60

#: How many pending requests one owner may hold at once (flood guard; the AI
#: quota with 429 is a different, S3 rule).
PENDING_LIMIT = 10

ACTIVE_STATUSES = ["pending", "accepted"]


def _role(user: User) -> str | None:
    metadata = getattr(user, "user_metadata", None) or {}
    return metadata.get("role")


def _parse_time(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, refusing naive values (400, exact message)."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=TIME_MESSAGE
        ) from None
    if parsed.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=TIME_MESSAGE
        )
    return parsed


def _utc_key(value: str) -> str:
    """Normalise a timestamp to UTC ISO so tree keys compare lexicographically."""
    return _parse_time(value).astimezone(timezone.utc).isoformat()


def _walk_window(requested_time: str) -> tuple[str, str]:
    """The half-open [start, end) window a walk occupies, in UTC ISO keys."""
    start = _parse_time(requested_time).astimezone(timezone.utc)
    return start.isoformat(), (start + timedelta(minutes=WALK_MINUTES)).isoformat()


def _windows_overlap(time_a: str, time_b: str) -> bool:
    """Whether two walks' 60-minute half-open windows overlap."""
    start_a = _parse_time(time_a)
    start_b = _parse_time(time_b)
    return start_a < start_b + timedelta(minutes=WALK_MINUTES) and start_b < (
        start_a + timedelta(minutes=WALK_MINUTES)
    )


def _dogs_by_id(supabase: Client, dog_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Load the dogs a set of request rows point at, keyed by id."""
    unique = sorted(set(dog_ids))
    if not unique:
        return {}
    rows = run_query(
        supabase.table(DOGS_TABLE).select("id,name,breed,profile").in_("id", unique)
    )
    return {row["id"]: row for row in rows}


def _profiles_by_walker_id(
    supabase: Client, walker_ids: list[str]
) -> dict[str, dict[str, Any]]:
    """Load the walker profiles a set of request rows point at."""
    unique = sorted(set(walker_ids))
    if not unique:
        return {}
    rows = run_query(supabase.table(PROFILES_TABLE).select("*").in_("walker_id", unique))
    return {row["walker_id"]: row for row in rows}


def _owner_view(
    row: dict[str, Any], walker_name: str | None, dog_name: str | None
) -> dict[str, Any]:
    """The full request as its owner sees it (it is all their own data)."""
    return {
        "id": row["id"],
        "walkerId": row["walker_id"],
        "walkerName": walker_name,
        "dogId": row["dog_id"],
        "dogName": dog_name,
        "status": row["status"],
        "requestedTime": row["requested_time"],
        "pickupLat": row["pickup_lat"],
        "pickupLng": row["pickup_lng"],
        "priceCop": row["price_cop"],
        "createdAt": row["created_at"],
        "respondedAt": row.get("responded_at"),
    }


def _walker_view(row: dict[str, Any], dog_row: dict[str, Any]) -> dict[str, Any]:
    """The request as the walker sees it, gated by STATUS.

    Only an ``accepted`` request carries the pin and the full questionnaire;
    every other status (pending, declined, cancelled — even one cancelled
    after having been accepted) returns the limited dog view.
    """
    questionnaire = dog_row.get("profile") or {}
    dog = {
        "name": dog_row.get("name"),
        "breed": dog_row.get("breed"),
        "size": questionnaire.get("size"),
        "temperament": questionnaire.get("temperament"),
    }
    view: dict[str, Any] = {
        "id": row["id"],
        "status": row["status"],
        "requestedTime": row["requested_time"],
        "priceCop": row["price_cop"],
        "createdAt": row["created_at"],
        "dog": dog,
    }
    if row["status"] == "accepted":
        view["respondedAt"] = row.get("responded_at")
        view["pickupLat"] = row["pickup_lat"]
        view["pickupLng"] = row["pickup_lng"]
        view["dog"] = {
            **dog,
            "energy": questionnaire.get("energy"),
            "leashTrained": questionnaire.get("leash_trained"),
            "allergies": questionnaire.get("allergies"),
            "medicalNotes": questionnaire.get("medical_notes"),
            "vetContact": questionnaire.get("vet_contact"),
            "emergencyContact": questionnaire.get("emergency_contact"),
        }
    return view


@router.post("", status_code=status.HTTP_201_CREATED)
def create_request(
    body: CreateRequestBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Create a pending walk request (owners only).

    The dog must be the caller's (404 otherwise), the walker must have a
    profile (404), the time must be ISO-8601 with offset (400) and not in the
    past (400, against the overridable clock). The price is copied from the
    walker's profile — a price in the body is dropped by the schema. Two guards
    follow: the same dog cannot hold an overlapping pending|accepted request
    with ANY walker (409), and an owner may hold at most ``PENDING_LIMIT``
    pending requests (400).

    Args:
        body: walker, dog, time and pin.
        user: The authenticated owner.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        The stored request in the owner's full view.

    Raises:
        HTTPException: 400 (role, validation, past time, flood), 404 (dog or
            profile), 409 (double booking), 500 (database).
    """
    if _role(user) != "owner":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=OWNER_ROLE_MESSAGE
        )

    dog = first_row(
        run_query(
            supabase.table(DOGS_TABLE)
            .select("*")
            .eq("id", body.dogId)
            .eq("owner_id", user.id)
        ),
        DOG_NOT_FOUND_MESSAGE,
    )
    profile = first_row(
        run_query(
            supabase.table(PROFILES_TABLE).select("*").eq("walker_id", body.walkerId)
        ),
        PROFILE_NOT_FOUND_MESSAGE,
    )

    requested = _parse_time(body.requestedTime)
    if requested < now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=FUTURE_MESSAGE
        )

    active = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .eq("dog_id", body.dogId)
        .eq("owner_id", user.id)
        .in_("status", ACTIVE_STATUSES)
    )
    if any(
        _windows_overlap(row["requested_time"], body.requestedTime) for row in active
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=DOUBLE_BOOKED_MESSAGE
        )

    pending = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .eq("owner_id", user.id)
        .eq("status", "pending")
    )
    if len(pending) >= PENDING_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=FLOOD_MESSAGE
        )

    rows = run_query(
        supabase.table(REQUESTS_TABLE).insert(
            {
                "owner_id": user.id,
                "walker_id": body.walkerId,
                "dog_id": body.dogId,
                "status": "pending",
                "requested_time": body.requestedTime,
                "pickup_lat": body.pickupLat,
                "pickup_lng": body.pickupLng,
                "price_cop": profile["price_per_walk"],
                "responded_at": None,
            }
        )
    )
    created = first_row(rows, NOT_FOUND_MESSAGE)
    return _owner_view(created, profile["display_name"], dog["name"])


@router.get("")
def list_requests(
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> list[dict[str, Any]]:
    """List the caller's requests, scoped and ordered per role.

    Walker: the inbox — pending requests oldest first (buffered through the
    hand-written Queue after sorting by created_at, so storage order never
    leaks into the result), then the resolved ones oldest first. Owner: their
    sent requests, newest first.

    Args:
        user: The authenticated caller (walker or owner).
        supabase: The Supabase client.

    Returns:
        The role-appropriate views of the caller's requests.

    Raises:
        HTTPException: 400 for a caller without a walker/owner role; 500 on
            database errors.
    """
    role = _role(user)

    if role == "walker":
        rows = run_query(
            supabase.table(REQUESTS_TABLE).select("*").eq("walker_id", user.id)
        )
        dogs = _dogs_by_id(supabase, [row["dog_id"] for row in rows])
        pending = sorted(
            (row for row in rows if row["status"] == "pending"),
            key=lambda row: row["created_at"],
        )
        resolved = sorted(
            (row for row in rows if row["status"] != "pending"),
            key=lambda row: row["created_at"],
        )
        queue: Queue[dict[str, Any]] = Queue()
        for row in pending:
            queue.enqueue(row)
        ordered: list[dict[str, Any]] = []
        while not queue.is_empty():
            ordered.append(queue.dequeue())
        ordered.extend(resolved)
        return [_walker_view(row, dogs.get(row["dog_id"], {})) for row in ordered]

    if role == "owner":
        rows = run_query(
            supabase.table(REQUESTS_TABLE).select("*").eq("owner_id", user.id)
        )
        rows = sorted(rows, key=lambda row: row["created_at"], reverse=True)
        dogs = _dogs_by_id(supabase, [row["dog_id"] for row in rows])
        profiles = _profiles_by_walker_id(supabase, [row["walker_id"] for row in rows])
        return [
            _owner_view(
                row,
                (profiles.get(row["walker_id"]) or {}).get("display_name"),
                (dogs.get(row["dog_id"]) or {}).get("name"),
            )
            for row in rows
        ]

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ROLE_MESSAGE)


@router.get("/{request_id}")
def get_request(
    request_id: uuid.UUID,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Read one request, scoped by role: the owner's full view, or the
    walker's status-gated view. Anybody else gets a 404 that reveals nothing.

    Args:
        request_id: The request to read.
        user: The authenticated caller.
        supabase: The Supabase client.

    Returns:
        The role-appropriate view of the request.

    Raises:
        HTTPException: 400 (non-uuid handled by FastAPI; unknown role), 404
            (foreign or unknown row), 500 (database).
    """
    role = _role(user)

    if role == "walker":
        row = first_row(
            run_query(
                supabase.table(REQUESTS_TABLE)
                .select("*")
                .eq("id", request_id)
                .eq("walker_id", user.id)
            ),
            NOT_FOUND_MESSAGE,
        )
        dogs = _dogs_by_id(supabase, [row["dog_id"]])
        return _walker_view(row, dogs.get(row["dog_id"], {}))

    if role == "owner":
        row = first_row(
            run_query(
                supabase.table(REQUESTS_TABLE)
                .select("*")
                .eq("id", request_id)
                .eq("owner_id", user.id)
            ),
            NOT_FOUND_MESSAGE,
        )
        dogs = _dogs_by_id(supabase, [row["dog_id"]])
        profiles = _profiles_by_walker_id(supabase, [row["walker_id"]])
        return _owner_view(
            row,
            (profiles.get(row["walker_id"]) or {}).get("display_name"),
            (dogs.get(row["dog_id"]) or {}).get("name"),
        )

    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ROLE_MESSAGE)


def _load_for_walker(
    supabase: Client, request_id: uuid.UUID, user: User
) -> dict[str, Any]:
    """Fetch the request addressed to this walker, or 404."""
    return first_row(
        run_query(
            supabase.table(REQUESTS_TABLE)
            .select("*")
            .eq("id", request_id)
            .eq("walker_id", user.id)
        ),
        NOT_FOUND_MESSAGE,
    )


@router.post("/{request_id}/accept")
def accept_request(
    request_id: uuid.UUID,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Accept a pending request (the addressed walker only).

    Accepting succeeds even when it overlaps other accepted walks — the
    conflicts are FLAGGED, not blocked (FR-17): the walker's other accepted
    requests are loaded into the hand-written IntervalTree keyed by their
    60-minute half-open windows, and the overlaps come back sorted by
    requested time.

    Args:
        request_id: The request to accept.
        user: The authenticated walker.
        supabase: The Supabase client.
        now: The current time, stamped as respondedAt.

    Returns:
        ``{id, status: "accepted", respondedAt, conflicts: [{requestId,
        requestedTime}]}``.

    Raises:
        HTTPException: 400 (non-pending), 404 (foreign/unknown), 500.
    """
    row = _load_for_walker(supabase, request_id, user)
    if row["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=ACCEPT_STATE_MESSAGE
        )

    tree: IntervalTree[tuple[str, str]] = IntervalTree()
    accepted = run_query(
        supabase.table(REQUESTS_TABLE)
        .select("*")
        .eq("walker_id", user.id)
        .eq("status", "accepted")
    )
    for other in accepted:
        start, end = _walk_window(other["requested_time"])
        tree.insert(start, end, (other["requested_time"], other["id"]))

    query_start, query_end = _walk_window(row["requested_time"])
    conflicts = tree.search(query_start, query_end)

    responded_at = now.isoformat()
    run_query(
        supabase.table(REQUESTS_TABLE)
        .update({"status": "accepted", "responded_at": responded_at})
        .eq("id", request_id)
        .eq("walker_id", user.id)
    )
    return {
        "id": row["id"],
        "status": "accepted",
        "respondedAt": responded_at,
        "conflicts": [
            {"requestId": value[1], "requestedTime": value[0]} for value in conflicts
        ],
    }


@router.post("/{request_id}/decline")
def decline_request(
    request_id: uuid.UUID,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Decline a pending request (the addressed walker only).

    Args:
        request_id: The request to decline.
        user: The authenticated walker.
        supabase: The Supabase client.
        now: The current time, stamped as respondedAt.

    Returns:
        ``{id, status: "declined", respondedAt}``.

    Raises:
        HTTPException: 400 (non-pending), 404 (foreign/unknown), 500.
    """
    row = _load_for_walker(supabase, request_id, user)
    if row["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=DECLINE_STATE_MESSAGE
        )

    responded_at = now.isoformat()
    run_query(
        supabase.table(REQUESTS_TABLE)
        .update({"status": "declined", "responded_at": responded_at})
        .eq("id", request_id)
        .eq("walker_id", user.id)
    )
    return {"id": row["id"], "status": "declined", "respondedAt": responded_at}


@router.post("/{request_id}/cancel")
def cancel_request(
    request_id: uuid.UUID,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Cancel a request (its owner only) while it is pending or accepted.

    S3: a request whose checkout was SENT is COMPLETE — the walk happened
    and the owner already has the photos, so cancelling it is a 400 (a
    checkout still in DRAFT does not complete the request: the walker has
    published nothing yet).

    ``responded_at`` is untouched: it stays null for a request cancelled
    before any walker response, and keeps the walker's timestamp for one
    cancelled after being accepted.

    Args:
        request_id: The request to cancel.
        user: The authenticated owner.
        supabase: The Supabase client.

    Returns:
        ``{id, status: "cancelled"}``.

    Raises:
        HTTPException: 400 (declined/already cancelled, or completed with a
            sent checkout), 404 (foreign — including the walker's), 500.
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
    if row["status"] not in ("pending", "accepted"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=CANCEL_STATE_MESSAGE
        )

    completed = run_query(
        supabase.table(CHECKOUTS_TABLE)
        .select("id")
        .eq("request_id", request_id)
        .eq("owner_id", user.id)
        .eq("status", "sent")
    )
    if completed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=COMPLETED_CANCEL_MESSAGE
        )

    run_query(
        supabase.table(REQUESTS_TABLE)
        .update({"status": "cancelled"})
        .eq("id", request_id)
        .eq("owner_id", user.id)
    )
    return {"id": row["id"], "status": "cancelled"}

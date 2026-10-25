"""W3 Clientes: the owners I walk for, built from bidding walks.

A client is an owner with at least one of MY walks in ``assigned``, ``in_walk``
or ``done``. The list orders owners by their latest completed walk, newest
first, with owners that only have upcoming walks last. The detail adds the
dogs' care profiles, the ten newest walks and the owner's phone, which is
revealed only while a walk with that owner is assigned or in progress
(InDrive: contact details live only during the trip). Emails and face
embeddings never leave the server: every query selects just the columns the
answer needs.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel
from supabase import Client
from supabase_auth.types import User

from ..app import limiter
from ..db import run_query
from ..deps import get_supabase
from ..walker_access import walker_from_path

router = APIRouter()

REQUESTS_TABLE = "walk_requests"
OFFERS_TABLE = "walk_offers"
DOGS_TABLE = "dogs"
PROFILES_TABLE = "owner_profiles"

DONE_STATUS = "done"
CLIENT_STATUSES = ["assigned", "in_walk", "done"]
UPCOMING_STATUSES = frozenset({"assigned", "in_walk"})

NOT_A_CLIENT_MESSAGE = "Not one of your clients."
RECENT_WALKS_LIMIT = 10

#: Only the columns each answer reads. The ``dogs`` embedding and any email
#: column are deliberately absent, so they can never reach the client.
REQUEST_COLUMNS = "id,owner_id,walker_id,dog_id,status,requested_time,price_cop"
DOG_COLUMNS = "id,name,profile"
DOG_LIST_COLUMNS = "id,name"
PROFILE_COLUMNS = "owner_id,display_name,phone"
OFFER_COLUMNS = "walk_request_id,price_cop"


class ClientDog(BaseModel):
    """A dog the walker has seen on this client's walks."""

    id: str | None
    name: str | None


class ClientListItem(BaseModel):
    """One owner in the walker's client list."""

    ownerId: str
    ownerName: str | None
    dogs: list[ClientDog]
    walksDone: int
    upcoming: int
    lastWalkAt: str | None
    grossCop: int


class DetailDog(BaseModel):
    """A client's dog with its stored care profile."""

    id: str | None
    name: str | None
    profile: dict[str, Any] | None


class ClientWalk(BaseModel):
    """One recent walk with a client, at the price the walker agreed to."""

    id: str
    dogName: str | None
    status: str
    requestedTime: str
    priceCop: int


class ClientDetail(BaseModel):
    """The full detail of one client: profile, care notes, recent walks."""

    ownerId: str
    ownerName: str | None
    dogs: list[DetailDog]
    walksDone: int
    upcoming: int
    lastWalkAt: str | None
    grossCop: int
    phone: str | None
    walks: list[ClientWalk]


def _load_walks(
    supabase: Client, walker_id: str, owner_id: str | None = None
) -> list[dict[str, Any]]:
    """Load the caller's assigned, in-walk and done walks. O(n).

    Args:
        supabase: The Supabase client.
        walker_id: The caller's user id.
        owner_id: When set, restrict the query to that owner.

    Returns:
        The matching ``walk_requests`` rows, in no particular order.
    """
    query = (
        supabase.table(REQUESTS_TABLE)
        .select(REQUEST_COLUMNS)
        .eq("walker_id", walker_id)
        .in_("status", CLIENT_STATUSES)
    )
    if owner_id is not None:
        query = query.eq("owner_id", owner_id)
    return run_query(query)


def _agreed_prices(
    supabase: Client, walker_id: str, walks: list[dict[str, Any]]
) -> dict[str, int]:
    """Map walk id to the price the caller offered for it. O(n).

    The walker's own ``walk_offers`` row wins; a walk without one falls back to
    the request's ``price_cop``, resolved later by ``_agreed_price``. One
    batched lookup is made for every walk, never one query per row.

    Args:
        supabase: The Supabase client.
        walker_id: The caller's user id, so another walker's offer never counts.
        walks: The walks whose agreed price is wanted.

    Returns:
        ``{walk_request_id: price_cop}`` for the walks that have an offer.
    """
    walk_ids = sorted({walk["id"] for walk in walks})
    if not walk_ids:
        return {}
    offers = run_query(
        supabase.table(OFFERS_TABLE)
        .select(OFFER_COLUMNS)
        .eq("walker_id", walker_id)
        .in_("walk_request_id", walk_ids)
    )
    prices: dict[str, int] = {}
    for offer in offers:
        prices.setdefault(offer["walk_request_id"], offer["price_cop"])
    return prices


def _agreed_price(walk: dict[str, Any], prices: dict[str, int]) -> int:
    """Return what a walk is worth: the agreed offer else the request price. O(1)."""
    offered = prices.get(walk["id"])
    if offered is not None:
        return offered
    return walk.get("price_cop") or 0


def _load_dogs(
    supabase: Client, dog_ids: set[str], with_profile: bool
) -> dict[str, dict[str, Any]]:
    """Load the dogs of the client's walks in one batched query. O(n).

    Args:
        supabase: The Supabase client.
        dog_ids: The dog ids to load.
        with_profile: Whether the detail view needs the care profile.

    Returns:
        ``{dog_id: row}``; the embedding column is never selected.
    """
    if not dog_ids:
        return {}
    columns = DOG_COLUMNS if with_profile else DOG_LIST_COLUMNS
    rows = run_query(
        supabase.table(DOGS_TABLE).select(columns).in_("id", sorted(dog_ids))
    )
    return {row["id"]: row for row in rows}


def _load_profiles(
    supabase: Client, owner_ids: set[str]
) -> dict[str, dict[str, Any]]:
    """Load the owners' display names and phones in one batched query. O(n).

    Emails live in Supabase auth, never in this table, and only the columns
    named in ``PROFILE_COLUMNS`` are selected.

    Args:
        supabase: The Supabase client.
        owner_ids: The owner ids to load.

    Returns:
        ``{owner_id: row}`` for the owners that have a profile.
    """
    if not owner_ids:
        return {}
    rows = run_query(
        supabase.table(PROFILES_TABLE)
        .select(PROFILE_COLUMNS)
        .in_("owner_id", sorted(owner_ids))
    )
    return {row["owner_id"]: row for row in rows}


def _stats(
    walks: list[dict[str, Any]], prices: dict[str, int]
) -> tuple[set[str], int, int, str | None, int]:
    """Summarize one client's walks. O(n) in the walks of that client.

    Args:
        walks: Every assigned, in-walk and done walk with that owner.
        prices: The agreed prices, keyed by walk id.

    Returns:
        The dog ids, completed walk count, upcoming count, latest completed
        ``requested_time`` (None when nothing is done) and gross COP.
    """
    dog_ids = {walk["dog_id"] for walk in walks}
    done = [walk for walk in walks if walk["status"] == DONE_STATUS]
    upcoming = sum(1 for walk in walks if walk["status"] in UPCOMING_STATUSES)
    last_walk_at = max((walk["requested_time"] for walk in done), default=None)
    gross = sum(_agreed_price(walk, prices) for walk in done)
    return dog_ids, len(done), upcoming, last_walk_at, gross


def _order_clients(items: list[ClientListItem]) -> list[ClientListItem]:
    """Order clients by latest completed walk, newest first, then owner id. O(n log n).

    Owners without a completed walk go last, ordered by owner id. Two stable
    sorts keep the owner-id tie-break ascending inside each group.

    Args:
        items: The client list items.

    Returns:
        The ordered items.
    """
    dated = [item for item in items if item.lastWalkAt is not None]
    undated = [item for item in items if item.lastWalkAt is None]
    dated.sort(key=lambda item: item.ownerId)
    dated.sort(key=lambda item: item.lastWalkAt, reverse=True)
    undated.sort(key=lambda item: item.ownerId)
    return dated + undated


@router.get("/walkers/{walkerId}/clients", response_model=list[ClientListItem])
@limiter.limit("30/minute")
def list_clients(
    request: Request,
    walker: User = Depends(walker_from_path),
    supabase: Client = Depends(get_supabase),
) -> list[ClientListItem]:
    """Return the owners this walker has walked for. O(n log n).

    A client is an owner with at least one of the caller's walks in
    ``assigned``, ``in_walk`` or ``done``. Items are ordered by the latest
    completed walk (newest first, owners without one last), then owner id.
    No phone number, email or embedding is part of the answer.

    Args:
        request: The incoming request, read by the rate limiter.
        walker: The authenticated walker named in the path.
        supabase: The Supabase client.

    Returns:
        The client list, empty when the walker has no clients.

    Raises:
        HTTPException: 403 when the caller is not that walker, 500 on database
            errors.
    """
    _ = request
    walks = _load_walks(supabase, walker.id)
    prices = _agreed_prices(supabase, walker.id, walks)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for walk in walks:
        grouped.setdefault(walk["owner_id"], []).append(walk)

    profiles = _load_profiles(supabase, set(grouped))
    all_dog_ids: set[str] = set()
    summaries: dict[str, tuple[set[str], int, int, str | None, int]] = {}
    for owner_id, owner_walks in grouped.items():
        summary = _stats(owner_walks, prices)
        summaries[owner_id] = summary
        all_dog_ids |= summary[0]
    dogs = _load_dogs(supabase, all_dog_ids, with_profile=False)

    items: list[ClientListItem] = []
    for owner_id, (dog_ids, walks_done, upcoming, last_walk_at, gross) in summaries.items():
        profile = profiles.get(owner_id, {})
        client_dogs = sorted(
            (
                ClientDog(id=dog_id, name=dogs.get(dog_id, {}).get("name"))
                for dog_id in dog_ids
            ),
            key=lambda dog: (dog.name or "", dog.id or ""),
        )
        items.append(
            ClientListItem(
                ownerId=owner_id,
                ownerName=profile.get("display_name"),
                dogs=client_dogs,
                walksDone=walks_done,
                upcoming=upcoming,
                lastWalkAt=last_walk_at,
                grossCop=gross,
            )
        )
    return _order_clients(items)


@router.get("/walkers/{walkerId}/clients/{ownerId}", response_model=ClientDetail)
@limiter.limit("30/minute")
def get_client(
    request: Request,
    owner_id: str = Path(alias="ownerId", min_length=1, max_length=64),
    walker: User = Depends(walker_from_path),
    supabase: Client = Depends(get_supabase),
) -> ClientDetail:
    """Return one client with care notes, recent walks and gated phone. O(n log n).

    Only an owner with at least one of the caller's assigned, in-walk or done
    walks is a client; anybody else answers the same 404. The ten newest walks
    are listed newest first and priced at the walker's agreed offer. The phone
    is included only while one of those walks is assigned or in progress.

    Args:
        request: The incoming request, read by the rate limiter.
        owner_id: The ``ownerId`` path segment.
        walker: The authenticated walker named in the path.
        supabase: The Supabase client.

    Returns:
        The client detail.

    Raises:
        HTTPException: 403 when the caller is not that walker, 404 when the
            owner is not one of their clients, 500 on database errors.
    """
    _ = request
    walks = _load_walks(supabase, walker.id, owner_id=owner_id)
    if not walks:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NOT_A_CLIENT_MESSAGE
        )

    prices = _agreed_prices(supabase, walker.id, walks)
    dog_ids, walks_done, upcoming, last_walk_at, gross = _stats(walks, prices)
    dogs = _load_dogs(supabase, dog_ids, with_profile=True)
    profile = _load_profiles(supabase, {owner_id}).get(owner_id, {})

    live = any(walk["status"] in UPCOMING_STATUSES for walk in walks)

    detail_dogs = sorted(
        (
            DetailDog(
                id=dog_id,
                name=dogs.get(dog_id, {}).get("name"),
                profile=dogs.get(dog_id, {}).get("profile"),
            )
            for dog_id in dog_ids
        ),
        key=lambda dog: (dog.name or "", dog.id or ""),
    )

    recent = sorted(
        walks,
        key=lambda walk: (walk["requested_time"], walk["id"]),
        reverse=True,
    )[:RECENT_WALKS_LIMIT]
    detail_walks = [
        ClientWalk(
            id=walk["id"],
            dogName=dogs.get(walk["dog_id"], {}).get("name"),
            status=walk["status"],
            requestedTime=walk["requested_time"],
            priceCop=_agreed_price(walk, prices),
        )
        for walk in recent
    ]

    return ClientDetail(
        ownerId=owner_id,
        ownerName=profile.get("display_name"),
        dogs=detail_dogs,
        walksDone=walks_done,
        upcoming=upcoming,
        lastWalkAt=last_walk_at,
        grossCop=gross,
        phone=profile.get("phone") if live else None,
        walks=detail_walks,
    )

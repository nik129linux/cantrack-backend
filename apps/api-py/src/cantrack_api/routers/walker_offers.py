"""W2 Solicitudes: withdraw an offer and follow my offers.

A walker withdraws the pending offer they sent on a still-open request, and
follows every offer they ever sent as ``pending`` (request still open), ``won``
(request assigned to them) or ``lost`` (anything else). The kind of an offer is
derived from its price: a price equal to the owner's fare IS an accept.
"""

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from supabase import Client
from supabase_auth.types import User

from ..db import run_query
from ..deps import current_role, get_current_user, get_supabase
from ..walker_access import WALKERS_ONLY_MESSAGE, walker_from_path

router = APIRouter()

REQUESTS_TABLE = "walk_requests"
OFFERS_TABLE = "walk_offers"
DOGS_TABLE = "dogs"

NO_OFFER_MESSAGE = "You have no offer on this request."
NOT_OPEN_MESSAGE = "The request is no longer open."
MAX_OFFERS = 50


class MyOfferView(BaseModel):
    """One offer in the walker's own history, with its derived outcome."""

    offerId: str
    requestId: str
    dogName: str | None = None
    requestedTime: str
    durationHours: float
    fareCop: int
    priceCop: int
    kind: Literal["accept", "counter"]
    status: Literal["pending", "won", "lost"]
    requestStatus: str


def _offer_kind(price_cop: int, fare_cop: int) -> Literal["accept", "counter"]:
    """An offer's kind derived from its price: matching the fare IS an accept. O(1)."""
    return "accept" if price_cop == fare_cop else "counter"


def _outcome(request: dict[str, Any], walker_id: str) -> Literal["pending", "won", "lost"]:
    """Classify a request from one walker's point of view. O(1)."""
    if request["status"] == "open":
        return "pending"
    if request.get("walker_id") == walker_id:
        return "won"
    return "lost"


@router.delete(
    "/bidding/requests/{request_id}/offers/mine",
    status_code=status.HTTP_204_NO_CONTENT,
)
def withdraw_offer(
    request_id: str,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> None:
    """Withdraw the caller's own offer on a still-open request (walkers only).

    The offer is found by request and caller, so another walker's bid is never
    touched. A request that already moved past ``open`` keeps its offers.

    Args:
        request_id: The walk request whose offer to withdraw.
        user: The authenticated caller.
        supabase: The Supabase client.

    Returns:
        An empty 204 response.

    Raises:
        HTTPException: 403 when the caller is not a walker, 404 when the caller
            has no offer on the request, 409 when the request is not open, 500
            on database errors.

    Big-O: O(o + r), where o is the caller's offers on the request and r the
        rows the delete matches.
    """
    if current_role(user) != "walker":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=WALKERS_ONLY_MESSAGE
        )

    offers = run_query(
        supabase.table(OFFERS_TABLE)
        .select("*")
        .eq("walk_request_id", request_id)
        .eq("walker_id", user.id)
    )
    if not offers:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=NO_OFFER_MESSAGE
        )

    requests = run_query(
        supabase.table(REQUESTS_TABLE).select("*").eq("id", request_id)
    )
    if not requests or requests[0]["status"] != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=NOT_OPEN_MESSAGE
        )

    run_query(
        supabase.table(OFFERS_TABLE).delete().eq("id", offers[0]["id"])
    )


@router.get("/walkers/{walkerId}/offers", response_model=list[MyOfferView])
def list_my_offers(
    user: User = Depends(walker_from_path),
    supabase: Client = Depends(get_supabase),
    status_filter: Literal["pending", "won", "lost"] | None = Query(
        default=None, alias="status"
    ),
) -> list[dict[str, Any]]:
    """List the caller's offers newest first, optionally filtered by outcome.

    Every offer the walker sent is joined to its request and classified:
    ``pending`` while the request is open, ``won`` when the request is assigned
    to the caller, ``lost`` otherwise. Dog names are fetched with one batched
    query.

    Args:
        user: The authenticated walker (must match ``walkerId``).
        supabase: The Supabase client.
        status_filter: Optional ``pending``/``won``/``lost`` filter.

    Returns:
        At most 50 offers, newest offer first.

    Raises:
        HTTPException: 403 when the caller is not that walker, 500 on database
            errors.

    Big-O: O(o + r + d), where o is the caller's offers, r their requests and d
        the distinct dogs on those requests.
    """
    offers = run_query(
        supabase.table(OFFERS_TABLE)
        .select("*")
        .eq("walker_id", user.id)
        .order("created_at", desc=True)
    )
    if not offers:
        return []

    request_ids = sorted({offer["walk_request_id"] for offer in offers})
    requests = run_query(
        supabase.table(REQUESTS_TABLE).select("*").in_("id", request_ids)
    )
    requests_by_id = {request["id"]: request for request in requests}

    dog_ids = sorted({request["dog_id"] for request in requests})
    dog_names = {
        dog["id"]: dog.get("name")
        for dog in run_query(
            supabase.table(DOGS_TABLE).select("*").in_("id", dog_ids)
        )
    }

    items: list[dict[str, Any]] = []
    for offer in offers:
        request = requests_by_id.get(offer["walk_request_id"])
        if request is None:
            continue
        outcome = _outcome(request, user.id)
        if status_filter is not None and outcome != status_filter:
            continue
        fare_cop = request["price_cop"]
        items.append(
            {
                "offerId": offer["id"],
                "requestId": offer["walk_request_id"],
                "dogName": dog_names.get(request["dog_id"]),
                "requestedTime": request["requested_time"],
                "durationHours": request["duration_hours"],
                "fareCop": fare_cop,
                "priceCop": offer["price_cop"],
                "kind": _offer_kind(offer["price_cop"], fare_cop),
                "status": outcome,
                "requestStatus": request["status"],
            }
        )
        if len(items) == MAX_OFFERS:
            break
    return items

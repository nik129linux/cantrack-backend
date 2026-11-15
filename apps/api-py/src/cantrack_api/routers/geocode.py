"""Pickup field: address search and reverse lookup (Photon), limited to Pasto."""

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from supabase_auth.types import User

from ..app import limiter
from ..deps import get_current_user
from ..geocode import (
    cache_get,
    cache_put,
    format_features,
    get_geocoder,
    reverse_key,
    search_key,
)
from ..navigation import inside_pasto

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/geocode", tags=["geocode"])

OUTSIDE_MESSAGE = "That point is outside Pasto."
UNAVAILABLE = {"status": "unavailable", "results": []}


@router.get("/search")
@limiter.limit("60/minute")
def search_address(
    request: Request,
    q: str = Query(max_length=80),
    user: User = Depends(get_current_user),
    geocoder: Any = Depends(get_geocoder),
) -> dict[str, Any]:
    """Addresses and places in Pasto matching the text, cached, never a 5xx.

    Any upstream failure answers ``status: "unavailable"`` so the field can fall
    back to the map. Failures are not cached.

    Args:
        request: The incoming request, read by the rate limiter.
        q: What the owner typed (2 to 80 characters once trimmed).
        user: The authenticated caller, of any role.
        geocoder: The geocoder (overridden in tests).

    Raises:
        HTTPException: 400 when the trimmed text is shorter than 2 characters.
    """
    _ = user
    text = q.strip()
    if len(text) < 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="q must have at least 2 characters.",
        )
    key = search_key(text)
    cached = cache_get(key)
    if cached is not None:
        return {"status": "ok", "results": cached}
    try:
        results = format_features(geocoder.search(text))
    except Exception:  # noqa: BLE001 - any upstream failure degrades to the map
        logger.warning("geocode search failed", exc_info=True)
        return dict(UNAVAILABLE)
    cache_put(key, results)
    return {"status": "ok", "results": results}


@router.get("/reverse")
@limiter.limit("60/minute")
def reverse_address(
    request: Request,
    lat: float = Query(ge=-90, le=90, allow_inf_nan=False),
    lng: float = Query(ge=-180, le=180, allow_inf_nan=False),
    user: User = Depends(get_current_user),
    geocoder: Any = Depends(get_geocoder),
) -> dict[str, Any]:
    """The address nearest a point in Pasto, cached, never a 5xx.

    Raises:
        HTTPException: 400 when the point is outside Pasto (never a free world geocoder).
    """
    _ = user
    if not inside_pasto(lat, lng):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=OUTSIDE_MESSAGE)
    key = reverse_key(lat, lng)
    cached = cache_get(key)
    if cached is not None:
        return {"status": "ok", "results": cached}
    try:
        results = format_features(geocoder.reverse(lat, lng), address_first=True)
    except Exception:  # noqa: BLE001 - any upstream failure degrades to the map
        logger.warning("geocode reverse failed", exc_info=True)
        return dict(UNAVAILABLE)
    cache_put(key, results)
    return {"status": "ok", "results": results}

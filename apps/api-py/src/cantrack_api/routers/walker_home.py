"""W1 Inicio: home panel, online switch and simulated earnings.

The walker's home shows the same day-to-day picture as the owner's, from the
other side: the walk in progress, the next assigned walk, today's money and
how many open requests are close enough to take. ``/availability`` is the
InDrive switch — a heartbeat row in ``walker_positions`` while online, gone
when offline — and ``/earnings`` reports the simulated 10% commission over a
local day, week (Monday first) or month. Every date is local to Bogota
(UTC-5, no DST).
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from supabase import Client
from supabase_auth.types import User

from .. import walker_access
from ..app import limiter
from ..db import run_query
from ..deps import get_now, get_supabase
from ..geo import haversine_km

router = APIRouter()

WALKS_TABLE = "walk_requests"
WALKER_PROFILES_TABLE = "walker_profiles"
OWNER_PROFILES_TABLE = "owner_profiles"
DOGS_TABLE = "dogs"
OFFERS_TABLE = "walk_offers"
RATINGS_TABLE = "walk_ratings"
POSITIONS_TABLE = "walker_positions"

#: Colombia has no DST, so one fixed offset is the local day everywhere.
BOGOTA = timezone(timedelta(hours=-5))

#: Simulated commission, in percent. No real payment exists.
COMMISSION_PCT = 10

#: Open requests closer than this are offered to the walker.
NEARBY_RADIUS_KM = 2.0

BAD_PERIOD_MESSAGE = "period must be day, week or month."
BAD_LOCATION_MESSAGE = "lat must be between -90 and 90 and lng between -180 and 180."
PERIODS = frozenset({"day", "week", "month"})


class DayEarnings(BaseModel):
    """One local day of simulated earnings."""

    date: str
    walks: int
    grossCop: int
    netCop: int


class EarningsResponse(BaseModel):
    """The earnings report for one period."""

    model_config = ConfigDict(populate_by_name=True)

    period: str
    from_: str = Field(alias="from")
    to: str
    simulated: bool
    commissionPct: int
    walks: int
    grossCop: int
    commissionCop: int
    netCop: int
    byDay: list[DayEarnings]


class TodayTotals(BaseModel):
    """The day's money on the home panel."""

    walks: int
    grossCop: int
    commissionCop: int
    netCop: int


class RatingSummary(BaseModel):
    """The walker's average rating, when they have one."""

    average: float | None
    count: int


class WalkSummary(BaseModel):
    """A walk on the home panel, priced at the walker's own offer."""

    id: str
    dogName: str | None
    ownerName: str | None
    requestedTime: str | None
    durationHours: float | None
    priceCop: int


class HomeResponse(BaseModel):
    """The whole walker home panel in one call."""

    greetingName: str
    online: bool
    activeWalk: WalkSummary | None
    nextWalk: WalkSummary | None
    today: TodayTotals
    rating: RatingSummary
    openNearby: int
    pendingOffers: int


class AvailabilityBody(BaseModel):
    """The online switch. Coordinates are required only when going online."""

    online: StrictBool
    lat: float | None = None
    lng: float | None = None


class AvailabilityResponse(BaseModel):
    """The switch's new state and heartbeat time."""

    online: bool
    updatedAt: str | None


def _parse_instant(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp, normalising it to an aware UTC datetime.

    O(1).

    Args:
        value: The stored timestamp string.

    Returns:
        The aware datetime, or None when the value is missing or unparseable.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _local_date(value: Any) -> date | None:
    """Return the Bogota local date of a stored timestamp. O(1).

    Args:
        value: The stored timestamp string.

    Returns:
        The local date, or None when the value cannot be parsed.
    """
    parsed = _parse_instant(value)
    return parsed.astimezone(BOGOTA).date() if parsed else None


def _sort_key(row: dict[str, Any]) -> str:
    """Order rows by their ``requested_time`` without comparing datetimes. O(1)."""
    parsed = _parse_instant(row.get("requested_time"))
    return parsed.isoformat() if parsed else ""


def _offer_prices(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Map walk request id to the caller's offered price. O(n).

    Args:
        rows: The caller's ``walk_offers`` rows.

    Returns:
        ``{walk_request_id: price_cop}`` for offers that carry both values.
    """
    prices: dict[str, int] = {}
    for row in rows:
        request_id = row.get("walk_request_id")
        price = row.get("price_cop")
        if request_id and isinstance(price, int):
            prices[str(request_id)] = price
    return prices


def _agreed_price(row: dict[str, Any], prices: dict[str, int]) -> int:
    """The agreed price of a walk: the walker's offer, else the fare. O(1).

    Args:
        row: A ``walk_requests`` row.
        prices: The caller's offer prices keyed by request id.

    Returns:
        The price in COP.
    """
    fare = row.get("price_cop")
    offered = prices.get(str(row.get("id")))
    if offered is not None:
        return int(offered)
    return int(fare) if isinstance(fare, int) else 0


def _commission(gross: int) -> int:
    """The simulated commission on a gross amount. O(1)."""
    return gross * COMMISSION_PCT // 100


def _walk_summary(
    row: dict[str, Any],
    dog_names: dict[str, str | None],
    owner_names: dict[str, str | None],
    prices: dict[str, int],
) -> dict[str, Any]:
    """Shape one walk for the home panel. O(1).

    ``ownerName`` is the profile display name, never an email address.

    Args:
        row: The ``walk_requests`` row.
        dog_names: Dog names keyed by id.
        owner_names: Owner display names keyed by owner id.
        prices: The caller's offer prices keyed by request id.

    Returns:
        The camelCase walk summary the panel expects.
    """
    return {
        "id": row["id"],
        "dogName": dog_names.get(str(row.get("dog_id"))),
        "ownerName": owner_names.get(str(row.get("owner_id"))),
        "requestedTime": row.get("requested_time"),
        "durationHours": row.get("duration_hours"),
        "priceCop": _agreed_price(row, prices),
    }


def _position_for(supabase: Client, walker_id: str) -> dict[str, Any] | None:
    """Load the caller's heartbeat row, if any. O(1) in rows returned.

    Args:
        supabase: The Supabase client.
        walker_id: The authenticated walker's id.

    Returns:
        The ``walker_positions`` row, or None.
    """
    rows = run_query(
        supabase.table(POSITIONS_TABLE)
        .select("*")
        .eq("walker_id", walker_id)
        .limit(1)
    )
    return rows[0] if rows else None


def _open_nearby(
    opened: list[dict[str, Any]], position: dict[str, Any] | None, now: datetime
) -> int:
    """Count open requests due in the future within the nearby radius. O(n).

    A walker without a live position is offline and sees zero, even if open
    requests exist.

    Args:
        opened: The open ``walk_requests`` rows.
        position: The caller's heartbeat row, or None.
        now: The current time.

    Returns:
        How many open requests are within ``NEARBY_RADIUS_KM``.
    """
    if position is None or not walker_access.is_online(position, now):
        return 0
    lat = position.get("lat")
    lng = position.get("lng")
    if not isinstance(lat, (int, float)) or not isinstance(lng, (int, float)):
        return 0
    count = 0
    for row in opened:
        deadline = _parse_instant(row.get("deadline_at"))
        if deadline is not None and deadline < now:
            continue
        pickup_lat = row.get("pickup_lat")
        pickup_lng = row.get("pickup_lng")
        if not isinstance(pickup_lat, (int, float)) or not isinstance(
            pickup_lng, (int, float)
        ):
            continue
        if haversine_km(lat, lng, pickup_lat, pickup_lng) <= NEARBY_RADIUS_KM:
            count += 1
    return count


@router.get("/walkers/{walkerId}/home", response_model=HomeResponse)
def get_home(
    user: User = Depends(walker_access.walker_from_path),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict:
    """Return the walker's whole home panel in one call.

    One walk query carries every one of the caller's walks and the rest is
    filtered and counted in Python, so the panel never fans out per walk.
    Related dogs and owners are loaded with one batched ``in_`` query each.

    O(w + o + d + p) where w is the caller's walks, o their offers, d the dogs
    and owners of the two shown walks, and p their ratings.

    Args:
        user: The authenticated walker named in the path.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        Greeting, online state, active and next walk, today's totals, rating,
        nearby open requests and pending offers.
    """
    walker_id = user.id

    profiles = run_query(
        supabase.table(WALKER_PROFILES_TABLE)
        .select("walker_id,display_name")
        .eq("walker_id", walker_id)
    )
    walks = run_query(
        supabase.table(WALKS_TABLE).select("*").eq("walker_id", walker_id)
    )
    offers = run_query(
        supabase.table(OFFERS_TABLE).select("*").eq("walker_id", walker_id)
    )
    ratings = run_query(
        supabase.table(RATINGS_TABLE).select("*").eq("walker_id", walker_id)
    )
    opened = run_query(
        supabase.table(WALKS_TABLE).select("*").eq("status", "open")
    )
    position = _position_for(supabase, walker_id)
    prices = _offer_prices(offers)

    email_prefix = (user.email or "").split("@")[0]
    display = (profiles[0].get("display_name") if profiles else None) or email_prefix
    greeting = str(display).strip() or email_prefix

    active_rows = sorted(
        (row for row in walks if row.get("status") == "in_walk"), key=_sort_key
    )
    next_rows = sorted(
        (row for row in walks if row.get("status") == "assigned"), key=_sort_key
    )
    active_row = active_rows[0] if active_rows else None
    next_row = next_rows[0] if next_rows else None

    dog_ids = sorted(
        {
            str(row["dog_id"])
            for row in (active_row, next_row)
            if row is not None and row.get("dog_id")
        }
    )
    owner_ids = sorted(
        {
            str(row["owner_id"])
            for row in (active_row, next_row)
            if row is not None and row.get("owner_id")
        }
    )
    dog_names: dict[str, str | None] = {}
    if dog_ids:
        for row in run_query(
            supabase.table(DOGS_TABLE).select("id,name").in_("id", dog_ids)
        ):
            dog_names[str(row["id"])] = row.get("name")
    owner_names: dict[str, str | None] = {}
    if owner_ids:
        for row in run_query(
            supabase.table(OWNER_PROFILES_TABLE)
            .select("owner_id,display_name")
            .in_("owner_id", owner_ids)
        ):
            owner_names[str(row["owner_id"])] = row.get("display_name")

    active_walk = (
        _walk_summary(active_row, dog_names, owner_names, prices)
        if active_row is not None
        else None
    )
    next_walk = (
        _walk_summary(next_row, dog_names, owner_names, prices)
        if next_row is not None
        else None
    )

    today = _local_date(now.isoformat())
    today_walks = [
        row
        for row in walks
        if row.get("status") == "done" and _local_date(row.get("requested_time")) == today
    ]
    gross = sum(_agreed_price(row, prices) for row in today_walks)
    commission = _commission(gross)

    values = [row.get("rating") for row in ratings]
    numeric = [float(value) for value in values if isinstance(value, (int, float))]
    average = round(sum(numeric) / len(numeric), 2) if numeric else None

    open_ids = {str(row["id"]) for row in opened}
    pending_offers = sum(
        1 for row in offers if str(row.get("walk_request_id")) in open_ids
    )

    return {
        "greetingName": greeting,
        "online": walker_access.is_online(position, now),
        "activeWalk": active_walk,
        "nextWalk": next_walk,
        "today": {
            "walks": len(today_walks),
            "grossCop": gross,
            "commissionCop": commission,
            "netCop": gross - commission,
        },
        "rating": {"average": average, "count": len(numeric)},
        "openNearby": _open_nearby(opened, position, now),
        "pendingOffers": pending_offers,
    }


@router.put("/walkers/{walkerId}/availability", response_model=AvailabilityResponse)
@limiter.limit("10/minute")
def set_availability(
    request: Request,
    body: AvailabilityBody,
    user: User = Depends(walker_access.walker_from_path),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict:
    """Switch the walker online (heartbeat row) or offline (row removed).

    Going online needs bounded coordinates and upserts the single heartbeat
    row: the double has no native upsert, so the row is looked up by walker and
    then updated or inserted. Going offline deletes the caller's row only.

    O(1) relative to the table: at most one row is read and written.

    Args:
        request: The incoming request, read by the rate limiter.
        body: The switch and, when going online, the coordinates.
        user: The authenticated walker named in the path.
        supabase: The Supabase client.
        now: The current time, stored as ``updated_at``.

    Returns:
        The new state and the heartbeat time, or None when offline.

    Raises:
        HTTPException: 400 when going online without bounded coordinates.
    """
    _ = request
    if not body.online:
        run_query(
            supabase.table(POSITIONS_TABLE).delete().eq("walker_id", user.id)
        )
        return {"online": False, "updatedAt": None}

    if body.lat is None or body.lng is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_LOCATION_MESSAGE
        )
    if not -90 <= body.lat <= 90 or not -180 <= body.lng <= 180:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_LOCATION_MESSAGE
        )

    updated_at = now.isoformat()
    existing = run_query(
        supabase.table(POSITIONS_TABLE)
        .select("*")
        .eq("walker_id", user.id)
        .limit(1)
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
    return {"online": True, "updatedAt": updated_at}


def _period_range(period: str, today: date) -> tuple[date, date] | None:
    """Return the inclusive local date range of a period. O(1).

    Args:
        period: ``day``, ``week`` or ``month``.
        today: The current Bogota local date.

    Returns:
        The first and last day of the period, or None for an unknown period.
    """
    if period == "day":
        return today, today
    if period == "week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=6)
    if period == "month":
        start = today.replace(day=1)
        next_month = (start + timedelta(days=32)).replace(day=1)
        return start, next_month - timedelta(days=1)
    return None


def _each_day(start: date, end: date) -> list[date]:
    """List every inclusive date from ``start`` to ``end``. O(days)."""
    days: list[date] = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


@router.get("/walkers/{walkerId}/earnings", response_model=EarningsResponse)
def get_earnings(
    period: str = "week",
    user: User = Depends(walker_access.walker_from_path),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict:
    """Return the simulated earnings of the caller for a day, week or month.

    Only the caller's ``done`` walks count. The agreed price is the walker's
    own offer, falling back to the request fare; every local day in the range
    is returned, zeros included, so the client can draw the chart directly.

    O(w + o + d) where w is the caller's walks, o their offers and d the days
    in the period.

    Args:
        period: ``day``, ``week`` (default, Monday first) or ``month``.
        user: The authenticated walker named in the path.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        The period, bounds, commission and per-day totals.

    Raises:
        HTTPException: 400 for an unknown period.
    """
    if period not in PERIODS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_PERIOD_MESSAGE
        )

    today = now.astimezone(BOGOTA).date()
    bounds = _period_range(period, today)
    if bounds is None:  # pragma: no cover - guarded by the check above
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=BAD_PERIOD_MESSAGE
        )
    start, end = bounds

    walks = run_query(
        supabase.table(WALKS_TABLE).select("*").eq("walker_id", user.id)
    )
    offers = run_query(
        supabase.table(OFFERS_TABLE).select("*").eq("walker_id", user.id)
    )
    prices = _offer_prices(offers)

    done = [
        row
        for row in walks
        if row.get("status") == "done"
        and (day := _local_date(row.get("requested_time"))) is not None
        and start <= day <= end
    ]

    per_day: dict[date, list[dict[str, Any]]] = {
        day: [] for day in _each_day(start, end)
    }
    for row in done:
        day = _local_date(row.get("requested_time"))
        if day is not None:
            per_day[day].append(row)

    by_day: list[dict[str, Any]] = []
    total_gross = 0
    total_walks = 0
    for day in _each_day(start, end):
        rows = per_day[day]
        gross = sum(_agreed_price(row, prices) for row in rows)
        total_gross += gross
        total_walks += len(rows)
        by_day.append(
            {
                "date": day.isoformat(),
                "walks": len(rows),
                "grossCop": gross,
                "netCop": gross - _commission(gross),
            }
        )

    commission = _commission(total_gross)
    return {
        "period": period,
        "from": start.isoformat(),
        "to": end.isoformat(),
        "simulated": True,
        "commissionPct": COMMISSION_PCT,
        "walks": total_walks,
        "grossCop": total_gross,
        "commissionCop": commission,
        "netCop": total_gross - commission,
        "byDay": by_day,
    }

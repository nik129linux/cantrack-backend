"""Inicio: the owner's home panel and the weather card (PR1)."""

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from supabase import Client
from supabase_auth.types import User

from .. import owner_access
from ..app import limiter
from ..db import run_query
from ..deps import get_current_user, get_now, get_supabase
from ..weather import WEATHER_CACHE, cache_put, get_weather_source

router = APIRouter()

WALKS_TABLE = "walk_requests"
DOGS_TABLE = "dogs"
OWNER_PROFILES_TABLE = "owner_profiles"
WALKER_PROFILES_TABLE = "walker_profiles"
OFFERS_TABLE = "walk_offers"
TIMELINE_TABLE = "walk_timeline"
GPS_TABLE = "walk_gps_points"

UNAVAILABLE_MESSAGE = "Weather is unavailable right now."
FRESH_SECONDS = 15 * 60
STALE_SECONDS = 6 * 60 * 60
HOURS_COUNT = 12
WINDOW_END_HOUR = 20

BAD_CONDITIONS = frozenset({"drizzle", "rain", "snow", "storm"})


class LastPosition(BaseModel):
    """The latest GPS fix of a walk in progress."""

    lat: float
    lng: float
    atEpoch: float


class ActiveWalk(BaseModel):
    """The owner's walk in progress, if any."""

    id: str
    dogName: str | None
    walkerName: str | None
    startedAt: str | None
    minutesElapsed: int
    lastPosition: LastPosition | None


class UpcomingWalk(BaseModel):
    """One assigned walk, soonest first."""

    id: str
    dogName: str | None
    walkerName: str | None
    requestedTime: str
    durationHours: float
    priceCop: int


class OpenRequest(BaseModel):
    """One open request with its offer count."""

    id: str
    dogName: str | None
    requestedTime: str
    priceCop: int
    offerCount: int
    deadlineAt: str | None


class HomeCounts(BaseModel):
    """Totals for the home panel."""

    dogs: int
    walksDone: int
    openRequests: int


class HomeResponse(BaseModel):
    """The whole home panel in one call."""

    greetingName: str
    activeWalk: ActiveWalk | None
    nextWalk: UpcomingWalk | None
    upcoming: list[UpcomingWalk]
    openRequests: list[OpenRequest]
    counts: HomeCounts


class WeatherHour(BaseModel):
    """One local hour of the forecast."""

    at: str
    temperatureC: float
    condition: str
    precipitationProbability: int


class BestWindow(BaseModel):
    """The driest mild 2-hour slot left today."""

    start: str
    end: str
    reason: str


class WeatherResponse(BaseModel):
    """Current conditions, the next twelve hours and the best walk window."""

    lat: float
    lng: float
    temperatureC: float
    condition: str
    stale: bool
    hours: list[WeatherHour]
    bestWindow: BestWindow | None


def _condition_for(code: Any) -> str:
    """Map a WMO weather code to a condition word. O(1).

    Args:
        code: The numeric WMO code from Open-Meteo.

    Returns:
        One of clear, cloudy, fog, drizzle, rain, snow or storm.
    """
    try:
        value = int(code)
    except (TypeError, ValueError):
        raise ValueError(f"bad weather code: {code!r}") from None
    if value == 0:
        return "clear"
    if 1 <= value <= 3:
        return "cloudy"
    if value in (45, 48):
        return "fog"
    if 51 <= value <= 57:
        return "drizzle"
    if 61 <= value <= 67 or 80 <= value <= 82:
        return "rain"
    if 71 <= value <= 77 or value in (85, 86):
        return "snow"
    if 95 <= value <= 99:
        return "storm"
    return "cloudy"


def _local_tz(offset_seconds: Any) -> timezone:
    """Build the location timezone from a utc_offset_seconds value. O(1).

    Args:
        offset_seconds: Seconds east of UTC reported by Open-Meteo.

    Returns:
        The matching fixed-offset timezone.

    Raises:
        ValueError: When the offset is missing or not a number.
    """
    if isinstance(offset_seconds, bool) or not isinstance(
        offset_seconds, (int, float)
    ):
        raise ValueError(f"bad utc offset: {offset_seconds!r}")
    return timezone(timedelta(seconds=offset_seconds))


def _number(value: Any, name: str) -> float:
    """Coerce a payload number, refusing bools and non-numbers. O(1).

    Args:
        value: The candidate number.
        name: What to name in the error when it is not one.

    Returns:
        The value as a float.

    Raises:
        ValueError: When the value is not a real number.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"bad {name}: {value!r}")
    return float(value)


def _parse_hour(value: Any, local: timezone) -> datetime:
    """Parse an hourly time string as a local hour. O(1).

    Args:
        value: A ``YYYY-MM-DDTHH:MM`` local time from Open-Meteo.
        local: The location timezone to attach.

    Returns:
        The aware local datetime.

    Raises:
        ValueError: When the string is not a valid hour.
    """
    if not isinstance(value, str):
        raise ValueError(f"bad hour: {value!r}")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise ValueError(f"bad hour: {value!r}") from None
    return parsed.replace(tzinfo=local)


def _build_weather(
    lat: float, lng: float, payload: dict, now: datetime, stale: bool
) -> dict:
    """Build the weather response from a raw Open-Meteo payload.

    O(h) in the number of hourly entries kept (twelve plus the scan for the
    best window).

    Args:
        lat: Rounded latitude for the answer.
        lng: Rounded longitude for the answer.
        payload: The raw Open-Meteo JSON.
        now: The current aware UTC time.
        stale: Whether this payload comes from the stale cache.

    Returns:
        The response body matching ``WeatherResponse``.

    Raises:
        ValueError: When the payload is malformed.
    """
    if not isinstance(payload, dict):
        raise ValueError("payload is not an object")
    local = _local_tz(payload.get("utc_offset_seconds"))
    current = payload.get("current")
    hourly = payload.get("hourly")
    if not isinstance(current, dict) or not isinstance(hourly, dict):
        raise ValueError("payload misses current/hourly")
    temperature = _number(current.get("temperature_2m"), "temperature")
    condition = _condition_for(current.get("weather_code"))

    times = hourly.get("time")
    temps = hourly.get("temperature_2m")
    rains = hourly.get("precipitation_probability")
    codes = hourly.get("weather_code")
    if not all(isinstance(item, list) for item in (times, temps, rains, codes)):
        raise ValueError("hourly series are not lists")
    count = len(times)
    if count == 0 or not (
        len(temps) == len(rains) == len(codes) == count
    ):
        raise ValueError("hourly series disagree")

    local_now = now.astimezone(timezone.utc) + timedelta(
        seconds=local.utcoffset(None).total_seconds()
    )
    current_label = local_now.strftime("%Y-%m-%dT%H:00")
    try:
        start_index = list(times).index(current_label)
    except ValueError:
        # The payload is well-formed but covers other hours than the current
        # one (e.g. a skewed clock): degrade to current conditions with no
        # hours instead of failing, after proving the series itself parses.
        for position in range(count):
            _parse_hour(times[position], local)
            _number(temps[position], "temperature")
            _number(rains[position], "rain")
            _condition_for(codes[position])
        return {
            "lat": lat,
            "lng": lng,
            "temperatureC": temperature,
            "condition": condition,
            "stale": stale,
            "hours": [],
            "bestWindow": None,
        }
    entries: list[dict] = []
    for position in range(start_index, min(start_index + HOURS_COUNT, count)):
        at = _parse_hour(times[position], local)
        entries.append(
            {
                "at": at.isoformat(),
                "temperatureC": _number(temps[position], "temperature"),
                "condition": _condition_for(codes[position]),
                "precipitationProbability": int(_number(rains[position], "rain")),
            }
        )

    day_end = local_now.replace(hour=WINDOW_END_HOUR, minute=0, second=0, microsecond=0)
    if day_end.tzinfo is None:
        day_end = day_end.replace(tzinfo=local)
    best: dict | None = None
    best_rain = 0.0
    for position in range(start_index, count - 1):
        start = _parse_hour(times[position], local)
        end = start + timedelta(hours=2)
        if end > day_end:
            break
        pair = [
            {
                "temperatureC": _number(temps[position + offset], "temperature"),
                "condition": _condition_for(codes[position + offset]),
                "precipitationProbability": int(
                    _number(rains[position + offset], "rain")
                ),
            }
            for offset in (0, 1)
        ]
        if any(
            item["precipitationProbability"] >= 30
            or not 12 <= item["temperatureC"] <= 22
            or item["condition"] in BAD_CONDITIONS
            for item in pair
        ):
            continue
        worst = max(item["precipitationProbability"] for item in pair)
        if best is None or worst < best_rain:
            best_rain = worst
            best = {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "reason": "dry_and_mild",
            }

    return {
        "lat": lat,
        "lng": lng,
        "temperatureC": temperature,
        "condition": condition,
        "stale": stale,
        "hours": entries,
        "bestWindow": best,
    }


@router.get("/owners/{ownerId}/home", response_model=HomeResponse)
def get_home(
    user: User = Depends(owner_access.owner_from_path),
    supabase: Client = Depends(get_supabase),
    now: datetime = Depends(get_now),
) -> dict:
    """Return the owner's whole home panel in one call.

    One walk_requests query carries every walk and the rest is filtered and
    counted in Python, so the panel never fans out per walk; every walk query
    is scoped to the caller, so another owner's rows can never appear.

    Args:
        user: The authenticated owner named in the path.
        supabase: The Supabase client.
        now: The current time (dependency, frozen in tests).

    Returns:
        The greeting name, active walk, next walk, upcoming walks, open
        requests with offer counts, and totals.
    """
    owner_id = user.id
    dogs = run_query(
        supabase.table(DOGS_TABLE).select("id,name").eq("owner_id", owner_id)
    )
    profiles = run_query(
        supabase.table(OWNER_PROFILES_TABLE)
        .select("owner_id,display_name")
        .eq("owner_id", owner_id)
    )
    walks = run_query(
        supabase.table(WALKS_TABLE).select("*").eq("owner_id", owner_id)
    )

    dog_names = {row["id"]: row.get("name") for row in dogs}

    display = (profiles[0].get("display_name") if profiles else None) or ""
    email = getattr(user, "email", "") or ""
    greeting = display.strip() or email.split("@")[0]

    walker_ids = sorted(
        {str(row["walker_id"]) for row in walks if row.get("walker_id")}
    )
    walker_names: dict[str, str | None] = {}
    if walker_ids:
        for row in run_query(
            supabase.table(WALKER_PROFILES_TABLE)
            .select("walker_id,display_name")
            .in_("walker_id", walker_ids)
        ):
            walker_names[str(row["walker_id"])] = row.get("display_name")

    assigned = sorted(
        (row for row in walks if row.get("status") == "assigned"),
        key=lambda row: str(row.get("requested_time")),
    )
    opened = sorted(
        (row for row in walks if row.get("status") == "open"),
        key=lambda row: str(row.get("requested_time")),
    )
    active = sorted(
        (row for row in walks if row.get("status") == "in_walk"),
        key=lambda row: str(row.get("requested_time")),
    )

    def _upcoming(row: dict) -> dict:
        """Shape one assigned walk for the panel. O(1)."""
        return {
            "id": row["id"],
            "dogName": dog_names.get(str(row.get("dog_id"))),
            "walkerName": walker_names.get(str(row.get("walker_id"))),
            "requestedTime": row.get("requested_time"),
            "durationHours": row.get("duration_hours"),
            "priceCop": row.get("price_cop"),
        }

    upcoming = [_upcoming(row) for row in assigned[:5]]
    next_walk = upcoming[0] if upcoming else None

    offer_counts: dict[str, int] = {}
    open_ids = [str(row["id"]) for row in opened]
    if open_ids:
        for offer in run_query(
            supabase.table(OFFERS_TABLE)
            .select("walk_request_id")
            .in_("walk_request_id", open_ids)
        ):
            key = str(offer.get("walk_request_id"))
            offer_counts[key] = offer_counts.get(key, 0) + 1
    open_requests = [
        {
            "id": row["id"],
            "dogName": dog_names.get(str(row.get("dog_id"))),
            "requestedTime": row.get("requested_time"),
            "priceCop": row.get("price_cop"),
            "offerCount": offer_counts.get(str(row["id"]), 0),
            "deadlineAt": row.get("deadline_at"),
        }
        for row in opened
    ]

    active_walk = None
    if active:
        row = active[0]
        walk_id = str(row["id"])
        checkins = run_query(
            supabase.table(TIMELINE_TABLE)
            .select("walk_request_id,kind,at")
            .in_("walk_request_id", [walk_id])
        )
        starts = sorted(
            (
                str(item.get("at"))
                for item in checkins
                if str(item.get("walk_request_id")) == walk_id
                and item.get("kind") == "check_in"
                and item.get("at")
            )
        )
        started_at = starts[0] if starts else None
        points = run_query(
            supabase.table(GPS_TABLE)
            .select("walk_request_id,at_epoch,lat,lng")
            .in_("walk_request_id", [walk_id])
        )
        own_points = [
            item
            for item in points
            if str(item.get("walk_request_id")) == walk_id
            and isinstance(item.get("at_epoch"), (int, float))
        ]
        latest = max(own_points, key=lambda item: item["at_epoch"]) if own_points else None
        minutes = 0
        if started_at is not None:
            try:
                start_time = datetime.fromisoformat(started_at)
                minutes = max(
                    0, int((now - start_time).total_seconds() // 60)
                )
            except (TypeError, ValueError):
                started_at = None
        active_walk = {
            "id": row["id"],
            "dogName": dog_names.get(str(row.get("dog_id"))),
            "walkerName": walker_names.get(str(row.get("walker_id"))),
            "startedAt": started_at,
            "minutesElapsed": minutes,
            "lastPosition": (
                {
                    "lat": latest["lat"],
                    "lng": latest["lng"],
                    "atEpoch": latest["at_epoch"],
                }
                if latest is not None
                else None
            ),
        }

    return {
        "greetingName": greeting,
        "activeWalk": active_walk,
        "nextWalk": next_walk,
        "upcoming": upcoming,
        "openRequests": open_requests,
        "counts": {
            "dogs": len(dogs),
            "walksDone": sum(1 for row in walks if row.get("status") == "done"),
            "openRequests": len(opened),
        },
    }


@router.get("/weather", response_model=WeatherResponse)
@limiter.limit("30/minute")
def get_weather(
    request: Request,
    lat: float,
    lng: float,
    user: User = Depends(get_current_user),
    source: Any = Depends(get_weather_source),
    now: datetime = Depends(get_now),
) -> dict:
    """Return the Open-Meteo forecast for a point, cached and never a 500.

    Coordinates are rounded to two decimals for the cache key, so nearby
    requests share one upstream call. Fresh entries (15 minutes) are served
    directly; on any upstream failure or malformed payload an entry up to six
    hours old is served as stale, otherwise the answer is a 503.

    Args:
        request: The incoming request, read by the rate limiter.
        lat: Latitude in degrees (-90 to 90).
        lng: Longitude in degrees (-180 to 180).
        user: The authenticated caller, of any role.
        source: The weather source (overridden in tests).
        now: The current time (dependency, frozen in tests).

    Returns:
        Current conditions, twelve local hours and the best walk window.

    Raises:
        HTTPException: 400 for bad coordinates, 503 when no forecast exists.
    """
    _ = user
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="lat must be between -90 and 90 and lng between -180 and 180.",
        )
    key = (round(lat, 2), round(lng, 2))
    cached = WEATHER_CACHE.get(key)

    def _fresh(entry: dict | None) -> dict | None:
        """Return the entry payload when it is at most 15 minutes old. O(1)."""
        if entry is None:
            return None
        fetched = entry.get("at")
        if not isinstance(fetched, datetime):
            return None
        if (now - fetched).total_seconds() <= FRESH_SECONDS:
            stored = entry.get("payload")
            return stored if isinstance(stored, dict) else None
        return None

    def _stale() -> dict | None:
        """Return the cached payload when it is at most 6 hours old. O(1)."""
        if cached is None:
            return None
        fetched = cached.get("at")
        if not isinstance(fetched, datetime):
            return None
        if (now - fetched).total_seconds() <= STALE_SECONDS:
            stored = cached.get("payload")
            return stored if isinstance(stored, dict) else None
        return None

    fresh = _fresh(cached)
    if fresh is not None:
        try:
            return _build_weather(key[0], key[1], fresh, now, False)
        except ValueError:
            pass

    try:
        payload = source.forecast(key[0], key[1])
    except Exception:
        fallback = _stale()
        if fallback is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=UNAVAILABLE_MESSAGE,
            ) from None
        return _build_weather(key[0], key[1], fallback, now, True)
    try:
        body = _build_weather(key[0], key[1], payload, now, False)
    except ValueError:
        fallback = _stale()
        if fallback is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=UNAVAILABLE_MESSAGE,
            ) from None
        return _build_weather(key[0], key[1], fallback, now, True)
    cache_put(key, payload, now)
    return body

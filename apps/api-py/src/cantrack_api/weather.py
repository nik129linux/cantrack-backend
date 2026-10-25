"""Open-Meteo weather source with a small in-memory cache (PR1)."""

from datetime import datetime

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

#: Cache of raw Open-Meteo payloads keyed by ``(round(lat, 2), round(lng, 2))``.
#: Each entry is ``{"at": <aware UTC datetime of the fetch>, "payload": <dict>}``.
WEATHER_CACHE: dict[tuple[float, float], dict] = {}

#: At most this many ~1 km cells are kept. Without a cap, a caller looping over
#: coordinates (30/min per user, many users) grows the process memory forever.
MAX_CACHE_ENTRIES = 512
#: Entries older than this are useless even as a stale fallback.
CACHE_MAX_AGE_SECONDS = 6 * 60 * 60


def cache_put(key: tuple[float, float], payload: dict, now: datetime) -> None:
    """Store a payload, evicting expired entries and then the oldest when full.

    O(n) in the cache size (at most ``MAX_CACHE_ENTRIES``), paid only on an
    upstream fetch, never on a cache hit.

    Args:
        key: The rounded ``(lat, lng)`` cell.
        payload: The raw Open-Meteo JSON for that cell.
        now: The fetch time.
    """
    for expired in [cell for cell, entry in WEATHER_CACHE.items()
                    if (now - entry["at"]).total_seconds() > CACHE_MAX_AGE_SECONDS]:
        del WEATHER_CACHE[expired]
    while len(WEATHER_CACHE) >= MAX_CACHE_ENTRIES and key not in WEATHER_CACHE:
        oldest = min(WEATHER_CACHE, key=lambda cell: WEATHER_CACHE[cell]["at"])
        del WEATHER_CACHE[oldest]
    WEATHER_CACHE[key] = {"at": now, "payload": payload}


class OpenMeteoClient:
    """Thin client over the Open-Meteo forecast API."""

    def forecast(self, lat: float, lng: float) -> dict:
        """Fetch the 2-day hourly forecast for a point. O(1).

        Args:
            lat: Latitude in degrees.
            lng: Longitude in degrees.

        Returns:
            The decoded Open-Meteo JSON payload.

        Raises:
            httpx.HTTPError: On any transport or non-2xx failure.
            ValueError: When the body is not valid JSON.
        """
        response = httpx.get(
            FORECAST_URL,
            params={
                "latitude": lat,
                "longitude": lng,
                "current": "temperature_2m,weather_code",
                "hourly": "temperature_2m,precipitation_probability,weather_code",
                "timezone": "auto",
                "forecast_days": 2,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()


def get_weather_source() -> OpenMeteoClient:
    """Return the weather source for ``GET /weather``. O(1).

    Tests override this dependency with a fake, so the endpoint never touches
    the network under test.

    Returns:
        An ``OpenMeteoClient`` whose ``forecast`` raises on any failure.
    """
    return OpenMeteoClient()

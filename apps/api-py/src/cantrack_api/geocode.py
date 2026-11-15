"""Photon (OpenStreetMap) address search for the pickup field, with a small in-memory cache."""

from collections import OrderedDict
from typing import Any

import httpx

from .navigation import PASTO_BOUNDS, inside_pasto

SEARCH_URL = "https://photon.komoot.io/api/"
REVERSE_URL = "https://photon.komoot.io/reverse"

#: A generic client name: the user's details never go to a third party.
USER_AGENT = "CanTrack/0.1"
TIMEOUT_SECONDS = 5.0

#: Photon wants ``minLon,minLat,maxLon,maxLat``: the same box the whole app serves.
BBOX = f"{PASTO_BOUNDS['west']},{PASTO_BOUNDS['south']},{PASTO_BOUNDS['east']},{PASTO_BOUNDS['north']}"

#: Where results are biased to when several places share a name.
BIAS_LAT = 1.2136
BIAS_LNG = -77.2811

#: Photon is asked for more than is shown, because the Pasto filter drops some.
UPSTREAM_LIMIT = 10
MAX_RESULTS = 6

#: Bounded LRU of the formatted answers. Without a cap, a caller looping over
#: queries (60/min per user, many users) grows the process memory forever.
MAX_CACHE_ENTRIES = 512
GEOCODE_CACHE: "OrderedDict[tuple, list[dict[str, Any]]]" = OrderedDict()


def cache_get(key: tuple) -> list[dict[str, Any]] | None:
    """The cached results for a key, marking it most recently used. O(1)."""
    if key not in GEOCODE_CACHE:
        return None
    GEOCODE_CACHE.move_to_end(key)
    return GEOCODE_CACHE[key]


def cache_put(key: tuple, results: list[dict[str, Any]]) -> None:
    """Store results, evicting the least recently used entry when full. O(1)."""
    GEOCODE_CACHE[key] = results
    GEOCODE_CACHE.move_to_end(key)
    while len(GEOCODE_CACHE) > MAX_CACHE_ENTRIES:
        GEOCODE_CACHE.popitem(last=False)


def search_key(query: str) -> tuple:
    """The cache key of a search: lower-cased, spaces collapsed. O(len(query))."""
    return ("search", " ".join(query.lower().split()))


def reverse_key(lat: float, lng: float) -> tuple:
    """The cache key of a reverse lookup: the point rounded to four decimals (~11 m). O(1)."""
    return ("reverse", round(lat, 4), round(lng, 4))


class PhotonClient:
    """Thin client over the Photon geocoder."""

    def search(self, query: str) -> list[dict[str, Any]]:
        """Places matching a text, inside the Pasto box. O(1).

        Raises:
            httpx.HTTPError: On any transport or non-2xx failure.
            ValueError: When the body is not valid JSON.
        """
        response = httpx.get(
            SEARCH_URL,
            params={
                "q": query,
                "bbox": BBOX,
                "lat": BIAS_LAT,
                "lon": BIAS_LNG,
                "limit": UPSTREAM_LIMIT,
                "lang": "es",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json().get("features", [])

    def reverse(self, lat: float, lng: float) -> list[dict[str, Any]]:
        """The places nearest a point. O(1).

        Raises:
            httpx.HTTPError: On any transport or non-2xx failure.
            ValueError: When the body is not valid JSON.
        """
        response = httpx.get(
            REVERSE_URL,
            params={"lat": lat, "lon": lng, "limit": 1, "lang": "es"},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json().get("features", [])


def get_geocoder() -> PhotonClient:
    """The geocoder for ``/geocode``; tests override this so nothing touches the network. O(1)."""
    return PhotonClient()


def _label(props: dict[str, Any]) -> str | None:
    """The bold line of a result: the place name, else ``street number``. O(1)."""
    name = props.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    street = props.get("street")
    if isinstance(street, str) and street.strip():
        number = props.get("housenumber")
        if isinstance(number, str) and number.strip():
            return f"{street.strip()} {number.strip()}"
        return street.strip()
    return None


def _detail(props: dict[str, Any]) -> str:
    """The muted line of a result: ``district, city``. O(1)."""
    parts = []
    for key in ("district", "locality", "city"):
        value = props.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in parts:
            parts.append(value.strip())
    return ", ".join(parts[:2])


def format_features(features: list[Any]) -> list[dict[str, Any]]:
    """Photon features to ``{label, detail, lat, lng}``, only real places inside Pasto.

    Malformed features are skipped, never fatal. At most ``MAX_RESULTS``. O(n).
    """
    results: list[dict[str, Any]] = []
    for feature in features:
        try:
            lng, lat = feature["geometry"]["coordinates"][:2]
            lat, lng = float(lat), float(lng)
            props = feature.get("properties") or {}
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        label = _label(props)
        if label is None or not inside_pasto(lat, lng):
            continue
        results.append({"label": label, "detail": _detail(props), "lat": lat, "lng": lng})
        if len(results) == MAX_RESULTS:
            break
    return results

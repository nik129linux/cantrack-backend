"""Photon (OpenStreetMap) address search for the pickup field, with a small in-memory cache."""

from collections import OrderedDict
from typing import Any

import httpx

from .navigation import PASTO_BOUNDS, inside_pasto

SEARCH_URL = "https://photon.komoot.io/api/"
REVERSE_URL = "https://photon.komoot.io/reverse"

#: No ``lang`` parameter on purpose: Photon only supports default/de/en/fr and answers 400 to
#: ``lang=es``. The default language is the local one (Spanish names for Pasto).
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
            params={"lat": lat, "lon": lng, "limit": 1},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json().get("features", [])


def get_geocoder() -> PhotonClient:
    """The geocoder for ``/geocode``; tests override this so nothing touches the network. O(1)."""
    return PhotonClient()


def _text(props: dict[str, Any], key: str) -> str | None:
    """A non-blank string property, trimmed. O(1)."""
    value = props.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _street_label(props: dict[str, Any]) -> str | None:
    """``street number``, or just the street. O(1)."""
    street = _text(props, "street")
    if street is None:
        return None
    number = _text(props, "housenumber")
    return f"{street} {number}" if number is not None else street


def _label(props: dict[str, Any], address_first: bool) -> str | None:
    """The bold line of a result. O(1).

    A search is for a place, so the name wins (``Unicentro``). A reverse lookup is for a point, and
    the nearest object is often an all-caps school or a shop: the street is the better address.
    """
    name = _text(props, "name")
    street = _street_label(props)
    first, second = (street, name) if address_first else (name, street)
    return first if first is not None else second


def _detail(props: dict[str, Any]) -> str:
    """The muted line of a result: the barrio first, then the comuna (or the city). O(1)."""
    parts: list[str] = []
    for key in ("locality", "district", "city"):
        value = props.get(key)
        if isinstance(value, str) and value.strip() and value.strip() not in parts:
            parts.append(value.strip())
    return ", ".join(parts[:2])


#: Same name within this many degrees (about 220 m) is the same place mapped twice.
SAME_PLACE_DEGREES = 0.002


def _same_place(label: str, lat: float, lng: float, found: dict[str, Any]) -> bool:
    """Whether a result is another copy of one already kept. O(1)."""
    return (
        found["label"].casefold() == label.casefold()
        and abs(found["lat"] - lat) <= SAME_PLACE_DEGREES
        and abs(found["lng"] - lng) <= SAME_PLACE_DEGREES
    )


def format_features(features: list[Any], address_first: bool = False) -> list[dict[str, Any]]:
    """Photon features to ``{label, detail, lat, lng}``, only real places inside Pasto.

    Malformed features are skipped, never fatal, and a place OpenStreetMap maps several times (the
    Plaza de Nariño is three objects a metre apart) is listed once (same name within about 200 m). At most ``MAX_RESULTS``. O(n).

    Args:
        features: The raw Photon features.
        address_first: Prefer the street over the place name (reverse lookups).
    """
    results: list[dict[str, Any]] = []
    for feature in features:
        try:
            lng, lat = feature["geometry"]["coordinates"][:2]
            lat, lng = float(lat), float(lng)
            props = feature.get("properties") or {}
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        label = _label(props, address_first)
        if label is None or not inside_pasto(lat, lng):
            continue
        if any(_same_place(label, lat, lng, found) for found in results):
            continue
        results.append({"label": label, "detail": _detail(props), "lat": lat, "lng": lng})
        if len(results) == MAX_RESULTS:
            break
    return results

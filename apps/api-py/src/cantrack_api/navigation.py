"""Street-by-street walking directions for "Cómo llegar" (W4 Rutas).

The app draws the way to a pickup on its own map, so it needs the shape of the
streets and the turns along them. OSRM answers both: this module is the thin
client, the Spanish wording of each turn and a small cache, in the same shape
as ``weather.py``. Nothing here knows who is asking — only two points go out.

The server is configurable through ``CANTRACK_OSRM_URL`` (a self-hosted OSRM
is a drop-in replacement for the public demo). When it fails, times out or
answers something unexpected, the caller falls back to the straight line, so a
walker is never left without a direction.
"""

import os
from datetime import datetime
from typing import Any

import httpx

#: OSRM base URL. The public demo is for light use; set the env var to self-host.
OSRM_URL = os.environ.get("CANTRACK_OSRM_URL", "https://router.project-osrm.org").rstrip("/")

#: A generic client name. No user, project or contact detail is ever sent.
USER_AGENT = "CanTrack/1.0 (walking directions)"

#: Seconds to wait for OSRM before falling back to the straight line.
TIMEOUT_SECONDS = 5.0

#: Cache of parsed routes keyed by both points rounded to ~11 m.
ROUTE_CACHE: dict[tuple[float, float, float, float], dict[str, Any]] = {}
MAX_CACHE_ENTRIES = 256
CACHE_MAX_AGE_SECONDS = 30 * 60

#: Pasto and its surroundings. Outside this box the endpoint answers 400, so the
#: API is never a free routing proxy for the whole world.
PASTO_BOUNDS = {"south": 1.12, "west": -77.36, "north": 1.30, "east": -77.20}


def inside_pasto(lat: float, lng: float) -> bool:
    """Whether a point is inside the service area. O(1)."""
    return (
        PASTO_BOUNDS["south"] <= lat <= PASTO_BOUNDS["north"]
        and PASTO_BOUNDS["west"] <= lng <= PASTO_BOUNDS["east"]
    )


def cache_key(from_lat: float, from_lng: float, to_lat: float, to_lng: float) -> tuple[float, float, float, float]:
    """The cache cell of a trip: both points rounded to four decimals. O(1)."""
    return (round(from_lat, 4), round(from_lng, 4), round(to_lat, 4), round(to_lng, 4))


def cache_get(key: tuple[float, float, float, float], now: datetime) -> dict[str, Any] | None:
    """Return a fresh cached route for the cell, or None. O(1)."""
    entry = ROUTE_CACHE.get(key)
    if entry is None:
        return None
    if (now - entry["at"]).total_seconds() > CACHE_MAX_AGE_SECONDS:
        del ROUTE_CACHE[key]
        return None
    return entry["route"]


def cache_put(key: tuple[float, float, float, float], route: dict[str, Any], now: datetime) -> None:
    """Store a route, dropping expired entries and then the oldest when full.

    O(n) in the cache size (at most ``MAX_CACHE_ENTRIES``), paid only when OSRM
    was actually called.
    """
    for expired in [
        cell for cell, entry in ROUTE_CACHE.items()
        if (now - entry["at"]).total_seconds() > CACHE_MAX_AGE_SECONDS
    ]:
        del ROUTE_CACHE[expired]
    while len(ROUTE_CACHE) >= MAX_CACHE_ENTRIES and key not in ROUTE_CACHE:
        oldest = min(ROUTE_CACHE, key=lambda cell: ROUTE_CACHE[cell]["at"])
        del ROUTE_CACHE[oldest]
    ROUTE_CACHE[key] = {"at": now, "route": route}


class OsrmClient:
    """Thin client over the OSRM foot profile."""

    def foot_route(
        self, from_lat: float, from_lng: float, to_lat: float, to_lng: float
    ) -> dict[str, Any]:
        """Fetch the walking route between two points. O(1) calls.

        Steps carry their own geometry, so the drawn line and the turn list
        always agree; the overview is not requested.

        Args:
            from_lat: Latitude of the start.
            from_lng: Longitude of the start.
            to_lat: Latitude of the destination.
            to_lng: Longitude of the destination.

        Returns:
            The decoded OSRM JSON payload.

        Raises:
            httpx.HTTPError: On any transport or non-2xx failure.
            ValueError: When the body is not valid JSON.
        """
        path = f"{from_lng},{from_lat};{to_lng},{to_lat}"
        response = httpx.get(
            f"{OSRM_URL}/route/v1/foot/{path}",
            params={
                "geometries": "geojson",
                "steps": "true",
                "overview": "false",
                "alternatives": "false",
            },
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()


def get_route_source() -> OsrmClient:
    """Return the routing source. Tests override it so no network is touched. O(1)."""
    return OsrmClient()


#: How a turn reads in Spanish, by OSRM's ``maneuver.modifier``.
MODIFIERS = {
    "left": "a la izquierda",
    "right": "a la derecha",
    "slight left": "levemente a la izquierda",
    "slight right": "levemente a la derecha",
    "sharp left": "cerrado a la izquierda",
    "sharp right": "cerrado a la derecha",
    "straight": "derecho",
    "uturn": "en U",
}


def _at(name: str) -> str:
    """The `` por <calle>`` tail of an instruction, empty for an unnamed way. O(1)."""
    return f" por {name}" if name else ""


def _on(name: str) -> str:
    """The `` en <calle>`` tail of an instruction, empty for an unnamed way. O(1)."""
    return f" en {name}" if name else ""


def _depart(modifier: str, name: str, maneuver: dict[str, Any]) -> str:
    return f"Empieza a caminar{_at(name)}"


def _arrive(modifier: str, name: str, maneuver: dict[str, Any]) -> str:
    return "Llegaste al punto de recogida"


def _roundabout(modifier: str, name: str, maneuver: dict[str, Any]) -> str:
    exit_number = maneuver.get("exit")
    if isinstance(exit_number, int):
        return f"En la glorieta, toma la salida {exit_number}{_at(name)}"
    return f"En la glorieta, sigue{_at(name)}"


def _leave_roundabout(modifier: str, name: str, maneuver: dict[str, Any]) -> str:
    return f"Sal de la glorieta{_at(name)}"


def _end_of_road(modifier: str, name: str, maneuver: dict[str, Any]) -> str:
    if modifier and modifier != "derecho":
        return f"Al final de la calle, gira {modifier}{_on(name)}"
    return f"Al final de la calle, sigue derecho{_at(name)}"


def _keep(modifier: str, name: str, maneuver: dict[str, Any]) -> str:
    return f"Mantente {modifier or 'derecho'}{_on(name)}"


def _follow(modifier: str, name: str, maneuver: dict[str, Any]) -> str:
    return f"Sigue{_at(name)}" if name else "Sigue derecho"


def _continue(modifier: str, name: str, maneuver: dict[str, Any]) -> str:
    if modifier == "en U":
        return f"Da la vuelta{_on(name)}"
    return f"Sigue derecho{_at(name)}"


def _turn(modifier: str, name: str, maneuver: dict[str, Any]) -> str:
    if not modifier or modifier == "derecho":
        return f"Sigue derecho{_at(name)}"
    if modifier == "en U":
        return f"Da la vuelta{_on(name)}"
    return f"Gira {modifier}{_on(name)}"


#: One wording per OSRM ``maneuver.type``; anything else just follows the street.
_HANDLERS = {
    "depart": _depart,
    "arrive": _arrive,
    "roundabout": _roundabout,
    "rotary": _roundabout,
    "roundabout turn": _roundabout,
    "exit roundabout": _leave_roundabout,
    "exit rotary": _leave_roundabout,
    "end of road": _end_of_road,
    "fork": _keep,
    "merge": _keep,
    "new name": _follow,
    "continue": _continue,
    "turn": _turn,
}


def step_text(maneuver: dict[str, Any], name: str) -> str:
    """Turn one OSRM maneuver into an instruction a walker can follow. O(1).

    Args:
        maneuver: The step's ``maneuver`` object (``type``, ``modifier``, ``exit``).
        name: The street name of the step, possibly empty.

    Returns:
        The instruction in Spanish, for example ``"Gira a la derecha en Carrera 22"``.
    """
    kind = str(maneuver.get("type") or "")
    modifier = MODIFIERS.get(str(maneuver.get("modifier") or ""), "")
    return _HANDLERS.get(kind, _follow)(modifier, name, maneuver)


def _append_step(
    geometry: list[list[float]], steps: list[dict[str, Any]], step: dict[str, Any]
) -> None:
    """Add one OSRM step to the drawn line and the turn list. O(p) in its points.

    A step starts where the previous one ended, so that repeated joint point is
    added once and the step records the index it starts at.
    """
    points = ((step.get("geometry") or {}).get("coordinates")) or []
    if not points:
        return
    first = [round(float(points[0][1]), 6), round(float(points[0][0]), 6)]
    if geometry and geometry[-1] == first:
        start_index = len(geometry) - 1
        points = points[1:]
    else:
        start_index = len(geometry)
    geometry.extend([[round(float(lat), 6), round(float(lng), 6)] for lng, lat in points])
    steps.append(
        {
            "text": step_text(step.get("maneuver") or {}, str(step.get("name") or "").strip()),
            "distanceM": int(round(float(step.get("distance") or 0.0))),
            "startIndex": start_index,
        }
    )


def parse_route(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Turn an OSRM answer into the shape the app draws. O(p), p the points.

    The line is the steps' own geometries joined end to end (a repeated joint
    point is dropped), so every step knows where it starts on the drawn line.

    Args:
        payload: The decoded OSRM JSON.

    Returns:
        ``{distanceKm, minutes, geometry, steps}`` with ``geometry`` as
        ``[[lat, lng], ...]``, or None when the payload holds no usable route.
    """
    routes = payload.get("routes")
    if not isinstance(routes, list) or not routes:
        return None
    route = routes[0]
    legs = route.get("legs")
    if not isinstance(legs, list) or not legs:
        return None

    geometry: list[list[float]] = []
    steps: list[dict[str, Any]] = []
    for leg in legs:
        for step in leg.get("steps") or []:
            _append_step(geometry, steps, step)

    if len(geometry) < 2:
        return None
    distance_km = float(route.get("distance") or 0.0) / 1000.0
    duration_min = float(route.get("duration") or 0.0) / 60.0
    return {
        "source": "osrm",
        "distanceKm": round(distance_km, 3),
        "minutes": max(1, int(round(duration_min))),
        "geometry": geometry,
        "steps": steps,
    }

"""The recommended fare and the ceiling built on it.

The recommended fare is the median agreed price per minute near the pickup
(at least five samples in the last 60 days) times the duration, or a flat
rate per minute when there is no market yet. A price may never go above
``MAX_FARE_MULTIPLIER`` times that recommendation: owners cannot post such a
fare and walkers cannot counter above it.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from supabase import Client

from .bidding import DEFAULT_CONFIG
from .db import run_query
from .geo import haversine_km

#: How far back agreed prices stay representative for the suggestion.
MARKET_WINDOW_DAYS = 60

#: Fallback fare: COP per minute, and the rounding/minimum in COP.
DEFAULT_COP_PER_MINUTE = 330
MIN_FARE_COP = 8000
FARE_STEP_COP = 1000

#: A fare or a counter offer may not pass this many times the recommended fare.
MAX_FARE_MULTIPLIER = 3

#: Samples needed before the market replaces the flat rate.
MIN_MARKET_SAMPLES = 5

REQUESTS_TABLE = "walk_requests"
OFFERS_TABLE = "walk_offers"
AGREED_STATUSES = ["assigned", "in_walk", "done"]


def _median(values: list[float]) -> float:
    """Return the median of a non-empty list. O(n log n) (sorting dominates).

    An even-sized list answers the mean of the two middle values.
    """
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def max_fare_cop(suggested_cop: int) -> int:
    """The highest price allowed for a recommended fare. O(1)."""
    return suggested_cop * MAX_FARE_MULTIPLIER


@dataclass(frozen=True)
class Suggestion:
    """A recommended fare and where it came from."""

    suggested_cop: int
    basis: str
    sample_size: int

    @property
    def ceiling_cop(self) -> int:
        """The most any fare or counter offer may be."""
        return max_fare_cop(self.suggested_cop)


class FareMarket:
    """Agreed prices per minute of the last 60 days, loaded once per request.

    Loading costs two queries however many walks are then priced (the walker
    inbox prices every card from one load).
    """

    def __init__(self, samples: list[tuple[float, float, float]]) -> None:
        #: ``(pickup_lat, pickup_lng, price_per_minute)`` per agreed walk.
        self._samples = samples

    @classmethod
    def load(cls, supabase: Client, now: datetime) -> "FareMarket":
        """Read the agreed walks and their winning offers.

        Big-O: O(w + o), where w is the agreed walks and o their offers.
        """
        cutoff = now - timedelta(days=MARKET_WINDOW_DAYS)
        rows = run_query(
            supabase.table(REQUESTS_TABLE).select("*").in_("status", AGREED_STATUSES)
        )
        recent: list[dict[str, Any]] = []
        for row in rows:
            if not row.get("walker_id"):
                continue
            try:
                created_at = datetime.fromisoformat(row["created_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if created_at >= cutoff:
                recent.append(row)
        if not recent:
            return cls([])

        offers = run_query(
            supabase.table(OFFERS_TABLE)
            .select("*")
            .in_("walk_request_id", [row["id"] for row in recent])
        )
        by_pair = {
            (offer["walk_request_id"], offer["walker_id"]): offer for offer in offers
        }
        samples: list[tuple[float, float, float]] = []
        for row in recent:
            offer = by_pair.get((row["id"], row["walker_id"]))
            if offer is None:
                continue
            try:
                minutes = float(row["duration_hours"]) * 60.0
                price = float(offer["price_cop"])
                lat = float(row["pickup_lat"])
                lng = float(row["pickup_lng"])
            except (KeyError, TypeError, ValueError):
                continue
            if minutes <= 0:
                continue
            samples.append((lat, lng, price / minutes))
        return cls(samples)

    def suggest(self, minutes: float, lat: float, lng: float) -> Suggestion:
        """Recommend a fare for a walk of ``minutes`` picked up at ``lat, lng``.

        Big-O: O(s) for the s loaded samples (the median sort is O(k log k)
        for the k that fall inside the bidding radius).
        """
        nearby = [
            per_minute
            for sample_lat, sample_lng, per_minute in self._samples
            if haversine_km(lat, lng, sample_lat, sample_lng) <= DEFAULT_CONFIG.radius_km
        ]
        if len(nearby) >= MIN_MARKET_SAMPLES:
            basis = "market"
            raw = _median(nearby) * minutes
        else:
            basis = "default"
            raw = DEFAULT_COP_PER_MINUTE * minutes
        rounded = int(math.floor(raw / FARE_STEP_COP + 0.5)) * FARE_STEP_COP
        return Suggestion(max(MIN_FARE_COP, rounded), basis, len(nearby))

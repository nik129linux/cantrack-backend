"""Walk-bidding offer scoring (FR-23, FR-24).

Pure, I/O-free scoring helpers: min-max normalisation across the offers on a
single walk request, the weighted final score, and the best-first ranking that
breaks ties by earliest submission.

No standard library structures are used (NFR-03); only plain lists and
dicts. Every term is recomputed from scratch per call so the module stays
deterministic and clock-free.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BiddingConfig:
    """Tunable knobs for the walk-bidding scoring model (FR-23).

    Big-O: construction and field access are O(1).
    """

    radius_km: float = 2.0
    detour_limit_min: float = 10.0
    auto_pick_after_min: float = 10.0
    walker_capacity: int = 4
    price_weight: float = 0.4
    distance_weight: float = 0.3
    rating_weight: float = 0.3
    empty_bucket_penalty: float = 0.8
    cancel_tokens_per_week: int = 3
    no_show_grace_min: float = 15.0
    rating_window_size: int = 25


DEFAULT_CONFIG: BiddingConfig = BiddingConfig()


@dataclass(frozen=True)
class WalkOffer:
    """One walker's bid on a single walk request (FR-23).

    ``recent_rating`` is the window average (``None`` when unrated). ``bucket_empty``
    indicates whether the walker has spent every cancel token this week.
    """

    offer_id: str
    walker_id: str
    price_cop: int
    distance_km: float
    recent_rating: float | None
    bucket_empty: bool
    created_at: float


def _min_max_higher_better(values: list[float]) -> list[float]:
    """Normalise so the largest raw value becomes 1.0, the smallest 0.0.

    All-equal (or single) input yields 1.0 for every entry. O(n).
    """

    lowest = min(values)
    highest = max(values)
    span = highest - lowest
    if span == 0.0:
        return [1.0 for _ in values]
    return [(value - lowest) / span for value in values]


def _min_max_lower_better(values: list[float]) -> list[float]:
    """Normalise so the smallest raw value becomes 1.0, the largest 0.0.

    All-equal (or single) input yields 1.0 for every entry. O(n).
    """

    lowest = min(values)
    highest = max(values)
    span = highest - lowest
    if span == 0.0:
        return [1.0 for _ in values]
    return [(highest - value) / span for value in values]


def score_offers(walk_offers: list[WalkOffer], config: BiddingConfig = DEFAULT_CONFIG) -> dict[str, float]:
    """Return ``{offer_id: final_score}`` for every offer on one request.

    Each term (price, distance, rating) is min-max normalised to 0..1 across
    the offers on the same request: lower price and lower distance score
    higher, higher rating scores higher (``None`` counts as raw 0.0). A single
    offer scores 1.0 on every term; an all-equal term scores 1.0 for every
    offer. The weighted sum is multiplied by ``empty_bucket_penalty`` while the
    walker's bucket is empty.

    Big-O: O(n), where n is the number of offers.
    """

    if not walk_offers:
        return {}

    prices = [float(offer.price_cop) for offer in walk_offers]
    distances = [offer.distance_km for offer in walk_offers]
    ratings = [0.0 if offer.recent_rating is None else float(offer.recent_rating) for offer in walk_offers]

    price_terms = _min_max_lower_better(prices)
    distance_terms = _min_max_lower_better(distances)
    rating_terms = _min_max_higher_better(ratings)

    scores: dict[str, float] = {}
    for index, offer in enumerate(walk_offers):
        base_score = (
            config.price_weight * price_terms[index]
            + config.distance_weight * distance_terms[index]
            + config.rating_weight * rating_terms[index]
        )
        if offer.bucket_empty:
            base_score *= config.empty_bucket_penalty
        scores[offer.offer_id] = base_score
    return scores


def rank_offers(walk_offers: list[WalkOffer], config: BiddingConfig = DEFAULT_CONFIG) -> list[str]:
    """Return offer ids best-first, ties broken by earliest ``created_at``.

    Scores are computed via :func:`score_offers` and offers are ordered by
    descending score, then ascending ``created_at``.

    Big-O: O(n log n), dominated by the comparison sort; scoring is O(n).
    """

    if not walk_offers:
        return []

    scores = score_offers(walk_offers, config)
    ordered_offers = sorted(
        walk_offers,
        key=lambda offer: (-scores[offer.offer_id], offer.created_at),
    )
    return [offer.offer_id for offer in ordered_offers]

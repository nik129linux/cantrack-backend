"""Cancellation budget with weekly refill (FR-26)."""

from __future__ import annotations

import math


class TokenBucket:
    """Cancellation budget that refills to capacity each period (FR-26).

    The bucket starts full. Refill boundaries fall at
    ``started_at + k * refill_period`` for integer ``k >= 1``. Crossing at
    least one boundary since the last call refills the bucket to
    ``capacity``. This is a whole-period refill, not a drip.

    Backing store: three scalars (capacity, refill period, remaining tokens)
    plus the last-seen timestamp.

    Big-O:
    - available: O(1)
    - take: O(1)
    """

    __slots__ = ("_capacity", "_refill_period", "_started_at", "_remaining", "_last_seen")

    def __init__(self, capacity: int, refill_period: float, started_at: float) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        if refill_period <= 0:
            raise ValueError("refill_period must be positive")
        self._capacity: int = capacity
        self._refill_period: float = refill_period
        self._started_at: float = started_at
        self._remaining: int = capacity
        self._last_seen: float | None = None

    def _period_index(self, moment: float) -> int:
        """Return the period index for ``moment``. O(1)."""
        if moment < self._started_at:
            return -1
        return int(math.floor((moment - self._started_at) / self._refill_period))

    def _maybe_refill(self, now: float) -> None:
        """Refill to capacity if a boundary was crossed since last call. O(1)."""
        if self._last_seen is None:
            return
        if self._period_index(now) > self._period_index(self._last_seen):
            self._remaining = self._capacity

    def available(self, now: float) -> int:
        """Return how many tokens are available at ``now``. O(1).

        Raises ValueError if ``now`` is earlier than the last seen time.
        """
        if self._last_seen is not None and now < self._last_seen:
            raise ValueError("clock went backwards")
        self._maybe_refill(now)
        self._last_seen = now
        return self._remaining

    def take(self, now: float) -> bool:
        """Try to spend one token at ``now``. O(1).

        Returns True and spends one token when at least one is available,
        otherwise False. Raises ValueError if ``now`` is earlier than the
        last seen time.
        """
        if self._last_seen is not None and now < self._last_seen:
            raise ValueError("clock went backwards")
        self._maybe_refill(now)
        self._last_seen = now
        if self._remaining > 0:
            self._remaining -= 1
            return True
        return False

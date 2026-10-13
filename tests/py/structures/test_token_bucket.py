import pytest

from cantrack_api.structures import TokenBucket

WEEK = 7 * 24 * 3600.0


class TestTokenBucket:
    def test_starts_full(self):
        cancel_bucket = TokenBucket(capacity=3, refill_period=WEEK, started_at=0.0)
        assert cancel_bucket.available(now=0.0) == 3

    def test_take_spends_tokens_until_empty(self):
        cancel_bucket = TokenBucket(capacity=3, refill_period=WEEK, started_at=0.0)
        assert [cancel_bucket.take(now=10.0) for _ in range(4)] == [True, True, True, False]
        assert cancel_bucket.available(now=10.0) == 0

    def test_no_refill_before_a_full_period(self):
        cancel_bucket = TokenBucket(capacity=3, refill_period=WEEK, started_at=0.0)
        for _ in range(3):
            cancel_bucket.take(now=1.0)
        assert cancel_bucket.available(now=WEEK - 1) == 0

    def test_refills_to_capacity_after_a_period(self):
        cancel_bucket = TokenBucket(capacity=3, refill_period=WEEK, started_at=0.0)
        for _ in range(3):
            cancel_bucket.take(now=1.0)
        assert cancel_bucket.available(now=WEEK) == 3

    def test_refill_never_exceeds_capacity(self):
        cancel_bucket = TokenBucket(capacity=3, refill_period=WEEK, started_at=0.0)
        assert cancel_bucket.available(now=10 * WEEK) == 3

    def test_clock_going_backwards_is_rejected(self):
        cancel_bucket = TokenBucket(capacity=3, refill_period=WEEK, started_at=100.0)
        cancel_bucket.take(now=200.0)
        with pytest.raises(ValueError):
            cancel_bucket.take(now=150.0)

    def test_clock_going_backwards_is_rejected_on_available(self):
        cancel_bucket = TokenBucket(capacity=3, refill_period=WEEK, started_at=100.0)
        cancel_bucket.available(now=200.0)
        with pytest.raises(ValueError):
            cancel_bucket.available(now=150.0)

    def test_spending_after_refill_starts_a_new_period(self):
        cancel_bucket = TokenBucket(capacity=3, refill_period=WEEK, started_at=0.0)
        for _ in range(3):
            cancel_bucket.take(now=1.0)
        assert cancel_bucket.take(now=WEEK) is True
        assert cancel_bucket.available(now=WEEK + 1) == 2
        assert cancel_bucket.available(now=2 * WEEK - 1) == 2
        assert cancel_bucket.available(now=2 * WEEK) == 3

    @pytest.mark.parametrize("capacity,period", [(0, WEEK), (-1, WEEK), (3, 0.0), (3, -5.0)])
    def test_invalid_configuration_is_rejected(self, capacity, period):
        with pytest.raises(ValueError):
            TokenBucket(capacity=capacity, refill_period=period, started_at=0.0)

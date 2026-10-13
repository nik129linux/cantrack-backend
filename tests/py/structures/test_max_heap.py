import math

import pytest

from cantrack_api.structures import MaxHeap


class TestMaxHeap:
    def test_starts_empty(self):
        offer_heap = MaxHeap()
        assert offer_heap.is_empty() is True
        assert offer_heap.size() == 0

    def test_pops_highest_priority_first(self):
        offer_heap = MaxHeap()
        offer_heap.push("offer-low", 0.2)
        offer_heap.push("offer-high", 0.9)
        offer_heap.push("offer-mid", 0.5)
        assert offer_heap.pop_max() == "offer-high"
        assert offer_heap.pop_max() == "offer-mid"
        assert offer_heap.pop_max() == "offer-low"
        assert offer_heap.is_empty() is True

    def test_equal_priority_earliest_push_wins(self):
        offer_heap = MaxHeap()
        offer_heap.push("first", 0.5)
        offer_heap.push("second", 0.5)
        offer_heap.push("third", 0.5)
        assert [offer_heap.pop_max() for _ in range(3)] == ["first", "second", "third"]

    def test_peek_does_not_remove(self):
        offer_heap = MaxHeap()
        offer_heap.push("only", 1.0)
        assert offer_heap.peek_max() == "only"
        assert offer_heap.size() == 1

    def test_many_values_come_out_sorted(self):
        offer_heap = MaxHeap()
        scores = [5, 1, 9, 3, 7, 2, 8, 6, 4, 0]
        for score in scores:
            offer_heap.push(score, float(score))
        assert [offer_heap.pop_max() for _ in scores] == sorted(scores, reverse=True)

    def test_expired_offers_are_skipped(self):
        offer_heap = MaxHeap()
        offer_heap.push("stale-best", 0.9, expires_at=100.0)
        offer_heap.push("live", 0.5, expires_at=500.0)
        assert offer_heap.pop_max(now=200.0) == "live"

    def test_expiry_is_inclusive_at_the_boundary(self):
        offer_heap = MaxHeap()
        offer_heap.push("ends-now", 0.9, expires_at=100.0)
        offer_heap.push("live", 0.1)
        assert offer_heap.peek_max(now=100.0) == "live"

    def test_without_now_nothing_expires(self):
        offer_heap = MaxHeap()
        offer_heap.push("old", 0.9, expires_at=1.0)
        assert offer_heap.pop_max() == "old"

    def test_all_expired_raises_index_error(self):
        offer_heap = MaxHeap()
        offer_heap.push("a", 0.9, expires_at=1.0)
        offer_heap.push("b", 0.8, expires_at=2.0)
        with pytest.raises(IndexError):
            offer_heap.pop_max(now=10.0)
        assert offer_heap.is_empty() is True

    def test_peek_with_all_expired_raises_and_discards(self):
        offer_heap = MaxHeap()
        offer_heap.push("a", 0.9, expires_at=1.0)
        with pytest.raises(IndexError):
            offer_heap.peek_max(now=10.0)
        assert offer_heap.size() == 0

    def test_pop_on_empty_raises_index_error(self):
        with pytest.raises(IndexError):
            MaxHeap().pop_max()

    def test_peek_on_empty_raises_index_error(self):
        with pytest.raises(IndexError):
            MaxHeap().peek_max()

    @pytest.mark.parametrize("bad_priority", [math.nan, math.inf, -math.inf])
    def test_non_finite_priority_is_rejected(self, bad_priority):
        with pytest.raises(ValueError):
            MaxHeap().push("offer", bad_priority)

    def test_none_is_a_storable_value(self):
        offer_heap = MaxHeap()
        offer_heap.push(None, 1.0)
        assert offer_heap.pop_max() is None

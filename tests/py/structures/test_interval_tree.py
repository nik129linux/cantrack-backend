"""FR-17: interval conflicts are detected with a hand-written IntervalTree.

The tree stores half-open intervals [start, end) keyed by ISO-8601 strings
(uniform UTC offset, so lexicographic order == chronological order) and
answers overlap queries: `search(start, end)` returns every value whose
interval overlaps the query window, where overlap means
`a.start < b.end and b.start < a.end` — touching intervals do NOT overlap.

Used by S1: accepting a walk request flags conflicts against the walker's
other accepted requests (each walk occupies [requested_time, +60 min)).

Beyond hand-picked cases the tree must survive randomized input (equivalence
against brute force, in three insertion orders) and 5000 ascending inserts —
a plain BST degenerates into a 5000-deep chain there and any recursive walk
blows the default recursion limit, so the implementation must stay balanced.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from cantrack_api.structures import IntervalTree

T09 = "2026-10-05T09:00:00+00:00"
T10 = "2026-10-05T10:00:00+00:00"
T1030 = "2026-10-05T10:30:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"
T1130 = "2026-10-05T11:30:00+00:00"
T12 = "2026-10-05T12:00:00+00:00"


class TestEmptyTree:
    def test_starts_empty(self):
        tree = IntervalTree()
        assert tree.is_empty() is True
        assert tree.size() == 0

    def test_search_on_empty_returns_empty_list(self):
        assert IntervalTree().search(T10, T11) == []


class TestInsertAndSearch:
    def test_finds_an_overlapping_interval(self):
        tree = IntervalTree()
        tree.insert(T10, T11, "r1")
        assert tree.search(T1030, T1130) == ["r1"]
        assert tree.size() == 1
        assert tree.is_empty() is False

    def test_disjoint_interval_is_not_found(self):
        tree = IntervalTree()
        tree.insert(T10, T11, "r1")
        assert tree.search(T1130, T12) == []

    def test_touching_intervals_do_not_overlap_end_to_start(self):
        tree = IntervalTree()
        tree.insert(T10, T11, "r1")
        # [11:00, 12:00) touches [10:00, 11:00) at 11:00 — half-open: no overlap
        assert tree.search(T11, T12) == []

    def test_touching_intervals_do_not_overlap_start_to_end(self):
        tree = IntervalTree()
        tree.insert(T10, T11, "r1")
        # [09:00, 10:00) touches at 10:00 — no overlap
        assert tree.search(T09, T10) == []

    def test_contained_interval_overlaps(self):
        tree = IntervalTree()
        tree.insert(T09, T12, "big")
        assert tree.search(T10, T11) == ["big"]

    def test_container_interval_overlaps(self):
        tree = IntervalTree()
        tree.insert(T10, T11, "small")
        assert tree.search(T09, T12) == ["small"]

    def test_shared_start_overlaps(self):
        tree = IntervalTree()
        tree.insert(T10, T11, "r1")
        assert tree.search(T10, T1030) == ["r1"]

    def test_multiple_hits_come_back_sorted_by_interval_start(self):
        tree = IntervalTree()
        tree.insert(T11, T12, "late")
        tree.insert(T09, T10, "early-out")  # does NOT overlap the query
        tree.insert(T10, T11, "middle")
        tree.insert(T1030, T1130, "spanning")
        # query [10:30, 11:30) overlaps middle ([10,11)), spanning and late ([11,12))
        assert tree.search(T1030, T1130) == ["middle", "spanning", "late"]

    def test_duplicate_values_are_reported_once_per_interval(self):
        tree = IntervalTree()
        tree.insert(T10, T11, "same")
        tree.insert(T1030, T1130, "same")
        assert tree.search(T10, T12) == ["same", "same"]

    def test_size_counts_every_insert(self):
        tree = IntervalTree()
        tree.insert(T09, T10, "a")
        tree.insert(T10, T11, "b")
        tree.insert(T11, T12, "c")
        assert tree.size() == 3


class TestInvalidIntervals:
    @pytest.mark.parametrize(
        "start,end",
        [(T11, T10), (T10, T10)],
    )
    def test_end_must_be_after_start(self, start, end):
        tree = IntervalTree()
        with pytest.raises(ValueError):
            tree.insert(start, end, "bad")
        with pytest.raises(ValueError):
            tree.search(start, end)

    def test_a_rejected_insert_stores_nothing(self):
        tree = IntervalTree()
        with pytest.raises(ValueError):
            tree.insert(T11, T10, "bad")
        assert tree.size() == 0
        assert tree.search(T09, T12) == []


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def iso(minutes: int) -> str:
    """A fixed-format UTC timestamp; lexicographic order == chronological order."""
    return (BASE + timedelta(minutes=minutes)).isoformat()


def brute_force(intervals: list[tuple[int, int, str]], qs: int, qe: int) -> list[str]:
    """Every value whose [start, end) overlaps [qs, qe), sorted by start."""
    hits = [(start, value) for start, end, value in intervals if start < qe and qs < end]
    hits.sort(key=lambda pair: pair[0])
    return [value for _start, value in hits]


class TestRandomizedEquivalence:
    def test_matches_brute_force_for_200_queries_in_three_insertion_orders(self):
        rng = random.Random(20261001)

        # 300 intervals with distinct starts (so the sorted output is unambiguous)
        starts = sorted(rng.sample(range(0, 100_000), 300))
        intervals: list[tuple[int, int, str]] = [
            (start, start + rng.randint(1, 180), f"v{i}") for i, start in enumerate(starts)
        ]

        # 200 random queries, plus explicit boundary-hugging ones (touching an
        # interval's end at the query start, and a start at the query end).
        queries: list[tuple[int, int]] = []
        for _ in range(200):
            qs = rng.randint(0, 100_000)
            qe = qs + rng.randint(1, 240)
            queries.append((qs, qe))
        first_start, first_end, _ = intervals[0]
        last_start, last_end, _ = intervals[-1]
        queries += [
            (first_end, first_end + 30),        # starts exactly where one ends
            (first_start - 30, first_start),    # ends exactly where one starts
            (first_start, first_end),           # exact same interval
            (last_start, last_end),
            (first_start, last_end),            # spans everything
        ]

        orders = {
            "ascending": intervals,
            "descending": list(reversed(intervals)),
            "shuffled": rng.sample(intervals, len(intervals)),
        }
        for order_name, ordered in orders.items():
            tree = IntervalTree()
            for start, end, value in ordered:
                tree.insert(iso(start), iso(end), value)
            assert tree.size() == 300, order_name
            for qs, qe in queries:
                assert tree.search(iso(qs), iso(qe)) == brute_force(intervals, qs, qe), (
                    f"{order_name} insertion order, query [{qs}, {qe})"
                )


class TestScale:
    def test_5000_ascending_inserts_stay_balanced_and_exact(self):
        tree = IntervalTree()
        count = 5000
        intervals = [(i * 10, i * 10 + 60, f"v{i}") for i in range(count)]
        # Ascending starts are the degenerate case for a plain BST: a recursive
        # insert/search would need 5000 stack frames and die with RecursionError
        # (default limit 1000). This must finish and stay exact.
        for start, end, value in intervals:
            tree.insert(iso(start), iso(end), value)
        assert tree.size() == count

        qs, qe = 25_000, 25_060
        assert tree.search(iso(qs), iso(qe)) == brute_force(intervals, qs, qe)

        # A query entirely past the last interval overlaps nothing.
        assert tree.search(iso(60_000), iso(60_100)) == []

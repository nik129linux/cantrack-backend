# Bidding Structures (Plan 1 of 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the five hand-written structures that the walk-bidding design needs: MaxHeap, WalkStateMachine, RatingWindow, TokenBucket and Quadtree.

**Architecture:** These are pure Python modules in `apps/api-py/src/cantrack_api/structures/`. They have no I/O, no clock reads and no Supabase access. Anything time-based takes `now` as an argument, so tests stay deterministic. Plan 2 wires them into the API (open walk requests, offers, a compare-and-set accept), and Plan 3 does the security hardening and negative tests.

**Tech Stack:** Python 3.12, `typing`/`dataclasses`/`enum`/`math` only (NFR-03), pytest.

Spec: `docs/superpowers/specs/2026-10-13-cantrack-bidding-design.md`. Requirements: `docs/REQUIREMENTS.md` (FR-23..FR-27, NFR-03, NFR-04, NFR-12, NFR-13).

## Rules for every task (read before starting)

- **No library structures.** Don't use `heapq`, `collections`, `bisect` or `queue`. Write it by hand (NFR-03).
- **Big-O docstrings.** The class docstring lists every public operation with its Big-O, and every public method's docstring states its own. Copy the style of `structures/stack.py`.
- **Domain names (NFR-13).** No `x`, `tmp`, `data`, `item` or `obj` outside internal node fields. Coordinate parameters are named `east`/`north`.
- **The tests are the contract.** Do NOT edit or weaken them. If a test looks wrong, stop and report it instead of changing it.
- **Exports.** Export each new class from `structures/__init__.py`, and add it to `__all__` in alphabetical order.
- **Running tests,** from `apps/api-py/`: `uv run --offline pytest ../../tests/py/structures/<file> -q`
- **Full suite before each commit:** `uv run --offline pytest -q`. The baseline is 562 passed, and no test may fail.
- **Commit:** one commit per task. The message is given in each task.

## File map

| File | Responsibility |
|---|---|
| `structures/max_heap.py` | Array-backed binary max-heap with tie-break by insertion order and lazy expiry |
| `structures/walk_state_machine.py` | Walk lifecycle states, events and the transition table |
| `structures/rating_window.py` | Fixed-capacity circular buffer of the last N ratings, plus the all-time average |
| `structures/token_bucket.py` | Cancellation budget with weekly refill |
| `structures/quadtree.py` | Point quadtree with radius query and max depth |
| `tests/py/structures/test_<name>.py` | One test file per structure |

---

### Task 1: MaxHeap

**Files:**
- Create: `apps/api-py/src/cantrack_api/structures/max_heap.py`
- Modify: `apps/api-py/src/cantrack_api/structures/__init__.py`
- Test: `tests/py/structures/test_max_heap.py`

**Interface:**
```python
class MaxHeap(Generic[T]):
    def push(self, value: T, priority: float, expires_at: float | None = None) -> None: ...
    def pop_max(self, now: float | None = None) -> T: ...   # IndexError if nothing live
    def peek_max(self, now: float | None = None) -> T: ...  # discards expired tops too
    def size(self) -> int: ...       # stored entries, may include not-yet-discarded expired ones
    def is_empty(self) -> bool: ...
```

Semantics:
- The highest priority comes out first. Between equal priorities, the one pushed earlier wins: keep an insertion counter and compare `(priority, -sequence)`.
- An entry with `expires_at is not None and expires_at <= now` is expired. `pop_max(now)` and `peek_max(now)` discard expired entries from the top until a live one is at the top. With `now=None`, nothing counts as expired.
- `priority` must be finite. NaN or inf raises `ValueError`.
- Backing store: a Python `list` used as the array, with hand-written sift-up and sift-down.

- [ ] **Step 1: Write the failing test** at `tests/py/structures/test_max_heap.py`:

```python
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
```

- [ ] **Step 2: Run it and confirm it fails.** `uv run --offline pytest ../../tests/py/structures/test_max_heap.py -q`. Expected: an ImportError on `MaxHeap`.
- [ ] **Step 3: Implement** `max_heap.py` to the interface above, with Big-O docstrings: push O(log n); pop_max O(log n) amortized, where each expired entry is discarded once; peek_max O(1) plus discards; size and is_empty O(1). Export it from `__init__.py`.
- [ ] **Step 4: Run it and confirm it passes.** Same command. Expected: all pass. Then run the full suite: 562 + new tests, 0 failed.
- [ ] **Step 5: Commit.** `git add -A apps/api-py/src/cantrack_api/structures tests/py/structures && git commit -m "feat(structures): hand-written MaxHeap for offer ranking (FR-23)"`

---

### Task 2: WalkStateMachine

**Files:**
- Create: `apps/api-py/src/cantrack_api/structures/walk_state_machine.py`
- Modify: `structures/__init__.py`
- Test: `tests/py/structures/test_walk_state_machine.py`

**Interface:**
```python
class WalkState(str, Enum):
    OPEN = "open"; ASSIGNED = "assigned"; IN_WALK = "in_walk"
    DONE = "done"; EXPIRED = "expired"; CANCELLED = "cancelled"

class WalkEvent(str, Enum):
    OWNER_PICK = "owner_pick"; AUTO_PICK = "auto_pick"; DEADLINE_EMPTY = "deadline_empty"
    OWNER_WITHDRAW = "owner_withdraw"; CHECK_IN = "check_in"; WALKER_CANCEL = "walker_cancel"
    NO_SHOW = "no_show"; OWNER_CANCEL = "owner_cancel"; CHECK_OUT = "check_out"

class IllegalTransition(Exception): ...  # message: f"cannot apply {event.value} to a walk in state {current.value}"

class WalkStateMachine:
    @staticmethod
    def next_state(current: WalkState, event: WalkEvent) -> WalkState: ...  # raises IllegalTransition
    @staticmethod
    def allowed_events(current: WalkState) -> frozenset[WalkEvent]: ...
    @staticmethod
    def is_terminal(state: WalkState) -> bool: ...
```

Transition table (taken directly from the spec, nothing else is allowed):

| From | Event | To |
|---|---|---|
| OPEN | OWNER_PICK, AUTO_PICK | ASSIGNED |
| OPEN | DEADLINE_EMPTY | EXPIRED |
| OPEN | OWNER_WITHDRAW | CANCELLED |
| ASSIGNED | CHECK_IN | IN_WALK |
| ASSIGNED | WALKER_CANCEL, NO_SHOW | OPEN |
| ASSIGNED | OWNER_CANCEL | CANCELLED |
| IN_WALK | CHECK_OUT | DONE |

DONE, EXPIRED and CANCELLED are terminal. The table is a module-level `dict[tuple[WalkState, WalkEvent], WalkState]`, so a lookup is O(1).

- [ ] **Step 1: Write the failing test** at `tests/py/structures/test_walk_state_machine.py`:

```python
import pytest

from cantrack_api.structures import (
    IllegalTransition,
    WalkEvent,
    WalkState,
    WalkStateMachine,
)

LEGAL = [
    (WalkState.OPEN, WalkEvent.OWNER_PICK, WalkState.ASSIGNED),
    (WalkState.OPEN, WalkEvent.AUTO_PICK, WalkState.ASSIGNED),
    (WalkState.OPEN, WalkEvent.DEADLINE_EMPTY, WalkState.EXPIRED),
    (WalkState.OPEN, WalkEvent.OWNER_WITHDRAW, WalkState.CANCELLED),
    (WalkState.ASSIGNED, WalkEvent.CHECK_IN, WalkState.IN_WALK),
    (WalkState.ASSIGNED, WalkEvent.WALKER_CANCEL, WalkState.OPEN),
    (WalkState.ASSIGNED, WalkEvent.NO_SHOW, WalkState.OPEN),
    (WalkState.ASSIGNED, WalkEvent.OWNER_CANCEL, WalkState.CANCELLED),
    (WalkState.IN_WALK, WalkEvent.CHECK_OUT, WalkState.DONE),
]


class TestWalkStateMachine:
    @pytest.mark.parametrize("current,event,expected", LEGAL)
    def test_legal_transitions(self, current, event, expected):
        assert WalkStateMachine.next_state(current, event) == expected

    def test_every_other_pair_is_illegal(self):
        legal_pairs = {(current, event) for current, event, _ in LEGAL}
        for current in WalkState:
            for event in WalkEvent:
                if (current, event) in legal_pairs:
                    continue
                with pytest.raises(IllegalTransition):
                    WalkStateMachine.next_state(current, event)

    def test_second_pick_on_assigned_walk_is_rejected(self):
        with pytest.raises(IllegalTransition):
            WalkStateMachine.next_state(WalkState.ASSIGNED, WalkEvent.OWNER_PICK)

    @pytest.mark.parametrize(
        "terminal", [WalkState.DONE, WalkState.EXPIRED, WalkState.CANCELLED]
    )
    def test_terminal_states_allow_nothing(self, terminal):
        assert WalkStateMachine.is_terminal(terminal) is True
        assert WalkStateMachine.allowed_events(terminal) == frozenset()

    def test_open_is_not_terminal(self):
        assert WalkStateMachine.is_terminal(WalkState.OPEN) is False

    def test_allowed_events_for_assigned(self):
        assert WalkStateMachine.allowed_events(WalkState.ASSIGNED) == frozenset(
            {
                WalkEvent.CHECK_IN,
                WalkEvent.WALKER_CANCEL,
                WalkEvent.NO_SHOW,
                WalkEvent.OWNER_CANCEL,
            }
        )

    def test_allowed_events_for_open_and_in_walk(self):
        assert WalkStateMachine.allowed_events(WalkState.OPEN) == frozenset(
            {
                WalkEvent.OWNER_PICK,
                WalkEvent.AUTO_PICK,
                WalkEvent.DEADLINE_EMPTY,
                WalkEvent.OWNER_WITHDRAW,
            }
        )
        assert WalkStateMachine.allowed_events(WalkState.IN_WALK) == frozenset(
            {WalkEvent.CHECK_OUT}
        )

    @pytest.mark.parametrize("live", [WalkState.ASSIGNED, WalkState.IN_WALK])
    def test_live_states_are_not_terminal(self, live):
        assert WalkStateMachine.is_terminal(live) is False

    def test_states_round_trip_through_database_strings(self):
        assert WalkState("in_walk") is WalkState.IN_WALK

    def test_illegal_transition_message_names_both_sides(self):
        with pytest.raises(IllegalTransition, match="done.*check_in|check_in.*done"):
            WalkStateMachine.next_state(WalkState.DONE, WalkEvent.CHECK_IN)
```

- [ ] **Step 2: Run it and confirm it fails** (ImportError).
- [ ] **Step 3: Implement** the module with the transition table and Big-O docstrings: next_state O(1), allowed_events O(E) where E is the number of events, is_terminal O(1). Export `IllegalTransition`, `WalkEvent`, `WalkState` and `WalkStateMachine`.
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite.
- [ ] **Step 5: Commit.** `git commit -m "feat(structures): walk lifecycle state machine (FR-24)"`

---

### Task 3: RatingWindow

**Files:**
- Create: `apps/api-py/src/cantrack_api/structures/rating_window.py`
- Modify: `structures/__init__.py`
- Test: `tests/py/structures/test_rating_window.py`

**Interface:**
```python
class RatingWindow:
    def __init__(self, capacity: int = 25) -> None: ...  # ValueError if capacity < 1
    def add(self, rating: float) -> None: ...            # ValueError unless 1 <= rating <= 5
    def recent_average(self) -> float | None: ...        # None when no ratings yet
    def all_time_average(self) -> float | None: ...
    def recent_count(self) -> int: ...
    def all_time_count(self) -> int: ...
```

Backing store: a fixed-size `list` of length `capacity`, plus a write index that wraps with modulo, plus a running window sum. When the buffer is full, subtract the overwritten rating from the sum. All operations are O(1). Keep the all-time totals in two separate counters.

- [ ] **Step 1: Write the failing test** at `tests/py/structures/test_rating_window.py`:

```python
import pytest

from cantrack_api.structures import RatingWindow


class TestRatingWindow:
    def test_empty_window_has_no_average(self):
        rating_window = RatingWindow()
        assert rating_window.recent_average() is None
        assert rating_window.all_time_average() is None
        assert rating_window.recent_count() == 0

    def test_average_before_wrap(self):
        rating_window = RatingWindow(capacity=3)
        for rating in (5, 4, 3):
            rating_window.add(rating)
        assert rating_window.recent_average() == pytest.approx(4.0)

    def test_oldest_rating_drops_out_after_wrap(self):
        rating_window = RatingWindow(capacity=3)
        for rating in (1, 5, 5, 5):
            rating_window.add(rating)
        assert rating_window.recent_average() == pytest.approx(5.0)
        assert rating_window.recent_count() == 3

    def test_all_time_keeps_every_rating(self):
        rating_window = RatingWindow(capacity=3)
        for rating in (1, 5, 5, 5):
            rating_window.add(rating)
        assert rating_window.all_time_average() == pytest.approx(4.0)
        assert rating_window.all_time_count() == 4

    def test_default_capacity_is_25(self):
        rating_window = RatingWindow()
        for _ in range(26):
            rating_window.add(4)
        assert rating_window.recent_count() == 25

    def test_many_wraps_stay_exact(self):
        rating_window = RatingWindow(capacity=2)
        for rating in [1, 2, 3, 4, 5] * 40:
            rating_window.add(rating)
        assert rating_window.recent_average() == pytest.approx(4.5)

    @pytest.mark.parametrize("bad_rating", [0, 0.99, 5.01, 6, -1])
    def test_out_of_range_rating_is_rejected(self, bad_rating):
        rating_window = RatingWindow()
        with pytest.raises(ValueError):
            rating_window.add(bad_rating)
        assert rating_window.all_time_count() == 0
        assert rating_window.recent_count() == 0
        assert rating_window.recent_average() is None

    def test_rejected_rating_leaves_existing_window_intact(self):
        rating_window = RatingWindow(capacity=3)
        rating_window.add(4)
        with pytest.raises(ValueError):
            rating_window.add(9)
        assert rating_window.recent_average() == pytest.approx(4.0)
        assert rating_window.recent_count() == 1

    @pytest.mark.parametrize("bad_capacity", [0, -3])
    def test_capacity_must_be_positive(self, bad_capacity):
        with pytest.raises(ValueError):
            RatingWindow(capacity=bad_capacity)
```

- [ ] **Step 2: Run it and confirm it fails.**
- [ ] **Step 3: Implement** it with every operation O(1) in the docstrings, and export it.
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite.
- [ ] **Step 5: Commit.** `git commit -m "feat(structures): circular-buffer RatingWindow for reputation (FR-26)"`

---

### Task 4: TokenBucket

**Files:**
- Create: `apps/api-py/src/cantrack_api/structures/token_bucket.py`
- Modify: `structures/__init__.py`
- Test: `tests/py/structures/test_token_bucket.py`

**Interface:**
```python
class TokenBucket:
    def __init__(self, capacity: int, refill_period: float, started_at: float) -> None: ...
    def available(self, now: float) -> int: ...
    def take(self, now: float) -> bool: ...   # True and spends one token, or False when empty
```

Semantics: the bucket starts full. Refill boundaries fall at `started_at + k * refill_period`. Crossing at least one boundary since the last call puts the bucket back to `capacity`. This is a whole-period refill, not a drip. `now` earlier than the last seen time raises `ValueError` (the clock went backwards). `capacity < 1` or `refill_period <= 0` raises `ValueError`. Every operation is O(1).

- [ ] **Step 1: Write the failing test** at `tests/py/structures/test_token_bucket.py`:

```python
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
```

- [ ] **Step 2: Run it and confirm it fails.**
- [ ] **Step 3: Implement** it and export it.
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite.
- [ ] **Step 5: Commit.** `git commit -m "feat(structures): TokenBucket cancellation budget (FR-26)"`

---

### Task 5: Quadtree

**Files:**
- Create: `apps/api-py/src/cantrack_api/structures/quadtree.py`
- Modify: `structures/__init__.py`
- Test: `tests/py/structures/test_quadtree.py`

**Interface** (planar metres; the API projects lat/lng to east/north in Plan 2):
```python
class Quadtree(Generic[K]):
    def __init__(self, min_east: float, min_north: float, max_east: float, max_north: float,
                 node_capacity: int = 4, max_depth: int = 12) -> None: ...
    def insert(self, key: K, east: float, north: float) -> None: ...  # ValueError outside bounds; re-insert of a key moves it
    def remove(self, key: K) -> bool: ...                             # False if key unknown
    def query_radius(self, east: float, north: float, radius: float) -> list[K]: ...  # ValueError if radius < 0
    def size(self) -> int: ...
```

Semantics:
- A leaf splits into 4 children when it holds more than `node_capacity` points and is above `max_depth`. At `max_depth` it just keeps accepting points, which caps the depth.
- A side index `dict[K, tuple[float, float]]` lets `remove` and the move-on-reinsert find a point. The dict is a lookup aid, not the structure itself.
- `query_radius` prunes whole quadrants whose rectangle doesn't intersect the circle. It includes points at distance ≤ radius and returns keys sorted by distance, nearest first.
- Bounds are inclusive on the min edge and inclusive on the max edge.
- Big-O: insert O(depth), remove O(depth + node_capacity), query_radius O(n) worst case and typically O(depth + k).

- [ ] **Step 1: Write the failing test** at `tests/py/structures/test_quadtree.py`:

```python
import pytest

from cantrack_api.structures import Quadtree


def make_tree(**overrides):
    return Quadtree(0.0, 0.0, 10_000.0, 10_000.0, **overrides)


class TestQuadtree:
    def test_empty_query_returns_nothing(self):
        nearby_walkers = make_tree()
        assert nearby_walkers.query_radius(5_000, 5_000, 2_000) == []
        assert nearby_walkers.size() == 0

    def test_finds_only_points_inside_radius_nearest_first(self):
        nearby_walkers = make_tree()
        nearby_walkers.insert("walker-near", 5_100, 5_000)
        nearby_walkers.insert("walker-mid", 5_000, 6_500)
        nearby_walkers.insert("walker-far", 9_900, 9_900)
        assert nearby_walkers.query_radius(5_000, 5_000, 2_000) == ["walker-near", "walker-mid"]

    def test_point_exactly_on_radius_is_included(self):
        nearby_walkers = make_tree()
        nearby_walkers.insert("edge", 7_000, 5_000)
        assert nearby_walkers.query_radius(5_000, 5_000, 2_000) == ["edge"]

    def test_splitting_keeps_every_point_findable(self):
        nearby_walkers = make_tree(node_capacity=2)
        for index in range(50):
            nearby_walkers.insert(f"walker-{index}", 100.0 * index, 100.0 * index)
        found = nearby_walkers.query_radius(0, 0, 20_000)
        assert sorted(found) == sorted(f"walker-{index}" for index in range(50))
        assert nearby_walkers.size() == 50

    def test_max_depth_caps_identical_points(self):
        nearby_walkers = make_tree(node_capacity=1, max_depth=3)
        for index in range(30):
            nearby_walkers.insert(f"same-spot-{index}", 2_500, 2_500)
        assert len(nearby_walkers.query_radius(2_500, 2_500, 1)) == 30

    def test_reinsert_moves_the_walker(self):
        nearby_walkers = make_tree()
        nearby_walkers.insert("walker", 1_000, 1_000)
        nearby_walkers.insert("walker", 9_000, 9_000)
        assert nearby_walkers.query_radius(1_000, 1_000, 500) == []
        assert nearby_walkers.query_radius(9_000, 9_000, 500) == ["walker"]
        assert nearby_walkers.size() == 1

    def test_remove(self):
        nearby_walkers = make_tree()
        nearby_walkers.insert("walker", 1_000, 1_000)
        assert nearby_walkers.remove("walker") is True
        assert nearby_walkers.remove("walker") is False
        assert nearby_walkers.query_radius(1_000, 1_000, 500) == []

    def test_corners_are_inside_bounds(self):
        nearby_walkers = make_tree()
        nearby_walkers.insert("min-corner", 0.0, 0.0)
        nearby_walkers.insert("max-corner", 10_000.0, 10_000.0)
        assert nearby_walkers.size() == 2

    @pytest.mark.parametrize("east,north", [(-1, 5), (5, -1), (10_001, 5), (5, 10_001)])
    def test_point_outside_bounds_is_rejected(self, east, north):
        with pytest.raises(ValueError):
            make_tree().insert("lost", east, north)

    def test_negative_radius_is_rejected(self):
        with pytest.raises(ValueError):
            make_tree().query_radius(0, 0, -1)
```

- [ ] **Step 2: Run it and confirm it fails.**
- [ ] **Step 3: Implement** it and export it.
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite.
- [ ] **Step 5: Commit.** `git commit -m "feat(structures): Quadtree radius query for nearby walkers (FR-16, FR-23)"`

---

## Done when

- 5 new modules plus 5 test files, each in its own commit, in this order.
- `uv run --offline pytest -q` → 562 + new tests pass, 0 failed.
- `grep -rnE "import (heapq|bisect|collections|queue)|from (heapq|bisect|collections|queue) " apps/api-py/src/cantrack_api/structures` → no output.

## Next plans (not in this file)

- **Plan 2, bidding API:** open walk requests, offers, offer scoring, a compare-and-set accept (`UPDATE … WHERE status='open'`), busy-walker eligibility, reputation wiring, replay.
- **Plan 3, hardening:** RLS audit (supabase skills), slowapi rate limits, upload limits, security headers, pip-audit in CI, Schemathesis fuzzing, and one negative test per endpoint (wrong owner, bad token, oversized file, flood).

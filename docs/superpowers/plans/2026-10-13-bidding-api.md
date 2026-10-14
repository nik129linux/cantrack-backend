# Walk-Bidding API (Plan 2 of 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the walk-bidding API on top of the Plan 1 structures: owners post open walk requests, nearby walkers bid, exactly one offer wins (compare-and-set), busy walkers stay eligible inside detour D, reputation and cancel tokens shape the ranking, and finished walks replay forward/backward with a GPS jump.

**Architecture:** A pure, I/O-free service layer (`bidding.py`, `bidding_eligibility.py`, `replay.py`: no Supabase, no clock reads, `now` always passed in) holds the scoring, eligibility and replay math so most logic is unit-testable. One new router (`routers/bidding.py`, prefix `/bidding`) owns the HTTP surface and persists through Supabase exactly like the existing routers. The S1 addressed-request flow in `routers/requests.py` (statuses `pending`/`accepted`/`declined`) is untouched and must keep passing: bidding rows use the new statuses (`open`/`assigned`/`in_walk`/`done`/`expired`) plus the shared `cancelled`, and bidding requests carry `walker_id = NULL` (broadcast), so no S1 query scoped by `walker_id = <caller>` ever matches them.

**Tech Stack:** Python 3.12, FastAPI, pydantic, Supabase (faked in tests), pytest.

Spec: `docs/superpowers/specs/2026-10-13-cantrack-bidding-design.md`. Requirements: `docs/REQUIREMENTS.md` (FR-23..FR-27, NFR-03, NFR-04, NFR-12, NFR-13). Plan 1: `docs/superpowers/plans/2026-10-13-bidding-structures.md` (already merged; structures exist).

## Rules for every task (read before starting)

- **No library structures.** Don't use `heapq`, `bisect`, `collections` or `queue` anywhere new (NFR-03). Binary search and ranking are hand-written.
- **Big-O docstrings.** Every new public function/method states its Big-O, in the style of `structures/stack.py` (NFR-04).
- **Domain names (NFR-13).** No `x`, `tmp`, `data`, `item` or `obj` outside structure internals. Instances are named `walk_request`, `offer_heap`, `nearby_walkers`, `rating_window`, `cancel_bucket`, `walk_timeline`.
- **Time is passed in.** Nothing new reads the clock; routers use the `get_now` dependency, pure modules take `now`/epoch floats. Tests use `+00:00` timestamps only (the fake normalises just `requested_time`/`responded_at`/`created_at` to UTC).
- **403, not 404, for foreign bidding rows.** When a bidding row belongs to another owner, answer 403 `Not your walk request.` (deliberate: the S1 routers use 404; the bidding router uses 403 so Postman can assert it).
- **The tests are the contract.** Do NOT edit or weaken them. If a test looks wrong, stop and report it instead of changing it.
- **Fake verdict (checked against `tests/py/api/fake_supabase.py`).** The fake already supports every query shape this plan needs: `select` with `eq`/`in_`/`order`/`limit`, `insert`, `update` with two `eq` filters returning the matched rows (`[]` when nothing matched, which is exactly what the compare-and-set accept needs), and generic tables via `seed()`. No fake extension is needed. `seed()` adds an `id` but no `created_at`: always pass `created_at` explicitly when order matters.
- **Running tests,** from `apps/api-py/`: `uv run --offline pytest ../../tests/py/api/<file> -q` (API) or `uv run --offline pytest ../../tests/py/api/<file> -q` (pure service tests live in the same directory).
- **Full suite before each commit:** `uv run --offline pytest -q`. The baseline is 638 passed, and no test may fail (this also guards the S1 flow).
- **Commit:** one commit per task. The message is given in each task.

## Build order and parallelism

- Task 1 (schema) goes first; every API task depends on it.
- Tasks 2, 3, 4 are pure service modules in different files: run them in parallel.
- Task 5 (post + visible list, schemas, router wiring) depends on 1, 2, 3.
- Tasks 6, 7, 8, 9, 10, 11 all extend `routers/bidding.py`: strictly sequential, in this order (6 → 7 → 8; 9 depends on 7; 10 depends on 6; 11 depends on 4 and 8).
- Task 12 (Postman) runs last, after every endpoint exists.

## File map

| File | Responsibility |
|---|---|
| `cantrack-db/apps/api-py/schema.sql` (append) | New statuses/columns on `walk_requests`, plus bidding tables (task 1) |
| `apps/api-py/src/cantrack_api/bidding.py` | `BiddingConfig` defaults, `WalkOffer`, min-max scoring and ranking (task 2); cancel-token spend helper (task 8) |
| `apps/api-py/src/cantrack_api/bidding_eligibility.py` | Lat/lng projection, free/busy eligibility, detour, nearby query (task 3) |
| `apps/api-py/src/cantrack_api/replay.py` | Timeline build/read over `DoublyLinkedList`, hand-written GPS binary search (task 4, endpoint in task 11) |
| `apps/api-py/src/cantrack_api/schemas.py` (extend) | Bidding request bodies (task 5) |
| `apps/api-py/src/cantrack_api/routers/bidding.py` | All `/bidding` endpoints (tasks 5-11) |
| `apps/api-py/src/cantrack_api/app.py` (one line) | Mount the bidding router (task 5) |
| `tests/py/api/test_bidding*.py`, `tests/py/api/test_replay*.py` | One test file per task |
| `cantrack-backend/postman/cantrack-bidding.postman_collection.json` | Postman v2.1 collection, happy path + negatives (task 12) |
| `cantrack-backend/postman/cantrack-bidding.postman_environment.json` | `baseUrl`, `ownerToken`, `walkerToken`, `walker2Token` variables, no secrets (task 12) |
| `cantrack-backend/postman/README.md` | Newman run section (task 12) |

---

### Task 1: Bidding schema

**Files:**
- Modify: `cantrack-db/apps/api-py/schema.sql` (append one `S4 bidding` block, idempotent `if not exists` style like the rest of the file)
- Test: `tests/py/api/test_bidding_schema.py`

Append to `schema.sql` (Nico runs it by hand in the Supabase SQL Editor):

```sql
-- S4: walk bidding (FR-23..FR-27). Column names match routers/bidding.py.
alter table public.walk_requests drop constraint if exists walk_requests_status_check;
alter table public.walk_requests add constraint walk_requests_status_check
  check (status in ('pending', 'accepted', 'declined', 'cancelled',
                    'open', 'assigned', 'in_walk', 'done', 'expired'));
alter table public.walk_requests alter column walker_id drop not null;
alter table public.walk_requests add column if not exists duration_hours double precision not null default 1.0;
alter table public.walk_requests add column if not exists window_end timestamptz;
alter table public.walk_requests add column if not exists deadline_at timestamptz;

create table if not exists public.walk_offers (
  id uuid primary key default gen_random_uuid(),
  walk_request_id uuid not null references public.walk_requests (id) on delete cascade,
  walker_id uuid not null references auth.users (id) on delete cascade,
  price_cop integer not null check (price_cop >= 0),
  eta_min double precision not null check (eta_min >= 0),
  distance_km double precision not null check (distance_km >= 0),
  created_at timestamptz not null default now(),
  unique (walk_request_id, walker_id)
);
create index if not exists walk_offers_request_idx on public.walk_offers (walk_request_id);

create table if not exists public.walker_positions (
  walker_id uuid primary key references auth.users (id) on delete cascade,
  lat double precision not null,
  lng double precision not null,
  updated_at timestamptz not null default now()
);

create table if not exists public.dog_pack_links (
  id uuid primary key default gen_random_uuid(),
  dog_a uuid not null references public.dogs (id) on delete cascade,
  dog_b uuid not null references public.dogs (id) on delete cascade,
  unique (dog_a, dog_b)
);

create table if not exists public.walk_graph_edges (
  id uuid primary key default gen_random_uuid(),
  from_stop text not null,
  to_stop text not null,
  minutes double precision not null check (minutes >= 0)
);
create index if not exists walk_graph_edges_from_idx on public.walk_graph_edges (from_stop);

create table if not exists public.walk_route_stops (
  id uuid primary key default gen_random_uuid(),
  walk_request_id uuid not null references public.walk_requests (id) on delete cascade,
  dog_id uuid not null references public.dogs (id) on delete cascade,
  stop_key text not null,
  pickup_time timestamptz not null,
  position integer not null
);
create index if not exists walk_route_stops_request_idx on public.walk_route_stops (walk_request_id);

create table if not exists public.walker_cancel_buckets (
  walker_id uuid primary key references auth.users (id) on delete cascade,
  remaining integer not null check (remaining >= 0),
  period_start timestamptz not null default now()
);

create table if not exists public.walk_ratings (
  id uuid primary key default gen_random_uuid(),
  walk_request_id uuid not null references public.walk_requests (id) on delete cascade,
  walker_id uuid not null references auth.users (id) on delete cascade,
  rating double precision not null check (rating >= 1 and rating <= 5),
  created_at timestamptz not null default now(),
  unique (walk_request_id)
);
create index if not exists walk_ratings_walker_idx on public.walk_ratings (walker_id, created_at desc);

create table if not exists public.walk_timeline (
  id uuid primary key default gen_random_uuid(),
  walk_request_id uuid not null references public.walk_requests (id) on delete cascade,
  kind text not null,
  at timestamptz not null,
  payload jsonb not null default '{}'::jsonb
);
create index if not exists walk_timeline_request_idx on public.walk_timeline (walk_request_id, at);

create table if not exists public.walk_gps_points (
  id uuid primary key default gen_random_uuid(),
  walk_request_id uuid not null references public.walk_requests (id) on delete cascade,
  at_epoch double precision not null,
  lat double precision not null,
  lng double precision not null
);
create index if not exists walk_gps_points_request_idx on public.walk_gps_points (walk_request_id, at_epoch);

alter table public.walk_offers enable row level security;
alter table public.walker_positions enable row level security;
alter table public.dog_pack_links enable row level security;
alter table public.walk_graph_edges enable row level security;
alter table public.walk_route_stops enable row level security;
alter table public.walker_cancel_buckets enable row level security;
alter table public.walk_ratings enable row level security;
alter table public.walk_timeline enable row level security;
alter table public.walk_gps_points enable row level security;
-- RLS stays policy-free on purpose (service-role API + caller-scoped queries, same as S1); the RLS audit is Plan 3.
```

**Interface:** SQL only; the contract the routers rely on is the column names above.

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_bidding_schema.py` (locks the table shapes and the compare-and-set query shape against the fake; fails until the SQL block exists as documentation of the contract — the fake itself needs no change):

```python
"""Task 1: the bidding tables accept the shapes routers/bidding.py uses."""


class TestBiddingSchema:
    def test_offer_round_trip(self, fake):
        walk = fake.seed(
            "walk_requests", owner_id="owner-a", walker_id=None, dog_id="dog-1",
            status="open", requested_time="2026-10-05T10:00:00+00:00",
            window_end="2026-10-05T11:00:00+00:00",
            deadline_at="2026-10-01T00:10:00+00:00",
            duration_hours=1.0, pickup_lat=6.2, pickup_lng=-75.5,
            price_cop=30000, created_at="2026-10-01T00:00:00+00:00",
        )
        offer = fake.seed(
            "walk_offers", walk_request_id=walk["id"], walker_id="walker-w",
            price_cop=28000, eta_min=12.0, distance_km=1.2,
            created_at="2026-10-01T00:01:00+00:00",
        )
        rows = fake.table("walk_offers").select("*").eq("walk_request_id", walk["id"]).execute().data
        assert [row["id"] for row in rows] == [offer["id"]]

    def test_compare_and_set_shape(self, fake):
        walk = fake.seed(
            "walk_requests", owner_id="owner-a", walker_id=None, dog_id="dog-1",
            status="open", requested_time="2026-10-05T10:00:00+00:00",
            created_at="2026-10-01T00:00:00+00:00",
        )
        won = (
            fake.table("walk_requests")
            .update({"status": "assigned"})
            .eq("id", walk["id"])
            .eq("status", "open")
            .execute()
            .data
        )
        assert len(won) == 1
        lost = (
            fake.table("walk_requests")
            .update({"status": "assigned"})
            .eq("id", walk["id"])
            .eq("status", "open")
            .execute()
            .data
        )
        assert lost == []

    def test_aux_tables_round_trip(self, fake):
        fake.seed("walker_positions", walker_id="walker-w", lat=6.2, lng=-75.5,
                 updated_at="2026-10-01T00:00:00+00:00")
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=3,
                 period_start="2026-10-01T00:00:00+00:00")
        fake.seed("walk_ratings", walk_request_id="walk-1", walker_id="walker-w",
                 rating=5.0, created_at="2026-10-01T00:00:00+00:00")
        fake.seed("walk_timeline", walk_request_id="walk-1", kind="check_in",
                 at="2026-10-05T10:00:00+00:00", payload={})
        fake.seed("walk_gps_points", walk_request_id="walk-1", at_epoch=1000.0,
                 lat=6.2, lng=-75.5)
        assert fake.table("walker_positions").select("*").execute().data[0]["walker_id"] == "walker-w"
        assert fake.table("walk_gps_points").select("*").execute().data[0]["at_epoch"] == 1000.0
```

- [ ] **Step 2: Run it and confirm it passes against the fake** (it locks shapes, not the SQL): `uv run --offline pytest ../../tests/py/api/test_bidding_schema.py -q`.
- [ ] **Step 3: Append** the SQL block to `schema.sql`, after the checkout-photos bucket insert.
- [ ] **Step 4: Run it and the full suite** (638 + 3 pass).
- [ ] **Step 5: Commit.** `git add -A cantrack-db/apps/api-py/schema.sql tests/py/api/test_bidding_schema.py && git commit -m "feat(db): bidding schema for walk requests, offers, reputation, replay (FR-23..FR-27)"`

---

### Task 2: Bidding config and offer scoring (parallel with 3, 4)

**Files:**
- Create: `apps/api-py/src/cantrack_api/bidding.py`
- Test: `tests/py/api/test_bidding_scoring.py`

**Interface:**

```python
@dataclass(frozen=True)
class BiddingConfig:
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
    offer_id: str
    walker_id: str
    price_cop: int
    distance_km: float
    recent_rating: float | None   # window average; None when unrated
    bucket_empty: bool
    created_at: float             # epoch seconds; earliest wins ties

def score_offers(walk_offers: list[WalkOffer], config: BiddingConfig = DEFAULT_CONFIG) -> dict[str, float]: ...
def rank_offers(walk_offers: list[WalkOffer], config: BiddingConfig = DEFAULT_CONFIG) -> list[str]: ...
```

Semantics (from the spec): each term is min-max normalised to 0..1 across the offers on the same request — lower price and lower distance score higher, higher rating scores higher (`None` counts as raw 0.0); a single offer scores 1.0 on every term; an all-equal term scores 1.0 for every offer. Final score is the weighted sum, multiplied by `empty_bucket_penalty` while the walker's bucket is empty. `rank_offers` returns offer ids best-first; ties break by earliest `created_at`. Both are O(n).

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_bidding_scoring.py`:

```python
from cantrack_api.bidding import DEFAULT_CONFIG, WalkOffer, rank_offers, score_offers


def make_offer(offer_id, price_cop=25000, distance_km=1.0, recent_rating=None,
               bucket_empty=False, created_at=1000.0, walker_id="walker-w"):
    return WalkOffer(offer_id=offer_id, walker_id=walker_id, price_cop=price_cop,
                     distance_km=distance_km, recent_rating=recent_rating,
                     bucket_empty=bucket_empty, created_at=created_at)


class TestBiddingScoring:
    def test_config_defaults_match_the_spec(self):
        assert (DEFAULT_CONFIG.radius_km, DEFAULT_CONFIG.detour_limit_min,
                DEFAULT_CONFIG.auto_pick_after_min, DEFAULT_CONFIG.walker_capacity) == (2.0, 10.0, 10.0, 4)
        assert (DEFAULT_CONFIG.price_weight, DEFAULT_CONFIG.distance_weight,
                DEFAULT_CONFIG.rating_weight, DEFAULT_CONFIG.empty_bucket_penalty) == (0.4, 0.3, 0.3, 0.8)
        assert (DEFAULT_CONFIG.cancel_tokens_per_week, DEFAULT_CONFIG.no_show_grace_min) == (3, 15.0)

    def test_single_offer_scores_one(self):
        assert score_offers([make_offer("only")]) == {"only": 1.0}

    def test_cheaper_closer_better_rated_wins(self):
        weak = make_offer("weak", price_cop=40000, distance_km=2.0, recent_rating=2.0, created_at=1000.0)
        strong = make_offer("strong", price_cop=20000, distance_km=0.5, recent_rating=5.0, created_at=2000.0)
        assert rank_offers([weak, strong]) == ["strong", "weak"]

    def test_min_max_normalisation(self):
        cheap = make_offer("cheap", price_cop=10000, created_at=1000.0)
        dear = make_offer("dear", price_cop=30000, created_at=2000.0)
        scores = score_offers([cheap, dear])
        assert scores["cheap"] > scores["dear"]

    def test_earliest_offer_breaks_ties(self):
        first = make_offer("first", created_at=1000.0)
        second = make_offer("second", created_at=2000.0)
        assert rank_offers([second, first]) == ["first", "second"]

    def test_empty_bucket_multiplies_by_penalty(self):
        penalised = make_offer("penalised", bucket_empty=True, created_at=1000.0)
        clean = make_offer("clean", bucket_empty=False, created_at=2000.0)
        scores = score_offers([penalised, clean])
        assert scores["penalised"] == scores["clean"] * 0.8

    def test_unrated_walker_scores_zero_on_rating(self):
        unrated = make_offer("unrated", recent_rating=None, created_at=1000.0)
        rated = make_offer("rated", recent_rating=5.0, created_at=2000.0)
        assert score_offers([unrated, rated])["unrated"] < score_offers([unrated, rated])["rated"]

    def test_empty_list_scores_empty(self):
        assert score_offers([]) == {}
        assert rank_offers([]) == []
```

- [ ] **Step 2: Run it and confirm it fails** (ImportError on `bidding`).
- [ ] **Step 3: Implement** `bidding.py` with Big-O docstrings.
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite.
- [ ] **Step 5: Commit.** `git add -A apps/api-py/src/cantrack_api/bidding.py tests/py/api/test_bidding_scoring.py && git commit -m "feat(bidding): config and min-max offer scoring with tie-break (FR-23)"`

---

### Task 3: Eligibility and geo helpers (parallel with 2, 4)

**Files:**
- Create: `apps/api-py/src/cantrack_api/bidding_eligibility.py`
- Test: `tests/py/api/test_bidding_eligibility.py`

**Interface:**

```python
WALKING_SPEED_KMH = 5.0

def project_to_metres(lat: float, lng: float, ref_lat: float) -> tuple[float, float]: ...
def is_free_in_window(busy_windows: IntervalTree[str], window_start: str, window_end: str) -> bool: ...
def is_pack_compatible(pack: UnionFind[str], new_dog_id: str, current_dog_ids: list[str]) -> bool: ...
def detour_minutes(street_graph: WeightedGraph[str], route_stop: str, pickup_stop: str) -> float | None: ...
def is_busy_eligible(*, pack: UnionFind[str], new_dog_id: str, current_dog_ids: list[str],
                     street_graph: WeightedGraph[str], route_stop: str, pickup_stop: str,
                     detour_limit_min: float, dog_count: int, capacity: int) -> bool: ...
def nearby_walker_ids(nearby_walkers: Quadtree[str], pickup_lat: float, pickup_lng: float, radius_km: float) -> list[str]: ...
```

Semantics: equirectangular projection (`east = R·cos(ref)·rad(lng)`, `north = R·rad(lat)`); free means no busy interval overlaps `[window_start, window_end)`; pack-compatible means connected to every current dog (vacuously true when the route is empty); unknown/unreachable detour is `None` (never eligible); busy-eligible needs all three of pack-compat, `detour <= D`, `dog_count + 1 <= capacity`. Big-O per docstrings (projection O(1), free O(log n + k), pack O(d·α(n)), detour O(V² + E), busy-eligible the max of the three, nearby O(n) worst case).

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_bidding_eligibility.py`:

```python
from cantrack_api.bidding_eligibility import (
    is_busy_eligible, is_free_in_window, is_pack_compatible,
    nearby_walker_ids, project_to_metres,
)
from cantrack_api.geo import haversine_km
from cantrack_api.structures import IntervalTree, Quadtree, UnionFind, WeightedGraph


def make_graph():
    street_graph = WeightedGraph()
    street_graph.add_edge("home", "pickup-near", 5.0)
    street_graph.add_edge("home", "pickup-far", 20.0)
    return street_graph


class TestEligibility:
    def test_projection_matches_haversine(self):
        import pytest

        east, north = project_to_metres(6.2, -75.5, 6.2)
        near_east, near_north = project_to_metres(6.201, -75.5, 6.2)
        planar_km = ((near_east - east) ** 2 + (near_north - north) ** 2) ** 0.5 / 1000.0
        assert planar_km == pytest.approx(haversine_km(6.2, -75.5, 6.201, -75.5), abs=0.01)

    def test_free_walker_has_no_overlap(self):
        busy_windows: IntervalTree[str] = IntervalTree()
        busy_windows.insert("2026-10-05T08:00:00+00:00", "2026-10-05T09:00:00+00:00", "morning")
        assert is_free_in_window(busy_windows, "2026-10-05T10:00:00+00:00", "2026-10-05T11:00:00+00:00") is True
        assert is_free_in_window(busy_windows, "2026-10-05T08:30:00+00:00", "2026-10-05T09:30:00+00:00") is False

    def test_touching_windows_do_not_overlap(self):
        busy_windows: IntervalTree[str] = IntervalTree()
        busy_windows.insert("2026-10-05T08:00:00+00:00", "2026-10-05T09:00:00+00:00", "morning")
        assert is_free_in_window(busy_windows, "2026-10-05T09:00:00+00:00", "2026-10-05T10:00:00+00:00") is True

    def test_pack_needs_every_current_dog(self):
        pack: UnionFind[str] = UnionFind()
        for dog_id in ("new", "old-a", "old-b"):
            pack.add(dog_id)
        pack.union("new", "old-a")
        assert is_pack_compatible(pack, "new", ["old-a"]) is True
        assert is_pack_compatible(pack, "new", ["old-a", "old-b"]) is False
        assert is_pack_compatible(pack, "new", []) is True

    def test_busy_walker_inside_detour_is_eligible(self):
        pack: UnionFind[str] = UnionFind()
        for dog_id in ("new", "old-a"):
            pack.add(dog_id)
        pack.union("new", "old-a")
        assert is_busy_eligible(pack=pack, new_dog_id="new", current_dog_ids=["old-a"],
                               street_graph=make_graph(), route_stop="home",
                               pickup_stop="pickup-near", detour_limit_min=10.0,
                               dog_count=1, capacity=4) is True

    def test_busy_walker_outside_detour_is_not(self):
        pack: UnionFind[str] = UnionFind()
        for dog_id in ("new", "old-a"):
            pack.add(dog_id)
        pack.union("new", "old-a")
        assert is_busy_eligible(pack=pack, new_dog_id="new", current_dog_ids=["old-a"],
                               street_graph=make_graph(), route_stop="home",
                               pickup_stop="pickup-far", detour_limit_min=10.0,
                               dog_count=1, capacity=4) is False

    def test_full_capacity_is_not_eligible(self):
        pack: UnionFind[str] = UnionFind()
        for dog_id in ("new", "old-a"):
            pack.add(dog_id)
        pack.union("new", "old-a")
        assert is_busy_eligible(pack=pack, new_dog_id="new", current_dog_ids=["old-a"],
                               street_graph=make_graph(), route_stop="home",
                               pickup_stop="pickup-near", detour_limit_min=10.0,
                               dog_count=4, capacity=4) is False

    def test_nearby_query_finds_only_walkers_inside_radius(self):
        nearby_walkers: Quadtree[str] = Quadtree(-20_000_000.0, -20_000_000.0, 20_000_000.0, 20_000_000.0)
        for walker_id, lat, lng in (("walker-near", 6.201, -75.5), ("walker-far", 6.5, -75.5)):
            east, north = project_to_metres(lat, lng, 6.2)
            nearby_walkers.insert(walker_id, east, north)
        assert nearby_walker_ids(nearby_walkers, 6.2, -75.5, 2.0) == ["walker-near"]
```

- [ ] **Step 2: Run it and confirm it fails** (ImportError).
- [ ] **Step 3: Implement** `bidding_eligibility.py` (imports `IntervalTree`, `Quadtree`, `UnionFind`, `WeightedGraph` from `structures`; no library structures).
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite.
- [ ] **Step 5: Commit.** `git add -A apps/api-py/src/cantrack_api/bidding_eligibility.py tests/py/api/test_bidding_eligibility.py && git commit -m "feat(bidding): busy-walker eligibility and nearby query (FR-25)"`

---

### Task 4: Replay helpers (parallel with 2, 3)

**Files:**
- Create: `apps/api-py/src/cantrack_api/replay.py`
- Test: `tests/py/api/test_replay.py`

**Interface:**

```python
@dataclass(frozen=True)
class TimelineEvent:
    kind: str
    at: str
    payload: dict[str, Any]

@dataclass(frozen=True)
class GpsPoint:
    at_epoch: float
    lat: float
    lng: float

def build_timeline(rows: list[dict[str, Any]]) -> DoublyLinkedList[TimelineEvent]: ...
def read_timeline(timeline: DoublyLinkedList[TimelineEvent], start_index: int, steps: int, direction: str) -> list[TimelineEvent]: ...
def find_gps_at(sorted_points: list[GpsPoint], moment: float) -> GpsPoint: ...
```

Semantics (FR-27): `build_timeline` sorts rows by `at` (stable, O(n log n)) and appends each as a `TimelineEvent` (O(n)); `read_timeline` walks `steps` events from `start_index` forward (`to_array` slice) or backward (`to_array_reverse` slice), O(n); `direction` must be `"forward"`/`"backward"` (ValueError otherwise), out-of-range `start_index` is IndexError. `find_gps_at` is a hand-written binary search (no `bisect`) returning the point with the greatest `at_epoch <= moment`, O(log n); empty input is ValueError, a `moment` before the first point is IndexError.

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_replay.py`:

```python
import pytest

from cantrack_api.replay import GpsPoint, build_timeline, find_gps_at, read_timeline


def make_rows():
    return [
        {"kind": "check_out", "at": "2026-10-05T12:00:00+00:00", "payload": {}},
        {"kind": "check_in", "at": "2026-10-05T10:00:00+00:00", "payload": {}},
        {"kind": "photo", "at": "2026-10-05T11:00:00+00:00", "payload": {"n": 1}},
    ]


class TestReplay:
    def test_build_sorts_by_time(self):
        assert [event.kind for event in build_timeline(make_rows()).to_array()] == ["check_in", "photo", "check_out"]

    def test_read_forward_and_backward(self):
        walk_timeline = build_timeline(make_rows())
        assert [event.kind for event in read_timeline(walk_timeline, 0, 2, "forward")] == ["check_in", "photo"]
        assert [event.kind for event in read_timeline(walk_timeline, 2, 2, "backward")] == ["check_out", "photo"]

    def test_bad_direction_is_rejected(self):
        with pytest.raises(ValueError):
            read_timeline(build_timeline(make_rows()), 0, 1, "sideways")

    def test_start_out_of_range_is_rejected(self):
        with pytest.raises(IndexError):
            read_timeline(build_timeline(make_rows()), 9, 1, "forward")

    def test_gps_exact_hit(self):
        sorted_points = [GpsPoint(1000.0, 6.2, -75.5), GpsPoint(2000.0, 6.3, -75.6)]
        assert find_gps_at(sorted_points, 2000.0) == GpsPoint(2000.0, 6.3, -75.6)

    def test_gps_between_points_returns_the_earlier_one(self):
        sorted_points = [GpsPoint(1000.0, 6.2, -75.5), GpsPoint(2000.0, 6.3, -75.6)]
        assert find_gps_at(sorted_points, 1500.0) == GpsPoint(1000.0, 6.2, -75.5)

    def test_gps_before_first_is_rejected(self):
        with pytest.raises(IndexError):
            find_gps_at([GpsPoint(1000.0, 6.2, -75.5)], 999.0)

    def test_gps_on_empty_is_rejected(self):
        with pytest.raises(ValueError):
            find_gps_at([], 1000.0)
```

- [ ] **Step 2: Run it and confirm it fails** (ImportError).
- [ ] **Step 3: Implement** `replay.py` over `DoublyLinkedList` (no `bisect`).
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite.
- [ ] **Step 5: Commit.** `git add -A apps/api-py/src/cantrack_api/replay.py tests/py/api/test_replay.py && git commit -m "feat(bidding): replay timeline and GPS binary search (FR-27)"`

---

### Task 5: Post a walk request + visible open list (depends on 1, 2, 3)

**Files:**
- Modify: `apps/api-py/src/cantrack_api/schemas.py` (all bidding bodies, so later tasks touch only the router)
- Modify: `apps/api-py/src/cantrack_api/app.py` (mount the router)
- Create: `apps/api-py/src/cantrack_api/routers/bidding.py` (endpoints 1-2; later tasks extend this file)
- Test: `tests/py/api/test_bidding_requests.py`

**Interface (schemas + first endpoints):**

```python
class CreateBiddingRequestBody(BaseModel):
    dogId: str = Field(min_length=1)
    windowStart: str                       # ISO-8601 with offset, parsed by the router
    durationHours: float = Field(gt=0, le=12)
    priceCop: int = Field(ge=0)
    pickupLat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    pickupLng: float = Field(ge=-180, le=180, allow_inf_nan=False)

class SendOfferBody(BaseModel):
    priceCop: int = Field(ge=0)
    etaMin: float = Field(ge=0)

class PickOfferBody(BaseModel):
    offerId: str = Field(min_length=1)

class RatingBody(BaseModel):
    rating: float = Field(ge=1, le=5)
```

- `POST /bidding/requests` (owner only): dog must be the caller's (404 `Dog not found.`), `windowStart` must carry an offset (400) and lie in the future (400). Stores `status='open'`, `walker_id=NULL`, `requested_time=windowStart`, `window_end=start+duration`, `deadline_at=now+X`. Answers 201 `{id, status:'open', deadlineAt, ...}`.
- `GET /bidding/requests/open` (walker only): builds the `Quadtree` from `walker_positions`, keeps requests within R whose deadline has not passed, then per request: free in the window (IntervalTree over the walker's `assigned`/`in_walk` windows) → visible; else visible only when pack-compatible (Union-Find over `dog_pack_links` vs the active route's dogs in `walk_route_stops`), detour (`WeightedGraph` over `walk_graph_edges`, pickup stop key `"pickup:<request_id>"`) `<= D`, and capacity allows. Walkers with no stored position see `[]`.

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_bidding_requests.py`:

```python
"""Task 5: owners post open requests; walkers see exactly the ones they may bid on."""

from .conftest import bearer, make_user

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"


def seed_dog(fake, dog_id="dog-1", owner_id="owner-a"):
    return fake.seed("dogs", id=dog_id, owner_id=owner_id, name="Firulais")


def post_open(fake_client, token="token-a", **over):
    body = {"dogId": "dog-1", "windowStart": T10, "durationHours": 1.0,
            "priceCop": 30000, "pickupLat": 6.2, "pickupLng": -75.5}
    body.update(over)
    return fake_client.post("/bidding/requests", json=body, headers=bearer(token))


def seed_open(fake, request_id="walk-1", deadline_at="2026-10-01T00:10:00+00:00"):
    return fake.seed(
        "walk_requests", id=request_id, owner_id="owner-a", walker_id=None, dog_id="dog-1",
        status="open", requested_time=T10, window_end=T11, deadline_at=deadline_at,
        duration_hours=1.0, pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


class TestPostOpenRequest:
    def test_owner_posts_open_with_deadline(self, fake_client, fake):
        seed_dog(fake)
        res = post_open(fake_client)
        assert res.status_code == 201
        assert res.json()["status"] == "open"
        assert res.json()["deadlineAt"] == "2026-10-01T00:10:00+00:00"

    def test_walker_cannot_post(self, fake_client, fake):
        seed_dog(fake)
        assert post_open(fake_client, token="token-w").status_code == 400

    def test_unknown_dog_is_404(self, fake_client, fake):
        assert post_open(fake_client).status_code == 404

    def test_past_window_is_400(self, fake_client, fake):
        seed_dog(fake)
        assert post_open(fake_client, windowStart="2026-09-01T10:00:00+00:00").status_code == 400


class TestVisibleOpenList:
    def test_free_walker_inside_radius_sees_it(self, fake_client, fake):
        seed_dog(fake)
        seed_open(fake)
        fake.seed("walker_positions", walker_id="walker-w", lat=6.201, lng=-75.5,
                 updated_at="2026-10-01T00:00:00+00:00")
        res = fake_client.get("/bidding/requests/open", headers=bearer("token-w"))
        assert [row["id"] for row in res.json()] == ["walk-1"]

    def test_walker_outside_radius_sees_nothing(self, fake_client, fake):
        seed_dog(fake)
        seed_open(fake)
        fake.seed("walker_positions", walker_id="walker-w", lat=6.5, lng=-75.5,
                 updated_at="2026-10-01T00:00:00+00:00")
        res = fake_client.get("/bidding/requests/open", headers=bearer("token-w"))
        assert res.json() == []

    def test_busy_walker_inside_detour_sees_it_outside_never(self, fake_client, fake):
        seed_dog(fake)
        seed_open(fake, request_id="walk-near")
        seed_open(fake, request_id="walk-far")
        fake.add_user("token-w2", make_user("walker-w2", "walker-w2@example.com", "walker"))
        fake.seed("walker_positions", walker_id="walker-w2", lat=6.201, lng=-75.5,
                 updated_at="2026-10-01T00:00:00+00:00")
        busy = fake.seed(
            "walk_requests", owner_id="owner-a", walker_id="walker-w2", dog_id="dog-1",
            status="assigned", requested_time="2026-10-05T10:00:00+00:00",
            window_end="2026-10-05T11:00:00+00:00", duration_hours=1.0,
            pickup_lat=6.201, pickup_lng=-75.5, price_cop=30000,
            created_at="2026-10-01T00:00:00+00:00",
        )
        fake.seed("walk_route_stops", walk_request_id=busy["id"], dog_id="dog-1",
                 stop_key="home", pickup_time=T10, position=0)
        fake.seed("dog_pack_links", dog_a="dog-1", dog_b="dog-2")
        fake.seed("walk_graph_edges", from_stop="home", to_stop="pickup:walk-near", minutes=5.0)
        fake.seed("walk_graph_edges", from_stop="home", to_stop="pickup:walk-far", minutes=20.0)
        res = fake_client.get("/bidding/requests/open", headers=bearer("token-w2"))
        assert [row["id"] for row in res.json()] == ["walk-near"]
```

- [ ] **Step 2: Run it and confirm it fails** (404 on `/bidding/requests`, router missing).
- [ ] **Step 3: Implement** the schemas, the two endpoints and the `app.py` mount. Every transition goes through `WalkStateMachine`; new rows start `OPEN`.
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite (S1 untouched: 638 + new pass).
- [ ] **Step 5: Commit.** `git add -A apps/api-py/src/cantrack_api/schemas.py apps/api-py/src/cantrack_api/app.py apps/api-py/src/cantrack_api/routers/bidding.py tests/py/api/test_bidding_requests.py && git commit -m "feat(bidding): post open requests and visible-nearby list (FR-23, FR-25)"`

---

### Task 6: Offers — send and ranked list (depends on 5)

**Files:**
- Modify: `apps/api-py/src/cantrack_api/routers/bidding.py` (endpoints 3-4)
- Test: `tests/py/api/test_bidding_offers.py`

**Interface:**

- `POST /bidding/requests/{id}/offers` (walker only, body `SendOfferBody`): request must be `open` and visible to this walker (else 403 `Walk not visible to you.`); one offer per walker per request (second → 409 `Walker already offered on this walk.`). Distance is the haversine from the walker's stored position to the pickup. Answers 201 `{id, score}` where `score` comes from `score_offers` over all offers on the request (recent rating from the last-25 `walk_ratings` rows rebuilt through `RatingWindow`; empty-bucket penalty from `walker_cancel_buckets`).
- `GET /bidding/requests/{id}/offers` (owning owner only, else 403 `Not your walk request.`): offers ranked best-first via `rank_offers`, each `{offerId, walkerId, priceCop, etaMin, score, recentRating, allTimeRating}`; `allTimeRating` is the running sum/count over every `walk_ratings` row.

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_bidding_offers.py`:

```python
"""Task 6: walkers bid on visible requests; owners read ranked offers."""

from .conftest import bearer, make_user

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"


def seed_open(fake, request_id="walk-1"):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    return fake.seed(
        "walk_requests", id=request_id, owner_id="owner-a", walker_id=None, dog_id="dog-1",
        status="open", requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


def place(fake, walker_id, lat=6.201, lng=-75.5):
    fake.seed("walker_positions", walker_id=walker_id, lat=lat, lng=lng,
             updated_at="2026-10-01T00:00:00+00:00")


class TestOffers:
    def test_walker_offers_and_owner_reads_ranked(self, fake_client, fake):
        seed_open(fake)
        fake.add_user("token-w2", make_user("walker-w2", "walker-w2@example.com", "walker"))
        place(fake, "walker-w")
        place(fake, "walker-w2", lat=6.2005)
        fake.seed("walk_ratings", walk_request_id="old-walk", walker_id="walker-w2",
                 rating=5.0, created_at="2026-10-01T00:00:00+00:00")
        assert fake_client.post("/bidding/requests/walk-1/offers",
                                json={"priceCop": 30000, "etaMin": 15.0},
                                headers=bearer("token-w")).status_code == 201
        assert fake_client.post("/bidding/requests/walk-1/offers",
                                json={"priceCop": 20000, "etaMin": 8.0},
                                headers=bearer("token-w2")).status_code == 201
        res = fake_client.get("/bidding/requests/walk-1/offers", headers=bearer("token-a"))
        assert res.status_code == 200
        assert [offer["walkerId"] for offer in res.json()] == ["walker-w2", "walker-w"]
        assert res.json()[0]["allTimeRating"] == 5.0

    def test_second_offer_from_same_walker_is_409(self, fake_client, fake):
        seed_open(fake)
        place(fake, "walker-w")
        fake_client.post("/bidding/requests/walk-1/offers", json={"priceCop": 30000, "etaMin": 15.0},
                         headers=bearer("token-w"))
        res = fake_client.post("/bidding/requests/walk-1/offers", json={"priceCop": 20000, "etaMin": 8.0},
                               headers=bearer("token-w"))
        assert res.status_code == 409
        assert res.json() == {"detail": "Walker already offered on this walk."}

    def test_offer_on_hidden_request_is_403(self, fake_client, fake):
        seed_open(fake)
        place(fake, "walker-w", lat=6.5)
        res = fake_client.post("/bidding/requests/walk-1/offers", json={"priceCop": 30000, "etaMin": 15.0},
                               headers=bearer("token-w"))
        assert res.status_code == 403

    def test_wrong_owner_reads_403(self, fake_client, fake):
        seed_open(fake)
        res = fake_client.get("/bidding/requests/walk-1/offers", headers=bearer("token-b"))
        assert res.status_code == 403
        assert res.json() == {"detail": "Not your walk request."}
```

- [ ] **Step 2: Run it and confirm it fails** (404 on the offers path).
- [ ] **Step 3: Implement** the two endpoints (heap ordering comes free from `rank_offers`; keep a `MaxHeap` of offer ids per request only if it simplifies the pick — otherwise rank on read).
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite.
- [ ] **Step 5: Commit.** `git commit -m "feat(bidding): walker offers and owner-ranked offer list (FR-23, FR-26)"`

---

### Task 7: Pick and auto-pick, exactly one winner (depends on 6)

**Files:**
- Modify: `apps/api-py/src/cantrack_api/routers/bidding.py` (endpoints 5-6)
- Test: `tests/py/api/test_bidding_pick.py`

**Interface:**

- `POST /bidding/requests/{id}/pick` (owning owner only, body `PickOfferBody`): validates `OWNER_PICK` through `WalkStateMachine`, then the compare-and-set `UPDATE walk_requests SET status='assigned', ... WHERE id=? AND status='open'`; zero rows updated → 409 `Walk no longer open.` Unknown offer → 404. Answers 200 `{id, status:'assigned', walkerId}`.
- `POST /bidding/requests/{id}/auto-pick` (owning owner only, uses `now`): before `deadline_at` → 400 `Deadline has not passed yet.`; at/after: validates `AUTO_PICK`/`DEADLINE_EMPTY` through the machine and applies the same compare-and-set — top-ranked offer wins, empty heap → `expired`. Not open → 409 `Walk no longer open.` An owner pick always wins over a later auto-pick because auto-pick only fires at X.

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_bidding_pick.py` (includes the spec's race test):

```python
"""Task 7: exactly one offer wins; owner pick beats a later auto-pick."""

from .conftest import FIXED_NOW, bearer, make_user

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"


def seed_open(fake, request_id="walk-1", deadline_at="2026-10-01T00:10:00+00:00"):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    return fake.seed(
        "walk_requests", id=request_id, owner_id="owner-a", walker_id=None, dog_id="dog-1",
        status="open", requested_time=T10, window_end=T11, deadline_at=deadline_at,
        duration_hours=1.0, pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


def place(fake, walker_id, lat=6.201):
    fake.seed("walker_positions", walker_id=walker_id, lat=lat, lng=-75.5,
             updated_at="2026-10-01T00:00:00+00:00")


def offer_id_for(fake_client, request_id, token, price):
    res = fake_client.post(f"/bidding/requests/{request_id}/offers",
                           json={"priceCop": price, "etaMin": 10.0}, headers=bearer(token))
    assert res.status_code == 201
    listed = fake_client.get(f"/bidding/requests/{request_id}/offers", headers=bearer("token-a")).json()
    return [offer["offerId"] for offer in listed if offer["walkerId"] == token_walker(token)][0]


def token_walker(token):
    return {"token-w": "walker-w", "token-w2": "walker-w2"}[token]


class TestPick:
    def test_two_walkers_race_exactly_one_wins(self, fake_client, fake):
        seed_open(fake)
        fake.add_user("token-w2", make_user("walker-w2", "walker-w2@example.com", "walker"))
        place(fake, "walker-w")
        place(fake, "walker-w2", lat=6.2005)
        first = offer_id_for(fake_client, "walk-1", "token-w", 30000)
        second = offer_id_for(fake_client, "walk-1", "token-w2", 20000)
        won = fake_client.post("/bidding/requests/walk-1/pick", json={"offerId": first},
                               headers=bearer("token-a"))
        lost = fake_client.post("/bidding/requests/walk-1/pick", json={"offerId": second},
                                headers=bearer("token-a"))
        assert won.status_code == 200
        assert won.json()["status"] == "assigned"
        assert lost.status_code == 409
        assert lost.json() == {"detail": "Walk no longer open."}

    def test_auto_pick_after_deadline_takes_the_top_offer(self, fake_client, fake, fake_clock):
        seed_open(fake)
        fake.add_user("token-w2", make_user("walker-w2", "walker-w2@example.com", "walker"))
        place(fake, "walker-w")
        place(fake, "walker-w2", lat=6.2005)
        offer_id_for(fake_client, "walk-1", "token-w", 30000)
        offer_id_for(fake_client, "walk-1", "token-w2", 20000)
        fake_clock.now = FIXED_NOW.replace(hour=1)
        res = fake_client.post("/bidding/requests/walk-1/auto-pick", headers=bearer("token-a"))
        assert res.status_code == 200
        assert res.json()["walkerId"] == "walker-w2"

    def test_auto_pick_with_no_offers_expires(self, fake_client, fake, fake_clock):
        seed_open(fake)
        fake_clock.now = FIXED_NOW.replace(hour=1)
        res = fake_client.post("/bidding/requests/walk-1/auto-pick", headers=bearer("token-a"))
        assert res.json()["status"] == "expired"

    def test_auto_pick_before_deadline_is_400(self, fake_client, fake):
        seed_open(fake)
        res = fake_client.post("/bidding/requests/walk-1/auto-pick", headers=bearer("token-a"))
        assert res.status_code == 400

    def test_wrong_owner_pick_is_403(self, fake_client, fake):
        seed_open(fake)
        place(fake, "walker-w")
        found = offer_id_for(fake_client, "walk-1", "token-w", 30000)
        res = fake_client.post("/bidding/requests/walk-1/pick", json={"offerId": found},
                               headers=bearer("token-b"))
        assert res.status_code == 403
```

- [ ] **Step 2: Run it and confirm it fails** (404 on `/pick`).
- [ ] **Step 3: Implement** both endpoints with the compare-and-set update and `WalkStateMachine` gating.
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite.
- [ ] **Step 5: Commit.** `git commit -m "feat(bidding): compare-and-set pick and deadline auto-pick (FR-24)"`

---

### Task 8: Cancels, no-show, check-in, check-out (depends on 7)

**Files:**
- Modify: `apps/api-py/src/cantrack_api/bidding.py` (add the token-spend helper below)
- Modify: `apps/api-py/src/cantrack_api/routers/bidding.py` (endpoints 7-11)
- Test: `tests/py/api/test_bidding_lifecycle.py`

**Interface (helper + endpoints):**

```python
WEEK_SECONDS = 7 * 24 * 3600.0

def spend_cancel_token(cancel_bucket: TokenBucket, remaining: int, period_start: float, now: float) -> tuple[bool, int]:
    """Replay `capacity - remaining` takes at `period_start`, then take at `now`. O(1).
    Returns (spent, new_remaining). Uses the real TokenBucket so weekly refill applies."""
```

- `POST /bidding/requests/{id}/walker-cancel` (assigned walker only): spends one cancel token (row in `walker_cancel_buckets`; missing row starts full at 3). Empty bucket → 403 `No cancellation tokens left.` Applies `WALKER_CANCEL` → `open`; remaining offers stay in the table so the reopened request keeps its heap.
- `POST /bidding/requests/{id}/owner-cancel` (owning owner only): `open` → `cancelled` (withdraw, `OWNER_WITHDRAW`), `assigned` → `cancelled` (`OWNER_CANCEL`); anything else → 409.
- `POST /bidding/requests/{id}/no-show` (owning owner only, uses `now`): only from `assigned` and only at/after `window_start + 15 min` (else 400 `Too early to mark a no-show.`); counts as a walker cancellation (one token via the same helper; an empty bucket still reopens the walk but the penalty applies at ranking time).
- `POST /bidding/requests/{id}/check-in` (assigned walker): `CHECK_IN` → `in_walk`, appends a `walk_timeline` row (`check_in`); else 409.
- `POST /bidding/requests/{id}/check-out` (walker, from `in_walk`): `CHECK_OUT` → `done`, appends `check_out`; from any other state → 409 `That transition is not allowed from the current state.` (this is the `IllegalTransition` mapping every bidding endpoint uses).

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_bidding_lifecycle.py`:

```python
"""Task 8: cancels cost tokens and reopen; check-in/out move the walk."""

from datetime import timedelta

from .conftest import FIXED_NOW, bearer

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"


def seed_assigned(fake, request_id="walk-1", walker_id="walker-w"):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    return fake.seed(
        "walk_requests", id=request_id, owner_id="owner-a", walker_id=walker_id, dog_id="dog-1",
        status="assigned", requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


class TestLifecycle:
    def test_walker_cancel_reopens_and_spends_token(self, fake_client, fake):
        seed_assigned(fake)
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=3,
                 period_start="2026-10-01T00:00:00+00:00")
        fake.seed("walk_offers", walk_request_id="walk-1", walker_id="walker-w",
                 price_cop=30000, eta_min=10.0, distance_km=1.0,
                 created_at="2026-10-01T00:00:00+00:00")
        res = fake_client.post("/bidding/requests/walk-1/walker-cancel", headers=bearer("token-w"))
        assert res.json()["status"] == "open"
        buckets = fake.table("walker_cancel_buckets").select("*").eq("walker_id", "walker-w").execute().data
        assert buckets[0]["remaining"] == 2
        offers = fake.table("walk_offers").select("*").eq("walk_request_id", "walk-1").execute().data
        assert len(offers) == 1

    def test_walker_cancel_with_empty_bucket_is_403(self, fake_client, fake):
        seed_assigned(fake)
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=0,
                 period_start="2026-10-01T00:00:00+00:00")
        res = fake_client.post("/bidding/requests/walk-1/walker-cancel", headers=bearer("token-w"))
        assert res.status_code == 403
        assert res.json() == {"detail": "No cancellation tokens left."}

    def test_owner_cancel_assigned_is_cancelled(self, fake_client, fake):
        seed_assigned(fake)
        res = fake_client.post("/bidding/requests/walk-1/owner-cancel", headers=bearer("token-a"))
        assert res.json()["status"] == "cancelled"

    def test_no_show_after_grace_reopens(self, fake_client, fake, fake_clock):
        seed_assigned(fake)
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=3,
                 period_start="2026-10-01T00:00:00+00:00")
        fake_clock.now = FIXED_NOW + timedelta(days=4, hours=10, minutes=20)
        res = fake_client.post("/bidding/requests/walk-1/no-show", headers=bearer("token-a"))
        assert res.json()["status"] == "open"

    def test_check_in_then_check_out_finishes(self, fake_client, fake):
        seed_assigned(fake)
        assert fake_client.post("/bidding/requests/walk-1/check-in", headers=bearer("token-w")).json()["status"] == "in_walk"
        done = fake_client.post("/bidding/requests/walk-1/check-out", headers=bearer("token-w"))
        assert done.json()["status"] == "done"
        kinds = sorted(row["kind"] for row in
                       fake.table("walk_timeline").select("*").eq("walk_request_id", "walk-1").execute().data)
        assert kinds == ["check_in", "check_out"]

    def test_check_out_from_assigned_is_409(self, fake_client, fake):
        seed_assigned(fake)
        res = fake_client.post("/bidding/requests/walk-1/check-out", headers=bearer("token-w"))
        assert res.status_code == 409
```

- [ ] **Step 2: Run it and confirm it fails** (404 on `/walker-cancel`).
- [ ] **Step 3: Implement** the helper and the five endpoints, all gated by `WalkStateMachine`.
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite.
- [ ] **Step 5: Commit.** `git commit -m "feat(bidding): cancels with token budget, no-show, check-in/out (FR-24, FR-26)"`

---

### Task 9: Busy winner joins the active route (depends on 7)

**Files:**
- Modify: `apps/api-py/src/cantrack_api/routers/bidding.py` (extend the pick path only)
- Test: `tests/py/api/test_bidding_route_insert.py`

**Interface:** no new endpoints. When the picked offer's walker was busy (has an `assigned`/`in_walk` walk with `walk_route_stops`), the pick appends the new dog to that route: stops are held in a `SinglyLinkedList` (`append` + `to_array`, O(1)/O(n)), persisted as a new `walk_route_stops` row (`position = max + 1`), and every stop's `pickup_time` is re-indexed into an `AvlTree` whose `min()` is returned as `nextPickup`. A free winner returns `routeStops: []` and no `nextPickup`.

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_bidding_route_insert.py`:

```python
"""Task 9: a busy winner's new dog joins the active route (FR-25, FR-06, FR-08)."""

from .conftest import bearer, make_user

T10 = "2026-10-05T10:00:00+00:00"
T1030 = "2026-10-05T10:30:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"


class TestRouteInsert:
    def test_busy_winner_gets_a_new_stop_and_next_pickup(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        fake.seed("dogs", id="dog-2", owner_id="owner-a", name="Rex")
        busy = fake.seed(
            "walk_requests", owner_id="owner-a", walker_id="walker-w", dog_id="dog-1",
            status="assigned", requested_time=T10, window_end=T11,
            deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
            pickup_lat=6.201, pickup_lng=-75.5, price_cop=30000,
            created_at="2026-10-01T00:00:00+00:00",
        )
        fake.seed("walk_route_stops", walk_request_id=busy["id"], dog_id="dog-1",
                 stop_key="home", pickup_time=T10, position=0)
        new_walk = fake.seed(
            "walk_requests", id="walk-new", owner_id="owner-a", walker_id=None, dog_id="dog-2",
            status="open", requested_time=T1030, window_end=T11,
            deadline_at="2026-10-01T00:10:00+00:00", duration_hours=0.5,
            pickup_lat=6.202, pickup_lng=-75.5, price_cop=30000,
            created_at="2026-10-01T00:00:00+00:00",
        )
        fake.seed("walker_positions", walker_id="walker-w", lat=6.201, lng=-75.5,
                 updated_at="2026-10-01T00:00:00+00:00")
        fake.seed("dog_pack_links", dog_a="dog-1", dog_b="dog-2")
        fake.seed("walk_graph_edges", from_stop="home", to_stop="pickup:walk-new", minutes=5.0)
        offered = fake_client.post("/bidding/requests/walk-new/offers",
                                   json={"priceCop": 30000, "etaMin": 10.0},
                                   headers=bearer("token-w"))
        assert offered.status_code == 201
        offer_id = fake_client.get("/bidding/requests/walk-new/offers",
                                   headers=bearer("token-a")).json()[0]["offerId"]
        res = fake_client.post("/bidding/requests/walk-new/pick", json={"offerId": offer_id},
                               headers=bearer("token-a"))
        assert res.status_code == 200
        assert res.json()["routeStops"][-1]["dogId"] == "dog-2"
        assert res.json()["nextPickup"]["pickupTime"] == T10
        stored = fake.table("walk_route_stops").select("*").eq("walk_request_id", busy["id"]).execute().data
        assert sorted(row["dog_id"] for row in stored) == ["dog-1", "dog-2"]

    def test_free_winner_gets_no_route_change(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        fake.seed("walk_requests", id="walk-new", owner_id="owner-a", walker_id=None, dog_id="dog-1",
                 status="open", requested_time=T1030, window_end=T11,
                 deadline_at="2026-10-01T00:10:00+00:00", duration_hours=0.5,
                 pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
                 created_at="2026-10-01T00:00:00+00:00")
        fake.seed("walker_positions", walker_id="walker-w", lat=6.201, lng=-75.5,
                 updated_at="2026-10-01T00:00:00+00:00")
        fake_client.post("/bidding/requests/walk-new/offers", json={"priceCop": 30000, "etaMin": 10.0},
                         headers=bearer("token-w"))
        offer_id = fake_client.get("/bidding/requests/walk-new/offers",
                                   headers=bearer("token-a")).json()[0]["offerId"]
        res = fake_client.post("/bidding/requests/walk-new/pick", json={"offerId": offer_id},
                               headers=bearer("token-a"))
        assert res.json()["routeStops"] == []
        assert "nextPickup" not in res.json()
```

- [ ] **Step 2: Run it and confirm it fails** (`routeStops` missing from the pick answer).
- [ ] **Step 3: Implement** the insert inside the pick path (`SinglyLinkedList` + `AvlTree.min()`).
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite.
- [ ] **Step 5: Commit.** `git commit -m "feat(bidding): busy winner joins active route with AVL next pickup (FR-25)"`

---

### Task 10: Ratings feed reputation (depends on 6)

**Files:**
- Modify: `apps/api-py/src/cantrack_api/routers/bidding.py` (endpoint 12)
- Test: `tests/py/api/test_bidding_ratings.py`

**Interface:**

- `POST /bidding/requests/{id}/ratings` (owning owner only, body `RatingBody`): only from `done` (else 409 `Only a finished walk can be rated.`); one rating per walk (second → 409 `Walk already rated.`). Range is enforced by pydantic (400). Each rating inserts one `walk_ratings` row; the offers list then shows the walker with `recentRating` = `RatingWindow(25)` rebuilt from the newest 25 rows and `allTimeRating` = sum/count over all rows.

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_bidding_ratings.py`:

```python
"""Task 10: ratings after DONE feed the window average and the all-time mean."""

from .conftest import bearer

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"


def seed_done(fake, request_id="walk-1", walker_id="walker-w"):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    return fake.seed(
        "walk_requests", id=request_id, owner_id="owner-a", walker_id=walker_id, dog_id="dog-1",
        status="done", requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


class TestRatings:
    def test_rating_done_walk_updates_reputation(self, fake_client, fake):
        seed_done(fake)
        res = fake_client.post("/bidding/requests/walk-1/ratings", json={"rating": 5.0},
                               headers=bearer("token-a"))
        assert res.status_code == 201
        assert res.json()["allTimeRating"] == 5.0
        rows = fake.table("walk_ratings").select("*").eq("walker_id", "walker-w").execute().data
        assert [row["rating"] for row in rows] == [5.0]

    def test_second_rating_same_walk_is_409(self, fake_client, fake):
        seed_done(fake)
        fake_client.post("/bidding/requests/walk-1/ratings", json={"rating": 5.0}, headers=bearer("token-a"))
        res = fake_client.post("/bidding/requests/walk-1/ratings", json={"rating": 4.0}, headers=bearer("token-a"))
        assert res.status_code == 409

    def test_rating_unfinished_walk_is_409(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        fake.seed("walk_requests", id="walk-1", owner_id="owner-a", walker_id="walker-w", dog_id="dog-1",
                 status="in_walk", requested_time=T10, window_end=T11,
                 deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
                 pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
                 created_at="2026-10-01T00:00:00+00:00")
        res = fake_client.post("/bidding/requests/walk-1/ratings", json={"rating": 5.0},
                               headers=bearer("token-a"))
        assert res.status_code == 409

    def test_out_of_range_rating_is_400(self, fake_client, fake):
        seed_done(fake)
        assert fake_client.post("/bidding/requests/walk-1/ratings", json={"rating": 9.0},
                                headers=bearer("token-a")).status_code == 400

    def test_window_holds_only_last_25(self, fake_client, fake):
        seed_done(fake)
        for index in range(25):
            fake.seed("walk_ratings", walk_request_id=f"old-{index}", walker_id="walker-w",
                     rating=1.0, created_at=f"2026-09-{index + 1:02d}T00:00:00+00:00")
        fake_client.post("/bidding/requests/walk-1/ratings", json={"rating": 5.0}, headers=bearer("token-a"))
        rows = fake.table("walk_ratings").select("*").eq("walker_id", "walker-w").execute().data
        assert len(rows) == 26
        recent = sorted(rows, key=lambda row: row["created_at"], reverse=True)[:25]
        assert sum(row["rating"] for row in recent) / 25 == (24 * 1.0 + 5.0) / 25
```

- [ ] **Step 2: Run it and confirm it fails** (404 on `/ratings`).
- [ ] **Step 3: Implement** the endpoint (rebuild `RatingWindow` from the newest 25 rows; all-time from all rows).
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite.
- [ ] **Step 5: Commit.** `git commit -m "feat(bidding): post-walk ratings feed window and all-time averages (FR-26)"`

---

### Task 11: Replay endpoint (depends on 4, 8)

**Files:**
- Modify: `apps/api-py/src/cantrack_api/routers/bidding.py` (endpoint 13)
- Test: `tests/py/api/test_bidding_replay.py`

**Interface:**

- `GET /bidding/requests/{id}/replay` (owning owner only, else 403): only for `done` walks (else 409 `Only a finished walk can be replayed.`). Query params `direction=forward|backward` (default `forward`), `start=0`, `steps=50`, and optional `atEpoch` (GPS jump). Answers `{events: [{kind, at}], gps: {atEpoch, lat, lng} | null}` where events come from `build_timeline` + `read_timeline` over the walk's `walk_timeline` rows and `gps` from `find_gps_at` over its `walk_gps_points` rows (no jump requested → last point; empty GPS → `null`).

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_bidding_replay.py`:

```python
"""Task 11: owners scrub a finished walk forward/back with a GPS jump."""

from .conftest import bearer

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"
T12 = "2026-10-05T12:00:00+00:00"


def seed_done_with_trace(fake, request_id="walk-1"):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    fake.seed("walk_requests", id=request_id, owner_id="owner-a", walker_id="walker-w", dog_id="dog-1",
             status="done", requested_time=T10, window_end=T11,
             deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
             pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
             created_at="2026-10-01T00:00:00+00:00")
    for kind, at in (("check_in", T10), ("photo", T11), ("check_out", T12)):
        fake.seed("walk_timeline", walk_request_id=request_id, kind=kind, at=at, payload={})
    for epoch, lat in ((1000.0, 6.2), (2000.0, 6.3), (3000.0, 6.4)):
        fake.seed("walk_gps_points", walk_request_id=request_id, at_epoch=epoch, lat=lat, lng=-75.5)


class TestReplayEndpoint:
    def test_forward_and_backward(self, fake_client, fake):
        seed_done_with_trace(fake)
        forward = fake_client.get("/bidding/requests/walk-1/replay?direction=forward&start=0&steps=2",
                                  headers=bearer("token-a")).json()
        assert [event["kind"] for event in forward["events"]] == ["check_in", "photo"]
        backward = fake_client.get("/bidding/requests/walk-1/replay?direction=backward&start=2&steps=2",
                                   headers=bearer("token-a")).json()
        assert [event["kind"] for event in backward["events"]] == ["check_out", "photo"]

    def test_gps_jump_returns_earlier_point(self, fake_client, fake):
        seed_done_with_trace(fake)
        res = fake_client.get("/bidding/requests/walk-1/replay?atEpoch=2500.0", headers=bearer("token-a"))
        assert res.json()["gps"] == {"atEpoch": 2000.0, "lat": 6.3, "lng": -75.5}

    def test_unfinished_walk_is_409(self, fake_client, fake):
        seed_done_with_trace(fake, request_id="walk-2")
        fake.table("walk_requests").update({"status": "in_walk"}).eq("id", "walk-2").execute()
        res = fake_client.get("/bidding/requests/walk-2/replay", headers=bearer("token-a"))
        assert res.status_code == 409

    def test_wrong_owner_is_403(self, fake_client, fake):
        seed_done_with_trace(fake)
        res = fake_client.get("/bidding/requests/walk-1/replay", headers=bearer("token-b"))
        assert res.status_code == 403
```

- [ ] **Step 2: Run it and confirm it fails** (404 on `/replay`).
- [ ] **Step 3: Implement** the endpoint over `replay.build_timeline`, `read_timeline`, `find_gps_at`.
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite.
- [ ] **Step 5: Commit.** `git commit -m "feat(bidding): finished-walk replay with GPS jump (FR-27)"`

---

### Task 12: Postman collection, environment, Newman docs (depends on all endpoints)

**Files:**
- Create: `cantrack-backend/postman/cantrack-bidding.postman_collection.json` (Postman v2.1 schema)
- Create: `cantrack-backend/postman/cantrack-bidding.postman_environment.json`
- Create/extend: `cantrack-backend/postman/README.md` (Newman section)

**Interface:** file formats, not code. The collection uses `{{baseUrl}}`, `{{ownerToken}}`, `{{walkerToken}}`, `{{walker2Token}}` variables and stores the created `walkId`/`offerId` with `pm.environment.set` in the happy-path requests. It covers all 13 endpoints in order: post request → open list → two offers → ranked offers → pick → auto-pick (second request) → walker-cancel → owner-cancel → no-show → check-in → check-out → rating → replay. Negative cases with `pm.test` assertions: second pick gets 409 `Walk no longer open.`, check-out from `assigned` gets 409, wrong-owner pick gets 403, second offer gets 409, rating above 5 gets 400. The environment file holds placeholder tokens (`"REPLACE_WITH_OWNER_JWT"`, etc.), never real secrets. README gains:

```markdown
## Bidding collection (Newman)

npx newman run cantrack-bidding.postman_collection.json -e cantrack-bidding.postman_environment.json
```

plus the two setup steps (start the API, replace the placeholder tokens).

- [ ] **Step 1: Write** both JSON files and the README section.
- [ ] **Step 2: Validate.** `python -m json.tool` passes on both files; every `item.request.url` contains `{{baseUrl}}`; `grep -c "pm.test" cantrack-bidding.postman_collection.json` ≥ 13; no real JWT appears (`grep -ri "eyJ"` → empty).
- [ ] **Step 3: Run** the Newman command against a local API (needs real tokens in a throwaway env file, never committed) and confirm the happy path passes.
- [ ] **Step 4: Run the full pytest suite** (still 638 + new, 0 failed).
- [ ] **Step 5: Commit.** `git add -A cantrack-backend/postman && git commit -m "test(bidding): Postman collection, environment and Newman docs (FR-23..FR-27)"`

---

## Done when

- 12 tasks in 12 commits, in the order above (2-4 may be built in parallel, the rest sequentially).
- `uv run --offline pytest -q` (from `apps/api-py/`) → 638 + new tests pass, 0 failed; S1 tests untouched and green.
- `grep -rnE "import (heapq|bisect|collections|queue)|from (heapq|bisect|collections|queue) " apps/api-py/src/cantrack_api/bidding*.py apps/api-py/src/cantrack_api/replay.py apps/api-py/src/cantrack_api/routers/bidding.py` → no output.
- Every walk transition in the router goes through `WalkStateMachine.next_state`; `IllegalTransition` always answers 409.
- The S1 addressed-request flow still works (its statuses and `walker_id` scoping are unchanged).

## Next plans (not in this file)

- **Plan 3, hardening:** RLS policy audit (supabase skills), slowapi rate limits, upload limits, security headers, pip-audit in CI, Schemathesis fuzzing, and one negative test per endpoint.

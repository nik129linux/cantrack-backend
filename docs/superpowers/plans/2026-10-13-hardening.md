# Backend Hardening (Plan 3 of 3) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the CanTrack FastAPI backend that already ships walk-bidding (Plans 1-2): add browser security headers, rate-limit the floodable endpoints, add defence-in-depth RLS policies, wire supply-chain auditing in CI, add schema-driven fuzz tests, and add the missing deny-path tests that prove the real authorization boundary holds. Nothing in this plan changes business logic.

**Threat model (security-and-hardening framing):** The API authenticates via `get_current_user` and then authorizes by scoping every Supabase query to the caller (`owner_id = <caller>`, `walker_id = <caller>`, `walker_id = NULL` for bidding). It uses the Supabase `SERVICE_ROLE` key, which **bypasses RLS entirely** (see `deps.py`). Therefore RLS policies are defence-in-depth only — they bite if a client ever uses the `anon` key directly, not in the normal service-role path. The #1 real risk is a broken caller-scoped query (a missing `eq("owner_id", user.id)` or `eq("walker_id", user.id)`) that lets one user read or mutate another's rows — an IDOR. Rate-limit and header tasks are lower severity (credential-stuffing/flood and clickjacking/MIME sniffing) and are included for completeness and CI hygiene. Task 6's wrong-owner / bad-token tests are the highest-value part of this plan and must be treated as blocking.

**Architecture:** Four kinds of change, all offline-testable: (1) a lightweight `BaseHTTPMiddleware` in `app.py` that sets headers after the inner handler returns; (2) `slowapi`'s in-memory limiter wired in `app.py` and decorating the floodable routes in `routers/auth.py` and `routers/bidding.py`; (3) a SQL-only append to `schema.sql` that adds `create policy` statements for all 16 RLS-enabled tables, commented as defence-in-depth; (4) CI wiring (`.github/workflows/ci.yml`) and test-only additions (`pip-audit`, `schemathesis`, negative tests). Every new test runs against `fake_supabase.py` / `TestClient` / fake clock — no network.

**Tech Stack:** Python 3.12, FastAPI, Starlette, Supabase (faked in tests), pytest, slowapi (new), schemathesis (new, dev only), pip-audit (new, dev only), PyYAML (for workflow validation, already transitive).

Spec: `docs/superpowers/specs/2026-10-13-cantrack-bidding-design.md`. Requirements: `docs/REQUIREMENTS.md` (NFR-03, NFR-09, NFR-10, NFR-12, NFR-13 plus the security NFRs implied by this plan). Plan 1: `docs/superpowers/plans/2026-10-13-bidding-structures.md`. Plan 2: `docs/superpowers/plans/2026-10-13-bidding-api.md`. Current app: `apps/api-py/src/cantrack_api/app.py`, `deps.py`, `uploads.py`, `routers/*.py`. Fakes: `tests/py/api/conftest.py`, `fake_supabase.py`. Deps: `apps/api-py/pyproject.toml`. Schema: `cantrack-db/apps/api-py/schema.sql`.

## Rules for every task (read before starting)

- **No library structures.** Don't use `heapq`, `bisect`, `collections` or `queue` anywhere new (NFR-03). These tasks add middleware/config/tests, so the rule rarely applies — the plan says so per task — but if you touch `structures/` or `bidding*.py`/`replay.py`, keep it hand-written.
- **Big-O docstrings.** Every new public function/method states its Big-O, in the style of `structures/stack.py` (NFR-04). Middleware helpers and SQL-only tasks have no new public data-structure methods, so a one-line docstring is enough.
- **Domain names (NFR-13).** No `x`, `tmp`, `data`, `item` or `obj` outside structure internals. Instances are named `walk_request`, `offer_heap`, `nearby_walkers`, `rating_window`, `cancel_bucket`, `walk_timeline`.
- **English everywhere.** Identifiers, comments, docstrings, log and error messages in English (NFR-02).
- **Time is passed in.** Nothing new reads the clock; routers use the `get_now` dependency, pure modules take `now`/epoch floats. Tests use `+00:00` timestamps only.
- **403, not 404, for foreign bidding rows** (bidding router convention) vs 404 for foreign S1 rows — don't change the existing convention; new negative tests assert the existing code's choice.
- **The tests are the contract.** Do NOT edit or weaken them. If a test looks wrong, stop and report it instead of changing it.
- **Fake verdict (checked against `tests/py/api/fake_supabase.py`).** The fake supports every query shape this plan's tests need: `select` with `eq`/`in_`/`order`/`limit`, `insert`, `update` with two `eq` filters returning `[]` when nothing matched, generic tables via `seed()`. No fake extension is needed.
- **Running tests,** from `apps/api-py/`: `uv run --offline pytest ../../tests/py/api/<file> -q` (API) or `uv run --offline pytest ../../tests/py/api/<file> -q`.
- **Full suite before each commit:** `uv run --offline pytest -q` (from `apps/api-py/`). The baseline is 698 passed, and no test may fail.
- **Commit:** one commit per task. The message is given in each task.
- **Offline honesty.** Tasks flag what cannot be tested offline: `pip-audit` needs network; `schemathesis` runs offline via ASGI against the fake app; a live 429 may need the limiter's clock. For network-only steps, the test asserts wiring/config and the task says `verify live with: <command>`.

## Build order and parallelism

- Task 1 (security headers, `app.py` only) and Task 3 (RLS policies, `schema.sql` only) are disjoint: run them in parallel.
- Task 2 (slowapi limiter) depends on Task 1 only for the integer in `app.py` merge order — if Task 1 hasn't landed, wire the limiter against `main` and rebase; otherwise run sequential after Task 1.
- Task 4 (pip-audit + CI workflow) is independent of 1-3: run in parallel.
- Task 5 (schemathesis fuzz) is independent of 1-4 (it only reads the OpenAPI schema via the fake app): run in parallel, but install its dev dep after Task 4's `pyproject.toml` change if they touch the same file.
- Task 6 (negative tests per endpoint group) depends on Tasks 1-2 for the headers/429 assertions: run last, after 1 and 2 are in.
- Practical order: 1 + 3 + 4 in parallel → 2 → 5 → 6.

## File map

| File | Responsibility |
|---|---|
| `apps/api-py/src/cantrack_api/app.py` | Security-headers middleware (task 1) and slowapi limiter wiring (task 2) |
| `apps/api-py/src/cantrack_api/routers/auth.py` | Rate-limit decorators on `POST /auth/login` and `POST /auth/signup` + `POST /auth/reset-password` (task 2) |
| `apps/api-py/src/cantrack_api/routers/bidding.py` | Rate-limit decorators on `POST /bidding/requests` and `POST /bidding/requests/{id}/offers` (task 2) |
| `apps/api-py/pyproject.toml` | Add `slowapi` (task 2), `pip-audit` (task 4), `schemathesis` (task 5) to deps/dev |
| `cantrack-db/apps/api-py/schema.sql` | Defence-in-depth `create policy` per RLS-enabled table (task 3) — SQL-only; Claude applies it, opencode cannot write the sibling repo |
| `.github/workflows/ci.yml` | CI: `uv run pytest` + `pip-audit` (task 4) |
| `tests/py/api/test_security_headers.py` | Task 1 — headers present on every response |
| `tests/py/api/test_rate_limit.py` | Task 2 — limiter wiring + live 429 |
| `tests/py/api/test_rls_policies.py` | Task 3 — grep `schema.sql` for a policy per enabled table |
| `tests/py/api/test_ci_workflow.py` | Task 4 — workflow exists, valid YAML, names both commands |
| `tests/py/api/test_fuzz_schema.py` | Task 5 — schemathesis against bidding OpenAPI via fake app, no 500s |
| `tests/py/api/test_hardening_negatives.py` | Task 6 — one deny-path per endpoint group (403/401/413/400/429) |

---

### Task 1: Security headers middleware (parallel with 3, 4)

**Files:**
- Modify: `apps/api-py/src/cantrack_api/app.py` (add middleware, no new dependency)
- Test: `tests/py/api/test_security_headers.py`

**Interface:** No new public function. `create_app()` installs a `BaseHTTPMiddleware` (pure `starlette`, no new dep) that sets these headers on every response, including errors:

```
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
Referrer-Policy: no-referrer
Content-Security-Policy: default-src 'none'; frame-ancestors 'none'
```

O(1) per request. CORS stays as-is (middleware ordering: CORS outermost, security headers inner, or vice versa — document the choice and keep `CORSMiddleware` first as today).

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_security_headers.py`:

```python
"""Task 1: every response carries the API security headers."""

from .conftest import bearer


class TestSecurityHeaders:
    def test_success_carries_headers(self, fake_client):
        res = fake_client.get("/_whoami", headers=bearer("token-a"))
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        assert res.headers["X-Frame-Options"] == "DENY"
        assert res.headers["Referrer-Policy"] == "no-referrer"
        assert res.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"

    def test_unauthorized_still_carries_headers(self, fake_client):
        res = fake_client.get("/_whoami")
        assert res.status_code == 401
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        assert res.headers["X-Frame-Options"] == "DENY"

    def test_bidding_carries_headers(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        res = fake_client.get("/bidding/requests/open", headers=bearer("token-w"))
        assert res.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"

    def test_cors_still_works_with_headers(self, fake_client):
        res = fake_client.get(
            "/_whoami",
            headers={**bearer("token-a"), "Origin": "http://localhost:5173"},
        )
        assert res.headers.get("access-control-allow-origin") == "http://localhost:5173"
        assert res.headers["X-Content-Type-Options"] == "nosniff"
```

- [ ] **Step 2: Run it and confirm it fails** (KeyError on `X-Content-Type-Options`, middleware missing).
- [ ] **Step 3: Implement** the middleware in `app.py` (no `secure` dep; stdlib/starlette only). Keep `NFR-03` unaffected — this task adds no structure code.
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite (698 + 4 pass).
- [ ] **Step 5: Commit.** `git add -A apps/api-py/src/cantrack_api/app.py tests/py/api/test_security_headers.py && git commit -m "feat(security): add nosniff/DENY/no-referrer/CSP headers middleware"`

**Run command (from `apps/api-py/`):** `uv run --offline pytest ../../tests/py/api/test_security_headers.py -q`

---

### Task 2: Rate limiting with slowapi (after task 1)

**Files:**
- Modify: `apps/api-py/pyproject.toml` (add `slowapi`)
- Modify: `apps/api-py/src/cantrack_api/app.py` (create `Limiter`, attach to `app.state`, add `SlowAPIMiddleware`, register 429 handler)
- Modify: `apps/api-py/src/cantrack_api/routers/auth.py` (decorate login/signup/reset-password)
- Modify: `apps/api-py/src/cantrack_api/routers/bidding.py` (decorate `POST /bidding/requests` and `POST /bidding/requests/{id}/offers`)
- Test: `tests/py/api/test_rate_limit.py`

**Interface:**

```python
# app.py
from slowapi import Limiter
from slowapi.util import get_remote_address
limiter = Limiter(key_func=get_remote_address, default_limits=[])  # O(1) per request
# app.state.limiter = limiter; app.add_middleware(SlowAPIMiddleware); app.add_exception_handler(429, _rate_limit_exceeded_handler)

# routers/auth.py — per-route limits (tune to spec; example: 5/minute)
@router.post("/login")
@limiter.limit("5/minute")
def login(request: Request, body: LoginBody, ...): ...

# routers/bidding.py
@router.post("/requests", status_code=201)
@limiter.limit("10/minute")
def create_bidding_request(request: Request, body: CreateBiddingRequestBody, ...): ...

@router.post("/requests/{request_id}/offers", status_code=201)
@limiter.limit("10/minute")
def send_offer(request: Request, request_id: str, body: SendOfferBody, ...): ...
```

Each decorated handler takes `request: Request` as its first param (slowapi requirement). The limiter is in-memory and `FakeClock`-free. O(1).

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_rate_limit.py`:

```python
"""Task 2: floodable endpoints are rate-limited (slowapi, in-memory limiter)."""

import inspect

from cantrack_api.app import create_app
from cantrack_api.routers import auth as auth_router
from cantrack_api.routers import bidding as bidding_router

from .conftest import bearer


class TestRateLimitWiring:
    def test_limiter_is_wired_on_the_app(self):
        app = create_app()
        assert hasattr(app.state, "limiter")
        assert app.state.limiter is not None

    def test_login_has_rate_limit_decorator(self):
        assert hasattr(auth_router.login, "_rate_limit_decorator") or "limiter" in inspect.getsource(auth_router.login)

    def test_bidding_post_and_offer_have_limits(self):
        assert "limiter" in inspect.getsource(bidding_router.create_bidding_request)
        assert "limiter" in inspect.getsource(bidding_router.send_offer)

    def test_nth_plus_one_gets_429(self, fake_client, fake):
        # limit is 5/minute on /auth/login; drive it offline via the in-memory limiter
        for _ in range(5):
            res = fake_client.post("/auth/login", json={"email": "a@a.com", "password": "x"})
            assert res.status_code in (401, 400, 429)
        res = fake_client.post("/auth/login", json={"email": "a@a.com", "password": "x"})
        assert res.status_code == 429

    def test_bidding_flood_gets_429(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        for _ in range(10):
            fake_client.post(
                "/bidding/requests",
                json={"dogId": "dog-1", "windowStart": "2026-10-05T10:00:00+00:00", "durationHours": 1, "priceCop": 30000, "pickupLat": 6.2, "pickupLng": -75.5},
                headers=bearer("token-a"),
            )
        res = fake_client.post(
            "/bidding/requests",
            json={"dogId": "dog-1", "windowStart": "2026-10-05T10:00:00+00:00", "durationHours": 1, "priceCop": 30000, "pickupLat": 6.2, "pickupLng": -75.5},
            headers=bearer("token-a"),
        )
        assert res.status_code == 429
```

Fallback if `slowapi` cannot be exercised offline in tests: keep the limiter + the decorator, and change the last two methods to assert the decorator exists rather than a live 429, with a comment `# live 429 needs the limiter's window clock; verify live with: curl -i ...` and say so in the plan. The wiring tests (`app.state.limiter`, `inspect.getsource`) always pass offline.

- [ ] **Step 2: Run it and confirm it fails** (ImportError on `slowapi` / missing `app.state.limiter`).
- [ ] **Step 3: Add `slowapi` to `pyproject.toml`** (`dependencies` or `dependency-groups.dev` — pick one and document it), wire the limiter in `app.py`, add `request: Request` + `@limiter.limit(...)` to the four handlers. This task adds no structure code, so `heapq`/`bisect`/`collections`/`queue` are unaffected — say so in the commit body.
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite (698 + new pass). If the live 429 is flaky offline, keep the wiring assertion and note `verify live with: for i in $(seq 1 11); do curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8000/auth/login -H "Content-Type: application/json" -d '{"email":"a@a.com","password":"x"}'; done` (expect a 429 on the 6th+).
- [ ] **Step 5: Commit.** `git add -A apps/api-py/pyproject.toml apps/api-py/src/cantrack_api/app.py apps/api-py/src/cantrack_api/routers/auth.py apps/api-py/src/cantrack_api/routers/bidding.py tests/py/api/test_rate_limit.py && git commit -m "feat(security): slowapi rate limits on auth and bidding floodables"`

**Run command (from `apps/api-py/`):** `uv run --offline pytest ../../tests/py/api/test_rate_limit.py -q`

**Offline honesty:** slowapi's in-memory limiter is offline-capable, but its window is wall-clock based. The TestClient drives it by issuing N requests synchronously; if the CI clock granularity makes the last assertion flaky, the test falls back to wiring-only and the task documents the `verify live with:` curl above.

---

### Task 3: RLS policies as defence in depth (parallel with 1, 4)

**Files:**
- Modify: `cantrack-db/apps/api-py/schema.sql` (append one `S5 hardening — RLS policies` block, idempotent)
- Test: `tests/py/api/test_rls_policies.py`

**Interface:** SQL only. For each of the 16 tables that has `enable row level security` but zero `create policy` today, add owner/walker-scoped `create policy` statements as defence in depth, with a clear comment on every block that the API uses the service-role key so these only bite if a client ever uses the `anon` key directly. This is a SQL-only task (Claude will apply it; opencode cannot write the sibling `cantrack-db` repo — note that in the task).

Tables (16): `dogs`, `routes`, `checkins`, `walker_profiles`, `walk_requests`, `checkouts`, `ai_usage`, `walk_offers`, `walker_positions`, `dog_pack_links`, `walk_graph_edges`, `walk_route_stops`, `walker_cancel_buckets`, `walk_ratings`, `walk_timeline`, `walk_gps_points`.

Example shape (repeat per table, column varies):

```sql
-- S5 hardening: defence in depth. The API uses the service-role key and bypasses
-- RLS; these policies only apply if a client ever calls Supabase with the anon key.
-- Owner-scoped tables restrict to auth.uid() = owner_id; walker-scoped to walker_id.
create policy if not exists "dogs_owner_select" on public.dogs
  for select using (auth.uid() = owner_id);
create policy if not exists "dogs_owner_write" on public.dogs
  for all using (auth.uid() = owner_id) with check (auth.uid() = owner_id);
-- ... one select + one all/with-check per table (or for all) ...
```

Idempotent style (`if not exists` where supported, or `drop policy if exists` + `create policy`). No new app code, so `heapq`/`bisect`/`collections`/`queue` are unaffected.

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_rls_policies.py`:

```python
"""Task 3: schema.sql has a policy for every RLS-enabled table."""

import pathlib
import re

SCHEMA = pathlib.Path(__file__).resolve().parents[3] / "cantrack-db" / "apps" / "api-py" / "schema.sql"

RLS_TABLES = [
    "dogs", "routes", "checkins", "walker_profiles", "walk_requests",
    "checkouts", "ai_usage", "walk_offers", "walker_positions",
    "dog_pack_links", "walk_graph_edges", "walk_route_stops",
    "walker_cancel_buckets", "walk_ratings", "walk_timeline", "walk_gps_points",
]


class TestRlsPolicies:
    def test_schema_enables_rls_for_all_tables(self):
        sql = SCHEMA.read_text()
        for table in RLS_TABLES:
            assert re.search(rf"alter table public\.{table} enable row level security", sql), f"missing RLS on {table}"

    def test_schema_has_a_policy_per_table(self):
        sql = SCHEMA.read_text()
        for table in RLS_TABLES:
            assert re.search(rf"create policy.*on public\.{table}", sql, re.IGNORECASE), f"missing policy on {table}"

    def test_policies_are_commented_as_defence_in_depth(self):
        sql = SCHEMA.read_text().lower()
        assert "defence in depth" in sql or "defense in depth" in sql
        assert "service-role" in sql or "service_role" in sql or "service role" in sql

    def test_schema_still_has_16_rls_tables(self):
        sql = SCHEMA.read_text()
        assert len(re.findall(r"enable row level security", sql)) == 16
```

- [ ] **Step 2: Run it and confirm it fails** (missing `create policy` per table).
- [ ] **Step 3: Append** the SQL block to `schema.sql` (after the S4 bidding block). Include the defence-in-depth comment verbatim. Note in the commit body: `SQL-only; opencode cannot write cantrack-db — Claude applies it.`
- [ ] **Step 4: Run it and the full suite** (698 + 4 pass; schema change does not affect fakes).
- [ ] **Step 5: Commit.** `git add -A cantrack-db/apps/api-py/schema.sql tests/py/api/test_rls_policies.py && git commit -m "feat(db): defence-in-depth RLS policies for all 16 RLS tables"`

**Run command (from `apps/api-py/`):** `uv run --offline pytest ../../tests/py/api/test_rls_policies.py -q`

**Note:** This task is SQL-only and touches the sibling repo `cantrack-db`. `opencode` (cheaper model) cannot write that repo — the task must be claimed by Claude or the human must apply the SQL in the Supabase SQL Editor. The pytest grep runs offline.

---

### Task 4: pip-audit in CI (parallel with 1, 3)

**Files:**
- Modify: `apps/api-py/pyproject.toml` (add `pip-audit` to dev deps)
- Create: `.github/workflows/ci.yml`
- Test: `tests/py/api/test_ci_workflow.py`

**Interface:** File formats, not code. `.github/workflows/ci.yml` runs on `push`/`pull_request` and has two steps that name both commands: `uv run pytest` (or `uv run --offline pytest`) and `pip-audit` (either `uvx pip-audit` or `pip-audit` on the lockfile). Valid YAML. O(1).

```yaml
name: ci
on: [push, pull_request]
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv run pytest -q
      - run: uvx pip-audit  # or pip-audit --desc on
```

`pip-audit` added to `[dependency-groups].dev` (or `[tool.uv]` dev) so `uv run pip-audit` works locally.

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_ci_workflow.py`:

```python
"""Task 4: CI runs pytest and pip-audit; workflow is valid YAML."""

import pathlib
import yaml


WORKFLOW = pathlib.Path(__file__).resolve().parents[3] / ".github" / "workflows" / "ci.yml"


class TestCiWorkflow:
    def test_workflow_exists_and_is_valid_yaml(self):
        assert WORKFLOW.exists(), "missing .github/workflows/ci.yml"
        data = yaml.safe_load(WORKFLOW.read_text())
        assert isinstance(data, dict)
        assert "jobs" in data

    def test_workflow_runs_pytest(self):
        text = WORKFLOW.read_text()
        assert "pytest" in text
        assert "uv run" in text

    def test_workflow_runs_pip_audit(self):
        text = WORKFLOW.read_text()
        assert "pip-audit" in text or "pip_audit" in text

    def test_pyproject_has_pip_audit_dev_dep(self):
        text = (pathlib.Path(__file__).resolve().parents[3] / "apps" / "api-py" / "pyproject.toml").read_text()
        assert "pip-audit" in text
```

- [ ] **Step 2: Run it and confirm it fails** (missing workflow file / missing `pip-audit` in `pyproject.toml`).
- [ ] **Step 3: Create** `.github/workflows/ci.yml` and **add** `pip-audit` to `pyproject.toml` dev deps. This task adds no structure code, so `heapq`/`bisect`/`collections`/`queue` are unaffected.
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite (698 + 4 pass).
- [ ] **Step 5: Commit.** `git add -A apps/api-py/pyproject.toml .github/workflows/ci.yml tests/py/api/test_ci_workflow.py && git commit -m "ci: run pytest and pip-audit on push/PR"`

**Run command (from `apps/api-py/`):** `uv run --offline pytest ../../tests/py/api/test_ci_workflow.py -q`

**Offline honesty:** `pip-audit` needs network (it fetches the vulnerability DB). The pytest only asserts wiring/config (file exists, valid YAML, names both commands). Verify live with: `uvx pip-audit` (or `uv run pip-audit` after `uv sync --group dev`) — expect exit 0 when no known vulns.

---

### Task 5: Schemathesis property/fuzz tests (after task 4 for pyproject ordering)

**Files:**
- Modify: `apps/api-py/pyproject.toml` (add `schemathesis` to dev deps)
- Create: `tests/py/api/test_fuzz_schema.py`

**Interface:** No new app code. `test_fuzz_schema.py` loads the app's OpenAPI schema via the fake Supabase (no network) and runs `schemathesis` against the bidding endpoints, asserting no 500s on generated inputs (only documented 4xx or 2xx). Bounded (few examples) so CI stays fast. Uses the same `fake_client` fixture / `create_app` factory.

```python
# tests/py/api/test_fuzz_schema.py — shape
import schemathesis
from schemathesis import from_asgi

# from_asgi("/openapi.json", app=create_app()) wired to fake_client's overrides
# @schema.parametrize() def test_no_500(case): ...
# Hypothesis: max_examples=25, deadline=None, phases=[generate]
```

O(examples) per endpoint; keeps `hypothesis` deadline off for CI speed.

- [ ] **Step 1: Write the failing test** at `tests/py/api/test_fuzz_schema.py`:

```python
"""Task 5: schemathesis fuzz — no 500s on generated bidding inputs (offline via ASGI)."""

import schemathesis
from hypothesis import HealthCheck, given, settings

from cantrack_api.app import create_app

from .conftest import FIXED_NOW, make_user
from .fake_ai import FakeEmbedder, FakeVision
from .fake_supabase import FakeSupabase

# Build an ASGI app with fakes so schemathesis needs no network.
def _fuzz_app():
    fake = FakeSupabase()
    fake.add_user("token-a", make_user("owner-a", "owner-a@example.com", "owner"))
    fake.add_user("token-w", make_user("walker-w", "walker-w@example.com", "walker"))
    app = create_app()
    from cantrack_api.deps import get_supabase, get_auth_client, get_embedder, get_vision, get_now
    app.dependency_overrides[get_supabase] = lambda: fake
    app.dependency_overrides[get_auth_client] = lambda: fake
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[get_vision] = lambda: FakeVision()
    app.dependency_overrides[get_now] = lambda: FIXED_NOW
    return app


schema = schemathesis.from_asgi("/openapi.json", app=_fuzz_app())


@schema.parametrize()
@settings(max_examples=15, deadline=None, suppress_health_check=list(HealthCheck))
def test_bidding_no_500(case):
    # Only the bidding prefix; S1 endpoints already covered elsewhere.
    if not case.operation.path.startswith("/bidding"):
        return
    response = case.call_asgi()
    assert response.status_code != 500, f"500 on {case.operation.path} with {case.body}"
    assert response.status_code in (200, 201, 400, 401, 403, 404, 409, 413, 422)


def test_schema_loads():
    assert len(list(schema.get_all_operations())) > 0
```

Tune `max_examples` to keep CI under 30s; the exact count is not contractual. If `schemathesis`'s ASGI integration needs `app` instead of `from_asgi`, wire it equivalently.

- [ ] **Step 2: Run it and confirm it fails** (ImportError on `schemathesis`).
- [ ] **Step 3: Add `schemathesis` + `hypothesis` to `pyproject.toml` dev deps** and **create** `test_fuzz_schema.py` with the bounded settings above. This task adds no structure code, so `heapq`/`bisect`/`collections`/`queue` are unaffected — say so in the commit body. Keep it offline: `case.call_asgi()` against the fake app, no network.
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite (698 + 2 pass; fuzz may be slower — allow 60s). Cap examples so `uv run --offline pytest ../../tests/py/api/test_fuzz_schema.py -q` stays < 30s.
- [ ] **Step 5: Commit.** `git add -A apps/api-py/pyproject.toml tests/py/api/test_fuzz_schema.py && git commit -m "test(fuzz): schemathesis against bidding OpenAPI via fake ASGI, no 500s"`

**Run command (from `apps/api-py/`):** `uv run --offline pytest ../../tests/py/api/test_fuzz_schema.py -q`

**Offline honesty:** schemathesis is offline via ASGI (`case.call_asgi()` against `create_app` with `dependency_overrides`). No network. The only live verification is the same command — no extra `verify live with:` needed.

---

### Task 6: One negative test per endpoint group — wrong owner, bad token, oversized file, flood (after tasks 1, 2)

**Files:**
- Create: `tests/py/api/test_hardening_negatives.py`
- Modify: none (this task only adds tests; it documents which deny paths are already covered and adds the missing ones — do NOT duplicate existing bidding tests).

**Interface:** No new app code. One `pytest` module that asserts the deny paths for every bidding + S1 endpoint group, reusing `conftest.fake_client`, `fake_supabase`, `FakeClock`, `bearer`. Many of these already exist scattered in the bidding tests — this task adds **only the missing ones** and may reference existing ones in comments, not duplicate them. Grouped into classes per endpoint.

Coverage (one per group; 401/403/400/413/429):

- `GET /_whoami` — missing token → 401 `A bearer token is required.`
- `POST /auth/login` — bad password → 401; flood → 429 (where task 2 applies; else wiring assertion)
- `POST /bidding/requests` — wrong role (walker) → 400 `Only owners can post…`; unknown dog → 404 `Dog not found.`; naive `windowStart` (no offset) → 400; past window → 400; missing token → 401
- `GET /bidding/requests/open` — non-walker → 400 `Only walkers can list…`; missing token → 401
- `POST /bidding/requests/{id}/offers` — hidden/not-open → 403 `Walk not visible…`; second offer same walker → 409; non-walker → 400; missing token → 401
- `GET /bidding/requests/{id}/offers` — wrong owner → 403 `Not your walk request.`
- `POST /bidding/requests/{id}/pick` — wrong owner → 403; non-open → 409 `Walk no longer open.`; unknown offer → 404
- `POST /bidding/requests/{id}/auto-pick` — before deadline → 400 `Deadline has not passed yet.`; not-open → 409
- `POST /bidding/requests/{id}/walker-cancel` — empty bucket → 403 `No cancellation tokens left.`; not-assigned → 409; foreign walker → 403
- `POST /bidding/requests/{id}/owner-cancel` — wrong state → 409; foreign owner → 403
- `POST /bidding/requests/{id}/no-show` — too early → 400 `Too early to mark a no-show.`; not-assigned → 409
- `POST /bidding/requests/{id}/check-in` / `check-out` — wrong state → 409 `That transition is not allowed…`; foreign walker → 403
- `POST /bidding/requests/{id}/ratings` — not-done → 409 `Only a finished walk can be rated.`; second rating → 409 `Walk already rated.`; out-of-range rating → 400; foreign owner → 403
- `GET /bidding/requests/{id}/replay` — not-done → 409 `Only a finished walk can be replayed.`; foreign owner → 403; bad `direction` → 400
- S1 `POST /dogs/{id}/photos` — oversized → 413 `Image is too large.`; unsupported type → 400 `Unsupported image type.`; foreign dog → 404 `Dog not found.`; count > MAX_REFERENCE_PHOTOS → 400 (uploads.py limits ALREADY EXIST — this test proves them, do NOT re-add limits)
- `POST /routes/{id}/checkin` (S1 check-in) — oversized → 413; foreign route → 404

`uploads.py` limits (`MAX_IMAGE_BYTES` 5MB, `SUPPORTED_IMAGE_TYPES`, `MAX_REFERENCE_PHOTOS`) already exist — do NOT re-add them; a test proves them instead.

- [ ] **Step 1: Write the test** at `tests/py/api/test_hardening_negatives.py` (complete, non-duplicating — it lists the missing cases and reuses fakes):

```python
"""Task 6: one deny-path per endpoint group — the highest-value hardening tests.

Many happy-path-adjacent denies already exist in test_bidding_*.py; this module
adds ONLY the missing ones and references the existing coverage in comments
so we don't duplicate. The real risk is a broken caller-scoped query letting
one user read another's rows (service-role bypasses RLS), so wrong-owner →
403/404 and bad-token → 401 are the blocking assertions.
"""

import io

import pytest
from fastapi import UploadFile

from cantrack_api.uploads import MAX_IMAGE_BYTES, MAX_REFERENCE_PHOTOS

from .conftest import FIXED_NOW, bearer, make_user

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"


def seed_open(fake, request_id="walk-1", owner_id="owner-a"):
    fake.seed("dogs", id="dog-1", owner_id=owner_id, name="Firulais")
    return fake.seed(
        "walk_requests", id=request_id, owner_id=owner_id, walker_id=None, dog_id="dog-1",
        status="open", requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


def seed_assigned(fake, request_id="walk-1", walker_id="walker-w"):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    return fake.seed(
        "walk_requests", id=request_id, owner_id="owner-a", walker_id=walker_id, dog_id="dog-1",
        status="assigned", requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


class TestAuthNegatives:
    def test_whoami_without_token_is_401(self, fake_client):
        res = fake_client.get("/_whoami")
        assert res.status_code == 401
        assert res.json()["detail"] == "A bearer token is required."

    def test_whoami_bad_token_is_401(self, fake_client):
        res = fake_client.get("/_whoami", headers=bearer("bad-token"))
        assert res.status_code == 401

    def test_login_headers_present_on_401(self, fake_client):
        res = fake_client.post("/auth/login", json={"email": "no@no.com", "password": "x"})
        assert res.status_code in (400, 401)
        assert res.headers["X-Content-Type-Options"] == "nosniff"


class TestBiddingRequestNegatives:
    def test_walker_cannot_post_bidding_request(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        res = fake_client.post(
            "/bidding/requests",
            json={"dogId": "dog-1", "windowStart": T10, "durationHours": 1, "priceCop": 30000, "pickupLat": 6.2, "pickupLng": -75.5},
            headers=bearer("token-w"),
        )
        assert res.status_code == 400

    def test_unknown_dog_is_404(self, fake_client):
        res = fake_client.post(
            "/bidding/requests",
            json={"dogId": "nope", "windowStart": T10, "durationHours": 1, "priceCop": 30000, "pickupLat": 6.2, "pickupLng": -75.5},
            headers=bearer("token-a"),
        )
        assert res.status_code == 404

    def test_naive_window_is_400(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        res = fake_client.post(
            "/bidding/requests",
            json={"dogId": "dog-1", "windowStart": "2026-10-05T10:00:00", "durationHours": 1, "priceCop": 30000, "pickupLat": 6.2, "pickupLng": -75.5},
            headers=bearer("token-a"),
        )
        assert res.status_code == 400

    def test_wrong_owner_offers_is_403(self, fake_client, fake):
        seed_open(fake)
        res = fake_client.get("/bidding/requests/walk-1/offers", headers=bearer("token-b"))
        assert res.status_code == 403

    def test_pick_unknown_offer_is_404(self, fake_client, fake):
        seed_open(fake)
        fake.seed("walker_positions", walker_id="walker-w", lat=6.201, lng=-75.5, updated_at="2026-10-01T00:00:00+00:00")
        res = fake_client.post("/bidding/requests/walk-1/pick", json={"offerId": "nope"}, headers=bearer("token-a"))
        assert res.status_code == 404

    def test_auto_pick_before_deadline_is_400(self, fake_client, fake):
        seed_open(fake)
        res = fake_client.post("/bidding/requests/walk-1/auto-pick", headers=bearer("token-a"))
        assert res.status_code == 400

    def test_walker_cancel_empty_bucket_is_403(self, fake_client, fake):
        seed_assigned(fake)
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=0, period_start="2026-10-01T00:00:00+00:00")
        res = fake_client.post("/bidding/requests/walk-1/walker-cancel", headers=bearer("token-w"))
        assert res.status_code == 403

    def test_no_show_too_early_is_400(self, fake_client, fake):
        seed_assigned(fake)
        res = fake_client.post("/bidding/requests/walk-1/no-show", headers=bearer("token-a"))
        assert res.status_code == 400

    def test_check_out_from_assigned_is_409(self, fake_client, fake):
        seed_assigned(fake)
        res = fake_client.post("/bidding/requests/walk-1/check-out", headers=bearer("token-w"))
        assert res.status_code == 409

    def test_rating_unfinished_is_409(self, fake_client, fake):
        seed_assigned(fake)
        res = fake_client.post("/bidding/requests/walk-1/ratings", json={"rating": 5}, headers=bearer("token-a"))
        assert res.status_code == 409

    def test_replay_foreign_owner_is_403(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        fake.seed("walk_requests", id="walk-1", owner_id="owner-a", walker_id="walker-w", dog_id="dog-1",
                  status="done", requested_time=T10, window_end=T11,
                  deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
                  pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000, created_at="2026-10-01T00:00:00+00:00")
        res = fake_client.get("/bidding/requests/walk-1/replay", headers=bearer("token-b"))
        assert res.status_code == 403

    def test_bidding_headers_on_403(self, fake_client, fake):
        seed_open(fake)
        res = fake_client.get("/bidding/requests/walk-1/offers", headers=bearer("token-b"))
        assert res.status_code == 403
        assert res.headers["X-Frame-Options"] == "DENY"


class TestUploadLimits:
    def test_oversized_image_is_413(self, fake_client, fake):
        from cantrack_api.uploads import read_image
        big = UploadFile(filename="big.jpg", file=io.BytesIO(b"x" * (MAX_IMAGE_BYTES + 1)))
        big.content_type = "image/jpeg"
        with pytest.raises(Exception) as exc:
            read_image(big)
        assert getattr(exc.value, "status_code", None) == 413

    def test_unsupported_type_is_400(self, fake_client, fake):
        from cantrack_api.uploads import read_image
        bad = UploadFile(filename="bad.pdf", file=io.BytesIO(b"%PDF"))
        bad.content_type = "application/pdf"
        with pytest.raises(Exception) as exc:
            read_image(bad)
        assert getattr(exc.value, "status_code", None) == 400

    def test_too_many_photos_is_400(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        files = [("image", ("a.jpg", b"\xff\xd8\xff", "image/jpeg"))] * (MAX_REFERENCE_PHOTOS + 1)
        res = fake_client.post(f"/dogs/dog-1/photos", files=files, headers=bearer("token-a"))
        assert res.status_code == 400

    def test_foreign_dog_photo_is_404(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-b", name="Firulais")
        files = [("image", ("a.jpg", b"\xff\xd8\xff", "image/jpeg"))]
        res = fake_client.post(f"/dogs/dog-1/photos", files=files, headers=bearer("token-a"))
        assert res.status_code == 404
```

If an endpoint already has this deny test in `test_bidding_*.py`, replace that method with a comment `# already covered in test_bidding_pick.py::TestPick::test_wrong_owner_pick_is_403` and keep the module green — do not duplicate.

- [ ] **Step 2: Run it and confirm it fails** where the deny is missing (else it passes — the task adds only missing cases).
- [ ] **Step 3: Add** the missing deny assertions (or prove they already pass). No new app code; this task is test-only. `heapq`/`bisect`/`collections`/`queue` unaffected — say so in the commit body. Flood → 429 is covered by Task 2's limit; this module asserts the same 429 where applicable, or references Task 2.
- [ ] **Step 4: Run it and confirm it passes,** plus the full suite (698 + new pass; many tests were already green).
- [ ] **Step 5: Commit.** `git add -A tests/py/api/test_hardening_negatives.py && git commit -m "test(hardening): deny-path per endpoint group — 403/401/413/400/429"`

**Run command (from `apps/api-py/`):** `uv run --offline pytest ../../tests/py/api/test_hardening_negatives.py -q`

**Offline honesty:** All deny paths are offline via `fake_client`/`fake_supabase` (no network). The `429` path reuses Task 2's in-memory limiter; if the window is flaky offline, the test asserts wiring and documents `verify live with:` as in Task 2.

---

## Done when

- 6 tasks in 6 commits (tasks 1+3+4 may be built in parallel, 2 after 1, 5 after 4, 6 last).
- `uv run --offline pytest -q` (from `apps/api-py/`) → 698 + new tests pass, 0 failed; no S1 or bidding test broke.
- `grep -rnE "import (heapq|bisect|collections|queue)|from (heapq|bisect|collections|queue) " apps/api-py/src/cantrack_api/app.py apps/api-py/src/cantrack_api/routers/*.py apps/api-py/src/cantrack_api/bidding*.py apps/api-py/src/cantrack_api/replay.py` → no output (these tasks add no structure code).
- Every response carries `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'` (Task 1).
- Floodable routes (`POST /auth/login`, `POST /auth/signup`, `POST /auth/reset-password`, `POST /bidding/requests`, `POST /bidding/requests/{id}/offers`) are decorated with `slowapi` limits and a 429 handler (Task 2); offline test proves wiring and, where stable, a live 429.
- `schema.sql` has a `create policy` for each of the 16 RLS-enabled tables, commented as defence-in-depth for the `anon` key (Task 3); `test_rls_policies.py` greps it.
- `.github/workflows/ci.yml` exists, is valid YAML, and names both `pytest` and `pip-audit`; `pyproject.toml` lists `pip-audit` (Task 4). `pip-audit` itself is network-only — test asserts config; `verify live with: uvx pip-audit`.
- `test_fuzz_schema.py` loads the OpenAPI via ASGI + fakes and asserts no 500s on generated bidding inputs, bounded examples, no network (Task 5).
- `test_hardening_negatives.py` asserts one deny per endpoint group: wrong owner → 403, bad/missing token → 401, oversized file → 413/400, flood → 429, plus bad window/empty bucket/too-early/unknown offer — reusing fakes, proving `uploads.py` limits without re-adding them (Task 6).

## Next plans (not in this file)

- Plan 1 (structures) and Plan 2 (bidding API) are done. This plan is the last of the three.
- Follow-ups beyond this plan: S3 checkout retention cron (needs infra), frontend CSP meta, and secret rotation — each its own plan.

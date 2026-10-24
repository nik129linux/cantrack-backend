"""Schemathesis fuzz: no 5xx on generated inputs, on EVERY endpoint (offline via ASGI).

PR0 widened it from /bidding to the whole API and sends a real owner or walker
token, so generated input reaches the business logic instead of stopping at 401.

No network: the app is built with the fake Supabase and FakeClock via dependency_overrides,
and schemathesis drives it through the in-process ASGI transport (case.call_asgi()).
Adds no structure code, so the heapq/bisect/collections/queue ban does not apply here.
"""

import schemathesis
from hypothesis import HealthCheck, settings

from cantrack_api.app import create_app

from .conftest import FIXED_NOW, make_user
from .fake_ai import FakeEmbedder, FakeVision
from .fake_supabase import FakeSupabase


def _fuzz_app():
    fake = FakeSupabase()
    fake.add_user("token-a", make_user("owner-a", "owner-a@example.com", "owner"))
    fake.add_user("token-w", make_user("walker-w", "walker-w@example.com", "walker"))
    app = create_app()
    from cantrack_api.deps import (
        get_auth_client,
        get_embedder,
        get_now,
        get_supabase,
        get_vision,
    )

    app.dependency_overrides[get_supabase] = lambda: fake
    app.dependency_overrides[get_auth_client] = lambda: fake
    app.dependency_overrides[get_embedder] = lambda: FakeEmbedder()
    app.dependency_overrides[get_vision] = lambda: FakeVision()
    app.dependency_overrides[get_now] = lambda: FIXED_NOW
    return app


# FastAPI emits OpenAPI 3.1.0; schemathesis 3.x only fully supports 3.0.x, so we ask it to
# read the schema under the 3.0 interpretation. The bidding bodies are flat JSON with no
# 3.1-only constructs, so this does not lose fidelity for what we fuzz.
schema = schemathesis.from_asgi("/openapi.json", app=_fuzz_app(), force_schema_version="30")


ALLOWED = (200, 201, 400, 401, 403, 404, 405, 409, 413, 422, 429)


def _assert_no_server_error(case, token):
    response = case.call_asgi(headers={"Authorization": f"Bearer {token}"})
    assert response.status_code < 500, (
        f"{response.status_code} on {case.method} {case.operation.path} with {case.body!r}"
    )
    assert response.status_code in ALLOWED


@schema.parametrize()
@settings(max_examples=15, deadline=None, suppress_health_check=list(HealthCheck))
def test_no_5xx_as_owner(case):
    _assert_no_server_error(case, "token-a")


@schema.parametrize()
@settings(max_examples=15, deadline=None, suppress_health_check=list(HealthCheck))
def test_no_5xx_as_walker(case):
    _assert_no_server_error(case, "token-w")


def test_schema_loads():
    assert len(list(schema.get_all_operations())) > 0

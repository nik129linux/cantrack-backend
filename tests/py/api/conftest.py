"""Shared fixtures for the API tests.

Supabase is always faked: these tests cover CanTrack's own HTTP surface and
validation, not Supabase's behaviour. The fake is injected through the
`get_supabase` FastAPI dependency the app must expose in `cantrack_api.deps`.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from supabase_auth.types import Session, User

from cantrack_api.app import create_app
from cantrack_api.deps import get_current_user, get_embedder, get_supabase, get_vision

from .fake_ai import FakeEmbedder, FakeVision
from .fake_supabase import FakeSupabase


def make_user(user_id="user-1", email="walker@example.com", role="walker") -> User:
    return User(
        id=user_id,
        email=email,
        app_metadata={},
        user_metadata={"role": role},
        aud="authenticated",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def make_session(token="token-abc", user=None) -> Session:
    return Session(
        access_token=token,
        refresh_token="refresh-abc",
        expires_in=3600,
        token_type="bearer",
        user=user or make_user(),
    )


@pytest.fixture
def supabase() -> MagicMock:
    return MagicMock(name="supabase")


@pytest.fixture
def app(supabase) -> FastAPI:
    application = create_app()
    application.dependency_overrides[get_supabase] = lambda: supabase

    # The guard has no production route yet (dogs/routes come next), so the
    # suite mounts one probe route that depends on it.
    @application.get("/_whoami")
    def whoami(user=Depends(get_current_user)):
        return {"id": user.id, "email": user.email}

    return application


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def fake() -> FakeSupabase:
    """An in-memory Supabase with two known users: token-a (owner-a), token-b (owner-b)."""
    db = FakeSupabase()
    db.add_user("token-a", make_user("owner-a", "owner-a@example.com", "owner"))
    db.add_user("token-b", make_user("owner-b", "owner-b@example.com", "owner"))
    db.add_user("token-w", make_user("walker-w", "walker-w@example.com", "walker"))
    return db


# Fixed "now" for time-sensitive endpoints (S1: requestedTime must be in the
# future). 2026-10-01T00:00:00+00:00 keeps the 2026-10-05 constants of the
# request tests valid. Mutate `fake_clock.now` inside a test to move time.
FIXED_NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(FIXED_NOW)


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def vision() -> FakeVision:
    return FakeVision()


@pytest.fixture
def fake_client(fake, embedder, vision, fake_clock) -> TestClient:
    application = create_app()
    application.dependency_overrides[get_supabase] = lambda: fake
    application.dependency_overrides[get_embedder] = lambda: embedder
    application.dependency_overrides[get_vision] = lambda: vision
    # get_now is the overridable clock dependency the S1 endpoints use for the
    # "requestedTime must be in the future" rule. It is imported lazily so this
    # conftest stays importable (and the pre-existing suite stays green) while
    # the dependency does not exist yet — the S1 tests are red either way
    # because the endpoints themselves are missing until s1-impl.
    try:
        from cantrack_api.deps import get_now
    except ImportError:
        pass
    else:
        application.dependency_overrides[get_now] = lambda: fake_clock.now
    return TestClient(application)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}

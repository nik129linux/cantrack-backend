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
from cantrack_api.deps import (
    get_auth_client,
    get_current_user,
    get_embedder,
    get_supabase,
    get_text_model,
    get_vision,
)

from .fake_ai import FakeEmbedder, FakeTextModel, FakeVision
from .fake_supabase import FakeSupabase


def make_user(user_id="user-1", email="walker@example.com", role="walker") -> User:
    """A user whose role was set server-side (``app_metadata``), as signup does.

    The same value is mirrored into ``user_metadata`` the way legacy accounts
    look, so a router that still reads the client-writable copy cannot be told
    apart here; ``make_spoofed_user`` is the case that separates them.
    """
    return User(
        id=user_id,
        email=email,
        app_metadata={"role": role},
        user_metadata={"role": role},
        aud="authenticated",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def make_spoofed_user(user_id, email, real_role, claimed_role) -> User:
    """A user who rewrote their own ``user_metadata.role`` from the browser.

    ``supabase.auth.updateUser({data: {role}})`` needs nothing but the user's
    own session, so ``claimed_role`` is attacker-controlled; ``real_role`` is
    what the server stored in ``app_metadata`` (None = never assigned).
    """
    return User(
        id=user_id,
        email=email,
        app_metadata={"role": real_role} if real_role is not None else {},
        user_metadata={"role": claimed_role},
        aud="authenticated",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def reset_all_rate_limits() -> None:
    """Empty every rate-limit bucket of every limiter instance that exists.

    test_app reloads ``cantrack_api.app``, which builds a NEW module-level
    limiter while the routers keep the one they were decorated with; resetting
    only ``cantrack_api.app.limiter`` then leaves the routers' buckets full and
    later tests get 429. Tests that loop past a per-minute limit call this too.
    """
    import sys

    limiters = {
        id(module.limiter): module.limiter
        for name, module in list(sys.modules.items())
        if name.startswith("cantrack_api") and hasattr(module, "limiter")
    }
    for limiter in limiters.values():
        limiter.reset()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Every test starts with empty rate-limit buckets (the limiter is global)."""
    reset_all_rate_limits()
    yield


class PermissiveLoginGuard:
    """A login guard that never locks (for the MagicMock-based auth tests)."""

    def seconds_locked(self, email, now):
        return 0

    def record_failure(self, email, now):
        return None

    def record_success(self, email):
        return None


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
    application.dependency_overrides[get_auth_client] = lambda: supabase
    # The MagicMock client cannot store login attempts; these legacy auth tests
    # get a guard that never locks (the lockout itself runs on fake_client).
    try:
        from cantrack_api.login_guard import get_login_guard
    except ImportError:
        pass
    else:
        application.dependency_overrides[get_login_guard] = lambda: PermissiveLoginGuard()

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
def text_model() -> FakeTextModel:
    return FakeTextModel()


@pytest.fixture
def fake_client(fake, embedder, vision, fake_clock, text_model) -> TestClient:
    application = create_app()
    application.dependency_overrides[get_supabase] = lambda: fake
    application.dependency_overrides[get_auth_client] = lambda: fake
    application.dependency_overrides[get_embedder] = lambda: embedder
    application.dependency_overrides[get_vision] = lambda: vision
    application.dependency_overrides[get_text_model] = lambda: text_model
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

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

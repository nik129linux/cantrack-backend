"""Task 1: every response carries the API security headers."""

from .conftest import bearer


class TestSecurityHeaders:
    def test_success_carries_headers(self, fake_client):
        res = fake_client.get("/dogs", headers=bearer("token-a"))
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        assert res.headers["X-Frame-Options"] == "DENY"
        assert res.headers["Referrer-Policy"] == "no-referrer"
        assert res.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"

    def test_unauthorized_still_carries_headers(self, fake_client):
        res = fake_client.get("/dogs")
        assert res.status_code == 401
        assert res.headers["X-Content-Type-Options"] == "nosniff"
        assert res.headers["X-Frame-Options"] == "DENY"

    def test_bidding_carries_headers(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        res = fake_client.get("/bidding/requests/open", headers=bearer("token-w"))
        assert res.headers["Content-Security-Policy"] == "default-src 'none'; frame-ancestors 'none'"

    def test_cors_still_works_with_headers(self, fake_client):
        res = fake_client.get(
            "/dogs",
            headers={**bearer("token-a"), "Origin": "http://localhost:5173"},
        )
        assert res.headers.get("access-control-allow-origin") == "http://localhost:5173"
        assert res.headers["X-Content-Type-Options"] == "nosniff"

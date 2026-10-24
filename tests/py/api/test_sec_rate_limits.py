"""PR0 (f): rate limits that hold behind Render's proxy and per user.

Before: the limiter keyed on ``request.client.host``. Behind Render's proxy
that is the proxy for everyone, so one attacker spending the 5/min login budget
locked every user out, and the AI re-run and the graph endpoint had no limit.
"""

import hashlib

from starlette.requests import Request

from cantrack_api import main as main_module
from cantrack_api.app import rate_limit_key

from .conftest import bearer, make_user
from .test_checkouts import accepted_request, create_dog, post_checkout, save_profile

T10 = "2026-10-05T10:00:00+00:00"
BIDDING = {"dogId": "dog-a", "windowStart": T10, "durationHours": 1, "priceCop": 30000,
           "pickupLat": 6.2, "pickupLng": -75.5}
TOO_MANY = {"detail": "Too many requests. Try again in a minute."}


def make_request(headers=None, client=("203.0.113.9", 4000)):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": raw, "client": client,
                    "query_string": b""})


class TestKey:
    def test_authenticated_requests_are_keyed_by_token_hash(self):
        key = rate_limit_key(make_request({"Authorization": "Bearer abc.def"}))
        assert key == "user:" + hashlib.sha256(b"abc.def").hexdigest()[:32]

    def test_anonymous_requests_are_keyed_by_client_ip(self):
        assert rate_limit_key(make_request()) == "ip:203.0.113.9"

    def test_the_raw_token_never_appears_in_the_key(self):
        assert "abc.def" not in rate_limit_key(make_request({"Authorization": "Bearer abc.def"}))


class TestPerUserBuckets:
    def test_one_flooding_owner_does_not_block_another(self, fake_client, fake):
        fake.seed("dogs", id="dog-a", owner_id="owner-a", name="A")
        fake.seed("dogs", id="dog-b", owner_id="owner-b", name="B")
        for _ in range(10):
            fake_client.post("/bidding/requests", json=BIDDING, headers=bearer("token-a"))
        assert fake_client.post("/bidding/requests", json=BIDDING, headers=bearer("token-a")).status_code == 429
        res = fake_client.post("/bidding/requests", json={**BIDDING, "dogId": "dog-b"}, headers=bearer("token-b"))
        assert res.status_code == 201

    def test_429_is_json_with_retry_after(self, fake_client, fake):
        fake.seed("dogs", id="dog-a", owner_id="owner-a", name="A")
        for _ in range(10):
            fake_client.post("/bidding/requests", json=BIDDING, headers=bearer("token-a"))
        res = fake_client.post("/bidding/requests", json=BIDDING, headers=bearer("token-a"))
        assert res.status_code == 429
        assert res.json() == TOO_MANY
        assert int(res.headers["Retry-After"]) > 0


class TestNewLimits:
    def test_ai_note_rerun_is_5_per_minute(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        cid = post_checkout(fake_client, rid).json()["id"]
        codes = [fake_client.post(f"/checkouts/{cid}/ai-note", headers=bearer("token-w")).status_code
                 for _ in range(6)]
        assert codes[:5] == [200] * 5
        assert codes[5] == 429

    def test_shortest_path_is_20_per_minute(self, fake_client):
        body = {"edges": [{"from": "a", "to": "b", "weight": 1}], "start": "a", "end": "b"}
        codes = [fake_client.post("/routes/path", json=body, headers=bearer("token-w")).status_code
                 for _ in range(21)]
        assert codes[:20] == [200] * 20
        assert codes[20] == 429

    def test_role_pick_is_5_per_minute(self, fake_client, fake):
        fake.add_user("token-x", make_user("x", "x@example.com", "owner"))
        codes = [fake_client.post("/auth/role", json={"role": "owner"}, headers=bearer("token-x")).status_code
                 for _ in range(6)]
        assert codes[5] == 429


class TestProxyTrust:
    def test_trusted_proxies_come_from_the_environment(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(main_module, "validate_settings", lambda env: None)
        monkeypatch.setattr(main_module.uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.0/8")
        main_module.run()
        assert captured["proxy_headers"] is True
        assert captured["forwarded_allow_ips"] == "10.0.0.0/8"

    def test_default_trusts_only_localhost(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(main_module, "validate_settings", lambda env: None)
        monkeypatch.setattr(main_module.uvicorn, "run", lambda app, **kwargs: captured.update(kwargs))
        monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)
        main_module.run()
        assert captured["forwarded_allow_ips"] == "127.0.0.1"

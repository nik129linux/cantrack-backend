"""Raise the fare of an open request: same request, higher price, new window."""

from datetime import datetime, timedelta

import pytest

from .conftest import FIXED_NOW, bearer

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"


def seed_open(fake, status="open", price=15000):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    return fake.seed(
        "walk_requests", id="walk-1", owner_id="owner-a", walker_id=None, dog_id="dog-1",
        status=status, requested_time=T10, window_end=T11,
        deadline_at="2026-09-30T23:55:00+00:00", duration_hours=1.0,
        pickup_lat=1.21, pickup_lng=-77.28, price_cop=price,
        created_at="2026-09-30T23:45:00+00:00",
    )


def raise_fare(client, price, token="token-a"):
    return client.post("/bidding/requests/walk-1/raise", json={"priceCop": price}, headers=bearer(token))


class TestRaise:
    def test_raises_price_and_reopens_window(self, fake_client, fake):
        seed_open(fake)
        res = raise_fare(fake_client, 17000)
        assert res.status_code == 200
        body = res.json()
        assert body["id"] == "walk-1" and body["priceCop"] == 17000 and body["status"] == "open"
        assert datetime.fromisoformat(body["deadlineAt"]) == FIXED_NOW + timedelta(minutes=10)
        rows = fake.table("walk_requests").select("*").execute().data
        assert len(rows) == 1 and rows[0]["price_cop"] == 17000

    @pytest.mark.parametrize("price", [15000, 12000])
    def test_must_go_up(self, fake_client, fake, price):
        seed_open(fake)
        res = raise_fare(fake_client, price)
        assert res.status_code == 400
        assert res.json() == {"detail": "The new fare must be higher than the current one."}

    def test_cap(self, fake_client, fake):
        seed_open(fake)
        assert raise_fare(fake_client, 1_000_001).status_code == 400
        assert fake.table("walk_requests").select("*").execute().data[0]["price_cop"] == 15000

    def test_foreign_request_is_403(self, fake_client, fake):
        seed_open(fake)
        assert raise_fare(fake_client, 17000, token="token-b").status_code == 403

    @pytest.mark.parametrize("status", ["assigned", "cancelled", "expired"])
    def test_only_open(self, fake_client, fake, status):
        seed_open(fake, status=status)
        assert raise_fare(fake_client, 17000).status_code == 409

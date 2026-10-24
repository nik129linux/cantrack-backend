"""PR0 (b): business-rule holes a user can drive from Postman with their own token.

Each test names the exploit it closes. Every one of them worked against the API
before this PR (verified by running these tests red).
"""

import uuid
from datetime import timedelta

import pytest

from .conftest import FIXED_NOW, bearer

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"

SELF_BID = {"detail": "You cannot bid on your own walk request."}
ROUTE_DOG_FORBIDDEN = {"detail": "You can only route dogs from walks assigned to you."}
WALKERS_ONLY_ROUTES = {"detail": "Only walkers can manage routes."}
OWNERS_ONLY_DOGS = {"detail": "Only owners can manage dogs."}
WALKER_AT_PICKUP = {"detail": "The walker is at the pickup point. Contact them before marking a no-show."}


def seed_walk(fake, *, request_id="walk-1", owner_id="owner-a", walker_id=None, status="open",
              dog_id="dog-1"):
    if not [d for d in fake.tables.get("dogs", []) if d["id"] == dog_id]:
        fake.seed("dogs", id=dog_id, owner_id=owner_id, name="Firulais")
    return fake.seed(
        "walk_requests", id=request_id, owner_id=owner_id, walker_id=walker_id, dog_id=dog_id,
        status=status, requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


class TestSelfDealing:
    """Exploit: one person holding both roles bids on their own walk to farm ratings."""

    def test_walker_cannot_bid_on_a_walk_they_own(self, fake_client, fake):
        seed_walk(fake, owner_id="walker-w")
        fake.seed("walker_positions", walker_id="walker-w", lat=6.2, lng=-75.5,
                  updated_at="2026-10-01T00:00:00+00:00")
        res = fake_client.post("/bidding/requests/walk-1/offers",
                               json={"priceCop": 25000, "etaMin": 5}, headers=bearer("token-w"))
        assert res.status_code == 409
        assert res.json() == SELF_BID
        assert fake.tables.get("walk_offers", []) == []


class TestRouteDogOwnership:
    """Exploit: POST /routes with any dogId leaks the dog's name and runs face matching on it."""

    def _route(self, client, dog_id, token="token-w"):
        return client.post("/routes", json={"stops": [{"dogId": dog_id, "pickupTime": "2026-10-05T08:00:00Z"}]},
                           headers=bearer(token))

    def test_stranger_dog_is_refused_and_its_name_never_returned(self, fake_client, fake):
        stranger = fake.seed("dogs", id=str(uuid.uuid4()), owner_id="owner-b", name="SecretName")
        res = self._route(fake_client, stranger["id"])
        assert res.status_code == 403
        assert res.json() == ROUTE_DOG_FORBIDDEN
        assert "SecretName" not in res.text
        assert fake.tables.get("routes", []) == []

    @pytest.mark.parametrize("status", ["accepted", "assigned", "in_walk"])
    def test_dog_from_a_walk_assigned_to_the_walker_is_allowed(self, fake_client, fake, status):
        dog = fake.seed("dogs", id=str(uuid.uuid4()), owner_id="owner-a", name="Rex")
        seed_walk(fake, request_id=str(uuid.uuid4()), walker_id="walker-w", status=status, dog_id=dog["id"])
        assert self._route(fake_client, dog["id"]).status_code == 201

    @pytest.mark.parametrize("status", ["pending", "declined", "cancelled", "open", "done"])
    def test_dog_from_an_inactive_walk_is_refused(self, fake_client, fake, status):
        dog = fake.seed("dogs", id=str(uuid.uuid4()), owner_id="owner-a", name="Rex")
        seed_walk(fake, request_id=str(uuid.uuid4()), walker_id="walker-w", status=status, dog_id=dog["id"])
        assert self._route(fake_client, dog["id"]).status_code == 403

    def test_dog_assigned_to_another_walker_is_refused(self, fake_client, fake):
        dog = fake.seed("dogs", id=str(uuid.uuid4()), owner_id="owner-a", name="Rex")
        seed_walk(fake, request_id=str(uuid.uuid4()), walker_id="walker-other", status="accepted",
                  dog_id=dog["id"])
        assert self._route(fake_client, dog["id"]).status_code == 403

    def test_one_bad_stop_refuses_the_whole_route(self, fake_client, fake):
        mine = fake.seed("dogs", id=str(uuid.uuid4()), owner_id="owner-a", name="Rex")
        seed_walk(fake, request_id=str(uuid.uuid4()), walker_id="walker-w", status="accepted", dog_id=mine["id"])
        stranger = fake.seed("dogs", id=str(uuid.uuid4()), owner_id="owner-b", name="SecretName")
        res = fake_client.post("/routes", json={"stops": [
            {"dogId": mine["id"], "pickupTime": "2026-10-05T08:00:00Z"},
            {"dogId": stranger["id"], "pickupTime": "2026-10-05T09:00:00Z"},
        ]}, headers=bearer("token-w"))
        assert res.status_code == 403
        assert fake.tables.get("routes", []) == []


class TestRoleBoundaries:
    def test_owner_cannot_create_a_route(self, fake_client, fake):
        res = fake_client.post("/routes", json={"stops": [{"dogId": "d", "pickupTime": "2026-10-05T08:00:00Z"}]},
                               headers=bearer("token-a"))
        assert res.status_code == 403
        assert res.json() == WALKERS_ONLY_ROUTES

    def test_walker_cannot_create_a_dog(self, fake_client, fake):
        res = fake_client.post("/dogs", json={"name": "Fake"}, headers=bearer("token-w"))
        assert res.status_code == 403
        assert res.json() == OWNERS_ONLY_DOGS
        assert fake.tables.get("dogs", []) == []


class TestNoShowCannotBeFaked:
    """Exploit: the owner marks a no-show on a walker standing at the door, burning their tokens."""

    def _late(self, fake_clock):
        fake_clock.now = FIXED_NOW + timedelta(days=4, hours=10, minutes=20)

    def test_walker_at_the_pickup_point_blocks_the_no_show(self, fake_client, fake, fake_clock):
        seed_walk(fake, walker_id="walker-w", status="assigned")
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=3,
                  period_start="2026-10-01T00:00:00+00:00")
        # ~50 m from the pickup, reported after the window opened.
        fake.seed("walker_positions", walker_id="walker-w", lat=6.2004, lng=-75.5,
                  updated_at="2026-10-05T10:05:00+00:00")
        self._late(fake_clock)
        res = fake_client.post("/bidding/requests/walk-1/no-show", headers=bearer("token-a"))
        assert res.status_code == 409
        assert res.json() == WALKER_AT_PICKUP
        assert fake.tables["walker_cancel_buckets"][0]["remaining"] == 3

    def test_stale_position_does_not_block(self, fake_client, fake, fake_clock):
        seed_walk(fake, walker_id="walker-w", status="assigned")
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=3,
                  period_start="2026-10-01T00:00:00+00:00")
        # At the pickup, but yesterday: says nothing about today.
        fake.seed("walker_positions", walker_id="walker-w", lat=6.2, lng=-75.5,
                  updated_at="2026-10-04T10:05:00+00:00")
        self._late(fake_clock)
        res = fake_client.post("/bidding/requests/walk-1/no-show", headers=bearer("token-a"))
        assert res.status_code == 200

    def test_far_away_walker_can_be_marked(self, fake_client, fake, fake_clock):
        seed_walk(fake, walker_id="walker-w", status="assigned")
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=3,
                  period_start="2026-10-01T00:00:00+00:00")
        fake.seed("walker_positions", walker_id="walker-w", lat=6.25, lng=-75.5,
                  updated_at="2026-10-05T10:05:00+00:00")
        self._late(fake_clock)
        res = fake_client.post("/bidding/requests/walk-1/no-show", headers=bearer("token-a"))
        assert res.status_code == 200
        assert res.json()["status"] == "open"

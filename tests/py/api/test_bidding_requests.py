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

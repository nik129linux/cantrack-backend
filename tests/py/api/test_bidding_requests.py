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

    def test_fare_in_hundreds_is_accepted(self, fake_client, fake):
        seed_dog(fake)
        assert post_open(fake_client, priceCop=15500).status_code == 201

    def test_fare_not_a_multiple_of_100_is_400(self, fake_client, fake):
        seed_dog(fake)
        assert post_open(fake_client, priceCop=15050).status_code == 400

    # One hour at the default 330 COP/min is recommended at $20.000, so the ceiling is $60.000.
    def test_fare_at_three_times_the_recommended_price_is_accepted(self, fake_client, fake):
        seed_dog(fake)
        assert post_open(fake_client, priceCop=60000).status_code == 201

    def test_fare_above_three_times_the_recommended_price_is_400(self, fake_client, fake):
        seed_dog(fake)
        res = post_open(fake_client, priceCop=60100)
        assert res.status_code == 400
        assert "three times the recommended price" in res.json()["detail"]

    def test_the_ceiling_follows_the_market_price(self, fake_client, fake):
        seed_dog(fake)
        # Five agreed walks nearby at 600 COP/min: recommended $36.000 an hour, ceiling $108.000.
        for i in range(5):
            fake.seed("walk_requests", id=f"old-{i}", owner_id="owner-z", walker_id="walker-z",
                      status="done", pickup_lat=6.2, pickup_lng=-75.5, duration_hours=1.0,
                      price_cop=36000, created_at="2026-09-20T00:00:00+00:00")
            fake.seed("walk_offers", id=f"off-{i}", walk_request_id=f"old-{i}", walker_id="walker-z",
                      price_cop=36000, eta_min=5.0, distance_km=1.0, created_at="2026-09-20T00:00:00+00:00")
        assert post_open(fake_client, priceCop=108000).status_code == 201
        assert post_open(fake_client, priceCop=108100).status_code == 400

    def test_walker_cannot_post(self, fake_client, fake):
        seed_dog(fake)
        assert post_open(fake_client, token="token-w").status_code == 400

    def test_unknown_dog_is_404(self, fake_client, fake):
        assert post_open(fake_client).status_code == 404

    def test_payment_method_is_stored_and_echoed(self, fake_client, fake):
        seed_dog(fake)
        res = post_open(fake_client, paymentMethod="nequi")
        assert res.status_code == 201
        assert res.json()["paymentMethod"] == "nequi"
        assert fake.tables["walk_requests"][0]["payment_method"] == "nequi"

    def test_without_payment_method_it_is_null(self, fake_client, fake):
        seed_dog(fake)
        assert post_open(fake_client).json()["paymentMethod"] is None

    def test_unknown_payment_method_is_422(self, fake_client, fake):
        seed_dog(fake)
        assert post_open(fake_client, paymentMethod="bitcoin").status_code in (400, 422)

    def test_past_window_is_400(self, fake_client, fake):
        seed_dog(fake)
        assert post_open(fake_client, windowStart="2026-09-01T10:00:00+00:00").status_code == 400


class TestPickupLabel:
    def test_the_label_is_stored_and_returned(self, fake_client, fake):
        seed_dog(fake)
        res = post_open(fake_client, pickupLabel="Carrera 27 15-30, Pasto")
        assert res.status_code == 201
        assert res.json()["pickupLabel"] == "Carrera 27 15-30, Pasto"
        assert fake.tables["walk_requests"][0]["pickup_label"] == "Carrera 27 15-30, Pasto"

    def test_it_is_optional_and_null_when_omitted(self, fake_client, fake):
        seed_dog(fake)
        res = post_open(fake_client)
        assert res.status_code == 201
        assert res.json()["pickupLabel"] is None

    def test_a_blank_label_becomes_null(self, fake_client, fake):
        seed_dog(fake)
        assert post_open(fake_client, pickupLabel="   ").json()["pickupLabel"] is None

    def test_the_label_is_trimmed(self, fake_client, fake):
        seed_dog(fake)
        assert post_open(fake_client, pickupLabel="  Plaza de Nariño ").json()["pickupLabel"] == "Plaza de Nariño"

    def test_120_characters_fit_and_121_are_400(self, fake_client, fake):
        seed_dog(fake)
        assert post_open(fake_client, pickupLabel="x" * 120).status_code == 201
        assert post_open(fake_client, pickupLabel="x" * 121).status_code == 400

    def test_a_non_string_label_is_400(self, fake_client, fake):
        seed_dog(fake)
        assert post_open(fake_client, pickupLabel=123).status_code == 400

    def test_the_walker_inbox_card_carries_the_label(self, fake_client, fake):
        seed_dog(fake)
        row = seed_open(fake)
        row["pickup_label"] = "Plaza de Nariño"
        fake.seed("walker_positions", walker_id="walker-w", lat=6.201, lng=-75.5,
                 updated_at="2026-10-01T00:00:00+00:00")
        card = fake_client.get("/bidding/requests/open", headers=bearer("token-w")).json()[0]
        assert card["pickupLabel"] == "Plaza de Nariño"

    def test_an_old_row_without_a_label_shows_null(self, fake_client, fake):
        seed_dog(fake)
        seed_open(fake)
        fake.seed("walker_positions", walker_id="walker-w", lat=6.201, lng=-75.5,
                 updated_at="2026-10-01T00:00:00+00:00")
        card = fake_client.get("/bidding/requests/open", headers=bearer("token-w")).json()[0]
        assert card["pickupLabel"] is None


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

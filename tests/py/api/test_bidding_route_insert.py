"""Task 9: a busy winner's new dog joins the active route (FR-25, FR-06, FR-08)."""

from .conftest import bearer, make_user

T10 = "2026-10-05T10:00:00+00:00"
T1030 = "2026-10-05T10:30:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"


class TestRouteInsert:
    def test_busy_winner_gets_a_new_stop_and_next_pickup(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        fake.seed("dogs", id="dog-2", owner_id="owner-a", name="Rex")
        busy = fake.seed(
            "walk_requests", owner_id="owner-a", walker_id="walker-w", dog_id="dog-1",
            status="assigned", requested_time=T10, window_end=T11,
            deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
            pickup_lat=6.201, pickup_lng=-75.5, price_cop=30000,
            created_at="2026-10-01T00:00:00+00:00",
        )
        fake.seed("walk_route_stops", walk_request_id=busy["id"], dog_id="dog-1",
                 stop_key="home", pickup_time=T10, position=0)
        new_walk = fake.seed(
            "walk_requests", id="walk-new", owner_id="owner-a", walker_id=None, dog_id="dog-2",
            status="open", requested_time=T1030, window_end=T11,
            deadline_at="2026-10-01T00:10:00+00:00", duration_hours=0.5,
            pickup_lat=6.202, pickup_lng=-75.5, price_cop=30000,
            created_at="2026-10-01T00:00:00+00:00",
        )
        fake.seed("walker_positions", walker_id="walker-w", lat=6.201, lng=-75.5,
                 updated_at="2026-10-01T00:00:00+00:00")
        fake.seed("dog_pack_links", dog_a="dog-1", dog_b="dog-2")
        fake.seed("walk_graph_edges", from_stop="home", to_stop="pickup:walk-new", minutes=5.0)
        offered = fake_client.post("/bidding/requests/walk-new/offers",
                                   json={"priceCop": 30000, "etaMin": 10.0},
                                   headers=bearer("token-w"))
        assert offered.status_code == 201
        offer_id = fake_client.get("/bidding/requests/walk-new/offers",
                                   headers=bearer("token-a")).json()[0]["offerId"]
        res = fake_client.post("/bidding/requests/walk-new/pick", json={"offerId": offer_id},
                               headers=bearer("token-a"))
        assert res.status_code == 200
        assert res.json()["routeStops"][-1]["dogId"] == "dog-2"
        assert res.json()["nextPickup"]["pickupTime"] == T10
        stored = fake.table("walk_route_stops").select("*").eq("walk_request_id", busy["id"]).execute().data
        assert sorted(row["dog_id"] for row in stored) == ["dog-1", "dog-2"]

    def test_free_winner_gets_no_route_change(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        fake.seed("walk_requests", id="walk-new", owner_id="owner-a", walker_id=None, dog_id="dog-1",
                 status="open", requested_time=T1030, window_end=T11,
                 deadline_at="2026-10-01T00:10:00+00:00", duration_hours=0.5,
                 pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
                 created_at="2026-10-01T00:00:00+00:00")
        fake.seed("walker_positions", walker_id="walker-w", lat=6.201, lng=-75.5,
                 updated_at="2026-10-01T00:00:00+00:00")
        fake_client.post("/bidding/requests/walk-new/offers", json={"priceCop": 30000, "etaMin": 10.0},
                         headers=bearer("token-w"))
        offer_id = fake_client.get("/bidding/requests/walk-new/offers",
                                   headers=bearer("token-a")).json()[0]["offerId"]
        res = fake_client.post("/bidding/requests/walk-new/pick", json={"offerId": offer_id},
                               headers=bearer("token-a"))
        assert res.json()["routeStops"] == []
        assert "nextPickup" not in res.json()

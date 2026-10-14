"""Task 1: the bidding tables accept the shapes routers/bidding.py uses."""


class TestBiddingSchema:
    def test_offer_round_trip(self, fake):
        walk = fake.seed(
            "walk_requests", owner_id="owner-a", walker_id=None, dog_id="dog-1",
            status="open", requested_time="2026-10-05T10:00:00+00:00",
            window_end="2026-10-05T11:00:00+00:00",
            deadline_at="2026-10-01T00:10:00+00:00",
            duration_hours=1.0, pickup_lat=6.2, pickup_lng=-75.5,
            price_cop=30000, created_at="2026-10-01T00:00:00+00:00",
        )
        offer = fake.seed(
            "walk_offers", walk_request_id=walk["id"], walker_id="walker-w",
            price_cop=28000, eta_min=12.0, distance_km=1.2,
            created_at="2026-10-01T00:01:00+00:00",
        )
        rows = fake.table("walk_offers").select("*").eq("walk_request_id", walk["id"]).execute().data
        assert [row["id"] for row in rows] == [offer["id"]]

    def test_compare_and_set_shape(self, fake):
        walk = fake.seed(
            "walk_requests", owner_id="owner-a", walker_id=None, dog_id="dog-1",
            status="open", requested_time="2026-10-05T10:00:00+00:00",
            created_at="2026-10-01T00:00:00+00:00",
        )
        won = (
            fake.table("walk_requests")
            .update({"status": "assigned"})
            .eq("id", walk["id"])
            .eq("status", "open")
            .execute()
            .data
        )
        assert len(won) == 1
        lost = (
            fake.table("walk_requests")
            .update({"status": "assigned"})
            .eq("id", walk["id"])
            .eq("status", "open")
            .execute()
            .data
        )
        assert lost == []

    def test_aux_tables_round_trip(self, fake):
        fake.seed("walker_positions", walker_id="walker-w", lat=6.2, lng=-75.5,
                 updated_at="2026-10-01T00:00:00+00:00")
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=3,
                 period_start="2026-10-01T00:00:00+00:00")
        fake.seed("walk_ratings", walk_request_id="walk-1", walker_id="walker-w",
                 rating=5.0, created_at="2026-10-01T00:00:00+00:00")
        fake.seed("walk_timeline", walk_request_id="walk-1", kind="check_in",
                 at="2026-10-05T10:00:00+00:00", payload={})
        fake.seed("walk_gps_points", walk_request_id="walk-1", at_epoch=1000.0,
                 lat=6.2, lng=-75.5)
        assert fake.table("walker_positions").select("*").execute().data[0]["walker_id"] == "walker-w"
        assert fake.table("walk_gps_points").select("*").execute().data[0]["at_epoch"] == 1000.0

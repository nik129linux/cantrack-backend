"""Task 10: ratings after DONE feed the window average and the all-time mean."""

from .conftest import bearer

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"


def seed_done(fake, request_id="walk-1", walker_id="walker-w"):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    return fake.seed(
        "walk_requests", id=request_id, owner_id="owner-a", walker_id=walker_id, dog_id="dog-1",
        status="done", requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


class TestRatings:
    def test_rating_done_walk_updates_reputation(self, fake_client, fake):
        seed_done(fake)
        res = fake_client.post("/bidding/requests/walk-1/ratings", json={"rating": 5.0},
                               headers=bearer("token-a"))
        assert res.status_code == 201
        assert res.json()["allTimeRating"] == 5.0
        rows = fake.table("walk_ratings").select("*").eq("walker_id", "walker-w").execute().data
        assert [row["rating"] for row in rows] == [5.0]

    def test_second_rating_same_walk_is_409(self, fake_client, fake):
        seed_done(fake)
        fake_client.post("/bidding/requests/walk-1/ratings", json={"rating": 5.0}, headers=bearer("token-a"))
        res = fake_client.post("/bidding/requests/walk-1/ratings", json={"rating": 4.0}, headers=bearer("token-a"))
        assert res.status_code == 409

    def test_rating_unfinished_walk_is_409(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        fake.seed("walk_requests", id="walk-1", owner_id="owner-a", walker_id="walker-w", dog_id="dog-1",
                 status="in_walk", requested_time=T10, window_end=T11,
                 deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
                 pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
                 created_at="2026-10-01T00:00:00+00:00")
        res = fake_client.post("/bidding/requests/walk-1/ratings", json={"rating": 5.0},
                               headers=bearer("token-a"))
        assert res.status_code == 409

    def test_out_of_range_rating_is_400(self, fake_client, fake):
        seed_done(fake)
        assert fake_client.post("/bidding/requests/walk-1/ratings", json={"rating": 9.0},
                                headers=bearer("token-a")).status_code == 400

    def test_window_holds_only_last_25(self, fake_client, fake):
        seed_done(fake)
        for index in range(25):
            fake.seed("walk_ratings", walk_request_id=f"old-{index}", walker_id="walker-w",
                     rating=1.0, created_at=f"2026-09-{index + 1:02d}T00:00:00+00:00")
        fake_client.post("/bidding/requests/walk-1/ratings", json={"rating": 5.0}, headers=bearer("token-a"))
        rows = fake.table("walk_ratings").select("*").eq("walker_id", "walker-w").execute().data
        assert len(rows) == 26
        recent = sorted(rows, key=lambda row: row["created_at"], reverse=True)[:25]
        assert sum(row["rating"] for row in recent) / 25 == (24 * 1.0 + 5.0) / 25

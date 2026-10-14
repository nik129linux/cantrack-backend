"""Task 8: cancels cost tokens and reopen; check-in/out move the walk."""

from datetime import timedelta

from .conftest import FIXED_NOW, bearer

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"


def seed_assigned(fake, request_id="walk-1", walker_id="walker-w"):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    return fake.seed(
        "walk_requests", id=request_id, owner_id="owner-a", walker_id=walker_id, dog_id="dog-1",
        status="assigned", requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


class TestLifecycle:
    def test_walker_cancel_reopens_and_spends_token(self, fake_client, fake):
        seed_assigned(fake)
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=3,
                 period_start="2026-10-01T00:00:00+00:00")
        fake.seed("walk_offers", walk_request_id="walk-1", walker_id="walker-w",
                 price_cop=30000, eta_min=10.0, distance_km=1.0,
                 created_at="2026-10-01T00:00:00+00:00")
        res = fake_client.post("/bidding/requests/walk-1/walker-cancel", headers=bearer("token-w"))
        assert res.json()["status"] == "open"
        buckets = fake.table("walker_cancel_buckets").select("*").eq("walker_id", "walker-w").execute().data
        assert buckets[0]["remaining"] == 2
        offers = fake.table("walk_offers").select("*").eq("walk_request_id", "walk-1").execute().data
        assert len(offers) == 1

    def test_walker_cancel_with_empty_bucket_is_403(self, fake_client, fake):
        seed_assigned(fake)
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=0,
                 period_start="2026-10-01T00:00:00+00:00")
        res = fake_client.post("/bidding/requests/walk-1/walker-cancel", headers=bearer("token-w"))
        assert res.status_code == 403
        assert res.json() == {"detail": "No cancellation tokens left."}

    def test_owner_cancel_assigned_is_cancelled(self, fake_client, fake):
        seed_assigned(fake)
        res = fake_client.post("/bidding/requests/walk-1/owner-cancel", headers=bearer("token-a"))
        assert res.json()["status"] == "cancelled"

    def test_no_show_after_grace_reopens(self, fake_client, fake, fake_clock):
        seed_assigned(fake)
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=3,
                 period_start="2026-10-01T00:00:00+00:00")
        fake_clock.now = FIXED_NOW + timedelta(days=4, hours=10, minutes=20)
        res = fake_client.post("/bidding/requests/walk-1/no-show", headers=bearer("token-a"))
        assert res.json()["status"] == "open"

    def test_check_in_then_check_out_finishes(self, fake_client, fake):
        seed_assigned(fake)
        assert fake_client.post("/bidding/requests/walk-1/check-in", headers=bearer("token-w")).json()["status"] == "in_walk"
        done = fake_client.post("/bidding/requests/walk-1/check-out", headers=bearer("token-w"))
        assert done.json()["status"] == "done"
        kinds = sorted(row["kind"] for row in
                       fake.table("walk_timeline").select("*").eq("walk_request_id", "walk-1").execute().data)
        assert kinds == ["check_in", "check_out"]

    def test_check_out_from_assigned_is_409(self, fake_client, fake):
        seed_assigned(fake)
        res = fake_client.post("/bidding/requests/walk-1/check-out", headers=bearer("token-w"))
        assert res.status_code == 409

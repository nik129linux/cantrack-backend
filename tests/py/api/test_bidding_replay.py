"""Task 11: owners scrub a finished walk forward/back with a GPS jump."""

from .conftest import bearer

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"
T12 = "2026-10-05T12:00:00+00:00"


def seed_done_with_trace(fake, request_id="walk-1"):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    fake.seed("walk_requests", id=request_id, owner_id="owner-a", walker_id="walker-w", dog_id="dog-1",
             status="done", requested_time=T10, window_end=T11,
             deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
             pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
             created_at="2026-10-01T00:00:00+00:00")
    for kind, at in (("check_in", T10), ("photo", T11), ("check_out", T12)):
        fake.seed("walk_timeline", walk_request_id=request_id, kind=kind, at=at, payload={})
    for epoch, lat in ((1000.0, 6.2), (2000.0, 6.3), (3000.0, 6.4)):
        fake.seed("walk_gps_points", walk_request_id=request_id, at_epoch=epoch, lat=lat, lng=-75.5)


class TestReplayEndpoint:
    def test_forward_and_backward(self, fake_client, fake):
        seed_done_with_trace(fake)
        forward = fake_client.get("/bidding/requests/walk-1/replay?direction=forward&start=0&steps=2",
                                  headers=bearer("token-a")).json()
        assert [event["kind"] for event in forward["events"]] == ["check_in", "photo"]
        backward = fake_client.get("/bidding/requests/walk-1/replay?direction=backward&start=2&steps=2",
                                   headers=bearer("token-a")).json()
        assert [event["kind"] for event in backward["events"]] == ["check_out", "photo"]

    def test_gps_jump_returns_earlier_point(self, fake_client, fake):
        seed_done_with_trace(fake)
        res = fake_client.get("/bidding/requests/walk-1/replay?atEpoch=2500.0", headers=bearer("token-a"))
        assert res.json()["gps"] == {"atEpoch": 2000.0, "lat": 6.3, "lng": -75.5}

    def test_unfinished_walk_is_409(self, fake_client, fake):
        seed_done_with_trace(fake, request_id="walk-2")
        fake.table("walk_requests").update({"status": "in_walk"}).eq("id", "walk-2").execute()
        res = fake_client.get("/bidding/requests/walk-2/replay", headers=bearer("token-a"))
        assert res.status_code == 409

    def test_wrong_owner_is_403(self, fake_client, fake):
        seed_done_with_trace(fake)
        res = fake_client.get("/bidding/requests/walk-1/replay", headers=bearer("token-b"))
        assert res.status_code == 403

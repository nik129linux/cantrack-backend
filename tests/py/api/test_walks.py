"""PR3 Mis paseos: the owner's walk lists, the walk detail, live tracking, GPS ingest, repeat.

The detail returns ``allowedActions`` so the UI only draws buttons that will
work; live tracking only exists while a walk is in progress and only for its
two parties; GPS can only be written by the assigned walker, in order, inside
the walk.
"""

from datetime import timedelta

import pytest

from .conftest import FIXED_NOW, bearer, make_user

T10 = "2026-10-05T10:00:00+00:00"
EPOCH_T10 = 1791194400.0  # 2026-10-05T10:00:00Z
NOT_FOUND = {"detail": "Walk not found."}
NOT_IN_PROGRESS = {"detail": "This walk is not in progress."}
GPS_ONLY_DURING = {"detail": "GPS is only accepted during a walk."}
GPS_ORDER = {"detail": "GPS points must be in order and within the walk."}


def seed_walk(fake, walk_id, status, requested_time=T10, owner_id="owner-a", walker_id="walker-w",
              dog_id="dog-luna", price=20000, **extra):
    return fake.seed(
        "walk_requests", id=walk_id, owner_id=owner_id, walker_id=walker_id, dog_id=dog_id, status=status,
        requested_time=requested_time, window_end=requested_time, deadline_at="2026-10-01T00:10:00+00:00",
        duration_hours=1.0, pickup_lat=1.2136, pickup_lng=-77.2811, price_cop=price,
        created_at="2026-09-30T00:00:00+00:00", **extra,
    )


@pytest.fixture
def base(fake):
    fake.add_user("token-w2", make_user("walker-w2", "w2@example.com", "walker"))
    fake.seed("dogs", id="dog-luna", owner_id="owner-a", name="Luna")
    fake.seed("dogs", id="dog-b", owner_id="owner-b", name="Theirs")
    fake.seed("walker_profiles", walker_id="walker-w", display_name="Mateo", bio=None,
              service_area=None, price_per_walk=15000)
    return fake


class TestLists:
    def tab(self, client, tab, token="token-a", owner="owner-a", **params):
        return client.get(f"/owners/{owner}/walks", params={"tab": tab, **params}, headers=bearer(token))

    def test_tabs_split_by_state(self, fake_client, base):
        seed_walk(base, "up", "assigned")
        seed_walk(base, "live", "in_walk")
        seed_walk(base, "done", "done")
        seed_walk(base, "gone", "cancelled", walker_id=None)
        seed_walk(base, "exp", "expired", walker_id=None)
        seed_walk(base, "open", "open", walker_id=None)  # open requests belong to Explorar
        assert [i["id"] for i in self.tab(fake_client, "upcoming").json()["items"]] == ["up"]
        assert [i["id"] for i in self.tab(fake_client, "active").json()["items"]] == ["live"]
        assert {i["id"] for i in self.tab(fake_client, "past").json()["items"]} == {"done", "gone", "exp"}

    def test_upcoming_soonest_first_and_past_newest_first(self, fake_client, base):
        seed_walk(base, "u2", "assigned", "2026-10-06T10:00:00+00:00")
        seed_walk(base, "u1", "assigned", "2026-10-05T10:00:00+00:00")
        seed_walk(base, "p1", "done", "2026-09-20T10:00:00+00:00")
        seed_walk(base, "p2", "done", "2026-09-25T10:00:00+00:00")
        assert [i["id"] for i in self.tab(fake_client, "upcoming").json()["items"]] == ["u1", "u2"]
        assert [i["id"] for i in self.tab(fake_client, "past").json()["items"]] == ["p2", "p1"]

    def test_item_shows_the_agreed_price_and_whether_it_can_be_rated(self, fake_client, base):
        seed_walk(base, "d1", "done", price=30000)
        base.seed("walk_offers", walk_request_id="d1", walker_id="walker-w", price_cop=18000, eta_min=5,
                  distance_km=1.0, created_at="2026-09-30T00:00:00+00:00")
        item = self.tab(fake_client, "past").json()["items"][0]
        assert item == {"id": "d1", "status": "done", "dogId": "dog-luna", "dogName": "Luna",
                        "walkerId": "walker-w", "walkerName": "Mateo", "requestedTime": T10,
                        "durationHours": 1.0, "priceCop": 18000, "canRate": True, "hasReport": False}
        base.seed("walk_ratings", walk_request_id="d1", walker_id="walker-w", rating=5,
                  created_at="2026-10-01T00:00:00+00:00")
        assert self.tab(fake_client, "past").json()["items"][0]["canRate"] is False

    def test_pagination(self, fake_client, base):
        for day in range(1, 6):
            seed_walk(base, f"p{day}", "done", f"2026-09-0{day}T10:00:00+00:00")
        first = self.tab(fake_client, "past", limit=3).json()
        second = self.tab(fake_client, "past", limit=3, cursor=first["nextCursor"]).json()
        assert [i["id"] for i in first["items"] + second["items"]] == ["p5", "p4", "p3", "p2", "p1"]
        assert second["nextCursor"] is None

    @pytest.mark.parametrize("params", [{"tab": "weird"}, {"tab": "past", "limit": 0}, {"tab": "past", "cursor": "%%%"}])
    def test_bad_params_are_400(self, fake_client, base, params):
        res = fake_client.get("/owners/owner-a/walks", params=params, headers=bearer("token-a"))
        assert res.status_code == 400

    def test_only_my_walks(self, fake_client, base):
        seed_walk(base, "theirs", "done", owner_id="owner-b", dog_id="dog-b")
        assert self.tab(fake_client, "past").json()["items"] == []
        assert self.tab(fake_client, "past", token="token-b").status_code == 403


class TestDetail:
    def detail(self, client, walk_id="w1", token="token-a"):
        return client.get(f"/bidding/requests/{walk_id}", headers=bearer(token))

    def test_owner_detail(self, fake_client, base):
        seed_walk(base, "w1", "done")
        base.seed("walk_offers", walk_request_id="w1", walker_id="walker-w", price_cop=18000, eta_min=5,
                  distance_km=1.0, created_at="2026-09-30T00:00:00+00:00")
        base.seed("walk_timeline", walk_request_id="w1", kind="check_out", at="2026-10-05T11:00:00+00:00", payload={})
        base.seed("walk_timeline", walk_request_id="w1", kind="check_in", at="2026-10-05T10:00:00+00:00", payload={})
        body = self.detail(fake_client).json()
        assert body["dog"] == {"id": "dog-luna", "name": "Luna"}
        assert body["walker"] == {"id": "walker-w", "name": "Mateo", "allTimeRating": None, "ratingCount": 0}
        assert body["priceCop"] == 18000
        assert body["offerCount"] == 1
        assert [event["kind"] for event in body["timeline"]] == ["check_in", "check_out"]
        assert body["rating"] is None
        assert body["report"] is None
        assert body["allowedActions"] == ["replay", "repeat", "rate"]

    @pytest.mark.parametrize("status,actions", [
        ("open", ["pick", "cancel"]),
        ("assigned", ["cancel"]),
        ("in_walk", ["track"]),
        ("cancelled", ["repeat"]),
        ("expired", ["repeat"]),
    ])
    def test_owner_actions_follow_the_state(self, fake_client, base, status, actions):
        seed_walk(base, "w1", status, walker_id=None if status in ("open", "cancelled", "expired") else "walker-w")
        assert self.detail(fake_client).json()["allowedActions"] == actions

    def test_no_show_appears_after_the_grace_period(self, fake_client, base, fake_clock):
        seed_walk(base, "w1", "assigned")
        fake_clock.now = FIXED_NOW + timedelta(days=4, hours=10, minutes=16)
        assert self.detail(fake_client).json()["allowedActions"] == ["cancel", "no_show"]

    def test_rated_walk_no_longer_offers_rate(self, fake_client, base):
        seed_walk(base, "w1", "done")
        base.seed("walk_ratings", walk_request_id="w1", walker_id="walker-w", rating=4,
                  created_at="2026-10-01T00:00:00+00:00")
        body = self.detail(fake_client).json()
        assert body["rating"] == 4
        assert "rate" not in body["allowedActions"]

    @pytest.mark.parametrize("status,actions", [
        ("assigned", ["check_in", "cancel"]), ("in_walk", ["check_out", "send_gps"]), ("done", []),
    ])
    def test_assigned_walker_sees_walker_actions(self, fake_client, base, status, actions):
        seed_walk(base, "w1", status)
        assert self.detail(fake_client, token="token-w").json()["allowedActions"] == actions

    @pytest.mark.parametrize("token", ["token-b", "token-w2"])
    def test_anyone_else_gets_404(self, fake_client, base, token):
        seed_walk(base, "w1", "done")
        res = self.detail(fake_client, token=token)
        assert res.status_code == 404
        assert res.json() == NOT_FOUND

    def test_unknown_walk_is_the_same_404(self, fake_client, base):
        assert self.detail(fake_client, walk_id="nope").json() == NOT_FOUND


class TestGps:
    def send(self, client, points, token="token-w", walk_id="w1"):
        return client.post(f"/bidding/requests/{walk_id}/gps", json={"points": points}, headers=bearer(token))

    @pytest.fixture
    def live(self, base, fake_clock):
        seed_walk(base, "w1", "in_walk")
        base.seed("walk_timeline", walk_request_id="w1", kind="check_in", at=T10, payload={})
        fake_clock.now = FIXED_NOW + timedelta(days=4, hours=10, minutes=10)
        return base

    def test_assigned_walker_stores_points(self, fake_client, live):
        points = [{"lat": 1.21, "lng": -77.28, "atEpoch": EPOCH_T10 + 60},
                  {"lat": 1.22, "lng": -77.28, "atEpoch": EPOCH_T10 + 120}]
        res = self.send(fake_client, points)
        assert res.status_code == 200
        assert res.json() == {"stored": 2}
        assert len(live.tables["walk_gps_points"]) == 2

    @pytest.mark.parametrize("token,code", [("token-w2", 403), ("token-a", 403)])
    def test_only_the_assigned_walker(self, fake_client, live, token, code):
        res = self.send(fake_client, [{"lat": 1.21, "lng": -77.28, "atEpoch": EPOCH_T10 + 60}], token=token)
        assert res.status_code == code
        assert live.tables.get("walk_gps_points", []) == []

    def test_only_during_the_walk(self, fake_client, base):
        seed_walk(base, "w1", "assigned")
        res = self.send(fake_client, [{"lat": 1.21, "lng": -77.28, "atEpoch": EPOCH_T10 + 60}])
        assert res.status_code == 409
        assert res.json() == GPS_ONLY_DURING

    @pytest.mark.parametrize("points", [
        [{"lat": 1.2, "lng": -77.2, "atEpoch": EPOCH_T10 - 60}],                       # before check-in
        [{"lat": 1.2, "lng": -77.2, "atEpoch": EPOCH_T10 + 3600}],                     # in the future
        [{"lat": 1.2, "lng": -77.2, "atEpoch": EPOCH_T10 + 120},
         {"lat": 1.2, "lng": -77.2, "atEpoch": EPOCH_T10 + 60}],                       # out of order
    ])
    def test_points_must_be_ordered_and_inside_the_walk(self, fake_client, live, points):
        res = self.send(fake_client, points)
        assert res.status_code == 400
        assert res.json() == GPS_ORDER

    def test_points_cannot_rewrite_history(self, fake_client, live):
        live.seed("walk_gps_points", walk_request_id="w1", at_epoch=EPOCH_T10 + 300, lat=1.2, lng=-77.2)
        res = self.send(fake_client, [{"lat": 1.2, "lng": -77.2, "atEpoch": EPOCH_T10 + 200}])
        assert res.status_code == 400

    @pytest.mark.parametrize("points", [[], [{"lat": 1.2, "lng": -77.2, "atEpoch": EPOCH_T10 + i} for i in range(1, 22)],
                                        [{"lat": 91, "lng": 0, "atEpoch": EPOCH_T10 + 60}]])
    def test_batch_shape(self, fake_client, live, points):
        assert self.send(fake_client, points).status_code == 400

    def test_six_batches_a_minute(self, fake_client, live):
        codes = [self.send(fake_client, [{"lat": 1.2, "lng": -77.2, "atEpoch": EPOCH_T10 + 10 + i}]).status_code
                 for i in range(7)]
        assert codes[6] == 429


class TestLive:
    def live(self, client, token="token-a"):
        return client.get("/bidding/requests/w1/live", headers=bearer(token))

    def test_owner_sees_the_latest_point(self, fake_client, base, fake_clock):
        seed_walk(base, "w1", "in_walk")
        base.seed("walk_timeline", walk_request_id="w1", kind="check_in", at=T10, payload={})
        base.seed("walk_gps_points", walk_request_id="w1", at_epoch=EPOCH_T10 + 60, lat=1.21, lng=-77.28)
        base.seed("walk_gps_points", walk_request_id="w1", at_epoch=EPOCH_T10 + 600, lat=1.23, lng=-77.29)
        fake_clock.now = FIXED_NOW + timedelta(days=4, hours=10, minutes=12, seconds=30)
        assert self.live(fake_client).json() == {
            "position": {"lat": 1.23, "lng": -77.29, "atEpoch": EPOCH_T10 + 600}, "minutesElapsed": 12,
        }

    def test_not_in_progress_is_409(self, fake_client, base):
        seed_walk(base, "w1", "done")
        res = self.live(fake_client)
        assert res.status_code == 409
        assert res.json() == NOT_IN_PROGRESS

    def test_strangers_get_404(self, fake_client, base):
        seed_walk(base, "w1", "in_walk")
        assert self.live(fake_client, token="token-b").status_code == 404


class TestRepeat:
    def repeat(self, client, window="2026-10-08T10:00:00+00:00", token="token-a", walk_id="w1"):
        return client.post(f"/bidding/requests/{walk_id}/repeat", json={"windowStart": window}, headers=bearer(token))

    @pytest.mark.parametrize("status", ["done", "cancelled", "expired"])
    def test_finished_walk_becomes_a_new_open_request(self, fake_client, base, status):
        seed_walk(base, "w1", status, price=21000)
        res = self.repeat(fake_client)
        assert res.status_code == 201
        body = res.json()
        assert body["status"] == "open" and body["id"] != "w1"
        assert body["dogId"] == "dog-luna" and body["priceCop"] == 21000 and body["durationHours"] == 1.0
        new_row = next(r for r in base.tables["walk_requests"] if r["id"] == body["id"])
        assert new_row["owner_id"] == "owner-a" and new_row["walker_id"] is None

    @pytest.mark.parametrize("status", ["open", "assigned", "in_walk"])
    def test_running_walks_cannot_be_repeated(self, fake_client, base, status):
        seed_walk(base, "w1", status)
        assert self.repeat(fake_client).status_code == 409

    @pytest.mark.parametrize("window", ["2026-09-01T10:00:00+00:00", "2026-12-01T10:00:00+00:00", "soon"])
    def test_window_is_validated(self, fake_client, base, window):
        seed_walk(base, "w1", "done")
        assert self.repeat(fake_client, window=window).status_code == 400

    def test_another_owner_cannot_repeat_my_walk(self, fake_client, base):
        seed_walk(base, "w1", "done")
        assert self.repeat(fake_client, token="token-b").status_code == 404

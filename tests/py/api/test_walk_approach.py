"""Voy en camino: the walker says he is heading to the pickup; the owner follows him from then.

Until the walker taps, the owner cannot see where he is (privacy): ``approach`` is a
409 and the owner's ``allowedActions`` has no ``approach``. After the tap and until
check-in, the owner reads the walker's last position and the foot route to the house.
The tap is idempotent and only the assigned walker may do it, on an ``assigned`` walk.
"""

import pytest

from cantrack_api.navigation import ROUTE_CACHE, get_route_source

from .conftest import bearer, make_user
from .test_walks import seed_walk

NOT_ON_THE_WAY = {"detail": "The walker is not on the way."}
NOT_ASSIGNED_TO_YOU = {"detail": "Not your walk request."}
NOT_FOUND = {"detail": "Walk not found."}
TAPPED_AT = "2026-10-01T00:00:00+00:00"   # FIXED_NOW


class FakePath:
    def __init__(self):
        self.calls = []
        self.fail = False

    def foot_path(self, points):
        self.calls.append(list(points))
        if self.fail:
            raise RuntimeError("osrm is down")
        return {"routes": [{"distance": 800.0, "duration": 600.0, "geometry": {
            "coordinates": [[-77.2830, 1.2120], [-77.2811, 1.2136]]}}]}


@pytest.fixture
def osrm(fake_client):
    ROUTE_CACHE.clear()
    source = FakePath()
    fake_client.app.dependency_overrides[get_route_source] = lambda: source
    yield source
    ROUTE_CACHE.clear()


@pytest.fixture
def base(fake):
    fake.add_user("token-w2", make_user("walker-w2", "w2@example.com", "walker"))
    fake.seed("dogs", id="dog-luna", owner_id="owner-a", name="Luna")
    fake.seed("walker_positions", walker_id="walker-w", lat=1.2120, lng=-77.2830,
              updated_at="2026-09-30T23:59:50+00:00")
    return fake


def go(client, token="token-w", walk_id="w1"):
    return client.post(f"/bidding/requests/{walk_id}/en-route", headers=bearer(token))


def approach(client, token="token-a", walk_id="w1"):
    return client.get(f"/bidding/requests/{walk_id}/approach", headers=bearer(token))


def row(fake, walk_id="w1"):
    return next(r for r in fake.tables["walk_requests"] if r["id"] == walk_id)


class TestEnRoute:
    def test_the_assigned_walker_taps_once(self, fake_client, base):
        seed_walk(base, "w1", "assigned")
        res = go(fake_client)
        assert res.status_code == 200
        assert res.json() == {"id": "w1", "enRouteAt": TAPPED_AT}
        assert row(base)["en_route_at"] == TAPPED_AT
        assert row(base)["status"] == "assigned"   # it is not a state change

    def test_tapping_again_keeps_the_first_time(self, fake_client, base, fake_clock):
        seed_walk(base, "w1", "assigned", en_route_at="2026-09-30T23:00:00+00:00")
        res = go(fake_client)
        assert res.status_code == 200
        assert res.json()["enRouteAt"] == "2026-09-30T23:00:00+00:00"

    @pytest.mark.parametrize("status", ["open", "in_walk", "done", "cancelled"])
    def test_only_an_assigned_walk(self, fake_client, base, status):
        seed_walk(base, "w1", status)
        assert go(fake_client).status_code == 409
        assert row(base).get("en_route_at") is None

    @pytest.mark.parametrize("token", ["token-a", "token-w2", "token-b"])
    def test_nobody_but_the_assigned_walker(self, fake_client, base, token):
        seed_walk(base, "w1", "assigned")
        res = go(fake_client, token=token)
        assert res.status_code == 403
        assert res.json() == NOT_ASSIGNED_TO_YOU
        assert row(base).get("en_route_at") is None


class TestApproach:
    def test_before_the_tap_the_owner_sees_nothing(self, fake_client, base, osrm):
        seed_walk(base, "w1", "assigned")
        res = approach(fake_client)
        assert res.status_code == 409
        assert res.json() == NOT_ON_THE_WAY
        assert osrm.calls == []

    def test_after_the_tap_the_owner_gets_position_and_route(self, fake_client, base, osrm):
        seed_walk(base, "w1", "assigned", en_route_at=TAPPED_AT)
        res = approach(fake_client)
        assert res.status_code == 200
        assert res.json() == {
            "enRouteAt": TAPPED_AT,
            "position": {"lat": 1.2120, "lng": -77.2830, "updatedAt": "2026-09-30T23:59:50+00:00"},
            "pickup": {"lat": 1.2136, "lng": -77.2811},
            "route": {"source": "osrm", "distanceKm": 0.8, "minutes": 10,
                      "geometry": [[1.2120, -77.2830], [1.2136, -77.2811]]},
        }
        assert osrm.calls == [[(1.2120, -77.2830), (1.2136, -77.2811)]]

    def test_the_walker_may_read_it_too(self, fake_client, base, osrm):
        seed_walk(base, "w1", "assigned", en_route_at=TAPPED_AT)
        assert approach(fake_client, token="token-w").status_code == 200

    def test_the_straight_line_when_the_router_is_down(self, fake_client, base, osrm):
        osrm.fail = True
        seed_walk(base, "w1", "assigned", en_route_at=TAPPED_AT)
        route = approach(fake_client).json()["route"]
        assert route["source"] == "straight"
        assert route["geometry"] == [[1.2120, -77.2830], [1.2136, -77.2811]]

    def test_no_position_yet_means_no_route(self, fake_client, base, osrm):
        base.tables["walker_positions"].clear()
        seed_walk(base, "w1", "assigned", en_route_at=TAPPED_AT)
        body = approach(fake_client).json()
        assert body["position"] is None and body["route"] is None
        assert osrm.calls == []

    def test_a_position_outside_pasto_is_not_routed(self, fake_client, base, osrm):
        base.tables["walker_positions"][0].update(lat=6.2, lng=-75.5)
        seed_walk(base, "w1", "assigned", en_route_at=TAPPED_AT)
        body = approach(fake_client).json()
        assert body["position"] is not None and body["route"] is None
        assert osrm.calls == []

    @pytest.mark.parametrize("status", ["in_walk", "done", "cancelled"])
    def test_it_ends_with_the_pickup(self, fake_client, base, osrm, status):
        seed_walk(base, "w1", status, en_route_at=TAPPED_AT)
        assert approach(fake_client).status_code == 409

    @pytest.mark.parametrize("token", ["token-b", "token-w2"])
    def test_strangers_get_the_same_404(self, fake_client, base, osrm, token):
        seed_walk(base, "w1", "assigned", en_route_at=TAPPED_AT)
        res = approach(fake_client, token=token)
        assert res.status_code == 404
        assert res.json() == NOT_FOUND

    def test_an_unknown_walk_is_404(self, fake_client, base, osrm):
        assert approach(fake_client, walk_id="nope").status_code == 404


class TestActions:
    def actions(self, client, token):
        return client.get("/bidding/requests/w1", headers=bearer(token)).json()["allowedActions"]

    def test_the_owner_gets_approach_only_once_the_walker_is_on_the_way(self, fake_client, base):
        seed_walk(base, "w1", "assigned")
        assert "approach" not in self.actions(fake_client, "token-a")
        row(base)["en_route_at"] = TAPPED_AT
        assert "approach" in self.actions(fake_client, "token-a")

    def test_approach_ends_when_the_walk_starts(self, fake_client, base):
        seed_walk(base, "w1", "in_walk", en_route_at=TAPPED_AT)
        assert "approach" not in self.actions(fake_client, "token-a")

    def test_the_walker_buttons_do_not_change(self, fake_client, base):
        seed_walk(base, "w1", "assigned", en_route_at=TAPPED_AT)
        assert self.actions(fake_client, "token-w") == ["check_in", "cancel"]

"""W4 Rutas (walker): the day's won walks as one route.

Stops are my walks (assigned, in_walk, done) on one local Bogota day, ordered
by pickup time through the hand-written AVL tree. Each leg's minutes come
from Dijkstra over ``walk_graph_edges`` (stop keys ``pickup:<requestId>``)
when the graph connects the two stops, else from the straight-line
(haversine) distance at 4.5 km/h. ``legKm`` is always the straight line.
"""

import pytest

from .conftest import bearer, make_user

# FIXED_NOW = 2026-10-01T00:00Z = 2026-09-30 19:00 in Bogota, so "today" is 2026-09-30.
DAY = "2026-10-05"
AT_9 = "2026-10-05T14:00:00+00:00"    # 09:00 local
AT_11 = "2026-10-05T16:00:00+00:00"   # 11:00 local
AT_15 = "2026-10-05T20:00:00+00:00"   # 15:00 local
NEXT_DAY_1AM = "2026-10-06T06:00:00+00:00"   # 01:00 local on the 6th
SAME_UTC_DAY_LATE = "2026-10-05T03:00:00+00:00"   # 22:00 local on the 4th

ROUTE_KEYS = {"date", "start", "stops", "totalKm", "totalMinutes", "nextStopId"}


def walk(fake, walk_id, status, requested_time, lat, walker_id="walker-w", dog_id="dog-luna"):
    return fake.seed(
        "walk_requests", id=walk_id, owner_id="owner-a", walker_id=walker_id, dog_id=dog_id,
        status=status, requested_time=requested_time, window_end=requested_time,
        deadline_at=requested_time, duration_hours=1.0, pickup_lat=lat, pickup_lng=-75.5,
        price_cop=20000, created_at="2026-09-01T00:00:00+00:00",
    )


def edge(fake, from_id, to_id, minutes):
    fake.seed("walk_graph_edges", from_stop=f"pickup:{from_id}", to_stop=f"pickup:{to_id}", minutes=minutes)


def route(client, date=DAY, token="token-w", walker_id="walker-w"):
    params = {"date": date} if date else {}
    return client.get(f"/walkers/{walker_id}/route", params=params, headers=bearer(token))


@pytest.fixture
def data(fake):
    fake.add_user("token-v", make_user("walker-v", "walker-v@example.com", "walker"))
    fake.seed("dogs", id="dog-luna", owner_id="owner-a", name="Luna")
    fake.seed("dogs", id="dog-max", owner_id="owner-a", name="Max")
    # Inserted out of order on purpose: the AVL tree must sort them.
    walk(fake, "w15", "assigned", AT_15, lat=6.218, dog_id="dog-max")
    walk(fake, "w9", "done", AT_9, lat=6.200)
    walk(fake, "w11", "assigned", AT_11, lat=6.209)
    walk(fake, "w-open", "open", AT_11, lat=6.3, walker_id=None)
    walk(fake, "w-cancel", "cancelled", AT_11, lat=6.3)
    walk(fake, "w-theirs", "assigned", AT_11, lat=6.3, walker_id="walker-v")
    walk(fake, "w-tomorrow", "assigned", NEXT_DAY_1AM, lat=6.3)
    walk(fake, "w-yesterday", "assigned", SAME_UTC_DAY_LATE, lat=6.3)
    return fake


class TestStops:
    def test_only_my_won_walks_of_the_local_day_in_pickup_order(self, fake_client, data):
        res = route(fake_client)
        assert res.status_code == 200
        body = res.json()
        assert set(body) == ROUTE_KEYS
        assert body["date"] == DAY
        assert [s["requestId"] for s in body["stops"]] == ["w9", "w11", "w15"]

    def test_stop_shape(self, fake_client, data):
        stop = route(fake_client).json()["stops"][2]
        assert stop == {
            "requestId": "w15", "dogName": "Max", "status": "assigned", "requestedTime": AT_15,
            "pickupLat": 6.218, "pickupLng": -75.5, "legKm": 1.0, "legMinutes": 13, "legSource": "straight",
        }

    def test_next_stop_is_the_first_not_done(self, fake_client, data):
        assert route(fake_client).json()["nextStopId"] == "w11"

    def test_an_empty_day(self, fake_client, data):
        body = route(fake_client, date="2026-10-20").json()
        assert body == {"date": "2026-10-20", "start": None, "stops": [], "totalKm": 0.0,
                        "totalMinutes": 0, "nextStopId": None}

    def test_default_date_is_today_in_bogota(self, fake_client, data):
        walk(data, "w-today", "assigned", "2026-09-30T23:00:00+00:00", lat=6.2)   # 18:00 local on the 30th
        body = route(fake_client, date=None).json()
        assert body["date"] == "2026-09-30"
        assert [s["requestId"] for s in body["stops"]] == ["w-today"]


class TestLegs:
    def test_without_a_position_the_first_leg_is_zero(self, fake_client, data):
        body = route(fake_client).json()
        assert body["start"] is None
        first = body["stops"][0]
        assert (first["legKm"], first["legMinutes"], first["legSource"]) == (0.0, 0, "start")

    def test_the_first_leg_starts_at_my_position(self, fake_client, data):
        data.seed("walker_positions", walker_id="walker-w", lat=6.191, lng=-75.5,
                  updated_at="2026-10-01T00:00:00+00:00")
        body = route(fake_client).json()
        assert body["start"] == {"lat": 6.191, "lng": -75.5}
        first = body["stops"][0]
        assert (first["legKm"], first["legMinutes"], first["legSource"]) == (1.0, 13, "straight")

    def test_graph_minutes_win_when_dijkstra_connects_the_stops(self, fake_client, data):
        # w9 -> w11 direct is 30 min, but w9 -> x -> w11 is 4 + 5 = 9: Dijkstra must find 9.
        edge(data, "w9", "w11", 30)
        data.seed("walk_graph_edges", from_stop="pickup:w9", to_stop="corner:x", minutes=4)
        data.seed("walk_graph_edges", from_stop="corner:x", to_stop="pickup:w11", minutes=5)
        second = route(fake_client).json()["stops"][1]
        assert (second["legKm"], second["legMinutes"], second["legSource"]) == (1.0, 9, "graph")

    def test_unconnected_stops_fall_back_to_straight(self, fake_client, data):
        edge(data, "w11", "somewhere-else", 2)
        third = route(fake_client).json()["stops"][2]
        assert third["legSource"] == "straight"

    def test_totals(self, fake_client, data):
        edge(data, "w9", "w11", 7)
        body = route(fake_client).json()
        assert body["totalKm"] == 2.0
        assert body["totalMinutes"] == 0 + 7 + 13


class TestAccess:
    @pytest.mark.parametrize("date", ["2026-13-01", "tomorrow", "05/10/2026"])
    def test_bad_date_is_400(self, fake_client, data, date):
        assert route(fake_client, date=date).status_code == 400

    @pytest.mark.parametrize("token, walker_id", [("token-v", "walker-w"), ("token-a", "owner-a")])
    def test_nobody_else(self, fake_client, data, token, walker_id):
        assert route(fake_client, token=token, walker_id=walker_id).status_code == 403

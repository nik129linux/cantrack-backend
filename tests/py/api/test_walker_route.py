"""W4 Rutas (walker): the day's won walks as one route.

Stops are my walks (assigned, in_walk, done) on one local Bogota day, ordered
by pickup time through the hand-written AVL tree. Each leg's minutes come
from Dijkstra over ``walk_graph_edges`` (stop keys ``pickup:<requestId>``)
when the graph connects the two stops, else from the straight-line
(haversine) distance at 4.5 km/h. ``legKm`` is always the straight line.
"""

import pytest

from cantrack_api.navigation import ROUTE_CACHE, get_route_source, step_text

from .conftest import bearer, make_user

# FIXED_NOW = 2026-10-01T00:00Z = 2026-09-30 19:00 in Bogota, so "today" is 2026-09-30.
DAY = "2026-10-05"
AT_9 = "2026-10-05T14:00:00+00:00"    # 09:00 local
AT_11 = "2026-10-05T16:00:00+00:00"   # 11:00 local
AT_15 = "2026-10-05T20:00:00+00:00"   # 15:00 local
NEXT_DAY_1AM = "2026-10-06T06:00:00+00:00"   # 01:00 local on the 6th
SAME_UTC_DAY_LATE = "2026-10-05T03:00:00+00:00"   # 22:00 local on the 4th

ROUTE_KEYS = {"date", "start", "stops", "totalKm", "totalMinutes", "nextStopId", "overdue", "active"}


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


class FakeOsrm:
    """The routing source under test: two steps along two streets."""

    def __init__(self):
        self.calls = 0
        self.fail = False
        self.payload = {
            "routes": [
                {
                    "distance": 430.0,
                    "duration": 372.0,
                    "legs": [
                        {
                            "steps": [
                                {
                                    "name": "Calle 16",
                                    "distance": 180.0,
                                    "maneuver": {"type": "depart"},
                                    "geometry": {"coordinates": [[-77.2811, 1.2136], [-77.2800, 1.2125]]},
                                },
                                {
                                    "name": "Carrera 22",
                                    "distance": 250.0,
                                    "maneuver": {"type": "turn", "modifier": "right"},
                                    "geometry": {"coordinates": [[-77.2800, 1.2125], [-77.2788, 1.2102]]},
                                },
                            ]
                        }
                    ],
                }
            ]
        }

    def foot_route(self, from_lat, from_lng, to_lat, to_lng):
        self.calls += 1
        if self.fail:
            raise RuntimeError("osrm is down")
        return self.payload


@pytest.fixture
def osrm(fake_client):
    ROUTE_CACHE.clear()
    source = FakeOsrm()
    fake_client.app.dependency_overrides[get_route_source] = lambda: source
    yield source
    ROUTE_CACHE.clear()


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
            "durationHours": 1.0, "netCop": 18000, "ownerName": None, "ownerPhone": None,
            "pickupLabel": None, "enRouteAt": None,
        }

    def test_a_stop_carries_the_moment_the_walker_set_off(self, fake_client, data):
        next(r for r in data.tables["walk_requests"] if r["id"] == "w11")["en_route_at"] = "2026-10-05T14:40:00+00:00"
        stops = {s["requestId"]: s for s in route(fake_client).json()["stops"]}
        assert stops["w11"]["enRouteAt"] == "2026-10-05T14:40:00+00:00"
        assert stops["w15"]["enRouteAt"] is None

    def test_next_stop_is_the_first_not_done(self, fake_client, data):
        assert route(fake_client).json()["nextStopId"] == "w11"

    def test_an_empty_day(self, fake_client, data):
        body = route(fake_client, date="2026-10-20").json()
        assert body == {"date": "2026-10-20", "start": None, "stops": [], "totalKm": 0.0,
                        "totalMinutes": 0, "nextStopId": None, "overdue": [], "active": None}

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


class TestOverdue:
    # FIXED_NOW is 2026-10-01T00:00Z: a slot that ended before then is overdue.
    def test_only_my_assigned_walks_whose_slot_ended(self, fake_client, data):
        walk(data, "w-late", "assigned", "2026-09-28T14:00:00+00:00", lat=6.2)
        walk(data, "w-late-done", "done", "2026-09-27T14:00:00+00:00", lat=6.2)
        walk(data, "w-late-theirs", "assigned", "2026-09-27T14:00:00+00:00", lat=6.2, walker_id="walker-v")
        walk(data, "w-running", "assigned", "2026-09-30T23:30:00+00:00", lat=6.2)   # ends 00:30Z
        body = route(fake_client).json()
        assert body["overdue"] == [
            {"requestId": "w-late", "dogName": "Luna", "requestedTime": "2026-09-28T14:00:00+00:00",
             "durationHours": 1.0, "pickupLat": 6.2, "pickupLng": -75.5, "pickupLabel": None},
        ]

    def test_oldest_first_on_any_day(self, fake_client, data):
        walk(data, "w-b", "assigned", "2026-09-29T14:00:00+00:00", lat=6.2)
        walk(data, "w-a", "assigned", "2026-09-20T14:00:00+00:00", lat=6.2, dog_id="dog-max")
        body = route(fake_client, date="2026-10-20").json()
        assert [late["requestId"] for late in body["overdue"]] == ["w-a", "w-b"]
        assert body["overdue"][0]["dogName"] == "Max"


class TestPickupLabel:
    def test_the_stop_the_overdue_and_the_active_walk_carry_the_label(self, fake_client, data):
        walk(data, "w-late", "assigned", "2026-09-28T14:00:00+00:00", lat=6.2)
        walk(data, "w-now", "in_walk", "2026-09-25T14:00:00+00:00", lat=6.25)
        data.seed("walk_timeline", walk_request_id="w-now", kind="check_in", at="2026-09-30T23:40:00+00:00", payload={})
        for walk_id in ("w11", "w-late", "w-now"):
            next(row for row in data.tables["walk_requests"] if row["id"] == walk_id)["pickup_label"] = f"Calle {walk_id}"
        body = route(fake_client).json()
        assert next(s for s in body["stops"] if s["requestId"] == "w11")["pickupLabel"] == "Calle w11"
        assert body["overdue"][0]["pickupLabel"] == "Calle w-late"
        assert body["active"]["pickupLabel"] == "Calle w-now"


class TestOwnerAndPay:
    def test_phone_only_on_live_walks(self, fake_client, data):
        data.seed("owner_profiles", owner_id="owner-a", display_name="Camila", phone="3001234567")
        stops = {s["requestId"]: s for s in route(fake_client).json()["stops"]}
        assert (stops["w11"]["ownerName"], stops["w11"]["ownerPhone"]) == ("Camila", "3001234567")
        assert (stops["w9"]["ownerName"], stops["w9"]["ownerPhone"]) == (None, None)

    def test_net_uses_my_offer_minus_commission(self, fake_client, data):
        data.seed("walk_offers", id="o1", walk_request_id="w11", walker_id="walker-w", price_cop=25000)
        data.seed("walk_offers", id="o2", walk_request_id="w15", walker_id="walker-v", price_cop=90000)
        stops = {s["requestId"]: s for s in route(fake_client).json()["stops"]}
        assert stops["w11"]["netCop"] == 22500
        assert stops["w15"]["netCop"] == 18000


class TestActive:
    def test_the_walk_in_progress_from_any_day_with_its_check_in(self, fake_client, data):
        data.seed("owner_profiles", owner_id="owner-a", display_name="Camila", phone="3001234567")
        walk(data, "w-now", "in_walk", "2026-09-25T14:00:00+00:00", lat=6.25)
        data.seed("walk_timeline", walk_request_id="w-now", kind="check_in", at="2026-09-30T23:40:00+00:00", payload={})
        body = route(fake_client, date="2026-10-20").json()
        assert body["active"] == {
            "requestId": "w-now", "dogName": "Luna", "requestedTime": "2026-09-25T14:00:00+00:00",
            "durationHours": 1.0, "startedAt": "2026-09-30T23:40:00+00:00", "netCop": 18000,
            "ownerName": "Camila", "ownerPhone": "3001234567", "pickupLat": 6.25, "pickupLng": -75.5,
            "pickupLabel": None,
        }

    def test_nobody_elses_walk(self, fake_client, data):
        walk(data, "w-their-walk", "in_walk", AT_9, lat=6.25, walker_id="walker-v")
        assert route(fake_client).json()["active"] is None


class TestAccess:
    @pytest.mark.parametrize("date", ["2026-13-01", "tomorrow", "05/10/2026"])
    def test_bad_date_is_400(self, fake_client, data, date):
        assert route(fake_client, date=date).status_code == 400

    @pytest.mark.parametrize("token, walker_id", [("token-v", "walker-w"), ("token-a", "owner-a")])
    def test_nobody_else(self, fake_client, data, token, walker_id):
        assert route(fake_client, token=token, walker_id=walker_id).status_code == 403

class TestWalkingRoute:
    """Cómo llegar: the streets to the pickup, drawn on the app's own map."""

    def ask(self, client, token="token-w", walker_id="walker-w", **over):
        params = {"fromLat": 1.2136, "fromLng": -77.2811, "toLat": 1.2102, "toLng": -77.2788}
        params.update(over)
        return client.get(f"/walkers/{walker_id}/walking-route", params=params, headers=bearer(token))

    def test_turns_and_line_come_from_the_router(self, fake_client, data, osrm):
        body = self.ask(fake_client).json()
        assert body["source"] == "osrm"
        assert body["distanceKm"] == 0.43
        assert body["minutes"] == 6
        # The joint point between the two steps is in the line once.
        assert body["geometry"] == [[1.2136, -77.2811], [1.2125, -77.2800], [1.2102, -77.2788]]
        assert body["steps"] == [
            {"text": "Empieza a caminar por Calle 16", "distanceM": 180, "startIndex": 0},
            {"text": "Gira a la derecha en Carrera 22", "distanceM": 250, "startIndex": 1},
        ]

    def test_the_straight_line_when_the_router_is_down(self, fake_client, data, osrm):
        osrm.fail = True
        body = self.ask(fake_client).json()
        assert body["source"] == "straight"
        assert body["steps"] == []
        assert body["geometry"] == [[1.2136, -77.2811], [1.2102, -77.2788]]
        assert body["distanceKm"] > 0 and body["minutes"] >= 1

    def test_an_answer_without_a_route_also_falls_back(self, fake_client, data, osrm):
        osrm.payload = {"code": "NoRoute", "routes": []}
        assert self.ask(fake_client).json()["source"] == "straight"

    def test_the_same_trip_is_only_routed_once(self, fake_client, data, osrm):
        self.ask(fake_client)
        self.ask(fake_client, fromLat=1.21361)   # same ~11 m cell
        assert osrm.calls == 1

    @pytest.mark.parametrize("over", [{"fromLat": 4.65}, {"toLng": -74.05}, {"toLat": 40.7, "toLng": -74.0}])
    def test_only_inside_pasto(self, fake_client, data, osrm, over):
        assert self.ask(fake_client, **over).status_code == 400

    def test_nobody_else(self, fake_client, data, osrm):
        assert self.ask(fake_client, token="token-v").status_code == 403


class TestStepWording:
    @pytest.mark.parametrize("maneuver, name, text", [
        ({"type": "depart"}, "Calle 16", "Empieza a caminar por Calle 16"),
        ({"type": "depart"}, "", "Empieza a caminar"),
        ({"type": "turn", "modifier": "left"}, "Carrera 22", "Gira a la izquierda en Carrera 22"),
        ({"type": "turn", "modifier": "slight right"}, "", "Gira levemente a la derecha"),
        ({"type": "turn", "modifier": "straight"}, "Calle 18", "Sigue derecho por Calle 18"),
        ({"type": "turn", "modifier": "uturn"}, "Calle 18", "Da la vuelta en Calle 18"),
        ({"type": "new name"}, "Avenida Los Estudiantes", "Sigue por Avenida Los Estudiantes"),
        ({"type": "continue"}, "Calle 20", "Sigue derecho por Calle 20"),
        ({"type": "end of road", "modifier": "right"}, "Carrera 19", "Al final de la calle, gira a la derecha en Carrera 19"),
        ({"type": "fork", "modifier": "left"}, "", "Mantente a la izquierda"),
        ({"type": "roundabout", "exit": 2}, "Calle 21", "En la glorieta, toma la salida 2 por Calle 21"),
        ({"type": "exit roundabout"}, "Calle 21", "Sal de la glorieta por Calle 21"),
        ({"type": "arrive"}, "Calle 16", "Llegaste al punto de recogida"),
        ({"type": "notify"}, "", "Sigue derecho"),
    ])
    def test_every_turn_reads_in_spanish(self, maneuver, name, text):
        assert step_text(maneuver, name) == text



class FakePathOsrm:
    """The multi-stop routing source: one answer for the whole day."""

    def __init__(self):
        self.calls = []
        self.fail = False
        # [lng, lat] pairs, as OSRM sends them; four points along the streets.
        self.payload = {
            "routes": [
                {
                    "distance": 1900.0,
                    "duration": 1500.0,
                    "geometry": {
                        "coordinates": [
                            [-77.2811, 1.2136], [-77.2800, 1.2125],
                            [-77.2790, 1.2150], [-77.2700, 1.2200],
                        ]
                    },
                }
            ]
        }

    def foot_path(self, points):
        self.calls.append(list(points))
        if self.fail:
            raise RuntimeError("osrm is down")
        return self.payload


@pytest.fixture
def path_osrm(fake_client):
    ROUTE_CACHE.clear()
    source = FakePathOsrm()
    fake_client.app.dependency_overrides[get_route_source] = lambda: source
    yield source
    ROUTE_CACHE.clear()


def pasto_walk(fake, walk_id, status, requested_time, lat, lng):
    return fake.seed(
        "walk_requests", id=walk_id, owner_id="owner-a", walker_id="walker-w", dog_id="dog-luna",
        status=status, requested_time=requested_time, window_end=requested_time,
        deadline_at=requested_time, duration_hours=1.0, pickup_lat=lat, pickup_lng=lng,
        price_cop=20000, created_at="2026-09-01T00:00:00+00:00",
    )


@pytest.fixture
def pasto_day(fake):
    fake.seed("dogs", id="dog-luna", owner_id="owner-a", name="Luna")
    fake.seed("walker_positions", walker_id="walker-w", lat=1.2136, lng=-77.2811,
              updated_at="2026-10-01T00:00:00+00:00")
    # Out of order on purpose: the day line follows pickup time, not insertion.
    pasto_walk(fake, "p15", "assigned", AT_15, 1.2200, -77.2700)
    pasto_walk(fake, "p9", "done", AT_9, 1.2100, -77.2850)
    pasto_walk(fake, "p11", "assigned", AT_11, 1.2150, -77.2790)
    return fake


def day_path(client, date=DAY, token="token-w", walker_id="walker-w"):
    params = {"date": date} if date else {}
    return client.get(f"/walkers/{walker_id}/route/path", params=params, headers=bearer(token))


class TestFootProfile:
    """The public demo server only knows cars (one-way streets); walkers need the foot server."""

    def test_the_default_server_is_the_foot_profile(self):
        from cantrack_api.navigation import OSRM_URL

        assert "routed-foot" in OSRM_URL

    def test_foot_path_asks_for_every_point_in_one_call(self, monkeypatch):
        from cantrack_api import navigation

        seen = {}

        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"routes": []}

        def fake_get(url, params=None, headers=None, timeout=None):
            seen.update(url=url, params=params)
            return Response()

        monkeypatch.setattr(navigation.httpx, "get", fake_get)
        navigation.OsrmClient().foot_path([(1.2136, -77.2811), (1.2150, -77.2790), (1.2200, -77.2700)])
        assert seen["url"].endswith("/route/v1/foot/-77.2811,1.2136;-77.279,1.215;-77.27,1.22")
        assert seen["params"]["overview"] == "full"
        assert seen["params"]["geometries"] == "geojson"


class TestDayPath:
    """Rutas: the whole day as one line along the streets, me -> stop -> stop."""

    def test_the_line_comes_from_the_router(self, fake_client, pasto_day, path_osrm):
        body = day_path(fake_client).json()
        assert body == {
            "source": "osrm", "distanceKm": 1.9, "minutes": 25,
            "geometry": [[1.2136, -77.2811], [1.2125, -77.2800], [1.215, -77.279], [1.22, -77.27]],
        }

    def test_goes_through_my_position_then_the_live_stops_in_pickup_order(self, fake_client, pasto_day, path_osrm):
        day_path(fake_client)
        # p9 is done and must not be walked again.
        assert path_osrm.calls == [[(1.2136, -77.2811), (1.215, -77.279), (1.22, -77.27)]]

    def test_without_a_position_it_starts_at_the_first_live_stop(self, fake_client, pasto_day, path_osrm):
        pasto_day.tables["walker_positions"].clear()
        day_path(fake_client)
        assert path_osrm.calls == [[(1.215, -77.279), (1.22, -77.27)]]

    def test_the_straight_segments_when_the_router_is_down(self, fake_client, pasto_day, path_osrm):
        path_osrm.fail = True
        body = day_path(fake_client).json()
        assert body["source"] == "straight"
        assert body["geometry"] == [[1.2136, -77.2811], [1.215, -77.279], [1.22, -77.27]]
        assert body["distanceKm"] > 0 and body["minutes"] >= 1

    def test_a_single_point_has_nothing_to_draw(self, fake_client, pasto_day, path_osrm):
        pasto_day.tables["walker_positions"].clear()
        pasto_day.tables["walk_requests"][:] = [
            row for row in pasto_day.tables["walk_requests"] if row["id"] != "p15"
        ]
        body = day_path(fake_client).json()
        assert body == {"source": "straight", "distanceKm": 0.0, "minutes": 0, "geometry": []}
        assert path_osrm.calls == []

    def test_the_same_day_is_only_routed_once(self, fake_client, pasto_day, path_osrm):
        day_path(fake_client)
        day_path(fake_client)
        assert len(path_osrm.calls) == 1

    def test_stops_outside_pasto_are_left_out(self, fake_client, pasto_day, path_osrm):
        # w-far is in Medellin: the API is never a routing proxy for the world.
        pasto_walk(pasto_day, "far", "assigned", AT_11, 6.2, -75.5)
        day_path(fake_client)
        assert (6.2, -75.5) not in path_osrm.calls[0]

    @pytest.mark.parametrize("date", ["2026-13-01", "tomorrow"])
    def test_bad_date_is_400(self, fake_client, pasto_day, path_osrm, date):
        assert day_path(fake_client, date=date).status_code == 400

    @pytest.mark.parametrize("token, walker_id", [("token-v", "walker-w"), ("token-a", "owner-a")])
    def test_nobody_else(self, fake_client, pasto_day, path_osrm, token, walker_id):
        pasto_day.add_user("token-v", make_user("walker-v", "walker-v@example.com", "walker"))
        assert day_path(fake_client, token=token, walker_id=walker_id).status_code == 403

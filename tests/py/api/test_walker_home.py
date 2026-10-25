"""W1 Inicio (walker): home panel, online switch and simulated earnings.

InDrive style: the walker goes online to receive requests, and the home shows
today's money. The commission is SIMULATED (10%): no real payments exist.
Days are local to Colombia (America/Bogota, UTC-5, no DST).
"""

from datetime import timedelta

import pytest

from .conftest import FIXED_NOW, bearer, make_user, reset_all_rate_limits

# FIXED_NOW = 2026-10-01T00:00:00Z = Wednesday 2026-09-30 19:00 in Bogota.
TODAY_10AM = "2026-09-30T15:00:00+00:00"       # 10:00 local, today
TODAY_6PM = "2026-09-30T23:00:00+00:00"        # 18:00 local, today
YESTERDAY_11PM = "2026-09-30T04:00:00+00:00"   # 23:00 local on the 29th: NOT today
TOMORROW_1AM = "2026-10-01T06:00:00+00:00"     # 01:00 local on Oct 1: NOT today
MONDAY = "2026-09-28T15:00:00+00:00"           # this week
LAST_SUNDAY = "2026-09-27T15:00:00+00:00"      # last week, same month
AUGUST = "2026-08-31T15:00:00+00:00"           # last month
SOON = "2026-10-01T15:00:00+00:00"
LATER = "2026-10-02T15:00:00+00:00"

PASTO = (1.2136, -77.2811)


def walk(fake, walk_id, status, requested_time=SOON, walker_id="walker-w", owner_id="owner-a",
         dog_id="dog-luna", price=20000, pickup=PASTO, deadline="2026-10-01T00:10:00+00:00"):
    return fake.seed(
        "walk_requests", id=walk_id, owner_id=owner_id, walker_id=walker_id, dog_id=dog_id,
        status=status, requested_time=requested_time, window_end=requested_time,
        deadline_at=deadline, duration_hours=0.75, pickup_lat=pickup[0], pickup_lng=pickup[1],
        price_cop=price, created_at="2026-09-20T00:00:00+00:00",
    )


def offer(fake, walk_id, price, walker_id="walker-w", offer_id=None):
    return fake.seed(
        "walk_offers", id=offer_id or f"of-{walk_id}-{walker_id}", walk_request_id=walk_id,
        walker_id=walker_id, price_cop=price, eta_min=10.0, distance_km=1.0,
        created_at="2026-09-20T00:00:00+00:00",
    )


def position(fake, seconds_ago=0, walker_id="walker-w", at=PASTO):
    return fake.seed("walker_positions", walker_id=walker_id, lat=at[0], lng=at[1],
                     updated_at=(FIXED_NOW - timedelta(seconds=seconds_ago)).isoformat())


@pytest.fixture
def data(fake):
    fake.add_user("token-v", make_user("walker-v", "walker-v@example.com", "walker"))
    fake.seed("dogs", id="dog-luna", owner_id="owner-a", name="Luna")
    fake.seed("dogs", id="dog-max", owner_id="owner-a", name="Max")
    fake.seed("owner_profiles", owner_id="owner-a", display_name="Camila")
    fake.seed("walker_profiles", walker_id="walker-w", display_name="Mateo", bio=None,
              service_area="Centro", price_per_walk=15000)
    return fake


def get_home(client, token="token-w", walker_id="walker-w"):
    return client.get(f"/walkers/{walker_id}/home", headers=bearer(token))


def earnings(client, period=None, token="token-w", walker_id="walker-w"):
    params = {"period": period} if period else {}
    return client.get(f"/walkers/{walker_id}/earnings", params=params, headers=bearer(token))


def availability(client, body, token="token-w", walker_id="walker-w"):
    return client.put(f"/walkers/{walker_id}/availability", json=body, headers=bearer(token))


class TestAccess:
    @pytest.mark.parametrize("path", ["home", "earnings"])
    def test_another_walker_gets_403(self, fake_client, data, path):
        res = fake_client.get(f"/walkers/walker-w/{path}", headers=bearer("token-v"))
        assert res.status_code == 403

    @pytest.mark.parametrize("path", ["home", "earnings"])
    def test_an_owner_gets_403(self, fake_client, data, path):
        res = fake_client.get(f"/walkers/owner-a/{path}", headers=bearer("token-a"))
        assert res.status_code == 403

    def test_no_token_is_401(self, fake_client):
        assert fake_client.get("/walkers/walker-w/home").status_code == 401

    def test_another_walker_cannot_switch_me_offline(self, fake_client, data):
        position(data)
        assert availability(fake_client, {"online": False}, token="token-v").status_code == 403
        assert len(data.tables["walker_positions"]) == 1


class TestHomeEmpty:
    def test_new_walker_gets_empty_sections_not_errors(self, fake_client):
        res = get_home(fake_client)
        assert res.status_code == 200
        assert res.json() == {
            "greetingName": "walker-w",
            "online": False,
            "activeWalk": None,
            "nextWalk": None,
            "today": {"walks": 0, "grossCop": 0, "commissionCop": 0, "netCop": 0},
            "rating": {"average": None, "count": 0},
            "openNearby": 0,
            "pendingOffers": 0,
        }


class TestHome:
    def test_greeting_uses_the_walker_profile_name(self, fake_client, data):
        assert get_home(fake_client).json()["greetingName"] == "Mateo"

    def test_online_follows_the_heartbeat(self, fake_client, data):
        position(data, seconds_ago=60)
        assert get_home(fake_client).json()["online"] is True
        data.tables["walker_positions"].clear()
        position(data, seconds_ago=300)
        assert get_home(fake_client).json()["online"] is False

    def test_next_walk_is_my_soonest_assigned_one_at_my_price(self, fake_client, data):
        walk(data, "w-later", "assigned", LATER)
        walk(data, "w-soon", "assigned", SOON, dog_id="dog-max", price=20000)
        offer(data, "w-soon", 24000)
        walk(data, "w-theirs", "assigned", SOON, walker_id="walker-v")
        assert get_home(fake_client).json()["nextWalk"] == {
            "id": "w-soon", "dogName": "Max", "ownerName": "Camila",
            "requestedTime": SOON, "durationHours": 0.75, "priceCop": 24000,
        }

    def test_without_an_offer_row_the_price_is_the_fare(self, fake_client, data):
        walk(data, "w1", "assigned", SOON, price=21000)
        assert get_home(fake_client).json()["nextWalk"]["priceCop"] == 21000

    def test_owner_without_profile_has_null_name_never_the_email(self, fake_client, data):
        data.tables["owner_profiles"].clear()
        walk(data, "w1", "assigned")
        body = get_home(fake_client)
        assert body.json()["nextWalk"]["ownerName"] is None
        assert "owner-a@example.com" not in body.text

    def test_active_walk_is_the_one_in_progress(self, fake_client, data):
        walk(data, "w-live", "in_walk", TODAY_6PM)
        walk(data, "w-next", "assigned", SOON)
        body = get_home(fake_client).json()
        assert body["activeWalk"]["id"] == "w-live"
        assert body["nextWalk"]["id"] == "w-next"

    def test_today_counts_only_done_walks_of_the_local_day(self, fake_client, data):
        walk(data, "d1", "done", TODAY_10AM, price=20000)
        walk(data, "d2", "done", TODAY_6PM, price=10000)
        offer(data, "d2", 30000)
        walk(data, "d-yday", "done", YESTERDAY_11PM, price=50000)
        walk(data, "d-tmrw", "done", TOMORROW_1AM, price=50000)
        walk(data, "a1", "assigned", TODAY_10AM, price=50000)
        walk(data, "d-theirs", "done", TODAY_10AM, walker_id="walker-v", price=50000)
        assert get_home(fake_client).json()["today"] == {
            "walks": 2, "grossCop": 50000, "commissionCop": 5000, "netCop": 45000,
        }

    def test_rating_is_my_average_rounded(self, fake_client, data):
        for index, value in enumerate([5, 4, 4]):
            data.seed("walk_ratings", walk_request_id=f"r{index}", walker_id="walker-w", rating=value,
                      created_at="2026-09-01T00:00:00+00:00")
        data.seed("walk_ratings", walk_request_id="r-v", walker_id="walker-v", rating=1,
                  created_at="2026-09-01T00:00:00+00:00")
        assert get_home(fake_client).json()["rating"] == {"average": 4.33, "count": 3}

    def test_open_nearby_counts_open_requests_within_2_km_while_online(self, fake_client, data):
        position(data)
        walk(data, "o-near", "open", walker_id=None, pickup=(1.2200, -77.2811))       # ~0.7 km
        walk(data, "o-far", "open", walker_id=None, pickup=(1.2500, -77.2811))        # ~4 km
        walk(data, "o-late", "open", walker_id=None, deadline="2026-09-30T23:00:00+00:00")  # deadline passed
        walk(data, "o-asg", "assigned", walker_id="walker-v")
        assert get_home(fake_client).json()["openNearby"] == 1

    def test_open_nearby_is_zero_while_offline(self, fake_client, data):
        position(data, seconds_ago=600)
        walk(data, "o-near", "open", walker_id=None)
        assert get_home(fake_client).json()["openNearby"] == 0

    def test_pending_offers_are_mine_on_still_open_requests(self, fake_client, data):
        walk(data, "o1", "open", walker_id=None)
        walk(data, "o2", "open", walker_id=None)
        walk(data, "gone", "assigned", walker_id="walker-v")
        offer(data, "o1", 20000)
        offer(data, "gone", 20000)
        offer(data, "o2", 20000, walker_id="walker-v")
        assert get_home(fake_client).json()["pendingOffers"] == 1


class TestAvailability:
    def test_going_online_stores_my_position_now(self, fake_client, data):
        res = availability(fake_client, {"online": True, "lat": 1.21, "lng": -77.28})
        assert res.status_code == 200
        assert res.json() == {"online": True, "updatedAt": FIXED_NOW.isoformat()}
        rows = data.tables["walker_positions"]
        assert len(rows) == 1
        assert (rows[0]["walker_id"], rows[0]["lat"], rows[0]["lng"]) == ("walker-w", 1.21, -77.28)
        assert get_home(fake_client).json()["online"] is True

    def test_going_online_twice_keeps_one_row(self, fake_client, data):
        position(data, seconds_ago=500)
        availability(fake_client, {"online": True, "lat": 1.3, "lng": -77.3})
        rows = [r for r in data.tables["walker_positions"] if r["walker_id"] == "walker-w"]
        assert len(rows) == 1 and rows[0]["lat"] == 1.3
        assert rows[0]["updated_at"] == FIXED_NOW.isoformat()

    def test_going_offline_erases_my_location(self, fake_client, data):
        position(data)
        position(data, walker_id="walker-v")
        res = availability(fake_client, {"online": False})
        assert res.status_code == 200
        assert res.json() == {"online": False, "updatedAt": None}
        assert [r["walker_id"] for r in data.tables["walker_positions"]] == ["walker-v"]

    def test_going_offline_when_already_offline_is_fine(self, fake_client, data):
        assert availability(fake_client, {"online": False}).status_code == 200

    @pytest.mark.parametrize("body", [
        {"online": True}, {"online": True, "lat": 1.2}, {"online": True, "lat": 91, "lng": 0},
        {"online": True, "lat": 0, "lng": 181}, {"online": "yes", "lat": 1, "lng": 1}, {},
    ])
    def test_validation(self, fake_client, data, body):
        assert availability(fake_client, body).status_code == 400
        assert not data.tables.get("walker_positions")

    def test_ten_switches_a_minute(self, fake_client, data):
        reset_all_rate_limits()
        codes = [availability(fake_client, {"online": False}).status_code for _ in range(11)]
        assert codes[:10] == [200] * 10 and codes[10] == 429


class TestEarnings:
    @pytest.fixture
    def history(self, data):
        walk(data, "today", "done", TODAY_10AM, price=20000)
        walk(data, "monday", "done", MONDAY, price=10000)
        offer(data, "monday", 15000)
        walk(data, "sunday", "done", LAST_SUNDAY, price=30000)
        walk(data, "august", "done", AUGUST, price=40000)
        walk(data, "cancelled", "cancelled", TODAY_10AM, price=99000)
        walk(data, "theirs", "done", TODAY_10AM, walker_id="walker-v", price=99000)
        return data

    def test_week_is_the_default_and_starts_on_monday(self, fake_client, history):
        body = earnings(fake_client).json()
        assert {k: body[k] for k in body if k != "byDay"} == {
            "period": "week", "from": "2026-09-28", "to": "2026-10-04", "simulated": True,
            "commissionPct": 10, "walks": 2, "grossCop": 35000, "commissionCop": 3500, "netCop": 31500,
        }
        assert [day["date"] for day in body["byDay"]] == [
            "2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
        ]
        assert body["byDay"][0] == {"date": "2026-09-28", "walks": 1, "grossCop": 15000, "netCop": 13500}
        assert body["byDay"][1] == {"date": "2026-09-29", "walks": 0, "grossCop": 0, "netCop": 0}
        assert body["byDay"][2] == {"date": "2026-09-30", "walks": 1, "grossCop": 20000, "netCop": 18000}

    def test_day(self, fake_client, history):
        body = earnings(fake_client, "day").json()
        assert (body["from"], body["to"], body["walks"], body["grossCop"]) == ("2026-09-30", "2026-09-30", 1, 20000)
        assert len(body["byDay"]) == 1

    def test_month(self, fake_client, history):
        body = earnings(fake_client, "month").json()
        assert (body["from"], body["to"]) == ("2026-09-01", "2026-09-30")
        assert (body["walks"], body["grossCop"], body["netCop"]) == (3, 65000, 58500)
        assert len(body["byDay"]) == 30

    def test_unknown_period_is_400(self, fake_client, history):
        assert earnings(fake_client, "year").status_code == 400

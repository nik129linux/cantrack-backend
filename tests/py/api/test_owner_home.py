"""PR1 Inicio: GET /owners/{ownerId}/home — the whole home panel in one call.

The panel must never waterfall (one request, everything it shows), never show
another owner's data, and degrade to empty sections instead of failing.
"""

from datetime import timedelta

import pytest

from .conftest import FIXED_NOW, bearer

# FIXED_NOW = 2026-10-01T00:00:00+00:00
SOON = "2026-10-01T15:00:00+00:00"
LATER = "2026-10-02T15:00:00+00:00"
LATEST = "2026-10-03T15:00:00+00:00"


def walk(fake, walk_id, status, requested_time=SOON, owner_id="owner-a", dog_id="dog-luna",
         walker_id=None, price=20000):
    return fake.seed(
        "walk_requests", id=walk_id, owner_id=owner_id, walker_id=walker_id, dog_id=dog_id,
        status=status, requested_time=requested_time, window_end=requested_time,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=0.75,
        pickup_lat=1.2136, pickup_lng=-77.2811, price_cop=price,
        created_at="2026-09-30T00:00:00+00:00",
    )


@pytest.fixture
def home_data(fake):
    fake.seed("dogs", id="dog-luna", owner_id="owner-a", name="Luna")
    fake.seed("dogs", id="dog-max", owner_id="owner-a", name="Max")
    fake.seed("dogs", id="dog-other", owner_id="owner-b", name="NotYours")
    fake.seed("walker_profiles", walker_id="walker-w", display_name="Mateo", bio=None,
              service_area="Centro", price_per_walk=15000)
    return fake


def get_home(client, token="token-a", owner_id="owner-a"):
    return client.get(f"/owners/{owner_id}/home", headers=bearer(token))


class TestAccess:
    def test_another_owner_gets_403(self, fake_client, home_data):
        assert get_home(fake_client, token="token-b").status_code == 403

    def test_walker_gets_403(self, fake_client, home_data):
        assert get_home(fake_client, token="token-w", owner_id="walker-w").status_code == 403

    def test_no_token_is_401(self, fake_client):
        assert fake_client.get("/owners/owner-a/home").status_code == 401


class TestEmptyAccount:
    def test_new_owner_gets_empty_sections_not_errors(self, fake_client):
        res = get_home(fake_client)
        assert res.status_code == 200
        assert res.json() == {
            "greetingName": "owner-a",
            "activeWalk": None,
            "nextWalk": None,
            "upcoming": [],
            "openRequests": [],
            "counts": {"dogs": 0, "walksDone": 0, "openRequests": 0},
        }


class TestGreeting:
    def test_profile_display_name_wins(self, fake_client, home_data):
        home_data.seed("owner_profiles", owner_id="owner-a", display_name="Camila")
        assert get_home(fake_client).json()["greetingName"] == "Camila"

    def test_falls_back_to_the_email_local_part(self, fake_client, home_data):
        assert get_home(fake_client).json()["greetingName"] == "owner-a"


class TestSections:
    def test_next_walk_is_the_soonest_assigned_one(self, fake_client, home_data):
        walk(home_data, "w-later", "assigned", LATER, walker_id="walker-w")
        walk(home_data, "w-soon", "assigned", SOON, walker_id="walker-w", dog_id="dog-max")
        body = get_home(fake_client).json()
        assert body["nextWalk"] == {
            "id": "w-soon", "dogName": "Max", "walkerName": "Mateo",
            "requestedTime": SOON, "durationHours": 0.75, "priceCop": 20000,
        }
        assert [item["id"] for item in body["upcoming"]] == ["w-soon", "w-later"]

    def test_upcoming_is_capped_at_five(self, fake_client, home_data):
        for day in range(7):
            walk(home_data, f"w{day}", "assigned", f"2026-10-0{day + 2}T15:00:00+00:00", walker_id="walker-w")
        assert len(get_home(fake_client).json()["upcoming"]) == 5

    def test_walker_without_profile_has_null_name(self, fake_client, home_data):
        walk(home_data, "w1", "assigned", walker_id="walker-unknown")
        assert get_home(fake_client).json()["nextWalk"]["walkerName"] is None

    def test_open_requests_carry_their_offer_count(self, fake_client, home_data):
        walk(home_data, "open-1", "open", LATER)
        walk(home_data, "open-2", "open", SOON, dog_id="dog-max")
        for walker_id in ("w1", "w2", "w3"):
            home_data.seed("walk_offers", walk_request_id="open-1", walker_id=walker_id, price_cop=18000,
                           eta_min=10, distance_km=1.0, created_at="2026-09-30T00:00:00+00:00")
        body = get_home(fake_client).json()
        assert body["openRequests"] == [
            {"id": "open-2", "dogName": "Max", "requestedTime": SOON, "priceCop": 20000,
             "offerCount": 0, "deadlineAt": "2026-10-01T00:10:00+00:00"},
            {"id": "open-1", "dogName": "Luna", "requestedTime": LATER, "priceCop": 20000,
             "offerCount": 3, "deadlineAt": "2026-10-01T00:10:00+00:00"},
        ]

    def test_active_walk_shows_elapsed_minutes_and_last_position(self, fake_client, home_data, fake_clock):
        walk(home_data, "live", "in_walk", SOON, walker_id="walker-w")
        home_data.seed("walk_timeline", walk_request_id="live", kind="check_in",
                       at="2026-10-01T15:00:00+00:00", payload={})
        home_data.seed("walk_gps_points", walk_request_id="live", at_epoch=1.0, lat=1.0, lng=-77.0)
        home_data.seed("walk_gps_points", walk_request_id="live", at_epoch=9.0, lat=1.2, lng=-77.2)
        home_data.seed("walk_gps_points", walk_request_id="live", at_epoch=5.0, lat=1.1, lng=-77.1)
        fake_clock.now = FIXED_NOW + timedelta(hours=15, minutes=23, seconds=50)
        assert get_home(fake_client).json()["activeWalk"] == {
            "id": "live", "dogName": "Luna", "walkerName": "Mateo",
            "startedAt": "2026-10-01T15:00:00+00:00", "minutesElapsed": 23,
            "lastPosition": {"lat": 1.2, "lng": -77.2, "atEpoch": 9.0},
        }

    def test_active_walk_without_gps_or_check_in_row(self, fake_client, home_data):
        walk(home_data, "live", "in_walk", SOON, walker_id="walker-w")
        active = get_home(fake_client).json()["activeWalk"]
        assert active["startedAt"] is None
        assert active["minutesElapsed"] == 0
        assert active["lastPosition"] is None

    def test_counts(self, fake_client, home_data):
        walk(home_data, "d1", "done", walker_id="walker-w")
        walk(home_data, "d2", "done", walker_id="walker-w")
        walk(home_data, "o1", "open")
        walk(home_data, "c1", "cancelled")
        assert get_home(fake_client).json()["counts"] == {"dogs": 2, "walksDone": 2, "openRequests": 1}


class TestIsolation:
    def test_other_owners_walks_never_appear(self, fake_client, home_data):
        walk(home_data, "theirs", "assigned", owner_id="owner-b", dog_id="dog-other", walker_id="walker-w")
        walk(home_data, "theirs-open", "open", owner_id="owner-b", dog_id="dog-other")
        body = get_home(fake_client).json()
        assert body["nextWalk"] is None and body["openRequests"] == []
        assert "NotYours" not in str(body)

    def test_every_walk_request_query_is_scoped_to_the_owner(self, fake_client, home_data):
        walk(home_data, "w1", "assigned", walker_id="walker-w")
        home_data.queries.clear()
        get_home(fake_client)
        walk_queries = [q for q in home_data.queries if q[0] == "walk_requests"]
        assert walk_queries
        assert all(("owner_id", "owner-a") in q[2] for q in walk_queries)

    def test_one_round_trip_budget(self, fake_client, home_data):
        # The panel is one call; it must not fan out per walk beyond a small fixed budget.
        for index in range(5):
            walk(home_data, f"w{index}", "assigned", f"2026-10-0{index + 2}T15:00:00+00:00", walker_id="walker-w")
            walk(home_data, f"o{index}", "open", f"2026-10-0{index + 2}T16:00:00+00:00")
        home_data.queries.clear()
        get_home(fake_client)
        assert len(home_data.queries) <= 12

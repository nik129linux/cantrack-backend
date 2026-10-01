"""S2: `POST /plans/suggest` — the walker's pickup plan for one LOCAL day.

Body: {"date": "YYYY-MM-DD", "utcOffsetMinutes": int = 0}. The plan is a
suggestion and is NOT persisted (no route row is written; "Accept plan" on
the web calls the existing POST /routes).

Pinned contract (every decision is also declared in the PR body):
- only the caller's ACCEPTED requests whose (UTC-normalized) requestedTime
  falls inside the local day [date 00:00, date+1 00:00) shifted by
  utcOffsetMinutes are planned; another walker's never appear;
- ordering: stops are clustered by requested time with the ANCHOR rule —
  sorted by time, a stop joins the current cluster only while it is within
  15 minutes (inclusive) of the cluster's FIRST stop, otherwise it anchors a
  new cluster (a chain rule would let a whole morning collapse into one
  cluster that nearest-neighbour can reorder by hours); clusters keep time
  order; inside a cluster the order is NEAREST NEIGHBOUR from the previous
  stop (haversine km), starting at the earliest stop; ties break by earlier
  requestedTime, then requestId (lexicographic). Invariant (property-tested
  over 300 random days): for every i < j in the plan,
  requestedTime[i] <= requestedTime[j] + 15 minutes;
- eta: the first stop's eta is its requestedTime; each next eta is
  max(its requestedTime, previous eta + leg km / 5 km/h), truncated to whole
  seconds, ISO-8601 UTC;
- legDistanceKm: 0.0 for the first stop, otherwise round(haversine, 2);
  totalDistanceKm: round(sum of RAW leg distances, 2);
- lateness: lateMinutes = ceil(max(0, eta - requestedTime) in minutes);
  late = lateMinutes > LATE_THRESHOLD_MINUTES (10); top-level feasible =
  no stop is late — an impossible schedule is never silently accepted;
- flags: ["reactive"] when the dog's questionnaire temperament is reactive,
  else [];
- groups: within each cluster, reactive dogs are ALWAYS alone; the other dogs
  are compatible when they are within GROUP_RADIUS_KM (1.5 km, inclusive) of
  each other — UnionFind unions every compatible pair, so components are
  proximity-connected transitively — and each component is chunked into
  consecutive runs of at most 4 in plan order; group numbers are consecutive
  integers assigned in order of first appearance along the whole plan
  (clusters never share a group);
- an empty day returns {"date": d, "stops": [], "totalDistanceKm": 0.0};
- bad date -> 400 exact message; owners -> 400; 401; 500 on database errors.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest
from postgrest.exceptions import APIError

from .conftest import bearer, make_user

DAY = "2026-10-05"
T10 = "2026-10-05T10:00:00+00:00"
T1005 = "2026-10-05T10:05:00+00:00"
T1010 = "2026-10-05T10:10:00+00:00"
T1015 = "2026-10-05T10:15:00+00:00"
T1016 = "2026-10-05T10:16:00+00:00"
T1020 = "2026-10-05T10:20:00+00:00"
T1025 = "2026-10-05T10:25:00+00:00"
T1030 = "2026-10-05T10:30:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"
T12 = "2026-10-05T12:00:00+00:00"

# Pins on a pure latitude line near Medellin: 0.01 degrees ~ 1.11195 km.
P0 = (6.2000, -75.5000)
P_NEAR = (6.2100, -75.5000)   # ~1.11 km north of P0 (compatible: <= 1.5 km)
P_FAR = (6.2500, -75.5000)    # ~5.56 km north of P0 (never compatible)
P_MID2 = (6.2200, -75.5000)   # ~2.22 km north of P0, ~1.11 km from P_NEAR
# Legs sized to land the lateness safely inside whole minutes (ceil is exact
# there for any sane earth radius): ~0.7917 km -> ~9.5 min walk -> ceil 10;
# ~0.8762 km -> ~10.5 min walk -> ceil 11.
P_LATE10 = (6.20712, -75.5000)
P_LATE11 = (6.20788, -75.5000)

PROFILE = {"displayName": "Nico Walks", "bio": None, "serviceArea": "Laureles",
           "pricePerWalk": 25000}

DATE_MESSAGE = {"detail": "date must be a valid YYYY-MM-DD date."}
ROLE_MESSAGE = {"detail": "Only walkers can suggest a plan."}


def api_error(message="connection lost"):
    return APIError({"message": message, "code": "XX000", "hint": None, "details": None})


@pytest.fixture
def second_walker(fake):
    fake.add_user("token-w2", make_user("walker-w2", "walker-w2@example.com", "walker"))


def save_profile(client, token="token-w", **over):
    res = client.put("/walker-profile", json={**PROFILE, **over}, headers=bearer(token))
    assert res.status_code == 200


def create_dog(client, name, token="token-a", breed="Mixed"):
    res = client.post("/dogs", json={"name": name, "breed": breed}, headers=bearer(token))
    assert res.status_code == 201
    return res.json()


def set_temperament(client, dog_id, temperament, token="token-a"):
    res = client.put(
        f"/dogs/{dog_id}/profile",
        json={"size": "medium", "temperament": temperament, "energy": "high",
              "leashTrained": True},
        headers=bearer(token),
    )
    assert res.status_code == 200


def seed_accepted(fake, dog_id, requested_time, pin=P0, *, rid=None, walker_id="walker-w",
                  status="accepted"):
    lat, lng = pin
    return fake.seed(
        "walk_requests",
        **({"id": rid} if rid is not None else {}),
        owner_id="owner-a", walker_id=walker_id, dog_id=dog_id, status=status,
        requested_time=requested_time, pickup_lat=lat, pickup_lng=lng,
        price_cop=25000, created_at="2026-10-01T00:00:00+00:00",
        responded_at="2026-10-01T00:00:00+00:00",
    )


def suggest(client, date=DAY, offset=None, token="token-w"):
    body = {"date": date}
    if offset is not None:
        body["utcOffsetMinutes"] = offset
    return client.post("/plans/suggest", json=body, headers=bearer(token))


class TestAuthAndRole:
    def test_no_token_is_401(self, fake_client):
        assert fake_client.post("/plans/suggest", json={"date": DAY}).status_code == 401

    def test_unknown_token_is_401(self, fake_client):
        assert fake_client.post("/plans/suggest", json={"date": DAY},
                                headers=bearer("nope")).status_code == 401

    def test_an_owner_gets_400(self, fake_client):
        res = suggest(fake_client, token="token-a")
        assert res.status_code == 400
        assert res.json() == ROLE_MESSAGE


class TestValidation:
    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"date": "05/10/2026"},
            {"date": "2026-13-01"},
            {"date": "2026-10-05", "utcOffsetMinutes": "abc"},
            {"date": "2026-10-05", "utcOffsetMinutes": 1441},
            {"date": "2026-10-05", "utcOffsetMinutes": -1441},
            {"date": None},
        ],
    )
    def test_invalid_body_is_400(self, fake_client, body):
        res = fake_client.post("/plans/suggest", json=body, headers=bearer("token-w"))
        assert res.status_code == 400

    def test_a_malformed_date_string_gets_the_exact_message(self, fake_client):
        res = fake_client.post("/plans/suggest", json={"date": "tomorrow"},
                               headers=bearer("token-w"))
        assert res.status_code == 400
        assert res.json() == DATE_MESSAGE


class TestEmptyDay:
    def test_no_accepted_requests_that_day(self, fake_client):
        save_profile(fake_client)
        res = suggest(fake_client)
        assert res.status_code == 200
        assert res.json() == {"date": DAY, "stops": [], "totalDistanceKm": 0.0,
                              "feasible": True}


class TestSingleStop:
    def test_exact_response_shape(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        seeded = seed_accepted(fake, dog["id"], T10)
        res = suggest(fake_client)
        assert res.status_code == 200
        assert res.json() == {
            "date": DAY,
            "stops": [
                {
                    "requestId": seeded["id"],
                    "dogId": dog["id"],
                    "dogName": "Firulais",
                    "requestedTime": T10,
                    "eta": T10,
                    "legDistanceKm": 0.0,
                    "flags": [],
                    "group": 1,
                    "lateMinutes": 0,
                    "late": False,
                }
            ],
            "totalDistanceKm": 0.0,
            "feasible": True,
        }


class TestOrdering:
    def test_separated_stops_come_in_requested_time_order_with_their_own_groups(
        self, fake_client, fake
    ):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        # stored out of order on purpose; the AVL keyed by requested time must
        # make the earliest stop the next due one
        noon = seed_accepted(fake, dog["id"], T12, rid="r-noon")
        eleven = seed_accepted(fake, dog["id"], T11, rid="r-eleven")
        ten = seed_accepted(fake, dog["id"], T10, rid="r-ten")

        body = suggest(fake_client).json()
        assert [stop["requestId"] for stop in body["stops"]] == [
            ten["id"], eleven["id"], noon["id"],
        ]
        assert [stop["group"] for stop in body["stops"]] == [1, 2, 3]
        assert [stop["eta"] for stop in body["stops"]] == [T10, T11, T12]
        assert [stop["legDistanceKm"] for stop in body["stops"]] == [0.0, 0.0, 0.0]
        assert body["totalDistanceKm"] == 0.0
        assert [stop["lateMinutes"] for stop in body["stops"]] == [0, 0, 0]
        assert [stop["late"] for stop in body["stops"]] == [False, False, False]
        assert body["feasible"] is True

    def test_a_cluster_within_15_minutes_is_ordered_by_nearest_neighbour(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        anchor = seed_accepted(fake, dog["id"], T10, pin=P0, rid="r-a")
        far = seed_accepted(fake, dog["id"], T1005, pin=P_FAR, rid="r-b")
        near = seed_accepted(fake, dog["id"], T1010, pin=P_NEAR, rid="r-c")

        body = suggest(fake_client).json()
        # time order would be a, b, c — nearest neighbour from the anchor picks
        # the 1.11 km stop before the 5.56 km one
        assert [stop["requestId"] for stop in body["stops"]] == [
            anchor["id"], near["id"], far["id"],
        ]
        legs = [stop["legDistanceKm"] for stop in body["stops"]]
        assert legs[0] == 0.0
        assert legs[1] == pytest.approx(1.11, abs=0.005)
        assert legs[2] == pytest.approx(4.45, abs=0.02)
        assert body["totalDistanceKm"] == pytest.approx(5.56, abs=0.02)
        # one cluster; A-C are ~1.11 km apart (compatible, GROUP_RADIUS_KM
        # 1.5) but C-B are ~4.45 km, so B walks alone: {A,C} = 1, {B} = 2
        assert [stop["group"] for stop in body["stops"]] == [1, 1, 2]

    def test_walking_legs_push_the_eta_past_the_requested_time(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        seed_accepted(fake, dog["id"], T10, pin=P0, rid="r-a")
        seed_accepted(fake, dog["id"], T1005, pin=P_FAR, rid="r-b")

        body = suggest(fake_client).json()
        second = body["stops"][1]
        # 5.56 km at 5 km/h ~ 66.7 min after 10:00 -> about 11:06:4x UTC;
        # the minute is exact for any sane earth radius, seconds are not
        assert second["eta"].startswith("2026-10-05T11:06:")
        assert second["eta"].endswith("+00:00")
        # eta 11:06:4x vs requested 10:05 -> 61m4xs -> ceil = 62 min late
        assert second["lateMinutes"] == 62
        assert second["late"] is True
        assert body["feasible"] is False

    def test_an_eta_never_goes_below_the_requested_time(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        seed_accepted(fake, dog["id"], T10, pin=P0, rid="r-a")
        seed_accepted(fake, dog["id"], T1030, pin=P0, rid="r-b")  # same pin, 30 min later

        body = suggest(fake_client).json()
        # 30 min apart -> two clusters; zero-distance legs keep etas == requested
        assert [stop["eta"] for stop in body["stops"]] == [T10, T1030]

    def test_nearest_neighbour_ties_break_by_time_then_request_id(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        anchor = seed_accepted(fake, dog["id"], T10, pin=P0, rid="r-anchor")
        later = seed_accepted(fake, dog["id"], T1010, pin=P_NEAR, rid="r-later")
        earlier = seed_accepted(fake, dog["id"], T1005, pin=P_NEAR, rid="r-earlier")

        body = suggest(fake_client).json()
        assert [stop["requestId"] for stop in body["stops"]] == [
            anchor["id"], earlier["id"], later["id"],
        ]

    def test_same_distance_same_time_ties_break_by_request_id(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        seed_accepted(fake, dog["id"], T10, pin=P0, rid="r-anchor")
        seed_accepted(fake, dog["id"], T1005, pin=P_NEAR, rid="r-b")
        seed_accepted(fake, dog["id"], T1005, pin=P_NEAR, rid="r-a")

        body = suggest(fake_client).json()
        assert [stop["requestId"] for stop in body["stops"]] == ["r-anchor", "r-a", "r-b"]


class TestClusters:
    def test_exactly_15_minutes_apart_is_the_same_cluster(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        seed_accepted(fake, dog["id"], T10, rid="r-a")
        seed_accepted(fake, dog["id"], T1015, rid="r-b")
        body = suggest(fake_client).json()
        assert [stop["group"] for stop in body["stops"]] == [1, 1]

    def test_more_than_15_minutes_apart_splits_clusters_and_groups(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        seed_accepted(fake, dog["id"], T10, rid="r-a")
        seed_accepted(fake, dog["id"], T1016, rid="r-b")
        body = suggest(fake_client).json()
        assert [stop["group"] for stop in body["stops"]] == [1, 2]

    def test_clusters_anchor_at_their_first_stop(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        # 10:00 -> 10:14 -> 10:28: 10:14 is within 15 min of the ANCHOR
        # (10:00), but 10:28 is 28 min after the anchor, so it opens a second
        # cluster. (The old chain rule merged all three and let nearest
        # neighbour reorder a whole morning — see the property test below.)
        seed_accepted(fake, dog["id"], T10, rid="r-a")
        seed_accepted(fake, dog["id"], "2026-10-05T10:14:00+00:00", rid="r-b")
        seed_accepted(fake, dog["id"], "2026-10-05T10:28:00+00:00", rid="r-c")
        body = suggest(fake_client).json()
        assert [stop["requestId"] for stop in body["stops"]] == ["r-a", "r-b", "r-c"]
        assert [stop["group"] for stop in body["stops"]] == [1, 1, 2]


class TestGroups:
    def test_reactive_dogs_are_flagged_and_never_grouped(self, fake_client, fake):
        save_profile(fake_client)
        friendly = create_dog(fake_client, "Firulais")
        reactive = create_dog(fake_client, "Rex")
        shy = create_dog(fake_client, "Luna")
        set_temperament(fake_client, friendly["id"], "friendly")
        set_temperament(fake_client, reactive["id"], "reactive")
        set_temperament(fake_client, shy["id"], "shy")
        seed_accepted(fake, friendly["id"], T10, rid="r-a")
        seed_accepted(fake, reactive["id"], T1005, rid="r-b")
        seed_accepted(fake, shy["id"], T1010, rid="r-c")

        body = suggest(fake_client).json()
        assert [stop["requestId"] for stop in body["stops"]] == ["r-a", "r-b", "r-c"]
        assert [stop["flags"] for stop in body["stops"]] == [[], ["reactive"], []]
        # the two compatible dogs share group 1; the reactive one is alone in 2
        assert [stop["group"] for stop in body["stops"]] == [1, 2, 1]

    def test_two_reactive_dogs_are_never_grouped_together(self, fake_client, fake):
        save_profile(fake_client)
        friendly_a = create_dog(fake_client, "Firulais")
        reactive_a = create_dog(fake_client, "Rex")
        reactive_b = create_dog(fake_client, "Toby")
        friendly_b = create_dog(fake_client, "Kira")
        set_temperament(fake_client, friendly_a["id"], "friendly")
        set_temperament(fake_client, reactive_a["id"], "reactive")
        set_temperament(fake_client, reactive_b["id"], "reactive")
        set_temperament(fake_client, friendly_b["id"], "shy")
        seed_accepted(fake, friendly_a["id"], T10, rid="r-a")
        seed_accepted(fake, reactive_a["id"], T1005, rid="r-b")
        seed_accepted(fake, reactive_b["id"], T1010, rid="r-c")
        seed_accepted(fake, friendly_b["id"], T1015, rid="r-d")

        body = suggest(fake_client).json()
        assert [stop["requestId"] for stop in body["stops"]] == ["r-a", "r-b", "r-c", "r-d"]
        assert [stop["group"] for stop in body["stops"]] == [1, 2, 3, 1]

    def test_a_group_holds_at_most_4_dogs_chunked_in_plan_order(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        # all six within 15 min of the anchor (10:00..10:15) -> ONE cluster,
        # so the 4+2 split can only come from the group-size cap
        times = [T10, "2026-10-05T10:03:00+00:00", "2026-10-05T10:06:00+00:00",
                 "2026-10-05T10:09:00+00:00", "2026-10-05T10:12:00+00:00", T1015]
        ids = ["r-1", "r-2", "r-3", "r-4", "r-5", "r-6"]
        for rid, when in zip(ids, times):
            seed_accepted(fake, dog["id"], when, rid=rid)

        body = suggest(fake_client).json()
        assert [stop["requestId"] for stop in body["stops"]] == ids
        assert [stop["group"] for stop in body["stops"]] == [1, 1, 1, 1, 2, 2]

    def test_group_numbers_run_across_clusters_in_first_appearance_order(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        # cluster 1: 10:00 + 10:05 (compatible pair); cluster 2: 11:00 alone
        seed_accepted(fake, dog["id"], T10, rid="r-a")
        seed_accepted(fake, dog["id"], T1005, rid="r-b")
        seed_accepted(fake, dog["id"], T11, rid="r-c")
        body = suggest(fake_client).json()
        assert [stop["group"] for stop in body["stops"]] == [1, 1, 2]


    def test_a_far_pair_in_one_cluster_gets_two_groups(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        seed_accepted(fake, dog["id"], T10, pin=P0, rid="r-a")
        seed_accepted(fake, dog["id"], T1005, pin=P_FAR, rid="r-b")  # ~5.56 km > 1.5
        body = suggest(fake_client).json()
        assert [stop["requestId"] for stop in body["stops"]] == ["r-a", "r-b"]
        assert [stop["group"] for stop in body["stops"]] == [1, 2]

    def test_proximity_is_transitive_inside_a_cluster(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        # A-B ~1.11 km, B-C ~1.11 km, A-C ~2.22 km (> 1.5): UnionFind connects
        # {A,B,C} through B, so all three walk in one group
        seed_accepted(fake, dog["id"], T10, pin=P0, rid="r-a")
        seed_accepted(fake, dog["id"], T1005, pin=P_NEAR, rid="r-b")
        seed_accepted(fake, dog["id"], T1010, pin=P_MID2, rid="r-c")
        body = suggest(fake_client).json()
        assert [stop["requestId"] for stop in body["stops"]] == ["r-a", "r-b", "r-c"]
        assert [stop["group"] for stop in body["stops"]] == [1, 1, 1]


class TestLateAndFeasibility:
    """lateMinutes = ceil(max(0, eta - requestedTime)); late = lateMinutes > 10
    (LATE_THRESHOLD_MINUTES); feasible = no late stop. The legs below are sized
    so the ceil lands mid-minute (~9.5 and ~10.5 minutes late), where any sane
    earth radius gives the same integer."""

    def test_exactly_10_minutes_late_is_not_late_and_the_plan_stays_feasible(
        self, fake_client, fake
    ):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        seed_accepted(fake, dog["id"], T10, pin=P0, rid="r-a")
        seed_accepted(fake, dog["id"], T10, pin=P_LATE10, rid="r-b")
        body = suggest(fake_client).json()
        first, second = body["stops"]
        assert [first["lateMinutes"], first["late"]] == [0, False]
        assert second["lateMinutes"] == 10
        assert second["late"] is False
        assert body["feasible"] is True

    def test_11_minutes_late_is_late_and_makes_the_plan_infeasible(
        self, fake_client, fake
    ):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        seed_accepted(fake, dog["id"], T10, pin=P0, rid="r-a")
        seed_accepted(fake, dog["id"], T10, pin=P_LATE11, rid="r-b")
        body = suggest(fake_client).json()
        second = body["stops"][1]
        assert second["lateMinutes"] == 11
        assert second["late"] is True
        assert body["feasible"] is False


class TestPlanInvariants:
    """Seeded property test (review must-fix 1): 300 random days of 3-25 stops.
    With the anchor rule the plan can never drift more than 15 minutes from
    requested-time order, and the output must not depend on storage order."""

    def test_300_random_days_stay_within_15_minutes_of_time_order_and_are_deterministic(
        self, fake, fake_client
    ):
        rng = random.Random(20261003)
        dog = fake.seed("dogs", owner_id="owner-a", name="Property Dog", breed="Mixed")
        base_day = datetime(2026, 11, 1, tzinfo=timezone.utc)

        for day_index in range(300):
            day = base_day + timedelta(days=day_index)
            date = day.strftime("%Y-%m-%d")
            count = rng.randint(3, 25)
            for _ in range(count):
                minute = rng.randrange(0, 24 * 60)
                when = (day + timedelta(minutes=minute)).isoformat()
                lat = 6.2 + rng.randint(0, 40) * 0.005
                lng = -75.5 + rng.randint(0, 40) * 0.005
                seed_accepted(fake, dog["id"], when, pin=(lat, lng))

            plan = suggest(fake_client, date=date).json()
            stops = plan["stops"]
            assert len(stops) == count, date

            times = [datetime.fromisoformat(stop["requestedTime"]) for stop in stops]
            for i in range(len(times)):
                for j in range(i + 1, len(times)):
                    assert times[i] <= times[j] + timedelta(minutes=15), (
                        f"{date}: stop {i} is requested more than 15 min after stop {j}"
                    )

            rng.shuffle(fake.tables["walk_requests"])
            assert suggest(fake_client, date=date).json() == plan, date


class TestScope:
    def test_only_accepted_requests_are_planned(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        seed_accepted(fake, dog["id"], T10, rid="r-accepted")
        seed_accepted(fake, dog["id"], T1005, rid="r-pending", status="pending")
        seed_accepted(fake, dog["id"], T1010, rid="r-declined", status="declined")
        seed_accepted(fake, dog["id"], T1015, rid="r-cancelled", status="cancelled")

        body = suggest(fake_client).json()
        assert [stop["requestId"] for stop in body["stops"]] == ["r-accepted"]

    def test_a_request_cancelled_after_accept_leaves_the_plan(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        res = fake_client.post(
            "/requests",
            json={"walkerId": "walker-w", "dogId": dog["id"], "requestedTime": T10,
                  "pickupLat": P0[0], "pickupLng": P0[1]},
            headers=bearer("token-a"),
        )
        rid = res.json()["id"]
        fake_client.post(f"/requests/{rid}/accept", headers=bearer("token-w"))
        assert [s["requestId"] for s in suggest(fake_client).json()["stops"]] == [rid]

        fake_client.post(f"/requests/{rid}/cancel", headers=bearer("token-a"))
        assert suggest(fake_client).json()["stops"] == []

    def test_another_walkers_requests_never_appear(self, fake_client, fake, second_walker):
        save_profile(fake_client)
        save_profile(fake_client, token="token-w2", displayName="Ana Packs")
        dog = create_dog(fake_client, "Firulais")
        seed_accepted(fake, dog["id"], T10, rid="r-mine")
        seed_accepted(fake, dog["id"], T1005, rid="r-theirs", walker_id="walker-w2")

        body = suggest(fake_client).json()
        assert [stop["requestId"] for stop in body["stops"]] == ["r-mine"]

    def test_the_plan_queries_are_scoped_to_the_caller(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        seed_accepted(fake, dog["id"], T10)
        fake.queries.clear()
        suggest(fake_client)
        scoped = [q for q in fake.queries if q[0] == "walk_requests" and q[1] != "insert"]
        assert scoped
        for _table, _op, filters in scoped:
            assert ("walker_id", "walker-w") in filters

    def test_suggesting_persists_nothing(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        seeded = seed_accepted(fake, dog["id"], T10)
        fake.queries.clear()

        res = suggest(fake_client)
        assert res.status_code == 200
        assert fake.tables.get("routes", []) == []
        assert not [q for q in fake.queries if q[1] == "insert"]
        stored = fake.tables["walk_requests"][0]
        assert stored["status"] == "accepted"
        assert stored["id"] == seeded["id"]


class TestLocalDay:
    def test_the_offset_shifts_the_day_window(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        seed_accepted(fake, dog["id"], "2026-10-05T04:59:59+00:00", rid="r-out-early")
        seed_accepted(fake, dog["id"], "2026-10-05T05:00:00+00:00", rid="r-in-first")
        seed_accepted(fake, dog["id"], "2026-10-06T04:59:59+00:00", rid="r-in-last")
        seed_accepted(fake, dog["id"], "2026-10-06T05:00:00+00:00", rid="r-out-late")

        # UTC-5: the local day 2026-10-05 is [05:00Z, next-day 05:00Z)
        local = suggest(fake_client, offset=-300).json()
        assert [stop["requestId"] for stop in local["stops"]] == ["r-in-first", "r-in-last"]

        # offset 0 (explicit) and the default agree: only the 10-05 UTC rows
        utc = suggest(fake_client, offset=0).json()
        assert [stop["requestId"] for stop in utc["stops"]] == ["r-out-early", "r-in-first"]
        default = suggest(fake_client).json()
        assert [stop["requestId"] for stop in default["stops"]] == ["r-out-early", "r-in-first"]

    def test_a_non_utc_input_is_windowed_and_echoed_in_utc(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        res = fake_client.post(
            "/requests",
            json={"walkerId": "walker-w", "dogId": dog["id"],
                  "requestedTime": "2026-10-05T05:00:00-05:00",
                  "pickupLat": P0[0], "pickupLng": P0[1]},
            headers=bearer("token-a"),
        )
        assert res.status_code == 201
        assert res.json()["requestedTime"] == "2026-10-05T10:00:00+00:00"
        fake_client.post(f"/requests/{res.json()['id']}/accept", headers=bearer("token-w"))

        body = suggest(fake_client).json()
        assert len(body["stops"]) == 1
        assert body["stops"][0]["requestedTime"] == "2026-10-05T10:00:00+00:00"
        assert body["stops"][0]["eta"] == "2026-10-05T10:00:00+00:00"

    def test_other_days_are_empty(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client, "Firulais")
        seed_accepted(fake, dog["id"], T10)
        assert suggest(fake_client, date="2026-10-04").json()["stops"] == []
        assert suggest(fake_client, date="2026-10-06").json()["stops"] == []


class TestErrors:
    def test_database_error_is_500_with_message(self, fake_client, fake):
        save_profile(fake_client)
        fake.fail_with = api_error()
        res = suggest(fake_client)
        assert res.status_code == 500
        assert res.json() == {"detail": "connection lost"}

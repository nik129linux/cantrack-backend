"""PR2 Explorar: inDrive-style request flow for the owner.

Covers the fare suggestion (deterministic median, not AI), the owner's own
request list with cursor pagination, offers that explain their ranking, cheap
polling (ETag/304), the walker position heartbeat (the only writer now that RLS
is deny-all), the nearby-walker count, and durationMinutes on create.
"""

import base64
from datetime import timedelta

import pytest

from cantrack_api.bidding import DEFAULT_CONFIG

from .conftest import FIXED_NOW, bearer, make_user

T10 = "2026-10-05T10:00:00+00:00"
PASTO = (1.2136, -77.2811)
FAR = (1.40, -77.28)  # ~20 km north


def seed_dog(fake, dog_id="dog-luna", owner_id="owner-a", name="Luna"):
    return fake.seed("dogs", id=dog_id, owner_id=owner_id, name=name)


def seed_walk(fake, walk_id, status="open", owner_id="owner-a", dog_id="dog-luna", walker_id=None,
              price=20000, duration=1.0, created_at="2026-09-30T00:00:00+00:00", where=PASTO,
              requested_time=T10):
    return fake.seed(
        "walk_requests", id=walk_id, owner_id=owner_id, walker_id=walker_id, dog_id=dog_id, status=status,
        requested_time=requested_time, window_end=T10, deadline_at="2026-10-01T00:10:00+00:00",
        duration_hours=duration, pickup_lat=where[0], pickup_lng=where[1], price_cop=price,
        created_at=created_at,
    )


def seed_offer(fake, walk_id, walker_id, price, eta=10.0, distance=1.0, created_at="2026-10-01T00:00:00+00:00"):
    return fake.seed("walk_offers", walk_request_id=walk_id, walker_id=walker_id, price_cop=price,
                     eta_min=eta, distance_km=distance, created_at=created_at)


class TestFareSuggestion:
    def ask(self, client, minutes=45, where=PASTO, token="token-a"):
        return client.get("/bidding/fare-suggestion",
                          params={"durationMinutes": minutes, "lat": where[0], "lng": where[1]},
                          headers=bearer(token))

    @pytest.mark.parametrize("minutes,expected", [(30, 10000), (45, 15000), (60, 20000), (120, 40000)])
    def test_default_is_330_cop_a_minute_rounded_to_1000(self, fake_client, minutes, expected):
        res = self.ask(fake_client, minutes)
        assert res.status_code == 200
        assert res.json() == {"suggestedCop": expected, "basis": "default", "sampleSize": 0,
                              "minCop": 8000, "stepCop": 1000}

    def _market(self, fake, prices_per_hour, where=PASTO, created_at="2026-09-20T00:00:00+00:00"):
        seed_dog(fake)
        for index, price in enumerate(prices_per_hour):
            walk_id = f"m{index}-{where[0]}"
            seed_walk(fake, walk_id, status="done", walker_id=f"wk{index}", created_at=created_at, where=where)
            seed_offer(fake, walk_id, f"wk{index}", price)

    def test_five_nearby_agreed_prices_give_a_market_median(self, fake_client, fake):
        self._market(fake, [18000, 30000, 24000, 12000, 26000])  # per hour; median 24000/h
        body = self.ask(fake_client, 45).json()
        assert body["basis"] == "market"
        assert body["sampleSize"] == 5
        assert body["suggestedCop"] == 18000  # 24000/60*45

    def test_the_agreed_price_is_the_winning_offer_not_the_posted_one(self, fake_client, fake):
        self._market(fake, [24000] * 5)
        for row in fake.tables["walk_requests"]:
            row["price_cop"] = 999000  # what the owner posted; the walk went for the offer price
        assert self.ask(fake_client, 60).json()["suggestedCop"] == 24000

    def test_four_samples_fall_back_to_default(self, fake_client, fake):
        self._market(fake, [24000] * 4)
        assert self.ask(fake_client, 45).json()["basis"] == "default"

    def test_far_or_old_walks_do_not_count(self, fake_client, fake):
        self._market(fake, [90000] * 5, where=FAR)
        self._market(fake, [90000] * 5, created_at="2026-07-01T00:00:00+00:00")
        assert self.ask(fake_client, 45).json()["basis"] == "default"

    def test_open_or_cancelled_walks_do_not_count(self, fake_client, fake):
        seed_dog(fake)
        for index in range(6):
            seed_walk(fake, f"x{index}", status="cancelled" if index % 2 else "open", walker_id=f"wk{index}")
            seed_offer(fake, f"x{index}", f"wk{index}", 90000)
        assert self.ask(fake_client, 45).json()["basis"] == "default"

    @pytest.mark.parametrize("minutes", [0, 29, 50, 121, 600])
    def test_only_offered_durations(self, fake_client, minutes):
        assert self.ask(fake_client, minutes).status_code == 400

    def test_walkers_cannot_ask(self, fake_client):
        assert self.ask(fake_client, token="token-w").status_code == 403


class TestDurationMinutes:
    def body(self, **over):
        return {"dogId": "dog-luna", "windowStart": T10, "priceCop": 15000,
                "pickupLat": PASTO[0], "pickupLng": PASTO[1], **over}

    def test_minutes_are_stored_as_hours(self, fake_client, fake):
        seed_dog(fake)
        res = fake_client.post("/bidding/requests", json=self.body(durationMinutes=45), headers=bearer("token-a"))
        assert res.status_code == 201
        assert res.json()["durationHours"] == 0.75

    def test_hours_still_work(self, fake_client, fake):
        seed_dog(fake)
        res = fake_client.post("/bidding/requests", json=self.body(durationHours=1), headers=bearer("token-a"))
        assert res.status_code == 201

    @pytest.mark.parametrize("extra", [{}, {"durationHours": 1, "durationMinutes": 60}, {"durationMinutes": 50}])
    def test_exactly_one_valid_duration(self, fake_client, fake, extra):
        seed_dog(fake)
        assert fake_client.post("/bidding/requests", json=self.body(**extra), headers=bearer("token-a")).status_code == 400


class TestOwnerRequestList:
    def list(self, client, token="token-a", owner="owner-a", **params):
        return client.get(f"/owners/{owner}/requests", params=params, headers=bearer(token))

    def test_lists_only_my_requests_newest_first_with_offer_counts(self, fake_client, fake):
        seed_dog(fake)
        seed_dog(fake, "dog-b", "owner-b", "Theirs")
        seed_walk(fake, "old", created_at="2026-09-01T00:00:00+00:00")
        seed_walk(fake, "new", created_at="2026-09-30T00:00:00+00:00", status="assigned", walker_id="walker-w")
        seed_walk(fake, "theirs", owner_id="owner-b", dog_id="dog-b")
        seed_offer(fake, "old", "w1", 15000)
        seed_offer(fake, "old", "w2", 16000)
        body = self.list(fake_client).json()
        assert [item["id"] for item in body["items"]] == ["new", "old"]
        assert body["items"][1] == {
            "id": "old", "status": "open", "dogId": "dog-luna", "dogName": "Luna", "requestedTime": T10,
            "durationHours": 1.0, "priceCop": 20000, "offerCount": 2,
            "deadlineAt": "2026-10-01T00:10:00+00:00", "walkerId": None,
        }
        assert body["nextCursor"] is None

    def test_status_filter(self, fake_client, fake):
        seed_dog(fake)
        seed_walk(fake, "a", status="open")
        seed_walk(fake, "b", status="done", walker_id="walker-w")
        assert [i["id"] for i in self.list(fake_client, status="done").json()["items"]] == ["b"]

    def test_cursor_pagination_walks_every_row_once(self, fake_client, fake):
        seed_dog(fake)
        for index in range(5):
            seed_walk(fake, f"w{index}", created_at=f"2026-09-2{index}T00:00:00+00:00")
        first = self.list(fake_client, limit=2).json()
        second = self.list(fake_client, limit=2, cursor=first["nextCursor"]).json()
        third = self.list(fake_client, limit=2, cursor=second["nextCursor"]).json()
        ids = [i["id"] for page in (first, second, third) for i in page["items"]]
        assert ids == ["w4", "w3", "w2", "w1", "w0"]
        assert third["nextCursor"] is None

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 51}, {"status": "weird"},
                                        {"cursor": "not-base64!!"},
                                        {"cursor": base64.urlsafe_b64encode(b"garbage").decode()}])
    def test_bad_params_are_400(self, fake_client, params):
        assert self.list(fake_client, **params).status_code == 400

    def test_another_owner_is_403(self, fake_client):
        assert self.list(fake_client, token="token-b").status_code == 403


class TestOffersExplainTheirRanking:
    @pytest.fixture
    def offers(self, fake):
        fake.add_user("token-w2", make_user("walker-w2", "w2@example.com", "walker"))
        seed_dog(fake)
        seed_walk(fake, "walk-1")
        fake.seed("walker_profiles", walker_id="walker-w", display_name="Mateo", bio=None,
                  service_area=None, price_per_walk=15000)
        seed_offer(fake, "walk-1", "walker-w", 15000, distance=0.5)
        seed_offer(fake, "walk-1", "walker-w2", 22000, distance=1.8)
        for rating in (5, 4):
            fake.seed("walk_ratings", walk_request_id=f"r{rating}", walker_id="walker-w", rating=rating,
                      created_at="2026-09-01T00:00:00+00:00")
        return fake

    def get(self, client, headers=None):
        return client.get("/bidding/requests/walk-1/offers", headers={**bearer("token-a"), **(headers or {})})

    def test_each_offer_carries_its_walker_card(self, fake_client, offers):
        best = self.get(fake_client).json()[0]
        assert best["walkerId"] == "walker-w"
        assert best["walkerName"] == "Mateo"
        assert best["ratingCount"] == 2
        assert best["distanceKm"] == 0.5
        assert set(best["scoreParts"]) == {"price", "distance", "rating"}

    def test_walker_without_profile_has_null_name(self, fake_client, offers):
        second = self.get(fake_client).json()[1]
        assert second["walkerName"] is None and second["ratingCount"] == 0

    def test_score_parts_add_up_to_the_score(self, fake_client, offers):
        weights = DEFAULT_CONFIG
        for offer in self.get(fake_client).json():
            parts = offer["scoreParts"]
            assert all(0.0 <= value <= 1.0 for value in parts.values())
            weighted = (weights.price_weight * parts["price"] + weights.distance_weight * parts["distance"]
                        + weights.rating_weight * parts["rating"])
            assert round(weighted, 6) == round(offer["score"], 6)

    def test_unchanged_offers_answer_304(self, fake_client, offers):
        first = self.get(fake_client)
        etag = first.headers["ETag"]
        again = self.get(fake_client, {"If-None-Match": etag})
        assert again.status_code == 304
        assert again.content == b""

    def test_a_new_offer_changes_the_etag(self, fake_client, offers):
        etag = self.get(fake_client).headers["ETag"]
        seed_offer(offers, "walk-1", "walker-w3", 18000)
        res = self.get(fake_client, {"If-None-Match": etag})
        assert res.status_code == 200
        assert res.headers["ETag"] != etag


class TestWalkerPosition:
    def put(self, client, token="token-w", lat=PASTO[0], lng=PASTO[1]):
        return client.put("/walker-position", json={"lat": lat, "lng": lng}, headers=bearer(token))

    def test_walker_reports_and_overwrites_one_row(self, fake_client, fake):
        assert self.put(fake_client).status_code == 200
        res = self.put(fake_client, lat=1.22)
        assert res.json() == {"lat": 1.22, "lng": PASTO[1], "updatedAt": FIXED_NOW.isoformat()}
        rows = fake.tables["walker_positions"]
        assert len(rows) == 1 and rows[0]["walker_id"] == "walker-w"

    def test_owners_cannot_report(self, fake_client):
        assert self.put(fake_client, token="token-a").status_code == 403

    @pytest.mark.parametrize("lat,lng", [(91, 0), (0, 181)])
    def test_coordinates_are_validated(self, fake_client, lat, lng):
        assert self.put(fake_client, lat=lat, lng=lng).status_code == 400

    def test_at_most_four_reports_a_minute(self, fake_client):
        codes = [self.put(fake_client).status_code for _ in range(5)]
        assert codes == [200, 200, 200, 200, 429]


class TestNearbyWalkers:
    def ask(self, client, token="token-a"):
        return client.get("/bidding/nearby-walkers", params={"lat": PASTO[0], "lng": PASTO[1]},
                          headers=bearer(token))

    def test_counts_fresh_positions_inside_the_radius_only(self, fake_client, fake):
        fresh = FIXED_NOW.isoformat()
        stale = (FIXED_NOW - timedelta(minutes=31)).isoformat()
        fake.seed("walker_positions", walker_id="near-fresh", lat=PASTO[0] + 0.005, lng=PASTO[1], updated_at=fresh)
        fake.seed("walker_positions", walker_id="near-stale", lat=PASTO[0], lng=PASTO[1], updated_at=stale)
        fake.seed("walker_positions", walker_id="far-fresh", lat=FAR[0], lng=FAR[1], updated_at=fresh)
        body = self.ask(fake_client).json()
        assert body == {"count": 1, "radiusKm": DEFAULT_CONFIG.radius_km}

    def test_never_reveals_who_or_where(self, fake_client, fake):
        fake.seed("walker_positions", walker_id="walker-w", lat=PASTO[0], lng=PASTO[1],
                  updated_at=FIXED_NOW.isoformat())
        assert set(self.ask(fake_client).json()) == {"count", "radiusKm"}

    def test_walkers_cannot_ask(self, fake_client):
        assert self.ask(fake_client, token="token-w").status_code == 403


class TestLegacyFlowIsDeprecated:
    def test_old_request_routes_are_marked_deprecated(self, fake_client):
        paths = fake_client.get("/openapi.json").json()["paths"]
        assert paths["/requests"]["post"].get("deprecated") is True
        assert paths["/bidding/requests"]["post"].get("deprecated") in (None, False)

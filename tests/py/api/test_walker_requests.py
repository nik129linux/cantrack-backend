"""W2 Solicitudes (walker): InDrive-style inbox.

Only an ONLINE walker (heartbeat within 120 s) sees requests and may bid. A
walker answers the owner's fare with one tap ("accept") or a counter-offer,
can withdraw a pending offer, and follows every offer as pending/won/lost.
An offer whose price equals the fare IS an accept: the kind is derived, so no
column can disagree with the price.
"""

from datetime import timedelta

import pytest

from .conftest import FIXED_NOW, bearer, make_user

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"
FARE = 20000

OFFLINE = {"detail": "Go online to send offers."}
NO_OFFER = {"detail": "You have no offer on this request."}
NOT_OPEN = {"detail": "The request is no longer open."}


def seed_open(fake, request_id="walk-1", fare=FARE, owner_id="owner-a", dog_id="dog-1",
              status="open", walker_id=None, created_at="2026-10-01T00:00:00+00:00"):
    return fake.seed(
        "walk_requests", id=request_id, owner_id=owner_id, walker_id=walker_id, dog_id=dog_id,
        status=status, requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=fare, created_at=created_at,
    )


def place(fake, walker_id="walker-w", seconds_ago=0, lat=6.209, lng=-75.5):
    fake.seed("walker_positions", walker_id=walker_id, lat=lat, lng=lng,
              updated_at=(FIXED_NOW - timedelta(seconds=seconds_ago)).isoformat())


def offer_row(fake, request_id, price, walker_id="walker-w", created_at="2026-10-01T00:00:00+00:00"):
    return fake.seed("walk_offers", id=f"of-{request_id}-{walker_id}", walk_request_id=request_id,
                     walker_id=walker_id, price_cop=price, eta_min=10.0, distance_km=1.0,
                     created_at=created_at)


@pytest.fixture
def data(fake):
    fake.add_user("token-v", make_user("walker-v", "walker-v@example.com", "walker"))
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    fake.seed("dogs", id="dog-2", owner_id="owner-a", name="Luna")
    fake.seed("owner_profiles", owner_id="owner-a", display_name="Camila")
    return fake


def open_list(client, token="token-w"):
    return client.get("/bidding/requests/open", headers=bearer(token))


def send(client, body, request_id="walk-1", token="token-w"):
    return client.post(f"/bidding/requests/{request_id}/offers", json=body, headers=bearer(token))


def withdraw(client, request_id="walk-1", token="token-w"):
    return client.delete(f"/bidding/requests/{request_id}/offers/mine", headers=bearer(token))


def my_offers(client, status=None, token="token-w", walker_id="walker-w"):
    params = {"status": status} if status else {}
    return client.get(f"/walkers/{walker_id}/offers", params=params, headers=bearer(token))


class TestOnlineGate:
    def test_online_walker_sees_the_request(self, fake_client, data):
        seed_open(data)
        place(data, seconds_ago=90)
        assert [row["id"] for row in open_list(fake_client).json()] == ["walk-1"]

    def test_stale_heartbeat_sees_nothing(self, fake_client, data):
        seed_open(data)
        place(data, seconds_ago=121)
        assert open_list(fake_client).json() == []

    def test_offline_walker_cannot_bid(self, fake_client, data):
        seed_open(data)
        place(data, seconds_ago=600)
        res = send(fake_client, {"kind": "accept", "etaMin": 10})
        assert res.status_code == 409
        assert res.json() == OFFLINE
        assert not data.tables.get("walk_offers")

    def test_owner_nearby_count_skips_stale_walkers(self, fake_client, data):
        place(data, "walker-w", seconds_ago=60)
        place(data, "walker-v", seconds_ago=180)
        res = fake_client.get("/bidding/nearby-walkers", params={"lat": 6.2, "lng": -75.5},
                              headers=bearer("token-a"))
        assert res.json()["count"] == 1


class TestCard:
    def test_card_explains_the_request_without_leaking_the_owner(self, fake_client, data):
        seed_open(data)
        place(data)
        res = open_list(fake_client)
        card = res.json()[0]
        assert card["dogName"] == "Firulais"
        assert card["ownerName"] == "Camila"
        assert card["fareCop"] == FARE
        assert card["distanceKm"] == 1.0
        assert card["myOffer"] is None
        assert "owner-a@example.com" not in res.text

    def test_owner_without_profile_is_null(self, fake_client, data):
        data.tables["owner_profiles"].clear()
        seed_open(data)
        place(data)
        assert open_list(fake_client).json()[0]["ownerName"] is None

    def test_my_offer_stays_visible_on_the_card(self, fake_client, data):
        seed_open(data)
        place(data)
        offer_row(data, "walk-1", 24000)
        offer_row(data, "walk-1", 18000, walker_id="walker-v")
        assert open_list(fake_client).json()[0]["myOffer"] == {
            "id": "of-walk-1-walker-w", "priceCop": 24000, "kind": "counter",
        }


class TestAcceptOrCounter:
    def test_accept_takes_the_owner_fare(self, fake_client, data):
        seed_open(data)
        place(data)
        res = send(fake_client, {"kind": "accept", "etaMin": 10})
        assert res.status_code == 201
        assert res.json()["kind"] == "accept"
        assert res.json()["priceCop"] == FARE
        assert data.tables["walk_offers"][0]["price_cop"] == FARE

    def test_accept_with_another_price_is_400(self, fake_client, data):
        seed_open(data)
        place(data)
        assert send(fake_client, {"kind": "accept", "priceCop": 25000, "etaMin": 10}).status_code == 400

    def test_counter_is_stored_with_its_price(self, fake_client, data):
        seed_open(data)
        place(data)
        res = send(fake_client, {"kind": "counter", "priceCop": 23000, "etaMin": 10})
        assert res.status_code == 201
        assert (res.json()["kind"], res.json()["priceCop"]) == ("counter", 23000)

    def test_a_counter_equal_to_the_fare_is_an_accept(self, fake_client, data):
        seed_open(data)
        place(data)
        assert send(fake_client, {"kind": "counter", "priceCop": FARE, "etaMin": 10}).json()["kind"] == "accept"

    @pytest.mark.parametrize("price", [23500, 7000, 41000])
    def test_counter_bounds(self, fake_client, data, price):
        # multiple of 1000, at least 8000, at most 2x the fare (40000 here)
        seed_open(data)
        place(data)
        assert send(fake_client, {"kind": "counter", "priceCop": price, "etaMin": 10}).status_code == 400
        assert not data.tables.get("walk_offers")

    def test_counter_at_twice_the_fare_is_allowed(self, fake_client, data):
        seed_open(data)
        place(data)
        assert send(fake_client, {"kind": "counter", "priceCop": 40000, "etaMin": 10}).status_code == 201

    def test_counter_needs_a_price(self, fake_client, data):
        seed_open(data)
        place(data)
        assert send(fake_client, {"kind": "counter", "etaMin": 10}).status_code == 400

    def test_unknown_kind_is_400(self, fake_client, data):
        seed_open(data)
        place(data)
        assert send(fake_client, {"kind": "haggle", "priceCop": 20000, "etaMin": 10}).status_code == 400

    def test_legacy_body_without_kind_is_a_counter(self, fake_client, data):
        seed_open(data)
        place(data)
        res = send(fake_client, {"priceCop": 22000, "etaMin": 10})
        assert res.status_code == 201
        assert res.json()["kind"] == "counter"


class TestWithdraw:
    def test_withdraw_deletes_only_my_offer(self, fake_client, data):
        seed_open(data)
        offer_row(data, "walk-1", 24000)
        offer_row(data, "walk-1", 18000, walker_id="walker-v")
        assert withdraw(fake_client).status_code == 204
        assert [o["walker_id"] for o in data.tables["walk_offers"]] == ["walker-v"]

    def test_withdraw_without_an_offer_is_404(self, fake_client, data):
        seed_open(data)
        res = withdraw(fake_client)
        assert res.status_code == 404
        assert res.json() == NO_OFFER

    def test_withdraw_after_the_owner_picked_is_409(self, fake_client, data):
        seed_open(data, status="assigned", walker_id="walker-w")
        offer_row(data, "walk-1", 24000)
        res = withdraw(fake_client)
        assert res.status_code == 409
        assert res.json() == NOT_OPEN
        assert len(data.tables["walk_offers"]) == 1

    def test_an_owner_cannot_withdraw(self, fake_client, data):
        seed_open(data)
        offer_row(data, "walk-1", 24000)
        assert withdraw(fake_client, token="token-a").status_code == 403
        assert len(data.tables["walk_offers"]) == 1

    def test_after_withdrawing_i_can_bid_again(self, fake_client, data):
        seed_open(data)
        place(data)
        offer_row(data, "walk-1", 24000)
        withdraw(fake_client)
        assert send(fake_client, {"kind": "accept", "etaMin": 10}).status_code == 201


class TestMyOffers:
    @pytest.fixture
    def offers(self, data):
        seed_open(data, "r-open", created_at="2026-09-30T10:00:00+00:00")
        seed_open(data, "r-won", status="assigned", walker_id="walker-w", dog_id="dog-2",
                  created_at="2026-09-30T11:00:00+00:00")
        seed_open(data, "r-lost", status="assigned", walker_id="walker-v",
                  created_at="2026-09-30T12:00:00+00:00")
        seed_open(data, "r-expired", status="expired", created_at="2026-09-30T13:00:00+00:00")
        seed_open(data, "r-other", created_at="2026-09-30T14:00:00+00:00")
        offer_row(data, "r-open", FARE, created_at="2026-09-30T10:05:00+00:00")
        offer_row(data, "r-won", 25000, created_at="2026-09-30T11:05:00+00:00")
        offer_row(data, "r-lost", 30000, created_at="2026-09-30T12:05:00+00:00")
        offer_row(data, "r-expired", 22000, created_at="2026-09-30T13:05:00+00:00")
        offer_row(data, "r-other", 21000, walker_id="walker-v", created_at="2026-09-30T14:05:00+00:00")
        return data

    def test_all_my_offers_newest_first_with_their_outcome(self, fake_client, offers):
        res = my_offers(fake_client)
        assert res.status_code == 200
        assert [(o["requestId"], o["status"]) for o in res.json()] == [
            ("r-expired", "lost"), ("r-lost", "lost"), ("r-won", "won"), ("r-open", "pending"),
        ]

    def test_item_shape(self, fake_client, offers):
        won = [o for o in my_offers(fake_client).json() if o["requestId"] == "r-won"][0]
        assert won == {
            "offerId": "of-r-won-walker-w", "requestId": "r-won", "dogName": "Luna",
            "requestedTime": T10, "durationHours": 1.0, "fareCop": FARE, "priceCop": 25000,
            "kind": "counter", "status": "won", "requestStatus": "assigned",
        }

    @pytest.mark.parametrize("status, ids", [
        ("pending", ["r-open"]), ("won", ["r-won"]), ("lost", ["r-expired", "r-lost"]),
    ])
    def test_filter(self, fake_client, offers, status, ids):
        assert [o["requestId"] for o in my_offers(fake_client, status).json()] == ids

    def test_unknown_status_is_400(self, fake_client, offers):
        assert my_offers(fake_client, "maybe").status_code == 400

    def test_another_walker_gets_403(self, fake_client, offers):
        assert my_offers(fake_client, token="token-v").status_code == 403

    def test_capped_at_50(self, fake_client, data):
        for index in range(55):
            seed_open(data, f"r{index}")
            offer_row(data, f"r{index}", FARE, created_at=f"2026-09-30T10:{index:02d}:00+00:00")
        assert len(my_offers(fake_client).json()) == 50

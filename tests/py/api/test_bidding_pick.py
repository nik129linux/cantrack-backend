"""Task 7: exactly one offer wins; owner pick beats a later auto-pick."""

from .conftest import FIXED_NOW, bearer, make_user

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"


def seed_open(fake, request_id="walk-1", deadline_at="2026-10-01T00:10:00+00:00"):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    return fake.seed(
        "walk_requests", id=request_id, owner_id="owner-a", walker_id=None, dog_id="dog-1",
        status="open", requested_time=T10, window_end=T11, deadline_at=deadline_at,
        duration_hours=1.0, pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


def place(fake, walker_id, lat=6.201):
    fake.seed("walker_positions", walker_id=walker_id, lat=lat, lng=-75.5,
             updated_at="2026-10-01T00:00:00+00:00")


def offer_id_for(fake_client, request_id, token, price):
    res = fake_client.post(f"/bidding/requests/{request_id}/offers",
                           json={"priceCop": price, "etaMin": 10.0}, headers=bearer(token))
    assert res.status_code == 201
    listed = fake_client.get(f"/bidding/requests/{request_id}/offers", headers=bearer("token-a")).json()
    return [offer["offerId"] for offer in listed if offer["walkerId"] == token_walker(token)][0]


def token_walker(token):
    return {"token-w": "walker-w", "token-w2": "walker-w2"}[token]


class TestPick:
    def test_two_walkers_race_exactly_one_wins(self, fake_client, fake):
        seed_open(fake)
        fake.add_user("token-w2", make_user("walker-w2", "walker-w2@example.com", "walker"))
        place(fake, "walker-w")
        place(fake, "walker-w2", lat=6.2005)
        first = offer_id_for(fake_client, "walk-1", "token-w", 30000)
        second = offer_id_for(fake_client, "walk-1", "token-w2", 20000)
        won = fake_client.post("/bidding/requests/walk-1/pick", json={"offerId": first},
                               headers=bearer("token-a"))
        lost = fake_client.post("/bidding/requests/walk-1/pick", json={"offerId": second},
                                headers=bearer("token-a"))
        assert won.status_code == 200
        assert won.json()["status"] == "assigned"
        assert lost.status_code == 409
        assert lost.json() == {"detail": "Walk no longer open."}

    def test_auto_pick_after_deadline_takes_the_top_offer(self, fake_client, fake, fake_clock):
        seed_open(fake)
        fake.add_user("token-w2", make_user("walker-w2", "walker-w2@example.com", "walker"))
        place(fake, "walker-w")
        place(fake, "walker-w2", lat=6.2005)
        offer_id_for(fake_client, "walk-1", "token-w", 30000)
        offer_id_for(fake_client, "walk-1", "token-w2", 20000)
        fake_clock.now = FIXED_NOW.replace(hour=1)
        res = fake_client.post("/bidding/requests/walk-1/auto-pick", headers=bearer("token-a"))
        assert res.status_code == 200
        assert res.json()["walkerId"] == "walker-w2"

    def test_auto_pick_with_no_offers_expires(self, fake_client, fake, fake_clock):
        seed_open(fake)
        fake_clock.now = FIXED_NOW.replace(hour=1)
        res = fake_client.post("/bidding/requests/walk-1/auto-pick", headers=bearer("token-a"))
        assert res.json()["status"] == "expired"

    def test_auto_pick_before_deadline_is_400(self, fake_client, fake):
        seed_open(fake)
        res = fake_client.post("/bidding/requests/walk-1/auto-pick", headers=bearer("token-a"))
        assert res.status_code == 400

    def test_wrong_owner_pick_is_403(self, fake_client, fake):
        seed_open(fake)
        place(fake, "walker-w")
        found = offer_id_for(fake_client, "walk-1", "token-w", 30000)
        res = fake_client.post("/bidding/requests/walk-1/pick", json={"offerId": found},
                               headers=bearer("token-b"))
        assert res.status_code == 403

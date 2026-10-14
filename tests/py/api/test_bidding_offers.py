"""Task 6: walkers bid on visible requests; owners read ranked offers."""

from .conftest import bearer, make_user

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"


def seed_open(fake, request_id="walk-1"):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    return fake.seed(
        "walk_requests", id=request_id, owner_id="owner-a", walker_id=None, dog_id="dog-1",
        status="open", requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


def place(fake, walker_id, lat=6.201, lng=-75.5):
    fake.seed("walker_positions", walker_id=walker_id, lat=lat, lng=lng,
             updated_at="2026-10-01T00:00:00+00:00")


class TestOffers:
    def test_walker_offers_and_owner_reads_ranked(self, fake_client, fake):
        seed_open(fake)
        fake.add_user("token-w2", make_user("walker-w2", "walker-w2@example.com", "walker"))
        place(fake, "walker-w")
        place(fake, "walker-w2", lat=6.2005)
        fake.seed("walk_ratings", walk_request_id="old-walk", walker_id="walker-w2",
                 rating=5.0, created_at="2026-10-01T00:00:00+00:00")
        assert fake_client.post("/bidding/requests/walk-1/offers",
                                json={"priceCop": 30000, "etaMin": 15.0},
                                headers=bearer("token-w")).status_code == 201
        assert fake_client.post("/bidding/requests/walk-1/offers",
                                json={"priceCop": 20000, "etaMin": 8.0},
                                headers=bearer("token-w2")).status_code == 201
        res = fake_client.get("/bidding/requests/walk-1/offers", headers=bearer("token-a"))
        assert res.status_code == 200
        assert [offer["walkerId"] for offer in res.json()] == ["walker-w2", "walker-w"]
        assert res.json()[0]["allTimeRating"] == 5.0

    def test_second_offer_from_same_walker_is_409(self, fake_client, fake):
        seed_open(fake)
        place(fake, "walker-w")
        fake_client.post("/bidding/requests/walk-1/offers", json={"priceCop": 30000, "etaMin": 15.0},
                         headers=bearer("token-w"))
        res = fake_client.post("/bidding/requests/walk-1/offers", json={"priceCop": 20000, "etaMin": 8.0},
                               headers=bearer("token-w"))
        assert res.status_code == 409
        assert res.json() == {"detail": "Walker already offered on this walk."}

    def test_offer_on_hidden_request_is_403(self, fake_client, fake):
        seed_open(fake)
        place(fake, "walker-w", lat=6.5)
        res = fake_client.post("/bidding/requests/walk-1/offers", json={"priceCop": 30000, "etaMin": 15.0},
                               headers=bearer("token-w"))
        assert res.status_code == 403

    def test_wrong_owner_reads_403(self, fake_client, fake):
        seed_open(fake)
        res = fake_client.get("/bidding/requests/walk-1/offers", headers=bearer("token-b"))
        assert res.status_code == 403
        assert res.json() == {"detail": "Not your walk request."}

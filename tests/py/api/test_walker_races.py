"""Withdraw vs pick race: the owner picks between the open-check and the delete."""

from .conftest import bearer, make_user

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"
FARE = 20000
PRICE = 24000

NOT_OPEN = {"detail": "The request is no longer open."}


def seed_open(fake, request_id="walk-1", status="open", walker_id=None):
    return fake.seed(
        "walk_requests", id=request_id, owner_id="owner-a", walker_id=walker_id, dog_id="dog-1",
        status=status, requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=FARE, created_at="2026-10-01T00:00:00+00:00",
    )


def offer_row(fake, request_id="walk-1", price=PRICE, walker_id="walker-w"):
    return fake.seed("walk_offers", id=f"of-{request_id}-{walker_id}", walk_request_id=request_id,
                     walker_id=walker_id, price_cop=price, eta_min=10.0, distance_km=1.0,
                     created_at="2026-10-01T00:00:00+00:00")


def data(fake):
    fake.add_user("token-v", make_user("walker-v", "walker-v@example.com", "walker"))
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    return fake


def withdraw(client, request_id="walk-1", token="token-w"):
    return client.delete(f"/bidding/requests/{request_id}/offers/mine", headers=bearer(token))


class TestWithdrawVsPick:
    def test_normal_withdraw_still_returns_204(self, fake_client, fake):
        data(fake)
        seed_open(fake)
        offer_row(fake)
        res = withdraw(fake_client)
        assert res.status_code == 204
        assert fake.tables.get("walk_offers", []) == []

    def test_pick_landing_before_the_delete_is_409_and_keeps_the_offer(self, fake_client, fake):
        data(fake)
        seed_open(fake)
        offer_row(fake)

        def owner_picks_first(db):
            for row in db.tables["walk_requests"]:
                if row["id"] == "walk-1":
                    row.update({"status": "assigned", "walker_id": "walker-w"})

        fake.before_execute[("walk_offers", "delete")] = owner_picks_first
        res = withdraw(fake_client)
        assert res.status_code == 409
        assert res.json() == NOT_OPEN
        offers = [o for o in fake.tables["walk_offers"] if o["walker_id"] == "walker-w"]
        assert len(offers) == 1
        assert offers[0]["price_cop"] == PRICE

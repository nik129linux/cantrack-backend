"""W3 Clientes (walker): the owners I walk for, built from bidding walks.

A client is an owner with at least one of MY walks in assigned, in_walk or
done. The owner's phone is shown only while a walk with them is assigned or
in progress (InDrive: contact details live only during the trip). Emails and
face embeddings never leave the server.
"""

import pytest

from .conftest import bearer, make_user

T_OLD = "2026-09-10T15:00:00+00:00"
T_MID = "2026-09-20T15:00:00+00:00"
T_NEW = "2026-09-28T15:00:00+00:00"
T_NEXT = "2026-10-03T15:00:00+00:00"

NOT_A_CLIENT = {"detail": "Not one of your clients."}


def walk(fake, walk_id, status, requested_time, owner_id="owner-a", dog_id="dog-luna",
         walker_id="walker-w", price=20000):
    return fake.seed(
        "walk_requests", id=walk_id, owner_id=owner_id, walker_id=walker_id, dog_id=dog_id,
        status=status, requested_time=requested_time, window_end=requested_time,
        deadline_at=requested_time, duration_hours=1.0, pickup_lat=1.2, pickup_lng=-77.2,
        price_cop=price, created_at="2026-09-01T00:00:00+00:00",
    )


@pytest.fixture
def data(fake):
    fake.add_user("token-v", make_user("walker-v", "walker-v@example.com", "walker"))
    fake.seed("owner_profiles", owner_id="owner-a", display_name="Camila", phone="+57 300 111 2222")
    fake.seed("owner_profiles", owner_id="owner-b", display_name="Andres", phone="+57 300 333 4444")
    fake.seed("dogs", id="dog-luna", owner_id="owner-a", name="Luna", embedding=[0.1] * 4,
              profile={"size": "medium", "notes": "Pulls on the leash"})
    fake.seed("dogs", id="dog-max", owner_id="owner-a", name="Max", embedding=[0.2] * 4, profile=None)
    fake.seed("dogs", id="dog-rocky", owner_id="owner-b", name="Rocky", embedding=[0.3] * 4, profile=None)
    fake.seed("dogs", id="dog-c", owner_id="owner-c", name="Toby", profile=None)
    walk(fake, "a1", "done", T_OLD, price=20000)
    walk(fake, "a2", "done", T_MID, dog_id="dog-max", price=10000)
    fake.seed("walk_offers", id="of-a2", walk_request_id="a2", walker_id="walker-w", price_cop=15000,
              eta_min=5.0, distance_km=1.0, created_at="2026-09-01T00:00:00+00:00")
    walk(fake, "a3", "assigned", T_NEXT)
    walk(fake, "b1", "done", T_NEW, owner_id="owner-b", dog_id="dog-rocky", price=30000)
    walk(fake, "c-open", "open", T_NEXT, owner_id="owner-c", dog_id="dog-c", walker_id=None)
    walk(fake, "c-theirs", "done", T_NEW, owner_id="owner-c", dog_id="dog-c", walker_id="walker-v")
    walk(fake, "a-cancel", "cancelled", T_NEW, dog_id="dog-luna")
    return fake


def clients(client, token="token-w", walker_id="walker-w"):
    return client.get(f"/walkers/{walker_id}/clients", headers=bearer(token))


def detail(client, owner_id, token="token-w", walker_id="walker-w"):
    return client.get(f"/walkers/{walker_id}/clients/{owner_id}", headers=bearer(token))


class TestList:
    def test_only_owners_i_walked_for_most_recent_first(self, fake_client, data):
        res = clients(fake_client)
        assert res.status_code == 200
        assert [c["ownerId"] for c in res.json()] == ["owner-b", "owner-a"]

    def test_item_shape_and_money_at_my_price(self, fake_client, data):
        camila = [c for c in clients(fake_client).json() if c["ownerId"] == "owner-a"][0]
        assert camila == {
            "ownerId": "owner-a", "ownerName": "Camila",
            "dogs": [{"id": "dog-luna", "name": "Luna"}, {"id": "dog-max", "name": "Max"}],
            "walksDone": 2, "upcoming": 1, "lastWalkAt": T_MID, "grossCop": 35000,
        }

    def test_never_leaks_emails_phones_or_embeddings(self, fake_client, data):
        text = clients(fake_client).text
        assert "@example.com" not in text and "+57" not in text and "embedding" not in text

    def test_a_client_with_only_upcoming_walks_has_no_last_walk(self, fake_client, data):
        walk(data, "c-new", "assigned", T_NEXT, owner_id="owner-c", dog_id="dog-c")
        rows = clients(fake_client).json()
        assert rows[-1]["ownerId"] == "owner-c"
        assert (rows[-1]["lastWalkAt"], rows[-1]["walksDone"], rows[-1]["upcoming"]) == (None, 0, 1)

    def test_empty(self, fake_client):
        assert clients(fake_client).json() == []

    @pytest.mark.parametrize("token, walker_id", [("token-v", "walker-w"), ("token-a", "owner-a")])
    def test_nobody_else(self, fake_client, data, token, walker_id):
        assert clients(fake_client, token=token, walker_id=walker_id).status_code == 403


class TestDetail:
    def test_detail_has_care_notes_and_recent_walks(self, fake_client, data):
        res = detail(fake_client, "owner-a")
        assert res.status_code == 200
        body = res.json()
        assert body["ownerId"] == "owner-a" and body["ownerName"] == "Camila"
        assert body["dogs"] == [
            {"id": "dog-luna", "name": "Luna", "profile": {"size": "medium", "notes": "Pulls on the leash"}},
            {"id": "dog-max", "name": "Max", "profile": None},
        ]
        assert [w["id"] for w in body["walks"]] == ["a3", "a2", "a1"]
        assert body["walks"][1] == {"id": "a2", "dogName": "Max", "status": "done",
                                    "requestedTime": T_MID, "priceCop": 15000}
        assert "embedding" not in res.text

    def test_phone_only_while_a_walk_is_assigned_or_live(self, fake_client, data):
        assert detail(fake_client, "owner-a").json()["phone"] == "+57 300 111 2222"
        assert detail(fake_client, "owner-b").json()["phone"] is None

    def test_walks_are_capped_at_ten(self, fake_client, data):
        for index in range(12):
            walk(data, f"x{index}", "done", f"2026-08-{index + 1:02d}T15:00:00+00:00")
        assert len(detail(fake_client, "owner-a").json()["walks"]) == 10

    @pytest.mark.parametrize("owner_id", ["owner-c", "owner-zzz"])
    def test_an_owner_who_is_not_my_client_is_404(self, fake_client, data, owner_id):
        # owner-c only has an open request and a walk with ANOTHER walker.
        res = detail(fake_client, owner_id)
        assert res.status_code == 404
        assert res.json() == NOT_A_CLIENT

    def test_another_walker_cannot_read_my_client(self, fake_client, data):
        assert detail(fake_client, "owner-a", token="token-v").status_code == 403

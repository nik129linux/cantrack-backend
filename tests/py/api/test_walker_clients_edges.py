"""Walker clients edges: spec pins (docstring + brief), never today's output."""

from .conftest import bearer, make_user, reset_all_rate_limits

T_OLD = "2026-09-10T15:00:00+00:00"
T_MID = "2026-09-20T15:00:00+00:00"
T_NEW = "2026-09-28T15:00:00+00:00"
T_NEXT = "2026-10-03T15:00:00+00:00"

NOT_A_CLIENT = {"detail": "Not one of your clients."}
PHONE_A = "+57 300 111 2222"


def walk(fake, walk_id, status, requested_time, owner_id="owner-a", dog_id="dog-luna",
         walker_id="walker-w", price=20000):
    return fake.seed(
        "walk_requests", id=walk_id, owner_id=owner_id, walker_id=walker_id, dog_id=dog_id,
        status=status, requested_time=requested_time, window_end=requested_time,
        deadline_at=requested_time, duration_hours=1.0, pickup_lat=1.2, pickup_lng=-77.2,
        price_cop=price, created_at="2026-09-01T00:00:00+00:00",
    )


def base(fake):
    fake.add_user("token-v", make_user("walker-v", "walker-v@example.com", "walker"))
    return fake


def clients(client, token="token-w", walker_id="walker-w"):
    return client.get(f"/walkers/{walker_id}/clients", headers=bearer(token))


def detail(client, owner_id, token="token-w", walker_id="walker-w"):
    return client.get(f"/walkers/{walker_id}/clients/{owner_id}", headers=bearer(token))


class TestInWalk:
    def test_in_walk_counts_as_upcoming_and_unlocks_phone(self, fake_client, fake):
        base(fake)
        fake.seed("owner_profiles", owner_id="owner-a", display_name="Camila", phone=PHONE_A)
        fake.seed("dogs", id="dog-luna", owner_id="owner-a", name="Luna")
        walk(fake, "w-done", "done", T_MID)
        walk(fake, "w-live", "in_walk", T_NEXT)
        item = [c for c in clients(fake_client).json() if c["ownerId"] == "owner-a"][0]
        assert item["upcoming"] == 1
        assert item["walksDone"] == 1
        res = detail(fake_client, "owner-a")
        assert res.status_code == 200
        assert res.json()["phone"] == PHONE_A
        assert {w["id"] for w in res.json()["walks"]} == {"w-done", "w-live"}


class TestNotAClient:
    def test_cancelled_expired_open_only_is_absent_and_404(self, fake_client, fake):
        base(fake)
        fake.seed("dogs", id="dog-luna", owner_id="owner-a", name="Luna")
        fake.seed("dogs", id="dog-rocky", owner_id="owner-b", name="Rocky")
        walk(fake, "a-cancel", "cancelled", T_NEW)
        walk(fake, "a-expired", "expired", T_NEW)
        walk(fake, "a-open", "open", T_NEXT)
        walk(fake, "b-done", "done", T_NEW, owner_id="owner-b", dog_id="dog-rocky")
        ids = [c["ownerId"] for c in clients(fake_client).json()]
        assert "owner-a" not in ids
        assert ids == ["owner-b"]
        res = detail(fake_client, "owner-a")
        assert res.status_code == 404
        assert res.json() == NOT_A_CLIENT


class TestMoney:
    def test_gross_walks_done_and_last_walk_ignore_non_done(self, fake_client, fake):
        base(fake)
        fake.seed("dogs", id="dog-luna", owner_id="owner-a", name="Luna")
        walk(fake, "m-old", "done", T_OLD, price=20000)
        walk(fake, "m-mid", "done", T_MID, price=10000)
        walk(fake, "m-future", "assigned", T_NEXT, price=99999)
        walk(fake, "m-live", "in_walk", T_NEW, price=88888)
        item = [c for c in clients(fake_client).json() if c["ownerId"] == "owner-a"][0]
        assert item["walksDone"] == 2
        assert item["grossCop"] == 30000
        assert item["upcoming"] == 2
        assert item["lastWalkAt"] == T_MID


class TestMissingDog:
    def test_gone_dog_is_null_name_without_500(self, fake_client, fake):
        base(fake)
        walk(fake, "g-1", "done", T_MID, dog_id="dog-gone")
        res = clients(fake_client)
        assert res.status_code == 200
        item = [c for c in res.json() if c["ownerId"] == "owner-a"][0]
        assert item["dogs"] == [{"id": "dog-gone", "name": None}]
        res = detail(fake_client, "owner-a")
        assert res.status_code == 200
        assert res.json()["dogs"] == [{"id": "dog-gone", "name": None, "profile": None}]
        assert res.json()["walks"][0]["dogName"] is None


class TestQueryBudget:
    def _seed_clients(self, fake, count, start=0):
        for i in range(start, start + count):
            fake.seed("owner_profiles", owner_id=f"owner-{i}", display_name=f"Owner {i}")
            fake.seed("dogs", id=f"dog-{i}", owner_id=f"owner-{i}", name=f"Dog {i}")
            walk(fake, f"q-{i}", "done", T_MID, owner_id=f"owner-{i}", dog_id=f"dog-{i}")

    def test_list_cost_does_not_grow_with_clients(self, fake_client, fake):
        base(fake)
        self._seed_clients(fake, 1)
        fake.queries.clear()
        assert clients(fake_client).status_code == 200
        one = len(fake.queries)
        self._seed_clients(fake, 9, start=1)
        fake.queries.clear()
        assert clients(fake_client).status_code == 200
        ten = len(fake.queries)
        assert one == ten


class TestValidationAndLimits:
    def test_owner_id_longer_than_64_chars_is_400(self, fake_client, fake):
        base(fake)
        res = detail(fake_client, "o" * 65)
        assert res.status_code == 400

    def test_31st_call_in_a_minute_is_429(self, fake_client, fake):
        base(fake)
        reset_all_rate_limits()
        codes = [clients(fake_client).status_code for _ in range(31)]
        assert codes[:30] == [200] * 30
        assert codes[30] == 429


class TestIsolation:
    def test_nothing_from_another_walker_appears(self, fake_client, fake):
        base(fake)
        fake.seed("owner_profiles", owner_id="owner-a", display_name="Camila", phone=PHONE_A)
        fake.seed("owner_profiles", owner_id="owner-b", display_name="Intruder", phone="+57 300 999 8888")
        fake.seed("dogs", id="dog-luna", owner_id="owner-a", name="Luna")
        fake.seed("dogs", id="dog-secret", owner_id="owner-b", name="SecretDog")
        walk(fake, "mine-1", "done", T_MID, price=20000)
        walk(fake, "theirs-1", "done", T_NEW, owner_id="owner-b", dog_id="dog-secret",
             walker_id="walker-v", price=77777)
        fake.seed("walk_offers", id="of-theirs", walk_request_id="theirs-1", walker_id="walker-v",
                  price_cop=77777, eta_min=5.0, distance_km=1.0, created_at="2026-09-01T00:00:00+00:00")
        # walker-v also bid on MY walk and lost: my price must stay mine.
        fake.seed("walk_offers", id="of-lost-on-mine", walk_request_id="mine-1", walker_id="walker-v",
                  price_cop=77777, eta_min=5.0, distance_km=1.0, created_at="2026-09-01T00:00:00+00:00")
        res = clients(fake_client)
        assert [c["ownerId"] for c in res.json()] == ["owner-a"]
        assert res.json()[0]["grossCop"] == 20000
        assert "SecretDog" not in res.text and "77777" not in res.text
        assert "+57 300 999 8888" not in res.text and "Intruder" not in res.text
        mine = detail(fake_client, "owner-a")
        assert mine.status_code == 200
        assert "SecretDog" not in mine.text and "77777" not in mine.text
        assert detail(fake_client, "owner-b").status_code == 404

"""W5 Perfil (walker): walk history, data export and account deletion (Ley 1581).

Deleting a walker must not erase the OWNERS' history: done walks stay, with
the walker detached (``walker_id`` -> null). Assigned walks go back to the
market as ``open`` so the owner can get new offers, InDrive style. Deletion is
refused while a dog is out on a walk with this walker.
"""

import pytest

from .conftest import bearer, make_user

WALK_IN_PROGRESS = {"detail": "Finish your walk in progress before deleting the account."}
CONFIRM_REQUIRED = {"detail": 'Send {"confirm": "DELETE"} to delete the account.'}

T_PAST1 = "2026-09-10T15:00:00+00:00"
T_PAST2 = "2026-09-20T15:00:00+00:00"
T_SOON = "2026-10-02T15:00:00+00:00"
T_LATER = "2026-10-04T15:00:00+00:00"


def walk(fake, walk_id, status, requested_time, walker_id="walker-w", dog_id="d1", price=20000):
    return fake.seed(
        "walk_requests", id=walk_id, owner_id="owner-a", walker_id=walker_id, dog_id=dog_id,
        status=status, requested_time=requested_time, window_end=requested_time,
        deadline_at=requested_time, duration_hours=1.0, pickup_lat=1.2, pickup_lng=-77.2,
        price_cop=price, created_at="2026-09-01T00:00:00+00:00",
    )


@pytest.fixture
def account(fake):
    fake.add_user("token-v", make_user("walker-v", "walker-v@example.com", "walker"))
    fake.seed("walker_profiles", walker_id="walker-w", display_name="Mateo", bio="Hola", service_area="Centro",
              price_per_walk=18000, phone="+57 300", avatar_path="walker-w/avatar.webp",
              consent_version="2026-10", consent_at="2026-10-01T00:00:00+00:00")
    fake.seed("walker_profiles", walker_id="walker-v", display_name="Vale", bio=None, service_area=None,
              price_per_walk=15000)
    fake.storage.objects[("avatars", "walker-w/avatar.webp")] = b"RIFF0000WEBP"
    fake.seed("owner_profiles", owner_id="owner-a", display_name="Camila")
    fake.seed("dogs", id="d1", owner_id="owner-a", name="Luna", embedding=[0.1] * 4)
    fake.seed("dogs", id="d2", owner_id="owner-a", name="Max", embedding=[0.2] * 4)
    walk(fake, "w-done1", "done", T_PAST1)
    walk(fake, "w-done2", "done", T_PAST2, dog_id="d2", price=10000)
    walk(fake, "w-asg-later", "assigned", T_LATER)
    walk(fake, "w-asg-soon", "assigned", T_SOON, dog_id="d2")
    walk(fake, "w-open", "open", T_SOON, walker_id=None)
    walk(fake, "w-theirs", "done", T_PAST2, walker_id="walker-v")
    fake.seed("walk_offers", id="of-1", walk_request_id="w-open", walker_id="walker-w", price_cop=20000,
              eta_min=5.0, distance_km=1.0, created_at="2026-09-30T00:00:00+00:00")
    fake.seed("walk_offers", id="of-2", walk_request_id="w-done2", walker_id="walker-w", price_cop=14000,
              eta_min=5.0, distance_km=1.0, created_at="2026-09-01T00:00:00+00:00")
    fake.seed("walk_offers", id="of-v", walk_request_id="w-open", walker_id="walker-v", price_cop=19000,
              eta_min=5.0, distance_km=1.0, created_at="2026-09-30T00:00:00+00:00")
    fake.seed("walk_ratings", walk_request_id="w-done1", walker_id="walker-w", rating=5,
              created_at="2026-09-11T00:00:00+00:00")
    fake.seed("walker_positions", walker_id="walker-w", lat=1.2, lng=-77.2, updated_at="2026-10-01T00:00:00+00:00")
    fake.seed("walker_positions", walker_id="walker-v", lat=1.3, lng=-77.3, updated_at="2026-10-01T00:00:00+00:00")
    return fake


def walks(client, tab=None, token="token-w", walker_id="walker-w"):
    params = {"tab": tab} if tab else {}
    return client.get(f"/walkers/{walker_id}/walks", params=params, headers=bearer(token))


class TestWalks:
    def test_upcoming_is_the_default_soonest_first(self, fake_client, account):
        assert [w["id"] for w in walks(fake_client).json()] == ["w-asg-soon", "w-asg-later"]

    def test_past_is_done_newest_first_with_my_price_and_rating(self, fake_client, account):
        body = walks(fake_client, "past").json()
        assert [w["id"] for w in body] == ["w-done2", "w-done1"]
        assert body[0] == {"id": "w-done2", "dogName": "Max", "ownerName": "Camila", "status": "done",
                           "requestedTime": T_PAST2, "durationHours": 1.0, "priceCop": 14000, "rating": None}
        assert body[1]["rating"] == 5 and body[1]["priceCop"] == 20000

    def test_active(self, fake_client, account):
        walk(account, "w-live", "in_walk", T_SOON)
        assert [w["id"] for w in walks(fake_client, "active").json()] == ["w-live"]

    def test_unknown_tab_is_400(self, fake_client, account):
        assert walks(fake_client, "soon").status_code == 400

    def test_another_walker_gets_403(self, fake_client, account):
        assert walks(fake_client, token="token-v").status_code == 403


class TestExport:
    def export(self, client, token="token-w"):
        return client.get("/walkers/walker-w/export", headers=bearer(token))

    def test_everything_mine_and_nothing_else(self, fake_client, account):
        res = self.export(fake_client)
        assert res.status_code == 200
        assert res.headers["Content-Disposition"] == 'attachment; filename="cantrack-export.json"'
        body = res.json()
        assert set(body) == {"exportedAt", "profile", "position", "offers", "walks", "ratingsReceived"}
        assert body["profile"]["displayName"] == "Mateo"
        assert body["profile"]["consent"] == {"version": "2026-10", "at": "2026-10-01T00:00:00+00:00"}
        assert body["position"] == {"lat": 1.2, "lng": -77.2, "updatedAt": "2026-10-01T00:00:00+00:00"}
        assert {o["id"] for o in body["offers"]} == {"of-1", "of-2"}
        assert {w["id"] for w in body["walks"]} == {"w-done1", "w-done2", "w-asg-later", "w-asg-soon"}
        assert [r["rating"] for r in body["ratingsReceived"]] == [5]
        assert "Vale" not in res.text and "of-v" not in res.text and "w-theirs" not in res.text

    def test_no_embeddings_and_no_owner_emails(self, fake_client, account):
        text = self.export(fake_client).text
        assert "embedding" not in text and "owner-a@example.com" not in text

    def test_twice_a_minute(self, fake_client, account):
        assert [self.export(fake_client).status_code for _ in range(3)] == [200, 200, 429]

    @pytest.mark.parametrize("token", ["token-v", "token-a"])
    def test_nobody_else(self, fake_client, account, token):
        assert self.export(fake_client, token=token).status_code == 403


class TestDelete:
    def delete(self, client, body=None, token="token-w"):
        return client.request("DELETE", "/walkers/walker-w", json=body if body is not None else {"confirm": "DELETE"},
                              headers=bearer(token))

    def test_erases_me_but_keeps_the_owners_history(self, fake_client, account):
        res = self.delete(fake_client)
        assert res.status_code == 204
        by_id = {w["id"]: w for w in account.tables["walk_requests"]}
        assert (by_id["w-done1"]["status"], by_id["w-done1"]["walker_id"]) == ("done", None)
        assert (by_id["w-asg-soon"]["status"], by_id["w-asg-soon"]["walker_id"]) == ("open", None)
        assert by_id["w-asg-later"]["status"] == "open"
        assert by_id["w-theirs"]["walker_id"] == "walker-v"
        assert [o["id"] for o in account.tables["walk_offers"]] == ["of-v"]
        assert [p["walker_id"] for p in account.tables["walker_positions"]] == ["walker-v"]
        assert [p["walker_id"] for p in account.tables["walker_profiles"]] == ["walker-v"]
        assert ("avatars", "walker-w/avatar.webp") not in account.storage.objects
        assert ("delete", "walker-w", None) in account.auth.admin.calls

    def test_refused_while_a_walk_is_in_progress(self, fake_client, account):
        walk(account, "w-live", "in_walk", T_SOON)
        res = self.delete(fake_client)
        assert res.status_code == 409 and res.json() == WALK_IN_PROGRESS
        assert len(account.tables["walker_profiles"]) == 2
        assert not [c for c in account.auth.admin.calls if c[0] == "delete"]

    @pytest.mark.parametrize("body", [{}, {"confirm": "delete"}, {"confirm": "yes"}])
    def test_needs_the_exact_confirmation(self, fake_client, account, body):
        res = self.delete(fake_client, body=body)
        assert res.status_code == 400 and res.json() == CONFIRM_REQUIRED
        assert len(account.tables["walker_profiles"]) == 2

    @pytest.mark.parametrize("token", ["token-v", "token-a"])
    def test_nobody_else_can_delete_me(self, fake_client, account, token):
        assert self.delete(fake_client, token=token).status_code == 403
        assert len(account.tables["walker_profiles"]) == 2

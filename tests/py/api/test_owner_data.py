"""PR4 Perfil: data export and account deletion (Colombia's Ley 1581 / Habeas Data).

The owner can take every piece of their data with them and can erase the
account. Deletion is refused while a walk is in progress (a dog is out with a
walker), and it never touches another owner's rows.
"""

import pytest

from .conftest import bearer

WALK_IN_PROGRESS = {"detail": "Finish your walk in progress before deleting the account."}
CONFIRM_REQUIRED = {"detail": 'Send {"confirm": "DELETE"} to delete the account.'}


@pytest.fixture
def account(fake):
    fake.seed("owner_profiles", owner_id="owner-a", display_name="Camila", phone="+57 300",
              avatar_path="owner-a/avatar.webp")
    fake.storage.objects[("avatars", "owner-a/avatar.webp")] = b"RIFF0000WEBP"
    fake.seed("dogs", id="d1", owner_id="owner-a", name="Luna", embedding=[0.1] * 4)
    fake.seed("dogs", id="d-b", owner_id="owner-b", name="Theirs")
    fake.seed("walk_requests", id="w-open", owner_id="owner-a", dog_id="d1", status="open")
    fake.seed("walk_requests", id="w-asg", owner_id="owner-a", dog_id="d1", status="assigned", walker_id="walker-w")
    fake.seed("walk_requests", id="w-done", owner_id="owner-a", dog_id="d1", status="done", walker_id="walker-w")
    fake.seed("walk_requests", id="w-b", owner_id="owner-b", dog_id="d-b", status="open")
    fake.seed("walk_ratings", walk_request_id="w-done", walker_id="walker-w", rating=5,
              created_at="2026-09-01T00:00:00+00:00")
    return fake


class TestExport:
    def export(self, client, token="token-a"):
        return client.get("/owners/owner-a/export", headers=bearer(token))

    def test_everything_mine_and_nothing_else(self, fake_client, account):
        res = self.export(fake_client)
        assert res.status_code == 200
        assert res.headers["Content-Disposition"] == 'attachment; filename="cantrack-export.json"'
        body = res.json()
        assert set(body) == {"exportedAt", "profile", "dogs", "walkRequests", "ratingsGiven"}
        assert body["profile"]["displayName"] == "Camila"
        assert [dog["name"] for dog in body["dogs"]] == ["Luna"]
        assert {walk["id"] for walk in body["walkRequests"]} == {"w-open", "w-asg", "w-done"}
        assert [rating["rating"] for rating in body["ratingsGiven"]] == [5]
        assert "Theirs" not in res.text and "w-b" not in res.text

    def test_face_embeddings_are_never_exported(self, fake_client, account):
        assert "embedding" not in self.export(fake_client).text

    def test_twice_a_minute(self, fake_client, account):
        codes = [self.export(fake_client).status_code for _ in range(3)]
        assert codes == [200, 200, 429]

    @pytest.mark.parametrize("token", ["token-b", "token-w"])
    def test_nobody_else(self, fake_client, account, token):
        assert self.export(fake_client, token=token).status_code == 403


class TestDelete:
    def delete(self, client, body=None, token="token-a"):
        return client.request("DELETE", "/owners/owner-a", json=body if body is not None else {"confirm": "DELETE"},
                              headers=bearer(token))

    def test_erases_my_data_and_my_login(self, fake_client, account):
        res = self.delete(fake_client)
        assert res.status_code == 204
        assert [d["id"] for d in account.tables["dogs"]] == ["d-b"]
        assert not [p for p in account.tables["owner_profiles"] if p["owner_id"] == "owner-a"]
        assert ("avatars", "owner-a/avatar.webp") not in account.storage.objects
        statuses = {w["id"]: w["status"] for w in account.tables["walk_requests"]}
        assert statuses["w-open"] == "cancelled" and statuses["w-asg"] == "cancelled"
        assert statuses["w-b"] == "open"
        assert ("delete", "owner-a", None) in account.auth.admin.calls

    def test_refused_while_a_walk_is_in_progress(self, fake_client, account):
        account.seed("walk_requests", id="w-live", owner_id="owner-a", dog_id="d1", status="in_walk",
                     walker_id="walker-w")
        res = self.delete(fake_client)
        assert res.status_code == 409
        assert res.json() == WALK_IN_PROGRESS
        assert len(account.tables["dogs"]) == 2
        assert not [c for c in account.auth.admin.calls if c[0] == "delete"]

    @pytest.mark.parametrize("body", [{}, {"confirm": "delete"}, {"confirm": "yes"}])
    def test_needs_the_exact_confirmation(self, fake_client, account, body):
        res = self.delete(fake_client, body=body)
        assert res.status_code == 400
        assert len(account.tables["dogs"]) == 2

    def test_wrong_confirmation_has_a_clear_message(self, fake_client, account):
        assert self.delete(fake_client, body={"confirm": "yes"}).json() == CONFIRM_REQUIRED

    @pytest.mark.parametrize("token", ["token-b", "token-w"])
    def test_nobody_else_can_delete_me(self, fake_client, account, token):
        assert self.delete(fake_client, token=token).status_code == 403
        assert len(account.tables["dogs"]) == 2

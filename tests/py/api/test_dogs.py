"""FR-04: an owner can create, read, update and delete their own dogs.

Every request carries a bearer token; the guard resolves it to a user and every
query must be scoped to that user's own rows (the fake enforces the filters).
"""

import uuid

import pytest
from postgrest.exceptions import APIError

from .conftest import bearer


def create_dog(client, token="token-a", **body):
    body = {"name": "Firulais", "breed": "Mixed", **body}
    return client.post("/dogs", json=body, headers=bearer(token))


class TestAuthRequired:
    @pytest.mark.parametrize(
        "method,path",
        [("get", "/dogs"), ("post", "/dogs"), ("patch", f"/dogs/{uuid.uuid4()}"),
         ("delete", f"/dogs/{uuid.uuid4()}")],
    )
    def test_no_token_is_401(self, fake_client, method, path):
        res = getattr(fake_client, method)(path)
        assert res.status_code == 401

    def test_unknown_token_is_401(self, fake_client):
        res = fake_client.get("/dogs", headers=bearer("nope"))
        assert res.status_code == 401


class TestCreate:
    def test_creates_a_dog_owned_by_the_caller(self, fake_client, fake):
        res = create_dog(fake_client, notes="Loves the park")
        assert res.status_code == 201
        body = res.json()
        assert body["name"] == "Firulais"
        assert body["breed"] == "Mixed"
        assert body["notes"] == "Loves the park"
        assert body["owner_id"] == "owner-a"
        assert body["id"]
        assert len(fake.tables["dogs"]) == 1

    def test_breed_and_notes_are_optional(self, fake_client):
        res = fake_client.post("/dogs", json={"name": "Rex"}, headers=bearer("token-a"))
        assert res.status_code == 201
        assert res.json()["name"] == "Rex"

    def test_client_cannot_choose_owner_id_or_other_columns(self, fake_client, fake):
        res = fake_client.post(
            "/dogs",
            json={"name": "Sneaky", "owner_id": "owner-b", "id": "custom",
                  "embedding": [0.1] * 512},
            headers=bearer("token-a"),
        )
        assert res.status_code == 201
        stored = fake.tables["dogs"][0]
        assert stored["owner_id"] == "owner-a"
        assert stored["id"] != "custom"
        assert "embedding" not in stored

    @pytest.mark.parametrize(
        "body",
        [{"breed": "Mixed"}, {"name": ""}, {"name": 5}, {"name": "A", "breed": 3},
         {"name": "A", "notes": ["x"]}],
    )
    def test_invalid_body_is_400_and_stores_nothing(self, fake_client, fake, body):
        res = fake_client.post("/dogs", json=body, headers=bearer("token-a"))
        assert res.status_code == 400
        assert fake.tables.get("dogs", []) == []

    def test_database_error_is_500_with_message(self, fake_client, fake):
        fake.fail_with = APIError(
            {"message": "connection lost", "code": "XX000", "hint": None, "details": None}
        )
        res = create_dog(fake_client)
        assert res.status_code == 500
        assert res.json() == {"detail": "connection lost"}


class TestList:
    def test_lists_only_the_callers_dogs(self, fake_client):
        create_dog(fake_client, "token-a", name="Firulais")
        create_dog(fake_client, "token-b", name="Rex", breed="Labrador")
        res = fake_client.get("/dogs", headers=bearer("token-a"))
        assert res.status_code == 200
        assert [d["name"] for d in res.json()] == ["Firulais"]

    def test_empty_list_for_a_new_owner(self, fake_client):
        res = fake_client.get("/dogs", headers=bearer("token-a"))
        assert res.status_code == 200
        assert res.json() == []

    def test_database_error_is_500(self, fake_client, fake):
        fake.fail_with = APIError(
            {"message": "boom", "code": "XX000", "hint": None, "details": None}
        )
        assert fake_client.get("/dogs", headers=bearer("token-a")).status_code == 500


class TestUpdate:
    def test_owner_can_update_a_subset_of_fields(self, fake_client):
        dog = create_dog(fake_client).json()
        res = fake_client.patch(
            f"/dogs/{dog['id']}",
            json={"notes": "Now afraid of skateboards"},
            headers=bearer("token-a"),
        )
        assert res.status_code == 200
        assert res.json()["notes"] == "Now afraid of skateboards"
        assert res.json()["name"] == "Firulais"

    def test_another_owner_gets_404_and_nothing_changes(self, fake_client, fake):
        dog = create_dog(fake_client).json()
        res = fake_client.patch(
            f"/dogs/{dog['id']}", json={"name": "Stolen"}, headers=bearer("token-b")
        )
        assert res.status_code == 404
        assert fake.tables["dogs"][0]["name"] == "Firulais"

    def test_unknown_id_is_404(self, fake_client):
        res = fake_client.patch(
            f"/dogs/{uuid.uuid4()}", json={"name": "X"}, headers=bearer("token-a")
        )
        assert res.status_code == 404
        assert res.json() == {"detail": "Dog not found."}

    def test_client_cannot_reassign_the_owner(self, fake_client, fake):
        dog = create_dog(fake_client).json()
        res = fake_client.patch(
            f"/dogs/{dog['id']}", json={"owner_id": "owner-b"}, headers=bearer("token-a")
        )
        assert res.status_code == 200
        assert fake.tables["dogs"][0]["owner_id"] == "owner-a"

    @pytest.mark.parametrize("body", [{"name": ""}, {"name": 3}, {"notes": [1]}])
    def test_invalid_body_is_400(self, fake_client, body):
        dog = create_dog(fake_client).json()
        res = fake_client.patch(f"/dogs/{dog['id']}", json=body, headers=bearer("token-a"))
        assert res.status_code == 400

    def test_non_uuid_id_is_400(self, fake_client):
        res = fake_client.patch("/dogs/not-a-uuid", json={"name": "X"}, headers=bearer("token-a"))
        assert res.status_code == 400


class TestDelete:
    def test_owner_can_delete_their_dog(self, fake_client):
        dog = create_dog(fake_client).json()
        res = fake_client.delete(f"/dogs/{dog['id']}", headers=bearer("token-a"))
        assert res.status_code == 200
        assert res.json() == {"message": "Dog deleted."}
        assert fake_client.get("/dogs", headers=bearer("token-a")).json() == []

    def test_another_owner_gets_404_and_the_dog_survives(self, fake_client, fake):
        dog = create_dog(fake_client).json()
        res = fake_client.delete(f"/dogs/{dog['id']}", headers=bearer("token-b"))
        assert res.status_code == 404
        assert len(fake.tables["dogs"]) == 1

    def test_deleting_twice_is_404_the_second_time(self, fake_client):
        dog = create_dog(fake_client).json()
        assert fake_client.delete(f"/dogs/{dog['id']}", headers=bearer("token-a")).status_code == 200
        assert fake_client.delete(f"/dogs/{dog['id']}", headers=bearer("token-a")).status_code == 404

    def test_non_uuid_id_is_400(self, fake_client):
        assert fake_client.delete("/dogs/xyz", headers=bearer("token-a")).status_code == 400


def test_every_dogs_query_is_scoped_to_the_caller(fake_client, fake):
    dog = create_dog(fake_client).json()
    fake_client.get("/dogs", headers=bearer("token-a"))
    fake_client.patch(f"/dogs/{dog['id']}", json={"name": "N"}, headers=bearer("token-a"))
    fake_client.delete(f"/dogs/{dog['id']}", headers=bearer("token-a"))
    scoped = [q for q in fake.queries if q[0] == "dogs" and q[1] != "insert"]
    assert len(scoped) == 3
    for _table, _op, filters in scoped:
        assert ("owner_id", "owner-a") in filters

"""S1: the dog questionnaire — `dogs.profile` jsonb, validated by pydantic.

`PUT /dogs/{dog_id}/profile` replaces the whole questionnaire (8 fields:
size, temperament, energy, leash trained, allergies, medical notes, vet
contact, emergency contact). Only the dog's owner can write it (404 for
everyone else, nothing changed). Wire camelCase, storage snake_case.
"""

import uuid

import pytest
from postgrest.exceptions import APIError

from .conftest import bearer

FULL = {
    "size": "medium",
    "temperament": "friendly",
    "energy": "high",
    "leashTrained": True,
    "allergies": "Peanuts",
    "medicalNotes": "Hip dysplasia, short walks ok",
    "vetContact": "Vet Laura 3001112233",
    "emergencyContact": "Ana 3104445566",
}

STORED_FULL = {
    "size": "medium",
    "temperament": "friendly",
    "energy": "high",
    "leash_trained": True,
    "allergies": "Peanuts",
    "medical_notes": "Hip dysplasia, short walks ok",
    "vet_contact": "Vet Laura 3001112233",
    "emergency_contact": "Ana 3104445566",
}


def create_dog(client, token="token-a", name="Firulais", breed="Mixed"):
    return client.post("/dogs", json={"name": name, "breed": breed}, headers=bearer(token)).json()


class TestAuthRequired:
    def test_no_token_is_401(self, fake_client):
        res = fake_client.put(f"/dogs/{uuid.uuid4()}/profile", json=FULL)
        assert res.status_code == 401

    def test_unknown_token_is_401(self, fake_client):
        res = fake_client.put(
            f"/dogs/{uuid.uuid4()}/profile", json=FULL, headers=bearer("nope")
        )
        assert res.status_code == 401


class TestSaveQuestionnaire:
    def test_saves_the_full_questionnaire(self, fake_client, fake):
        dog = create_dog(fake_client)
        res = fake_client.put(
            f"/dogs/{dog['id']}/profile", json=FULL, headers=bearer("token-a")
        )
        assert res.status_code == 200
        assert res.json() == FULL
        assert fake.tables["dogs"][0]["profile"] == STORED_FULL

    def test_text_fields_are_optional_and_default_to_null(self, fake_client, fake):
        dog = create_dog(fake_client)
        minimal = {"size": "small", "temperament": "shy", "energy": "low", "leashTrained": False}
        res = fake_client.put(
            f"/dogs/{dog['id']}/profile", json=minimal, headers=bearer("token-a")
        )
        assert res.status_code == 200
        assert res.json() == {
            **minimal,
            "allergies": None,
            "medicalNotes": None,
            "vetContact": None,
            "emergencyContact": None,
        }

    def test_saving_replaces_the_whole_questionnaire(self, fake_client, fake):
        dog = create_dog(fake_client)
        fake_client.put(f"/dogs/{dog['id']}/profile", json=FULL, headers=bearer("token-a"))
        updated = {**FULL, "temperament": "reactive", "allergies": None}
        res = fake_client.put(
            f"/dogs/{dog['id']}/profile", json=updated, headers=bearer("token-a")
        )
        assert res.status_code == 200
        assert res.json() == updated
        assert fake.tables["dogs"][0]["profile"]["temperament"] == "reactive"
        assert fake.tables["dogs"][0]["profile"]["allergies"] is None

    def test_client_cannot_write_other_columns_through_the_profile(self, fake_client, fake):
        dog = create_dog(fake_client)
        res = fake_client.put(
            f"/dogs/{dog['id']}/profile",
            json={**FULL, "name": "Stolen", "owner_id": "owner-b", "embedding": [0.1]},
            headers=bearer("token-a"),
        )
        assert res.status_code == 200
        stored = fake.tables["dogs"][0]
        assert stored["name"] == "Firulais"
        assert stored["owner_id"] == "owner-a"
        assert "embedding" not in stored["profile"]
        assert "name" not in stored["profile"]

    @pytest.mark.parametrize(
        "body",
        [
            {**FULL, "size": "huge"},            # bad enum
            {**FULL, "temperament": "aggressive"},  # bad enum
            {**FULL, "energy": "extreme"},       # bad enum
            {**FULL, "leashTrained": "yes"},     # not a bool
            {**FULL, "size": None},              # required
            {"temperament": "friendly", "energy": "high", "leashTrained": True},  # no size
            {**FULL, "allergies": ["peanuts"]},  # text field must be a string
        ],
    )
    def test_invalid_body_is_400_and_stores_nothing(self, fake_client, fake, body):
        dog = create_dog(fake_client)
        res = fake_client.put(
            f"/dogs/{dog['id']}/profile", json=body, headers=bearer("token-a")
        )
        assert res.status_code == 400
        assert "profile" not in fake.tables["dogs"][0] or fake.tables["dogs"][0]["profile"] is None


class TestOwnership:
    def test_another_owner_gets_404_and_nothing_changes(self, fake_client, fake):
        dog = create_dog(fake_client)
        res = fake_client.put(
            f"/dogs/{dog['id']}/profile", json=FULL, headers=bearer("token-b")
        )
        assert res.status_code == 404
        assert res.json() == {"detail": "Dog not found."}
        assert fake.tables["dogs"][0].get("profile") in (None,)

    def test_a_walker_gets_404_for_an_owner_dog(self, fake_client, fake):
        dog = create_dog(fake_client)
        res = fake_client.put(
            f"/dogs/{dog['id']}/profile", json=FULL, headers=bearer("token-w")
        )
        assert res.status_code == 404
        assert fake.tables["dogs"][0].get("profile") in (None,)

    def test_unknown_id_is_404(self, fake_client):
        res = fake_client.put(
            f"/dogs/{uuid.uuid4()}/profile", json=FULL, headers=bearer("token-a")
        )
        assert res.status_code == 404
        assert res.json() == {"detail": "Dog not found."}

    def test_non_uuid_id_is_400(self, fake_client):
        res = fake_client.put("/dogs/not-a-uuid/profile", json=FULL, headers=bearer("token-a"))
        assert res.status_code == 400

    def test_database_error_is_500_with_message(self, fake_client, fake):
        dog = create_dog(fake_client)
        fake.fail_with = APIError(
            {"message": "connection lost", "code": "XX000", "hint": None, "details": None}
        )
        res = fake_client.put(
            f"/dogs/{dog['id']}/profile", json=FULL, headers=bearer("token-a")
        )
        assert res.status_code == 500
        assert res.json() == {"detail": "connection lost"}

    def test_the_profile_query_is_scoped_to_the_caller(self, fake_client, fake):
        dog = create_dog(fake_client)
        fake.queries.clear()
        fake_client.put(f"/dogs/{dog['id']}/profile", json=FULL, headers=bearer("token-a"))
        scoped = [q for q in fake.queries if q[0] == "dogs" and q[1] != "insert"]
        assert scoped, "expected the profile write to look the dog up first"
        for _table, _op, filters in scoped:
            assert ("owner_id", "owner-a") in filters

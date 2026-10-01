"""S1: walker profiles — the public card data owners pick a walker from.

`PUT /walker-profile` upserts the caller's own profile (walkers only);
`GET /walker-profiles` is the catalog any authenticated user can browse.
Wire format is camelCase (like routes); storage is snake_case (like dogs).
"""

import pytest
from postgrest.exceptions import APIError

from .conftest import bearer, make_user

VALID = {"displayName": "Nico Walks", "bio": "Five years walking packs.",
         "serviceArea": "Laureles", "pricePerWalk": 25000}


def api_error(message="connection lost"):
    return APIError({"message": message, "code": "XX000", "hint": None, "details": None})


@pytest.fixture
def second_walker(fake):
    fake.add_user("token-w2", make_user("walker-w2", "walker-w2@example.com", "walker"))


class TestAuthRequired:
    @pytest.mark.parametrize("method,path", [("put", "/walker-profile"), ("get", "/walker-profiles")])
    def test_no_token_is_401(self, fake_client, method, path):
        assert getattr(fake_client, method)(path).status_code == 401

    def test_unknown_token_is_401(self, fake_client):
        assert fake_client.get("/walker-profiles", headers=bearer("nope")).status_code == 401


class TestSaveProfile:
    def test_saves_the_callers_profile(self, fake_client, fake):
        res = fake_client.put("/walker-profile", json=VALID, headers=bearer("token-w"))
        assert res.status_code == 200
        assert res.json() == {
            "walkerId": "walker-w",
            "displayName": "Nico Walks",
            "bio": "Five years walking packs.",
            "serviceArea": "Laureles",
            "pricePerWalk": 25000,
        }
        assert len(fake.tables["walker_profiles"]) == 1
        stored = fake.tables["walker_profiles"][0]
        assert stored["walker_id"] == "walker-w"
        assert stored["display_name"] == "Nico Walks"
        assert stored["price_per_walk"] == 25000

    def test_saving_twice_updates_the_single_row(self, fake_client, fake):
        fake_client.put("/walker-profile", json=VALID, headers=bearer("token-w"))
        res = fake_client.put(
            "/walker-profile", json={**VALID, "pricePerWalk": 30000}, headers=bearer("token-w")
        )
        assert res.status_code == 200
        assert res.json()["pricePerWalk"] == 30000
        assert len(fake.tables["walker_profiles"]) == 1
        assert fake.tables["walker_profiles"][0]["price_per_walk"] == 30000

    def test_bio_and_service_area_are_optional_and_default_to_null(self, fake_client):
        res = fake_client.put(
            "/walker-profile",
            json={"displayName": "Ana", "pricePerWalk": 0},
            headers=bearer("token-w"),
        )
        assert res.status_code == 200
        assert res.json() == {
            "walkerId": "walker-w",
            "displayName": "Ana",
            "bio": None,
            "serviceArea": None,
            "pricePerWalk": 0,
        }

    def test_client_cannot_choose_walker_id_or_other_columns(self, fake_client, fake):
        res = fake_client.put(
            "/walker-profile",
            json={**VALID, "walkerId": "someone-else", "id": "custom", "createdAt": "x"},
            headers=bearer("token-w"),
        )
        assert res.status_code == 200
        stored = fake.tables["walker_profiles"][0]
        assert stored["walker_id"] == "walker-w"
        assert stored["id"] != "custom"
        assert res.json()["walkerId"] == "walker-w"

    @pytest.mark.parametrize(
        "body",
        [
            {"bio": "no name", "pricePerWalk": 100},          # displayName missing
            {"displayName": "", "pricePerWalk": 100},          # empty name
            {"displayName": 5, "pricePerWalk": 100},           # wrong type
            {"displayName": "Ana", "pricePerWalk": -1},        # negative price
            {"displayName": "Ana", "pricePerWalk": "free"},    # non-integer price
            {"displayName": "Ana", "pricePerWalk": 10.5},      # fractional price
            {"displayName": "Ana"},                            # price missing
        ],
    )
    def test_invalid_body_is_400_and_stores_nothing(self, fake_client, fake, body):
        res = fake_client.put("/walker-profile", json=body, headers=bearer("token-w"))
        assert res.status_code == 400
        assert fake.tables.get("walker_profiles", []) == []

    def test_an_owner_gets_400_and_stores_nothing(self, fake_client, fake):
        res = fake_client.put("/walker-profile", json=VALID, headers=bearer("token-a"))
        assert res.status_code == 400
        assert res.json() == {"detail": "Only walkers can save a walker profile."}
        assert fake.tables.get("walker_profiles", []) == []

    def test_database_error_is_500_with_message(self, fake_client, fake):
        fake.fail_with = api_error()
        res = fake_client.put("/walker-profile", json=VALID, headers=bearer("token-w"))
        assert res.status_code == 500
        assert res.json() == {"detail": "connection lost"}

    def test_the_pre_insert_lookup_is_scoped_to_the_caller(self, fake_client, fake):
        fake_client.put("/walker-profile", json=VALID, headers=bearer("token-w"))
        fake_client.put("/walker-profile", json=VALID, headers=bearer("token-w"))
        selects = [q for q in fake.queries if q[0] == "walker_profiles" and q[1] == "select"]
        assert len(selects) == 2
        for _table, _op, filters in selects:
            assert ("walker_id", "walker-w") in filters


class TestCatalog:
    def test_lists_every_profile_in_creation_order(self, fake_client, second_walker):
        fake_client.put("/walker-profile", json=VALID, headers=bearer("token-w"))
        fake_client.put(
            "/walker-profile",
            json={"displayName": "Ana Packs", "bio": None, "serviceArea": "El Poblado",
                  "pricePerWalk": 18000},
            headers=bearer("token-w2"),
        )
        res = fake_client.get("/walker-profiles", headers=bearer("token-a"))
        assert res.status_code == 200
        assert res.json() == [
            {"walkerId": "walker-w", "displayName": "Nico Walks",
             "bio": "Five years walking packs.", "serviceArea": "Laureles", "pricePerWalk": 25000},
            {"walkerId": "walker-w2", "displayName": "Ana Packs", "bio": None,
             "serviceArea": "El Poblado", "pricePerWalk": 18000},
        ]

    def test_walkers_can_browse_the_catalog_too(self, fake_client, second_walker):
        fake_client.put("/walker-profile", json=VALID, headers=bearer("token-w2"))
        res = fake_client.get("/walker-profiles", headers=bearer("token-w"))
        assert res.status_code == 200
        assert [p["walkerId"] for p in res.json()] == ["walker-w2"]

    def test_empty_catalog(self, fake_client):
        res = fake_client.get("/walker-profiles", headers=bearer("token-a"))
        assert res.status_code == 200
        assert res.json() == []

    def test_database_error_is_500(self, fake_client, fake):
        fake.fail_with = api_error("boom")
        assert fake_client.get("/walker-profiles", headers=bearer("token-a")).status_code == 500

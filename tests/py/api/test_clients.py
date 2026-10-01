"""S2: `GET /clients` — the walker's "My clients".

A client is a dog with an ACCEPTED request addressed to the caller, carrying
the pin, the time and the full questionnaire (the walker already passed the
S1 privacy gate by accepting). Privacy follows the status, always: pending,
declined and cancelled requests are not clients — including a request
cancelled AFTER being accepted, which disappears from the list.

Pinned contract:
- items are ordered by requestedTime ascending, then requestId ascending;
- an item is exactly {"requestId", "dogId", "dogName", "requestedTime",
  "pickupLat", "pickupLng", "profile"} where profile is the camelCase
  questionnaire or null when the owner never filled it;
- timestamps come back normalized to UTC (the fake models PostgREST);
- owners get 400; missing/unknown token 401; database errors 500;
- another walker's accepted requests never appear.
"""

import pytest
from postgrest.exceptions import APIError

from .conftest import bearer, make_user

T09 = "2026-10-05T09:00:00+00:00"
T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"

PROFILE = {"displayName": "Nico Walks", "bio": None, "serviceArea": "Laureles",
           "pricePerWalk": 25000}

QUESTIONNAIRE = {
    "size": "medium",
    "temperament": "friendly",
    "energy": "high",
    "leashTrained": True,
    "allergies": "Peanuts",
    "medicalNotes": "Hip dysplasia",
    "vetContact": "Vet Laura 3001112233",
    "emergencyContact": "Ana 3104445566",
}

ROLE_MESSAGE = {"detail": "Only walkers have clients."}


def api_error(message="connection lost"):
    return APIError({"message": message, "code": "XX000", "hint": None, "details": None})


@pytest.fixture
def second_walker(fake):
    fake.add_user("token-w2", make_user("walker-w2", "walker-w2@example.com", "walker"))


def save_profile(client, token="token-w", **over):
    res = client.put("/walker-profile", json={**PROFILE, **over}, headers=bearer(token))
    assert res.status_code == 200
    return res.json()


def create_dog(client, token="token-a", name="Firulais", breed="Mixed"):
    res = client.post("/dogs", json={"name": name, "breed": breed}, headers=bearer(token))
    assert res.status_code == 201
    return res.json()


def set_questionnaire(client, dog_id, token="token-a", **over):
    res = client.put(
        f"/dogs/{dog_id}/profile", json={**QUESTIONNAIRE, **over}, headers=bearer(token)
    )
    assert res.status_code == 200


def accepted_request(fake_client, dog_id, walker_id="walker-w", token="token-a",
                     requested_time=T10, lat=6.2, lng=-75.5):
    """Create a request through the S1 flow and accept it as the walker."""
    res = fake_client.post(
        "/requests",
        json={"walkerId": walker_id, "dogId": dog_id, "requestedTime": requested_time,
              "pickupLat": lat, "pickupLng": lng},
        headers=bearer(token),
    )
    assert res.status_code == 201
    rid = res.json()["id"]
    walk_token = "token-w" if walker_id == "walker-w" else "token-w2"
    accepted = fake_client.post(f"/requests/{rid}/accept", headers=bearer(walk_token))
    assert accepted.status_code == 200
    return rid


class TestAuthAndRole:
    def test_no_token_is_401(self, fake_client):
        assert fake_client.get("/clients").status_code == 401

    def test_unknown_token_is_401(self, fake_client):
        assert fake_client.get("/clients", headers=bearer("nope")).status_code == 401

    def test_an_owner_gets_400(self, fake_client):
        res = fake_client.get("/clients", headers=bearer("token-a"))
        assert res.status_code == 400
        assert res.json() == ROLE_MESSAGE


class TestClientList:
    def test_empty_for_a_walker_without_accepted_requests(self, fake_client):
        save_profile(fake_client)
        res = fake_client.get("/clients", headers=bearer("token-w"))
        assert res.status_code == 200
        assert res.json() == []

    def test_an_accepted_request_is_a_client_with_pin_time_and_full_questionnaire(
        self, fake_client
    ):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        set_questionnaire(fake_client, dog["id"])
        rid = accepted_request(fake_client, dog["id"])

        res = fake_client.get("/clients", headers=bearer("token-w"))
        assert res.status_code == 200
        assert res.json() == [
            {
                "requestId": rid,
                "dogId": dog["id"],
                "dogName": "Firulais",
                "requestedTime": T10,
                "pickupLat": 6.2,
                "pickupLng": -75.5,
                "profile": QUESTIONNAIRE,
            }
        ]

    def test_profile_is_null_when_the_owner_never_filled_the_questionnaire(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        accepted_request(fake_client, dog["id"])
        item = fake_client.get("/clients", headers=bearer("token-w")).json()[0]
        assert item["profile"] is None

    def test_ordered_by_requested_time_then_request_id(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        late = accepted_request(fake_client, dog["id"], requested_time=T11)
        early = accepted_request(fake_client, dog["id"], requested_time=T09)
        items = fake_client.get("/clients", headers=bearer("token-w")).json()
        assert [item["requestId"] for item in items] == [early, late]

    def test_a_dog_with_accepted_requests_on_two_days_appears_once_per_request(
        self, fake_client
    ):
        # The contract is one item per accepted REQUEST (not per dog): the same
        # dog with walks on two days is two clients entries, in time order.
        save_profile(fake_client)
        dog = create_dog(fake_client)
        set_questionnaire(fake_client, dog["id"])
        first = accepted_request(fake_client, dog["id"], requested_time=T10)
        second = accepted_request(
            fake_client, dog["id"], requested_time="2026-10-06T10:00:00+00:00"
        )
        items = fake_client.get("/clients", headers=bearer("token-w")).json()
        assert [item["requestId"] for item in items] == [first, second]
        assert [item["dogId"] for item in items] == [dog["id"], dog["id"]]
        assert [item["requestedTime"] for item in items] == [
            T10,
            "2026-10-06T10:00:00+00:00",
        ]

    def test_pending_requests_are_not_clients(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        res = fake_client.post(
            "/requests",
            json={"walkerId": "walker-w", "dogId": dog["id"], "requestedTime": T10,
                  "pickupLat": 6.2, "pickupLng": -75.5},
            headers=bearer("token-a"),
        )
        assert res.status_code == 201
        assert fake_client.get("/clients", headers=bearer("token-w")).json() == []

    def test_declined_requests_are_not_clients(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        res = fake_client.post(
            "/requests",
            json={"walkerId": "walker-w", "dogId": dog["id"], "requestedTime": T10,
                  "pickupLat": 6.2, "pickupLng": -75.5},
            headers=bearer("token-a"),
        )
        rid = res.json()["id"]
        fake_client.post(f"/requests/{rid}/decline", headers=bearer("token-w"))
        assert fake_client.get("/clients", headers=bearer("token-w")).json() == []

    def test_a_request_cancelled_after_accept_disappears(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        assert len(fake_client.get("/clients", headers=bearer("token-w")).json()) == 1

        fake_client.post(f"/requests/{rid}/cancel", headers=bearer("token-a"))
        assert fake_client.get("/clients", headers=bearer("token-w")).json() == []

    def test_another_walkers_clients_never_appear(self, fake_client, second_walker):
        save_profile(fake_client)
        save_profile(fake_client, token="token-w2", displayName="Ana Packs")
        dog = create_dog(fake_client)
        theirs = accepted_request(fake_client, dog["id"], walker_id="walker-w2")
        mine = accepted_request(fake_client, dog["id"], requested_time=T11)

        items = fake_client.get("/clients", headers=bearer("token-w")).json()
        assert [item["requestId"] for item in items] == [mine]
        assert theirs not in [item["requestId"] for item in items]

    def test_requested_time_comes_back_normalized_to_utc(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        # the owner sends a -05:00 wall-clock time; PostgREST (and the fake)
        # store and return timestamptz normalized to UTC
        rid = accepted_request(
            fake_client, dog["id"], requested_time="2026-10-05T05:00:00-05:00"
        )
        item = fake_client.get("/clients", headers=bearer("token-w")).json()[0]
        assert item["requestId"] == rid
        assert item["requestedTime"] == "2026-10-05T10:00:00+00:00"

    def test_database_error_is_500_with_message(self, fake_client, fake):
        fake.fail_with = api_error()
        res = fake_client.get("/clients", headers=bearer("token-w"))
        assert res.status_code == 500
        assert res.json() == {"detail": "connection lost"}


def test_clients_queries_are_scoped_to_the_caller(fake_client, fake):
    save_profile(fake_client)
    dog = create_dog(fake_client)
    accepted_request(fake_client, dog["id"])

    fake.queries.clear()
    fake_client.get("/clients", headers=bearer("token-w"))
    scoped = [q for q in fake.queries if q[0] == "walk_requests" and q[1] != "insert"]
    assert scoped, "expected the client list to read walk_requests"
    for _table, _op, filters in scoped:
        assert ("walker_id", "walker-w") in filters

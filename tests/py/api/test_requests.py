"""S1: walk requests — the marketplace flow owner -> walker.

POST /requests (owner) stores the request priced from the walker's profile;
GET /requests is role-scoped (owner: their sent requests newest first;
walker: the inbox with pending requests oldest first, per the hand-written
Queue); GET /requests/{id} applies the privacy gate (while pending the walker
sees name, breed, size and temperament only — pin, full questionnaire and
contacts only after accept); accept/decline are walker-only, cancel is
owner-only, every illegal transition is a 400 and every foreign row a 404.
Accepting flags interval conflicts (FR-17, hand-written IntervalTree): each
walk occupies [requested_time, requested_time + 60 min), half-open, against
the walker's other ACCEPTED requests.
"""

import uuid

import pytest
from postgrest.exceptions import APIError

from .conftest import FIXED_NOW, bearer, make_user

T09 = "2026-10-05T09:00:00+00:00"
T0930 = "2026-10-05T09:30:00+00:00"
T10 = "2026-10-05T10:00:00+00:00"
T1015 = "2026-10-05T10:15:00+00:00"
T1030 = "2026-10-05T10:30:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"
T12 = "2026-10-05T12:00:00+00:00"

PROFILE = {"displayName": "Nico Walks", "bio": "Five years walking packs.",
           "serviceArea": "Laureles", "pricePerWalk": 25000}

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

NOT_FOUND = {"detail": "Request not found."}
ACCEPT_STATE = {"detail": "Only a pending request can be accepted."}
DECLINE_STATE = {"detail": "Only a pending request can be declined."}
CANCEL_STATE = {"detail": "Only a pending or accepted request can be cancelled."}
TIME_MESSAGE = {"detail": "requestedTime must be an ISO-8601 datetime with a timezone offset."}
FUTURE_MESSAGE = {"detail": "requestedTime must be in the future."}
DOUBLE_BOOKED = {"detail": "This dog already has an active request for that time."}
FLOOD_MESSAGE = {"detail": "Too many pending requests."}


def api_error(message="connection lost"):
    return APIError({"message": message, "code": "XX000", "hint": None, "details": None})


@pytest.fixture
def second_walker(fake):
    fake.add_user("token-w2", make_user("walker-w2", "walker-w2@example.com", "walker"))


def save_profile(client, token="token-w", **over):
    res = client.put("/walker-profile", json={**PROFILE, **over}, headers=bearer(token))
    assert res.status_code == 200
    return res.json()


def create_dog(client, token="token-a", name="Firulais"):
    res = client.post("/dogs", json={"name": name, "breed": "Mixed"}, headers=bearer(token))
    assert res.status_code == 201
    return res.json()


def set_questionnaire(client, dog_id, token="token-a", **over):
    res = client.put(
        f"/dogs/{dog_id}/profile", json={**QUESTIONNAIRE, **over}, headers=bearer(token)
    )
    assert res.status_code == 200


def post_request(client, walker_id, dog_id, token="token-a", requested_time=T10,
                 lat=6.2, lng=-75.5, **extra):
    return client.post(
        "/requests",
        json={"walkerId": walker_id, "dogId": dog_id, "requestedTime": requested_time,
              "pickupLat": lat, "pickupLng": lng, **extra},
        headers=bearer(token),
    )


def seed_request(fake, dog_id, created_at, *, owner_id="owner-a", walker_id="walker-w",
                 status="pending", requested_time=T10, responded_at=None, price=25000):
    return fake.seed(
        "walk_requests",
        owner_id=owner_id, walker_id=walker_id, dog_id=dog_id, status=status,
        requested_time=requested_time, pickup_lat=6.2, pickup_lng=-75.5,
        price_cop=price, created_at=created_at, responded_at=responded_at,
    )


def split_dynamic(body):
    """Pop the two server-generated values so the rest can be compared exactly."""
    rest = dict(body)
    dynamic = {"id": rest.pop("id"), "createdAt": rest.pop("createdAt")}
    assert isinstance(dynamic["id"], str) and dynamic["id"]
    assert isinstance(dynamic["createdAt"], str) and dynamic["createdAt"]
    return dynamic, rest


class TestAuthRequired:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("post", "/requests"),
            ("get", "/requests"),
            ("get", f"/requests/{uuid.uuid4()}"),
            ("post", f"/requests/{uuid.uuid4()}/accept"),
            ("post", f"/requests/{uuid.uuid4()}/decline"),
            ("post", f"/requests/{uuid.uuid4()}/cancel"),
        ],
    )
    def test_no_token_is_401(self, fake_client, method, path):
        assert getattr(fake_client, method)(path).status_code == 401

    def test_unknown_token_is_401(self, fake_client):
        assert fake_client.get("/requests", headers=bearer("nope")).status_code == 401


class TestCreate:
    def test_creates_a_pending_request_priced_from_the_walker_profile(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        res = post_request(fake_client, profile["walkerId"], dog["id"])
        assert res.status_code == 201
        dynamic, body = split_dynamic(res.json())
        assert body == {
            "walkerId": "walker-w",
            "walkerName": "Nico Walks",
            "dogId": dog["id"],
            "dogName": "Firulais",
            "status": "pending",
            "requestedTime": T10,
            "pickupLat": 6.2,
            "pickupLng": -75.5,
            "priceCop": 25000,
            "respondedAt": None,
        }
        assert len(fake.tables["walk_requests"]) == 1
        stored = fake.tables["walk_requests"][0]
        assert stored["id"] == dynamic["id"]
        assert stored["owner_id"] == "owner-a"
        assert stored["walker_id"] == "walker-w"
        assert stored["dog_id"] == dog["id"]
        assert stored["status"] == "pending"
        assert stored["requested_time"] == T10
        assert stored["pickup_lat"] == 6.2
        assert stored["pickup_lng"] == -75.5
        assert stored["price_cop"] == 25000
        assert stored["responded_at"] is None

    def test_a_price_in_the_body_is_ignored_the_profile_decides(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        res = post_request(fake_client, profile["walkerId"], dog["id"], priceCop=1)
        assert res.status_code == 201
        assert res.json()["priceCop"] == 25000
        assert fake.tables["walk_requests"][0]["price_cop"] == 25000

    def test_foreign_dog_is_404_and_stores_nothing(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client, token="token-b", name="Rex")
        res = post_request(fake_client, profile["walkerId"], dog["id"], token="token-a")
        assert res.status_code == 404
        assert res.json() == {"detail": "Dog not found."}
        assert fake.tables.get("walk_requests", []) == []

    def test_unknown_dog_is_404(self, fake_client, fake):
        profile = save_profile(fake_client)
        res = post_request(fake_client, profile["walkerId"], str(uuid.uuid4()))
        assert res.status_code == 404
        assert res.json() == {"detail": "Dog not found."}
        assert fake.tables.get("walk_requests", []) == []

    def test_walker_without_profile_is_404_and_stores_nothing(self, fake_client, fake):
        dog = create_dog(fake_client)
        res = post_request(fake_client, str(uuid.uuid4()), dog["id"])
        assert res.status_code == 404
        assert res.json() == {"detail": "Walker profile not found."}
        assert fake.tables.get("walk_requests", []) == []

    @pytest.mark.parametrize(
        "requested_time",
        ["not-a-time", "2026-10-05T10:00:00", "2026-10-05", ""],
    )
    def test_requested_time_must_be_iso_with_offset(self, fake_client, fake, requested_time):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        res = post_request(fake_client, profile["walkerId"], dog["id"],
                           requested_time=requested_time)
        assert res.status_code == 400
        assert res.json() == TIME_MESSAGE
        assert fake.tables.get("walk_requests", []) == []

    @pytest.mark.parametrize("field,value", [
        ("pickupLat", 91.0), ("pickupLat", -90.5),
        ("pickupLng", 180.5), ("pickupLng", -181.0),
        ("pickupLat", "six"), ("requestedTime", None),
        ("walkerId", ""), ("dogId", ""),
    ])
    def test_invalid_field_is_400_and_stores_nothing(self, fake_client, fake, field, value):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        body = {"walkerId": profile["walkerId"], "dogId": dog["id"],
                "requestedTime": T10, "pickupLat": 6.2, "pickupLng": -75.5}
        body[field] = value
        res = fake_client.post("/requests", json=body, headers=bearer("token-a"))
        assert res.status_code == 400
        assert fake.tables.get("walk_requests", []) == []

    @pytest.mark.parametrize(
        "missing", ["walkerId", "dogId", "requestedTime", "pickupLat", "pickupLng"],
    )
    def test_missing_field_is_400_and_stores_nothing(self, fake_client, fake, missing):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        body = {"walkerId": profile["walkerId"], "dogId": dog["id"],
                "requestedTime": T10, "pickupLat": 6.2, "pickupLng": -75.5}
        del body[missing]
        res = fake_client.post("/requests", json=body, headers=bearer("token-a"))
        assert res.status_code == 400
        assert fake.tables.get("walk_requests", []) == []

    def test_a_walker_cannot_create_a_request(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        res = post_request(fake_client, profile["walkerId"], dog["id"], token="token-w")
        assert res.status_code == 400
        assert res.json() == {"detail": "Only owners can create a walk request."}
        assert fake.tables.get("walk_requests", []) == []

    def test_database_error_is_500_with_message(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        fake.fail_with = api_error()
        res = post_request(fake_client, profile["walkerId"], dog["id"])
        assert res.status_code == 500
        assert res.json() == {"detail": "connection lost"}


class TestOwnerList:
    def test_owner_sees_their_own_requests_newest_first(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        old = seed_request(fake, dog["id"], T09, requested_time=T10)
        new = seed_request(fake, dog["id"], T10, requested_time=T11)
        res = fake_client.get("/requests", headers=bearer("token-a"))
        assert res.status_code == 200
        items = res.json()
        assert [item["id"] for item in items] == [new["id"], old["id"]]
        dynamic, body = split_dynamic(items[0])
        assert dynamic["id"] == new["id"]
        assert body == {
            "walkerId": "walker-w",
            "walkerName": "Nico Walks",
            "dogId": dog["id"],
            "dogName": "Firulais",
            "status": "pending",
            "requestedTime": T11,
            "pickupLat": 6.2,
            "pickupLng": -75.5,
            "priceCop": 25000,
            "respondedAt": None,
        }

    def test_another_owner_sees_nothing(self, fake_client):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        post_request(fake_client, profile["walkerId"], dog["id"])
        res = fake_client.get("/requests", headers=bearer("token-b"))
        assert res.status_code == 200
        assert res.json() == []

    def test_empty_list(self, fake_client):
        assert fake_client.get("/requests", headers=bearer("token-a")).json() == []

    def test_database_error_is_500(self, fake_client, fake):
        fake.fail_with = api_error("boom")
        assert fake_client.get("/requests", headers=bearer("token-a")).status_code == 500


class TestWalkerInbox:
    def test_pending_come_oldest_first_regardless_of_storage_order(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        # stored newest-first on purpose; the inbox must reorder (hand-written Queue)
        newest = seed_request(fake, dog["id"], "2026-10-03T08:00:00+00:00")
        middle = seed_request(fake, dog["id"], "2026-10-02T08:00:00+00:00")
        oldest = seed_request(fake, dog["id"], "2026-10-01T08:00:00+00:00")
        res = fake_client.get("/requests", headers=bearer("token-w"))
        assert res.status_code == 200
        assert [item["id"] for item in res.json()] == [oldest["id"], middle["id"], newest["id"]]

    def test_pending_first_then_the_rest_oldest_first(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        accepted = seed_request(fake, dog["id"], T09, status="accepted", responded_at=T0930)
        pending_late = seed_request(fake, dog["id"], T10)
        declined = seed_request(fake, dog["id"], T0930, status="declined", responded_at=T10)
        pending_early = seed_request(fake, dog["id"], "2026-10-01T08:00:00+00:00")
        res = fake_client.get("/requests", headers=bearer("token-w"))
        assert [(item["id"], item["status"]) for item in res.json()] == [
            (pending_early["id"], "pending"),
            (pending_late["id"], "pending"),
            (accepted["id"], "accepted"),
            (declined["id"], "declined"),
        ]

    def test_pending_item_is_the_limited_view(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        set_questionnaire(fake_client, dog["id"])
        seeded = seed_request(fake, dog["id"], T09)
        res = fake_client.get("/requests", headers=bearer("token-w"))
        dynamic, body = split_dynamic(res.json()[0])
        assert dynamic["id"] == seeded["id"]
        assert body == {
            "status": "pending",
            "requestedTime": T10,
            "priceCop": 25000,
            "dog": {"name": "Firulais", "breed": "Mixed", "size": "medium",
                    "temperament": "friendly"},
        }

    def test_size_and_temperament_are_null_without_questionnaire(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        seed_request(fake, dog["id"], T09)
        item = fake_client.get("/requests", headers=bearer("token-w")).json()[0]
        assert item["dog"] == {"name": "Firulais", "breed": "Mixed", "size": None,
                               "temperament": None}

    def test_requests_addressed_to_another_walker_are_invisible(self, fake_client, fake,
                                                                second_walker):
        save_profile(fake_client)
        save_profile(fake_client, token="token-w2", displayName="Ana Packs")
        dog = create_dog(fake_client)
        mine = seed_request(fake, dog["id"], T09)
        theirs = seed_request(fake, dog["id"], T10, walker_id="walker-w2")
        res = fake_client.get("/requests", headers=bearer("token-w"))
        assert [item["id"] for item in res.json()] == [mine["id"]]
        assert theirs["id"] not in [item["id"] for item in res.json()]


class TestDetail:
    def setup_request(self, fake_client, with_questionnaire=True):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        if with_questionnaire:
            set_questionnaire(fake_client, dog["id"])
        res = post_request(fake_client, profile["walkerId"], dog["id"])
        assert res.status_code == 201
        return res.json(), dog

    def test_owner_sees_the_full_view(self, fake_client):
        created, dog = self.setup_request(fake_client)
        res = fake_client.get(f"/requests/{created['id']}", headers=bearer("token-a"))
        assert res.status_code == 200
        dynamic, body = split_dynamic(res.json())
        assert dynamic["id"] == created["id"]
        assert body == {
            "walkerId": "walker-w",
            "walkerName": "Nico Walks",
            "dogId": dog["id"],
            "dogName": "Firulais",
            "status": "pending",
            "requestedTime": T10,
            "pickupLat": 6.2,
            "pickupLng": -75.5,
            "priceCop": 25000,
            "respondedAt": None,
        }

    def test_walker_pending_sees_only_name_breed_size_temperament(self, fake_client):
        created, _dog = self.setup_request(fake_client)
        res = fake_client.get(f"/requests/{created['id']}", headers=bearer("token-w"))
        assert res.status_code == 200
        body = res.json()
        assert set(body) == {"id", "status", "requestedTime", "priceCop", "createdAt", "dog"}
        assert set(body["dog"]) == {"name", "breed", "size", "temperament"}
        assert body["dog"] == {"name": "Firulais", "breed": "Mixed", "size": "medium",
                               "temperament": "friendly"}

    def test_after_accept_the_walker_sees_pin_and_full_questionnaire(self, fake_client):
        created, _dog = self.setup_request(fake_client)
        rid = created["id"]
        accepted = fake_client.post(f"/requests/{rid}/accept", headers=bearer("token-w"))
        assert accepted.status_code == 200
        res = fake_client.get(f"/requests/{rid}", headers=bearer("token-w"))
        dynamic, body = split_dynamic(res.json())
        assert dynamic["id"] == rid
        assert body == {
            "status": "accepted",
            "requestedTime": T10,
            "priceCop": 25000,
            "respondedAt": accepted.json()["respondedAt"],
            "pickupLat": 6.2,
            "pickupLng": -75.5,
            "dog": {"name": "Firulais", "breed": "Mixed", "size": "medium",
                    "temperament": "friendly", "energy": "high", "leashTrained": True,
                    "allergies": "Peanuts", "medicalNotes": "Hip dysplasia",
                    "vetContact": "Vet Laura 3001112233",
                    "emergencyContact": "Ana 3104445566"},
        }

    def test_another_owner_gets_404(self, fake_client):
        created, _dog = self.setup_request(fake_client)
        res = fake_client.get(f"/requests/{created['id']}", headers=bearer("token-b"))
        assert res.status_code == 404
        assert res.json() == NOT_FOUND

    def test_another_walker_gets_404(self, fake_client, second_walker):
        created, _dog = self.setup_request(fake_client)
        res = fake_client.get(f"/requests/{created['id']}", headers=bearer("token-w2"))
        assert res.status_code == 404
        assert res.json() == NOT_FOUND

    def test_unknown_id_is_404(self, fake_client):
        res = fake_client.get(f"/requests/{uuid.uuid4()}", headers=bearer("token-a"))
        assert res.status_code == 404
        assert res.json() == NOT_FOUND

    def test_non_uuid_id_is_400(self, fake_client):
        assert fake_client.get("/requests/not-a-uuid",
                               headers=bearer("token-a")).status_code == 400

    def test_database_error_is_500(self, fake_client, fake):
        created, _dog = self.setup_request(fake_client)
        fake.fail_with = api_error("boom")
        assert fake_client.get(f"/requests/{created['id']}",
                               headers=bearer("token-a")).status_code == 500


class TestAccept:
    def test_accept_sets_status_and_responded_at(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        res = post_request(fake_client, profile["walkerId"], dog["id"])
        rid = res.json()["id"]
        accepted = fake_client.post(f"/requests/{rid}/accept", headers=bearer("token-w"))
        assert accepted.status_code == 200
        body = accepted.json()
        assert body["id"] == rid
        assert body["status"] == "accepted"
        assert isinstance(body["respondedAt"], str) and body["respondedAt"]
        assert body["conflicts"] == []
        assert fake.tables["walk_requests"][0]["status"] == "accepted"
        assert fake.tables["walk_requests"][0]["responded_at"] == body["respondedAt"]

    def test_overlapping_accepted_walks_are_flagged_as_conflicts(self, fake_client):
        profile = save_profile(fake_client)
        dog_a = create_dog(fake_client, name="Firulais")
        dog_b = create_dog(fake_client, token="token-b", name="Rex")
        r1 = post_request(fake_client, profile["walkerId"], dog_a["id"],
                          requested_time=T10).json()
        r2 = post_request(fake_client, profile["walkerId"], dog_b["id"], token="token-b",
                          requested_time=T1030).json()
        r3 = post_request(fake_client, profile["walkerId"], dog_a["id"],
                          requested_time=T11).json()

        first = fake_client.post(f"/requests/{r1['id']}/accept", headers=bearer("token-w"))
        assert first.json()["conflicts"] == []

        second = fake_client.post(f"/requests/{r2['id']}/accept", headers=bearer("token-w"))
        assert second.status_code == 200
        assert second.json()["conflicts"] == [{"requestId": r1["id"], "requestedTime": T10}]

        # r3 starts exactly when r1's 60-minute window ends: half-open, no overlap.
        third = fake_client.post(f"/requests/{r3['id']}/accept", headers=bearer("token-w"))
        assert third.json()["conflicts"] == [{"requestId": r2["id"], "requestedTime": T1030}]

    def test_conflicts_come_sorted_by_requested_time(self, fake_client):
        profile = save_profile(fake_client)
        dog_a = create_dog(fake_client, name="Firulais")
        dog_b = create_dog(fake_client, token="token-b", name="Rex")
        early = post_request(fake_client, profile["walkerId"], dog_a["id"],
                             requested_time=T0930).json()
        middle = post_request(fake_client, profile["walkerId"], dog_b["id"], token="token-b",
                             requested_time=T10).json()
        late = post_request(fake_client, profile["walkerId"], dog_a["id"],
                            requested_time=T1015).json()
        fake_client.post(f"/requests/{early['id']}/accept", headers=bearer("token-w"))
        fake_client.post(f"/requests/{middle['id']}/accept", headers=bearer("token-w"))
        res = fake_client.post(f"/requests/{late['id']}/accept", headers=bearer("token-w"))
        # late's window [10:15, 11:15) overlaps early [09:30, 10:30) and middle [10:00, 11:00)
        assert res.json()["conflicts"] == [
            {"requestId": early["id"], "requestedTime": T0930},
            {"requestId": middle["id"], "requestedTime": T10},
        ]

    def test_non_accepted_requests_never_conflict(self, fake_client):
        profile = save_profile(fake_client)
        dog_a = create_dog(fake_client, name="Firulais")
        dog_b = create_dog(fake_client, token="token-b", name="Rex")
        pending = post_request(fake_client, profile["walkerId"], dog_a["id"],
                               requested_time=T10).json()
        declined = post_request(fake_client, profile["walkerId"], dog_a["id"],
                                requested_time=T1030).json()
        fake_client.post(f"/requests/{declined['id']}/decline", headers=bearer("token-w"))
        overlapping = post_request(fake_client, profile["walkerId"], dog_b["id"],
                                   token="token-b", requested_time=T1030).json()
        res = fake_client.post(f"/requests/{overlapping['id']}/accept", headers=bearer("token-w"))
        assert res.json()["conflicts"] == []
        assert pending["id"] != overlapping["id"]

    def test_cancelled_requests_never_conflict(self, fake_client):
        profile = save_profile(fake_client)
        dog_a = create_dog(fake_client, name="Firulais")
        dog_b = create_dog(fake_client, token="token-b", name="Rex")
        first = post_request(fake_client, profile["walkerId"], dog_a["id"],
                             requested_time=T10).json()
        fake_client.post(f"/requests/{first['id']}/accept", headers=bearer("token-w"))
        fake_client.post(f"/requests/{first['id']}/cancel", headers=bearer("token-a"))
        second = post_request(fake_client, profile["walkerId"], dog_b["id"], token="token-b",
                              requested_time=T1030).json()
        res = fake_client.post(f"/requests/{second['id']}/accept", headers=bearer("token-w"))
        assert res.json()["conflicts"] == []

    @pytest.mark.parametrize("first_action", ["accept", "decline", "cancel"])
    def test_accepting_a_non_pending_request_is_400(self, fake_client, first_action):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        if first_action == "accept":
            fake_client.post(f"/requests/{rid}/accept", headers=bearer("token-w"))
        elif first_action == "decline":
            fake_client.post(f"/requests/{rid}/decline", headers=bearer("token-w"))
        else:
            fake_client.post(f"/requests/{rid}/cancel", headers=bearer("token-a"))
        res = fake_client.post(f"/requests/{rid}/accept", headers=bearer("token-w"))
        assert res.status_code == 400
        assert res.json() == ACCEPT_STATE

    def test_the_owner_cannot_accept_their_own_request(self, fake_client):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        res = fake_client.post(f"/requests/{rid}/accept", headers=bearer("token-a"))
        assert res.status_code == 404
        assert res.json() == NOT_FOUND

    def test_another_walker_gets_404(self, fake_client, second_walker):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        res = fake_client.post(f"/requests/{rid}/accept", headers=bearer("token-w2"))
        assert res.status_code == 404
        assert res.json() == NOT_FOUND

    def test_unknown_id_is_404(self, fake_client):
        res = fake_client.post(f"/requests/{uuid.uuid4()}/accept", headers=bearer("token-w"))
        assert res.status_code == 404
        assert res.json() == NOT_FOUND

    def test_non_uuid_id_is_400(self, fake_client):
        assert fake_client.post("/requests/xyz/accept",
                                headers=bearer("token-w")).status_code == 400

    def test_database_error_is_500(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        fake.fail_with = api_error("boom")
        assert fake_client.post(f"/requests/{rid}/accept",
                                headers=bearer("token-w")).status_code == 500


class TestDecline:
    def test_decline_sets_status_and_responded_at(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        res = fake_client.post(f"/requests/{rid}/decline", headers=bearer("token-w"))
        assert res.status_code == 200
        body = res.json()
        assert body["id"] == rid
        assert body["status"] == "declined"
        assert isinstance(body["respondedAt"], str) and body["respondedAt"]
        assert fake.tables["walk_requests"][0]["responded_at"] == body["respondedAt"]

    @pytest.mark.parametrize("first_action", ["accept", "decline", "cancel"])
    def test_declining_a_non_pending_request_is_400(self, fake_client, first_action):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        if first_action == "accept":
            fake_client.post(f"/requests/{rid}/accept", headers=bearer("token-w"))
        elif first_action == "decline":
            fake_client.post(f"/requests/{rid}/decline", headers=bearer("token-w"))
        else:
            fake_client.post(f"/requests/{rid}/cancel", headers=bearer("token-a"))
        res = fake_client.post(f"/requests/{rid}/decline", headers=bearer("token-w"))
        assert res.status_code == 400
        assert res.json() == DECLINE_STATE

    def test_the_owner_cannot_decline(self, fake_client):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        res = fake_client.post(f"/requests/{rid}/decline", headers=bearer("token-a"))
        assert res.status_code == 404
        assert res.json() == NOT_FOUND

    def test_another_walker_gets_404(self, fake_client, second_walker):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        assert fake_client.post(f"/requests/{rid}/decline",
                                headers=bearer("token-w2")).status_code == 404

    def test_non_uuid_id_is_400(self, fake_client):
        assert fake_client.post("/requests/xyz/decline",
                                headers=bearer("token-w")).status_code == 400

    def test_database_error_is_500(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        fake.fail_with = api_error("boom")
        assert fake_client.post(f"/requests/{rid}/decline",
                                headers=bearer("token-w")).status_code == 500


class TestCancel:
    def test_owner_cancels_a_pending_request_and_responded_at_stays_null(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        res = fake_client.post(f"/requests/{rid}/cancel", headers=bearer("token-a"))
        assert res.status_code == 200
        assert res.json() == {"id": rid, "status": "cancelled"}
        assert fake.tables["walk_requests"][0]["status"] == "cancelled"
        assert fake.tables["walk_requests"][0]["responded_at"] is None
        detail = fake_client.get(f"/requests/{rid}", headers=bearer("token-a")).json()
        assert detail["status"] == "cancelled"
        assert detail["respondedAt"] is None

    def test_owner_cancels_an_accepted_request_keeping_responded_at(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        accepted = fake_client.post(f"/requests/{rid}/accept", headers=bearer("token-w")).json()
        res = fake_client.post(f"/requests/{rid}/cancel", headers=bearer("token-a"))
        assert res.status_code == 200
        assert res.json() == {"id": rid, "status": "cancelled"}
        assert fake.tables["walk_requests"][0]["responded_at"] == accepted["respondedAt"]

    @pytest.mark.parametrize("first_action", ["decline", "cancel"])
    def test_cancelling_a_declined_or_cancelled_request_is_400(self, fake_client, first_action):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        if first_action == "decline":
            fake_client.post(f"/requests/{rid}/decline", headers=bearer("token-w"))
        else:
            fake_client.post(f"/requests/{rid}/cancel", headers=bearer("token-a"))
        res = fake_client.post(f"/requests/{rid}/cancel", headers=bearer("token-a"))
        assert res.status_code == 400
        assert res.json() == CANCEL_STATE

    def test_the_walker_cannot_cancel(self, fake_client):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        res = fake_client.post(f"/requests/{rid}/cancel", headers=bearer("token-w"))
        assert res.status_code == 404
        assert res.json() == NOT_FOUND
        assert fake.tables["walk_requests"][0]["status"] == "pending"

    def test_another_owner_gets_404(self, fake_client):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        assert fake_client.post(f"/requests/{rid}/cancel",
                                headers=bearer("token-b")).status_code == 404

    def test_non_uuid_id_is_400(self, fake_client):
        assert fake_client.post("/requests/xyz/cancel",
                                headers=bearer("token-a")).status_code == 400

    def test_database_error_is_500(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
        fake.fail_with = api_error("boom")
        assert fake_client.post(f"/requests/{rid}/cancel",
                                headers=bearer("token-a")).status_code == 500


def test_every_walk_requests_query_is_scoped_to_the_caller(fake_client, fake):
    profile = save_profile(fake_client)
    dog = create_dog(fake_client)
    rid = post_request(fake_client, profile["walkerId"], dog["id"]).json()["id"]
    other = post_request(fake_client, profile["walkerId"], dog["id"],
                         requested_time=T11).json()["id"]

    def walk_request_queries():
        return [q for q in fake.queries if q[0] == "walk_requests" and q[1] != "insert"]

    fake.queries.clear()
    fake_client.get("/requests", headers=bearer("token-a"))
    assert walk_request_queries(), "owner list must query walk_requests"
    for _t, _op, filters in walk_request_queries():
        assert ("owner_id", "owner-a") in filters

    fake.queries.clear()
    fake_client.get("/requests", headers=bearer("token-w"))
    for _t, _op, filters in walk_request_queries():
        assert ("walker_id", "walker-w") in filters

    fake.queries.clear()
    fake_client.get(f"/requests/{rid}", headers=bearer("token-w"))
    for _t, _op, filters in walk_request_queries():
        assert ("walker_id", "walker-w") in filters

    fake.queries.clear()
    fake_client.post(f"/requests/{rid}/accept", headers=bearer("token-w"))
    for _t, _op, filters in walk_request_queries():
        assert ("walker_id", "walker-w") in filters

    fake.queries.clear()
    fake_client.post(f"/requests/{other}/decline", headers=bearer("token-w"))
    for _t, _op, filters in walk_request_queries():
        assert ("walker_id", "walker-w") in filters

    fake.queries.clear()
    fake_client.post(f"/requests/{other}/cancel", headers=bearer("token-a"))
    for _t, _op, filters in walk_request_queries():
        assert ("owner_id", "owner-a") in filters


class TestPrivacyAfterResolution:
    """The full walker view exists ONLY while the request is accepted: a
    declined or cancelled request (even one cancelled after being accepted)
    goes back to the limited view — the pin, allergies and contacts disappear
    again. Pinned for the detail endpoint AND the inbox list items."""

    LIMITED_DOG = {"name": "Firulais", "breed": "Mixed", "size": "medium",
                   "temperament": "friendly"}

    def make_request(self, fake_client):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        set_questionnaire(fake_client, dog["id"])
        res = post_request(fake_client, profile["walkerId"], dog["id"])
        assert res.status_code == 201
        return res.json()["id"]

    def test_declined_detail_is_limited(self, fake_client):
        rid = self.make_request(fake_client)
        fake_client.post(f"/requests/{rid}/decline", headers=bearer("token-w"))
        res = fake_client.get(f"/requests/{rid}", headers=bearer("token-w"))
        assert res.status_code == 200
        dynamic, body = split_dynamic(res.json())
        assert dynamic["id"] == rid
        assert body == {
            "status": "declined",
            "requestedTime": T10,
            "priceCop": 25000,
            "dog": self.LIMITED_DOG,
        }

    def test_cancelled_from_pending_detail_is_limited(self, fake_client):
        rid = self.make_request(fake_client)
        fake_client.post(f"/requests/{rid}/cancel", headers=bearer("token-a"))
        res = fake_client.get(f"/requests/{rid}", headers=bearer("token-w"))
        dynamic, body = split_dynamic(res.json())
        assert body == {
            "status": "cancelled",
            "requestedTime": T10,
            "priceCop": 25000,
            "dog": self.LIMITED_DOG,
        }

    def test_cancelled_after_accept_hides_pin_and_questionnaire_again(self, fake_client):
        rid = self.make_request(fake_client)
        fake_client.post(f"/requests/{rid}/accept", headers=bearer("token-w"))
        full = fake_client.get(f"/requests/{rid}", headers=bearer("token-w")).json()
        assert full["pickupLat"] == 6.2  # sanity: the gate was open while accepted

        fake_client.post(f"/requests/{rid}/cancel", headers=bearer("token-a"))
        res = fake_client.get(f"/requests/{rid}", headers=bearer("token-w"))
        dynamic, body = split_dynamic(res.json())
        assert body == {
            "status": "cancelled",
            "requestedTime": T10,
            "priceCop": 25000,
            "dog": self.LIMITED_DOG,
        }

    def test_inbox_items_for_declined_and_cancelled_are_limited(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        set_questionnaire(fake_client, dog["id"])
        seed_request(fake, dog["id"], T09, status="declined", responded_at=T0930)
        seed_request(fake, dog["id"], T0930, status="cancelled", responded_at=T10)

        res = fake_client.get("/requests", headers=bearer("token-w"))
        assert res.status_code == 200
        items = res.json()
        assert [item["status"] for item in items] == ["declined", "cancelled"]
        for item in items:
            assert set(item) == {"id", "status", "requestedTime", "priceCop", "createdAt", "dog"}
            assert set(item["dog"]) == {"name", "breed", "size", "temperament"}
            assert item["dog"] == self.LIMITED_DOG


class TestDoubleBooking:
    """A dog cannot hold two active (pending|accepted) requests whose 60-minute
    windows overlap — with ANY walker. declined/cancelled never block. Same
    half-open window rule as the walker-side accept conflicts (FR-17)."""

    def test_same_walker_same_time_is_409_and_stores_nothing(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        first = post_request(fake_client, profile["walkerId"], dog["id"], requested_time=T10)
        assert first.status_code == 201
        second = post_request(fake_client, profile["walkerId"], dog["id"], requested_time=T10)
        assert second.status_code == 409
        assert second.json() == DOUBLE_BOOKED
        assert len(fake.tables["walk_requests"]) == 1

    def test_overlapping_window_with_a_different_walker_is_409(self, fake_client, second_walker):
        save_profile(fake_client)
        save_profile(fake_client, token="token-w2", displayName="Ana Packs")
        dog = create_dog(fake_client)
        first = post_request(fake_client, "walker-w", dog["id"], requested_time=T10)
        assert first.status_code == 201
        second = post_request(fake_client, "walker-w2", dog["id"], requested_time=T1030)
        assert second.status_code == 409
        assert second.json() == DOUBLE_BOOKED

    def test_back_to_back_at_plus_60_minutes_is_allowed(self, fake_client):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        assert post_request(fake_client, profile["walkerId"], dog["id"],
                            requested_time=T10).status_code == 201
        # [11:00, 12:00) touches [10:00, 11:00) — half-open, no overlap
        assert post_request(fake_client, profile["walkerId"], dog["id"],
                            requested_time=T11).status_code == 201

    def test_earlier_window_touching_from_below_is_allowed(self, fake_client):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        assert post_request(fake_client, profile["walkerId"], dog["id"],
                            requested_time=T10).status_code == 201
        assert post_request(fake_client, profile["walkerId"], dog["id"],
                            requested_time=T09).status_code == 201

    def test_a_declined_request_does_not_block(self, fake_client):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        first = post_request(fake_client, profile["walkerId"], dog["id"], requested_time=T10)
        fake_client.post(f"/requests/{first.json()['id']}/decline", headers=bearer("token-w"))
        assert post_request(fake_client, profile["walkerId"], dog["id"],
                            requested_time=T10).status_code == 201

    def test_a_cancelled_request_does_not_block(self, fake_client):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        first = post_request(fake_client, profile["walkerId"], dog["id"], requested_time=T10)
        fake_client.post(f"/requests/{first.json()['id']}/cancel", headers=bearer("token-a"))
        assert post_request(fake_client, profile["walkerId"], dog["id"],
                            requested_time=T10).status_code == 201

    def test_an_accepted_request_blocks_an_overlapping_one(self, fake_client):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        first = post_request(fake_client, profile["walkerId"], dog["id"], requested_time=T10)
        fake_client.post(f"/requests/{first.json()['id']}/accept", headers=bearer("token-w"))
        res = post_request(fake_client, profile["walkerId"], dog["id"], requested_time=T1030)
        assert res.status_code == 409


class TestRequestedTimeWindow:
    def test_a_past_time_is_400_and_stores_nothing(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        res = post_request(fake_client, profile["walkerId"], dog["id"],
                           requested_time="2026-09-30T12:00:00+00:00")
        assert res.status_code == 400
        assert res.json() == FUTURE_MESSAGE
        assert fake.tables.get("walk_requests", []) == []

    def test_exactly_now_is_allowed(self, fake_client):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        res = post_request(fake_client, profile["walkerId"], dog["id"],
                           requested_time=FIXED_NOW.isoformat())
        assert res.status_code == 201
        assert res.json()["requestedTime"] == FIXED_NOW.isoformat()


class TestPendingFlood:
    """An owner may hold at most 10 pending requests (cheap flood guard; the
    AI quota with 429 is a different S3 rule)."""

    DAYS = [f"2026-10-{day:02d}T10:00:00+00:00" for day in range(5, 15)]

    def test_the_eleventh_pending_request_is_400(self, fake_client, fake):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        for day in self.DAYS:
            res = post_request(fake_client, profile["walkerId"], dog["id"], requested_time=day)
            assert res.status_code == 201
        eleventh = post_request(fake_client, profile["walkerId"], dog["id"],
                                requested_time="2026-10-15T10:00:00+00:00")
        assert eleventh.status_code == 400
        assert eleventh.json() == FLOOD_MESSAGE
        assert len(fake.tables["walk_requests"]) == 10

    def test_resolving_one_frees_a_slot(self, fake_client):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        first = None
        for day in self.DAYS:
            res = post_request(fake_client, profile["walkerId"], dog["id"], requested_time=day)
            assert res.status_code == 201
            first = first or res.json()["id"]
        fake_client.post(f"/requests/{first}/cancel", headers=bearer("token-a"))
        res = post_request(fake_client, profile["walkerId"], dog["id"],
                           requested_time="2026-10-15T10:00:00+00:00")
        assert res.status_code == 201

    def test_resolved_requests_do_not_count_towards_the_limit(self, fake_client):
        profile = save_profile(fake_client)
        dog = create_dog(fake_client)
        for day in self.DAYS:
            res = post_request(fake_client, profile["walkerId"], dog["id"], requested_time=day)
            rid = res.json()["id"]
            fake_client.post(f"/requests/{rid}/decline", headers=bearer("token-w"))
        # all 10 declined -> a new pending one is fine
        res = post_request(fake_client, profile["walkerId"], dog["id"],
                           requested_time="2026-10-15T10:00:00+00:00")
        assert res.status_code == 201

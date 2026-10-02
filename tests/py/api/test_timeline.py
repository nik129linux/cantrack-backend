"""S3: per-dog owner timeline — ``GET /dogs/{id}/timeline?order=asc|desc``.

The timeline mixes the dog's ACCEPTED walks (by requested time) and its SENT
checkouts (by sent time) in one chronological feed, built with the
hand-written DoublyLinkedList (``to_array`` / ``to_array_reverse``). Drafts
never appear (the owner must not learn a checkout exists before it is sent),
and only the dog's owner can read the feed. Ties between a walk and a
checkout at the same timestamp break walk-first, then by id.
"""

from datetime import datetime, timezone

import pytest

from .conftest import bearer

PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"\x00" * 40

ORDER_MESSAGE = {"detail": "order must be 'asc' or 'desc'."}
DOG_NOT_FOUND = {"detail": "Dog not found."}

PROFILE = {"displayName": "Nico Walks", "bio": None, "serviceArea": "Laureles",
           "pricePerWalk": 25000}


def photo(data: bytes = PNG, name: str = "p.png"):
    return ("image", (name, data, "image/png"))


def save_profile(client, token="token-w"):
    assert client.put("/walker-profile", json=PROFILE, headers=bearer(token)).status_code == 200


def create_dog(client, token="token-a", name="Firulais"):
    res = client.post("/dogs", json={"name": name, "breed": "Mixed"}, headers=bearer(token))
    assert res.status_code == 201
    return res.json()


def accepted_request(client, dog_id, requested_time, token="token-a"):
    res = client.post(
        "/requests",
        json={"walkerId": "walker-w", "dogId": dog_id, "requestedTime": requested_time,
              "pickupLat": 6.2, "pickupLng": -75.5},
        headers=bearer(token),
    )
    assert res.status_code == 201
    rid = res.json()["id"]
    assert client.post(f"/requests/{rid}/accept", headers=bearer("token-w")).status_code == 200
    return rid


def send_checkout(client, rid):
    res = client.post(f"/requests/{rid}/checkout", files=[photo()], headers=bearer("token-w"))
    assert res.status_code == 201
    cid = res.json()["id"]
    assert client.post(f"/checkouts/{cid}/send", headers=bearer("token-w")).status_code == 200
    return cid


SENT_AT = "2026-10-05T15:00:00+00:00"


@pytest.fixture
def story(fake_client, fake_clock):
    """One dog with: an accepted walk (10-05 10:00), a SENT checkout stamped
    10-05 15:00 (the clock is moved before sending), a second accepted walk
    (10-06 09:00) and a DRAFT checkout that must stay invisible."""
    save_profile(fake_client)
    dog = create_dog(fake_client)
    walk1 = accepted_request(fake_client, dog["id"], "2026-10-05T10:00:00+00:00")
    fake_clock.now = datetime(2026, 10, 5, 15, 0, tzinfo=timezone.utc)
    checkout = send_checkout(fake_client, walk1)
    walk2 = accepted_request(fake_client, dog["id"], "2026-10-06T09:00:00+00:00")
    draft_res = fake_client.post(f"/requests/{walk2}/checkout", files=[photo()],
                                 headers=bearer("token-w"))
    assert draft_res.status_code == 201
    return {
        "dog": dog,
        "walk1": walk1,
        "walk2": walk2,
        "checkout": checkout,
        "draft": draft_res.json()["id"],
        "sent_at": SENT_AT,
    }


def walk_item(story, request_id, requested_time):
    return {
        "type": "walk",
        "requestId": request_id,
        "walkerId": "walker-w",
        "walkerName": "Nico Walks",
        "requestedTime": requested_time,
        "status": "accepted",
    }


def checkout_item(story):
    return {
        "type": "checkout",
        "checkoutId": story["checkout"],
        "requestId": story["walk1"],
        "walkerId": "walker-w",
        "walkerName": "Nico Walks",
        "note": "Calm and clean.",
        "sentAt": story["sent_at"],
    }


class TestAuthAndScope:
    def test_no_token_is_401(self, fake_client, story):
        assert fake_client.get(f"/dogs/{story['dog']['id']}/timeline").status_code == 401

    def test_another_owner_gets_404(self, fake_client, story):
        res = fake_client.get(f"/dogs/{story['dog']['id']}/timeline", headers=bearer("token-b"))
        assert res.status_code == 404
        assert res.json() == DOG_NOT_FOUND

    def test_a_walker_gets_404(self, fake_client, story):
        res = fake_client.get(f"/dogs/{story['dog']['id']}/timeline", headers=bearer("token-w"))
        assert res.status_code == 404
        assert res.json() == DOG_NOT_FOUND

    def test_a_bad_order_is_400(self, fake_client, story):
        res = fake_client.get(f"/dogs/{story['dog']['id']}/timeline?order=sideways",
                              headers=bearer("token-a"))
        assert res.status_code == 400
        assert res.json() == ORDER_MESSAGE

    def test_non_uuid_dog_is_400(self, fake_client):
        assert fake_client.get("/dogs/nope/timeline", headers=bearer("token-a")).status_code == 400


class TestFeed:
    def test_ascending_mixes_walks_and_sent_checkouts_in_time_order(self, fake_client, story):
        res = fake_client.get(f"/dogs/{story['dog']['id']}/timeline?order=asc",
                              headers=bearer("token-a"))
        assert res.status_code == 200
        assert res.json() == [
            walk_item(story, story["walk1"], "2026-10-05T10:00:00+00:00"),
            checkout_item(story),
            walk_item(story, story["walk2"], "2026-10-06T09:00:00+00:00"),
        ]

    def test_descending_is_the_exact_reverse_and_the_default(self, fake_client, story):
        dog_id = story["dog"]["id"]
        desc = fake_client.get(f"/dogs/{dog_id}/timeline?order=desc",
                               headers=bearer("token-a")).json()
        default = fake_client.get(f"/dogs/{dog_id}/timeline", headers=bearer("token-a")).json()
        assert desc == default == [
            walk_item(story, story["walk2"], "2026-10-06T09:00:00+00:00"),
            checkout_item(story),
            walk_item(story, story["walk1"], "2026-10-05T10:00:00+00:00"),
        ]

    def test_the_draft_checkout_never_appears(self, fake_client, story):
        body = fake_client.get(f"/dogs/{story['dog']['id']}/timeline?order=asc",
                               headers=bearer("token-a")).json()
        assert story["draft"] not in [item.get("checkoutId") for item in body]
        assert len(body) == 3

    def test_pending_and_declined_walks_are_not_timeline_events(self, fake_client, story):
        dog_id = story["dog"]["id"]
        pending = fake_client.post(
            "/requests",
            json={"walkerId": "walker-w", "dogId": dog_id,
                  "requestedTime": "2026-10-07T10:00:00+00:00",
                  "pickupLat": 6.2, "pickupLng": -75.5},
            headers=bearer("token-a"),
        ).json()["id"]
        body = fake_client.get(f"/dogs/{dog_id}/timeline?order=asc",
                               headers=bearer("token-a")).json()
        assert pending not in [item.get("requestId") for item in body]
        assert len(body) == 3

    def test_a_walk_and_a_checkout_at_the_same_instant_break_walk_first(
        self, fake_client, fake_clock, story
    ):
        # Send the story's walk2 DRAFT stamped exactly at walk2's requested
        # time. (This used to call send_checkout(walk2), which CREATES a
        # second checkout for a request that already has the story's draft —
        # a direct contradiction with the one-checkout-per-request 409 pinned
        # in test_checkouts.py; no implementation can satisfy both. Sending
        # the existing draft keeps this test's own pin — the walk-first tie —
        # intact.)
        fake_clock.now = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)
        cid = story["draft"]
        assert fake_client.post(f"/checkouts/{cid}/send",
                                headers=bearer("token-w")).status_code == 200
        body = fake_client.get(f"/dogs/{story['dog']['id']}/timeline?order=asc",
                               headers=bearer("token-a")).json()
        tail = body[-2:]
        assert [item["type"] for item in tail] == ["walk", "checkout"]
        assert tail[1]["checkoutId"] == cid


def test_timeline_queries_are_scoped_to_the_caller(fake_client, fake, story):
    fake.queries.clear()
    fake_client.get(f"/dogs/{story['dog']['id']}/timeline", headers=bearer("token-a"))
    reads = [q for q in fake.queries if q[0] in ("dogs", "walk_requests", "checkouts")
             and q[1] != "insert"]
    assert reads
    dogs_reads = [q for q in reads if q[0] == "dogs"]
    assert dogs_reads and ("owner_id", "owner-a") in dogs_reads[0][2]
    for _t, _op, filters in [q for q in reads if q[0] == "checkouts"]:
        assert ("owner_id", "owner-a") in filters

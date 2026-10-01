"""S3: checkout — photos from the walker to the owner, with a human gate.

Flow: a walker finishes an ACCEPTED request and uploads 1..3 photos
(``POST /requests/{id}/checkout``, multipart field ``image``); the API stores
them in the PRIVATE bucket ``checkout-photos`` under a server-built path
``{walker_id}/{checkout_id}/{index}.{ext}``, asks the vision model for an
observation and creates a ``draft``. Two fields keep the provenance apart:
``aiNote`` is what the model said (read-only; changed only by draft creation
and by an ``ai-note`` re-run) and ``note`` is the walker's own text (<= 200
chars, initially a copy of ``aiNote``, null when the AI gave nothing; the
ONLY field ``PATCH`` changes). ``dogVisible`` (true/false/null) tells the
walker whether the model saw a dog at all. The walker sends (``draft ->
sent``, ``sent_at`` from the clock); only then can the owner see anything —
and the owner sees ``note``, never the AI fields. Sent checkouts are
immutable.

Security pins in this file (think attacker + flaky network):
- magic bytes decide the type (a lying Content-Type header changes nothing),
  including the content-type stored with the object;
- an ``ai-note`` re-run replaces ``aiNote``, and replaces ``note`` only when
  the walker had not edited it (``note == previous aiNote`` or null); an
  unavailable re-run changes nothing and does not count toward the quota;
- 0 or 4 photos is a 400 BEFORE any storage or AI work; > 5 MiB is a 413;
- a failure on photo 2 rolls photo 1 back (no orphan objects, no row);
- client filenames (``../../etc/passwd``) never reach the object path;
- photos are served ONLY as signed URLs with ``expiresIn == 3600``; the fake
  records ``get_public_url`` so any call to it fails the suite;
- every checkouts query AND every storage call is scoped to the caller;
- the owner never sees a draft (list, timeline, by id: 404);
- quota: 60 counted AI answers per walker per UTC month; the 61st re-run is
  429 but draft creation still succeeds with ``aiStatus: "quota"``; counted =
  the model ANSWERED (``dog_visible is not None``) — an unavailable model must
  not burn the walker's allowance, and a quota-skipped call invokes nothing;
- retention: sent checkouts purge their photos at 30 days (29 keeps, 30
  purges), lazily on list; a failing removal keeps exactly the paths whose
  objects still exist;
- a request with a SENT checkout is complete: cancel is a 400 (S1 change).
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from postgrest.exceptions import APIError

from .conftest import FIXED_NOW, bearer, make_user

PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"\x00" * 40
JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"\x00" * 40
WEBP = b"RIFF" + (36).to_bytes(4, "little") + b"WEBP" + b"\x00" * 24
GARBAGE = b"#!/bin/sh\necho this is not an image\n" + b"\x00" * 16

FIVE_MIB = 5 * 1024 * 1024

BUCKET = "checkout-photos"

NOT_FOUND = {"detail": "Checkout not found."}
REQUEST_NOT_FOUND = {"detail": "Request not found."}
DOG_NOT_FOUND = {"detail": "Dog not found."}
WALKER_ROLE = {"detail": "Only walkers can create a checkout."}
CHECKOUT_STATE = {"detail": "Only an accepted request can have a checkout."}
CHECKOUT_EXISTS = {"detail": "This request already has a checkout."}
PHOTOS_COUNT = {"detail": "Between 1 and 3 photos are required."}
PHOTO_SIZE = {"detail": "Each photo must be 5 MiB or less."}
UNREADABLE = {"detail": "Unreadable image."}
IMMUTABLE = {"detail": "A sent checkout is immutable."}
NOTE_LEN = {"detail": "The note must be 200 characters or less."}
QUOTA_429 = {"detail": "Monthly AI limit reached."}
QUOTA_ROLE = {"detail": "Only walkers have an AI quota."}
COMPLETED_CANCEL = {"detail": "A completed request cannot be cancelled."}
LIMIT_MESSAGE = {"detail": "limit must be between 1 and 100."}

PROFILE = {"displayName": "Nico Walks", "bio": None, "serviceArea": "Laureles",
           "pricePerWalk": 25000}


def api_error(message="connection lost"):
    return APIError({"message": message, "code": "XX000", "hint": None, "details": None})


def photo(data: bytes, name: str = "p.png", mime: str = "image/png"):
    return ("image", (name, data, mime))


@pytest.fixture
def second_walker(fake):
    fake.add_user("token-w2", make_user("walker-w2", "walker-w2@example.com", "walker"))


def save_profile(client, token="token-w", **over):
    res = client.put("/walker-profile", json={**PROFILE, **over}, headers=bearer(token))
    assert res.status_code == 200


def create_dog(client, token="token-a", name="Firulais"):
    res = client.post("/dogs", json={"name": name, "breed": "Mixed"}, headers=bearer(token))
    assert res.status_code == 201
    return res.json()


def accepted_request(client, dog_id, token="token-a", walker="walker-w",
                     requested_time="2026-10-05T10:00:00+00:00"):
    res = client.post(
        "/requests",
        json={"walkerId": walker, "dogId": dog_id, "requestedTime": requested_time,
              "pickupLat": 6.2, "pickupLng": -75.5},
        headers=bearer(token),
    )
    assert res.status_code == 201
    rid = res.json()["id"]
    walk_token = "token-w" if walker == "walker-w" else "token-w2"
    assert client.post(f"/requests/{rid}/accept", headers=bearer(walk_token)).status_code == 200
    return rid


def post_checkout(client, rid, photos=None, token="token-w"):
    photos = photos if photos is not None else [photo(PNG)]
    return client.post(f"/requests/{rid}/checkout", files=photos, headers=bearer(token))


def split_dynamic(body):
    rest = dict(body)
    dynamic = {"id": rest.pop("id"), "createdAt": rest.pop("createdAt")}
    assert dynamic["id"] and dynamic["createdAt"]
    return dynamic, rest


class TestAuthRequired:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("post", f"/requests/{'r-1'}/checkout"),
            ("get", "/checkouts"),
            ("get", "/checkouts/c-1"),
            ("patch", "/checkouts/c-1"),
            ("post", "/checkouts/c-1/send"),
            ("post", "/checkouts/c-1/ai-note"),
            ("get", "/ai-quota"),
            ("get", "/requests/r-1/checkout"),
            ("get", f"/dogs/{'d-1'}/timeline"),
        ],
    )
    def test_no_token_is_401(self, fake_client, method, path):
        assert getattr(fake_client, method)(path).status_code == 401


class TestDraftCreation:
    def test_creates_a_draft_with_signed_photos_and_the_ai_note(self, fake_client, fake, vision):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])

        res = post_checkout(fake_client, rid, [photo(PNG), photo(JPEG, "q.jpg", "image/jpeg")])
        assert res.status_code == 201
        dynamic, body = split_dynamic(res.json())
        cid = dynamic["id"]
        assert body == {
            "requestId": rid,
            "dogId": dog["id"],
            "dogName": "Firulais",
            "walkerId": "walker-w",
            "status": "draft",
            "photos": [
                {"url": f"https://supabase.example/signed/{BUCKET}/walker-w/{cid}/1.png?expires=3600",
                 "expiresIn": 3600},
                {"url": f"https://supabase.example/signed/{BUCKET}/walker-w/{cid}/2.jpg?expires=3600",
                 "expiresIn": 3600},
            ],
            "aiNote": "Calm and clean.",
            "dogVisible": True,
            "note": "Calm and clean.",
            "aiStatus": "ok",
            "sentAt": None,
        }
        # storage: private bucket, server-built paths, no public URL ever
        uploads = [c for c in fake.storage.calls if c[1] == "upload"]
        assert [u[0] for u in uploads] == [BUCKET, BUCKET]
        assert [u[2] for u in uploads] == [f"walker-w/{cid}/1.png", f"walker-w/{cid}/2.jpg"]
        assert not [c for c in fake.storage.calls if c[1] == "public"]
        assert fake.storage.objects[(BUCKET, f"walker-w/{cid}/1.png")] == PNG
        # the AI was asked once and counted once
        assert len(vision.calls) == 1
        assert fake_client.get("/ai-quota", headers=bearer("token-w")).json()["used"] == 1
        # no raw path or public url leaks in the payload
        assert "public" not in json.dumps(res.json())

    def test_a_client_filename_cannot_escape_the_object_path(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        res = post_checkout(fake_client, rid, [photo(PNG, name="../../../etc/passwd")])
        assert res.status_code == 201
        cid = res.json()["id"]
        paths = [c[2] for c in fake.storage.calls if c[1] == "upload"]
        assert paths == [f"walker-w/{cid}/1.png"]
        assert list(fake.storage.objects) == [(BUCKET, f"walker-w/{cid}/1.png")]

    def test_webp_magic_bytes_are_accepted(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        res = post_checkout(fake_client, rid, [photo(WEBP, "r.webp", "image/webp")])
        assert res.status_code == 201
        cid = res.json()["id"]
        assert list(fake.storage.objects) == [(BUCKET, f"walker-w/{cid}/1.webp")]

    def test_the_stored_content_type_follows_the_magic_bytes_not_the_header(
        self, fake_client, fake
    ):
        """A mislabeled file must not be served to the owner with the wrong
        type: the upload's ``content-type`` option is derived from the magic
        bytes (png body claiming jpeg, jpeg body claiming png, webp body
        claiming jpeg) — never from the client header."""
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        res = post_checkout(fake_client, rid, [
            photo(PNG, "lying.jpg", "image/jpeg"),
            photo(JPEG, "lying.png", "image/png"),
            photo(WEBP, "lying.jpeg", "image/jpeg"),
        ])
        assert res.status_code == 201
        cid = res.json()["id"]
        uploads = [c for c in fake.storage.calls if c[1] == "upload"]
        assert [u[2] for u in uploads] == [
            f"walker-w/{cid}/1.png", f"walker-w/{cid}/2.jpg", f"walker-w/{cid}/3.webp",
        ]
        assert [u[3]["content-type"] for u in uploads] == [
            "image/png", "image/jpeg", "image/webp",
        ]

    def test_the_owner_cannot_create_a_checkout(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        res = post_checkout(fake_client, rid, token="token-a")
        assert res.status_code == 400
        assert res.json() == WALKER_ROLE
        assert fake.storage.objects == {}

    def test_another_walker_gets_404(self, fake_client, fake, second_walker):
        save_profile(fake_client)
        save_profile(fake_client, token="token-w2", displayName="Ana Packs")
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        res = post_checkout(fake_client, rid, token="token-w2")
        assert res.status_code == 404
        assert res.json() == REQUEST_NOT_FOUND
        assert fake.storage.objects == {}

    @pytest.mark.parametrize("status_action", ["none", "decline", "cancel"])
    def test_only_accepted_requests_can_be_checked_out(self, fake_client, fake, status_action):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        res = fake_client.post(
            "/requests",
            json={"walkerId": "walker-w", "dogId": dog["id"],
                  "requestedTime": "2026-10-05T10:00:00+00:00",
                  "pickupLat": 6.2, "pickupLng": -75.5},
            headers=bearer("token-a"),
        )
        rid = res.json()["id"]
        if status_action == "decline":
            fake_client.post(f"/requests/{rid}/decline", headers=bearer("token-w"))
        elif status_action == "cancel":
            fake_client.post(f"/requests/{rid}/cancel", headers=bearer("token-a"))
        res = post_checkout(fake_client, rid)
        assert res.status_code == 400
        assert res.json() == CHECKOUT_STATE
        assert fake.storage.objects == {}

    def test_zero_or_four_photos_are_400_before_any_storage_or_ai(self, fake_client, fake, vision):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        assert post_checkout(fake_client, rid, []).status_code == 400
        four = [photo(PNG, name=f"{i}.png") for i in range(4)]
        res = post_checkout(fake_client, rid, four)
        assert res.status_code == 400
        assert res.json() == PHOTOS_COUNT
        assert fake.storage.objects == {}
        assert fake.storage.calls == []
        assert vision.calls == []
        assert fake.tables.get("checkouts", []) == []

    def test_a_5_mib_photo_is_allowed_and_one_byte_more_is_413(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        exact = PNG + b"\x00" * (FIVE_MIB - len(PNG))
        assert post_checkout(fake_client, rid, [photo(exact)]).status_code == 201

        rid2 = accepted_request(fake_client, dog["id"], requested_time="2026-10-06T10:00:00+00:00")
        too_big = PNG + b"\x00" * (FIVE_MIB + 1 - len(PNG))
        res = post_checkout(fake_client, rid2, [photo(too_big)])
        assert res.status_code == 413
        assert res.json() == PHOTO_SIZE
        assert fake.tables["checkouts"] == [fake.tables["checkouts"][0]]

    def test_a_lying_content_type_does_not_help_garbage_bytes(self, fake_client, fake, vision):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        res = post_checkout(fake_client, rid, [photo(GARBAGE, mime="image/png")])
        assert res.status_code == 400
        assert res.json() == UNREADABLE
        assert fake.storage.objects == {}
        assert vision.calls == []
        assert fake.tables.get("checkouts", []) == []

    def test_a_failure_on_photo_2_rolls_photo_1_back(self, fake_client, fake, vision):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        res = post_checkout(fake_client, rid, [photo(PNG), photo(GARBAGE)])
        assert res.status_code == 400
        assert res.json() == UNREADABLE
        assert fake.storage.objects == {}
        assert [c[1] for c in fake.storage.calls].count("upload") == 1  # rolled back by remove
        assert vision.calls == []
        assert fake.tables.get("checkouts", []) == []

    def test_a_storage_failure_on_photo_2_rolls_photo_1_back(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        # make the SECOND upload blow up at the storage layer
        calls = {"n": 0}
        original = fake.storage.from_(BUCKET).upload

        def flaky(path, file, file_options=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise APIError({"message": "storage down", "code": "XX000",
                                "hint": None, "details": None})
            return original(path, file, file_options)

        fake.storage.buckets[BUCKET].upload = flaky
        res = post_checkout(fake_client, rid, [photo(PNG), photo(JPEG, "q.jpg", "image/jpeg")])
        assert res.status_code == 500
        assert fake.storage.objects == {}
        assert fake.tables.get("checkouts", []) == []

    def test_a_second_checkout_for_the_same_request_is_409(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        assert post_checkout(fake_client, rid).status_code == 201
        res = post_checkout(fake_client, rid)
        assert res.status_code == 409
        assert res.json() == CHECKOUT_EXISTS

    def test_a_long_ai_note_is_cut_to_200_chars(self, fake_client, vision):
        vision.result = type(vision.result)(dog_visible=True, note="x" * 250)
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        body = post_checkout(fake_client, rid).json()
        assert body["aiNote"] == "x" * 200
        assert body["note"] == "x" * 200  # the initial copy is the CUT text
        assert body["dogVisible"] is True
        assert body["aiStatus"] == "ok"

    def test_no_dog_visible_means_no_note_but_a_working_draft(self, fake_client, vision):
        vision.result = type(vision.result)(dog_visible=False, note="An empty room.")
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        body = post_checkout(fake_client, rid).json()
        assert body["aiNote"] is None
        assert body["note"] is None
        assert body["dogVisible"] is False  # the walker is TOLD no dog was seen
        assert body["aiStatus"] == "ok"
        assert body["status"] == "draft"

    def test_an_unavailable_model_never_blocks_and_never_counts(self, fake_client, vision):
        vision.result = type(vision.result)(dog_visible=None, note=None)
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        body = post_checkout(fake_client, rid).json()
        assert body["aiNote"] is None
        assert body["note"] is None
        assert body["dogVisible"] is None  # nobody answered: unknown, not "no dog"
        assert body["aiStatus"] == "unavailable"
        assert body["status"] == "draft"
        assert fake_client.get("/ai-quota", headers=bearer("token-w")).json()["used"] == 0

    def test_database_error_is_500(self, fake_client, fake):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        fake.fail_with = api_error()
        res = post_checkout(fake_client, rid)
        assert res.status_code == 500
        assert res.json() == {"detail": "connection lost"}


class TestEditNote:
    def draft(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        res = post_checkout(fake_client, rid)
        return res.json(), rid, dog

    def test_the_walker_edits_the_note_and_the_ai_provenance_survives(self, fake_client):
        draft, _rid, _dog = self.draft(fake_client)
        res = fake_client.patch(
            f"/checkouts/{draft['id']}", json={"note": "Rocky waited at the door."},
            headers=bearer("token-w"),
        )
        assert res.status_code == 200
        # PATCH changes ONLY the walker's field; what the model said is kept
        assert res.json()["note"] == "Rocky waited at the door."
        assert res.json()["aiNote"] == "Calm and clean."
        assert res.json()["dogVisible"] is True

    def test_patch_null_clears_the_note_for_a_photos_only_checkout(self, fake_client):
        draft, _rid, _dog = self.draft(fake_client)
        res = fake_client.patch(
            f"/checkouts/{draft['id']}", json={"note": None}, headers=bearer("token-w"),
        )
        assert res.status_code == 200
        assert res.json()["note"] is None
        assert res.json()["aiNote"] == "Calm and clean."  # provenance is never cleared

    def test_a_201_char_note_is_400_and_changes_nothing(self, fake_client):
        draft, _rid, _dog = self.draft(fake_client)
        res = fake_client.patch(
            f"/checkouts/{draft['id']}", json={"note": "y" * 201}, headers=bearer("token-w"),
        )
        assert res.status_code == 400
        assert res.json() == NOTE_LEN
        stored = fake_client.get(f"/checkouts/{draft['id']}", headers=bearer("token-w")).json()
        assert stored["note"] == "Calm and clean."
        assert stored["aiNote"] == "Calm and clean."

    def test_a_sent_checkout_is_immutable(self, fake_client):
        draft, _rid, _dog = self.draft(fake_client)
        fake_client.post(f"/checkouts/{draft['id']}/send", headers=bearer("token-w"))
        res = fake_client.patch(
            f"/checkouts/{draft['id']}", json={"note": "too late"}, headers=bearer("token-w"),
        )
        assert res.status_code == 400
        assert res.json() == IMMUTABLE

    def test_the_owner_cannot_even_see_the_draft_to_edit_it(self, fake_client):
        draft, _rid, _dog = self.draft(fake_client)
        res = fake_client.patch(
            f"/checkouts/{draft['id']}", json={"note": "hack"}, headers=bearer("token-a"),
        )
        assert res.status_code == 404
        assert res.json() == NOT_FOUND

    def test_another_walker_gets_404(self, fake_client, second_walker):
        draft, _rid, _dog = self.draft(fake_client)
        res = fake_client.patch(
            f"/checkouts/{draft['id']}", json={"note": "hack"}, headers=bearer("token-w2"),
        )
        assert res.status_code == 404
        assert res.json() == NOT_FOUND


class TestSend:
    def draft(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        return post_checkout(fake_client, rid).json(), rid, dog

    def test_send_stamps_sent_at_from_the_clock_and_opens_the_owner_view(self, fake_client):
        draft, rid, dog = self.draft(fake_client)
        res = fake_client.post(f"/checkouts/{draft['id']}/send", headers=bearer("token-w"))
        assert res.status_code == 200
        assert res.json() == {
            "id": draft["id"],
            "requestId": rid,
            "status": "sent",
            "sentAt": FIXED_NOW.isoformat(),
        }
        # the draft was never edited: note is the initial copy of aiNote, so
        # the owner receives the AI text the walker approved by sending
        owner_view = fake_client.get(f"/checkouts/{draft['id']}", headers=bearer("token-a")).json()
        cid = draft["id"]
        assert owner_view == {
            "id": cid,
            "requestId": rid,
            "dogId": dog["id"],
            "dogName": "Firulais",
            "walkerId": "walker-w",
            "walkerName": "Nico Walks",
            "note": "Calm and clean.",
            "photos": [
                {"url": f"https://supabase.example/signed/{BUCKET}/walker-w/{cid}/1.png?expires=3600",
                 "expiresIn": 3600},
            ],
            "sentAt": FIXED_NOW.isoformat(),
        }
        assert "aiStatus" not in owner_view
        assert "aiNote" not in owner_view

    def test_sending_a_photos_only_draft_publishes_a_null_note(self, fake_client, vision):
        """AI gave nothing (unavailable) and the walker typed nothing: sending
        publishes the photos with note null — the owner sees no invented text."""
        vision.result = type(vision.result)(dog_visible=None, note=None)
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        draft = post_checkout(fake_client, rid).json()
        assert draft["note"] is None
        assert fake_client.post(f"/checkouts/{draft['id']}/send",
                                headers=bearer("token-w")).status_code == 200
        owner_view = fake_client.get(f"/checkouts/{draft['id']}",
                                     headers=bearer("token-a")).json()
        assert owner_view["note"] is None
        assert len(owner_view["photos"]) == 1
        assert "aiNote" not in owner_view and "dogVisible" not in owner_view

    def test_a_cleared_note_is_published_as_null_too(self, fake_client):
        """The walker reviewed the AI text and removed it: the owner gets the
        photos alone (the AI provenance stays on the walker side)."""
        draft, _rid, _dog = self.draft(fake_client)
        cid = draft["id"]
        assert fake_client.patch(f"/checkouts/{cid}", json={"note": None},
                                 headers=bearer("token-w")).status_code == 200
        assert fake_client.post(f"/checkouts/{cid}/send",
                                headers=bearer("token-w")).status_code == 200
        owner_view = fake_client.get(f"/checkouts/{cid}", headers=bearer("token-a")).json()
        assert owner_view["note"] is None

    def test_a_second_send_is_400_and_never_re_sends(self, fake_client):
        draft, _rid, _dog = self.draft(fake_client)
        assert fake_client.post(f"/checkouts/{draft['id']}/send",
                                headers=bearer("token-w")).status_code == 200
        res = fake_client.post(f"/checkouts/{draft['id']}/send", headers=bearer("token-w"))
        assert res.status_code == 400
        assert res.json() == IMMUTABLE

    def test_the_owner_cannot_send_someone_else_s_draft(self, fake_client):
        draft, _rid, _dog = self.draft(fake_client)
        res = fake_client.post(f"/checkouts/{draft['id']}/send", headers=bearer("token-a"))
        assert res.status_code == 404
        assert res.json() == NOT_FOUND

    def test_unknown_checkout_is_404(self, fake_client):
        res = fake_client.post("/checkouts/nope/send", headers=bearer("token-w"))
        assert res.status_code == 404
        assert res.json() == NOT_FOUND


class TestAiNoteRerunAndQuota:
    def setup_draft(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        return post_checkout(fake_client, rid).json()

    def test_a_rerun_on_an_untouched_draft_replaces_both_fields(self, fake_client, vision):
        draft = self.setup_draft(fake_client)  # note == aiNote: never edited
        vision.result = type(vision.result)(dog_visible=True, note="Second look: all good.")
        res = fake_client.post(f"/checkouts/{draft['id']}/ai-note", headers=bearer("token-w"))
        assert res.status_code == 200
        assert res.json()["aiNote"] == "Second look: all good."
        assert res.json()["note"] == "Second look: all good."  # untouched -> follows the model
        assert res.json()["dogVisible"] is True
        assert res.json()["aiStatus"] == "ok"
        assert fake_client.get("/ai-quota", headers=bearer("token-w")).json() == {
            "used": 2, "limit": 60, "resetsAt": "2026-11-01T00:00:00+00:00",
        }

    def test_a_rerun_never_destroys_a_note_the_walker_edited(self, fake_client, vision):
        draft = self.setup_draft(fake_client)
        assert fake_client.patch(
            f"/checkouts/{draft['id']}", json={"note": "Rocky waited at the door."},
            headers=bearer("token-w"),
        ).status_code == 200
        vision.result = type(vision.result)(dog_visible=True, note="The model disagrees.")
        res = fake_client.post(f"/checkouts/{draft['id']}/ai-note", headers=bearer("token-w"))
        assert res.status_code == 200
        assert res.json()["aiNote"] == "The model disagrees."  # provenance refreshed
        assert res.json()["note"] == "Rocky waited at the door."  # the human edit SURVIVES

    def test_a_rerun_fills_a_null_note_and_counts_exactly_once(self, fake_client, vision):
        """Draft created while the model was down (aiNote null, note null,
        used 0). A re-run that gets an answer fills BOTH fields (the walker
        never typed anything) and counts once."""
        vision.result = type(vision.result)(dog_visible=None, note=None)
        draft = self.setup_draft(fake_client)
        assert draft["aiStatus"] == "unavailable"
        assert fake_client.get("/ai-quota", headers=bearer("token-w")).json()["used"] == 0

        vision.result = type(vision.result)(dog_visible=True, note="Now I see the dog.")
        res = fake_client.post(f"/checkouts/{draft['id']}/ai-note", headers=bearer("token-w"))
        assert res.status_code == 200
        assert res.json()["aiNote"] == "Now I see the dog."
        assert res.json()["note"] == "Now I see the dog."
        assert res.json()["dogVisible"] is True
        assert res.json()["aiStatus"] == "ok"
        assert fake_client.get("/ai-quota", headers=bearer("token-w")).json()["used"] == 1

    def test_an_unavailable_rerun_changes_nothing_and_does_not_count(self, fake_client, vision):
        """Flaky network: the walker asks again and the model is down. No data
        is destroyed (an infrastructure failure must not erase the last good
        observation nor the human note) and no quota is burned."""
        draft = self.setup_draft(fake_client)  # answered once: used 1
        assert fake_client.patch(
            f"/checkouts/{draft['id']}", json={"note": "Rocky waited at the door."},
            headers=bearer("token-w"),
        ).status_code == 200
        vision.result = type(vision.result)(dog_visible=None, note=None)
        res = fake_client.post(f"/checkouts/{draft['id']}/ai-note", headers=bearer("token-w"))
        assert res.status_code == 200
        assert res.json()["aiStatus"] == "unavailable"
        assert res.json()["aiNote"] == "Calm and clean."  # last answered observation kept
        assert res.json()["note"] == "Rocky waited at the door."
        assert res.json()["dogVisible"] is True
        assert fake_client.get("/ai-quota", headers=bearer("token-w")).json()["used"] == 1

    def test_the_61st_call_is_429_but_drafts_still_work(self, fake_client, vision):
        draft = self.setup_draft(fake_client)  # call 1
        for _ in range(59):  # calls 2..60
            res = fake_client.post(f"/checkouts/{draft['id']}/ai-note", headers=bearer("token-w"))
            assert res.status_code == 200
        assert fake_client.get("/ai-quota", headers=bearer("token-w")).json()["used"] == 60

        res = fake_client.post(f"/checkouts/{draft['id']}/ai-note", headers=bearer("token-w"))
        assert res.status_code == 429
        assert res.json() == QUOTA_429

        # a new draft at the limit skips the model entirely and says so
        before = len(vision.calls)
        dog2 = create_dog(fake_client, name="Rex")
        rid2 = accepted_request(fake_client, dog2["id"], requested_time="2026-10-06T10:00:00+00:00")
        draft2 = post_checkout(fake_client, rid2).json()
        assert draft2["aiStatus"] == "quota"
        assert draft2["aiNote"] is None
        assert draft2["note"] is None
        assert draft2["dogVisible"] is None
        assert len(vision.calls) == before

    def test_the_counter_resets_at_the_utc_month_boundary(self, fake_client, fake_clock):
        draft = self.setup_draft(fake_client)
        for _ in range(59):
            fake_client.post(f"/checkouts/{draft['id']}/ai-note", headers=bearer("token-w"))

        fake_clock.now = datetime(2026, 10, 31, 23, 59, 59, tzinfo=timezone.utc)
        res = fake_client.post(f"/checkouts/{draft['id']}/ai-note", headers=bearer("token-w"))
        assert res.status_code == 429
        quota = fake_client.get("/ai-quota", headers=bearer("token-w")).json()
        assert quota == {"used": 60, "limit": 60, "resetsAt": "2026-11-01T00:00:00+00:00"}

        fake_clock.now = datetime(2026, 11, 1, 0, 0, 0, tzinfo=timezone.utc)
        quota = fake_client.get("/ai-quota", headers=bearer("token-w")).json()
        assert quota == {"used": 0, "limit": 60, "resetsAt": "2026-12-01T00:00:00+00:00"}
        assert fake_client.post(f"/checkouts/{draft['id']}/ai-note",
                                headers=bearer("token-w")).status_code == 200

    def test_another_walkers_usage_never_counts(self, fake_client, second_walker):
        self.setup_draft(fake_client)
        quota = fake_client.get("/ai-quota", headers=bearer("token-w2")).json()
        assert quota == {"used": 0, "limit": 60, "resetsAt": "2026-11-01T00:00:00+00:00"}

    def test_the_owner_has_no_quota_endpoint(self, fake_client):
        res = fake_client.get("/ai-quota", headers=bearer("token-a"))
        assert res.status_code == 400
        assert res.json() == QUOTA_ROLE

    def test_rerun_on_a_sent_checkout_is_400(self, fake_client):
        draft = self.setup_draft(fake_client)
        fake_client.post(f"/checkouts/{draft['id']}/send", headers=bearer("token-w"))
        res = fake_client.post(f"/checkouts/{draft['id']}/ai-note", headers=bearer("token-w"))
        assert res.status_code == 400
        assert res.json() == IMMUTABLE


class TestPrivacy:
    def setup_draft(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        return post_checkout(fake_client, rid).json(), rid

    def test_the_owner_never_sees_a_draft_not_in_the_list(self, fake_client):
        draft, _rid = self.setup_draft(fake_client)
        listing = fake_client.get("/checkouts", headers=bearer("token-a")).json()
        assert listing == []
        assert [c["id"] for c in fake_client.get("/checkouts", headers=bearer("token-w")).json()] == [draft["id"]]

    def test_the_owner_never_sees_a_draft_by_id(self, fake_client):
        draft, _rid = self.setup_draft(fake_client)
        res = fake_client.get(f"/checkouts/{draft['id']}", headers=bearer("token-a"))
        assert res.status_code == 404
        assert res.json() == NOT_FOUND

    def test_the_owner_never_sees_a_draft_through_the_request(self, fake_client):
        draft, rid = self.setup_draft(fake_client)
        res = fake_client.get(f"/requests/{rid}/checkout", headers=bearer("token-a"))
        assert res.status_code == 404
        assert res.json() == NOT_FOUND
        walker_res = fake_client.get(f"/requests/{rid}/checkout", headers=bearer("token-w"))
        assert walker_res.status_code == 200
        assert walker_res.json()["id"] == draft["id"]

    def test_another_owner_and_another_walker_get_404_everywhere(self, fake_client, second_walker):
        draft, rid = self.setup_draft(fake_client)
        fake_client.post(f"/checkouts/{draft['id']}/send", headers=bearer("token-w"))
        for token in ("token-b", "token-w2"):
            assert fake_client.get(f"/checkouts/{draft['id']}",
                                   headers=bearer(token)).status_code == 404
            assert fake_client.get(f"/requests/{rid}/checkout",
                                   headers=bearer(token)).status_code == 404
            assert fake_client.get("/checkouts", headers=bearer(token)).json() == []


class TestLists:
    def sent_pair(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid1 = accepted_request(fake_client, dog["id"], requested_time="2026-10-05T10:00:00+00:00")
        first = post_checkout(fake_client, rid1).json()
        fake_client.post(f"/checkouts/{first['id']}/send", headers=bearer("token-w"))
        rid2 = accepted_request(fake_client, dog["id"], requested_time="2026-10-06T10:00:00+00:00")
        second = post_checkout(fake_client, rid2).json()
        fake_client.post(f"/checkouts/{second['id']}/send", headers=bearer("token-w"))
        return first, second

    def test_the_owner_list_is_sent_only_newest_first(self, fake_client, fake_clock):
        first, second = self.sent_pair(fake_client)
        # move the clock so the second send is strictly newer
        listing = fake_client.get("/checkouts", headers=bearer("token-a")).json()
        assert [c["id"] for c in listing] == [second["id"], first["id"]]
        assert all(set(c) == {"id", "requestId", "dogId", "dogName", "walkerId",
                              "walkerName", "note", "photos", "sentAt"} for c in listing)

    def test_limit_is_capped_and_validated(self, fake_client):
        self.sent_pair(fake_client)
        assert len(fake_client.get("/checkouts?limit=1", headers=bearer("token-a")).json()) == 1
        assert fake_client.get("/checkouts?limit=0", headers=bearer("token-a")).status_code == 400
        res = fake_client.get("/checkouts?limit=101", headers=bearer("token-a"))
        assert res.status_code == 400
        assert res.json() == LIMIT_MESSAGE

    def test_the_walker_list_keeps_drafts_in_created_order(self, fake_client):
        first, second = self.sent_pair(fake_client)
        listing = fake_client.get("/checkouts", headers=bearer("token-w")).json()
        assert [c["id"] for c in listing] == [second["id"], first["id"]]
        assert all(c["status"] == "sent" for c in listing)


class TestRetention:
    def sent_at(self, fake_client, fake_clock, age_days):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        draft = post_checkout(fake_client, rid, [photo(PNG), photo(JPEG, "q.jpg", "image/jpeg")]).json()
        fake_client.post(f"/checkouts/{draft['id']}/send", headers=bearer("token-w"))
        fake_clock.now = FIXED_NOW + timedelta(days=age_days)
        return draft

    def test_day_29_keeps_the_photos(self, fake_client, fake, fake_clock):
        draft = self.sent_at(fake_client, fake_clock, 29)
        listing = fake_client.get("/checkouts", headers=bearer("token-a")).json()
        assert len(listing[0]["photos"]) == 2
        cid = draft["id"]
        assert (BUCKET, f"walker-w/{cid}/1.png") in fake.storage.objects

    def test_day_30_purges_objects_and_paths_but_keeps_the_row(self, fake_client, fake, fake_clock):
        draft = self.sent_at(fake_client, fake_clock, 30)
        cid = draft["id"]
        listing = fake_client.get("/checkouts", headers=bearer("token-a")).json()
        assert len(listing) == 1
        assert listing[0]["photos"] == []
        assert listing[0]["note"] == "Calm and clean."
        assert fake.storage.objects == {}
        row = fake.tables["checkouts"][0]
        assert row["id"] == cid
        assert row["photo_paths"] == []
        assert row["status"] == "sent"

    def test_a_failing_removal_keeps_exactly_the_paths_that_still_exist(
        self, fake_client, fake, fake_clock
    ):
        draft = self.sent_at(fake_client, fake_clock, 30)
        cid = draft["id"]
        fake.storage.fail_remove_paths = {f"walker-w/{cid}/1.png"}
        listing = fake_client.get("/checkouts", headers=bearer("token-a")).json()
        assert len(listing[0]["photos"]) == 1
        assert listing[0]["photos"][0]["url"].endswith(f"walker-w/{cid}/1.png?expires=3600")
        assert list(fake.storage.objects) == [(BUCKET, f"walker-w/{cid}/1.png")]
        assert fake.tables["checkouts"][0]["photo_paths"] == [f"walker-w/{cid}/1.png"]

    def test_the_walker_list_purges_too(self, fake_client, fake, fake_clock):
        draft = self.sent_at(fake_client, fake_clock, 31)
        listing = fake_client.get("/checkouts", headers=bearer("token-w")).json()
        assert listing[0]["photos"] == []
        assert fake.storage.objects == {}
        assert draft["id"] == listing[0]["id"]


class TestCompletedRequests:
    def test_a_request_with_a_sent_checkout_cannot_be_cancelled(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        draft = post_checkout(fake_client, rid).json()
        fake_client.post(f"/checkouts/{draft['id']}/send", headers=bearer("token-w"))
        res = fake_client.post(f"/requests/{rid}/cancel", headers=bearer("token-a"))
        assert res.status_code == 400
        assert res.json() == COMPLETED_CANCEL

    def test_a_draft_alone_does_not_complete_the_request(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        post_checkout(fake_client, rid)
        assert fake_client.post(f"/requests/{rid}/cancel",
                                headers=bearer("token-a")).status_code == 200


def test_every_checkouts_query_and_storage_call_is_scoped(fake_client, fake):
    save_profile(fake_client)
    dog = create_dog(fake_client)
    rid = accepted_request(fake_client, dog["id"])
    draft = post_checkout(fake_client, rid).json()
    cid = draft["id"]

    fake.queries.clear()
    fake_client.get("/checkouts", headers=bearer("token-w"))
    walker_reads = [q for q in fake.queries if q[0] == "checkouts"]
    assert walker_reads
    for _t, _op, filters in walker_reads:
        assert ("walker_id", "walker-w") in filters

    fake.queries.clear()
    fake_client.get("/checkouts", headers=bearer("token-a"))
    owner_reads = [q for q in fake.queries if q[0] == "checkouts"]
    assert owner_reads
    for _t, _op, filters in owner_reads:
        assert ("owner_id", "owner-a") in filters

    fake.queries.clear()
    fake_client.get(f"/checkouts/{cid}", headers=bearer("token-w"))
    for _t, _op, filters in [q for q in fake.queries if q[0] == "checkouts"]:
        assert ("walker_id", "walker-w") in filters

    # storage removals during purge only ever touch this walker's own paths
    fake.storage.calls.clear()
    fake_client.get("/checkouts", headers=bearer("token-w"))
    removes = [c for c in fake.storage.calls if c[1] == "remove"]
    for _bucket, _op, path in removes:
        assert path.startswith(f"walker-w/{cid}/")

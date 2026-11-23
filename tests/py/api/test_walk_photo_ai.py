"""Photo AI at both ends of a walk: is it the same dog, and does it look fine.

Check-in keeps its optional photo; check-out now REQUIRES one (Nico, 11-23). Each
photo is read twice, and neither reading ever blocks the walk:

* CLIP (the injected embedder) compares it with the dog's enrolled vector, and at
  check-out also with the check-in photo: ``sameAsCheckIn`` says whether the dog
  handed back is the dog picked up.
* the vision chain (``get_photo_condition``) reads the dog's condition:
  ``ok`` | ``concern`` (with a short note) | ``no_dog`` | ``unavailable``.

The verdicts are stored in the walk's timeline payload (no migration), shown to the
owner as ``photoChecks`` in the walk detail, and fed to the walk report as facts.
The check-in CLIP vector is kept server side to compare at check-out; it never
leaves the API.
"""

from cantrack_api.walk_photo_ai import PhotoCondition, to_condition

from .conftest import bearer
from .fake_ai import E_LUNA, E_REX
from .test_bidding_lifecycle import T10, T11

W = "token-w"  # walker-w, the assigned walker
OWNER = "token-a"  # owner-a, the dog's owner


def seed_walk(fake, status="assigned", embedding=E_REX):
    dog = {"id": "dog-1", "owner_id": "owner-a", "name": "Firulais"}
    if embedding is not None:
        dog["embedding"] = embedding
    fake.seed("dogs", **dog)
    fake.seed(
        "walk_requests", id="walk-1", owner_id="owner-a", walker_id="walker-w", dog_id="dog-1",
        status=status, requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


def check_in(client, payload=None):
    files = None if payload is None else {"image": ("p.jpg", payload, "image/jpeg")}
    return client.post("/bidding/requests/walk-1/check-in", files=files, headers=bearer(W))


def check_out(client, payload=b"photo-rex", mime="image/jpeg", token=W):
    files = None if payload is None else {"image": ("p.jpg", payload, mime)}
    return client.post("/bidding/requests/walk-1/check-out", files=files, headers=bearer(token))


def walk_status(fake):
    return next(row for row in fake.tables["walk_requests"] if row["id"] == "walk-1")["status"]


def timeline(fake, kind):
    return next(row for row in fake.tables["walk_timeline"] if row["kind"] == kind)


# --- check-in ------------------------------------------------------------------------


def test_check_in_photo_gets_identity_and_condition(fake_client, fake, condition):
    seed_walk(fake)
    res = check_in(fake_client, b"photo-rex")
    assert res.status_code == 200
    assert res.json()["ai"] == {"match": "match", "similarity": 1.0, "condition": "ok", "note": None}
    assert condition.calls == [b"photo-rex"]


def test_check_in_concern_carries_the_note(fake_client, fake, condition):
    seed_walk(fake)
    condition.result = PhotoCondition(True, "concern", "Cojea de la pata trasera.")
    ai = check_in(fake_client, b"photo-rex").json()["ai"]
    assert (ai["condition"], ai["note"]) == ("concern", "Cojea de la pata trasera.")


def test_check_in_condition_failure_is_unavailable_and_the_walk_starts(fake_client, fake, condition):
    seed_walk(fake)
    condition.result = None
    res = check_in(fake_client, b"photo-rex")
    assert res.json()["status"] == "in_walk"
    assert res.json()["ai"]["condition"] == "unavailable"
    assert res.json()["ai"]["match"] == "match"


def test_check_in_no_dog_in_the_photo(fake_client, fake, condition):
    seed_walk(fake)
    condition.result = PhotoCondition(False, None, None)
    assert check_in(fake_client, b"photo-rex").json()["ai"]["condition"] == "no_dog"


def test_check_in_without_photo_calls_no_ai(fake_client, fake, condition, embedder):
    seed_walk(fake)
    body = check_in(fake_client).json()
    assert "ai" not in body
    assert condition.calls == [] and embedder.calls == []
    assert timeline(fake, "check_in")["payload"] == {}


def test_check_in_stores_the_verdict_and_the_vector_in_the_timeline(fake_client, fake):
    seed_walk(fake)
    check_in(fake_client, b"photo-rex")
    payload = timeline(fake, "check_in")["payload"]
    assert payload["ai"] == {"match": "match", "similarity": 1.0, "condition": "ok", "note": None}
    assert payload["embedding"] == E_REX


def test_check_in_ai_only_runs_after_a_legal_transition(fake_client, fake, condition, embedder):
    seed_walk(fake, status="in_walk")
    assert check_in(fake_client, b"photo-rex").status_code == 409
    assert condition.calls == [] and embedder.calls == []


# --- check-out -----------------------------------------------------------------------


def test_check_out_without_photo_is_400_and_the_walk_goes_on(fake_client, fake):
    seed_walk(fake, status="in_walk")
    res = check_out(fake_client, payload=None)
    assert res.status_code == 400
    assert "photo" in res.json()["detail"].lower()
    assert walk_status(fake) == "in_walk"


def test_check_out_with_unsupported_file_is_400(fake_client, fake):
    seed_walk(fake, status="in_walk")
    assert check_out(fake_client, payload=b"%PDF", mime="application/pdf").status_code == 400
    assert walk_status(fake) == "in_walk"


def test_check_out_with_oversized_photo_is_413(fake_client, fake):
    from cantrack_api.uploads import MAX_IMAGE_BYTES

    seed_walk(fake, status="in_walk")
    assert check_out(fake_client, payload=b"x" * (MAX_IMAGE_BYTES + 1)).status_code == 413
    assert walk_status(fake) == "in_walk"


def test_wrong_walker_is_403_before_any_ai(fake_client, fake, condition, embedder):
    seed_walk(fake, status="in_walk")
    assert check_out(fake_client, token=OWNER).status_code == 403
    assert condition.calls == [] and embedder.calls == []


def test_check_out_from_assigned_is_409_without_ai(fake_client, fake, condition, embedder):
    seed_walk(fake, status="assigned")
    assert check_out(fake_client).status_code == 409
    assert condition.calls == [] and embedder.calls == []


def test_same_dog_back_is_a_match_on_both_comparisons(fake_client, fake, condition):
    seed_walk(fake)
    check_in(fake_client, b"photo-rex")
    res = check_out(fake_client, b"photo-rex")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "done"
    assert body["ai"] == {
        "match": "match",
        "similarity": 1.0,
        "sameAsCheckIn": {"match": "match", "similarity": 1.0},
        "condition": "ok",
        "note": None,
    }
    assert condition.calls == [b"photo-rex", b"photo-rex"]


def test_a_different_dog_back_is_uncertain_but_the_walk_closes(fake_client, fake):
    seed_walk(fake)
    check_in(fake_client, b"photo-rex")
    res = check_out(fake_client, b"photo-luna")
    assert res.json()["status"] == "done"
    ai = res.json()["ai"]
    assert ai["match"] == "uncertain"
    assert ai["sameAsCheckIn"] == {"match": "uncertain", "similarity": 0.0}


def test_no_check_in_photo_means_same_as_check_in_is_unknown(fake_client, fake):
    seed_walk(fake)
    check_in(fake_client)
    ai = check_out(fake_client, b"photo-rex").json()["ai"]
    assert ai["sameAsCheckIn"] == {"match": "unknown", "similarity": None}
    assert ai["match"] == "match"


def test_dog_without_enrolled_vector_still_compares_with_check_in(fake_client, fake):
    seed_walk(fake, embedding=None)
    check_in(fake_client, b"photo-rex")
    ai = check_out(fake_client, b"photo-rex").json()["ai"]
    assert (ai["match"], ai["similarity"]) == ("unknown", None)
    assert ai["sameAsCheckIn"]["match"] == "match"


def test_embedder_failure_never_blocks_the_check_out(fake_client, fake, embedder):
    seed_walk(fake, status="in_walk")
    embedder.explode = True
    res = check_out(fake_client, b"photo-rex")
    assert res.status_code == 200
    assert res.json()["status"] == "done"
    ai = res.json()["ai"]
    assert ai["match"] == "unavailable"
    assert ai["sameAsCheckIn"]["match"] == "unavailable"
    assert ai["condition"] == "ok"


def test_check_out_stores_the_verdict_without_a_vector(fake_client, fake):
    seed_walk(fake)
    check_in(fake_client, b"photo-rex")
    check_out(fake_client, b"photo-rex")
    payload = timeline(fake, "check_out")["payload"]
    assert payload["ai"]["sameAsCheckIn"]["match"] == "match"
    assert "embedding" not in payload


# --- what the owner sees -------------------------------------------------------------


def test_owner_detail_shows_photo_checks_and_never_the_vectors(fake_client, fake, condition):
    seed_walk(fake)
    check_in(fake_client, b"photo-rex")
    condition.result = PhotoCondition(True, "concern", "Llega mojado y con barro.")
    check_out(fake_client, b"photo-rex")
    res = fake_client.get("/bidding/requests/walk-1", headers=bearer(OWNER))
    assert res.status_code == 200
    body = res.json()
    assert body["photoChecks"]["checkIn"]["condition"] == "ok"
    assert body["photoChecks"]["checkOut"]["condition"] == "concern"
    assert body["photoChecks"]["checkOut"]["note"] == "Llega mojado y con barro."
    assert body["photoChecks"]["checkOut"]["sameAsCheckIn"]["match"] == "match"
    assert "embedding" not in res.text


def test_owner_detail_without_photos_has_null_checks(fake_client, fake):
    seed_walk(fake)
    check_in(fake_client)
    body = fake_client.get("/bidding/requests/walk-1", headers=bearer(OWNER)).json()
    assert body["photoChecks"] == {"checkIn": None, "checkOut": None}


def test_the_report_is_told_what_the_return_photo_showed(fake_client, fake, condition, text_model):
    seed_walk(fake)
    check_in(fake_client, b"photo-rex")
    condition.result = PhotoCondition(True, "concern", "Llega mojado y con barro.")
    check_out(fake_client, b"photo-rex")
    _, prompt = text_model.calls[-1]
    assert "Llega mojado y con barro." in prompt
    assert "same dog" in prompt.lower()


# --- sanitizing the model's answer ---------------------------------------------------


def test_to_condition_keeps_only_known_values():
    assert to_condition({"dog_visible": True, "condition": "ok", "note": "  "}) == PhotoCondition(True, "ok", None)
    assert to_condition({"dog_visible": True, "condition": "great"}) is None
    assert to_condition({"dog_visible": False}) == PhotoCondition(False, None, None)
    assert to_condition({"condition": "ok"}) is None


def test_to_condition_caps_the_note():
    long = "a" * 500
    reading = to_condition({"dog_visible": True, "condition": "concern", "note": long})
    assert reading is not None and len(reading.note) == 160

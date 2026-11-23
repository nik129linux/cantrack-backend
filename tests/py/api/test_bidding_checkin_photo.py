"""Bidding check-in with an optional dog photo (the CLIP "is this the right dog" step).

The walker may attach one photo (multipart field ``image``) to
``POST /bidding/requests/{id}/check-in``. The server embeds it and compares it with the
request's dog. The walk ALWAYS starts: the AI is advice, never a gate. The answer gains an
``ai`` object ``{match, similarity}`` where ``match`` is one of:

* ``match``       similarity >= DOG_MATCH_SIMILARITY (0.88, from cantrack-ai/evals/clip_eval.py on raw photos)
* ``uncertain``   similarity below it: the walker must confirm the dog by eye
* ``unknown``     the dog has no enrolled embedding yet
* ``unavailable`` the embedder failed or the photo was unreadable

Without a photo the response is exactly what it was before (no ``ai`` key).
"""

from .conftest import bearer
from .fake_ai import E_REX
from .test_bidding_lifecycle import T10, T11

W = "token-w"  # walker-w


def seed_walk(fake, embedding=E_REX):
    dog = {"id": "dog-1", "owner_id": "owner-a", "name": "Firulais"}
    if embedding is not None:
        dog["embedding"] = embedding
    fake.seed("dogs", **dog)
    fake.seed(
        "walk_requests", id="walk-1", owner_id="owner-a", walker_id="walker-w", dog_id="dog-1",
        status="assigned", requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


def check_in(client, payload=None):
    files = None if payload is None else {"image": ("p.jpg", payload, "image/jpeg")}
    return client.post("/bidding/requests/walk-1/check-in", files=files, headers=bearer(W))


def test_without_photo_the_answer_is_unchanged(fake_client, fake):
    seed_walk(fake)
    body = check_in(fake_client).json()
    assert body["status"] == "in_walk"
    assert "ai" not in body


def test_matching_photo_is_a_match(fake_client, fake, embedder):
    seed_walk(fake)
    res = check_in(fake_client, b"photo-rex")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "in_walk"
    assert body["ai"]["match"] == "match"
    assert body["ai"]["similarity"] == 1.0
    assert embedder.calls == [b"photo-rex"]


def test_other_dog_photo_is_uncertain_but_the_walk_still_starts(fake_client, fake):
    seed_walk(fake)
    res = check_in(fake_client, b"photo-between")  # cosine 0.71 with the stored Rex vector
    assert res.status_code == 200
    assert res.json()["status"] == "in_walk"
    assert res.json()["ai"]["match"] == "uncertain"
    assert 0.70 < res.json()["ai"]["similarity"] < 0.72


def test_the_threshold_is_exactly_the_evaluated_one(fake_client, fake):
    from cantrack_api.routers import bidding

    assert bidding.DOG_MATCH_SIMILARITY == 0.88


def test_dog_without_embedding_is_unknown(fake_client, fake):
    seed_walk(fake, embedding=None)
    res = check_in(fake_client, b"photo-rex")
    assert res.json()["status"] == "in_walk"
    assert {k: res.json()["ai"][k] for k in ("match", "similarity")} == {"match": "unknown", "similarity": None}


def test_embedder_failure_never_blocks_the_walk(fake_client, fake, embedder):
    seed_walk(fake)
    embedder.explode = True
    res = check_in(fake_client, b"photo-rex")
    assert res.status_code == 200
    assert res.json()["status"] == "in_walk"
    assert {k: res.json()["ai"][k] for k in ("match", "similarity")} == {"match": "unavailable", "similarity": None}


def test_unreadable_photo_never_blocks_the_walk(fake_client, fake):
    seed_walk(fake)
    res = check_in(fake_client, b"garbage")  # FakeEmbedder raises ImageError for this
    assert res.status_code == 200
    assert res.json()["status"] == "in_walk"
    assert {k: res.json()["ai"][k] for k in ("match", "similarity")} == {"match": "unavailable", "similarity": None}


def test_only_the_assigned_walker_can_check_in_with_a_photo(fake_client, fake, embedder):
    seed_walk(fake)
    res = fake_client.post(
        "/bidding/requests/walk-1/check-in",
        files={"image": ("p.jpg", b"photo-rex", "image/jpeg")},
        headers=bearer("token-a"),  # the owner, not the walker
    )
    assert res.status_code == 403
    assert embedder.calls == []  # the photo is not even embedded for the wrong user


def test_a_second_check_in_is_still_a_conflict(fake_client, fake):
    seed_walk(fake)
    assert check_in(fake_client, b"photo-rex").status_code == 200
    assert check_in(fake_client, b"photo-rex").status_code == 409


def test_oversized_photo_is_not_embedded_and_never_blocks_the_walk(fake_client, fake, embedder):
    from cantrack_api.uploads import MAX_IMAGE_BYTES

    seed_walk(fake)
    res = check_in(fake_client, b"x" * (MAX_IMAGE_BYTES + 1))
    assert res.status_code == 200
    assert res.json()["status"] == "in_walk"
    assert {k: res.json()["ai"][k] for k in ("match", "similarity")} == {"match": "unavailable", "similarity": None}
    assert embedder.calls == []  # never handed to the model

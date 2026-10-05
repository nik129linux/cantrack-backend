"""FR-05, FR-10, FR-11: enroll a dog from a photo, check in by photo, confirm manually, undo.

The photo is embedded and analysed on the SERVER (layers: web -> API -> DB + AI). The embedder and
the vision model are injected fakes; Supabase is the in-memory fake.
"""

import json
import uuid

import pytest
from postgrest.exceptions import APIError

from cantrack_ai.vision import PhotoCheck

from .conftest import bearer
from .fake_ai import E_LUNA, E_REX

W = "token-w"  # walker-w
OWNER = "token-a"  # owner-a


def photo(payload: bytes = b"photo-near-rex", name="p.jpg", ctype="image/jpeg"):
    return {"image": (name, payload, ctype)}


@pytest.fixture
def rex(fake):
    return fake.seed("dogs", owner_id="owner-a", name="Rex", embedding=E_REX)


@pytest.fixture
def luna(fake):
    return fake.seed("dogs", owner_id="owner-a", name="Luna", embedding=E_LUNA)


@pytest.fixture
def route(fake, rex, luna):
    return fake.seed(
        "routes",
        walker_id="walker-w",
        stops=[
            {"dog_id": rex["id"], "pickup_time": "2026-10-05T08:00:00Z"},
            {"dog_id": luna["id"], "pickup_time": "2026-10-05T09:00:00Z"},
        ],
    )


def checkin(client, route_id, payload=b"photo-near-rex", token=W, **kw):
    return client.post(f"/routes/{route_id}/checkin", files=photo(payload, **kw), headers=bearer(token))


class TestEnrollDogPhoto:
    def test_stores_the_server_side_embedding(self, fake_client, fake, rex, embedder):
        res = fake_client.post(
            f"/dogs/{rex['id']}/photos", files=photo(b"photo-luna"), headers=bearer(OWNER)
        )
        assert res.status_code == 200
        assert res.json()["id"] == rex["id"]
        assert fake.tables["dogs"][0]["embedding"] == E_LUNA
        assert embedder.calls == [b"photo-luna"]

    def test_response_does_not_leak_the_raw_embedding(self, fake_client, rex):
        res = fake_client.post(
            f"/dogs/{rex['id']}/photos", files=photo(b"photo-luna"), headers=bearer(OWNER)
        )
        assert "embedding" not in res.json()

    def test_another_owners_dog_is_404_and_no_ai_work_is_done(
        self, fake_client, fake, rex, embedder
    ):
        res = fake_client.post(
            f"/dogs/{rex['id']}/photos", files=photo(b"photo-luna"), headers=bearer("token-b")
        )
        assert res.status_code == 404
        assert embedder.calls == []
        assert fake.tables["dogs"][0]["embedding"] == E_REX

    def test_unknown_dog_is_404(self, fake_client):
        res = fake_client.post(
            f"/dogs/{uuid.uuid4()}/photos", files=photo(b"photo-luna"), headers=bearer(OWNER)
        )
        assert res.status_code == 404

    def test_non_uuid_dog_id_is_400(self, fake_client):
        res = fake_client.post("/dogs/x/photos", files=photo(b"photo-luna"), headers=bearer(OWNER))
        assert res.status_code == 400

    def test_no_token_is_401(self, fake_client, rex):
        assert fake_client.post(f"/dogs/{rex['id']}/photos", files=photo()).status_code == 401

    def test_unreadable_image_is_400_and_stores_nothing(self, fake_client, fake, rex):
        res = fake_client.post(
            f"/dogs/{rex['id']}/photos", files=photo(b"garbage"), headers=bearer(OWNER)
        )
        assert res.status_code == 400
        assert res.json() == {"detail": "Unreadable image."}
        assert fake.tables["dogs"][0]["embedding"] == E_REX

    @pytest.mark.parametrize("ctype", ["text/plain", "application/pdf", "image/gif"])
    def test_unsupported_content_type_is_400(self, fake_client, rex, ctype):
        res = fake_client.post(
            f"/dogs/{rex['id']}/photos",
            files=photo(b"photo-luna", ctype=ctype),
            headers=bearer(OWNER),
        )
        assert res.status_code == 400
        assert res.json() == {"detail": "Unsupported image type."}

    @pytest.mark.parametrize("ctype", ["image/jpeg", "image/png", "image/webp"])
    def test_supported_content_types(self, fake_client, rex, ctype):
        res = fake_client.post(
            f"/dogs/{rex['id']}/photos",
            files=photo(b"photo-luna", ctype=ctype),
            headers=bearer(OWNER),
        )
        assert res.status_code == 200

    def test_image_over_5_mb_is_413(self, fake_client, rex, embedder):
        big = b"x" * (5 * 1024 * 1024 + 1)
        res = fake_client.post(
            f"/dogs/{rex['id']}/photos", files=photo(big), headers=bearer(OWNER)
        )
        assert res.status_code == 413
        assert embedder.calls == []

    def test_missing_file_is_400(self, fake_client, rex):
        res = fake_client.post(f"/dogs/{rex['id']}/photos", headers=bearer(OWNER))
        assert res.status_code == 400

    def test_model_failure_is_503(self, fake_client, rex, embedder):
        embedder.explode = True
        res = fake_client.post(
            f"/dogs/{rex['id']}/photos", files=photo(b"photo-luna"), headers=bearer(OWNER)
        )
        assert res.status_code == 503
        assert res.json() == {"detail": "Image model unavailable."}


class TestEnrollSeveralPhotos:
    """FR-05: a dog is enrolled with several reference photos; the stored vector is their mean."""

    @staticmethod
    def many(*payloads):
        return [("image", (f"p{i}.jpg", p, "image/jpeg")) for i, p in enumerate(payloads)]

    def test_embedding_is_the_elementwise_mean_of_all_photos(self, fake_client, fake, rex, embedder):
        res = fake_client.post(
            f"/dogs/{rex['id']}/photos",
            files=self.many(b"photo-rex", b"photo-luna"),
            headers=bearer(OWNER),
        )
        assert res.status_code == 200
        assert fake.tables["dogs"][0]["embedding"] == [0.5, 0.5, 0.0, 0.0]
        assert embedder.calls == [b"photo-rex", b"photo-luna"]

    def test_three_photos(self, fake_client, fake, rex):
        res = fake_client.post(
            f"/dogs/{rex['id']}/photos",
            files=self.many(b"photo-rex", b"photo-luna", b"photo-between"),
            headers=bearer(OWNER),
        )
        assert res.status_code == 200
        stored = fake.tables["dogs"][0]["embedding"]
        assert stored == pytest.approx([2 / 3, 2 / 3, 0.0, 0.0])

    def test_more_than_five_photos_is_400_and_no_ai_work(self, fake_client, fake, rex, embedder):
        res = fake_client.post(
            f"/dogs/{rex['id']}/photos",
            files=self.many(*([b"photo-rex"] * 6)),
            headers=bearer(OWNER),
        )
        assert res.status_code == 400
        assert res.json() == {"detail": "Between 1 and 5 photos are required."}
        assert embedder.calls == []
        assert fake.tables["dogs"][0]["embedding"] == E_REX

    def test_one_unreadable_photo_rejects_the_whole_enrollment(self, fake_client, fake, rex):
        res = fake_client.post(
            f"/dogs/{rex['id']}/photos",
            files=self.many(b"photo-luna", b"garbage", b"photo-luna"),
            headers=bearer(OWNER),
        )
        assert res.status_code == 400
        assert res.json() == {"detail": "Unreadable image."}
        assert fake.tables["dogs"][0]["embedding"] == E_REX

    def test_one_oversized_photo_rejects_the_whole_enrollment(self, fake_client, fake, rex):
        big = b"x" * (5 * 1024 * 1024 + 1)
        res = fake_client.post(
            f"/dogs/{rex['id']}/photos",
            files=self.many(b"photo-luna", big),
            headers=bearer(OWNER),
        )
        assert res.status_code == 413
        assert fake.tables["dogs"][0]["embedding"] == E_REX

    def test_one_wrong_type_rejects_the_whole_enrollment(self, fake_client, fake, rex):
        files = self.many(b"photo-luna") + [("image", ("n.txt", b"photo-rex", "text/plain"))]
        res = fake_client.post(f"/dogs/{rex['id']}/photos", files=files, headers=bearer(OWNER))
        assert res.status_code == 400
        assert res.json() == {"detail": "Unsupported image type."}
        assert fake.tables["dogs"][0]["embedding"] == E_REX

    def test_embeddings_of_different_length_are_a_503_not_a_crash(self, fake_client, fake, rex, embedder):
        embedder.vectors[b"short"] = [1.0, 0.0]
        res = fake_client.post(
            f"/dogs/{rex['id']}/photos",
            files=self.many(b"photo-rex", b"short"),
            headers=bearer(OWNER),
        )
        assert res.status_code == 503
        assert fake.tables["dogs"][0]["embedding"] == E_REX


class TestCheckIn:
    def test_confident_match_auto_confirms_and_persists(self, fake_client, fake, route, rex):
        res = checkin(fake_client, route["id"], b"photo-near-rex")
        assert res.status_code == 201
        body = res.json()
        assert body["autoConfirmed"] is True
        assert body["dogId"] == rex["id"]
        assert body["dogName"] == "Rex"
        assert body["similarity"] > 0.95
        assert body["checkinId"]
        assert body["ai"] == {"dogVisible": True, "note": "Calm and clean."}
        rows = fake.tables["checkins"]
        assert len(rows) == 1
        assert (rows[0]["route_id"], rows[0]["dog_id"]) == (route["id"], rex["id"])
        assert body["checkinId"] == rows[0]["id"]

    def test_unsure_match_returns_every_route_dog_as_candidates_and_persists_nothing(
        self, fake_client, fake, route, rex, luna
    ):
        res = checkin(fake_client, route["id"], b"photo-between")
        assert res.status_code == 200
        body = res.json()
        assert body["autoConfirmed"] is False
        assert {c["dogId"] for c in body["candidates"]} == {rex["id"], luna["id"]}
        assert {c["dogName"] for c in body["candidates"]} == {"Rex", "Luna"}
        sims = [c["similarity"] for c in body["candidates"]]
        assert sims == sorted(sims, reverse=True)
        assert all(0.6 < s < 0.8 for s in sims)
        assert fake.tables.get("checkins", []) == []

    def test_a_dog_without_an_embedding_is_still_a_candidate_with_zero_similarity(
        self, fake_client, fake, route, rex, luna
    ):
        fake.tables["dogs"][1]["embedding"] = None  # luna never enrolled
        body = checkin(fake_client, route["id"], b"photo-between").json()
        by_id = {c["dogId"]: c["similarity"] for c in body["candidates"]}
        assert by_id[luna["id"]] == 0.0
        assert by_id[rex["id"]] > 0.6

    def test_pgvector_string_embeddings_are_understood(self, fake_client, fake, route, rex):
        fake.tables["dogs"][0]["embedding"] = json.dumps(E_REX)  # how PostgREST returns vectors
        body = checkin(fake_client, route["id"], b"photo-near-rex").json()
        assert body["autoConfirmed"] is True
        assert body["dogId"] == rex["id"]

    def test_threshold_is_0_8(self, fake_client, fake, route, embedder, rex):
        from math import cos, radians, sin

        for degrees, expect_auto in ((36, True), (37, False)):  # cos 36.87deg == 0.8
            fake.tables["checkins"] = []
            embedder.vectors[b"angle"] = [cos(radians(degrees)), sin(radians(degrees)), 0, 0]
            body = checkin(fake_client, route["id"], b"angle").json()
            assert body["autoConfirmed"] is expect_auto

    def test_route_with_no_stops_has_no_candidates(self, fake_client, fake):
        empty = fake.seed("routes", walker_id="walker-w", stops=[])
        res = checkin(fake_client, empty["id"], b"photo-rex")
        assert res.status_code == 200
        assert res.json()["autoConfirmed"] is False
        assert res.json()["candidates"] == []

    def test_someone_elses_route_is_404_before_any_ai_work(
        self, fake_client, fake, route, embedder, vision
    ):
        fake.tables["routes"][0]["walker_id"] = "someone-else"
        res = checkin(fake_client, route["id"])
        assert res.status_code == 404
        assert embedder.calls == [] and vision.calls == []
        assert fake.tables.get("checkins", []) == []

    def test_no_token_is_401(self, fake_client, route):
        res = fake_client.post(f"/routes/{route['id']}/checkin", files=photo())
        assert res.status_code == 401

    def test_non_uuid_route_id_is_400(self, fake_client):
        assert checkin(fake_client, "nope").status_code == 400

    def test_unreadable_image_is_400(self, fake_client, fake, route):
        res = checkin(fake_client, route["id"], b"garbage")
        assert res.status_code == 400
        assert res.json() == {"detail": "Unreadable image."}
        assert fake.tables.get("checkins", []) == []

    def test_unsupported_type_is_400_and_missing_file_is_400(self, fake_client, route):
        assert checkin(fake_client, route["id"], ctype="text/plain").status_code == 400
        res = fake_client.post(f"/routes/{route['id']}/checkin", headers=bearer(W))
        assert res.status_code == 400

    def test_model_failure_is_503(self, fake_client, route, embedder):
        embedder.explode = True
        assert checkin(fake_client, route["id"]).status_code == 503

    def test_database_error_is_500(self, fake_client, fake, route):
        fake.fail_with = APIError({"message": "down", "code": "X", "hint": None, "details": None})
        assert checkin(fake_client, route["id"]).status_code == 500


class TestVisionGuard:
    def test_photo_without_a_dog_is_rejected_and_nothing_is_saved(
        self, fake_client, fake, route, vision
    ):
        vision.result = PhotoCheck(dog_visible=False, note="Only a tree.")
        res = checkin(fake_client, route["id"], b"photo-near-rex")
        assert res.status_code == 400
        assert res.json() == {"detail": "No dog detected in the photo."}
        assert fake.tables.get("checkins", []) == []

    def test_unavailable_vision_does_not_block_the_check_in(self, fake_client, route, vision):
        vision.result = PhotoCheck(dog_visible=None, note=None)
        res = checkin(fake_client, route["id"], b"photo-near-rex")
        assert res.status_code == 201
        assert res.json()["ai"] == {"dogVisible": None, "note": None}

    def test_the_vision_model_receives_the_photo_bytes(self, fake_client, route, vision):
        checkin(fake_client, route["id"], b"photo-near-rex")
        assert vision.calls == [b"photo-near-rex"]

    def test_ai_note_is_reported_even_when_the_match_is_unsure(self, fake_client, route, vision):
        vision.result = PhotoCheck(dog_visible=True, note="Muddy paws.")
        body = checkin(fake_client, route["id"], b"photo-between").json()
        assert body["ai"] == {"dogVisible": True, "note": "Muddy paws."}


class TestManualConfirm:
    def confirm(self, client, route_id, dog_id, token=W):
        return client.post(
            f"/routes/{route_id}/checkin/confirm", json={"dogId": dog_id}, headers=bearer(token)
        )

    def test_walker_picks_a_dog_on_the_route(self, fake_client, fake, route, luna):
        res = self.confirm(fake_client, route["id"], luna["id"])
        assert res.status_code == 201
        assert res.json()["dogId"] == luna["id"]
        assert res.json()["dogName"] == "Luna"
        assert res.json()["checkinId"] == fake.tables["checkins"][0]["id"]
        assert fake.tables["checkins"][0]["dog_id"] == luna["id"]

    def test_a_dog_that_is_not_on_the_route_is_400(self, fake_client, fake, route):
        stranger = fake.seed("dogs", owner_id="owner-b", name="Stranger", embedding=None)
        res = self.confirm(fake_client, route["id"], stranger["id"])
        assert res.status_code == 400
        assert res.json() == {"detail": "That dog is not on this route."}
        assert fake.tables.get("checkins", []) == []

    def test_someone_elses_route_is_404(self, fake_client, fake, route, rex):
        fake.tables["routes"][0]["walker_id"] = "someone-else"
        assert self.confirm(fake_client, route["id"], rex["id"]).status_code == 404

    @pytest.mark.parametrize("body", [{}, {"dogId": ""}, {"dogId": 5}])
    def test_invalid_body_is_400(self, fake_client, route, body):
        res = fake_client.post(
            f"/routes/{route['id']}/checkin/confirm", json=body, headers=bearer(W)
        )
        assert res.status_code == 400

    def test_no_token_is_401(self, fake_client, route):
        res = fake_client.post(f"/routes/{route['id']}/checkin/confirm", json={"dogId": "x"})
        assert res.status_code == 401


class TestUndo:
    def undo(self, client, route_id, token=W):
        return client.delete(f"/routes/{route_id}/checkin/undo", headers=bearer(token))

    def seed_checkins(self, fake, route, dogs):
        ids = []
        for i, dog in enumerate(dogs):
            row = fake.seed(
                "checkins",
                route_id=route["id"],
                dog_id=dog["id"],
                created_at=f"2026-10-05T0{8 + i}:00:00+00:00",
            )
            ids.append(row["id"])
        return ids

    def test_undoes_the_most_recent_check_in_lifo(self, fake_client, fake, route, rex, luna):
        first, second = self.seed_checkins(fake, route, [rex, luna])
        res = self.undo(fake_client, route["id"])
        assert res.status_code == 200
        assert res.json() == {"message": "Check-in undone."}
        assert [c["id"] for c in fake.tables["checkins"]] == [first]
        assert self.undo(fake_client, route["id"]).status_code == 200
        assert fake.tables["checkins"] == []
        assert self.undo(fake_client, route["id"]).status_code == 404

    def test_undo_after_a_real_check_in_removes_it(self, fake_client, fake, route):
        assert checkin(fake_client, route["id"], b"photo-near-rex").status_code == 201
        assert self.undo(fake_client, route["id"]).status_code == 200
        assert fake.tables["checkins"] == []

    def test_no_check_ins_is_404(self, fake_client, route):
        res = self.undo(fake_client, route["id"])
        assert res.status_code == 404
        assert res.json() == {"detail": "No check-in found."}

    def test_only_touches_the_given_route(self, fake_client, fake, route, rex):
        other = fake.seed("routes", walker_id="walker-w", stops=[])
        fake.seed("checkins", route_id=other["id"], dog_id=rex["id"], created_at="2026-10-05T12:00:00+00:00")
        assert self.undo(fake_client, route["id"]).status_code == 404
        assert len(fake.tables["checkins"]) == 1

    def test_someone_elses_route_is_404_and_nothing_is_deleted(self, fake_client, fake, route, rex):
        self.seed_checkins(fake, route, [rex])
        fake.tables["routes"][0]["walker_id"] = "someone-else"
        assert self.undo(fake_client, route["id"]).status_code == 404
        assert len(fake.tables["checkins"]) == 1

    def test_no_token_is_401(self, fake_client, route):
        assert fake_client.delete(f"/routes/{route['id']}/checkin/undo").status_code == 401

    def test_delete_is_scoped_by_id_and_route(self, fake_client, fake, route, rex):
        self.seed_checkins(fake, route, [rex])
        self.undo(fake_client, route["id"])
        deletes = [q for q in fake.queries if q[0] == "checkins" and q[1] == "delete"]
        assert len(deletes) == 1
        cols = {c for c, _ in deletes[0][2]}
        assert {"id", "route_id"} <= cols

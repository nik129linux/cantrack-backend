"""Task 6: one deny-path per endpoint group — the highest-value hardening tests.

Many happy-path-adjacent denies already exist in test_bidding_*.py; this module
adds ONLY the missing ones and references the existing coverage in comments
so we don't duplicate. The real risk is a broken caller-scoped query letting
one user read another's rows (service-role bypasses RLS), so wrong-owner →
403/404 and bad-token → 401 are the blocking assertions.
"""

import io

import pytest
from fastapi import UploadFile

from cantrack_api.uploads import MAX_IMAGE_BYTES, MAX_REFERENCE_PHOTOS

from .conftest import FIXED_NOW, bearer, make_user

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"


def seed_open(fake, request_id="walk-1", owner_id="owner-a"):
    fake.seed("dogs", id="dog-1", owner_id=owner_id, name="Firulais")
    return fake.seed(
        "walk_requests", id=request_id, owner_id=owner_id, walker_id=None, dog_id="dog-1",
        status="open", requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


def seed_assigned(fake, request_id="walk-1", walker_id="walker-w"):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    return fake.seed(
        "walk_requests", id=request_id, owner_id="owner-a", walker_id=walker_id, dog_id="dog-1",
        status="assigned", requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000,
        created_at="2026-10-01T00:00:00+00:00",
    )


class TestAuthNegatives:
    def test_protected_route_without_token_is_401(self, fake_client):
        # /_whoami was removed in PR0 (the professor's "/me"); any guarded route works.
        res = fake_client.get("/dogs")
        assert res.status_code == 401
        assert res.json()["detail"] == "A bearer token is required."

    def test_protected_route_bad_token_is_401(self, fake_client):
        res = fake_client.get("/dogs", headers=bearer("bad-token"))
        assert res.status_code == 401

    def test_login_headers_present_on_401(self, fake_client):
        res = fake_client.post("/auth/login", json={"email": "no@no.com", "password": "x"})
        assert res.status_code in (400, 401)
        assert res.headers["X-Content-Type-Options"] == "nosniff"


class TestBiddingRequestNegatives:
    def test_walker_cannot_post_bidding_request(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        res = fake_client.post(
            "/bidding/requests",
            json={"dogId": "dog-1", "windowStart": T10, "durationHours": 1, "priceCop": 30000, "pickupLat": 6.2, "pickupLng": -75.5},
            headers=bearer("token-w"),
        )
        assert res.status_code == 400

    def test_unknown_dog_is_404(self, fake_client):
        res = fake_client.post(
            "/bidding/requests",
            json={"dogId": "nope", "windowStart": T10, "durationHours": 1, "priceCop": 30000, "pickupLat": 6.2, "pickupLng": -75.5},
            headers=bearer("token-a"),
        )
        assert res.status_code == 404

    def test_naive_window_is_400(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        res = fake_client.post(
            "/bidding/requests",
            json={"dogId": "dog-1", "windowStart": "2026-10-05T10:00:00", "durationHours": 1, "priceCop": 30000, "pickupLat": 6.2, "pickupLng": -75.5},
            headers=bearer("token-a"),
        )
        assert res.status_code == 400

    def test_wrong_owner_offers_is_403(self, fake_client, fake):
        seed_open(fake)
        res = fake_client.get("/bidding/requests/walk-1/offers", headers=bearer("token-b"))
        assert res.status_code == 403

    def test_pick_unknown_offer_is_404(self, fake_client, fake):
        seed_open(fake)
        fake.seed("walker_positions", walker_id="walker-w", lat=6.201, lng=-75.5, updated_at="2026-10-01T00:00:00+00:00")
        res = fake_client.post("/bidding/requests/walk-1/pick", json={"offerId": "nope"}, headers=bearer("token-a"))
        assert res.status_code == 404

    def test_auto_pick_before_deadline_is_400(self, fake_client, fake):
        seed_open(fake)
        res = fake_client.post("/bidding/requests/walk-1/auto-pick", headers=bearer("token-a"))
        assert res.status_code == 400

    def test_walker_cancel_empty_bucket_is_403(self, fake_client, fake):
        seed_assigned(fake)
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=0, period_start="2026-10-01T00:00:00+00:00")
        res = fake_client.post("/bidding/requests/walk-1/walker-cancel", headers=bearer("token-w"))
        assert res.status_code == 403

    def test_no_show_too_early_is_400(self, fake_client, fake):
        seed_assigned(fake)
        res = fake_client.post("/bidding/requests/walk-1/no-show", headers=bearer("token-a"))
        assert res.status_code == 400

    def test_check_out_from_assigned_is_409(self, fake_client, fake):
        seed_assigned(fake)
        res = fake_client.post("/bidding/requests/walk-1/check-out", headers=bearer("token-w"))
        assert res.status_code == 409

    def test_rating_unfinished_is_409(self, fake_client, fake):
        seed_assigned(fake)
        res = fake_client.post("/bidding/requests/walk-1/ratings", json={"rating": 5}, headers=bearer("token-a"))
        assert res.status_code == 409

    def test_replay_foreign_owner_is_403(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        fake.seed("walk_requests", id="walk-1", owner_id="owner-a", walker_id="walker-w", dog_id="dog-1",
                  status="done", requested_time=T10, window_end=T11,
                  deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
                  pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000, created_at="2026-10-01T00:00:00+00:00")
        res = fake_client.get("/bidding/requests/walk-1/replay", headers=bearer("token-b"))
        assert res.status_code == 403

    def test_bidding_headers_on_403(self, fake_client, fake):
        seed_open(fake)
        res = fake_client.get("/bidding/requests/walk-1/offers", headers=bearer("token-b"))
        assert res.status_code == 403
        assert res.headers["X-Frame-Options"] == "DENY"


# The /dogs/{dog_id} routes type dog_id as uuid.UUID, so a non-UUID path 400s before
# the body runs. These tests use real UUIDs so the upload/ownership logic is reached.
DOG_ID = "11111111-1111-1111-1111-111111111111"


class TestUploadLimits:
    def test_oversized_image_is_413(self, fake_client, fake):
        # Starlette's UploadFile.content_type is read-only, set via the part headers.
        from starlette.datastructures import Headers

        from cantrack_api.uploads import read_image
        big = UploadFile(
            filename="big.jpg",
            file=io.BytesIO(b"x" * (MAX_IMAGE_BYTES + 1)),
            headers=Headers({"content-type": "image/jpeg"}),
        )
        with pytest.raises(Exception) as exc:
            read_image(big)
        assert getattr(exc.value, "status_code", None) == 413

    def test_unsupported_type_is_400(self, fake_client, fake):
        from starlette.datastructures import Headers

        from cantrack_api.uploads import read_image
        bad = UploadFile(
            filename="bad.pdf",
            file=io.BytesIO(b"%PDF"),
            headers=Headers({"content-type": "application/pdf"}),
        )
        with pytest.raises(Exception) as exc:
            read_image(bad)
        assert getattr(exc.value, "status_code", None) == 400

    def test_too_many_photos_is_400(self, fake_client, fake):
        fake.seed("dogs", id=DOG_ID, owner_id="owner-a", name="Firulais")
        files = [("image", ("a.jpg", b"\xff\xd8\xff", "image/jpeg"))] * (MAX_REFERENCE_PHOTOS + 1)
        res = fake_client.post(f"/dogs/{DOG_ID}/photos", files=files, headers=bearer("token-a"))
        assert res.status_code == 400

    def test_foreign_dog_photo_is_404(self, fake_client, fake):
        # The dog belongs to owner-b; owner-a (token-a) must not learn it exists:
        # the caller-scoped query returns "Dog not found." (404), the same as a missing dog.
        fake.seed("dogs", id=DOG_ID, owner_id="owner-b", name="Firulais")
        files = [("image", ("a.jpg", b"\xff\xd8\xff", "image/jpeg"))]
        res = fake_client.post(f"/dogs/{DOG_ID}/photos", files=files, headers=bearer("token-a"))
        assert res.status_code == 404

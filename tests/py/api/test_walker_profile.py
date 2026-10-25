"""W5 Perfil (walker): profile, avatar and data-protection consent.

Same contract as the owner's Perfil (test_owner_profile.py), on the
``walker_profiles`` table. ``PUT`` replaces the editable fields; display name
and price are required because the table needs them to exist at all.
"""

import io

import pytest
from PIL import Image

from .conftest import bearer, make_user

BUCKET = "avatars"
UNREADABLE = {"detail": "Unreadable image."}
TOO_BIG = {"detail": "The avatar must be 2 MB or less."}
FULL = {"displayName": "Mateo", "bio": "Paseo perros grandes", "serviceArea": "Centro",
        "pricePerWalk": 18000, "phone": "+57 300 999 8888"}


def png_bytes(size=(900, 600)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (90, 160, 80)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def data(fake):
    fake.add_user("token-v", make_user("walker-v", "walker-v@example.com", "walker"))
    return fake


def get_profile(client, token="token-w", walker="walker-w"):
    return client.get(f"/walkers/{walker}/profile", headers=bearer(token))


def put_profile(client, body, token="token-w", walker="walker-w"):
    return client.put(f"/walkers/{walker}/profile", json=body, headers=bearer(token))


class TestProfile:
    def test_a_new_walker_has_an_empty_profile(self, fake_client, data):
        assert get_profile(fake_client).json() == {
            "walkerId": "walker-w", "email": "walker-w@example.com", "displayName": None, "bio": None,
            "serviceArea": None, "pricePerWalk": None, "phone": None, "avatarUrl": None, "consent": None,
            "stats": {"walksDone": 0, "rating": {"average": None, "count": 0}},
        }

    def test_save_and_read_back(self, fake_client, data):
        res = put_profile(fake_client, FULL)
        assert res.status_code == 200
        profile = get_profile(fake_client).json()
        assert {key: profile[key] for key in FULL} == FULL

    def test_the_legacy_catalog_sees_the_same_row(self, fake_client, data):
        put_profile(fake_client, FULL)
        catalog = fake_client.get("/walker-profiles", headers=bearer("token-a")).json()
        assert [w["displayName"] for w in catalog] == ["Mateo"]

    def test_saving_twice_keeps_one_row_and_stamps_the_time(self, fake_client, data):
        put_profile(fake_client, FULL)
        put_profile(fake_client, {**FULL, "displayName": "Mateo R."})
        rows = [r for r in data.tables["walker_profiles"] if r["walker_id"] == "walker-w"]
        assert len(rows) == 1 and rows[0]["display_name"] == "Mateo R."
        assert rows[0]["updated_at"] == "2026-10-01T00:00:00+00:00"

    def test_saving_keeps_the_avatar_and_consent(self, fake_client, data):
        put_profile(fake_client, FULL)
        fake_client.post("/walkers/walker-w/consent", json={"version": "2026-10"}, headers=bearer("token-w"))
        put_profile(fake_client, {**FULL, "bio": None})
        assert get_profile(fake_client).json()["consent"] == {"version": "2026-10", "at": "2026-10-01T00:00:00+00:00"}

    @pytest.mark.parametrize("body", [
        {**FULL, "displayName": ""}, {**FULL, "displayName": "x" * 81},
        {key: value for key, value in FULL.items() if key != "displayName"},
        {key: value for key, value in FULL.items() if key != "pricePerWalk"},
        {**FULL, "pricePerWalk": -1}, {**FULL, "pricePerWalk": 1_000_001},
        {**FULL, "bio": "x" * 501}, {**FULL, "serviceArea": "x" * 121},
        {**FULL, "phone": "call me"}, {**FULL, "phone": "1" * 25},
    ])
    def test_validation(self, fake_client, data, body):
        assert put_profile(fake_client, body).status_code == 400

    def test_the_walker_id_comes_from_the_path_guard_not_the_body(self, fake_client, data):
        put_profile(fake_client, {**FULL, "walkerId": "walker-v", "walker_id": "walker-v"})
        assert {r["walker_id"] for r in data.tables["walker_profiles"]} == {"walker-w"}

    @pytest.mark.parametrize("token, walker", [("token-v", "walker-w"), ("token-a", "walker-w"), ("token-a", "owner-a")])
    def test_nobody_else_reads_or_writes_it(self, fake_client, data, token, walker):
        assert get_profile(fake_client, token=token, walker=walker).status_code == 403
        assert put_profile(fake_client, FULL, token=token, walker=walker).status_code == 403

    def test_stats(self, fake_client, data):
        data.seed("walk_requests", id="w1", owner_id="owner-a", walker_id="walker-w", status="done")
        data.seed("walk_requests", id="w2", owner_id="owner-a", walker_id="walker-w", status="assigned")
        data.seed("walk_requests", id="w3", owner_id="owner-a", walker_id="walker-v", status="done")
        data.seed("walk_ratings", walk_request_id="w1", walker_id="walker-w", rating=4,
                  created_at="2026-09-01T00:00:00+00:00")
        data.seed("walk_ratings", walk_request_id="w0", walker_id="walker-w", rating=5,
                  created_at="2026-09-01T00:00:00+00:00")
        assert get_profile(fake_client).json()["stats"] == {"walksDone": 1, "rating": {"average": 4.5, "count": 2}}


class TestAvatar:
    def upload(self, client, data, mime="image/png", token="token-w"):
        return client.post("/walkers/walker-w/avatar", files={"photo": ("a.png", data, mime)}, headers=bearer(token))

    def test_needs_a_profile_first(self, fake_client, data):
        res = self.upload(fake_client, png_bytes())
        assert res.status_code == 409
        assert res.json() == {"detail": "Save your profile before adding a photo."}
        assert not data.storage.objects

    def test_stored_as_a_512px_webp_with_a_signed_url(self, fake_client, data):
        put_profile(fake_client, FULL)
        res = self.upload(fake_client, png_bytes())
        assert res.status_code == 200
        assert "signed/avatars/walker-w/avatar.webp" in res.json()["avatarUrl"]
        image = Image.open(io.BytesIO(data.storage.objects[(BUCKET, "walker-w/avatar.webp")]))
        assert image.format == "WEBP" and max(image.size) <= 512
        assert get_profile(fake_client).json()["avatarUrl"] is not None

    def test_replacing_keeps_one_object(self, fake_client, data):
        put_profile(fake_client, FULL)
        self.upload(fake_client, png_bytes())
        assert self.upload(fake_client, png_bytes((300, 300))).status_code == 200
        assert [k for k in data.storage.objects if k[0] == BUCKET] == [(BUCKET, "walker-w/avatar.webp")]

    def test_never_public(self, fake_client, data):
        put_profile(fake_client, FULL)
        self.upload(fake_client, png_bytes())
        assert not [c for c in data.storage.calls if c[1] == "public"]

    def test_a_script_named_png_is_refused(self, fake_client, data):
        put_profile(fake_client, FULL)
        res = self.upload(fake_client, b"#!/bin/sh\nrm -rf /\n" + b"\x00" * 64)
        assert res.status_code == 400 and res.json() == UNREADABLE

    def test_a_decompression_bomb_is_refused(self, fake_client, data):
        put_profile(fake_client, FULL)
        buffer = io.BytesIO()
        Image.new("1", (6000, 6000), 0).save(buffer, format="PNG")
        assert self.upload(fake_client, buffer.getvalue()).status_code == 400
        assert not data.storage.objects

    def test_over_2_mb_is_413(self, fake_client, data):
        put_profile(fake_client, FULL)
        res = self.upload(fake_client, b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024 + 1))
        assert res.status_code == 413 and res.json() == TOO_BIG

    def test_another_walker_cannot_upload_mine(self, fake_client, data):
        assert self.upload(fake_client, png_bytes(), token="token-v").status_code == 403


class TestConsent:
    def test_recorded_with_version_and_time(self, fake_client, data):
        put_profile(fake_client, FULL)
        res = fake_client.post("/walkers/walker-w/consent", json={"version": "2026-10"}, headers=bearer("token-w"))
        assert res.status_code == 200
        assert res.json() == {"version": "2026-10", "at": "2026-10-01T00:00:00+00:00"}

    def test_needs_a_profile_first(self, fake_client, data):
        res = fake_client.post("/walkers/walker-w/consent", json={"version": "2026-10"}, headers=bearer("token-w"))
        assert res.status_code == 409

    @pytest.mark.parametrize("version", ["", "v" * 21])
    def test_version_is_bounded(self, fake_client, data, version):
        put_profile(fake_client, FULL)
        res = fake_client.post("/walkers/walker-w/consent", json={"version": version}, headers=bearer("token-w"))
        assert res.status_code == 400

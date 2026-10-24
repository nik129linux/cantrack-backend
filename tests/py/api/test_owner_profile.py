"""PR4 Perfil: the owner's profile, avatar and data-protection consent."""

import io

import pytest
from PIL import Image

from .conftest import bearer

BUCKET = "avatars"
UNREADABLE = {"detail": "Unreadable image."}
TOO_BIG = {"detail": "The avatar must be 2 MB or less."}


def png_bytes(size=(900, 600)):
    buffer = io.BytesIO()
    Image.new("RGB", size, (200, 120, 80)).save(buffer, format="PNG")
    return buffer.getvalue()


def get_profile(client, token="token-a", owner="owner-a"):
    return client.get(f"/owners/{owner}/profile", headers=bearer(token))


def put_profile(client, body, token="token-a", owner="owner-a"):
    return client.put(f"/owners/{owner}/profile", json=body, headers=bearer(token))


class TestProfile:
    def test_a_new_owner_has_an_empty_profile(self, fake_client):
        assert get_profile(fake_client).json() == {
            "ownerId": "owner-a", "email": "owner-a@example.com", "displayName": None, "phone": None,
            "avatarUrl": None, "home": None, "consent": None, "stats": {"dogs": 0, "walksDone": 0},
        }

    def test_save_and_read_back(self, fake_client, fake):
        body = {"displayName": "Camila", "phone": "+57 300 123 4567",
                "home": {"lat": 1.2136, "lng": -77.2811, "label": "Casa, Barrio Las Cuadras"}}
        res = put_profile(fake_client, body)
        assert res.status_code == 200
        profile = get_profile(fake_client).json()
        assert profile["displayName"] == "Camila"
        assert profile["phone"] == "+57 300 123 4567"
        assert profile["home"] == body["home"]

    def test_saving_twice_keeps_one_row(self, fake_client, fake):
        put_profile(fake_client, {"displayName": "A"})
        put_profile(fake_client, {"displayName": "B"})
        rows = [r for r in fake.tables["owner_profiles"] if r["owner_id"] == "owner-a"]
        assert len(rows) == 1 and rows[0]["display_name"] == "B"

    @pytest.mark.parametrize("body", [
        {"displayName": ""}, {"displayName": "x" * 81},
        {"displayName": "A", "phone": "call me"}, {"displayName": "A", "phone": "1" * 25},
        {"displayName": "A", "home": {"lat": 91, "lng": 0, "label": "x"}},
        {"displayName": "A", "home": {"lat": 1, "lng": 1, "label": "x" * 121}},
    ])
    def test_validation(self, fake_client, body):
        assert put_profile(fake_client, body).status_code == 400

    def test_the_owner_id_comes_from_the_path_guard_not_the_body(self, fake_client, fake):
        put_profile(fake_client, {"displayName": "Mine", "ownerId": "owner-b", "owner_id": "owner-b"})
        assert {r["owner_id"] for r in fake.tables["owner_profiles"]} == {"owner-a"}

    @pytest.mark.parametrize("token", ["token-b", "token-w"])
    def test_nobody_else_reads_or_writes_it(self, fake_client, token):
        assert get_profile(fake_client, token=token).status_code == 403
        assert put_profile(fake_client, {"displayName": "X"}, token=token).status_code == 403

    def test_stats(self, fake_client, fake):
        fake.seed("dogs", id="d1", owner_id="owner-a", name="Luna")
        fake.seed("walk_requests", id="w1", owner_id="owner-a", status="done")
        fake.seed("walk_requests", id="w2", owner_id="owner-a", status="open")
        assert get_profile(fake_client).json()["stats"] == {"dogs": 1, "walksDone": 1}


class TestAvatar:
    def upload(self, client, data, mime="image/png", token="token-a"):
        return client.post("/owners/owner-a/avatar", files={"photo": ("a.png", data, mime)}, headers=bearer(token))

    def test_stored_as_a_512px_webp_with_a_signed_url(self, fake_client, fake):
        res = self.upload(fake_client, png_bytes())
        assert res.status_code == 200
        assert "signed/avatars/owner-a/avatar.webp" in res.json()["avatarUrl"]
        stored = fake.storage.objects[(BUCKET, "owner-a/avatar.webp")]
        image = Image.open(io.BytesIO(stored))
        assert image.format == "WEBP"
        assert max(image.size) <= 512
        assert get_profile(fake_client).json()["avatarUrl"] is not None

    def test_replacing_keeps_one_object(self, fake_client, fake):
        self.upload(fake_client, png_bytes())
        assert self.upload(fake_client, png_bytes((300, 300))).status_code == 200
        assert [k for k in fake.storage.objects if k[0] == BUCKET] == [(BUCKET, "owner-a/avatar.webp")]

    def test_never_public(self, fake_client, fake):
        self.upload(fake_client, png_bytes())
        assert not [c for c in fake.storage.calls if c[1] == "public"]

    def test_a_script_named_png_is_refused(self, fake_client, fake):
        res = self.upload(fake_client, b"#!/bin/sh\nrm -rf /\n" + b"\x00" * 64)
        assert res.status_code == 400
        assert res.json() == UNREADABLE
        assert not fake.storage.objects

    def test_over_2_mb_is_413(self, fake_client):
        res = self.upload(fake_client, b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024 + 1))
        assert res.status_code == 413
        assert res.json() == TOO_BIG

    def test_another_owner_cannot_upload_mine(self, fake_client):
        assert self.upload(fake_client, png_bytes(), token="token-b").status_code == 403


class TestConsent:
    def test_consent_is_recorded_with_its_version_and_time(self, fake_client):
        res = fake_client.post("/owners/owner-a/consent", json={"version": "2026-10"}, headers=bearer("token-a"))
        assert res.status_code == 200
        assert res.json() == {"version": "2026-10", "at": "2026-10-01T00:00:00+00:00"}
        assert get_profile(fake_client).json()["consent"] == {"version": "2026-10", "at": "2026-10-01T00:00:00+00:00"}

    @pytest.mark.parametrize("version", ["", "v" * 21])
    def test_version_is_bounded(self, fake_client, version):
        res = fake_client.post("/owners/owner-a/consent", json={"version": version}, headers=bearer("token-a"))
        assert res.status_code == 400

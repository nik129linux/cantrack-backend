"""The owner can see the reference photos they enrolled for a dog."""

import io

import pytest
from PIL import Image

from .conftest import bearer
from .fake_ai import E_LUNA, E_REX

OWNER = "token-a"  # owner-a
BUCKET = "avatars"


def png(color: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (1600, 900), color).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def rex(fake):
    return fake.seed("dogs", owner_id="owner-a", name="Rex", embedding=E_REX)


def enroll(client, dog_id, *photos, token=OWNER):
    files = [("image", (f"{i}.png", data, "image/png")) for i, data in enumerate(photos)]
    return client.post(f"/dogs/{dog_id}/photos", files=files, headers=bearer(token))


def listed(client, dog_id, token=OWNER):
    return client.get(f"/dogs/{dog_id}/photos", headers=bearer(token))


class TestDogPhotos:
    def test_enrolled_photos_are_stored_small_and_listed_signed(self, fake_client, fake, rex, embedder):
        red, blue = png("red"), png("blue")
        embedder.vectors[red] = E_LUNA
        embedder.vectors[blue] = E_LUNA
        assert enroll(fake_client, rex["id"], red, blue).status_code == 200

        first = fake.storage.objects[(BUCKET, f"owner-a/dogs/{rex['id']}/0.webp")]
        image = Image.open(io.BytesIO(first))
        assert image.format == "WEBP"
        assert max(image.size) == 1024

        res = listed(fake_client, rex["id"])
        assert res.status_code == 200
        photos = res.json()["photos"]
        assert len(photos) == 2
        assert f"signed/{BUCKET}/owner-a/dogs/{rex['id']}/0.webp" in photos[0]

    def test_a_new_enrolment_replaces_the_old_photos(self, fake_client, fake, rex, embedder):
        red, blue, green = png("red"), png("blue"), png("green")
        for data in (red, blue, green):
            embedder.vectors[data] = E_LUNA
        enroll(fake_client, rex["id"], red, blue)
        enroll(fake_client, rex["id"], green)
        assert len(listed(fake_client, rex["id"]).json()["photos"]) == 1

    def test_no_photos_yet_is_an_empty_list(self, fake_client, rex):
        assert listed(fake_client, rex["id"]).json() == {"photos": []}

    def test_another_owners_dog_is_404(self, fake_client, rex):
        assert listed(fake_client, rex["id"], token="token-b").status_code == 404

    def test_deleting_the_dog_removes_its_photos(self, fake_client, fake, rex, embedder):
        red = png("red")
        embedder.vectors[red] = E_LUNA
        enroll(fake_client, rex["id"], red)
        fake_client.delete(f"/dogs/{rex['id']}", headers=bearer(OWNER))
        assert not [key for key in fake.storage.objects if "/dogs/" in key[1]]

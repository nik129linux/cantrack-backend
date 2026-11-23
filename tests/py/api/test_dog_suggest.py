"""Breed/size suggestion from one photo: ownership, validation, budget, model chain.

The photo is only shown to the vision chain: nothing is written to the dogs table
nor to storage, and a model that cannot answer yields nulls instead of an error.
"""

import base64
import json
import uuid
from io import BytesIO

import httpx
import pytest
from PIL import Image

from cantrack_api.ai_chain import Provider
from cantrack_api.deps import get_dog_traits
from cantrack_api.dog_traits import PROMPT, ChainDogTraits, DogTraits
from cantrack_api.routers import dogs
from cantrack_api.uploads import MAX_IMAGE_BYTES

from .conftest import bearer
from .fake_ai import FakeDogTraits


@pytest.fixture
def traits(fake_client, monkeypatch):
    fake = FakeDogTraits()
    fake_client.app.dependency_overrides[get_dog_traits] = lambda: fake
    monkeypatch.setattr(dogs, "_suggest_now", lambda: 1000.0)
    dogs._reset_suggest_buckets()
    yield fake
    dogs._reset_suggest_buckets()
    fake_client.app.dependency_overrides.pop(get_dog_traits, None)


@pytest.fixture
def photo():
    buffer = BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="PNG")
    return buffer.getvalue()


def suggest(client, dog_id, photo, token="token-a", mime="image/png"):
    return client.post(
        f"/dogs/{dog_id}/suggest", headers=bearer(token),
        files={"image": ("dog.png", photo, mime)},
    )


# ---------------------------------------------------------------- endpoint

def test_owner_gets_traits_without_storing_anything(fake_client, fake, traits, photo):
    dog = fake.seed("dogs", owner_id="owner-a", name="Rex")
    before = dict(dog)
    response = suggest(fake_client, dog["id"], photo)
    assert response.status_code == 200
    assert response.json() == {"breed": "Mestizo", "size": "medium"}
    assert traits.calls == [(photo, "image/png")]
    assert fake.tables["dogs"] == [before]
    assert fake.storage.calls == []
    assert fake.queries == [("dogs", "select", [("id", dog["id"]), ("owner_id", "owner-a")])]


@pytest.mark.parametrize("foreign", [True, False], ids=["other-owner", "missing"])
def test_unowned_dog_never_calls_model(fake_client, fake, traits, photo, foreign):
    dog_id = fake.seed("dogs", owner_id="owner-b")["id"] if foreign else str(uuid.uuid4())
    response = suggest(fake_client, dog_id, photo)
    assert response.status_code == 404
    assert response.json() == {"detail": dogs.NOT_FOUND_MESSAGE}
    assert traits.calls == []
    assert dogs._suggest_buckets == {}


@pytest.mark.parametrize("data,mime,code", [
    (b"garbage", "image/png", 400),
    (b"", "image/jpeg", 400),
    (b"not a photo", "text/plain", 400),
    (b"x" * (MAX_IMAGE_BYTES + 1), "image/png", 413),
], ids=["unreadable", "empty", "unsupported", "oversized"])
def test_invalid_photo_precedes_lookup(fake_client, fake, traits, data, mime, code):
    response = suggest(fake_client, uuid.uuid4(), data, mime=mime)
    assert response.status_code == code
    assert traits.calls == []
    assert fake.queries == []
    assert dogs._suggest_buckets == {}


def test_decompression_bomb_is_refused_before_decoding(fake_client, fake, traits, monkeypatch, photo):
    monkeypatch.setattr(dogs, "MAX_AVATAR_PIXELS", 3)  # the 2x2 photo has 4 pixels
    response = suggest(fake_client, uuid.uuid4(), photo)
    assert response.status_code == 400
    assert traits.calls == []
    assert fake.queries == []


def test_walker_is_forbidden(fake_client, fake, traits, photo):
    response = suggest(fake_client, uuid.uuid4(), photo, token="token-w")
    assert response.status_code == 403
    assert response.json() == {"detail": dogs.OWNERS_ONLY_MESSAGE}
    assert traits.calls == []
    assert fake.queries == []


def test_model_failure_returns_nulls(fake_client, fake, traits, photo):
    dog = fake.seed("dogs", owner_id="owner-a")
    traits.result = None
    response = suggest(fake_client, dog["id"], photo)
    assert response.status_code == 200
    assert response.json() == {"breed": None, "size": None}


def test_budget_is_per_owner_and_refills_each_hour(fake_client, fake, traits, photo, monkeypatch):
    first = fake.seed("dogs", owner_id="owner-a")["id"]
    second = fake.seed("dogs", owner_id="owner-a")["id"]
    other_owner = fake.seed("dogs", owner_id="owner-b")["id"]
    for _ in range(dogs.SUGGEST_BUDGET):
        assert suggest(fake_client, first, photo).status_code == 200
    response = suggest(fake_client, second, photo)
    assert response.status_code == 429
    assert response.json() == {"detail": dogs.SUGGEST_LIMIT_MESSAGE}
    assert len(traits.calls) == dogs.SUGGEST_BUDGET
    assert suggest(fake_client, other_owner, photo, token="token-b").status_code == 200
    monkeypatch.setattr(dogs, "_suggest_now", lambda: 4599.0)
    assert suggest(fake_client, first, photo).status_code == 429
    monkeypatch.setattr(dogs, "_suggest_now", lambda: 4600.0)
    for _ in range(dogs.SUGGEST_BUDGET):
        assert suggest(fake_client, first, photo).status_code == 200
    assert suggest(fake_client, first, photo).status_code == 429
    assert len(traits.calls) == 2 * dogs.SUGGEST_BUDGET + 1


# ---------------------------------------------------------------- chain client

def prov(name):
    return Provider(
        name=name, base_url=f"https://{name}.test/v1", api_key=f"key-{name}",
        text_model=f"{name}-text", vision_model=f"{name}-vision",
    )


def ok(content):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.mark.parametrize("content,expected", [
    ('{"breed":"  Pastor alemán  ","size":"large"}', DogTraits("Pastor alemán", "large")),
    ('```json\n{"breed":"Mestizo","size":"small"}\n```', DogTraits("Mestizo", "small")),
    ('{"breed":"Mestizo","size":"LARGE"}', DogTraits("Mestizo", None)),
    ('{"breed":"Mestizo","size":[]}', DogTraits("Mestizo", None)),
    (json.dumps({"breed": "a" * 100, "size": "medium"}), DogTraits("a" * 80, "medium")),
    ('{"breed":"  ","size":null}', DogTraits(None, None)),
    ('{"breed":23,"size":null}', DogTraits(None, None)),
    ('{"breed":null,"size":null}', DogTraits(None, None)),
])
def test_answer_is_sanitized_and_photo_sent_as_data_url(content, expected):
    def handler(request):
        assert str(request.url) == "https://gemini.test/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer key-gemini"
        payload = json.loads(request.content)
        assert payload["model"] == "gemini-vision"
        text, image = payload["messages"][0]["content"]
        assert text == {"type": "text", "text": PROMPT}
        encoded = base64.b64encode(b"photo").decode("ascii")
        assert image == {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}
        return ok(content)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        chain = ChainDogTraits([prov("gemini")], client=http)
        assert chain.suggest(b"photo", "image/png") == expected


@pytest.mark.parametrize("failure", ["status", "timeout", "body", "shape", "not-an-object"])
def test_a_failing_provider_hands_over_to_the_next(failure):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        if request.url.host == "groq.test":
            return ok('{"breed":"Beagle","size":"medium"}')
        if failure == "timeout":
            raise httpx.ReadTimeout("slow", request=request)
        if failure == "body":
            return httpx.Response(200, text="not JSON")
        if failure == "shape":
            return httpx.Response(200, json={"choices": []})
        if failure == "not-an-object":
            return ok("[]")
        return httpx.Response(429)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        chain = ChainDogTraits([prov("gemini"), prov("groq")], client=http)
        assert chain.suggest(b"photo", "image/jpeg") == DogTraits("Beagle", "medium")
    assert seen == ["gemini.test", "groq.test"]


def test_every_provider_failing_gives_none():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
        chain = ChainDogTraits([prov("gemini"), prov("groq")], client=http)
        assert chain.suggest(b"photo", "image/png") is None


def test_without_providers_it_is_disabled_and_makes_no_request():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        chain = ChainDogTraits([], client=http)
        assert chain.enabled is False
        assert chain.suggest(b"photo", "image/png") is None
    assert calls == []


def test_environment_reads_the_same_chain_as_the_photo_check(monkeypatch):
    for name in ("AI_CHAIN", "GEMINI_API_KEY", "GROQ_API_KEY", "GROQ_VISION_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert ChainDogTraits.from_env().enabled is False
    monkeypatch.setenv("AI_CHAIN", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_VISION_MODEL", "vision-x")
    chain = ChainDogTraits.from_env()
    assert chain.enabled is True
    assert [(p.name, p.vision_model) for p in chain._providers] == [("groq", "vision-x")]

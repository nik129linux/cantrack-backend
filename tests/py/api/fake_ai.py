"""Fakes for the two AI collaborators the API receives through dependencies."""

from cantrack_ai.embeddings import ImageError
from cantrack_ai.vision import PhotoCheck
from cantrack_api.dog_traits import DogTraits
from cantrack_api.walk_photo_ai import PhotoCondition

# Three well separated 4-d "CLIP" vectors. Real ones have 512 dims; the maths is the same.
E_REX = [1.0, 0.0, 0.0, 0.0]
E_LUNA = [0.0, 1.0, 0.0, 0.0]
E_NEAR_REX = [0.97, 0.1, 0.05, 0.0]  # cosine with E_REX ~ 0.99  (auto-confirm)
E_BETWEEN = [1.0, 1.0, 0.0, 0.0]  # cosine ~ 0.71 with both (manual pick)


class FakeEmbedder:
    """`embed(bytes)` looks the payload up in `vectors`; b'garbage' is unreadable."""

    def __init__(self) -> None:
        self.vectors: dict[bytes, list[float]] = {
            b"photo-near-rex": E_NEAR_REX,
            b"photo-between": E_BETWEEN,
            b"photo-rex": E_REX,
            b"photo-luna": E_LUNA,
        }
        self.calls: list[bytes] = []
        self.explode = False

    def embed(self, data: bytes) -> list[float]:
        self.calls.append(data)
        if self.explode:
            raise RuntimeError("onnx runtime blew up")
        if data not in self.vectors:
            raise ImageError("unreadable image")
        return list(self.vectors[data])


class FakeVision:
    def __init__(self) -> None:
        self.result = PhotoCheck(dog_visible=True, note="Calm and clean.")
        self.calls: list[bytes] = []

    def check(self, data: bytes) -> PhotoCheck:
        self.calls.append(data)
        return self.result


class FakeTextModel:
    """Double of ``OllamaText``: returns ``result`` (a dict or None) and records calls."""

    def __init__(self) -> None:
        self.result: dict | None = None
        self.enabled = True
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, system: str, prompt: str) -> dict | None:
        self.calls.append((system, prompt))
        return self.result


class FakeDogTraits:
    """Double of ``ChainDogTraits``: returns ``result`` (traits or None) and records calls."""

    def __init__(self) -> None:
        self.result: DogTraits | None = DogTraits("Mestizo", "medium")
        self.calls: list[tuple[bytes, str]] = []

    def suggest(self, image_bytes: bytes, mime: str) -> DogTraits | None:
        self.calls.append((image_bytes, mime))
        return self.result


class FakeCondition:
    """Double of ``ChainCondition``: returns ``result`` (a reading or None) and records calls."""

    def __init__(self) -> None:
        self.result: PhotoCondition | None = PhotoCondition(dog_visible=True, condition="ok", note=None)
        self.calls: list[bytes] = []

    def assess(self, image_bytes: bytes) -> PhotoCondition | None:
        self.calls.append(image_bytes)
        return self.result

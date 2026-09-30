"""Fakes for the two AI collaborators the API receives through dependencies."""

from cantrack_api.ai.embeddings import ImageError
from cantrack_api.ai.vision import PhotoCheck

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

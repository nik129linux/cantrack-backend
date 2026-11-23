"""Breed and size suggested from one photo, through the AI provider chain.

The owner's photo is only shown to the vision model: nothing is stored, and the
answer is a *suggestion* the owner can accept or ignore in the dog form. Like the
other chain clients, this one never raises — any failure (no provider configured,
a provider down, an answer that is not the JSON asked for) ends as ``None`` so the
endpoint can answer "no suggestion" instead of an error.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

import httpx

from .ai_chain import DEFAULT_TIMEOUT, Provider, _post, load_providers
from .ai_text import extract_object

PROMPT = (
    'Look at this photo and answer with JSON only: {"breed": '
    '"<most likely breed in Spanish, or Mestizo if mixed>", '
    '"size": "small"|"medium"|"large"}. '
    'If no dog is visible, return {"breed": null, "size": null}.'
)

#: Sizes the dog form accepts; anything else from the model is dropped.
SIZES = frozenset({"small", "medium", "large"})

#: Longest breed kept, so a rambling answer never reaches the form.
MAX_BREED_LENGTH = 80


@dataclass(frozen=True)
class DogTraits:
    """Suggested breed and size; either can be unknown."""

    breed: str | None
    size: str | None


def to_traits(answer: dict) -> DogTraits:
    """Sanitize one decoded model answer into traits. O(m) for m characters.

    Args:
        answer: The JSON object the model returned.

    Returns:
        The trimmed breed (or None) and a size from ``SIZES`` (or None).
    """
    raw_breed = answer.get("breed")
    breed = None
    if isinstance(raw_breed, str):
        breed = raw_breed.strip()[:MAX_BREED_LENGTH] or None
    size = answer.get("size")
    return DogTraits(breed, size if isinstance(size, str) and size in SIZES else None)


class ChainDogTraits:
    """Asks the first working vision provider for a dog's breed and size."""

    def __init__(
        self,
        providers: list[Provider],
        client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Configure the chain; without providers it is disabled. O(1).

        Args:
            providers: Providers in priority order.
            client: HTTP client to use. Created per request when omitted.
            timeout: Per-request timeout in seconds.
        """
        self._providers = list(providers)
        self._client = client
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> ChainDogTraits:
        """Build the chain from ``AI_CHAIN`` and the provider keys. O(len(chain)).

        Returns:
            A chain reading the same configuration as the check-in photo check.
        """
        return cls(load_providers(os.environ))

    @property
    def enabled(self) -> bool:
        """True when at least one provider is configured. O(1)."""
        return bool(self._providers)

    def suggest(self, image_bytes: bytes, mime: str) -> DogTraits | None:
        """Return the first provider's sanitized answer, or None. O(n + m).

        Args:
            image_bytes: Validated encoded image (n bytes).
            mime: Its validated MIME type, used in the data URL.

        Returns:
            Traits from the first answer that is a JSON object (m characters),
            or None when every provider failed or none is configured.
        """
        data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        for provider in self._providers:
            content = _post(
                self._client,
                provider,
                {
                    "model": provider.vision_model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": PROMPT},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        }
                    ],
                },
                self.timeout,
            )
            answer = extract_object(content) if content is not None else None
            if answer is not None:
                return to_traits(answer)
        return None

"""How the dog looks in a check-in or check-out photo, through the AI provider chain.

The walker's photo is shown to the vision model once and dropped. The answer is
advice for the walker and the owner, never a gate: like the other chain clients,
this one never raises, and every failure ends as ``None`` so the caller reports
``unavailable`` and the walk goes on.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass

import httpx

from .ai_chain import DEFAULT_TIMEOUT, Provider, _image_mime, _post, load_providers
from .ai_text import extract_object

PROMPT = (
    "You check a dog's condition for its owner, from one photo taken by the dog "
    "walker at pickup or at return. Answer with JSON only: "
    '{"dog_visible": true|false, "condition": "ok"|"concern", "note": <string or null>}. '
    'Use "concern" only for something visible that the owner should know: a wound, '
    "blood, limping or an odd posture, a missing or broken collar or harness, the "
    "dog soaked, very muddy or showing distress. Otherwise \"ok\". The note is one "
    "short sentence in Spanish (Colombia) describing the concern, or null when ok. "
    "Never guess about things the photo does not show. "
    'If no dog is visible, return {"dog_visible": false, "condition": null, "note": null}.'
)

#: Conditions the API reports; anything else from the model is discarded.
CONDITIONS = frozenset({"ok", "concern"})

#: Longest note kept, so a rambling answer never reaches the owner's screen.
MAX_NOTE_LENGTH = 160


@dataclass(frozen=True)
class PhotoCondition:
    """One reading of a photo: is a dog there, and how does it look."""

    dog_visible: bool
    condition: str | None
    note: str | None


def to_condition(answer: dict) -> PhotoCondition | None:
    """Sanitize one decoded model answer. O(m) for m characters.

    Args:
        answer: The JSON object the model returned.

    Returns:
        The reading, or None when the answer does not follow the asked shape.
    """
    visible = answer.get("dog_visible")
    if not isinstance(visible, bool):
        return None
    if not visible:
        return PhotoCondition(False, None, None)
    condition = answer.get("condition")
    if condition not in CONDITIONS:
        return None
    raw_note = answer.get("note")
    note = (raw_note.strip()[:MAX_NOTE_LENGTH] or None) if isinstance(raw_note, str) else None
    return PhotoCondition(True, condition, note)


def condition_label(reading: PhotoCondition | None) -> str:
    """Collapse a reading into the API's ``condition`` value. O(1).

    Returns:
        ``ok`` | ``concern`` | ``no_dog`` | ``unavailable``.
    """
    if reading is None:
        return "unavailable"
    if not reading.dog_visible:
        return "no_dog"
    return reading.condition or "unavailable"


class ChainCondition:
    """Asks the first working vision provider how the dog in a photo looks."""

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
    def from_env(cls) -> ChainCondition:
        """Build the chain from ``AI_CHAIN`` and the provider keys. O(len(chain)).

        Returns:
            A chain reading the same configuration as the other vision calls.
        """
        return cls(load_providers(os.environ))

    @property
    def enabled(self) -> bool:
        """True when at least one provider is configured. O(1)."""
        return bool(self._providers)

    def assess(self, image_bytes: bytes) -> PhotoCondition | None:
        """Return the first provider's sanitized reading, or None. O(n + m).

        Args:
            image_bytes: The encoded photo (n bytes).

        Returns:
            The first usable reading (m answer characters), or None when every
            provider failed or none is configured.
        """
        data_url = (
            f"data:{_image_mime(image_bytes)};base64,"
            f"{base64.b64encode(image_bytes).decode('ascii')}"
        )
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
            reading = to_condition(answer) if answer is not None else None
            if reading is not None:
                return reading
        return None

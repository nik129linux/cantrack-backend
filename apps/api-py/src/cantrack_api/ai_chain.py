"""Ordered chain of OpenAI-compatible providers for the AI features.

Ollama used to be the only backend. Now every provider that speaks the
OpenAI ``/chat/completions`` shape can serve the text and vision features, and
they are tried in the order given by ``AI_CHAIN``: a provider that fails in
any way (a 4xx/5xx status, a timeout, a transport error, an answer that is not
what was asked for) hands the request to the next one, and the first good
answer wins.

The contract is the one the Ollama clients kept: these clients never raise.
Text returns ``dict | None`` and vision returns ``PhotoCheck``
(``PhotoCheck(None, None)`` when nothing could be read), so callers fall back
to the plain flow without a try/except. The API key is never logged nor put in
an error message.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from cantrack_ai.vision import PROMPT, PhotoCheck, _to_photo_check

from .ai_text import extract_object

#: Chain used when ``AI_CHAIN`` is unset, in priority order.
DEFAULT_CHAIN = "gemini,groq"

DEFAULT_TIMEOUT = 8.0


@dataclass(frozen=True)
class Provider:
    """One OpenAI-compatible endpoint the chain may call.

    Attributes:
        name: Short identifier used in ``AI_CHAIN`` and in ``last_provider``.
        base_url: Root of the OpenAI-compatible API, with or without a
            trailing "/".
        api_key: Bearer token. A provider without one is never loaded.
        text_model: Model name for the JSON text features.
        vision_model: Model name for the photo checks.
    """

    name: str
    base_url: str
    api_key: str
    text_model: str
    vision_model: str


#: Documented defaults per provider name: base_url, text and vision models, and
#: the environment variable its key is read from.
KNOWN_PROVIDERS: dict[str, dict[str, str]] = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "text_model": "gemini-3.5-flash-lite",
        "vision_model": "gemini-3.5-flash-lite",
        "key_var": "GEMINI_API_KEY",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "text_model": "openai/gpt-oss-120b",
        "vision_model": "qwen/qwen3.8-27b",
        "key_var": "GROQ_API_KEY",
    },
}


def load_providers(env: Mapping[str, str]) -> list[Provider]:
    """Build the ordered provider list from an environment mapping. O(len(chain)).

    ``AI_CHAIN`` lists provider names in priority order (default
    ``"gemini,groq"``). Unknown names, repeated names and providers whose key
    is missing or blank are skipped, so a half-configured deployment degrades
    to the providers it can actually use — possibly none.

    Args:
        env: Environment to read, usually ``os.environ``.

    Returns:
        The usable providers, in chain order (empty when no key is set).
    """
    chain = env.get("AI_CHAIN") or DEFAULT_CHAIN
    providers: list[Provider] = []
    seen: set[str] = set()
    for raw_name in chain.split(","):
        name = raw_name.strip().lower()
        defaults = KNOWN_PROVIDERS.get(name)
        if defaults is None or name in seen:
            continue
        api_key = (env.get(defaults["key_var"]) or "").strip()
        if not api_key:
            continue
        seen.add(name)
        prefix = name.upper()
        providers.append(
            Provider(
                name=name,
                base_url=env.get(f"{prefix}_BASE_URL") or defaults["base_url"],
                api_key=api_key,
                text_model=env.get(f"{prefix}_TEXT_MODEL") or defaults["text_model"],
                vision_model=env.get(f"{prefix}_VISION_MODEL")
                or defaults["vision_model"],
            )
        )
    return providers


def _post(client: httpx.Client | None, provider: Provider, payload: dict[str, Any],
          timeout: float) -> str | None:
    """POST one chat completion and return its raw content, or None. O(len(body)).

    Every failure mode — disabled provider, transport error, timeout, a status
    other than 200, a body shaped differently than expected — is folded into
    ``None`` so the chain can simply move on.

    Args:
        client: HTTP client to use, or None to make a throwaway one.
        provider: The provider to call.
        payload: JSON body of the request.
        timeout: Per-request timeout in seconds.

    Returns:
        The ``message.content`` string, or None when it could not be obtained.
    """
    try:
        # No injected client: httpx.post opens AND closes its own, so nothing leaks.
        send = client.post if client is not None else httpx.post
        response = send(
            f"{provider.base_url.rstrip('/')}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {provider.api_key}"},
            timeout=timeout,
        )
        if response.status_code != httpx.codes.OK:
            return None
        body = response.json()
    except Exception:
        return None
    try:
        content = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    return content if isinstance(content, str) else None


class ChainText:
    """Asks the first working provider for one JSON object."""

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
        self._last_provider: str | None = None

    @classmethod
    def from_env(cls) -> ChainText:
        """Build a chain from the current environment. O(len(chain)).

        The environment is read here rather than at import time, so importing
        this module never depends on the deployment's configuration.

        Returns:
            A chain configured from ``AI_CHAIN`` and the provider keys.
        """
        return cls(load_providers(os.environ))

    @property
    def providers(self) -> list[Provider]:
        """The providers in priority order. O(1)."""
        return list(self._providers)

    @property
    def enabled(self) -> bool:
        """True when at least one provider is configured. O(1)."""
        return bool(self._providers)

    @property
    def last_provider(self) -> str | None:
        """Name of the provider that answered the last call, or None. O(1)."""
        return self._last_provider

    def complete_json(self, system: str, prompt: str) -> dict[str, Any] | None:
        """Return the first good JSON object any provider gives. O(len(answer)).

        Args:
            system: Instructions, including the exact keys expected.
            prompt: The user content (already bounded by the caller).

        Returns:
            The decoded JSON object, or None when every provider failed, was
            disabled, or answered something that is not an object.
        """
        self._last_provider = None
        for provider in self._providers:
            content = _post(
                self._client,
                provider,
                {
                    "model": provider.text_model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {"type": "json_object"},
                },
                self.timeout,
            )
            answer = extract_object(content) if content is not None else None
            if answer is not None:
                self._last_provider = provider.name
                return answer
        return None


def _image_mime(image_bytes: bytes) -> str:
    """Guess an image's MIME type from its magic bytes, defaulting to JPEG. O(1).

    Args:
        image_bytes: The raw contents of an encoded image.

    Returns:
        ``"image/png"``, ``"image/webp"`` or ``"image/jpeg"``.
    """
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class ChainVision:
    """Asks the first working provider whether a photo shows a dog."""

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
        self._last_provider: str | None = None

    @classmethod
    def from_env(cls) -> ChainVision:
        """Build a chain from the current environment. O(len(chain)).

        The environment is read here rather than at import time, so importing
        this module never depends on the deployment's configuration.

        Returns:
            A chain configured from ``AI_CHAIN`` and the provider keys.
        """
        return cls(load_providers(os.environ))

    @property
    def providers(self) -> list[Provider]:
        """The providers in priority order. O(1)."""
        return list(self._providers)

    @property
    def enabled(self) -> bool:
        """True when at least one provider is configured. O(1)."""
        return bool(self._providers)

    @property
    def last_provider(self) -> str | None:
        """Name of the provider that answered the last call, or None. O(1)."""
        return self._last_provider

    def check(self, image_bytes: bytes) -> PhotoCheck:
        """Ask the first working provider whether ``image_bytes`` shows a dog.

        This never raises: an unreachable service, a rejected key, a non-JSON
        body or a nonsensical answer all mean the same thing to a check-in —
        we could not verify the photo.

        Args:
            image_bytes: The raw contents of an encoded image.

        Returns:
            The first usable reading of the photo, or ``PhotoCheck(None, None)``
            when no provider produced one.
        """
        self._last_provider = None
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
                                {
                                    "type": "image_url",
                                    "image_url": {"url": data_url},
                                },
                            ],
                        }
                    ],
                },
                self.timeout,
            )
            if content is None:
                continue
            result = _to_photo_check({"message": {"content": content}})
            if result != PhotoCheck(None, None):
                self._last_provider = provider.name
                return result
        return PhotoCheck(None, None)

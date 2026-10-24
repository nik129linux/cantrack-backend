"""A small JSON-only client for an Ollama-hosted text model.

Two owner features use it: turning a typed walk request into a draft form
(Explorar) and writing the one-time walk report (Mis paseos). Both treat the
model as optional: this client never raises, and any failure is ``None`` so the
caller falls back to the plain flow.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

DEFAULT_HOST = "https://ollama.com"
DEFAULT_MODEL = "gemma4:31b"
DEFAULT_TIMEOUT = 20.0

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class OllamaText:
    """Asks a text model for one JSON object."""

    def __init__(
        self,
        host: str,
        api_key: str | None,
        model: str,
        client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Configure the client; without ``api_key`` it is disabled. O(1)."""
        self.host = host
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._client = client

    @classmethod
    def from_env(cls) -> OllamaText:
        """Build from ``OLLAMA_HOST``, ``OLLAMA_API_KEY``, ``OLLAMA_TEXT_MODEL``. O(1)."""
        return cls(
            host=os.environ.get("OLLAMA_HOST") or DEFAULT_HOST,
            api_key=os.environ.get("OLLAMA_API_KEY"),
            model=os.environ.get("OLLAMA_TEXT_MODEL") or DEFAULT_MODEL,
        )

    @property
    def enabled(self) -> bool:
        """True when a key is configured. O(1)."""
        return bool(self.api_key)

    def complete_json(self, system: str, prompt: str) -> dict[str, Any] | None:
        """Return the model's answer as a dict, or None on any failure. O(len(answer)).

        Args:
            system: Instructions, including the exact keys expected.
            prompt: The user content (already bounded by the caller).

        Returns:
            The decoded JSON object, or None when the model is disabled,
            unreachable, slow, or answers something that is not an object.
        """
        if not self.api_key:
            return None
        try:
            response = self._http().post(
                f"{self.host.removesuffix('/')}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "format": "json",
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                },
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=self.timeout,
            )
            if response.status_code != httpx.codes.OK:
                return None
            content = response.json()["message"]["content"]
        except Exception:
            return None
        return extract_object(content) if isinstance(content, str) else None

    def _http(self) -> httpx.Client:
        """Return the injected client, creating one on first use. O(1)."""
        if self._client is None:
            self._client = httpx.Client()
        return self._client


def extract_object(content: str) -> dict[str, Any] | None:
    """Pull a JSON object out of a model answer (plain, fenced, or embedded). O(n).

    Args:
        content: The raw ``message.content``.

    Returns:
        The decoded object, or None when no attempt yields one.
    """
    text = content.strip()
    start, end = text.find("{"), text.rfind("}")
    braces = text[start : end + 1] if start != -1 and end > start else text
    for candidate in (text, _FENCE_RE.sub("", text).strip(), braces):
        try:
            decoded = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(decoded, dict):
            return decoded
    return None

"""Shared FastAPI dependencies: the Supabase client, the AI collaborators and
the bearer-token guard.

Nothing here reads the environment at import time and no Supabase client is
built eagerly, so importing this module is always safe (tests and tooling
import it without any ``SUPABASE_*`` variable set).
"""

import os
from datetime import datetime, timezone
from functools import lru_cache

from fastapi import Depends, HTTPException, Request, status
from supabase import Client, create_client
from supabase_auth.errors import AuthError
from supabase_auth.types import User

from .ai.embeddings import ClipEmbedder
from .ai.vision import OllamaVision

BEARER = "bearer"

MISSING_TOKEN_MESSAGE = "A bearer token is required."
INVALID_TOKEN_MESSAGE = "Invalid authentication token."


@lru_cache(maxsize=1)
def get_supabase() -> Client:
    """Return the process-wide Supabase client, building it on first use.

    The client is created lazily and cached, so the environment is only read
    when a request actually needs it. Tests substitute their own double
    through ``app.dependency_overrides[get_supabase]``, which is why routes
    must receive it with ``Depends(get_supabase)`` instead of importing a
    module-level instance.

    Returns:
        A ``supabase`` client authenticated with the service-role key.

    Raises:
        KeyError: If ``SUPABASE_URL`` or ``SUPABASE_SERVICE_ROLE_KEY`` is unset.
    """
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


@lru_cache(maxsize=1)
def get_embedder() -> ClipEmbedder:
    """Return the process-wide CLIP embedder, building it on first use.

    The model file is only downloaded and the onnxruntime session only created
    when a request actually needs an embedding, so nothing slow happens at
    import or startup. Tests substitute their own double through
    ``app.dependency_overrides[get_embedder]``.

    Returns:
        A ``ClipEmbedder`` reading its model location from the environment.
    """
    return ClipEmbedder()


@lru_cache(maxsize=1)
def get_vision() -> OllamaVision:
    """Return the process-wide Ollama vision client, building it on first use.

    Configuration is read lazily for the same reason as the embedder, and the
    client is disabled (it makes no requests) when ``OLLAMA_API_KEY`` is unset.
    Tests substitute their own double through
    ``app.dependency_overrides[get_vision]``.

    Returns:
        An ``OllamaVision`` configured from the environment.
    """
    return OllamaVision.from_env()


def get_now() -> datetime:
    """Return the current time as an aware UTC datetime.

    Exposed as a dependency (like ``get_supabase`` or ``get_embedder``) so
    tests can freeze the clock through ``app.dependency_overrides``: the S1
    request endpoints use it to refuse a ``requestedTime`` in the past. Not
    cached — every call must see the real current time in production.

    Returns:
        ``datetime.now(timezone.utc)``.
    """
    return datetime.now(timezone.utc)


def get_auth_client() -> Client:
    """Return a FRESH Supabase client for one sign-up / login / reset call.

    supabase-py reacts to a sign-in by swapping the client's REST Authorization
    header to the user's JWT. On the shared, cached service-role client from
    ``get_supabase`` that silently turns every later table query into a query
    *as that user* (so row level security blocks it, and concurrent requests
    would borrow each other's identity). Auth calls therefore never touch the
    shared client: each one gets its own throwaway instance.

    Returns:
        A new ``supabase`` client built from the service-role key.

    Raises:
        KeyError: If ``SUPABASE_URL`` or ``SUPABASE_SERVICE_ROLE_KEY`` is unset.
    """
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


def get_current_user(
    request: Request,
    supabase: Client = Depends(get_supabase),
) -> User:
    """Resolve the Supabase user behind the request's bearer token.

    A missing or malformed ``Authorization`` header is rejected before the
    Supabase client is touched. A token Supabase refuses, or a lookup that
    yields no user, is also a 401.

    Args:
        request: The incoming request, read for its ``Authorization`` header.
        supabase: The Supabase client used to validate the token.

    Returns:
        The authenticated ``User``.

    Raises:
        HTTPException: 401 if no usable bearer token is present, or if Supabase
            rejects the token or returns no user for it.
    """
    authorization = request.headers.get("authorization")
    parts = authorization.split(" ") if authorization is not None else []
    scheme = parts[0] if parts else None
    token = parts[1] if len(parts) > 1 else None

    if scheme is None or scheme.lower() != BEARER or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=MISSING_TOKEN_MESSAGE
        )

    try:
        result = supabase.auth.get_user(token)
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_TOKEN_MESSAGE
        ) from exc

    user = getattr(result, "user", None) if result is not None else None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_TOKEN_MESSAGE
        )

    return user
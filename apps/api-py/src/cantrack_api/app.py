"""Application factory: CORS wiring and a 400 for every validation failure."""

import json
import os

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .routers import auth, dogs, requests, routes, walker_profiles

DEFAULT_WEB_ORIGIN = "http://localhost:5173"


def _web_origins() -> list[str]:
    """Return the allowed browser origins, read from the environment now.

    ``WEB_ORIGIN`` is a comma-separated list. Reading it per call keeps the
    value out of import time, so the factory stays testable and re-readable
    after the environment changes.

    Returns:
        The origins CORS should accept, defaulting to the Vite dev server.
    """
    raw = os.environ.get("WEB_ORIGIN", DEFAULT_WEB_ORIGIN)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def create_app() -> FastAPI:
    """Build the CanTrack FastAPI application.

    Creates no Supabase client and reads no ``SUPABASE_*`` variable, so it can
    be called in any environment; the client is only built when a route's
    dependencies are resolved.

    Returns:
        A ready FastAPI app with CORS enabled and the auth router mounted.
    """
    application = FastAPI(title="CanTrack API")

    application.add_middleware(
        CORSMiddleware,
        allow_origins=_web_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.exception_handler(RequestValidationError)
    async def _on_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Answer every validation failure with 400 instead of FastAPI's 422.

        Covers both malformed JSON and fields that fail validation. The error
        list is round-tripped through ``json`` because ``ctx`` can hold
        exception objects that are not serializable.
        """
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": json.loads(json.dumps(exc.errors(), default=str))},
        )

    application.include_router(auth.router)
    application.include_router(dogs.router)
    application.include_router(routes.router)
    application.include_router(walker_profiles.router)
    application.include_router(requests.router)
    return application
"""Application factory: CORS wiring, browser security headers, and a 400 for
every validation failure.
"""

import json
import os

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response
from supabase_auth.types import User

from .deps import get_current_user
from .routers import (
    auth,
    bidding,
    checkouts,
    clients,
    dogs,
    plans,
    requests,
    routes,
    walker_profiles,
)

DEFAULT_WEB_ORIGIN = "http://localhost:5173"

#: Headers every response carries, the 401/400/500 ones included. The API
#: answers JSON to the web app only, so nothing here may be sniffed, framed or
#: leaked through the Referer header.
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Stamp the browser security headers onto every response.

    O(1) per request: a fixed set of header assignments, so the middleware
    adds no per-request work that grows with the app.
    """

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Return the inner response with ``SECURITY_HEADERS`` applied.

        Args:
            request: The inbound request, unused — the headers are static.
            call_next: Awaits the inner application and returns its response.

        Returns:
            The inner response object, with the security headers set on it.
        """
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers[header] = value
        return response


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

    # CORS is added first, as before; Starlette wraps middleware in reverse
    # order of insertion, so the security headers end up on the outside and
    # therefore cover the CORS responses and the error responses (401/400/500)
    # that the exception handlers below produce.
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_web_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(SecurityHeadersMiddleware)

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
    application.include_router(bidding.router)
    application.include_router(dogs.router)
    application.include_router(routes.router)
    application.include_router(walker_profiles.router)
    application.include_router(requests.router)
    application.include_router(clients.router)
    application.include_router(plans.router)
    application.include_router(checkouts.router)

    @application.get("/_whoami")
    def _whoami(user: User = Depends(get_current_user)) -> dict[str, str]:
        """Return the identity the request's bearer token resolved to.

        No production screen needs it, but the guard needs a route to hang
        from, and a token echo is the cheapest one: a bad or missing token is
        a 401 before Supabase is reached, and a good one yields the caller's
        own id and email.

        Args:
            user: The authenticated user, resolved by the bearer-token guard.

        Returns:
            The caller's id and email, and nothing else.

        Raises:
            HTTPException: 401 if the token is missing, malformed or refused.
        """
        return {"id": user.id, "email": user.email}

    return application
"""Application factory: CORS wiring, browser security headers, and a 400 for
every validation failure.
"""

import hashlib
import json
import logging
import os
import uuid

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from .db import INTERNAL_ERROR_MESSAGE
from .deps import get_supabase

DEFAULT_WEB_ORIGIN = "http://localhost:5173"

#: Headers every response carries, the 401/400/500 ones included. The API
#: answers JSON to the web app only, so nothing here may be sniffed, framed or
#: leaked through the Referer header.
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
}

RATE_LIMIT_MESSAGE = "Too many requests. Try again in a minute."
BODY_TOO_LARGE_MESSAGE = "The request is too large."
JSON_BODY_LIMIT = 1024 * 1024
MULTIPART_BODY_LIMIT = 26 * 1024 * 1024

logger = logging.getLogger(__name__)


def rate_limit_key(request: Request) -> str:
    """Return the rate-limit bucket for a request.

    O(1): one header lookup and one hash of a short token.

    Args:
        request: The incoming request, read for its bearer token and client.

    Returns:
        ``user:<sha256 hex prefix>`` when a bearer token is present, else
        ``ip:<client host>``, so one flooding user never spends another's
        budget and the raw token never appears in logs or keys.
    """
    authorization = request.headers.get("authorization", "")
    parts = authorization.split(" ", 1) if authorization else []
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        digest = hashlib.sha256(parts[1].strip().encode()).hexdigest()[:32]
        return f"user:{digest}"
    host = request.client.host if request.client is not None else "unknown"
    return f"ip:{host}"


#: In-memory limiter for floodable endpoints. O(1) per request.
limiter = Limiter(key_func=rate_limit_key, default_limits=[])


class BodySizeLimitMiddleware:
    """Reject request bodies larger than the configured limits.

    O(n) in the request body size: every received byte is counted once and
    buffered once so the limit cannot be bypassed with a lying Content-Length
    or a chunked encoding.

    Args:
        app: The downstream ASGI application.
    """

    def __init__(self, app) -> None:
        """Store the downstream ASGI application. O(1)."""
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        """Count the streamed body and answer 413 when it exceeds the limit.

        O(n) in the body size: buffers the body once to replay it downstream.

        Args:
            scope: The ASGI connection scope.
            receive: The ASGI receive channel.
            send: The ASGI send channel.
        """
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        content_type = headers.get("content-type", "")
        limit = (
            MULTIPART_BODY_LIMIT
            if "multipart" in content_type.lower()
            else JSON_BODY_LIMIT
        )
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > limit:
                response = JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={"detail": BODY_TOO_LARGE_MESSAGE},
                )
                await response(scope, receive, send)
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        # Simple replay: the body was already fully buffered, so hand it over
        # in a single message no matter how it was chunked on the wire.
        full_body = b"".join(chunks)
        replayed = False

        async def _receive():
            """Return the buffered body once, then an empty message. O(1)."""
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": full_body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, _receive, send)


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
    is_production = os.environ.get("ENV") == "production"
    application = FastAPI(
        title="CanTrack API",
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )

    application.state.limiter = limiter

    @application.exception_handler(RateLimitExceeded)
    async def _on_rate_limit_exceeded(
        _request: Request, _exc: RateLimitExceeded
    ) -> JSONResponse:
        """Answer a spent rate-limit bucket with a 429 and a Retry-After.

        O(1): builds one fixed JSON response.

        Returns:
            A 429 carrying the public retry message and a Retry-After header.
        """
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": RATE_LIMIT_MESSAGE},
            headers={"Retry-After": "60"},
        )

    # Starlette wraps middleware in reverse order of insertion. The body-size
    # guard goes in first (innermost) so its 413 still passes through CORS and
    # the browser can read it; then CORS, SlowAPI, and the security headers
    # outermost, covering every response including the error ones.
    application.add_middleware(BodySizeLimitMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=_web_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(SlowAPIMiddleware)
    application.add_middleware(SecurityHeadersMiddleware)

    @application.exception_handler(RequestValidationError)
    async def _on_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Answer every validation failure with a 400 that never echoes input.

        O(n) in the number of validation errors, which is bounded by the
        number of submitted fields.

        Covers both malformed JSON and fields that fail validation. Only the
        ``loc``/``msg``/``type`` of each error is returned, so the submitted
        values in ``input``/``ctx`` can never be reflected back.
        """
        errors = [
            {
                "loc": list(error.get("loc", ())),
                "msg": error.get("msg", ""),
                "type": error.get("type", ""),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": json.loads(json.dumps(errors, default=str))},
        )

    @application.exception_handler(Exception)
    async def _on_unhandled_exception(_request: Request, exc: Exception) -> JSONResponse:
        """Answer any unhandled failure with an opaque 500 and an error id.

        O(1): one UUID and one fixed JSON body.

        The traceback goes to the server log only; the client gets a stable
        message plus an ``errorId`` the support team can correlate with it.
        """
        logger.exception("unhandled exception: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "detail": INTERNAL_ERROR_MESSAGE,
                "errorId": uuid.uuid4().hex,
            },
        )

    # Routers are imported lazily to avoid a circular import with
    # ``cantrack_api.app.limiter`` which the auth/bidding routers decorate.
    from .routers import (
        auth,
        bidding,
        checkouts,
        clients,
        dogs,
        explore,
        explore_ai,
        home,
        plans,
        profile,
        profile_data,
        requests,
        routes,
        walker_profiles,
        walks,
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
    # Owner tabs (PR1-PR4). Each module owns its endpoints; registered here once
    # so the tab PRs never touch this file.
    for owner_tab in (home, explore, explore_ai, walks, profile, profile_data):
        application.include_router(owner_tab.router)

    from starlette.exceptions import HTTPException as StarletteHTTPException

    _DEFAULT_DETAILS = {404: "Not Found", 405: "Method Not Allowed"}
    _FRIENDLY_DETAILS = {
        404: "We couldn't find what you're looking for.",
        405: "That action isn't allowed here.",
    }

    @application.exception_handler(StarletteHTTPException)
    async def _on_http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Replace default framework error details with friendly ones.

        Router-level HTTPExceptions that carry a specific detail are left untouched;
        only the framework defaults for 404 and 405 are replaced.
        """
        if isinstance(exc.detail, dict):
            return JSONResponse(
                status_code=exc.status_code,
                content=exc.detail,
            )
        if exc.status_code in _DEFAULT_DETAILS and exc.detail == _DEFAULT_DETAILS[exc.status_code]:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": _FRIENDLY_DETAILS[exc.status_code]},
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    @application.get("/health")
    def health(supabase=Depends(get_supabase)) -> dict[str, str]:
        """Check the API and its database dependency. O(1).

        Runs one cheap bounded select so a database outage surfaces here
        instead of on the first user request.

        Args:
            supabase: The Supabase client used for the probe query.

        Returns:
            ``{"status": "ok"}`` when the probe succeeds, else a 503 with
            ``{"status": "degraded"}`` that carries no error details.
        """
        try:
            supabase.table("dogs").select("id").limit(1).execute()
        except Exception:
            logger.exception("health: database probe failed")
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={"status": "degraded"},
            )
        return {"status": "ok"}

    return application

"""Shared helpers for turning a PostgREST query into an HTTP result.

Every CanTrack endpoint reads its rows through a ``supabase-py`` query builder
and then has to answer two questions: did the database accept the query, and did
it match the row the caller was promised? Keeping that translation here means
each router states *which* rows it wants while the failure mapping stays the
same everywhere: a PostgREST error is a 500 and a missing row is a 404.
"""

import logging
import uuid
from typing import Any

from fastapi import HTTPException, status
from postgrest.exceptions import APIError

logger = logging.getLogger(__name__)

INTERNAL_ERROR_MESSAGE = "Something went wrong on our side."
NOT_FOUND_MESSAGE = "We couldn't find what you're looking for."
OUT_OF_RANGE_MESSAGE = "A number in the request is out of range."


def _opaque_500() -> HTTPException:
    """Build the opaque 500 every unexpected database failure returns. O(1).

    The detail is a whole response body (``{detail, errorId}``); the app's
    HTTPException handler sends a dict detail as-is. Raised as an HTTPException
    rather than left to the generic handler so it is answered in-band.
    """
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"detail": INTERNAL_ERROR_MESSAGE, "errorId": uuid.uuid4().hex},
    )


def run_query(query: Any) -> list[dict[str, Any]]:
    """Execute a PostgREST query and return its rows.

    O(n) in the number of matched rows: the rows are returned as-is.

    Args:
        query: A ``supabase-py`` query builder, already filtered.

    Returns:
        The rows the query matched, which is an empty list when there are none.

    Raises:
        HTTPException: 404 when Postgres reports an invalid input shape
            (``22P02``), 400 when a number is out of range (``22003``), and an
            opaque 500 with an ``errorId`` for anything else, so no database
            text ever reaches the client.
    """
    try:
        result = query.execute()
    except APIError as exc:
        code = getattr(exc, "code", None)
        if code == "22P02":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=NOT_FOUND_MESSAGE,
            ) from exc
        if code == "22003":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=OUT_OF_RANGE_MESSAGE,
            ) from exc
        logger.exception("run_query: database rejected the query: %s", exc)
        raise _opaque_500() from exc

    return result.data or []


def first_row(rows: list[dict[str, Any]], message: str) -> dict[str, Any]:
    """Return the single row a request was expected to touch.

    Args:
        rows: The rows the query matched.
        message: The detail to answer with when nothing matched.

    Returns:
        The first matched row.

    Raises:
        HTTPException: 404 carrying ``message`` if no row matched, so a row owned
            by another user is never revealed to exist.
    """
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)
    return rows[0]

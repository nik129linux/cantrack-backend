"""Shared helpers for turning a PostgREST query into an HTTP result.

Every CanTrack endpoint reads its rows through a ``supabase-py`` query builder
and then has to answer two questions: did the database accept the query, and did
it match the row the caller was promised? Keeping that translation here means
each router states *which* rows it wants while the failure mapping stays the
same everywhere: a PostgREST error is a 500 and a missing row is a 404.
"""

from typing import Any

from fastapi import HTTPException, status
from postgrest.exceptions import APIError


def run_query(query: Any) -> list[dict[str, Any]]:
    """Execute a PostgREST query and return its rows.

    Args:
        query: A ``supabase-py`` query builder, already filtered.

    Returns:
        The rows the query matched, which is an empty list when there are none.

    Raises:
        HTTPException: 500 carrying PostgREST's own message if the database
            rejects the query.
    """
    try:
        result = query.execute()
    except APIError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=exc.message
        ) from exc

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

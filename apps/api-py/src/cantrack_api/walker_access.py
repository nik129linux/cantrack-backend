"""The guard every ``/walkers/{walkerId}/...`` endpoint hangs from, plus "online".

Same rule as ``owner_access``: the account is named in the path (no implicit
"/me"), so the token's user must BE that walker. An owner, or a walker asking
for another walker's id, is refused before any query runs.

Online, InDrive style: a walker is online while their app keeps sending its
position. There is no flag to forget to clear: the ``walker_positions`` row
IS the switch (going offline deletes it), and a row older than
``ONLINE_TTL_SECONDS`` means the app stopped sending, so the walker is offline.
"""

from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, Path, status
from supabase_auth.types import User

from .deps import current_role, get_current_user

WALKERS_ONLY_MESSAGE = "Only walkers can use this."
NOT_YOUR_ACCOUNT_MESSAGE = "You can only access your own account."

ONLINE_TTL_SECONDS = 120


def walker_from_path(
    walker_id: str = Path(alias="walkerId", min_length=1, max_length=64),
    user: User = Depends(get_current_user),
) -> User:
    """Return the caller when they are the walker named in the path. O(1).

    Args:
        walker_id: The ``walkerId`` path segment.
        user: The authenticated user behind the bearer token.

    Returns:
        The authenticated walker.

    Raises:
        HTTPException: 403 when the caller is not a walker, or is a walker
            asking for another account (same status either way, so the
            answer never says whether that id exists).
    """
    if current_role(user) != "walker":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WALKERS_ONLY_MESSAGE)
    if walker_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NOT_YOUR_ACCOUNT_MESSAGE)
    return user


def is_online(position: dict[str, Any] | None, now: datetime) -> bool:
    """Whether a ``walker_positions`` row is a live heartbeat at ``now``. O(1).

    A missing row, a missing or unparseable ``updated_at``, or one older than
    ``ONLINE_TTL_SECONDS`` all mean offline. A timestamp slightly ahead of
    ``now`` (clock skew) still counts as online.

    Args:
        position: The walker's ``walker_positions`` row, or None.
        now: The current time (timezone-aware).

    Returns:
        True when the walker is online.
    """
    if not position:
        return False
    try:
        updated_at = datetime.fromisoformat(position["updated_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return now - updated_at <= timedelta(seconds=ONLINE_TTL_SECONDS)

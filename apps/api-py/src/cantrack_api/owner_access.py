"""The guard every ``/owners/{ownerId}/...`` endpoint hangs from.

Owner endpoints name the account in the path instead of inferring it from the
token (the professor asked for no implicit "/me" routes). That makes the id
attacker-controlled, so the token's user must BE that owner: a walker, or an
owner asking for someone else's id, is refused before any query runs.
"""

from fastapi import Depends, HTTPException, Path, status
from supabase_auth.types import User

from .deps import current_role, get_current_user

OWNERS_ONLY_MESSAGE = "Only owners can use this."
NOT_YOUR_ACCOUNT_MESSAGE = "You can only access your own account."


def owner_from_path(
    owner_id: str = Path(alias="ownerId", min_length=1, max_length=64),
    user: User = Depends(get_current_user),
) -> User:
    """Return the caller when they are the owner named in the path. O(1).

    Args:
        owner_id: The ``ownerId`` path segment.
        user: The authenticated user behind the bearer token.

    Returns:
        The authenticated owner.

    Raises:
        HTTPException: 403 when the caller is not an owner, or is an owner
            asking for another account (same status either way, so the
            answer never says whether that id exists).
    """
    if current_role(user) != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=OWNERS_ONLY_MESSAGE)
    if owner_id != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=NOT_YOUR_ACCOUNT_MESSAGE)
    return user

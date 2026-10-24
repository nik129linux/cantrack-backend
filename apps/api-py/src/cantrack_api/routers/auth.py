"""FR-01/02/03: sign up, log in, and request a password reset."""

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from supabase import Client
from supabase_auth.errors import AuthError
from supabase_auth.types import User

from ..app import limiter
from ..deps import current_role, get_auth_client, get_current_user, get_supabase
from ..schemas import LoginBody, ResetPasswordBody, SignupBody, UserRole

router = APIRouter(prefix="/auth")
logger = logging.getLogger(__name__)

DEFAULT_RESET_REDIRECT_URL = "http://localhost:5173/reset-password"
RESET_SENT_MESSAGE = "Password reset email sent."
INVALID_CREDENTIALS_MESSAGE = "Invalid login credentials."
SIGNUP_FAILED_MESSAGE = "We couldn't create an account with those details."
SIGNUP_ROLLBACK_MESSAGE = "We couldn't finish creating your account. Please try again."
ROLE_ALREADY_SET_MESSAGE = "This account already has a role."
ROLE_SAVE_FAILED_MESSAGE = "We couldn't save your role. Please try again."


class RoleBody(BaseModel):
    """Body of ``POST /auth/role``: anything but owner/walker is a 400."""

    role: UserRole


@router.post("/signup", status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
def signup(
    request: Request,
    body: SignupBody,
    supabase: Client = Depends(get_auth_client),
) -> dict[str, Any]:
    """Create an account for a walker or an owner.

    Args:
        body: Email, password and the role to store in the user's metadata.
        supabase: The Supabase client used to create the account.

    Returns:
        The created ``user`` and the ``session``, the latter being ``None``
        when the project requires email confirmation before signing in.

    Raises:
        HTTPException: 400 if the signup fails, 503 if the server-side
            role cannot be stored.
    """
    try:
        result = supabase.auth.sign_up(
            {
                "email": body.email,
                "password": body.password,
                "options": {"data": {"role": body.role}},
            }
        )
    except (AuthError, AttributeError) as exc:
        # One message for every refusal: "already registered" would tell an
        # attacker which emails have accounts.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=SIGNUP_FAILED_MESSAGE
        ) from exc

    user = result.user
    session = result.session
    if user is not None:
        try:
            supabase.auth.admin.update_user_by_id(
                user.id, {"app_metadata": {"role": body.role}}
            )
        except Exception as exc:
            logger.exception("signup: storing the role failed, rolling back %s", user.id)
            try:
                supabase.auth.admin.delete_user(user.id)
            except Exception:
                logger.exception("signup: rollback failed, orphan account %s", user.id)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=SIGNUP_ROLLBACK_MESSAGE,
            ) from exc
    return {
        "user": user.model_dump(mode="json") if user is not None else None,
        "session": session.model_dump(mode="json") if session is not None else None,
    }


@router.post("/login")
@limiter.limit("5/minute")
def login(
    request: Request,
    body: LoginBody,
    supabase: Client = Depends(get_auth_client),
) -> dict[str, Any]:
    """Exchange email and password for a session.

    Args:
        body: The user's email and password.
        supabase: The Supabase client used to authenticate.

    Returns:
        The serialized session, including its access and refresh tokens.

    Raises:
        HTTPException: 401 if Supabase rejects the credentials, or if it
            returns no session for them.
    """
    try:
        result = supabase.auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
    except (AuthError, AttributeError) as exc:
        # "Email not confirmed" and "wrong password" look the same from outside.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_CREDENTIALS_MESSAGE,
        ) from exc

    session = result.session
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_CREDENTIALS_MESSAGE,
        )

    return session.model_dump(mode="json")


@router.post("/reset-password")
@limiter.limit("5/minute")
def reset_password(
    request: Request,
    body: ResetPasswordBody,
    supabase: Client = Depends(get_auth_client),
) -> dict[str, str]:
    """Email the user a link that lets them choose a new password.

    The redirect target is read from the environment on every request so a
    deployment can change it without a rebuild. The answer is the same
    whether or not the email exists, so accounts cannot be enumerated.

    Args:
        body: The email address to send the reset link to.
        supabase: The Supabase client used to send the email.

    Returns:
        A short confirmation message.
    """
    redirect_to = os.environ.get(
        "AUTH_RESET_REDIRECT_URL", DEFAULT_RESET_REDIRECT_URL
    )
    try:
        supabase.auth.reset_password_for_email(
            body.email, {"redirect_to": redirect_to}
        )
    except Exception:
        # Same answer either way, so the reset form cannot tell which emails
        # exist; the real failure goes to the server log only.
        logger.exception("reset-password: provider call failed")

    return {"message": RESET_SENT_MESSAGE}


@router.post("/role")
@limiter.limit("5/minute")
def set_role(
    request: Request,
    body: RoleBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Assign the caller's server-side role exactly once. O(1).

    Args:
        body: The requested role, which must be ``owner`` or ``walker``.
        user: The authenticated user resolved from the bearer token.
        supabase: The Supabase client used for the admin role update.

    Returns:
        The assigned ``role``.

    Raises:
        HTTPException: 409 when the account already has a server-side role,
            503 when the admin update fails. A role other than owner/walker
            is a 400 from the schema.
    """
    if current_role(user) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=ROLE_ALREADY_SET_MESSAGE
        )
    try:
        supabase.auth.admin.update_user_by_id(
            user.id, {"app_metadata": {"role": body.role}}
        )
    except Exception as exc:
        logger.exception("auth/role: admin update failed for %s", user.id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=ROLE_SAVE_FAILED_MESSAGE,
        ) from exc
    return {"role": body.role}

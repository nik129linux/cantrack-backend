"""FR-01/02/03: sign up, log in, and request a password reset."""

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from supabase import Client
from supabase_auth.errors import AuthError

from ..deps import get_supabase
from ..schemas import LoginBody, ResetPasswordBody, SignupBody

router = APIRouter(prefix="/auth")

DEFAULT_RESET_REDIRECT_URL = "http://localhost:5173/reset-password"
RESET_SENT_MESSAGE = "Password reset email sent."
INVALID_CREDENTIALS_MESSAGE = "Invalid login credentials."


@router.post("/signup", status_code=status.HTTP_201_CREATED)
def signup(
    body: SignupBody,
    supabase: Client = Depends(get_supabase),
) -> dict[str, Any]:
    """Create an account for a walker or an owner.

    Args:
        body: Email, password and the role to store in the user's metadata.
        supabase: The Supabase client used to create the account.

    Returns:
        The created ``user`` and the ``session``, the latter being ``None``
        when the project requires email confirmation before signing in.

    Raises:
        HTTPException: 400 with Supabase's own message if the signup fails.
    """
    try:
        result = supabase.auth.sign_up(
            {
                "email": body.email,
                "password": body.password,
                "options": {"data": {"role": body.role}},
            }
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message
        ) from exc

    user = result.user
    session = result.session
    return {
        "user": user.model_dump(mode="json") if user is not None else None,
        "session": session.model_dump(mode="json") if session is not None else None,
    }


@router.post("/login")
def login(
    body: LoginBody,
    supabase: Client = Depends(get_supabase),
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
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=exc.message
        ) from exc

    session = result.session
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_CREDENTIALS_MESSAGE,
        )

    return session.model_dump(mode="json")


@router.post("/reset-password")
def reset_password(
    body: ResetPasswordBody,
    supabase: Client = Depends(get_supabase),
) -> dict[str, str]:
    """Email the user a link that lets them choose a new password.

    The redirect target is read from the environment on every request so a
    deployment can change it without a rebuild.

    Args:
        body: The email address to send the reset link to.
        supabase: The Supabase client used to send the email.

    Returns:
        A short confirmation message.

    Raises:
        HTTPException: 400 with Supabase's own message if the email cannot
            be sent.
    """
    redirect_to = os.environ.get(
        "AUTH_RESET_REDIRECT_URL", DEFAULT_RESET_REDIRECT_URL
    )
    try:
        supabase.auth.reset_password_for_email(
            body.email, {"redirect_to": redirect_to}
        )
    except AuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=exc.message
        ) from exc

    return {"message": RESET_SENT_MESSAGE}
"""FR-01/02/03: sign up, log in, and request a password reset."""

import logging
import os
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from supabase import Client
from supabase_auth.errors import AuthError
from supabase_auth.types import User

from ..app import limiter
from ..deps import current_role, get_auth_client, get_current_user, get_now, get_supabase
from ..login_guard import LoginGuard, get_login_guard
from ..schemas import (
    ChangePasswordBody,
    LoginBody,
    LogoutBody,
    RefreshBody,
    ResendVerificationBody,
    ResetPasswordBody,
    SignupBody,
    UserRole,
    password_problem,
)

router = APIRouter(prefix="/auth")
logger = logging.getLogger(__name__)

DEFAULT_RESET_REDIRECT_URL = "http://localhost:5173/reset-password"
RESET_SENT_MESSAGE = "Password reset email sent."
INVALID_CREDENTIALS_MESSAGE = "Invalid login credentials."
SIGNUP_FAILED_MESSAGE = "We couldn't create an account with those details."
SIGNUP_ROLLBACK_MESSAGE = "We couldn't finish creating your account. Please try again."
ROLE_ALREADY_SET_MESSAGE = "This account already has a role."
ROLE_SAVE_FAILED_MESSAGE = "We couldn't save your role. Please try again."
NOT_CONFIRMED_MESSAGE = "Confirm your email first. We can send the link again."
LOCKED_MESSAGE = "Too many failed attempts. Try again in 15 minutes."
RESENT_MESSAGE = "If the account exists and is not verified, we sent a new link."
BAD_SESSION_MESSAGE = "Invalid or expired session."
WRONG_CURRENT_MESSAGE = "Current password is incorrect."
SAME_PASSWORD_MESSAGE = "Choose a password different from the current one."
CHANGED_MESSAGE = "Password changed. Every other session was signed out."
WEAK_PASSWORD_MESSAGE = "Password does not meet the security policy."


class RoleBody(BaseModel):
    """Body of ``POST /auth/role``: anything but owner/walker is a 400."""

    role: UserRole


class ResendVerificationResponse(BaseModel):
    """Answer of ``POST /auth/resend-verification``: always the same."""

    message: str


class RefreshResponse(BaseModel):
    """Answer of ``POST /auth/refresh``: the fresh session tokens."""

    model_config = ConfigDict(extra="allow")

    access_token: str
    refresh_token: str


class ChangePasswordResponse(BaseModel):
    """Answer of ``POST /auth/change-password``: the message and a NEW session.

    Supabase revokes every session of the user when the password changes, the
    caller's own included, so the API signs in again with the new password and
    hands that session back for this device (``setSession`` on the client).
    """

    message: str
    session: dict[str, Any]


def _bearer_token(request: Request) -> str:
    """Return the raw bearer token of the request, or an empty string. O(1).

    Args:
        request: The incoming request, read for its ``Authorization`` header.

    Returns:
        The token text, or ``""`` when the header is missing or malformed.
    """
    authorization = request.headers.get("authorization", "")
    parts = authorization.split(" ", 1) if authorization else []
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
        return parts[1].strip()
    return ""


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
    options: dict[str, Any] = {"data": {"role": body.role}}
    redirect_to = os.environ.get("AUTH_EMAIL_REDIRECT_URL")
    if redirect_to:
        options["email_redirect_to"] = redirect_to
    try:
        result = supabase.auth.sign_up(
            {
                "email": body.email,
                "password": body.password,
                "options": options,
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
    guard: LoginGuard = Depends(get_login_guard),
    now: datetime = Depends(get_now),
) -> dict[str, Any]:
    """Exchange email and password for a session.

    A locked address is refused before Supabase is touched, so the answer
    never says which emails exist. A correct password on an unconfirmed
    account is a 403 inviting a resend; anything else Supabase refuses is
    the generic 401.

    Args:
        body: The user's email and password.
        supabase: The Supabase client used to authenticate.
        guard: The per-email lockout guard.
        now: The current time, from the ``get_now`` clock.

    Returns:
        The serialized session, including its access and refresh tokens.

    Raises:
        HTTPException: 429 while the address is locked, 403 when the email
            is unconfirmed, 401 if Supabase rejects the credentials or
            returns no session for them.
    """
    locked = guard.seconds_locked(body.email, now)
    if locked > 0:
        # Returned (not raised): the app's HTTPException handler drops
        # custom headers, and the spec requires Retry-After on this 429.
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": LOCKED_MESSAGE},
            headers={"Retry-After": str(locked)},
        )
    try:
        result = supabase.auth.sign_in_with_password(
            {"email": body.email, "password": body.password}
        )
    except (AuthError, AttributeError) as exc:
        if getattr(exc, "code", None) == "email_not_confirmed":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=NOT_CONFIRMED_MESSAGE,
            ) from exc
        guard.record_failure(body.email, now)
        # "Email not confirmed" and "wrong password" look the same from outside.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_CREDENTIALS_MESSAGE,
        ) from exc

    session = result.session
    if session is None:
        guard.record_failure(body.email, now)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=INVALID_CREDENTIALS_MESSAGE,
        )

    guard.record_success(body.email)
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


@router.post(
    "/resend-verification", response_model=ResendVerificationResponse
)
@limiter.limit("3/minute")
def resend_verification(
    request: Request,
    body: ResendVerificationBody,
    supabase: Client = Depends(get_auth_client),
) -> dict[str, str]:
    """Send another signup confirmation link, answering the same always. O(1).

    The email is normalised before it is sent to the provider, and the
    provider's answer (or failure) never reaches the client, so the endpoint
    cannot tell which emails exist.

    Args:
        request: The incoming request, used by the rate limiter.
        body: The email address to send the verification link to.
        supabase: The Supabase client used to resend the link.

    Returns:
        The same confirmation message for every email.
    """
    normalised = str(body.email).strip().lower()
    payload: dict[str, Any] = {"type": "signup", "email": normalised}
    redirect_to = os.environ.get("AUTH_EMAIL_REDIRECT_URL")
    if redirect_to:
        payload["options"] = {"email_redirect_to": redirect_to}
    try:
        supabase.auth.resend(payload)
    except Exception:
        logger.exception("resend-verification: provider call failed")
    return {"message": RESENT_MESSAGE}


@router.post("/refresh", response_model=RefreshResponse)
@limiter.limit("30/minute")
def refresh(
    request: Request,
    body: RefreshBody,
    supabase: Client = Depends(get_auth_client),
) -> dict[str, Any]:
    """Exchange a refresh token for a fresh session. O(1).

    Args:
        request: The incoming request, used by the rate limiter.
        body: The refresh token to exchange.
        supabase: A fresh Supabase client used for the refresh call.

    Returns:
        The serialized fresh session.

    Raises:
        HTTPException: 401 when the token is invalid or already used.
    """
    try:
        result = supabase.auth.refresh_session(body.refreshToken)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=BAD_SESSION_MESSAGE,
        ) from exc
    session = getattr(result, "session", None)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=BAD_SESSION_MESSAGE,
        )
    return session.model_dump(mode="json")


@router.post("/change-password", response_model=ChangePasswordResponse)
@limiter.limit("5/minute")
def change_password(
    request: Request,
    body: ChangePasswordBody,
    user: User = Depends(get_current_user),
    auth_client: Client = Depends(get_auth_client),
    supabase: Client = Depends(get_supabase),
    guard: LoginGuard = Depends(get_login_guard),
    now: datetime = Depends(get_now),
) -> dict[str, str]:
    """Change the caller's password and sign out their other devices. O(1).

    The current password is verified with a fresh sign-in; a wrong one counts
    toward the per-email lockout. The new password must differ from the
    current one and pass the policy (including the email local-part check).

    Args:
        request: The incoming request, read for the caller's bearer token.
        body: The current and the new password.
        user: The authenticated user, resolved from the bearer token.
        auth_client: A fresh Supabase client used to verify the password.
        supabase: The Supabase client used for the admin update and sign-out.
        guard: The per-email lockout guard.
        now: The current time, from the ``get_now`` clock.

    Returns:
        A confirmation message.

    Raises:
        HTTPException: 401 when the current password is wrong, 400 when the
            new password equals it or fails the policy.
    """
    if body.newPassword == body.currentPassword:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=SAME_PASSWORD_MESSAGE,
        )
    problem = password_problem(body.newPassword, user.email)
    if problem is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=problem
        )
    locked = guard.seconds_locked(user.email, now)
    if locked > 0:
        # A locked account must not keep guessing its password through here.
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": LOCKED_MESSAGE},
            headers={"Retry-After": str(locked)},
        )
    try:
        auth_client.auth.sign_in_with_password(
            {"email": user.email, "password": body.currentPassword}
        )
    except Exception as exc:
        guard.record_failure(user.email, now)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=WRONG_CURRENT_MESSAGE,
        ) from exc
    # Supabase revokes ALL of the user's sessions on a password change (found
    # against the real project: a follow-up sign_out("others") then fails with
    # "Session ... does not exist"), so every other device is already out.
    supabase.auth.admin.update_user_by_id(user.id, {"password": body.newPassword})
    fresh = auth_client.auth.sign_in_with_password(
        {"email": user.email, "password": body.newPassword}
    )
    return {"message": CHANGED_MESSAGE, "session": fresh.session.model_dump(mode="json")}


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
def logout(
    request: Request,
    body: LogoutBody,
    user: User = Depends(get_current_user),
    supabase: Client = Depends(get_supabase),
) -> None:
    """Sign the caller out on this device, or everywhere. O(1).

    Args:
        request: The incoming request, read for the caller's bearer token.
        body: The sign-out scope, ``local`` (this device) or ``global``.
        user: The authenticated user, resolved from the bearer token.
        supabase: The Supabase client used for the admin sign-out.

    Returns:
        Nothing, with a 204 status.
    """
    _ = user
    supabase.auth.admin.sign_out(_bearer_token(request), body.scope)
    return None


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

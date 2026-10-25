"""Per-email login lockout backed by the ``auth_login_attempts`` table.

Only a hash of the email is stored (``sha256`` of the lowercased, stripped
address), so the table never reveals which emails tried to sign in. The
counter restarts when the first failure is older than 15 minutes; the 5th
failure inside the window locks the address for 15 minutes.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException
from supabase import Client

from .db import run_query
from .deps import get_supabase

TABLE = "auth_login_attempts"
MAX_FAILURES = 5
WINDOW = timedelta(minutes=15)
LOCK_DURATION = timedelta(minutes=15)
#: Compare-and-set retries before giving up on recording a failure.
MAX_WRITE_ATTEMPTS = 5


def hash_email(email: str) -> str:
    """Return the ``sha256`` hex digest of the normalised email. O(n).

    Args:
        email: The raw address, stripped and lowercased before hashing.

    Returns:
        The hex digest stored as ``email_hash``.
    """
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()


def _parse_moment(value: object) -> datetime | None:
    """Parse a stored timestamp back to a datetime, or None. O(1).

    Args:
        value: An ISO-8601 string, a datetime, or None/anything else.

    Returns:
        The parsed datetime, or None when it cannot be parsed.
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


class LoginGuard:
    """Track failed logins per email hash and enforce the 15-minute lock. O(1)."""

    def __init__(self, supabase: Client) -> None:
        """Store the data client used for the attempts table. O(1).

        Args:
            supabase: The Supabase client used for table reads and writes.
        """
        self._supabase = supabase

    def _find_row(self, email_hash: str) -> dict | None:
        """Return the attempt row for ``email_hash``, or None. O(1).

        Args:
            email_hash: The hashed email to look up.

        Returns:
            The stored row, or None when the address has no record.
        """
        rows = run_query(
            self._supabase.table(TABLE).select("*").eq("email_hash", email_hash)
        )
        return rows[0] if rows else None

    def seconds_locked(self, email: str, now: datetime) -> int:
        """Return how many seconds the email stays locked, or 0. O(1).

        Args:
            email: The address whose lock is checked.
            now: The current time, from the ``get_now`` clock.

        Returns:
            The seconds left until ``locked_until``, or 0 when the address
            is not locked or the lock already expired.
        """
        row = self._find_row(hash_email(email))
        if row is None:
            return 0
        locked_until = _parse_moment(row.get("locked_until"))
        if locked_until is None:
            return 0
        remaining = (locked_until - now).total_seconds()
        return int(remaining) if remaining > 0 else 0

    def record_failure(self, email: str, now: datetime) -> None:
        """Count one failed attempt; the 5th inside 15 minutes locks the email. O(1).

        Every write is a compare-and-set on the counter that was read, retried
        when another request got there first, so parallel guesses cannot
        overwrite each other's failures. Two "first failures" at once collide on
        the primary key; the loser retries as an update.

        Args:
            email: The email as typed (normalised and hashed here).
            now: The current time.
        """
        email_hash = hash_email(email)
        moment = now.isoformat()
        for _ in range(MAX_WRITE_ATTEMPTS):
            row = self._find_row(email_hash)
            if row is None:
                try:
                    run_query(self._supabase.table(TABLE).insert({
                        "email_hash": email_hash, "failed_count": 1, "first_failed_at": moment,
                        "locked_until": None, "updated_at": moment,
                    }))
                    return
                except HTTPException:
                    continue  # another request inserted it first: count on top of theirs
            read_count = int(row.get("failed_count", 0))
            first_failed_at = _parse_moment(row.get("first_failed_at"))
            if first_failed_at is None or now - first_failed_at > WINDOW:
                patch: dict = {"failed_count": 1, "first_failed_at": moment,
                               "locked_until": None, "updated_at": moment}
            else:
                patch = {"failed_count": read_count + 1, "updated_at": moment}
                if read_count + 1 >= MAX_FAILURES:
                    patch["locked_until"] = (now + LOCK_DURATION).isoformat()
            updated = run_query(
                self._supabase.table(TABLE).update(patch)
                .eq("email_hash", email_hash).eq("failed_count", read_count)
            )
            if updated:
                return

    def record_success(self, email: str) -> None:
        """Clear the failure record after a successful login. O(1).

        Args:
            email: The address that signed in.
        """
        run_query(
            self._supabase.table(TABLE).delete().eq("email_hash", hash_email(email))
        )


def get_login_guard(supabase: Client = Depends(get_supabase)) -> LoginGuard:
    """Return a ``LoginGuard`` bound to the data client. O(1).

    Args:
        supabase: The Supabase data client the guard reads and writes with.

    Returns:
        A ``LoginGuard`` over the ``auth_login_attempts`` table.
    """
    return LoginGuard(supabase)

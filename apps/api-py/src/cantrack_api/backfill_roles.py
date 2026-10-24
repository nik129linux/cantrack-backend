"""One-off: copy each account's legacy ``user_metadata.role`` into ``app_metadata``.

Before PR0 the role lived only in ``user_metadata``, which users can rewrite
themselves. The API now reads ``app_metadata.role`` only, so existing accounts
would lose their role. This copies it once, server-side, for every account that
has a valid legacy role and no server role yet. Accounts that already have a
server role are never touched, so a role flipped from the browser afterwards
cannot be promoted.

Dry run by default; pass ``--apply`` to write:

    cd apps/api-py && uv run python -m cantrack_api.backfill_roles           # report
    cd apps/api-py && uv run python -m cantrack_api.backfill_roles --apply   # write
"""

import os
import sys
from typing import Any

from dotenv import load_dotenv
from supabase import create_client

from .deps import ROLES

PAGE_SIZE = 200


def plan_backfill(users: list[Any]) -> list[tuple[str, str]]:
    """Return the ``(user_id, role)`` pairs that need a server role. O(n) in users.

    Args:
        users: Supabase ``User`` objects (anything with ``id``, ``app_metadata``
            and ``user_metadata``).

    Returns:
        One pair per account whose ``app_metadata`` has no role and whose
        legacy ``user_metadata.role`` is ``owner`` or ``walker``.
    """
    pending: list[tuple[str, str]] = []
    for user in users:
        server_role = (user.app_metadata or {}).get("role")
        legacy_role = (user.user_metadata or {}).get("role")
        if server_role is None and legacy_role in ROLES:
            pending.append((user.id, legacy_role))
    return pending


def list_all_users(admin: Any) -> list[Any]:
    """Page through every auth user with the admin API. O(n) in users."""
    users: list[Any] = []
    page = 1
    while True:
        batch = admin.list_users(page=page, per_page=PAGE_SIZE)
        users.extend(batch)
        if len(batch) < PAGE_SIZE:
            return users
        page += 1


def run(admin: Any, apply: bool) -> list[tuple[str, str]]:
    """Plan the backfill and, when ``apply`` is set, write it. O(n) in users.

    Args:
        admin: ``supabase.auth.admin`` (service-role).
        apply: False for a dry run.

    Returns:
        The ``(user_id, role)`` pairs planned (and written when ``apply``).
    """
    pending = plan_backfill(list_all_users(admin))
    if apply:
        for user_id, role in pending:
            admin.update_user_by_id(user_id, {"app_metadata": {"role": role}})
    return pending


def main(argv: list[str]) -> int:
    """CLI entrypoint: print the plan, write it only with ``--apply``."""
    load_dotenv()
    supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    apply = "--apply" in argv
    pending = run(supabase.auth.admin, apply)
    for user_id, role in pending:
        print(f"{'set' if apply else 'would set'} {user_id} -> {role}")
    print(f"{len(pending)} account(s) {'updated' if apply else 'to update (dry run, pass --apply)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

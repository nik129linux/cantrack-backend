"""An in-memory stand-in for the parts of the supabase-py client CanTrack uses.

It mimics the real query-builder shape (`table(name).select/insert/update/delete`
chained with `.eq(...)` and finished with `.execute()` returning an object with
`.data`), and it *enforces* the filters it is given. That way a service that
forgets an `owner_id` filter fails a behavioural test instead of passing a
call-shape assertion.
"""

from __future__ import annotations

import pathlib
import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from postgrest.exceptions import APIError
from storage3.exceptions import StorageApiError
from supabase_auth.errors import AuthApiError
from supabase_auth.types import Session, User

# Postgres/PostgREST normalizes every timestamptz to UTC on the way back
# (`2026-10-05T10:00:00-05:00` is stored and returned as
# `2026-10-05T15:00:00+00:00`). The fake models that for the timestamp columns
# CanTrack writes, so tests cannot pass by relying on a verbatim echo of a
# non-UTC input. Values that do not parse as ISO-8601 are left untouched:
# rejecting them is the API's job (pydantic), not the database double's.
TIMESTAMP_COLUMNS = frozenset({"requested_time", "responded_at", "created_at"})


def _normalize_utc_timestamp(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value
    return parsed.astimezone(timezone.utc).isoformat()


def _normalize_timestamps(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    return {
        key: _normalize_utc_timestamp(value) if key in TIMESTAMP_COLUMNS else value
        for key, value in payload.items()
    }


_BACKEND = pathlib.Path(__file__).resolve().parents[3]
_SCHEMA_CANDIDATES = [
    _BACKEND.parent / "cantrack-db" / "apps" / "api-py" / "schema.sql",
    _BACKEND / "cantrack-db" / "apps" / "api-py" / "schema.sql",
]


def _schema_columns() -> dict[str, set[str]]:
    """Columns per table from cantrack-db's schema.sql (create table + add column).

    The real PostgREST refuses a write to a column that does not exist
    (PGRST204). Without this, every test passes while the API writes columns
    the database does not have (it happened: owner_profiles.home).
    """
    schema = next((path for path in _SCHEMA_CANDIDATES if path.exists()), None)
    if schema is None:
        return {}
    sql = schema.read_text()
    columns: dict[str, set[str]] = {}
    for table, body in re.findall(r"create table if not exists public\.(\w+) \((.*?)\n\);", sql, re.S):
        names = set()
        for line in body.splitlines():
            match = re.match(r"\s+([a-z_][a-z0-9_]*)\s+(?!key\b)[a-z]", line)
            if match and match.group(1) not in ("primary", "unique", "check", "constraint", "foreign"):
                names.add(match.group(1))
        columns[table] = names
    for table, column in re.findall(r"alter table public\.(\w+) add column if not exists (\w+)", sql):
        columns.setdefault(table, set()).add(column)
    return columns


SCHEMA_COLUMNS = _schema_columns()

#: Natural primary keys the API relies on for conflict detection.
PRIMARY_KEYS = {"auth_login_attempts": "email_hash"}


class FakeQuery:
    def __init__(self, db: "FakeSupabase", table: str) -> None:
        self._db = db
        self._table = table
        self._op = "select"
        self._payload: Any = None
        self._filters: list[tuple[str, Any]] = []
        self._in_filters: list[tuple[str, list[Any]]] = []
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None

    def select(self, *_columns: str) -> "FakeQuery":
        self._op = "select"
        return self

    def insert(self, row: dict[str, Any]) -> "FakeQuery":
        self._op = "insert"
        self._payload = row
        return self

    def update(self, patch: dict[str, Any]) -> "FakeQuery":
        self._op = "update"
        self._payload = patch
        return self

    def delete(self) -> "FakeQuery":
        self._op = "delete"
        return self

    def eq(self, column: str, value: Any) -> "FakeQuery":
        self._filters.append((column, value))
        return self

    def in_(self, column: str, values: list[Any]) -> "FakeQuery":
        self._in_filters.append((column, list(values)))
        return self

    def order(self, column: str, desc: bool = False) -> "FakeQuery":
        self._order = (column, desc)
        return self

    def limit(self, count: int) -> "FakeQuery":
        self._limit = count
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        return all(str(row.get(col)) == str(val) for col, val in self._filters) and all(
            str(row.get(col)) in {str(v) for v in values}
            for col, values in self._in_filters
        )

    def execute(self) -> SimpleNamespace:
        self._db.queries.append((self._table, self._op, list(self._filters)))
        if self._db.fail_with is not None:
            error, self._db.fail_with = self._db.fail_with, None
            raise error
        # A concurrent writer slipping in between this request's read and its
        # write: the hook runs once, right before the first matching operation.
        hook = self._db.before_execute.pop((self._table, self._op), None)
        if hook is not None:
            hook(self._db)
        known = SCHEMA_COLUMNS.get(self._table)
        if known and self._op in ("insert", "update") and isinstance(self._payload, dict):
            unknown = sorted(set(self._payload) - known)
            if unknown:
                raise APIError({"message": f"Could not find the '{unknown[0]}' column of "
                                           f"'{self._table}' in the schema cache",
                                "code": "PGRST204", "hint": None, "details": None})
        rows = self._db.tables.setdefault(self._table, [])
        if self._op == "insert":
            key = PRIMARY_KEYS.get(self._table)
            if key and any(existing.get(key) == self._payload.get(key) for existing in rows):
                raise APIError({"message": "duplicate key value violates unique constraint",
                                "code": "23505", "hint": None, "details": None})
            row = {
                "id": str(uuid.uuid4()),
                "created_at": datetime.now(timezone.utc).isoformat(),
                **_normalize_timestamps(self._payload),
            }
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        matched = [r for r in rows if self._matches(r)]
        if self._op == "update":
            for row in matched:
                row.update(_normalize_timestamps(self._payload))
            return SimpleNamespace(data=[dict(r) for r in matched])
        if self._op == "delete":
            for row in matched:
                rows.remove(row)
            return SimpleNamespace(data=[dict(r) for r in matched])
        if self._order is not None:
            column, desc = self._order
            matched = sorted(matched, key=lambda r: r.get(column), reverse=desc)
        if self._limit is not None:
            matched = matched[: self._limit]
        return SimpleNamespace(data=[dict(r) for r in matched])


class FakeStorageBucket:
    """Double of ``storage3``'s ``SyncBucketProxy`` object API.

    Copied from the installed library (storage3/_sync/file_api.py), not from
    memory: ``upload(path, file, file_options=None)`` returns an
    ``UploadResponse``-shaped object (``.path`` / ``.full_path`` / ``.fullPath``)
    and raises ``StorageApiError(..., "duplicate", 409)`` for an existing path;
    ``create_signed_url(path, expires_in)`` returns the ``SignedUrlResponse``
    dict ``{"signedURL": ..., "signedUrl": ...}`` and raises
    ``StorageApiError(..., "not_found", 404)`` for a missing object;
    ``remove(paths)`` returns a list of dicts; ``download(path)`` returns the
    stored bytes verbatim and raises the same ``not_found``/404 shape for a
    missing object (the ai-note re-run reads the first photo back through
    it); ``get_public_url`` is recorded
    so tests can assert it is NEVER called (the bucket is private). Upload
    calls record the ``file_options`` verbatim (4th element of the call
    tuple) so tests can pin the stored ``content-type`` — which the API must
    derive from the magic bytes, never from the client's header.
    """

    def __init__(self, store: "FakeStorage", bucket: str) -> None:
        self._store = store
        self.id = bucket

    def upload(self, path: str, file: Any, file_options: Any = None) -> SimpleNamespace:
        self._store.calls.append((self.id, "upload", path, file_options))
        if self._store.fail_upload_path == path:
            raise StorageApiError("Storage unavailable", "storage_error", 500)
        if (self.id, path) in self._store.objects:
            raise StorageApiError("The resource already exists", "duplicate", 409)
        data = file if isinstance(file, bytes) else file.read()
        self._store.objects[(self.id, path)] = data
        return SimpleNamespace(path=path, full_path=f"{self.id}/{path}", fullPath=f"{self.id}/{path}")

    def create_signed_url(self, path: str, expires_in: int, options: Any = None) -> dict[str, Any]:
        self._store.calls.append((self.id, "sign", path, expires_in))
        if (self.id, path) not in self._store.objects:
            raise StorageApiError("Object not found", "not_found", 404)
        url = f"https://supabase.example/signed/{self.id}/{path}?expires={expires_in}"
        return {"signedURL": url, "signedUrl": url}

    def download(
        self, path: str, options: Any = None, query_params: Any = None
    ) -> bytes:
        # Copied from storage3/_sync/file_api.py: download(path, options=None,
        # query_params=None) -> bytes (the raw object content). The real client
        # surfaces a missing object as a StorageApiError from the request layer;
        # the not_found/404 shape matches create_signed_url above so the API
        # code can treat "object is gone" the same way for both calls.
        self._store.calls.append((self.id, "download", path))
        if (self.id, path) not in self._store.objects:
            raise StorageApiError("Object not found", "not_found", 404)
        return self._store.objects[(self.id, path)]

    def remove(self, paths: list[str]) -> list[dict[str, Any]]:
        removed: list[dict[str, Any]] = []
        for path in paths:
            self._store.calls.append((self.id, "remove", path))
            if path in self._store.fail_remove_paths:
                raise StorageApiError("remove failed", "storage_error", 500)
            self._store.objects.pop((self.id, path), None)
            removed.append({"name": path})
        return removed

    def get_public_url(self, path: str, options: Any = None) -> str:
        # The checkout bucket is private: any call here is a bug the tests see.
        self._store.calls.append((self.id, "public", path))
        return f"https://supabase.example/public/{self.id}/{path}"


class FakeStorage:
    """Double of the ``supabase.storage`` client: ``from_(bucket)`` only."""

    def __init__(self) -> None:
        self.buckets: dict[str, FakeStorageBucket] = {}
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple] = []
        self.fail_upload_path: str | None = None
        self.fail_remove_paths: set[str] = set()

    def from_(self, bucket: str) -> FakeStorageBucket:
        return self.buckets.setdefault(bucket, FakeStorageBucket(self, bucket))


class FakeAdmin:
    """Double of ``supabase.auth.admin``: the service-role user management API.

    ``update_user_by_id`` merges ``app_metadata`` (and sets a new ``password``)
    the way the real call does server-side; ``sign_out`` revokes tokens with the
    real scopes (local = this token, others = every other token of the user,
    global = all of them); ``create_user``/``list_users``/``delete_user`` back
    the demo seeding. Every call is recorded in ``calls``.
    """

    def __init__(self, auth: "FakeAuth") -> None:
        self._auth = auth
        self.calls: list[tuple[str, str, Any]] = []
        self.fail = False

    def update_user_by_id(self, uid: str, attributes: dict[str, Any]) -> SimpleNamespace:
        self.calls.append(("update", uid, attributes))
        if self.fail:
            raise AuthApiError("admin unavailable", 500, None)
        for user in self._auth.all_users():
            if user.id == uid:
                if "app_metadata" in attributes:
                    user.app_metadata = {**(user.app_metadata or {}), **attributes["app_metadata"]}
                if "password" in attributes:
                    for account in self._auth.accounts.values():
                        if account["user"].id == uid:
                            account["password"] = attributes["password"]
                return SimpleNamespace(user=user)
        raise AuthApiError("User not found", 404, None)

    def delete_user(self, uid: str) -> None:
        self.calls.append(("delete", uid, None))
        self._auth.accounts = {email: acc for email, acc in self._auth.accounts.items()
                               if acc["user"].id != uid}
        self._auth.users_by_token = {token: user for token, user in self._auth.users_by_token.items()
                                     if user.id != uid}

    def sign_out(self, jwt: str, scope: str = "global") -> None:
        self.calls.append(("sign_out", jwt, scope))
        user = self._auth.users_by_token.get(jwt)
        if user is None:
            raise AuthApiError("invalid JWT", 401, None)
        for token, owner in list(self._auth.users_by_token.items()):
            if owner.id != user.id:
                continue
            if scope == "global" or (scope == "local" and token == jwt) or (scope == "others" and token != jwt):
                del self._auth.users_by_token[token]

    def create_user(self, attributes: dict[str, Any]) -> SimpleNamespace:
        self.calls.append(("create", attributes.get("email"), attributes))
        email = attributes["email"].lower()
        if email in self._auth.accounts:
            raise AuthApiError("A user with this email address has already been registered", 422, "email_exists")
        user = User(
            id=str(uuid.uuid4()), email=email,
            app_metadata=dict(attributes.get("app_metadata") or {}),
            user_metadata=dict(attributes.get("user_metadata") or {}),
            aud="authenticated", created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        )
        self._auth.accounts[email] = {"password": attributes.get("password"), "user": user,
                                      "confirmed": bool(attributes.get("email_confirm"))}
        return SimpleNamespace(user=user)

    def list_users(self, page: int = 1, per_page: int = 50) -> list[Any]:
        users = [account["user"] for account in self._auth.accounts.values()]
        start = (page - 1) * per_page
        return users[start:start + per_page]


class FakeAuth:
    """Double of ``supabase.auth``: token lookup plus email/password accounts.

    ``add_account`` registers a login; ``sign_in_with_password`` answers like
    GoTrue (the password is checked BEFORE the confirmation, so
    ``email_not_confirmed`` is only reachable with the right password).
    """

    def __init__(self) -> None:
        self.users_by_token: dict[str, Any] = {}
        self.accounts: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, Any] = {}
        self.resent: list[dict[str, Any]] = []
        self.fail_resend = False
        self.admin = FakeAdmin(self)

    def all_users(self) -> list[Any]:
        seen = {user.id: user for user in self.users_by_token.values()}
        for account in self.accounts.values():
            seen.setdefault(account["user"].id, account["user"])
        return list(seen.values())

    def add_account(self, email: str, password: str, user: Any, confirmed: bool = True) -> None:
        self.accounts[email.lower()] = {"password": password, "user": user, "confirmed": confirmed}

    def _new_session(self, user: Any) -> Session:
        access, refresh = f"at-{uuid.uuid4().hex}", f"rt-{uuid.uuid4().hex}"
        self.users_by_token[access] = user
        self.refresh_tokens[refresh] = user
        return Session(access_token=access, refresh_token=refresh, expires_in=3600,
                       token_type="bearer", user=user)

    def get_user(self, token: str) -> SimpleNamespace:
        user = self.users_by_token.get(token)
        if user is None:
            raise AuthApiError("invalid JWT", 401, None)
        return SimpleNamespace(user=user)

    def sign_in_with_password(self, credentials: dict[str, Any]) -> SimpleNamespace:
        account = self.accounts.get(credentials.get("email", "").lower())
        if account is None or account["password"] != credentials.get("password"):
            raise AuthApiError("Invalid login credentials", 400, "invalid_credentials")
        if not account["confirmed"]:
            raise AuthApiError("Email not confirmed", 400, "email_not_confirmed")
        session = self._new_session(account["user"])
        return SimpleNamespace(session=session, user=account["user"])

    def refresh_session(self, refresh_token: str | None = None) -> SimpleNamespace:
        user = self.refresh_tokens.pop(refresh_token or "", None)
        if user is None:
            raise AuthApiError("Invalid Refresh Token: Refresh Token Not Found", 400, "refresh_token_not_found")
        session = self._new_session(user)
        return SimpleNamespace(session=session, user=user)

    def resend(self, credentials: dict[str, Any]) -> SimpleNamespace:
        self.resent.append(credentials)
        if self.fail_resend:
            raise AuthApiError("Email rate limit exceeded", 429, "over_email_send_rate_limit")
        return SimpleNamespace(user=None, session=None)


class FakeSupabase:
    def __init__(self) -> None:
        self.auth = FakeAuth()
        self.storage = FakeStorage()
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.queries: list[tuple[str, str, list[tuple[str, Any]]]] = []
        self.fail_with: Exception | None = None
        #: (table, op) -> callable(db), run once before that operation executes.
        self.before_execute: dict[tuple[str, str], Any] = {}

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    def add_user(self, token: str, user: Any) -> None:
        self.auth.users_by_token[token] = user

    def add_account(self, email: str, password: str, user: Any, confirmed: bool = True) -> None:
        """Register an email/password login (see ``FakeAuth.sign_in_with_password``)."""
        self.auth.add_account(email, password, user, confirmed)

    @property
    def accounts(self) -> dict[str, dict[str, Any]]:
        return self.auth.accounts

    def seed(self, table: str, **row: Any) -> dict[str, Any]:
        saved = {"id": str(uuid.uuid4()), **_normalize_timestamps(row)}
        self.tables.setdefault(table, []).append(saved)
        return saved

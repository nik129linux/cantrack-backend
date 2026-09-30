"""An in-memory stand-in for the parts of the supabase-py client CanTrack uses.

It mimics the real query-builder shape (`table(name).select/insert/update/delete`
chained with `.eq(...)` and finished with `.execute()` returning an object with
`.data`), and it *enforces* the filters it is given. That way a service that
forgets an `owner_id` filter fails a behavioural test instead of passing a
call-shape assertion.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

from supabase_auth.errors import AuthApiError


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
        rows = self._db.tables.setdefault(self._table, [])
        if self._op == "insert":
            row = {
                "id": str(uuid.uuid4()),
                "created_at": datetime.now(timezone.utc).isoformat(),
                **self._payload,
            }
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        matched = [r for r in rows if self._matches(r)]
        if self._op == "update":
            for row in matched:
                row.update(self._payload)
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


class FakeAuth:
    def __init__(self) -> None:
        self.users_by_token: dict[str, Any] = {}

    def get_user(self, token: str) -> SimpleNamespace:
        user = self.users_by_token.get(token)
        if user is None:
            raise AuthApiError("invalid JWT", 401, None)
        return SimpleNamespace(user=user)


class FakeSupabase:
    def __init__(self) -> None:
        self.auth = FakeAuth()
        self.tables: dict[str, list[dict[str, Any]]] = {}
        self.queries: list[tuple[str, str, list[tuple[str, Any]]]] = []
        self.fail_with: Exception | None = None

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)

    def add_user(self, token: str, user: Any) -> None:
        self.auth.users_by_token[token] = user

    def seed(self, table: str, **row: Any) -> dict[str, Any]:
        saved = {"id": str(uuid.uuid4()), **row}
        self.tables.setdefault(table, []).append(saved)
        return saved

"""PR0: every table has RLS enabled and NO policy (deny-all for anon/authenticated).

The schema lives in the sibling repo cantrack-db (split repos under ~/cantrack-split),
not inside this repo, so we resolve it as ../cantrack-db relative to the backend root.
"""

import pathlib
import re

import pytest

# tests/py/api/test_rls_policies.py -> parents[3] == cantrack-backend, parents[4] == cantrack-split.
_BACKEND = pathlib.Path(__file__).resolve().parents[3]
_CANDIDATES = [
    _BACKEND.parent / "cantrack-db" / "apps" / "api-py" / "schema.sql",  # split repos (real layout)
    _BACKEND / "cantrack-db" / "apps" / "api-py" / "schema.sql",          # monorepo fallback
]
SCHEMA = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

# CI checks out this repo alone; the schema lives in cantrack-db.
pytestmark = pytest.mark.skipif(not SCHEMA.exists(), reason="cantrack-db not checked out next to this repo")

RLS_TABLES = [
    "dogs", "routes", "checkins", "walker_profiles", "walk_requests",
    "checkouts", "ai_usage", "walk_offers", "walker_positions",
    "dog_pack_links", "walk_graph_edges", "walk_route_stops",
    "walker_cancel_buckets", "walk_ratings", "walk_timeline", "walk_gps_points",
]


class TestRlsPolicies:
    def test_schema_enables_rls_for_all_tables(self):
        sql = SCHEMA.read_text()
        for table in RLS_TABLES:
            assert re.search(rf"alter table public\.{table} enable row level security", sql), f"missing RLS on {table}"

    def test_no_table_has_any_policy(self):
        # PR0: deny-all. The `for all` policies let a user write straight to
        # Supabase REST with the anon key + their own token, skipping the API.
        sql = SCHEMA.read_text()
        assert not re.search(r"create policy", sql, re.IGNORECASE)

    def test_every_old_policy_is_dropped(self):
        sql = SCHEMA.read_text()
        for table in RLS_TABLES:
            assert re.search(rf'drop policy if exists "[a-z_]+" on public\.{table};', sql), f"no drop for {table}"

    def test_deny_all_is_explained(self):
        sql = SCHEMA.read_text().lower()
        assert "deny-all" in sql
        assert "service-role" in sql

    def test_schema_still_has_16_rls_tables(self):
        sql = SCHEMA.read_text()
        assert len(re.findall(r"enable row level security", sql)) == 16

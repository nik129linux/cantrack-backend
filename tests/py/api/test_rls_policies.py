"""Task 3: schema.sql has a policy for every RLS-enabled table.

The schema lives in the sibling repo cantrack-db (split repos under ~/cantrack-split),
not inside this repo, so we resolve it as ../cantrack-db relative to the backend root.
"""

import pathlib
import re

# tests/py/api/test_rls_policies.py -> parents[3] == cantrack-backend, parents[4] == cantrack-split.
_BACKEND = pathlib.Path(__file__).resolve().parents[3]
_CANDIDATES = [
    _BACKEND.parent / "cantrack-db" / "apps" / "api-py" / "schema.sql",  # split repos (real layout)
    _BACKEND / "cantrack-db" / "apps" / "api-py" / "schema.sql",          # monorepo fallback
]
SCHEMA = next((p for p in _CANDIDATES if p.exists()), _CANDIDATES[0])

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

    def test_schema_has_a_policy_per_table(self):
        sql = SCHEMA.read_text()
        for table in RLS_TABLES:
            assert re.search(rf"create policy.*on public\.{table}", sql, re.IGNORECASE), f"missing policy on {table}"

    def test_policies_are_commented_as_defence_in_depth(self):
        sql = SCHEMA.read_text().lower()
        assert "defence in depth" in sql or "defense in depth" in sql
        assert "service-role" in sql or "service_role" in sql or "service role" in sql

    def test_schema_still_has_16_rls_tables(self):
        sql = SCHEMA.read_text()
        assert len(re.findall(r"enable row level security", sql)) == 16

"""Demo accounts and data for the professor's review (python -m cantrack_api.seed_demo).

The plan is deterministic (same people, dogs and walks every run); only the
passwords are random. Every row goes through the schema-checked Supabase
double, so a column the real database lacks fails here, not on review day.
"""

import os
import stat

import pytest

from cantrack_api import seed_demo
from cantrack_api.geo import haversine_km
from cantrack_api.seed_demo import DEMO_DOMAIN, apply_plan, build_plan, reset_demo, write_credentials

from .conftest import FIXED_NOW
from .fake_supabase import FakeSupabase

PASTO = (1.2136, -77.2811)


@pytest.fixture
def db():
    return FakeSupabase()


@pytest.fixture
def seeded(db):
    credentials = apply_plan(db, build_plan(), FIXED_NOW)
    return db, credentials


class TestPlan:
    def test_people(self):
        plan = build_plan()
        roles = [person["role"] for person in plan["people"]]
        assert roles.count("owner") >= 2 and roles.count("walker") >= 3
        emails = [person["email"] for person in plan["people"]]
        assert len(set(emails)) == len(emails)
        assert all(email.endswith("@" + DEMO_DOMAIN) for email in emails)

    def test_the_demo_domain_is_reserved_for_examples(self):
        # RFC 2606: nobody can own it, so no real person ever gets these emails.
        assert DEMO_DOMAIN == "example.com"

    def test_the_plan_is_deterministic(self):
        assert build_plan() == build_plan()


class TestApply:
    def test_accounts_are_confirmed_with_a_server_role(self, seeded):
        db, credentials = seeded
        created = [call for call in db.auth.admin.calls if call[0] == "create"]
        assert len(created) == len(build_plan()["people"])
        for _, email, attributes in created:
            assert attributes["email_confirm"] is True
            assert attributes["app_metadata"]["role"] in ("owner", "walker")

    def test_credentials_are_returned_once_with_strong_unique_passwords(self, seeded):
        _, credentials = seeded
        passwords = [entry["password"] for entry in credentials]
        assert len(set(passwords)) == len(passwords)
        assert all(len(password) >= 16 for password in passwords)
        assert {entry["role"] for entry in credentials} == {"owner", "walker"}

    def test_the_accounts_can_log_in(self, seeded):
        db, credentials = seeded
        entry = credentials[0]
        session = db.auth.sign_in_with_password({"email": entry["email"], "password": entry["password"]}).session
        assert session.access_token

    def test_walkers_have_profiles_and_fresh_positions_in_pasto(self, seeded):
        db, credentials = seeded
        walker_ids = {row["walker_id"] for row in db.tables["walker_profiles"]}
        assert len(walker_ids) >= 3
        for position in db.tables["walker_positions"]:
            assert position["walker_id"] in walker_ids
            assert haversine_km(position["lat"], position["lng"], *PASTO) <= 3.0
            assert position["updated_at"] == FIXED_NOW.isoformat()

    def test_owners_have_dogs_and_profiles(self, seeded):
        db, _ = seeded
        owner_ids = {row["owner_id"] for row in db.tables["owner_profiles"]}
        assert len(owner_ids) >= 2
        assert {dog["owner_id"] for dog in db.tables["dogs"]} <= owner_ids
        assert len(db.tables["dogs"]) >= 3

    def test_walks_cover_every_screen(self, seeded):
        db, _ = seeded
        statuses = {walk["status"] for walk in db.tables["walk_requests"]}
        assert {"open", "assigned", "done"} <= statuses
        dog_ids = {dog["id"] for dog in db.tables["dogs"]}
        assert all(walk["dog_id"] in dog_ids for walk in db.tables["walk_requests"])

    def test_the_open_walk_already_has_offers_from_two_walkers(self, seeded):
        db, _ = seeded
        open_walk = next(walk for walk in db.tables["walk_requests"] if walk["status"] == "open")
        offers = [offer for offer in db.tables["walk_offers"] if offer["walk_request_id"] == open_walk["id"]]
        assert len({offer["walker_id"] for offer in offers}) >= 2

    def test_the_finished_walk_has_a_track_a_rating_and_a_report(self, seeded):
        db, _ = seeded
        done = next(walk for walk in db.tables["walk_requests"] if walk["status"] == "done")
        assert len([p for p in db.tables["walk_gps_points"] if p["walk_request_id"] == done["id"]]) >= 3
        assert [r for r in db.tables["walk_ratings"] if r["walk_request_id"] == done["id"]]
        assert done["report_status"] == "ok" and done["report_distance_km"] > 0
        kinds = [e["kind"] for e in db.tables["walk_timeline"] if e["walk_request_id"] == done["id"]]
        assert "check_in" in kinds and "check_out" in kinds

    def test_existing_demo_accounts_are_refused_without_reset(self, seeded):
        db, _ = seeded
        with pytest.raises(RuntimeError, match="reset"):
            apply_plan(db, build_plan(), FIXED_NOW)


class TestResetAndCredentials:
    def test_reset_deletes_only_demo_accounts(self, seeded):
        db, _ = seeded
        db.auth.admin.create_user({"email": "real.person@gmail.com", "password": "x" * 20, "email_confirm": True})
        removed = reset_demo(db)
        assert removed == len(build_plan()["people"])
        remaining = [user.email for user in db.auth.admin.list_users(page=1, per_page=200)]
        assert remaining == ["real.person@gmail.com"]

    def test_credentials_file_is_private(self, tmp_path, seeded):
        _, credentials = seeded
        path = tmp_path / "demo-credentials.local.txt"
        write_credentials(path, credentials)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600
        text = path.read_text()
        assert all(entry["email"] in text and entry["password"] in text for entry in credentials)

    def test_the_default_credentials_file_is_git_ignored(self):
        backend = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        ignore = open(os.path.join(backend, ".gitignore")).read()
        assert seed_demo.DEFAULT_CREDENTIALS_FILE in ignore or "*.local.txt" in ignore

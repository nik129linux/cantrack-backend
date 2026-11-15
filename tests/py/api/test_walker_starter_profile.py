"""A walker who never saved Perfil still gets a walker_profiles row before
offering, so the owner's pick (walk_requests.walker_id -> walker_profiles)
cannot fail with a 500. Found live: an owner who switched to walker offered,
and picking that offer broke."""

from datetime import datetime, timezone

from supabase_auth.types import User

from cantrack_api.walker_access import STARTER_PRICE_COP, ensure_walker_profile

from .conftest import make_user


def profiles(fake):
    return fake.tables.get("walker_profiles", [])


def test_a_walker_without_profile_gets_a_starter_one(fake):
    walker = User(
        id="w-new", email="nuevo@example.com", app_metadata={"role": "walker"},
        user_metadata={"full_name": "Ana Paseos"}, aud="authenticated",
        created_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
    )
    ensure_walker_profile(fake, walker)
    [row] = [p for p in profiles(fake) if p["walker_id"] == "w-new"]
    assert row["display_name"] == "Ana Paseos"
    assert row["price_per_walk"] == STARTER_PRICE_COP


def test_without_a_name_the_email_local_part_is_used(fake):
    ensure_walker_profile(fake, make_user("w-x", "solo.correo@example.com", "walker") .model_copy(update={"user_metadata": {}}))
    [row] = [p for p in profiles(fake) if p["walker_id"] == "w-x"]
    assert row["display_name"] == "solo.correo"


def test_an_existing_profile_is_left_alone(fake):
    fake.seed("walker_profiles", walker_id="w-old", display_name="Mía", price_per_walk=15000)
    ensure_walker_profile(fake, make_user("w-old", "old@example.com", "walker"))
    rows = [p for p in profiles(fake) if p["walker_id"] == "w-old"]
    assert len(rows) == 1
    assert rows[0]["display_name"] == "Mía"
    assert rows[0]["price_per_walk"] == 15000

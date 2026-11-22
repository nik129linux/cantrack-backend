"""FR-22: aggregate completed walks and sent checkouts for one owner."""

from datetime import datetime, timezone

from .conftest import bearer

OWNER_PATH = "/owners/owner-a/stats"


def seed_walk(fake, *, owner_id, walker_id, requested_time, status="done"):
    return fake.seed(
        "walk_requests",
        owner_id=owner_id,
        walker_id=walker_id,
        dog_id="dog-1",
        requested_time=requested_time,
        status=status,
        duration_hours=1.0,
    )


def seed_checkout(fake, *, owner_id, walker_id, sent_at, status="sent"):
    return fake.seed(
        "checkouts",
        owner_id=owner_id,
        walker_id=walker_id,
        request_id=f"request-{owner_id}-{walker_id}-{sent_at}",
        dog_id="dog-1",
        sent_at=sent_at if status == "sent" else None,
        status=status,
        photo_paths=[],
    )


def expected_stats(*, october_walks, september_walks, checkouts, walkers):
    return {
        "walksByMonth": [
            {"month": "2026-05", "count": 0},
            {"month": "2026-06", "count": 0},
            {"month": "2026-07", "count": 0},
            {"month": "2026-08", "count": 0},
            {"month": "2026-09", "count": september_walks},
            {"month": "2026-10", "count": october_walks},
        ],
        "checkoutsReceived": checkouts,
        "distinctWalkers": walkers,
    }


def seed_owner_story(fake):
    seed_walk(
        fake, owner_id="owner-a", walker_id="walker-w",
        requested_time="2026-10-05T10:00:00+00:00",
    )
    seed_walk(
        fake, owner_id="owner-a", walker_id="walker-w2",
        requested_time="2026-10-06T10:00:00+00:00",
    )
    seed_walk(
        fake, owner_id="owner-a", walker_id="walker-w",
        requested_time="2026-09-28T10:00:00+00:00",
    )
    seed_walk(
        fake, owner_id="owner-a", walker_id="walker-w",
        requested_time="2026-04-30T10:00:00+00:00",
    )
    seed_walk(
        fake,
        owner_id="owner-a",
        walker_id="walker-w",
        requested_time="2026-10-07T10:00:00+00:00",
        status="assigned",
    )
    seed_walk(
        fake,
        owner_id="owner-a",
        walker_id="walker-w2",
        requested_time="2026-10-08T10:00:00+00:00",
        status="cancelled",
    )
    seed_walk(
        fake, owner_id="owner-b", walker_id="walker-w2",
        requested_time="2026-10-09T10:00:00+00:00",
    )
    seed_checkout(
        fake, owner_id="owner-a", walker_id="walker-w",
        sent_at="2026-10-05T15:00:00+00:00",
    )
    seed_checkout(
        fake, owner_id="owner-a", walker_id="walker-w",
        sent_at="2026-09-28T15:00:00+00:00",
    )
    seed_checkout(
        fake, owner_id="owner-a", walker_id="walker-w2",
        sent_at="2026-09-29T15:00:00+00:00",
    )
    seed_checkout(
        fake, owner_id="owner-a", walker_id="walker-w3", sent_at=None,
        status="draft",
    )
    seed_checkout(
        fake, owner_id="owner-b", walker_id="walker-w2",
        sent_at="2026-10-09T15:00:00+00:00",
    )


def test_owner_stats_returns_exact_completed_walk_and_checkout_totals(
    fake_client, fake, fake_clock
):
    fake_clock.now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    seed_owner_story(fake)

    response = fake_client.get(OWNER_PATH, headers=bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == expected_stats(
        october_walks=2,
        september_walks=1,
        checkouts=3,
        walkers=2,
    )


def test_owner_stats_are_isolated_between_owners(fake_client, fake, fake_clock):
    fake_clock.now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    seed_owner_story(fake)

    owner_a = fake_client.get("/owners/owner-a/stats", headers=bearer("token-a"))
    owner_b = fake_client.get("/owners/owner-b/stats", headers=bearer("token-b"))

    assert owner_a.status_code == owner_b.status_code == 200
    assert owner_a.json() == expected_stats(
        october_walks=2,
        september_walks=1,
        checkouts=3,
        walkers=2,
    )
    assert owner_b.json() == expected_stats(
        october_walks=1,
        september_walks=0,
        checkouts=1,
        walkers=1,
    )

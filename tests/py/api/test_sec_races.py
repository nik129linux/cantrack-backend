"""PR0 (c): races between two requests that read the same row and both write.

The fake runs a ``before_execute`` hook right before the write, standing in for
a second request that commits in between this request's read and its write
(e.g. the owner cancels while the walker's accept is in flight, or two
browser tabs fire the same button). Every write must be a compare-and-set on
the state it read, so the loser gets 409 instead of silently overwriting.
"""

from cantrack_api.routers.checkouts import AI_LIMIT

from .conftest import bearer
from .test_checkouts import PNG, accepted_request, create_dog, post_checkout, save_profile

T10 = "2026-10-05T10:00:00+00:00"
T11 = "2026-10-05T11:00:00+00:00"

REQ = "6f1c2a7e-0d1b-4c3a-9e8f-1a2b3c4d5e6f"

STALE = {"detail": "This changed while you were acting on it. Reload and try again."}


def set_status(table, row_id, new_status, **extra):
    def hook(db):
        for row in db.tables[table]:
            if row["id"] == row_id:
                row.update({"status": new_status, **extra})
    return hook


def seed_pending(fake):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    return fake.seed(
        "walk_requests", id=REQ, owner_id="owner-a", walker_id="walker-w", dog_id="dog-1",
        status="pending", requested_time=T10, pickup_lat=6.2, pickup_lng=-75.5, price_cop=25000,
        created_at="2026-10-01T00:00:00+00:00",
    )


def seed_assigned(fake):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    return fake.seed(
        "walk_requests", id="walk-1", owner_id="owner-a", walker_id="walker-w", dog_id="dog-1",
        status="assigned", requested_time=T10, window_end=T11,
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000, created_at="2026-10-01T00:00:00+00:00",
    )


class TestLegacyRequestCompareAndSet:
    def test_accept_loses_to_a_cancel_that_landed_first(self, fake_client, fake):
        seed_pending(fake)
        fake.before_execute[("walk_requests", "update")] = set_status("walk_requests", REQ, "cancelled")
        res = fake_client.post(f"/requests/{REQ}/accept", headers=bearer("token-w"))
        assert res.status_code == 409
        assert res.json() == STALE
        assert fake.tables["walk_requests"][0]["status"] == "cancelled"

    def test_decline_loses_to_a_cancel_that_landed_first(self, fake_client, fake):
        seed_pending(fake)
        fake.before_execute[("walk_requests", "update")] = set_status("walk_requests", REQ, "cancelled")
        res = fake_client.post(f"/requests/{REQ}/decline", headers=bearer("token-w"))
        assert res.status_code == 409
        assert fake.tables["walk_requests"][0]["status"] == "cancelled"

    def test_cancel_loses_to_a_decline_that_landed_first(self, fake_client, fake):
        seed_pending(fake)
        fake.before_execute[("walk_requests", "update")] = set_status("walk_requests", REQ, "declined")
        res = fake_client.post(f"/requests/{REQ}/cancel", headers=bearer("token-a"))
        assert res.status_code == 409
        assert fake.tables["walk_requests"][0]["status"] == "declined"


class TestCheckoutCompareAndSet:
    def _draft(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        return post_checkout(fake_client, rid).json()

    def test_note_edit_cannot_land_after_the_checkout_was_sent(self, fake_client, fake):
        draft = self._draft(fake_client)
        fake.before_execute[("checkouts", "update")] = set_status(
            "checkouts", draft["id"], "sent", sent_at="2026-10-01T00:00:00+00:00")
        res = fake_client.patch(f"/checkouts/{draft['id']}", json={"note": "edited late"},
                                headers=bearer("token-w"))
        assert res.status_code == 409
        assert res.json() == STALE
        assert fake.tables["checkouts"][0]["note"] != "edited late"

    def test_double_send_keeps_the_first_timestamp(self, fake_client, fake):
        draft = self._draft(fake_client)
        first = "2026-09-30T23:00:00+00:00"
        fake.before_execute[("checkouts", "update")] = set_status(
            "checkouts", draft["id"], "sent", sent_at=first)
        res = fake_client.post(f"/checkouts/{draft['id']}/send", headers=bearer("token-w"))
        assert res.status_code == 409
        assert fake.tables["checkouts"][0]["sent_at"] == first


class TestCancelTokensAreSpentAtomically:
    """Exploit: fire walker-cancel on several walks at once; each reads 3 tokens, all write 2."""

    def test_a_concurrent_spend_is_not_lost(self, fake_client, fake):
        seed_assigned(fake)
        fake.seed("walker_cancel_buckets", id="bucket-w", walker_id="walker-w", remaining=3,
                  period_start="2026-10-01T00:00:00+00:00")

        def concurrent_spend(db):
            db.tables["walker_cancel_buckets"][0]["remaining"] = 2

        fake.before_execute[("walker_cancel_buckets", "update")] = concurrent_spend
        res = fake_client.post("/bidding/requests/walk-1/walker-cancel", headers=bearer("token-w"))
        assert res.status_code == 200
        assert fake.tables["walker_cancel_buckets"][0]["remaining"] == 1

    def test_last_token_taken_concurrently_means_no_cancel(self, fake_client, fake):
        seed_assigned(fake)
        fake.seed("walker_cancel_buckets", id="bucket-w", walker_id="walker-w", remaining=1,
                  period_start="2026-10-01T00:00:00+00:00")

        def concurrent_spend(db):
            db.tables["walker_cancel_buckets"][0]["remaining"] = 0

        fake.before_execute[("walker_cancel_buckets", "update")] = concurrent_spend
        res = fake_client.post("/bidding/requests/walk-1/walker-cancel", headers=bearer("token-w"))
        assert res.status_code == 403
        assert fake.tables["walk_requests"][0]["status"] == "assigned"
        assert fake.tables["walker_cancel_buckets"][0]["remaining"] == 0


class TestReopenedWalkForgetsTheWalker:
    """A reopened walk kept the old walker_id, so the cancelled walker could still check in."""

    def test_walker_cancel_clears_walker_id(self, fake_client, fake):
        seed_assigned(fake)
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=3,
                  period_start="2026-10-01T00:00:00+00:00")
        res = fake_client.post("/bidding/requests/walk-1/walker-cancel", headers=bearer("token-w"))
        assert res.json()["status"] == "open"
        assert res.json()["walkerId"] is None
        assert fake.tables["walk_requests"][0]["walker_id"] is None

    def test_cancelled_walker_cannot_check_in_afterwards(self, fake_client, fake):
        seed_assigned(fake)
        fake.seed("walker_cancel_buckets", walker_id="walker-w", remaining=3,
                  period_start="2026-10-01T00:00:00+00:00")
        fake_client.post("/bidding/requests/walk-1/walker-cancel", headers=bearer("token-w"))
        res = fake_client.post("/bidding/requests/walk-1/check-in", headers=bearer("token-w"))
        assert res.status_code == 403


class TestAiQuotaIsReservedBeforeTheCall:
    """Exploit: fire 20 parallel ai-note re-runs at 59/60; all pass the check, all get answered."""

    def _draft(self, fake_client):
        save_profile(fake_client)
        dog = create_dog(fake_client)
        rid = accepted_request(fake_client, dog["id"])
        return post_checkout(fake_client, rid).json()

    def _usage(self, fake):
        return [row for row in fake.tables.get("ai_usage", []) if row["walker_id"] == "walker-w"]

    def test_usage_is_recorded_before_the_model_is_called(self, fake_client, fake, vision):
        draft = self._draft(fake_client)
        before = len(self._usage(fake))
        seen = []
        original = vision.check

        def spying_check(data):
            seen.append(len(self._usage(fake)))
            return original(data)

        vision.check = spying_check
        res = fake_client.post(f"/checkouts/{draft['id']}/ai-note", headers=bearer("token-w"))
        assert res.status_code == 200
        assert seen == [before + 1]
        assert len(self._usage(fake)) == before + 1

    def test_a_concurrent_reservation_at_the_limit_gets_429(self, fake_client, fake, vision):
        draft = self._draft(fake_client)
        for _ in range(AI_LIMIT - 1 - len(self._usage(fake))):
            fake.seed("ai_usage", walker_id="walker-w", used_at="2026-10-01T00:00:00+00:00")
        assert len(self._usage(fake)) == AI_LIMIT - 1

        def concurrent_reservation(db):
            db.seed("ai_usage", walker_id="walker-w", used_at="2026-10-01T00:00:00+00:00")

        fake.before_execute[("ai_usage", "insert")] = concurrent_reservation
        calls_before = len(vision.calls)
        res = fake_client.post(f"/checkouts/{draft['id']}/ai-note", headers=bearer("token-w"))
        assert res.status_code == 429
        assert len(vision.calls) == calls_before
        assert len(self._usage(fake)) == AI_LIMIT  # our reservation was given back

    def test_an_unavailable_model_gives_the_reservation_back(self, fake_client, fake, vision):
        draft = self._draft(fake_client)
        before = len(self._usage(fake))

        def broken(_data):
            raise RuntimeError("ollama down")

        vision.check = broken
        res = fake_client.post(f"/checkouts/{draft['id']}/ai-note", headers=bearer("token-w"))
        assert res.status_code == 200
        assert res.json()["aiStatus"] == "unavailable"
        assert len(self._usage(fake)) == before

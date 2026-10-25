"""PR3 Mis paseos: the walk report, built ONCE at check-out (FR-21).

Distance and duration are computed exactly (haversine over the GPS track,
check-in to check-out). The optional AI text is generated once, stored, and
never blocks the check-out; reading the report later costs nothing.
"""

from datetime import timedelta

import pytest

from .conftest import FIXED_NOW, bearer

T10 = "2026-10-05T10:00:00+00:00"
EPOCH_T10 = 1791194400.0


@pytest.fixture
def walking(fake, fake_clock):
    fake.seed("dogs", id="dog-luna", owner_id="owner-a", name="Luna")
    fake.seed("walk_requests", id="w1", owner_id="owner-a", walker_id="walker-w", dog_id="dog-luna",
              status="in_walk", requested_time=T10, window_end=T10, deadline_at="2026-10-01T00:10:00+00:00",
              duration_hours=1.0, pickup_lat=1.2136, pickup_lng=-77.2811, price_cop=20000,
              created_at="2026-09-30T00:00:00+00:00")
    fake.seed("walk_timeline", walk_request_id="w1", kind="check_in", at=T10, payload={})
    # Three points ~1.11 km apart each along the meridian: 2.22 km in total.
    for index, lat in enumerate((1.20, 1.21, 1.22)):
        fake.seed("walk_gps_points", walk_request_id="w1", at_epoch=EPOCH_T10 + 600 * (index + 1),
                  lat=lat, lng=-77.28)
    fake_clock.now = FIXED_NOW + timedelta(days=4, hours=10, minutes=47)
    return fake


def check_out(client):
    return client.post("/bidding/requests/w1/check-out", headers=bearer("token-w"))


def stored(fake):
    return next(row for row in fake.tables["walk_requests"] if row["id"] == "w1")


def test_check_out_stores_exact_stats_and_the_ai_text(fake_client, walking, text_model):
    text_model.result = {"text": "Luna caminó 2.2 km a buen ritmo."}
    res = check_out(fake_client)
    assert res.status_code == 200
    assert res.json()["status"] == "done"
    row = stored(walking)
    assert row["report_status"] == "ok"
    assert row["report_text"] == "Luna caminó 2.2 km a buen ritmo."
    assert row["report_distance_km"] == 2.22
    assert row["report_duration_min"] == 47
    assert len(text_model.calls) == 1


def test_the_prompt_has_the_walk_facts_and_no_personal_data(fake_client, walking, text_model):
    text_model.result = {"text": "ok"}
    check_out(fake_client)
    system, prompt = text_model.calls[0]
    assert "Luna" in prompt and "2.22" in prompt and "47" in prompt
    for secret in ("owner-a@example.com", "walker-w@example.com", "1.2136", "-77.2811"):
        assert secret not in system + prompt  # no emails, no home coordinates


def test_model_failure_never_blocks_the_check_out(fake_client, walking, text_model):
    def boom(system, prompt):
        raise RuntimeError("ollama exploded")

    text_model.complete_json = boom
    res = check_out(fake_client)
    assert res.status_code == 200
    row = stored(walking)
    assert row["status"] == "done"
    assert row["report_status"] == "unavailable"
    assert row["report_distance_km"] == 2.22


def test_unusable_answer_is_unavailable(fake_client, walking, text_model):
    text_model.result = {"summary": 42}
    check_out(fake_client)
    assert stored(walking)["report_status"] == "unavailable"


def test_long_text_is_cut_at_600_characters(fake_client, walking, text_model):
    text_model.result = {"text": "a" * 900}
    check_out(fake_client)
    assert len(stored(walking)["report_text"]) == 600


def test_disabled_model_still_stores_the_stats(fake_client, walking, text_model):
    text_model.enabled = False
    check_out(fake_client)
    row = stored(walking)
    assert row["report_status"] == "disabled"
    assert row["report_distance_km"] == 2.22
    assert text_model.calls == []


def test_env_flag_turns_the_ai_text_off(fake_client, walking, text_model, monkeypatch):
    monkeypatch.setenv("AI_WALK_REPORT", "off")
    text_model.result = {"text": "should not be asked"}
    check_out(fake_client)
    assert stored(walking)["report_status"] == "disabled"
    assert text_model.calls == []


def test_no_gps_means_zero_distance(fake_client, walking, text_model):
    walking.tables["walk_gps_points"] = []
    text_model.result = {"text": "ok"}
    check_out(fake_client)
    assert stored(walking)["report_distance_km"] == 0.0


def test_failed_check_out_does_not_call_the_model(fake_client, walking, text_model):
    stored(walking)["status"] = "assigned"
    assert check_out(fake_client).status_code == 409
    assert text_model.calls == []


def test_the_detail_serves_the_stored_report(fake_client, walking, text_model):
    text_model.result = {"text": "Buen paseo."}
    check_out(fake_client)
    report = fake_client.get("/bidding/requests/w1", headers=bearer("token-a")).json()["report"]
    assert report == {"status": "ok", "text": "Buen paseo.", "distanceKm": 2.22, "durationMin": 47}
    fake_client.get("/bidding/requests/w1", headers=bearer("token-a"))
    assert len(text_model.calls) == 1  # reading it again costs nothing

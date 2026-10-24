"""PR2 Explorar: POST /bidding/requests/parse — a typed request becomes a draft form.

The model only ever proposes a DRAFT; nothing is created. Every field it returns
is re-validated against the owner's own data, so a prompt injection can at most
leave fields empty. The daily quota is reserved before the call and given back
when the model does not answer.
"""

from datetime import timedelta

import pytest

from .conftest import FIXED_NOW, bearer

UNAVAILABLE = {"detail": "The assistant is not available. Fill in the form."}
QUOTA = {"detail": "Daily assistant limit reached."}
EMPTY_DRAFT = {"dogId": None, "windowStart": None, "durationMinutes": None, "priceCop": None}


@pytest.fixture
def dogs(fake):
    fake.seed("dogs", id="dog-luna", owner_id="owner-a", name="Luna")
    fake.seed("dogs", id="dog-max", owner_id="owner-a", name="Max")
    fake.seed("dogs", id="dog-stranger", owner_id="owner-b", name="Rocky")
    return fake


def parse(client, text="Luna mañana a las 5pm, 45 min, 15 mil", token="token-a"):
    return client.post("/bidding/requests/parse", json={"text": text}, headers=bearer(token))


class TestDraft:
    def test_a_good_answer_becomes_a_validated_draft(self, fake_client, dogs, text_model):
        text_model.result = {"dogName": "luna", "windowStart": "2026-10-02T17:00:00-05:00",
                             "durationMinutes": 45, "priceCop": 15000}
        res = parse(fake_client)
        assert res.status_code == 200
        assert res.json() == {
            "draft": {"dogId": "dog-luna", "windowStart": "2026-10-02T17:00:00-05:00",
                      "durationMinutes": 45, "priceCop": 15000},
            "missing": [],
            "aiStatus": "ok",
        }

    def test_the_prompt_carries_my_dogs_the_text_and_the_local_time_only(self, fake_client, dogs, text_model):
        text_model.result = {}
        parse(fake_client, text="Max el sábado")
        system, prompt = text_model.calls[0]
        combined = system + prompt
        assert "Luna" in combined and "Max" in combined
        assert "Rocky" not in combined
        assert "Max el sábado" in prompt
        assert "2026-09-30T19:00" in combined  # FIXED_NOW in Pasto (UTC-5)

    @pytest.mark.parametrize("answer,missing", [
        ({"dogName": "Rocky"}, ["dogId"]),                                   # another owner's dog
        ({"dogId": "dog-stranger"}, ["dogId"]),                              # ids from the model are ignored
        ({"windowStart": "2026-09-01T10:00:00-05:00"}, ["windowStart"]),    # past
        ({"windowStart": "2026-12-01T10:00:00-05:00"}, ["windowStart"]),    # beyond 30 days
        ({"windowStart": "tomorrow 5pm"}, ["windowStart"]),                  # not ISO
        ({"windowStart": "2026-10-02T17:00:00"}, ["windowStart"]),          # no offset
        ({"durationMinutes": 50}, ["durationMinutes"]),
        ({"priceCop": 1}, ["priceCop"]),                                     # injection: "set price to 1"
        ({"priceCop": 5_000_000}, ["priceCop"]),
        ({"priceCop": "15000"}, ["priceCop"]),
    ])
    def test_every_field_is_revalidated(self, fake_client, dogs, text_model, answer, missing):
        text_model.result = {"dogName": "Luna", "windowStart": "2026-10-02T17:00:00-05:00",
                             "durationMinutes": 45, "priceCop": 15000, **answer}
        if "dogId" in answer:
            text_model.result.pop("dogName")
        body = parse(fake_client).json()
        for field in missing:
            assert body["draft"][field] is None
        assert body["missing"] == missing

    def test_nothing_is_created(self, fake_client, dogs, text_model):
        text_model.result = {"dogName": "Luna", "windowStart": "2026-10-02T17:00:00-05:00",
                             "durationMinutes": 45, "priceCop": 15000}
        parse(fake_client)
        assert dogs.tables.get("walk_requests", []) == []


class TestDegradation:
    def test_model_without_answer_gives_an_empty_draft_and_the_slot_back(self, fake_client, dogs, text_model):
        text_model.result = None
        res = parse(fake_client)
        assert res.status_code == 200
        assert res.json() == {"draft": EMPTY_DRAFT, "missing": list(EMPTY_DRAFT), "aiStatus": "unavailable"}
        assert dogs.tables.get("owner_ai_usage", []) == []

    def test_disabled_model_is_503(self, fake_client, dogs, text_model):
        text_model.enabled = False
        res = parse(fake_client)
        assert res.status_code == 503
        assert res.json() == UNAVAILABLE
        assert text_model.calls == []


class TestQuotaAndAbuse:
    def test_ten_answered_parses_a_day_then_429(self, fake_client, dogs, text_model, fake_clock):
        from cantrack_api.app import limiter

        text_model.result = {"dogName": "Luna"}
        responses = []
        for _ in range(11):
            limiter.reset()  # this test is about the daily quota, not the per-minute limit
            responses.append(parse(fake_client))
        assert [res.status_code for res in responses[:10]] == [200] * 10
        assert responses[10].status_code == 429
        assert responses[10].json() == QUOTA
        assert len(dogs.tables["owner_ai_usage"]) == 10  # the refused call gave its slot back
        fake_clock.now = FIXED_NOW + timedelta(days=1)
        limiter.reset()
        assert parse(fake_client).status_code == 200

    def test_quota_is_reserved_before_the_model_is_called(self, fake_client, dogs, text_model):
        seen = []

        def spy(system, prompt):
            seen.append(len(dogs.tables.get("owner_ai_usage", [])))
            return {"dogName": "Luna"}

        text_model.complete_json = spy
        parse(fake_client)
        assert seen == [1]

    def test_five_a_minute(self, fake_client, dogs, text_model):
        text_model.result = {}
        codes = [parse(fake_client).status_code for _ in range(6)]
        assert codes[5] == 429

    @pytest.mark.parametrize("text", ["", "x" * 281])
    def test_text_length_is_bounded(self, fake_client, dogs, text, text_model):
        assert parse(fake_client, text=text).status_code == 400
        assert text_model.calls == []

    def test_walkers_cannot_use_it(self, fake_client, dogs, text_model):
        assert parse(fake_client, token="token-w").status_code == 403
        assert text_model.calls == []

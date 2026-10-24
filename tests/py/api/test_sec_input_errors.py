"""PR0 (d): hostile input gets a clean 4xx, and no error leaks internals.

Exploits closed here: overflow and NaN values that crash into a 500 echoing
Postgres text, unbounded strings/lists (storage and CPU abuse), huge bodies,
account enumeration through auth error messages, and validation errors that
echo the submitted input back.
"""

import inspect
import json
import pathlib
from types import SimpleNamespace

import pytest
from postgrest.exceptions import APIError
from supabase_auth.errors import AuthApiError

from cantrack_api import db as db_module
from cantrack_api.routers import auth as auth_router

from .conftest import bearer

ROUTERS_DIR = pathlib.Path(inspect.getfile(auth_router)).parent

T10 = "2026-10-05T10:00:00+00:00"
BIDDING = {"dogId": "dog-1", "windowStart": T10, "durationHours": 1, "priceCop": 30000,
           "pickupLat": 6.2, "pickupLng": -75.5}
PROFILE = {"displayName": "Nico Walks", "bio": None, "serviceArea": "Laureles", "pricePerWalk": 20000}

TOO_LARGE = {"detail": "The request is too large."}
NOT_FOUND = {"detail": "We couldn't find what you're looking for."}
OUT_OF_RANGE = {"detail": "A number in the request is out of range."}
INTERNAL = "Something went wrong on our side."
WINDOW_TOO_FAR = {"detail": "windowStart must be within the next 30 days."}
BAD_CREDENTIALS = {"detail": "Invalid login credentials."}
SIGNUP_FAILED = {"detail": "We couldn't create an account with those details."}
RESET_SENT = {"message": "Password reset email sent."}


def seed_open(fake):
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    fake.seed(
        "walk_requests", id="walk-1", owner_id="owner-a", walker_id=None, dog_id="dog-1",
        status="open", requested_time=T10, window_end="2026-10-05T11:00:00+00:00",
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000, created_at="2026-10-01T00:00:00+00:00",
    )


class TestNumbersAreBounded:
    @pytest.mark.parametrize("price", [1_000_001, 2**31, 2**63])
    def test_walk_price_above_one_million_cop_is_400(self, fake_client, fake, price):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        res = fake_client.post("/bidding/requests", json={**BIDDING, "priceCop": price}, headers=bearer("token-a"))
        assert res.status_code == 400

    def test_one_million_cop_is_accepted(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        res = fake_client.post("/bidding/requests", json={**BIDDING, "priceCop": 1_000_000}, headers=bearer("token-a"))
        assert res.status_code == 201

    @pytest.mark.parametrize("body", [
        {"priceCop": 1_000_001, "etaMin": 10},
        {"priceCop": 20000, "etaMin": 241},
    ])
    def test_offer_bounds(self, fake_client, fake, body):
        seed_open(fake)
        res = fake_client.post("/bidding/requests/walk-1/offers", json=body, headers=bearer("token-w"))
        assert res.status_code == 400

    @pytest.mark.parametrize("raw", ["Infinity", "-Infinity", "NaN"])
    def test_offer_eta_rejects_non_finite_json(self, fake_client, fake, raw):
        seed_open(fake)
        res = fake_client.post(
            "/bidding/requests/walk-1/offers",
            content=f'{{"priceCop": 20000, "etaMin": {raw}}}',
            headers={**bearer("token-w"), "Content-Type": "application/json"},
        )
        assert res.status_code == 400

    def test_walker_price_per_walk_is_bounded(self, fake_client):
        res = fake_client.put("/walker-profile", json={**PROFILE, "pricePerWalk": 1_000_001},
                              headers=bearer("token-w"))
        assert res.status_code == 400

    def test_window_more_than_30_days_ahead_is_400(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        res = fake_client.post("/bidding/requests", json={**BIDDING, "windowStart": "2026-11-01T00:00:01+00:00"},
                               headers=bearer("token-a"))
        assert res.status_code == 400
        assert res.json() == WINDOW_TOO_FAR

    def test_year_9999_window_is_400_not_500(self, fake_client, fake):
        fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
        res = fake_client.post("/bidding/requests", json={**BIDDING, "windowStart": "9999-12-31T23:59:59+00:00"},
                               headers=bearer("token-a"))
        assert res.status_code == 400


class TestTextAndListsAreBounded:
    @pytest.mark.parametrize("field,limit", [("name", 80), ("breed", 80), ("notes", 1000)])
    def test_dog_text_limits(self, fake_client, field, limit):
        base = {"name": "Rex"}
        assert fake_client.post("/dogs", json={**base, field: "x" * limit}, headers=bearer("token-a")).status_code == 201
        assert fake_client.post("/dogs", json={**base, field: "x" * (limit + 1)}, headers=bearer("token-a")).status_code == 400

    @pytest.mark.parametrize("field,limit", [("displayName", 80), ("bio", 500), ("serviceArea", 120)])
    def test_walker_profile_text_limits(self, fake_client, field, limit):
        assert fake_client.put("/walker-profile", json={**PROFILE, field: "x" * limit},
                               headers=bearer("token-w")).status_code == 200
        assert fake_client.put("/walker-profile", json={**PROFILE, field: "x" * (limit + 1)},
                               headers=bearer("token-w")).status_code == 400

    def test_checkout_note_text_is_still_200_chars(self, fake_client):
        # Existing rule, listed so the caps table in the docs stays complete.
        res = fake_client.patch("/checkouts/c-1", json={"note": "x" * 201}, headers=bearer("token-w"))
        assert res.status_code in (400, 404)

    def test_route_with_more_than_50_stops_is_400(self, fake_client):
        stops = [{"dogId": f"d{i}", "pickupTime": "2026-10-05T08:00:00Z"} for i in range(51)]
        assert fake_client.post("/routes", json={"stops": stops}, headers=bearer("token-w")).status_code == 400

    def test_path_graph_with_more_than_500_edges_is_400(self, fake_client):
        edges = [{"from": f"n{i}", "to": f"n{i + 1}", "weight": 1} for i in range(501)]
        res = fake_client.post("/routes/path", json={"edges": edges, "start": "n0", "end": "n501"},
                               headers=bearer("token-w"))
        assert res.status_code == 400

    @pytest.mark.parametrize("length,expected", [(100, 200), (101, 400)])
    def test_path_node_names_are_bounded(self, fake_client, length, expected):
        name = "n" * length
        body = {"edges": [{"from": name, "to": "b", "weight": 1}], "start": name, "end": "b"}
        assert fake_client.post("/routes/path", json=body, headers=bearer("token-w")).status_code == expected

    def test_email_longer_than_254_is_400(self, fake_client):
        res = fake_client.post("/auth/login", json={"email": "a" * 250 + "@x.co", "password": "x"})
        assert res.status_code == 400


class TestBodySize:
    def test_json_body_over_1_mib_is_413(self, fake_client):
        res = fake_client.post("/dogs", content=b'{"name": "' + b"x" * (1024 * 1024 + 10) + b'"}',
                               headers={**bearer("token-a"), "Content-Type": "application/json"})
        assert res.status_code == 413
        assert res.json() == TOO_LARGE

    def test_multipart_body_over_26_mib_is_413(self, fake_client):
        boundary = "b0undary"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photos\"; filename=\"a.png\"\r\n"
                f"Content-Type: image/png\r\n\r\n").encode() + b"\x00" * (26 * 1024 * 1024 + 1) + f"\r\n--{boundary}--\r\n".encode()
        res = fake_client.post("/dogs/dog-1/photos", content=body,
                               headers={**bearer("token-a"), "Content-Type": f"multipart/form-data; boundary={boundary}"})
        assert res.status_code == 413

    def test_lying_content_length_cannot_sneak_a_big_body(self, fake_client):
        res = fake_client.post("/dogs", content=b'{"name": "' + b"x" * (1024 * 1024 + 10) + b'"}',
                               headers={**bearer("token-a"), "Content-Type": "application/json",
                                        "Transfer-Encoding": "chunked"})
        assert res.status_code in (400, 413)


class TestDatabaseErrorsDoNotLeak:
    def _boom(self, code, message):
        return APIError({"message": message, "code": code, "hint": "check pg_hba", "details": "table secret_x"})

    def test_unknown_database_error_is_opaque_500_with_error_id(self, fake_client, fake):
        fake.fail_with = self._boom("XX000", 'relation "public.secret_table" does not exist')
        res = fake_client.get("/dogs", headers=bearer("token-a"))
        assert res.status_code == 500
        body = res.json()
        assert body["detail"] == INTERNAL
        assert len(body["errorId"]) >= 8
        assert "secret" not in res.text and "relation" not in res.text and "pg_hba" not in res.text

    def test_invalid_uuid_from_postgres_is_404(self, fake_client, fake):
        fake.fail_with = self._boom("22P02", 'invalid input syntax for type uuid: "walk-1"')
        res = fake_client.get("/dogs", headers=bearer("token-a"))
        assert res.status_code == 404
        assert res.json() == NOT_FOUND

    def test_numeric_overflow_from_postgres_is_400(self, fake_client, fake):
        fake.fail_with = self._boom("22003", "value out of range for type integer")
        res = fake_client.get("/dogs", headers=bearer("token-a"))
        assert res.status_code == 400
        assert res.json() == OUT_OF_RANGE

    def test_unhandled_exception_is_the_same_opaque_500(self, fake_client, fake):
        fake.fail_with = RuntimeError("SUPABASE_SERVICE_ROLE_KEY=abc leaked")
        local = fake_client.__class__(fake_client.app, raise_server_exceptions=False)
        res = local.get("/dogs", headers=bearer("token-a"))
        assert res.status_code == 500
        assert res.json()["detail"] == INTERNAL
        assert "SUPABASE" not in res.text

    def test_no_router_returns_raw_exception_text(self):
        offenders = []
        for path in [*ROUTERS_DIR.glob("*.py"), pathlib.Path(inspect.getfile(db_module))]:
            source = path.read_text()
            for needle in ("detail=exc.message", "detail=str(exc)"):
                if needle in source:
                    offenders.append(f"{path.name}: {needle}")
        assert offenders == []


class TestAuthDoesNotRevealAccounts:
    def test_unconfirmed_email_looks_like_bad_credentials(self, client, supabase):
        supabase.auth.sign_in_with_password.side_effect = AuthApiError("Email not confirmed", 400, None)
        res = client.post("/auth/login", json={"email": "a@a.com", "password": "x"})
        assert res.status_code == 401
        assert res.json() == BAD_CREDENTIALS

    def test_existing_email_on_signup_gets_a_generic_message(self, client, supabase):
        supabase.auth.sign_up.side_effect = AuthApiError("User already registered", 422, None)
        res = client.post("/auth/signup", json={"email": "a@a.com", "password": "pw-123456", "role": "owner"})
        assert res.status_code == 400
        assert res.json() == SIGNUP_FAILED

    def test_reset_answers_the_same_whether_or_not_the_email_exists(self, client, supabase):
        supabase.auth.reset_password_for_email.side_effect = AuthApiError("User not found", 404, None)
        res = client.post("/auth/reset-password", json={"email": "ghost@a.com"})
        assert res.status_code == 200
        assert res.json() == RESET_SENT


class TestValidationErrorsDoNotEchoInput:
    def test_invalid_field_value_is_not_reflected(self, fake_client):
        res = fake_client.post("/auth/login", json={"email": "not-an-email-SECRET123", "password": "x"})
        assert res.status_code == 400
        assert "SECRET123" not in res.text
        for error in res.json()["detail"]:
            assert set(error) <= {"loc", "msg", "type"}

    def test_frontend_shape_is_kept(self, fake_client):
        res = fake_client.post("/dogs", json={"name": ""}, headers=bearer("token-a"))
        error = res.json()["detail"][0]
        assert error["loc"] == ["body", "name"]
        assert isinstance(error["msg"], str) and error["msg"]

    def test_malformed_json_is_still_400(self, fake_client):
        res = fake_client.post("/dogs", content=b"{not json",
                               headers={**bearer("token-a"), "Content-Type": "application/json"})
        assert res.status_code == 400
        json.dumps(res.json())

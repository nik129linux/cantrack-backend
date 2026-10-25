"""PR0 (a): the role is server-assigned (``app_metadata``), never client-claimed.

Exploit being closed: in the browser console, a logged-in owner runs
``supabase.auth.updateUser({data: {role: "walker"}})``. Supabase lets any user
rewrite their own ``user_metadata``, so if the API reads the role from there the
owner becomes a walker, bids on their own walk, picks themselves and rates
themselves 5 stars. The API must read ``app_metadata.role`` only, which only the
service-role key can write.
"""

import inspect
import pathlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from supabase_auth.errors import AuthApiError

from cantrack_api import deps
from cantrack_api.routers import auth as auth_router

from .conftest import bearer, make_spoofed_user, make_user

ROUTERS_DIR = pathlib.Path(inspect.getfile(auth_router)).parent

T10 = "2026-10-05T10:00:00+00:00"
PROFILE = {"displayName": "Nico Walks", "bio": None, "serviceArea": "Laureles", "pricePerWalk": 20000}
BIDDING_BODY = {"dogId": "dog-1", "windowStart": T10, "durationHours": 1, "priceCop": 30000,
                "pickupLat": 6.2, "pickupLng": -75.5}

ROLE_SET = {"role": "owner"}
ROLE_ALREADY_SET = {"detail": "This account already has a role."}


@pytest.fixture
def spoofers(fake):
    # A real owner claiming to be a walker, a real walker claiming to be an
    # owner, and a legacy account with no server role claiming to be a walker.
    fake.add_user("token-owner-as-walker", make_spoofed_user("owner-s", "s@example.com", "owner", "walker"))
    fake.add_user("token-walker-as-owner", make_spoofed_user("walker-s", "ws@example.com", "walker", "owner"))
    fake.add_user("token-nobody-as-walker", make_spoofed_user("nobody", "n@example.com", None, "walker"))
    fake.add_user("token-nobody", make_spoofed_user("nobody-2", "n2@example.com", None, None))
    fake.seed("dogs", id="dog-1", owner_id="owner-a", name="Firulais")
    fake.seed("dogs", id="dog-s", owner_id="owner-s", name="Spoofy")
    fake.seed(
        "walk_requests", id="walk-1", owner_id="owner-a", walker_id=None, dog_id="dog-1",
        status="open", requested_time=T10, window_end="2026-10-05T11:00:00+00:00",
        deadline_at="2026-10-01T00:10:00+00:00", duration_hours=1.0,
        pickup_lat=6.2, pickup_lng=-75.5, price_cop=30000, created_at="2026-10-01T00:00:00+00:00",
    )
    return fake


WALKER_ONLY = [
    ("put", "/walker-profile", PROFILE),
    ("get", "/clients", None),
    ("post", "/plans/suggest", {"date": "2026-10-05"}),
    ("get", "/bidding/requests/open", None),
    ("post", "/bidding/requests/walk-1/offers", {"priceCop": 25000, "etaMin": 10}),
    ("get", "/ai-quota", None),
]

OWNER_ONLY = [
    ("post", "/bidding/requests", BIDDING_BODY),
    ("get", "/bidding/requests/walk-1/offers", None),
]


def call(client, method, path, body, token):
    kwargs = {"headers": bearer(token)}
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(path, **kwargs)


class TestClaimedRoleIsIgnored:
    @pytest.mark.parametrize("method,path,body", WALKER_ONLY)
    def test_owner_claiming_walker_is_treated_as_owner(self, fake_client, spoofers, method, path, body):
        genuine = call(fake_client, method, path, body, "token-a")
        spoofed = call(fake_client, method, path, body, "token-owner-as-walker")
        assert genuine.status_code >= 400, "precondition: the endpoint refuses a real owner"
        assert spoofed.status_code == genuine.status_code
        assert spoofed.json() == genuine.json()

    @pytest.mark.parametrize("method,path,body", WALKER_ONLY)
    def test_account_without_server_role_gets_no_walker_powers(self, fake_client, spoofers, method, path, body):
        res = call(fake_client, method, path, body, "token-nobody-as-walker")
        assert res.status_code >= 400

    def test_walker_claiming_owner_cannot_post_a_walk(self, fake_client, spoofers):
        res = call(fake_client, "post", "/bidding/requests", {**BIDDING_BODY, "dogId": "dog-s"},
                   "token-walker-as-owner")
        assert res.status_code == 400
        assert not [q for q in spoofers.queries if q[:2] == ("walk_requests", "insert")]

    @pytest.mark.parametrize("method,path,body", OWNER_ONLY)
    def test_walker_claiming_owner_is_treated_as_walker(self, fake_client, spoofers, method, path, body):
        genuine = call(fake_client, method, path, body, "token-w")
        spoofed = call(fake_client, method, path, body, "token-walker-as-owner")
        assert genuine.status_code >= 400
        assert spoofed.status_code == genuine.status_code
        assert spoofed.json() == genuine.json()

    def test_no_router_reads_user_metadata(self):
        offenders = [
            path.name for path in ROUTERS_DIR.glob("*.py") if "user_metadata" in path.read_text()
        ]
        assert offenders == [], f"routers still read the client-writable role: {offenders}"

    def test_current_role_reads_app_metadata_only(self):
        assert deps.current_role(make_user("u", "u@example.com", "walker")) == "walker"
        assert deps.current_role(make_spoofed_user("u", "u@example.com", "owner", "walker")) == "owner"
        assert deps.current_role(make_spoofed_user("u", "u@example.com", None, "walker")) is None

    def test_current_role_rejects_unknown_values(self):
        assert deps.current_role(make_spoofed_user("u", "u@example.com", "admin", "walker")) is None


class TestSignupAssignsTheRoleServerSide:
    def _signed_up(self, supabase, user_id="new-user"):
        user = MagicMock()
        user.id = user_id
        user.model_dump.return_value = {"id": user_id}
        supabase.auth.sign_up.return_value = SimpleNamespace(user=user, session=None)

    def test_signup_writes_app_metadata_through_the_admin_api(self, client, supabase):
        self._signed_up(supabase)
        res = client.post("/auth/signup", json={"email": "o@example.com", "password": "my dog runs fast", "role": "owner"})
        assert res.status_code == 201
        supabase.auth.admin.update_user_by_id.assert_called_once_with(
            "new-user", {"app_metadata": {"role": "owner"}}
        )

    def test_signup_rolls_back_the_account_when_the_role_cannot_be_stored(self, client, supabase):
        self._signed_up(supabase)
        supabase.auth.admin.update_user_by_id.side_effect = AuthApiError("down", 500, None)
        res = client.post("/auth/signup", json={"email": "o@example.com", "password": "my dog runs fast", "role": "owner"})
        assert res.status_code == 503
        assert res.json() == {"detail": "We couldn't finish creating your account. Please try again."}
        supabase.auth.admin.delete_user.assert_called_once_with("new-user")


class TestOneShotRoleForOAuthAccounts:
    def test_account_without_role_can_pick_one(self, fake_client, spoofers):
        res = fake_client.post("/auth/role", json={"role": "owner"}, headers=bearer("token-nobody"))
        assert res.status_code == 200
        assert res.json() == ROLE_SET
        assert ("update", "nobody-2", {"app_metadata": {"role": "owner"}}) in spoofers.auth.admin.calls

    def test_the_picked_role_is_then_honoured(self, fake_client, spoofers):
        fake_client.post("/auth/role", json={"role": "owner"}, headers=bearer("token-nobody"))
        spoofers.seed("dogs", id="dog-n", owner_id="nobody-2", name="Nube")
        res = fake_client.post("/bidding/requests", json={**BIDDING_BODY, "dogId": "dog-n"},
                               headers=bearer("token-nobody"))
        assert res.status_code == 201

    def test_role_cannot_be_changed_once_set(self, fake_client, spoofers):
        res = fake_client.post("/auth/role", json={"role": "walker"}, headers=bearer("token-a"))
        assert res.status_code == 409
        assert res.json() == ROLE_ALREADY_SET
        assert spoofers.auth.admin.calls == []

    def test_legacy_claim_does_not_count_as_a_role(self, fake_client, spoofers):
        # user_metadata says walker, app_metadata is empty: the account may still pick once.
        res = fake_client.post("/auth/role", json={"role": "owner"}, headers=bearer("token-nobody-as-walker"))
        assert res.status_code == 200

    @pytest.mark.parametrize("role", ["admin", "", "OWNER", None, 1])
    def test_only_owner_or_walker(self, fake_client, spoofers, role):
        res = fake_client.post("/auth/role", json={"role": role}, headers=bearer("token-nobody"))
        assert res.status_code == 400
        assert spoofers.auth.admin.calls == []

    def test_requires_a_token(self, fake_client):
        assert fake_client.post("/auth/role", json={"role": "owner"}).status_code == 401

    def test_admin_failure_is_503_not_500(self, fake_client, spoofers):
        spoofers.auth.admin.fail = True
        res = fake_client.post("/auth/role", json={"role": "owner"}, headers=bearer("token-nobody"))
        assert res.status_code == 503

"""FR-01/02/03: sign up (walker or owner), log in, request a password reset."""

from types import SimpleNamespace

import pytest
from supabase_auth.errors import AuthApiError
from supabase_auth.types import AuthResponse

from .conftest import make_session, make_user

PASSWORD = "correct horse battery staple"


def signup_body(**overrides):
    body = {"email": "walker@example.com", "password": PASSWORD, "role": "walker"}
    body.update(overrides)
    return body


def auth_error(message, status=400):
    return AuthApiError(message, status, None)


class TestSignup:
    @pytest.mark.parametrize("role", ["walker", "owner"])
    def test_creates_account_for_each_role(self, client, supabase, role):
        user = make_user(email=f"{role}@example.com", role=role)
        supabase.auth.sign_up.return_value = AuthResponse(user=user, session=None)

        res = client.post(
            "/auth/signup", json=signup_body(email=f"{role}@example.com", role=role)
        )

        assert res.status_code == 201
        assert res.json()["user"]["email"] == f"{role}@example.com"
        assert res.json()["session"] is None
        supabase.auth.sign_up.assert_called_once_with(
            {
                "email": f"{role}@example.com",
                "password": PASSWORD,
                "options": {"data": {"role": role}},
            }
        )

    @pytest.mark.parametrize(
        "override",
        [
            {"role": "admin"},
            {"role": ""},
            {"email": "not-an-email"},
            {"password": ""},
        ],
    )
    def test_invalid_field_is_400_and_never_reaches_supabase(
        self, client, supabase, override
    ):
        res = client.post("/auth/signup", json=signup_body(**override))
        assert res.status_code == 400
        supabase.auth.sign_up.assert_not_called()

    @pytest.mark.parametrize("missing", ["email", "password", "role"])
    def test_missing_field_is_400(self, client, supabase, missing):
        body = signup_body()
        del body[missing]
        res = client.post("/auth/signup", json=body)
        assert res.status_code == 400
        supabase.auth.sign_up.assert_not_called()

    def test_non_json_body_is_400(self, client):
        res = client.post(
            "/auth/signup", content="nope", headers={"content-type": "application/json"}
        )
        assert res.status_code == 400

    def test_supabase_error_becomes_400_with_its_message(self, client, supabase):
        supabase.auth.sign_up.side_effect = auth_error("User already registered")
        res = client.post("/auth/signup", json=signup_body())
        assert res.status_code == 400
        assert res.json() == {"detail": "User already registered"}


class TestLogin:
    def test_correct_credentials_return_the_session(self, client, supabase):
        supabase.auth.sign_in_with_password.return_value = SimpleNamespace(
            session=make_session("token-abc"), user=make_user()
        )
        res = client.post(
            "/auth/login", json={"email": "walker@example.com", "password": PASSWORD}
        )
        assert res.status_code == 200
        assert res.json()["access_token"] == "token-abc"
        assert res.json()["refresh_token"] == "refresh-abc"
        supabase.auth.sign_in_with_password.assert_called_once_with(
            {"email": "walker@example.com", "password": PASSWORD}
        )

    def test_wrong_credentials_are_401(self, client, supabase):
        supabase.auth.sign_in_with_password.side_effect = auth_error(
            "Invalid login credentials"
        )
        res = client.post(
            "/auth/login", json={"email": "walker@example.com", "password": "wrong"}
        )
        assert res.status_code == 401
        assert res.json() == {"detail": "Invalid login credentials"}

    def test_no_session_in_response_is_401(self, client, supabase):
        supabase.auth.sign_in_with_password.return_value = SimpleNamespace(
            session=None, user=None
        )
        res = client.post(
            "/auth/login", json={"email": "walker@example.com", "password": "x"}
        )
        assert res.status_code == 401

    @pytest.mark.parametrize(
        "body", [{}, {"email": "walker@example.com"}, {"password": "x"},
                 {"email": "bad", "password": "x"}, {"email": "a@example.com", "password": ""}]
    )
    def test_invalid_body_is_400(self, client, supabase, body):
        res = client.post("/auth/login", json=body)
        assert res.status_code == 400
        supabase.auth.sign_in_with_password.assert_not_called()


class TestResetPassword:
    def test_sends_reset_email_with_default_redirect(self, client, supabase, monkeypatch):
        monkeypatch.delenv("AUTH_RESET_REDIRECT_URL", raising=False)
        res = client.post("/auth/reset-password", json={"email": "walker@example.com"})
        assert res.status_code == 200
        assert res.json() == {"message": "Password reset email sent."}
        supabase.auth.reset_password_for_email.assert_called_once_with(
            "walker@example.com",
            {"redirect_to": "http://localhost:5173/reset-password"},
        )

    def test_redirect_url_comes_from_env(self, client, supabase, monkeypatch):
        monkeypatch.setenv("AUTH_RESET_REDIRECT_URL", "https://cantrack.app/reset")
        client.post("/auth/reset-password", json={"email": "walker@example.com"})
        supabase.auth.reset_password_for_email.assert_called_once_with(
            "walker@example.com", {"redirect_to": "https://cantrack.app/reset"}
        )

    @pytest.mark.parametrize("body", [{}, {"email": "nope"}])
    def test_invalid_body_is_400(self, client, supabase, body):
        res = client.post("/auth/reset-password", json=body)
        assert res.status_code == 400
        supabase.auth.reset_password_for_email.assert_not_called()

    def test_supabase_error_becomes_400(self, client, supabase):
        supabase.auth.reset_password_for_email.side_effect = auth_error("rate limited")
        res = client.post("/auth/reset-password", json={"email": "walker@example.com"})
        assert res.status_code == 400
        assert res.json() == {"detail": "rate limited"}


class TestAuthGuard:
    def test_valid_bearer_token_yields_the_user(self, client, supabase):
        supabase.auth.get_user.return_value = SimpleNamespace(
            user=make_user("user-9", "owner@example.com")
        )
        res = client.get("/_whoami", headers={"Authorization": "Bearer good-token"})
        assert res.status_code == 200
        assert res.json() == {"id": "user-9", "email": "owner@example.com"}
        supabase.auth.get_user.assert_called_once_with("good-token")

    def test_scheme_is_case_insensitive(self, client, supabase):
        supabase.auth.get_user.return_value = SimpleNamespace(user=make_user())
        res = client.get("/_whoami", headers={"Authorization": "bearer t"})
        assert res.status_code == 200

    @pytest.mark.parametrize(
        "headers",
        [{}, {"Authorization": "Bearer"}, {"Authorization": "Bearer "},
         {"Authorization": "Basic abc"}, {"Authorization": "token-only"}],
    )
    def test_missing_or_malformed_header_is_401_without_calling_supabase(
        self, client, supabase, headers
    ):
        res = client.get("/_whoami", headers=headers)
        assert res.status_code == 401
        supabase.auth.get_user.assert_not_called()

    def test_rejected_token_is_401(self, client, supabase):
        supabase.auth.get_user.side_effect = auth_error("invalid JWT", 401)
        res = client.get("/_whoami", headers={"Authorization": "Bearer bad"})
        assert res.status_code == 401

    def test_token_with_no_user_is_401(self, client, supabase):
        supabase.auth.get_user.return_value = None
        res = client.get("/_whoami", headers={"Authorization": "Bearer bad"})
        assert res.status_code == 401


class TestAuthNeverTouchesTheSharedDataClient:
    """supabase-py swaps a client's REST Authorization header to the user's JWT on sign-in. If the
    cached service-role data client served auth calls, every later table query would run as that
    user and row level security would block it (found against the real project). Auth routes must
    use their own throwaway client (`get_auth_client`) and never `get_supabase`."""

    class Explosive:
        def __getattr__(self, name):
            raise AssertionError(f"the shared data client was used by an auth route ({name})")

    @pytest.fixture
    def isolated(self, app, supabase):
        from cantrack_api.deps import get_supabase

        app.dependency_overrides[get_supabase] = lambda: self.Explosive()
        return supabase

    def test_login_signup_and_reset_use_only_the_auth_client(self, client, isolated):
        isolated.auth.sign_in_with_password.return_value = SimpleNamespace(
            session=make_session("t"), user=make_user()
        )
        isolated.auth.sign_up.return_value = AuthResponse(user=make_user(), session=None)
        assert client.post(
            "/auth/login", json={"email": "walker@example.com", "password": PASSWORD}
        ).status_code == 200
        assert client.post("/auth/signup", json=signup_body()).status_code == 201
        assert client.post(
            "/auth/reset-password", json={"email": "walker@example.com"}
        ).status_code == 200

    def test_get_auth_client_returns_a_new_client_each_time_and_get_supabase_a_shared_one(
        self, monkeypatch
    ):
        from cantrack_api import deps

        monkeypatch.setenv("SUPABASE_URL", "http://localhost:54321")
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "x" * 40)
        deps.get_supabase.cache_clear()
        assert deps.get_auth_client() is not deps.get_auth_client()
        assert deps.get_supabase() is deps.get_supabase()
        deps.get_supabase.cache_clear()

"""Accounts and sessions: password policy, email verification, login lockout,
refresh, change password, sign out (one device or all).

Lockout is per email (hashed), not per IP: the same 5 failures lock a real and
a non-existent account alike, so the answer never says which emails exist.
"""

import hashlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from .conftest import FIXED_NOW, bearer, make_user, reset_all_rate_limits

GOOD = "correct horse battery staple"
LOCKED = {"detail": "Too many failed attempts. Try again in 15 minutes."}
BAD_CREDENTIALS = {"detail": "Invalid login credentials."}
NOT_CONFIRMED = {"detail": "Confirm your email first. We can send the link again."}
RESENT = {"message": "If the account exists and is not verified, we sent a new link."}
BAD_SESSION = {"detail": "Invalid or expired session."}
WRONG_CURRENT = {"detail": "Current password is incorrect."}
SAME_PASSWORD = {"detail": "Choose a password different from the current one."}
CHANGED = {"message": "Password changed. Other sessions were signed out."}


@pytest.fixture
def accounts(fake):
    owner = make_user("owner-a", "owner-a@example.com", "owner")
    fake.add_account("owner-a@example.com", GOOD, owner)
    fake.add_account("late@example.com", GOOD, make_user("late", "late@example.com", "owner"), confirmed=False)
    return fake


def login(client, email="owner-a@example.com", password=GOOD):
    reset_all_rate_limits()  # these tests are about the lockout, not the 5/min limit
    return client.post("/auth/login", json={"email": email, "password": password})


class TestPasswordPolicy:
    def signup(self, client, password, email="new@example.com"):
        return client.post("/auth/signup", json={"email": email, "password": password, "role": "owner"})

    @pytest.mark.parametrize("password", [
        "short1!",                    # under 10
        "password123", "12345678910", "qwertyuiop", "cantrack2026",  # common
        "newnewnew-2026",             # contains the email's local part
        "x" * 129,                    # over 128
    ])
    def test_weak_passwords_are_refused(self, client, supabase, password):
        res = self.signup(client, password)
        assert res.status_code == 400
        supabase.auth.sign_up.assert_not_called()

    @pytest.mark.parametrize("password", [GOOD, "Pasto-Volcan-2026", "ñandú corre rápido"])
    def test_long_passphrases_are_fine_without_composition_rules(self, client, supabase, password):
        user = MagicMock(id="u-1")
        user.model_dump.return_value = {"id": "u-1"}
        supabase.auth.sign_up.return_value = SimpleNamespace(user=user, session=None)
        assert self.signup(client, password).status_code == 201

    def test_verification_link_returns_to_the_web_app(self, client, supabase, monkeypatch):
        monkeypatch.setenv("AUTH_EMAIL_REDIRECT_URL", "https://cantrack.vercel.app/verificado")
        user = MagicMock(id="u-1")
        user.model_dump.return_value = {"id": "u-1"}
        supabase.auth.sign_up.return_value = SimpleNamespace(user=user, session=None)
        self.signup(client, GOOD)
        options = supabase.auth.sign_up.call_args[0][0]["options"]
        assert options["email_redirect_to"] == "https://cantrack.vercel.app/verificado"


class TestEmailVerification:
    def test_unconfirmed_account_with_the_right_password_is_told_to_verify(self, fake_client, accounts):
        # GoTrue checks the password BEFORE the confirmation, so this answer is
        # only reachable by someone who already knows the password.
        res = login(fake_client, "late@example.com")
        assert res.status_code == 403
        assert res.json() == NOT_CONFIRMED

    def test_unconfirmed_account_with_a_wrong_password_looks_like_any_failure(self, fake_client, accounts):
        res = login(fake_client, "late@example.com", "wrong password here")
        assert res.status_code == 401
        assert res.json() == BAD_CREDENTIALS

    @pytest.mark.parametrize("email", ["late@example.com", "nobody@example.com"])
    def test_resend_answers_the_same_for_every_email(self, fake_client, accounts, email):
        res = fake_client.post("/auth/resend-verification", json={"email": email})
        assert res.status_code == 200
        assert res.json() == RESENT

    def test_resend_asks_supabase_for_a_signup_link(self, fake_client, accounts, monkeypatch):
        monkeypatch.setenv("AUTH_EMAIL_REDIRECT_URL", "https://cantrack.vercel.app/verificado")
        fake_client.post("/auth/resend-verification", json={"email": "Late@Example.com "})
        sent = accounts.auth.resent[-1]
        assert sent["type"] == "signup" and sent["email"] == "late@example.com"
        assert sent["options"]["email_redirect_to"] == "https://cantrack.vercel.app/verificado"

    def test_provider_errors_are_not_revealed(self, fake_client, accounts):
        accounts.auth.fail_resend = True
        res = fake_client.post("/auth/resend-verification", json={"email": "late@example.com"})
        assert res.status_code == 200 and res.json() == RESENT

    def test_resend_is_three_a_minute(self, fake_client, accounts):
        codes = [fake_client.post("/auth/resend-verification", json={"email": "late@example.com"}).status_code
                 for _ in range(4)]
        assert codes == [200, 200, 200, 429]


class TestLockout:
    def test_five_failures_lock_the_email_for_15_minutes(self, fake_client, accounts, fake_clock):
        assert [login(fake_client, password="nope nope nope").status_code for _ in range(5)] == [401] * 5
        res = login(fake_client)  # even the right password
        assert res.status_code == 429
        assert res.json() == LOCKED
        assert int(res.headers["Retry-After"]) == 15 * 60
        fake_clock.now = FIXED_NOW + timedelta(minutes=16)
        assert login(fake_client).status_code == 200

    def test_unknown_emails_lock_exactly_the_same(self, fake_client, accounts):
        for _ in range(5):
            login(fake_client, "ghost@example.com", "nope nope nope")
        res = login(fake_client, "ghost@example.com", "nope nope nope")
        assert res.status_code == 429 and res.json() == LOCKED

    def test_a_success_clears_the_counter(self, fake_client, accounts):
        for _ in range(4):
            login(fake_client, password="nope nope nope")
        assert login(fake_client).status_code == 200
        for _ in range(4):
            login(fake_client, password="nope nope nope")
        assert login(fake_client, password="nope nope nope").status_code == 401

    def test_old_failures_expire(self, fake_client, accounts, fake_clock):
        for _ in range(4):
            login(fake_client, password="nope nope nope")
        fake_clock.now = FIXED_NOW + timedelta(minutes=16)
        assert login(fake_client, password="nope nope nope").status_code == 401  # counter restarted at 1

    def test_case_and_spaces_do_not_dodge_the_counter(self, fake_client, accounts):
        for variant in ("owner-a@example.com", "OWNER-A@example.com", " Owner-A@Example.com",
                        "owner-a@EXAMPLE.com", "owner-a@example.com"):
            login(fake_client, variant, "nope nope nope")
        assert login(fake_client).status_code == 429

    def test_only_a_hash_of_the_email_is_stored(self, fake_client, accounts):
        login(fake_client, password="nope nope nope")
        rows = accounts.tables["auth_login_attempts"]
        assert "owner-a@example.com" not in str(rows)
        assert rows[0]["email_hash"] == hashlib.sha256(b"owner-a@example.com").hexdigest()


class TestRefresh:
    def test_a_valid_refresh_token_gets_a_new_session(self, fake_client, accounts):
        refresh = login(fake_client).json()["refresh_token"]
        res = fake_client.post("/auth/refresh", json={"refreshToken": refresh})
        assert res.status_code == 200
        assert res.json()["access_token"].startswith("at-")

    def test_a_refresh_token_works_once(self, fake_client, accounts):
        refresh = login(fake_client).json()["refresh_token"]
        fake_client.post("/auth/refresh", json={"refreshToken": refresh})
        res = fake_client.post("/auth/refresh", json={"refreshToken": refresh})
        assert res.status_code == 401 and res.json() == BAD_SESSION

    @pytest.mark.parametrize("token", ["", "rt-forged", "x" * 2049])
    def test_bad_tokens(self, fake_client, accounts, token):
        assert fake_client.post("/auth/refresh", json={"refreshToken": token}).status_code in (400, 401)


class TestChangePassword:
    def change(self, client, token, current=GOOD, new="a brand new passphrase"):
        return client.post("/auth/change-password", json={"currentPassword": current, "newPassword": new},
                           headers=bearer(token))

    def test_changes_it_and_signs_out_the_other_devices(self, fake_client, accounts):
        phone = login(fake_client).json()["access_token"]
        laptop = login(fake_client).json()["access_token"]
        res = self.change(fake_client, laptop)
        assert res.status_code == 200 and res.json() == CHANGED
        assert accounts.accounts["owner-a@example.com"]["password"] == "a brand new passphrase"
        assert fake_client.get("/dogs", headers=bearer(phone)).status_code == 401   # other device out
        assert fake_client.get("/dogs", headers=bearer(laptop)).status_code == 200  # this one stays
        assert login(fake_client, password="a brand new passphrase").status_code == 200

    def test_wrong_current_password(self, fake_client, accounts):
        token = login(fake_client).json()["access_token"]
        res = self.change(fake_client, token, current="not my password")
        assert res.status_code == 401 and res.json() == WRONG_CURRENT
        assert accounts.accounts["owner-a@example.com"]["password"] == GOOD

    def test_wrong_current_password_counts_toward_the_lockout(self, fake_client, accounts):
        token = login(fake_client).json()["access_token"]
        for _ in range(5):
            reset_all_rate_limits()
            self.change(fake_client, token, current="not my password")
        assert login(fake_client).status_code == 429

    def test_new_password_follows_the_policy(self, fake_client, accounts):
        token = login(fake_client).json()["access_token"]
        assert self.change(fake_client, token, new="password123").status_code == 400

    def test_new_must_differ_from_current(self, fake_client, accounts):
        token = login(fake_client).json()["access_token"]
        res = self.change(fake_client, token, new=GOOD)
        assert res.status_code == 400 and res.json() == SAME_PASSWORD

    def test_needs_a_session(self, fake_client, accounts):
        res = fake_client.post("/auth/change-password", json={"currentPassword": GOOD, "newPassword": "x" * 12})
        assert res.status_code == 401


class TestLogout:
    def test_this_device_by_default(self, fake_client, accounts):
        phone = login(fake_client).json()["access_token"]
        laptop = login(fake_client).json()["access_token"]
        assert fake_client.post("/auth/logout", json={}, headers=bearer(laptop)).status_code == 204
        assert fake_client.get("/dogs", headers=bearer(laptop)).status_code == 401
        assert fake_client.get("/dogs", headers=bearer(phone)).status_code == 200

    def test_everywhere(self, fake_client, accounts):
        phone = login(fake_client).json()["access_token"]
        laptop = login(fake_client).json()["access_token"]
        assert fake_client.post("/auth/logout", json={"scope": "global"}, headers=bearer(laptop)).status_code == 204
        for token in (phone, laptop):
            assert fake_client.get("/dogs", headers=bearer(token)).status_code == 401

    def test_scope_is_validated(self, fake_client, accounts):
        token = login(fake_client).json()["access_token"]
        res = fake_client.post("/auth/logout", json={"scope": "others-and-more"}, headers=bearer(token))
        assert res.status_code == 400

    def test_needs_a_session(self, fake_client):
        assert fake_client.post("/auth/logout", json={}).status_code == 401

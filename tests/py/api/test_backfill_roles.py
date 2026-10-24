"""PR0: existing accounts keep their role after the move to app_metadata."""

from cantrack_api.backfill_roles import PAGE_SIZE, plan_backfill, run

from .conftest import make_spoofed_user, make_user


class FakeAdmin:
    def __init__(self, users):
        self.users = users
        self.updates = []

    def list_users(self, page=1, per_page=50):
        start = (page - 1) * per_page
        return self.users[start:start + per_page]

    def update_user_by_id(self, uid, attributes):
        self.updates.append((uid, attributes))


def legacy(user_id, role):
    # Pre-PR0 account: role only in user_metadata.
    return make_spoofed_user(user_id, f"{user_id}@example.com", None, role)


def test_only_legacy_accounts_without_a_server_role_are_planned():
    users = [
        legacy("old-owner", "owner"),
        legacy("old-walker", "walker"),
        legacy("weird", "admin"),                         # invalid legacy role: skipped
        make_user("new-owner", "n@example.com", "owner"),  # already has a server role
        make_spoofed_user("flipped", "f@example.com", "owner", "walker"),  # never promoted
    ]
    assert plan_backfill(users) == [("old-owner", "owner"), ("old-walker", "walker")]


def test_dry_run_writes_nothing():
    admin = FakeAdmin([legacy("old-owner", "owner")])
    assert run(admin, apply=False) == [("old-owner", "owner")]
    assert admin.updates == []


def test_apply_writes_app_metadata():
    admin = FakeAdmin([legacy("old-owner", "owner")])
    run(admin, apply=True)
    assert admin.updates == [("old-owner", {"app_metadata": {"role": "owner"}})]


def test_every_page_is_read():
    users = [legacy(f"u{i}", "owner") for i in range(PAGE_SIZE + 3)]
    admin = FakeAdmin(users)
    assert len(run(admin, apply=False)) == PAGE_SIZE + 3

"""FR-22: owner statistics authentication, isolation, and failure handling."""

from postgrest.exceptions import APIError

from .conftest import bearer

OWNER_PATH = "/owners/owner-a/stats"


def api_error(message="connection lost"):
    return APIError({"message": message, "code": "XX000", "hint": None, "details": None})


def test_owner_stats_requires_an_owner_token_and_own_account(fake_client):
    missing_token = fake_client.get(OWNER_PATH)
    assert missing_token.status_code == 401
    assert missing_token.json() == {"detail": "A bearer token is required."}

    walker = fake_client.get(OWNER_PATH, headers=bearer("token-w"))
    assert walker.status_code == 403
    assert walker.json() == {"detail": "Only owners can use this."}

    foreign_owner = fake_client.get(OWNER_PATH, headers=bearer("token-b"))
    assert foreign_owner.status_code == 403
    assert foreign_owner.json() == {"detail": "You can only access your own account."}


def test_every_owner_stats_query_is_scoped_to_the_owner(fake_client, fake):
    fake.queries.clear()

    fake_client.get(OWNER_PATH, headers=bearer("token-a"))

    reads = [
        query
        for query in fake.queries
        if query[0] in {"walk_requests", "checkouts"} and query[1] == "select"
    ]
    assert {query[0] for query in reads} == {"walk_requests", "checkouts"}
    assert all(("owner_id", "owner-a") in query[2] for query in reads)


def test_owner_stats_database_errors_are_opaque_500(fake_client, fake):
    fake.fail_with = api_error()

    response = fake_client.get(OWNER_PATH, headers=bearer("token-a"))

    assert response.status_code == 500
    assert response.json()["detail"] == "Something went wrong on our side."
    assert "connection lost" not in response.text

"""Anonymous auth budgets must not trust caller-supplied bearer identities."""

import pytest

from cantrack_api.app import RATE_LIMIT_MESSAGE


@pytest.mark.parametrize(
    ("path", "limit", "expected_status"),
    [
        ("/auth/login", 5, 401),
        ("/auth/reset-password", 5, 200),
        ("/auth/resend-verification", 3, 200),
    ],
)
def test_rotating_unverified_bearer_tokens_cannot_reset_auth_budget(
    fake_client, path, limit, expected_status
):
    for index in range(limit):
        response = fake_client.post(
            path,
            json={"email": f"attempt-{index}@example.com", "password": "wrong"},
            headers={"Authorization": f"Bearer fabricated-{index}"},
        )
        assert response.status_code == expected_status

    response = fake_client.post(
        path,
        json={"email": "next@example.com", "password": "wrong"},
        headers={"Authorization": "Bearer another-fabricated-token"},
    )
    assert response.status_code == 429
    assert response.json() == {"detail": RATE_LIMIT_MESSAGE}
    assert response.headers["Retry-After"] == "60"

"""Global error handlers: friendly, consistent, non-leaky messages for framework-level
404 (unmatched route) and 405 (wrong method).

The professor wants human error messages ("we couldn't find X") rather than bare framework
defaults ("Not Found"). Two things are deliberately NOT in scope here:

- Validation errors already have their own handler (returns 400, not 422) — unchanged.
- Router-level HTTPExceptions carry their own friendly, enumeration-safe messages
  (e.g. "Request not found." returned identically whether a walk is missing or not the
  caller's, so existence cannot be probed). The global 404 handler must NOT overwrite those:
  it only rewrites the framework's own default "Not Found"/"Method Not Allowed" text.
"""

import uuid

from cantrack_api.routers.requests import NOT_FOUND_MESSAGE as REQUEST_NOT_FOUND

from .conftest import bearer, make_user

NOT_FOUND = "We couldn't find what you're looking for."
METHOD_NOT_ALLOWED = "That action isn't allowed here."


class TestGlobalErrorHandlers:
    def test_unmatched_route_is_friendly_404(self, fake_client):
        r = fake_client.get("/no-such-endpoint-anywhere")
        assert r.status_code == 404
        assert r.json() == {"detail": NOT_FOUND}

    def test_wrong_method_is_friendly_405(self, fake_client):
        # /auth/login exists as POST; GET it to force the framework 405.
        r = fake_client.get("/auth/login")
        assert r.status_code == 405
        assert r.json() == {"detail": METHOD_NOT_ALLOWED}

    def test_router_404_message_is_not_clobbered(self, fake_client, fake):
        # A matched route that 404s a *missing resource* must keep its own specific
        # message, not be swallowed by the global handler. GET /requests/{id} raises
        # the router's own "Request not found." for an unknown id — the same message
        # whether the walk is missing or not the caller's, so existence can't be probed.
        fake.add_user("token-o", make_user("owner-o", "owner-o@example.com", "owner"))
        r = fake_client.get(f"/requests/{uuid.uuid4()}", headers=bearer("token-o"))
        assert r.status_code == 404
        assert r.json() == {"detail": REQUEST_NOT_FOUND}
        assert r.json()["detail"] != NOT_FOUND

"""PR0 (e): production configuration — nothing exposed that the app doesn't need.

Closes: the professor's "/me" (``GET /_whoami`` echoed the caller's id and
email), the public Swagger UI in production, no HSTS, a Docker image running as
root, CI with a write-capable token, and a server that boots with missing
secrets and only fails on the first request.
"""

import pathlib

import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

from cantrack_api import main as main_module
from cantrack_api.app import create_app
from cantrack_api.deps import get_supabase
from cantrack_api.settings import validate_settings

from .conftest import bearer

BACKEND = pathlib.Path(__file__).resolve().parents[3]
API_DIR = BACKEND / "apps" / "api-py"

PROD_ENV = {
    "ENV": "production",
    "SUPABASE_URL": "https://x.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "service-role",
    "WEB_ORIGIN": "https://cantrack.vercel.app",
}


class TestNoIdentityEcho:
    def test_whoami_is_gone_from_the_real_app(self, fake_client):
        res = fake_client.get("/_whoami", headers=bearer("token-a"))
        assert res.status_code == 404

    def test_no_route_echoes_the_caller(self):
        paths = {getattr(route, "path", None) for route in create_app().routes}
        assert not {"/_whoami", "/me", "/users/me", "/auth/me", "/whoami"} & paths


class TestDocsOnlyOutsideProduction:
    @pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
    def test_docs_hidden_in_production(self, monkeypatch, path):
        monkeypatch.setenv("ENV", "production")
        assert TestClient(create_app()).get(path).status_code == 404

    @pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
    def test_docs_available_in_development(self, monkeypatch, path):
        monkeypatch.delenv("ENV", raising=False)
        assert TestClient(create_app()).get(path).status_code == 200


class TestHealth:
    def test_health_is_public_and_checks_the_database(self, fake_client, fake):
        res = fake_client.get("/health")
        assert res.status_code == 200
        assert res.json() == {"status": "ok"}
        assert fake.queries, "health must touch the database"

    def test_database_down_is_503_without_details(self, fake_client, fake):
        fake.fail_with = APIError({"message": "connection refused to 10.0.0.5", "code": "XX000",
                                   "hint": None, "details": None})
        res = fake_client.get("/health")
        assert res.status_code == 503
        assert res.json() == {"status": "degraded"}

    def test_render_health_check_points_at_health(self):
        render = (BACKEND / "render.yaml").read_text()
        assert "healthCheckPath: /health" in render
        assert "key: ENV" in render and "value: production" in render
        assert "key: TRUSTED_PROXY_IPS" in render


class TestHeaders:
    def test_hsts_on_every_response(self, fake_client):
        for res in (fake_client.get("/health"), fake_client.get("/dogs")):
            assert res.headers["Strict-Transport-Security"] == "max-age=63072000; includeSubDomains"


class TestContainerAndCi:
    def test_docker_image_does_not_run_as_root(self):
        lines = [line.strip() for line in (API_DIR / "Dockerfile").read_text().splitlines()]
        users = [line for line in lines if line.startswith("USER ")]
        assert users, "Dockerfile needs a USER line"
        assert users[-1] not in ("USER root", "USER 0")
        assert lines.index(users[-1]) < max(i for i, line in enumerate(lines) if line.startswith("CMD"))

    def test_ci_token_is_read_only(self):
        ci = (BACKEND / ".github" / "workflows" / "ci.yml").read_text()
        assert "permissions:" in ci and "contents: read" in ci

    def test_bytecode_and_env_files_are_ignored(self):
        ignore = (BACKEND / ".gitignore").read_text()
        assert "__pycache__/" in ignore
        assert ".env" in ignore


class TestSettingsFailFast:
    def test_production_with_everything_set_passes(self):
        validate_settings(PROD_ENV)

    @pytest.mark.parametrize("missing", ["SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "WEB_ORIGIN"])
    def test_production_missing_a_secret_refuses_to_boot(self, missing):
        env = {key: value for key, value in PROD_ENV.items() if key != missing}
        with pytest.raises(RuntimeError, match=missing):
            validate_settings(env)

    @pytest.mark.parametrize("origin", ["*", "http://localhost:5173", "https://a.app,http://127.0.0.1:5173"])
    def test_production_refuses_wildcard_or_local_origins(self, origin):
        with pytest.raises(RuntimeError, match="WEB_ORIGIN"):
            validate_settings({**PROD_ENV, "WEB_ORIGIN": origin})

    def test_development_tolerates_missing_settings(self):
        validate_settings({})

    def test_main_validates_before_serving(self, monkeypatch):
        calls = []
        monkeypatch.setattr(main_module, "validate_settings", lambda env: calls.append("validated"))
        monkeypatch.setattr(main_module.uvicorn, "run", lambda *a, **k: calls.append("served"))
        main_module.run()
        assert calls == ["validated", "served"]

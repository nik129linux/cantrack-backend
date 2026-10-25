"""Railway deployment contract: config-as-code, client IP from the edge, and a runbook.

Railway's edge overwrites X-Real-IP on every request (staff answer, Oct 2026) and
does not publish stable proxy CIDRs, so the client IP for anonymous rate limits
comes from that header, and ONLY when CLIENT_IP_HEADER says so: anywhere else a
caller could forge it and dodge the 5/min login limit.
"""

import pathlib
import tomllib

from starlette.requests import Request

from cantrack_api.app import rate_limit_key

BACKEND = pathlib.Path(__file__).resolve().parents[3]
API_DIR = BACKEND / "apps" / "api-py"


def make_request(headers=None, client=("100.64.0.7", 4000)):
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": "POST", "path": "/auth/login", "headers": raw,
                    "client": client, "query_string": b""})


class TestClientIp:
    def test_edge_header_is_used_when_configured(self, monkeypatch):
        monkeypatch.setenv("CLIENT_IP_HEADER", "X-Real-IP")
        assert rate_limit_key(make_request({"X-Real-IP": "190.24.5.9"})) == "ip:190.24.5.9"

    def test_header_is_ignored_when_not_configured(self, monkeypatch):
        monkeypatch.delenv("CLIENT_IP_HEADER", raising=False)
        # Off Railway nobody overwrites the header, so a client could forge it.
        assert rate_limit_key(make_request({"X-Real-IP": "1.2.3.4"})) == "ip:100.64.0.7"

    def test_missing_header_falls_back_to_the_peer(self, monkeypatch):
        monkeypatch.setenv("CLIENT_IP_HEADER", "X-Real-IP")
        assert rate_limit_key(make_request()) == "ip:100.64.0.7"

    def test_tokens_still_win_over_any_ip(self, monkeypatch):
        monkeypatch.setenv("CLIENT_IP_HEADER", "X-Real-IP")
        key = rate_limit_key(make_request({"X-Real-IP": "190.24.5.9", "Authorization": "Bearer abc"}))
        assert key.startswith("user:")

    def test_header_value_is_bounded(self, monkeypatch):
        monkeypatch.setenv("CLIENT_IP_HEADER", "X-Real-IP")
        key = rate_limit_key(make_request({"X-Real-IP": "9" * 500}))
        assert len(key) <= 64


class TestRailwayConfig:
    def config(self):
        return tomllib.loads((API_DIR / "railway.toml").read_text())

    def test_builds_the_dockerfile(self):
        build = self.config()["build"]
        assert build["builder"] == "DOCKERFILE"
        assert build["dockerfilePath"] == "Dockerfile"

    def test_health_check_and_restart_policy(self):
        deploy = self.config()["deploy"]
        assert deploy["healthcheckPath"] == "/health"
        assert deploy["healthcheckTimeout"] >= 120  # the CLIP session loads on first use
        assert deploy["restartPolicyType"] == "ON_FAILURE"
        assert 1 <= deploy["restartPolicyMaxRetries"] <= 10

    def test_dockerfile_still_runs_as_non_root_and_reads_port(self):
        dockerfile = (API_DIR / "Dockerfile").read_text()
        assert "USER " in dockerfile
        assert "cantrack_api.main" in dockerfile  # main.run() reads PORT, which Railway injects


class TestRunbook:
    def test_deploy_guide_covers_the_steps_and_every_variable(self):
        guide = (BACKEND / "docs" / "DEPLOY.md").read_text()
        for needle in ("railway login", "railway up", "/health", "rollback",
                       "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "WEB_ORIGIN", "ENV=production",
                       "CLIENT_IP_HEADER=X-Real-IP", "TRUSTED_PROXY_IPS", "OLLAMA_API_KEY",
                       "AI_WALK_REPORT"):
            assert needle in guide, f"DEPLOY.md is missing {needle}"

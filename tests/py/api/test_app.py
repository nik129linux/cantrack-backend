"""App wiring: CORS and import-time safety."""

import importlib


def test_importing_the_app_does_not_need_supabase_env(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    import cantrack_api.app as app_module

    importlib.reload(app_module)
    assert app_module.create_app() is not None


def test_cors_allows_the_configured_web_origin(client, monkeypatch):
    res = client.options(
        "/auth/login",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert res.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_cors_rejects_an_unknown_origin(client):
    res = client.options(
        "/auth/login",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert "access-control-allow-origin" not in res.headers


def test_cors_origins_come_from_web_origin_env(monkeypatch):
    monkeypatch.setenv("WEB_ORIGIN", "https://a.app,https://b.app")
    from fastapi.testclient import TestClient

    from cantrack_api.app import create_app

    tc = TestClient(create_app())
    for origin in ("https://a.app", "https://b.app"):
        res = tc.options(
            "/auth/login",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )
        assert res.headers.get("access-control-allow-origin") == origin

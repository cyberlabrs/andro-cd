"""API-level security tests: headers, CSRF, auth, path traversal, rate limiting."""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


@pytest.fixture()
def client():
    # No context manager: the lifespan (db init, reconcile loop) must not run in tests.
    return TestClient(app, raise_server_exceptions=False)


def test_security_headers_present(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in r.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_schema_is_public_and_valid(client):
    r = client.get("/api/schema")
    assert r.status_code == 200
    schema = r.json()
    assert schema["title"] == "Manifest"
    assert "spec" in schema["required"]


def test_csrf_cross_origin_post_rejected(client):
    r = client.post("/api/refresh", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert "cross-origin" in r.json()["detail"]


def test_csrf_same_origin_post_allowed(client, monkeypatch):
    # Same host origin passes the CSRF gate (may then fail auth/handler, not 403 CSRF).
    monkeypatch.setattr(settings, "auth_mode", "github")
    monkeypatch.setattr(settings, "github_client_id", "id")
    monkeypatch.setattr(settings, "github_client_secret", "secret")
    r = client.post("/api/refresh", headers={"origin": "http://testserver"})
    assert r.status_code == 401  # CSRF ok, blocked by auth instead


def test_api_requires_auth_in_github_mode(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "github")
    monkeypatch.setattr(settings, "github_client_id", "id")
    monkeypatch.setattr(settings, "github_client_secret", "secret")
    assert client.get("/api/apps").status_code == 401
    assert client.get("/api/repos").status_code == 401
    # public endpoints stay reachable
    assert client.get("/api/schema").status_code == 200
    assert client.get("/api/auth/me").status_code == 200


def test_api_token_grants_role(client, monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "github")
    monkeypatch.setattr(settings, "github_client_id", "id")
    monkeypatch.setattr(settings, "github_client_secret", "secret")
    monkeypatch.setattr(settings, "api_tokens", {"ci-token-0123456789abcdef": "viewer"})
    ok = client.get("/api/apps", headers={"Authorization": "Bearer ci-token-0123456789abcdef"})
    assert ok.status_code == 200
    bad = client.get("/api/apps", headers={"Authorization": "Bearer wrong-token"})
    assert bad.status_code == 401
    # viewer token cannot trigger operator actions
    denied = client.post("/api/refresh",
                         headers={"Authorization": "Bearer ci-token-0123456789abcdef"})
    assert denied.status_code == 403


def test_spa_path_traversal_blocked(client, tmp_path, monkeypatch):
    # The SPA fallback must never serve files outside the static root.
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<html>app</html>")
    secret = tmp_path / "secret.txt"
    secret.write_text("top-secret")
    monkeypatch.setattr(settings, "static_dir", str(static))
    r = client.get("/../secret.txt")
    assert "top-secret" not in r.text
    r = client.get("/%2e%2e/secret.txt")
    assert "top-secret" not in r.text


def test_rate_limiter_window():
    from app import ratelimit
    key = "test:limiter"
    ratelimit._hits.pop(key, None)
    assert all(ratelimit.allow(key, limit=3) for _ in range(3))
    assert not ratelimit.allow(key, limit=3)


def test_startup_problems_flag_misconfig(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "github")
    monkeypatch.setattr(settings, "github_client_id", "")
    monkeypatch.setattr(settings, "github_client_secret", "")
    problems = settings.startup_problems()
    assert any("GITHUB_CLIENT_ID" in p for p in problems)


def test_https_token_not_written_to_remote_url():
    from app.git_sync import _resolve_auth
    url, env, secret = _resolve_auth({"id": 1, "url": "https://github.com/o/r",
                                      "auth_type": "https", "token": "tok123"})
    assert "tok123" not in url
    assert "GIT_AUTH_HEADER" in env and env["GIT_AUTH_HEADER"].startswith("Authorization: Basic ")
    assert secret == "tok123"

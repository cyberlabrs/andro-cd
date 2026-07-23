"""Tests for generic OIDC login (AUTH_MODE=oidc)."""
import time

import pytest
from fastapi.testclient import TestClient

from app import oidc
from app.config import settings
from app.main import app


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def oidc_mode(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example")
    monkeypatch.setattr(settings, "oidc_client_id", "client-123")
    monkeypatch.setattr(settings, "oidc_client_secret", "secret-abc")


# ---------- flow cookie (signed, tamper-resistant) ----------

def test_flow_cookie_roundtrip():
    packed = oidc._pack_flow({"state": "s", "nonce": "n", "cv": "v",
                              "exp": int(time.time()) + 60})
    data = oidc._unpack_flow(packed)
    assert data and data["state"] == "s" and data["cv"] == "v"


def test_flow_cookie_tamper_rejected():
    packed = oidc._pack_flow({"state": "s", "exp": int(time.time()) + 60})
    payload, sig = packed.rsplit(".", 1)
    assert oidc._unpack_flow(f"{payload}x.{sig}") is None
    assert oidc._unpack_flow(f"{payload}.{sig[:-2]}00") is None


def test_flow_cookie_expiry():
    packed = oidc._pack_flow({"state": "s", "exp": int(time.time()) - 1})
    assert oidc._unpack_flow(packed) is None


# ---------- group extraction ----------

def test_extract_groups_variants(monkeypatch):
    monkeypatch.setattr(settings, "oidc_groups_claim", "groups")
    assert oidc._extract_groups({"groups": ["a", "b"]}) == ["a", "b"]
    assert oidc._extract_groups({"groups": "solo"}) == ["solo"]
    assert oidc._extract_groups({}) == []


# ---------- allowlists (fail closed, AND across configured gates) ----------

def test_allowed_users(monkeypatch):
    monkeypatch.setattr(settings, "oidc_allowed_users", frozenset({"alice@example.com"}))
    monkeypatch.setattr(settings, "oidc_allowed_domains", frozenset())
    monkeypatch.setattr(settings, "oidc_allowed_groups", frozenset())
    oidc._check_allowed("alice@example.com", {})              # ok
    with pytest.raises(Exception):
        oidc._check_allowed("bob@example.com", {})


def test_allowed_domains(monkeypatch):
    monkeypatch.setattr(settings, "oidc_allowed_users", frozenset())
    monkeypatch.setattr(settings, "oidc_allowed_domains", frozenset({"example.com"}))
    monkeypatch.setattr(settings, "oidc_allowed_groups", frozenset())
    oidc._check_allowed("anyone@example.com", {})
    with pytest.raises(Exception):
        oidc._check_allowed("intruder@evil.com", {})


def test_allowed_groups(monkeypatch):
    monkeypatch.setattr(settings, "oidc_allowed_users", frozenset())
    monkeypatch.setattr(settings, "oidc_allowed_domains", frozenset())
    monkeypatch.setattr(settings, "oidc_allowed_groups", frozenset({"platform"}))
    monkeypatch.setattr(settings, "oidc_groups_claim", "groups")
    oidc._check_allowed("x@y.com", {"groups": ["platform", "eng"]})
    with pytest.raises(Exception):
        oidc._check_allowed("x@y.com", {"groups": ["eng"]})


def test_no_allowlist_permits_everyone(monkeypatch):
    monkeypatch.setattr(settings, "oidc_allowed_users", frozenset())
    monkeypatch.setattr(settings, "oidc_allowed_domains", frozenset())
    monkeypatch.setattr(settings, "oidc_allowed_groups", frozenset())
    oidc._check_allowed("anyone@anywhere.com", {})   # no exception


# ---------- routes / integration ----------

def test_login_redirects_to_provider(client, oidc_mode, monkeypatch):
    monkeypatch.setattr(oidc, "_discover", lambda: {
        "authorization_endpoint": "https://idp.example/authorize",
        "token_endpoint": "https://idp.example/token",
        "jwks_uri": "https://idp.example/jwks",
        "issuer": "https://idp.example",
    })
    r = client.get("/api/auth/oidc/login", follow_redirects=False)
    assert r.status_code in (302, 307)
    loc = r.headers["location"]
    assert loc.startswith("https://idp.example/authorize")
    assert "code_challenge=" in loc and "code_challenge_method=S256" in loc
    assert "client_id=client-123" in loc
    assert "androcd_oidc_flow" in r.headers.get("set-cookie", "")


def test_github_login_404_in_oidc_mode(client, oidc_mode):
    # the GitHub OAuth routes must be inert when running OIDC
    assert client.get("/api/auth/login", follow_redirects=False).status_code == 404


def test_me_reports_oidc_mode(client, oidc_mode):
    body = client.get("/api/auth/me").json()
    assert body["mode"] == "oidc"
    assert body["authenticated"] is False


def test_api_requires_auth_in_oidc_mode(client, oidc_mode):
    assert client.get("/api/apps").status_code == 401
    # public endpoints still reachable
    assert client.get("/api/schema").status_code == 200


def test_oidc_session_cookie_grants_access(client, oidc_mode):
    from app import auth
    token = auth.create_session({"login": "alice@example.com", "name": "Alice", "avatar": None})
    r = client.get("/api/apps", cookies={auth.SESSION_COOKIE: token})
    assert r.status_code == 200


def test_startup_flags_oidc_misconfig(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    monkeypatch.setattr(settings, "oidc_issuer", "")
    monkeypatch.setattr(settings, "oidc_client_id", "")
    monkeypatch.setattr(settings, "oidc_client_secret", "")
    problems = settings.startup_problems()
    assert any("OIDC_ISSUER" in p for p in problems)


def test_startup_warns_oidc_without_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "oidc")
    monkeypatch.setattr(settings, "oidc_issuer", "https://idp.example")
    monkeypatch.setattr(settings, "oidc_client_id", "c")
    monkeypatch.setattr(settings, "oidc_client_secret", "s")
    monkeypatch.setattr(settings, "oidc_allowed_users", frozenset())
    monkeypatch.setattr(settings, "oidc_allowed_domains", frozenset())
    monkeypatch.setattr(settings, "oidc_allowed_groups", frozenset())
    assert any("anyone with an account" in p for p in settings.startup_problems())

import time

from app.auth import (
    SESSION_ABSOLUTE_TTL,
    SESSION_RENEW_WITHIN,
    SESSION_TTL,
    create_session,
    renew_session,
    should_renew,
    verify_session,
)


def test_valid_session_roundtrip():
    token = create_session({"login": "andrija", "name": "A", "avatar": None})
    data = verify_session(token)
    assert data is not None
    assert data["login"] == "andrija"
    assert data["exp"] > time.time()


def test_tampered_payload_rejected():
    token = create_session({"login": "andrija"})
    payload, sig = token.rsplit(".", 1)
    forged_payload = payload[:-2] + "xx"
    assert verify_session(f"{forged_payload}.{sig}") is None


def test_tampered_signature_rejected():
    token = create_session({"login": "andrija"})
    assert verify_session(token[:-4] + "0000") is None


def test_expired_session_rejected(monkeypatch):
    import app.auth as auth_mod
    monkeypatch.setattr(auth_mod, "SESSION_TTL", -10)
    token = create_session({"login": "andrija"})
    assert verify_session(token) is None


def test_garbage_tokens_rejected():
    assert verify_session("") is None
    assert verify_session("no-dot") is None
    assert verify_session("a.b") is None


def test_should_not_renew_fresh_session():
    token = create_session({"login": "andrija"})
    data = verify_session(token)
    assert data is not None
    # brand new session: full TTL ahead → nowhere near the renewal window
    assert should_renew(data) is False


def test_should_renew_near_expiry():
    now = int(time.time())
    # exp inside the renewal window; iat well within the absolute cap
    session = {"login": "andrija", "iat": now - 60, "exp": now + (SESSION_RENEW_WITHIN // 2)}
    assert should_renew(session) is True


def test_renew_preserves_iat_and_user():
    now = int(time.time())
    original_iat = now - 3600
    session = {"login": "andrija", "name": "A", "iat": original_iat,
               "exp": now + (SESSION_RENEW_WITHIN // 2)}
    token = renew_session(session)
    data = verify_session(token)
    assert data is not None
    assert data["login"] == "andrija"
    assert data["name"] == "A"
    assert data["iat"] == original_iat            # anchor is preserved
    assert data["exp"] > now + (SESSION_TTL - 10)  # fresh full TTL


def test_absolute_cap_rejects_ancient_session():
    now = int(time.time())
    # a valid signature over data older than the absolute cap must still fail
    token = create_session({"login": "andrija"}, iat=now - SESSION_ABSOLUTE_TTL - 60)
    assert verify_session(token) is None


def test_should_not_renew_past_absolute_cap():
    now = int(time.time())
    # iat old enough that any renewal would exceed the absolute cap
    session = {"login": "andrija", "iat": now - (SESSION_ABSOLUTE_TTL - 60),
               "exp": now + 60}
    assert should_renew(session) is False

import time

from app.auth import create_session, verify_session


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

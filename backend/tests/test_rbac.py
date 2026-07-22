from app import auth
from app.config import settings


def test_default_is_admin_without_rbac_config(monkeypatch):
    monkeypatch.setattr(settings, "rbac_admins", frozenset())
    monkeypatch.setattr(settings, "rbac_operators", frozenset())
    monkeypatch.setattr(settings, "rbac_default_role", "")
    assert auth.role_for("anyone") == "admin"


def test_roles_from_lists(monkeypatch):
    monkeypatch.setattr(settings, "rbac_admins", frozenset({"boss"}))
    monkeypatch.setattr(settings, "rbac_operators", frozenset({"dev"}))
    monkeypatch.setattr(settings, "rbac_default_role", "")
    assert auth.role_for("Boss") == "admin"
    assert auth.role_for("dev") == "operator"
    assert auth.role_for("guest") == "viewer"


def test_default_role_override(monkeypatch):
    monkeypatch.setattr(settings, "rbac_admins", frozenset({"boss"}))
    monkeypatch.setattr(settings, "rbac_operators", frozenset())
    monkeypatch.setattr(settings, "rbac_default_role", "operator")
    assert auth.role_for("guest") == "operator"

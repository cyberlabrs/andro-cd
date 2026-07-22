import pytest

from app import crypto, reconciler
from app.models import Manifest
from app.state import store


def base_manifest(profile: str | None = None, region: str | None = None) -> Manifest:
    doc = {
        "apiVersion": "andro-cd/v1",
        "kind": "ECSService",
        "metadata": {"name": "app"},
        "spec": {
            "cluster": "prod",
            "network": {"subnets": ["subnet-a"]},
            "taskDefinition": {"containers": [{"name": "c", "image": "img:1"}]},
        },
    }
    if profile:
        doc["spec"]["awsProfile"] = profile
    if region:
        doc["spec"]["region"] = region
    return Manifest.model_validate(doc)


def test_crypto_roundtrip():
    secret = "AKIAEXAMPLE/very+secret"
    assert crypto.decrypt(crypto.encrypt(secret)) == secret


def test_tampered_ciphertext_rejected():
    token = crypto.encrypt("x")
    with pytest.raises(crypto.DecryptError):
        crypto.decrypt(token[:-2] + "aa")


def test_unknown_profile_raises():
    m = base_manifest(profile="missing", region="us-east-1")
    with pytest.raises(ValueError, match="not configured"):
        reconciler._profile(m)


def test_region_precedence(monkeypatch):
    store.profiles["prod"] = {
        "name": "prod", "region": "eu-west-1",
        "access_key_id": "AKIA", "secret_access_key": "s",
    }
    try:
        # manifest region wins
        assert reconciler._region(base_manifest(profile="prod", region="us-east-1")) == "us-east-1"
        # profile region is the fallback
        assert reconciler._region(base_manifest(profile="prod")) == "eu-west-1"
        # no profile -> global settings
        monkeypatch.setattr(reconciler.settings, "aws_region", "ap-south-1")
        assert reconciler._region(base_manifest()) == "ap-south-1"
    finally:
        store.profiles.pop("prod", None)

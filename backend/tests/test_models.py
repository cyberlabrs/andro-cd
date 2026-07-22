import pytest
from pydantic import ValidationError

from app.models import Manifest


def base_doc() -> dict:
    return {
        "apiVersion": "andro-cd/v1",
        "kind": "ECSService",
        "metadata": {"name": "app"},
        "spec": {
            "cluster": "prod",
            "network": {"subnets": ["subnet-a"]},
            "taskDefinition": {"containers": [{"name": "c", "image": "img:1"}]},
        },
    }


def test_minimal_manifest_valid_with_defaults():
    m = Manifest.model_validate(base_doc())
    assert m.name == "app"
    assert m.family == "app"
    assert m.spec.service.desiredCount == 1
    assert m.spec.service.launchType == "FARGATE"
    assert m.spec.service.circuitBreaker is True
    assert m.spec.taskDefinition.cpu == "256"


def test_labels_parsed_and_coerced():
    doc = base_doc()
    doc["metadata"]["labels"] = {"team": "platform", "replicas": 3}
    m = Manifest.model_validate(doc)
    assert m.metadata.labels == {"team": "platform", "replicas": "3"}


def test_labels_default_empty():
    assert Manifest.model_validate(base_doc()).metadata.labels == {}


def test_family_override():
    doc = base_doc()
    doc["spec"]["taskDefinition"]["family"] = "custom-family"
    assert Manifest.model_validate(doc).family == "custom-family"


def test_unsupported_kind_rejected():
    doc = base_doc()
    doc["kind"] = "Deployment"
    with pytest.raises(ValidationError):
        Manifest.model_validate(doc)


def test_empty_containers_rejected():
    doc = base_doc()
    doc["spec"]["taskDefinition"]["containers"] = []
    with pytest.raises(ValidationError):
        Manifest.model_validate(doc)


def test_empty_subnets_rejected():
    doc = base_doc()
    doc["spec"]["network"]["subnets"] = []
    with pytest.raises(ValidationError):
        Manifest.model_validate(doc)


def test_cpu_memory_coerced_to_string():
    doc = base_doc()
    doc["spec"]["taskDefinition"]["cpu"] = 512
    doc["spec"]["taskDefinition"]["memory"] = 1024
    m = Manifest.model_validate(doc)
    assert m.spec.taskDefinition.cpu == "512"
    assert m.spec.taskDefinition.memory == "1024"


def test_env_list_and_dict_forms():
    doc = base_doc()
    doc["spec"]["taskDefinition"]["containers"][0]["environment"] = {"B": 2, "A": "x"}
    m = Manifest.model_validate(doc)
    assert m.spec.taskDefinition.containers[0].env_list() == [
        {"name": "A", "value": "x"},
        {"name": "B", "value": "2"},
    ]

    doc["spec"]["taskDefinition"]["containers"][0]["environment"] = [
        {"name": "B", "value": "2"},
        {"name": "A", "value": "x"},
    ]
    m = Manifest.model_validate(doc)
    assert m.spec.taskDefinition.containers[0].env_list() == [
        {"name": "A", "value": "x"},
        {"name": "B", "value": "2"},
    ]


def test_sync_policy_defaults_and_parse():
    m = Manifest.model_validate(base_doc())
    assert m.spec.syncPolicy.autoSync is None
    assert m.spec.syncPolicy.selfHeal is False
    assert m.spec.syncPolicy.prune is False

    doc = base_doc()
    doc["spec"]["syncPolicy"] = {"autoSync": True, "selfHeal": True, "prune": True}
    p = Manifest.model_validate(doc).spec.syncPolicy
    assert p.autoSync is True and p.selfHeal is True and p.prune is True

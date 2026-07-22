import pytest

from app.engine import _expand_docs, _substitute
from app.models import Manifest
from app.reconciler import _load_balancers, _resolve_image

REPO = {"id": 1, "url": "file:///x"}


def test_substitute_recursive():
    out = _substitute(
        {"a": "api-${env}", "b": ["${env}", 1], "c": {"d": "${count}"}},
        {"env": "prod", "count": 3},
    )
    assert out == {"a": "api-prod", "b": ["prod", 1], "c": {"d": "3"}}


def test_serviceset_expansion():
    doc = {
        "apiVersion": "andro-cd/v1", "kind": "ECSServiceSet",
        "metadata": {"name": "set"},
        "spec": {
            "generators": [{"values": {"env": "dev"}}, {"values": {"env": "prod"}}],
            "template": {
                "apiVersion": "andro-cd/v1", "kind": "ECSService",
                "metadata": {"name": "api-${env}"},
                "spec": {"cluster": "${env}",
                         "network": {"subnets": ["subnet-a"]},
                         "taskDefinition": {"containers": [{"name": "c", "image": "i:1"}]}},
            },
        },
    }
    expanded = _expand_docs([(REPO, "set.yaml", doc)])
    names = [d[2]["metadata"]["name"] for d in expanded]
    assert names == ["api-dev", "api-prod"]
    for _, _, d in expanded:
        Manifest.model_validate(d)


def test_serviceset_missing_template_is_error():
    doc = {"kind": "ECSServiceSet", "metadata": {"name": "x"}, "spec": {}}
    expanded = _expand_docs([(REPO, "x.yaml", doc)])
    assert "__parse_error__" in expanded[0][2]


def test_scheduled_task_requires_schedule():
    doc = {
        "apiVersion": "andro-cd/v1", "kind": "ECSScheduledTask",
        "metadata": {"name": "job"},
        "spec": {"cluster": "c", "network": {"subnets": ["s"]},
                 "taskDefinition": {"containers": [{"name": "c", "image": "i"}]}},
    }
    with pytest.raises(Exception, match="schedule is required"):
        Manifest.model_validate(doc)
    doc["spec"]["schedule"] = {"expression": "cron(0 3 * * ? *)", "roleArn": "arn:x"}
    m = Manifest.model_validate(doc)
    assert m.kind == "ECSScheduledTask"


def test_non_ecr_image_passthrough():
    assert _resolve_image("nginx:1.27", "us-east-1", None) == "nginx:1.27"
    digest_ref = "1.dkr.ecr.us-east-1.amazonaws.com/app@sha256:abc"
    assert _resolve_image(digest_ref, "us-east-1", None) == digest_ref


def test_load_balancer_defaults_first_container():
    doc = {
        "apiVersion": "andro-cd/v1", "kind": "ECSService",
        "metadata": {"name": "app"},
        "spec": {
            "cluster": "c",
            "service": {"loadBalancer": {"targetGroupArn": "arn:tg", "containerPort": 80}},
            "network": {"subnets": ["s"]},
            "taskDefinition": {"containers": [{"name": "web", "image": "i"}]},
        },
    }
    m = Manifest.model_validate(doc)
    assert _load_balancers(m) == [
        {"targetGroupArn": "arn:tg", "containerName": "web", "containerPort": 80}
    ]


def test_wave_default_zero():
    doc = {
        "apiVersion": "andro-cd/v1", "kind": "ECSService",
        "metadata": {"name": "app"},
        "spec": {"cluster": "c", "network": {"subnets": ["s"]},
                 "taskDefinition": {"containers": [{"name": "c", "image": "i"}]}},
    }
    assert Manifest.model_validate(doc).spec.wave == 0

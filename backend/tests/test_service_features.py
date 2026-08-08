"""Tests for the second-half feature batch: session refresh, ALB request-count
autoscaling, Service Connect, EFS volumes + FireLens."""
import pytest
from pydantic import ValidationError

from app.models import Manifest
from app.reconciler import (
    _norm_container,
    _norm_service_connect,
    _norm_taskdef,
    _service_connect_changes,
    _service_connect_config,
    _volume_definition,
    desired_container_definitions,
)


def make_manifest(**spec_overrides) -> Manifest:
    spec = {
        "cluster": "test",
        "region": "eu-central-1",
        "network": {"subnets": ["subnet-1"]},
        "taskDefinition": {"containers": [{"name": "web", "image": "nginx:1"}]},
    }
    spec.update(spec_overrides)
    return Manifest.model_validate({
        "apiVersion": "andro-cd/v1", "kind": "ECSService",
        "metadata": {"name": "test-app"}, "spec": spec,
    })


# ---------- ALB request-count autoscaling ----------

def test_target_requests_requires_load_balancer():
    with pytest.raises(ValidationError):
        make_manifest(service={
            "autoscaling": {"minCount": 1, "maxCount": 5, "targetRequestsPerTarget": 100},
        })


def test_target_requests_accepted_with_managed_lb():
    m = make_manifest(service={
        "autoscaling": {"minCount": 1, "maxCount": 5, "targetRequestsPerTarget": 250.0},
        "loadBalancer": {
            "containerPort": 80,
            "create": {
                "listenerArn": "arn:aws:elasticloadbalancing:us-east-1:123:listener/app/lb/abc/def",
                "rule": {"priority": 10, "pathPattern": "/api/*"},
            },
        },
    })
    assert m.spec.service.autoscaling.targetRequestsPerTarget == 250.0


def test_target_requests_accepted_with_referenced_tg():
    m = make_manifest(service={
        "autoscaling": {"minCount": 1, "maxCount": 5, "targetRequestsPerTarget": 100},
        "loadBalancer": {
            "containerPort": 80,
            "targetGroupArn": "arn:aws:elasticloadbalancing:us-east-1:123:targetgroup/tg/xyz",
        },
    })
    assert m.spec.service.autoscaling.targetRequestsPerTarget == 100


# ---------- Service Connect ----------

def test_service_connect_config_none_when_not_specified():
    m = make_manifest()
    assert _service_connect_config(m) is None


def test_service_connect_config_builds_full_block():
    m = make_manifest(service={
        "serviceConnect": {
            "enabled": True,
            "namespace": "svc.local",
            "services": [
                {"portName": "http", "discoveryName": "api",
                 "clientAliases": [{"port": 8080, "dnsName": "api"}]},
            ],
        },
    })
    cfg = _service_connect_config(m)
    assert cfg["enabled"] is True
    assert cfg["namespace"] == "svc.local"
    assert cfg["services"][0]["portName"] == "http"
    assert cfg["services"][0]["discoveryName"] == "api"
    assert cfg["services"][0]["clientAliases"] == [{"port": 8080, "dnsName": "api"}]


def test_service_connect_no_churn_when_manifest_omits_and_live_is_disabled():
    m = make_manifest()
    live_service = {"serviceConnectConfiguration": {"enabled": False}}
    assert _service_connect_changes(m, live_service) == []


def test_service_connect_diff_when_enabling():
    m = make_manifest(service={"serviceConnect": {"enabled": True, "namespace": "svc.local"}})
    live_service = {"serviceConnectConfiguration": {"enabled": False}}
    changes = _service_connect_changes(m, live_service)
    assert changes and "serviceConnect" in changes[0]


def test_service_connect_client_alias_order_independent():
    a = {
        "enabled": True, "namespace": "n",
        "services": [{"portName": "http",
                      "clientAliases": [{"port": 8080}, {"port": 8081}]}],
    }
    b = {
        "enabled": True, "namespace": "n",
        "services": [{"portName": "http",
                      "clientAliases": [{"port": 8081}, {"port": 8080}]}],
    }
    assert _norm_service_connect(a) == _norm_service_connect(b)


# ---------- EFS volumes ----------

def test_volume_without_efs_rejected():
    with pytest.raises(ValidationError):
        Manifest.model_validate({
            "apiVersion": "andro-cd/v1", "kind": "ECSService",
            "metadata": {"name": "efs-app"},
            "spec": {
                "cluster": "c", "region": "us-east-1",
                "network": {"subnets": ["s"]},
                "taskDefinition": {
                    "containers": [{"name": "web", "image": "nginx"}],
                    "volumes": [{"name": "data"}],
                },
            },
        })


def test_mount_point_referencing_unknown_volume_rejected():
    with pytest.raises(ValidationError):
        Manifest.model_validate({
            "apiVersion": "andro-cd/v1", "kind": "ECSService",
            "metadata": {"name": "efs-app"},
            "spec": {
                "cluster": "c", "region": "us-east-1",
                "network": {"subnets": ["s"]},
                "taskDefinition": {
                    "containers": [{
                        "name": "web", "image": "nginx",
                        "mountPoints": [{"sourceVolume": "typo", "containerPath": "/data"}],
                    }],
                },
            },
        })


def test_efs_volume_definition_shape():
    m = make_manifest(taskDefinition={
        "containers": [{
            "name": "web", "image": "nginx",
            "mountPoints": [{"sourceVolume": "data", "containerPath": "/data"}],
        }],
        "volumes": [{
            "name": "data",
            "efs": {
                "fileSystemId": "fs-abc",
                "rootDirectory": "/app",
                "transitEncryption": True,
                "authorizationConfig": {"accessPointId": "fsap-1", "iam": True},
            },
        }],
    })
    vd = _volume_definition(m.spec.taskDefinition.volumes[0])
    assert vd["name"] == "data"
    assert vd["efsVolumeConfiguration"]["fileSystemId"] == "fs-abc"
    assert vd["efsVolumeConfiguration"]["rootDirectory"] == "/app"
    assert vd["efsVolumeConfiguration"]["transitEncryption"] == "ENABLED"
    assert vd["efsVolumeConfiguration"]["authorizationConfig"] == {
        "accessPointId": "fsap-1", "iam": "ENABLED",
    }
    # mountPoints propagated into the container definition
    defs = desired_container_definitions(m, "us-east-1")
    assert defs[0]["mountPoints"] == [
        {"sourceVolume": "data", "containerPath": "/data", "readOnly": False},
    ]


def test_taskdef_volume_normalization_stable_across_describe():
    """AWS describe returns extra keys and default-ish values — our normalization
    must ignore them so a fresh diff after apply reports no change."""
    live = {
        "cpu": "256", "memory": "512", "networkMode": "awsvpc",
        "containerDefinitions": [{"name": "web", "image": "nginx"}],
        "volumes": [{
            "name": "data",
            "efsVolumeConfiguration": {
                "fileSystemId": "fs-abc",
                "rootDirectory": "/",
                "transitEncryption": "ENABLED",
                "authorizationConfig": {"iam": "DISABLED"},
            },
        }],
    }
    desired = {
        "cpu": "256", "memory": "512", "networkMode": "awsvpc",
        "containerDefinitions": [{"name": "web", "image": "nginx"}],
        "volumes": [{
            "name": "data",
            "efsVolumeConfiguration": {
                "fileSystemId": "fs-abc",
                "rootDirectory": "/",
                "transitEncryption": "ENABLED",
                "authorizationConfig": {"iam": "DISABLED"},
            },
        }],
    }
    assert _norm_taskdef(live)["volumes"] == _norm_taskdef(desired)["volumes"]


# ---------- FireLens ----------

def test_firelens_container_definition():
    m = make_manifest(taskDefinition={"containers": [
        {"name": "web", "image": "nginx", "logConfiguration": {
            "logDriver": "awsfirelens",
            "options": {"Name": "cloudwatch"},
        }},
        {"name": "log-router", "image": "amazon/aws-for-fluent-bit:latest",
         "firelensConfiguration": {"type": "fluentbit"}, "essential": False},
    ]})
    defs = desired_container_definitions(m, "us-east-1")
    web = next(d for d in defs if d["name"] == "web")
    router = next(d for d in defs if d["name"] == "log-router")
    assert web["logConfiguration"]["logDriver"] == "awsfirelens"
    assert router["firelensConfiguration"] == {"type": "fluentbit"}


def test_firelens_options_normalization():
    """Options coming back from describe may be in any order — normalization sorts."""
    live = {"name": "r", "firelensConfiguration": {
        "type": "fluentbit", "options": {"b": "2", "a": "1"}}}
    desired = {"name": "r", "firelensConfiguration": {
        "type": "fluentbit", "options": {"a": "1", "b": "2"}}}
    assert _norm_container(live)["firelensConfiguration"] == _norm_container(desired)["firelensConfiguration"]


def test_firelens_type_rejected_when_invalid():
    with pytest.raises(ValidationError):
        Manifest.model_validate({
            "apiVersion": "andro-cd/v1", "kind": "ECSService",
            "metadata": {"name": "app"},
            "spec": {
                "cluster": "c", "region": "us-east-1",
                "network": {"subnets": ["s"]},
                "taskDefinition": {"containers": [{
                    "name": "r", "image": "x",
                    "firelensConfiguration": {"type": "logspout"},
                }]},
            },
        })

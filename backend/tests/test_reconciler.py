import copy

from app.models import Manifest
from app.reconciler import (
    _deploy_config_changes,
    _deployment_configuration,
    _register_kwargs,
    _taskdef_changes,
)

REGION = "eu-central-1"


def make_manifest(**overrides) -> Manifest:
    doc = {
        "apiVersion": "andro-cd/v1",
        "kind": "ECSService",
        "metadata": {"name": "web-app"},
        "spec": {
            "region": REGION,
            "cluster": "production",
            "service": {"desiredCount": 2, "launchType": "FARGATE", "assignPublicIp": True},
            "network": {"subnets": ["subnet-a"], "securityGroups": ["sg-1"]},
            "taskDefinition": {
                "cpu": "256",
                "memory": "512",
                "executionRoleArn": "arn:aws:iam::123:role/exec",
                "containers": [
                    {
                        "name": "web",
                        "image": "nginx:1.27",
                        "portMappings": [80],
                        "environment": {"B_VAR": "2", "A_VAR": "1"},
                        "secrets": {"DB_PASS": "arn:aws:ssm:eu:123:parameter/p"},
                        "logGroup": "/ecs/web-app",
                    }
                ],
            },
        },
    }
    for key, value in overrides.items():
        node = doc
        parts = key.split(".")
        for p in parts[:-1]:
            node = node[p]
        node[parts[-1]] = value
    return Manifest.model_validate(doc)


def as_live_taskdef(m: Manifest) -> dict:
    """Simulate what ECS describe_task_definition returns for our registered kwargs."""
    live = copy.deepcopy(_register_kwargs(m, REGION))
    live["taskDefinitionArn"] = f"arn:aws:ecs:{REGION}:123:task-definition/web-app:7"
    live["revision"] = 7
    live["status"] = "ACTIVE"
    return live


def test_no_changes_when_live_matches_desired():
    m = make_manifest()
    assert _taskdef_changes(as_live_taskdef(m), _register_kwargs(m, REGION)) == []


def test_environment_order_is_irrelevant():
    m = make_manifest()
    live = as_live_taskdef(m)
    live["containerDefinitions"][0]["environment"].reverse()
    assert _taskdef_changes(live, _register_kwargs(m, REGION)) == []


def test_image_change_is_detected():
    m = make_manifest()
    live = as_live_taskdef(m)
    m2 = make_manifest(**{"spec.taskDefinition.containers": [
        {**m.spec.taskDefinition.containers[0].model_dump(exclude_none=True), "image": "nginx:1.28"}
    ]})
    changes = _taskdef_changes(live, _register_kwargs(m2, REGION))
    assert len(changes) == 1
    assert "image" in changes[0] and "nginx:1.28" in changes[0]


def test_env_value_change_is_detected():
    m = make_manifest()
    live = as_live_taskdef(m)
    live["containerDefinitions"][0]["environment"] = [
        {"name": "A_VAR", "value": "1"},
        {"name": "B_VAR", "value": "OLD"},
    ]
    changes = _taskdef_changes(live, _register_kwargs(m, REGION))
    assert len(changes) == 1
    assert "environment" in changes[0]


def test_cpu_change_is_detected():
    m = make_manifest()
    live = as_live_taskdef(m)
    m2 = make_manifest(**{"spec.taskDefinition.cpu": "512"})
    changes = _taskdef_changes(live, _register_kwargs(m2, REGION))
    assert changes == ["taskDefinition.cpu: 256 -> 512"]


def test_container_added_and_removed():
    m = make_manifest()
    live = as_live_taskdef(m)
    live["containerDefinitions"].append({"name": "sidecar", "image": "envoy:v1"})
    changes = _taskdef_changes(live, _register_kwargs(m, REGION))
    assert changes == ["container 'sidecar' will be removed"]

    changes = _taskdef_changes(
        {**live, "containerDefinitions": []}, _register_kwargs(m, REGION)
    )
    assert changes == ["container 'web' will be added"]


def test_port_int_and_dict_forms_are_equivalent():
    m1 = make_manifest()
    m2 = make_manifest(**{
        "spec.taskDefinition.containers": [
            {
                "name": "web", "image": "nginx:1.27",
                "portMappings": [{"containerPort": 80, "protocol": "tcp"}],
                "environment": {"A_VAR": "1", "B_VAR": "2"},
                "secrets": {"DB_PASS": "arn:aws:ssm:eu:123:parameter/p"},
                "logGroup": "/ecs/web-app",
            }
        ]
    })
    assert _taskdef_changes(as_live_taskdef(m1), _register_kwargs(m2, REGION)) == []


def test_deployment_configuration_defaults_to_circuit_breaker():
    m = make_manifest()
    dc = _deployment_configuration(m)
    assert dc["deploymentCircuitBreaker"] == {"enable": True, "rollback": True}
    assert "minimumHealthyPercent" not in dc


def test_deploy_config_change_detected_when_breaker_missing_live():
    m = make_manifest()
    service = {"deploymentConfiguration": {"minimumHealthyPercent": 100, "maximumPercent": 200}}
    changes = _deploy_config_changes(m, service)
    assert len(changes) == 1 and "circuitBreaker" in changes[0]

    service["deploymentConfiguration"]["deploymentCircuitBreaker"] = {"enable": True, "rollback": True}
    assert _deploy_config_changes(m, service) == []


def test_secrets_are_sorted_and_compared():
    m = make_manifest()
    live = as_live_taskdef(m)
    live["containerDefinitions"][0]["secrets"] = [
        {"name": "DB_PASS", "valueFrom": "arn:aws:ssm:eu:123:parameter/DIFFERENT"}
    ]
    changes = _taskdef_changes(live, _register_kwargs(m, REGION))
    assert len(changes) == 1 and "secrets" in changes[0]

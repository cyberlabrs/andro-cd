"""Tests for ECS native blue/green, canary and linear deployment strategies."""
import pytest
from pydantic import ValidationError

from app.models import Manifest
from app.reconciler import (
    _deploy_config_changes,
    _deployment_configuration,
    _deployment_controller,
    _lb_key,
    _load_balancers,
)


BASE_LB = {
    "containerPort": 80,
    "targetGroupArn": "arn:...:targetgroup/blue/aaa",
    "alternateTargetGroupArn": "arn:...:targetgroup/green/bbb",
    "productionListenerRule": "arn:...:listener-rule/prod/111",
}


def make_manifest(*, strategy=None, lb=None, **spec_overrides) -> Manifest:
    service = {}
    if strategy is not None:
        service["deploymentStrategy"] = strategy
    if lb is not None:
        service["loadBalancer"] = lb
    spec = {
        "cluster": "test",
        "region": "eu-central-1",
        "network": {"subnets": ["subnet-1"]},
        "taskDefinition": {"containers": [{"name": "web", "image": "nginx:1"}]},
    }
    if service:
        spec["service"] = service
    spec.update(spec_overrides)
    return Manifest.model_validate({
        "apiVersion": "andro-cd/v1", "kind": "ECSService",
        "metadata": {"name": "test-app"}, "spec": spec,
    })


# ---------- validation ----------

def test_rolling_is_default_and_needs_no_extra_lb():
    m = make_manifest()  # no strategy
    assert m.spec.service.deploymentStrategy is None


def test_blue_green_requires_alt_tg_and_prod_rule():
    with pytest.raises(ValidationError, match="alternateTargetGroupArn"):
        make_manifest(strategy={"type": "BLUE_GREEN"},
                      lb={"containerPort": 80,
                          "targetGroupArn": "arn:...:targetgroup/only/aaa"})


def test_canary_requires_percent():
    with pytest.raises(ValidationError, match="canaryPercent"):
        make_manifest(strategy={"type": "CANARY"}, lb=BASE_LB)


def test_canary_percent_out_of_range_rejected():
    with pytest.raises(ValidationError, match="between 0 and 100"):
        make_manifest(strategy={"type": "CANARY", "canaryPercent": 150}, lb=BASE_LB)


def test_linear_requires_step_percent():
    with pytest.raises(ValidationError, match="linearStepPercent"):
        make_manifest(strategy={"type": "LINEAR"}, lb=BASE_LB)


def test_invalid_strategy_type_rejected():
    with pytest.raises(ValidationError, match="BLUE_GREEN"):
        make_manifest(strategy={"type": "SILLY"}, lb=BASE_LB)


def test_lifecycle_hook_requires_stages():
    with pytest.raises(ValidationError, match="at least one stage"):
        make_manifest(strategy={
            "type": "BLUE_GREEN",
            "lifecycleHooks": [{
                "targetType": "AWS_LAMBDA",
                "hookTargetArn": "arn:aws:lambda:us-east-1:123:function:h",
                "stages": [],
            }],
        }, lb=BASE_LB)


def test_lifecycle_hook_invalid_stage_rejected():
    with pytest.raises(ValidationError, match="POST_APOCALYPSE"):
        make_manifest(strategy={
            "type": "BLUE_GREEN",
            "lifecycleHooks": [{
                "targetType": "AWS_LAMBDA",
                "hookTargetArn": "arn:aws:lambda:us-east-1:123:function:h",
                "stages": ["POST_APOCALYPSE"],
            }],
        }, lb=BASE_LB)


def test_aws_lambda_hook_requires_arn():
    with pytest.raises(ValidationError, match="hookTargetArn is required"):
        make_manifest(strategy={
            "type": "BLUE_GREEN",
            "lifecycleHooks": [{
                "targetType": "AWS_LAMBDA",
                "stages": ["POST_SCALE_UP"],
            }],
        }, lb=BASE_LB)


def test_pause_hook_accepted_without_arn():
    m = make_manifest(strategy={
        "type": "BLUE_GREEN",
        "lifecycleHooks": [{
            "targetType": "PAUSE",
            "stages": ["POST_TEST_TRAFFIC_SHIFT"],
        }],
    }, lb=BASE_LB)
    assert m.spec.service.deploymentStrategy.lifecycleHooks[0].targetType == "PAUSE"


# ---------- reconciler shape ----------

def test_deployment_controller_none_for_rolling():
    m = make_manifest()
    assert _deployment_controller(m) is None


def test_deployment_controller_ecs_for_blue_green():
    m = make_manifest(strategy={"type": "BLUE_GREEN"}, lb=BASE_LB)
    assert _deployment_controller(m) == {"type": "ECS"}


def test_deployment_configuration_includes_strategy_and_bake_time():
    m = make_manifest(strategy={"type": "BLUE_GREEN", "bakeTimeMinutes": 15}, lb=BASE_LB)
    dc = _deployment_configuration(m)
    assert dc["strategy"] == "BLUE_GREEN"
    assert dc["bakeTimeInMinutes"] == 15


def test_canary_configuration_shape():
    m = make_manifest(strategy={
        "type": "CANARY", "canaryPercent": 10, "canaryBakeTimeMinutes": 5, "bakeTimeMinutes": 20,
    }, lb=BASE_LB)
    dc = _deployment_configuration(m)
    assert dc["canaryConfiguration"] == {"canaryPercent": 10.0, "canaryBakeTimeInMinutes": 5}
    assert dc["bakeTimeInMinutes"] == 20


def test_linear_configuration_shape():
    m = make_manifest(strategy={
        "type": "LINEAR", "linearStepPercent": 20, "linearStepBakeTimeMinutes": 3,
    }, lb=BASE_LB)
    dc = _deployment_configuration(m)
    assert dc["linearConfiguration"] == {"stepPercent": 20.0, "stepBakeTimeInMinutes": 3}


def test_alarms_config_shape():
    m = make_manifest(strategy={
        "type": "BLUE_GREEN",
        "alarms": {"alarmNames": ["api-5xx", "api-latency"], "rollback": True, "enable": True},
    }, lb=BASE_LB)
    dc = _deployment_configuration(m)
    assert dc["alarms"]["alarmNames"] == ["api-5xx", "api-latency"]
    assert dc["alarms"]["rollback"] is True
    assert dc["alarms"]["enable"] is True


def test_lifecycle_hook_shape_with_timeout():
    m = make_manifest(strategy={
        "type": "BLUE_GREEN",
        "lifecycleHooks": [{
            "targetType": "AWS_LAMBDA",
            "hookTargetArn": "arn:aws:lambda:us-east-1:123:function:validate",
            "roleArn": "arn:aws:iam::123:role/ecs-hook",
            "stages": ["POST_TEST_TRAFFIC_SHIFT", "POST_PRODUCTION_TRAFFIC_SHIFT"],
            "timeoutMinutes": 10,
            "timeoutAction": "ROLLBACK",
        }],
    }, lb=BASE_LB)
    hook = _deployment_configuration(m)["lifecycleHooks"][0]
    assert hook["targetType"] == "AWS_LAMBDA"
    assert hook["hookTargetArn"] == "arn:aws:lambda:us-east-1:123:function:validate"
    assert set(hook["lifecycleStages"]) == {"POST_TEST_TRAFFIC_SHIFT", "POST_PRODUCTION_TRAFFIC_SHIFT"}
    assert hook["timeoutConfiguration"] == {"timeoutInMinutes": 10, "action": "ROLLBACK"}


def test_load_balancer_advanced_configuration():
    m = make_manifest(strategy={"type": "BLUE_GREEN"}, lb={
        **BASE_LB,
        "testListenerRule": "arn:...:listener-rule/test/222",
        "roleArn": "arn:aws:iam::123:role/ELBBlueGreen",
    })
    lbs = _load_balancers(m)
    assert lbs[0]["targetGroupArn"] == BASE_LB["targetGroupArn"]
    adv = lbs[0]["advancedConfiguration"]
    assert adv["alternateTargetGroupArn"] == BASE_LB["alternateTargetGroupArn"]
    assert adv["productionListenerRule"] == BASE_LB["productionListenerRule"]
    assert adv["testListenerRule"] == "arn:...:listener-rule/test/222"
    assert adv["roleArn"] == "arn:aws:iam::123:role/ELBBlueGreen"


def test_lb_key_detects_alt_tg_drift():
    """Two LB attachments with the same TG but different alternate TGs must not
    compare equal — otherwise blue/green rewiring wouldn't be caught in diff."""
    a = {"targetGroupArn": "tg-a", "containerName": "web", "containerPort": 80,
         "advancedConfiguration": {"alternateTargetGroupArn": "tg-green-1"}}
    b = {"targetGroupArn": "tg-a", "containerName": "web", "containerPort": 80,
         "advancedConfiguration": {"alternateTargetGroupArn": "tg-green-2"}}
    assert _lb_key(a) != _lb_key(b)


# ---------- diff ----------

def test_deploy_config_changes_flags_new_strategy():
    m = make_manifest(strategy={"type": "BLUE_GREEN", "bakeTimeMinutes": 10}, lb=BASE_LB)
    live_service = {"deploymentConfiguration": {
        "deploymentCircuitBreaker": {"enable": True, "rollback": True},
        "strategy": "ROLLING",
    }}
    changes = _deploy_config_changes(m, live_service)
    assert any("deploymentStrategy: ROLLING -> BLUE_GREEN" in c for c in changes)
    assert any("bakeTimeInMinutes" in c for c in changes)


def test_deploy_config_changes_no_churn_without_strategy():
    m = make_manifest()
    live_service = {"deploymentConfiguration": {
        "deploymentCircuitBreaker": {"enable": True, "rollback": True},
        "strategy": "ROLLING",  # AWS may return it even if we didn't set it
    }}
    assert _deploy_config_changes(m, live_service) == []


def test_deploy_config_changes_flags_alarm_names():
    m = make_manifest(strategy={
        "type": "BLUE_GREEN",
        "alarms": {"alarmNames": ["api-5xx"], "rollback": True, "enable": True},
    }, lb=BASE_LB)
    live_service = {"deploymentConfiguration": {
        "deploymentCircuitBreaker": {"enable": True, "rollback": True},
        "strategy": "BLUE_GREEN",
        "alarms": {"alarmNames": ["api-latency"], "rollback": True, "enable": True},
    }}
    changes = _deploy_config_changes(m, live_service)
    assert any("alarms" in c for c in changes)


def test_deploy_config_changes_flags_lifecycle_hooks():
    m = make_manifest(strategy={
        "type": "BLUE_GREEN",
        "lifecycleHooks": [{
            "targetType": "AWS_LAMBDA",
            "hookTargetArn": "arn:aws:lambda:us-east-1:123:function:validate",
            "stages": ["POST_TEST_TRAFFIC_SHIFT"],
        }],
    }, lb=BASE_LB)
    live_service = {"deploymentConfiguration": {
        "deploymentCircuitBreaker": {"enable": True, "rollback": True},
        "strategy": "BLUE_GREEN",
    }}
    changes = _deploy_config_changes(m, live_service)
    assert any("lifecycleHooks" in c for c in changes)

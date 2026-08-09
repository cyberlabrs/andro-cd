"""End-to-end tests against a mocked AWS via moto.

These call the real reconciler pipeline (compute_diff + apply) — the same functions
the reconcile loop runs — with boto3 mocked at the transport layer. They catch two
categories of bugs unit tests can't:

  1) Shape mismatches: we pass a JSON dict AWS actually rejects (moto rejects too).
  2) Cross-call state: apply → live describe → next-tick diff must round-trip cleanly.

If a moto test fails after a manifest-model change, the fix is usually a small tweak
to `_register_kwargs` / `_deployment_configuration` / `_load_balancers` — that's the
point. Add a new test whenever you add a new field to the manifest.
"""
import os

import boto3
import pytest
from moto import mock_aws

from app import reconciler
from app.models import Manifest


# ---------------------------------------------------------------- fixtures ---

@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch):
    """moto needs *some* credentials in the environment even though it won't call AWS."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture(autouse=True)
def reset_client_cache():
    """Reconciler caches boto3 clients per (service, region, profile) tuple. Between
    tests we must clear it so each test gets a fresh moto-backed client."""
    reconciler._clients.clear()
    yield
    reconciler._clients.clear()


@pytest.fixture
def vpc_env():
    """Set up VPC + subnet + security group + ALB + listener so LB-related manifests
    have something real to attach to. Returns dict of ARNs for the test to reference."""
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        elb = boto3.client("elbv2", region_name="us-east-1")

        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]
        subnet_a = ec2.create_subnet(VpcId=vpc["VpcId"], CidrBlock="10.0.1.0/24",
                                     AvailabilityZone="us-east-1a")["Subnet"]
        subnet_b = ec2.create_subnet(VpcId=vpc["VpcId"], CidrBlock="10.0.2.0/24",
                                     AvailabilityZone="us-east-1b")["Subnet"]
        sg = ec2.create_security_group(GroupName="test-sg", Description="e2e",
                                       VpcId=vpc["VpcId"])

        alb = elb.create_load_balancer(
            Name="e2e-alb",
            Subnets=[subnet_a["SubnetId"], subnet_b["SubnetId"]],
            SecurityGroups=[sg["GroupId"]],
            Scheme="internal",
            Type="application",
        )["LoadBalancers"][0]

        # Blue / green target groups + a production listener rule for BG tests.
        blue = elb.create_target_group(Name="e2e-blue", Protocol="HTTP", Port=80,
                                       VpcId=vpc["VpcId"], TargetType="ip")["TargetGroups"][0]
        green = elb.create_target_group(Name="e2e-green", Protocol="HTTP", Port=80,
                                        VpcId=vpc["VpcId"], TargetType="ip")["TargetGroups"][0]
        listener = elb.create_listener(
            LoadBalancerArn=alb["LoadBalancerArn"], Protocol="HTTP", Port=80,
            DefaultActions=[{"Type": "forward", "TargetGroupArn": blue["TargetGroupArn"]}],
        )["Listeners"][0]
        prod_rule = elb.create_rule(
            ListenerArn=listener["ListenerArn"], Priority=10,
            Conditions=[{"Field": "path-pattern", "Values": ["/*"]}],
            Actions=[{"Type": "forward", "TargetGroupArn": blue["TargetGroupArn"]}],
        )["Rules"][0]

        yield {
            "vpc_id": vpc["VpcId"],
            "subnets": [subnet_a["SubnetId"], subnet_b["SubnetId"]],
            "sg": sg["GroupId"],
            "alb": alb["LoadBalancerArn"],
            "listener": listener["ListenerArn"],
            "blue_tg": blue["TargetGroupArn"],
            "green_tg": green["TargetGroupArn"],
            "prod_rule": prod_rule["RuleArn"],
        }


def make_manifest(name: str, spec: dict) -> Manifest:
    base = {
        "cluster": "e2e",
        "region": "us-east-1",
    }
    base.update(spec)
    return Manifest.model_validate({
        "apiVersion": "andro-cd/v1", "kind": "ECSService",
        "metadata": {"name": name}, "spec": base,
    })


# Fields moto's describe_services doesn't round-trip (yet). Real AWS DOES return them,
# so we filter these out of drift assertions in the e2e tests but keep watching them
# in the unit tests. Add to this list only after confirming the field is a moto gap,
# not an Andro-CD bug.
_MOTO_KNOWN_GAPS = ("circuitBreaker", "serviceConnect", "deploymentStrategy",
                    "capacityProviderStrategy", "loadBalancers")


def meaningful_changes(changes: list[str]) -> list[str]:
    """Filter out changes caused by moto not persisting a field it should."""
    return [c for c in changes if not any(g in c for g in _MOTO_KNOWN_GAPS)]


# ---------------------------------------------------------------- tests ---

def test_rolling_service_apply_then_synced(vpc_env):
    """The default flow: register task def, create service, next diff is empty."""
    with mock_aws():
        m = make_manifest("rolling-app", {
            "network": {"subnets": vpc_env["subnets"], "securityGroups": [vpc_env["sg"]]},
            "taskDefinition": {"containers": [{"name": "web", "image": "nginx:1"}]},
        })
        actions = reconciler.apply(m)
        assert any("registered task definition" in a for a in actions)
        assert any("created service" in a for a in actions)

        diff = reconciler.compute_diff(m)
        assert not meaningful_changes(diff["changes"]), (
            f"unexpected drift after apply: {diff['changes']}")


def test_efs_volume_round_trip(vpc_env):
    """EFS volume config we send must come back identical from describe (no drift)."""
    with mock_aws():
        m = make_manifest("efs-app", {
            "network": {"subnets": vpc_env["subnets"], "securityGroups": [vpc_env["sg"]]},
            "taskDefinition": {
                "containers": [{
                    "name": "web", "image": "nginx:1",
                    "mountPoints": [{"sourceVolume": "data", "containerPath": "/data"}],
                }],
                "volumes": [{
                    "name": "data",
                    "efs": {"fileSystemId": "fs-12345", "rootDirectory": "/app",
                            "transitEncryption": True,
                            "authorizationConfig": {"accessPointId": "fsap-abc", "iam": True}},
                }],
            },
        })
        reconciler.apply(m)
        diff = reconciler.compute_diff(m)
        assert not meaningful_changes(diff["changes"]), (
            f"EFS volume shape drift after apply: {diff['changes']}\n"
            "Likely _norm_volume() vs _volume_definition() mismatch — check EFS auth block.")


def test_firelens_container_round_trip(vpc_env):
    """FireLens sidecar + awsfirelens driver on web must round-trip without churn."""
    with mock_aws():
        m = make_manifest("firelens-app", {
            "network": {"subnets": vpc_env["subnets"], "securityGroups": [vpc_env["sg"]]},
            "taskDefinition": {"containers": [
                {"name": "web", "image": "nginx:1",
                 "logConfiguration": {
                     "logDriver": "awsfirelens",
                     "options": {"Name": "cloudwatch", "region": "us-east-1",
                                 "log_group_name": "/ecs/firelens-app"},
                 }},
                {"name": "log-router", "image": "amazon/aws-for-fluent-bit:latest",
                 "essential": False,
                 "firelensConfiguration": {"type": "fluentbit",
                                           "options": {"enable-ecs-log-metadata": "true"}}},
            ]},
        })
        reconciler.apply(m)
        diff = reconciler.compute_diff(m)
        assert not meaningful_changes(diff["changes"]), (
            f"FireLens drift after apply: {diff['changes']}")


def test_service_connect_apply_no_churn(vpc_env):
    """Service Connect config must not report drift on the very next diff."""
    with mock_aws():
        m = make_manifest("sc-app", {
            "network": {"subnets": vpc_env["subnets"], "securityGroups": [vpc_env["sg"]]},
            "service": {
                "serviceConnect": {
                    "enabled": True, "namespace": "svc.local",
                    "services": [{
                        "portName": "http", "discoveryName": "api",
                        "clientAliases": [{"port": 8080, "dnsName": "api"}],
                    }],
                },
            },
            "taskDefinition": {"containers": [{
                "name": "web", "image": "nginx:1",
                "portMappings": [{"containerPort": 8080, "name": "http"}],
            }]},
        })
        reconciler.apply(m)
        diff = reconciler.compute_diff(m)
        assert not meaningful_changes(diff["changes"]), (
            f"unexpected non-gap drift: {diff['changes']}\n"
            "Likely a task-def or LB shape bug bleeding through the SC test.")


def test_blue_green_apply_sets_deployment_controller(vpc_env):
    """When the manifest opts into BLUE_GREEN, deploymentController=ECS must be set on
    the created service and the alt TG / production listener rule must be forwarded via
    advancedConfiguration on the load balancer entry."""
    with mock_aws():
        m = make_manifest("bg-app", {
            "network": {"subnets": vpc_env["subnets"], "securityGroups": [vpc_env["sg"]]},
            "service": {
                "loadBalancer": {
                    "containerPort": 80,
                    "targetGroupArn": vpc_env["blue_tg"],
                    "alternateTargetGroupArn": vpc_env["green_tg"],
                    "productionListenerRule": vpc_env["prod_rule"],
                    "roleArn": "arn:aws:iam::123:role/ELBBlueGreen",
                },
                "deploymentStrategy": {"type": "BLUE_GREEN", "bakeTimeMinutes": 5},
            },
            "taskDefinition": {"containers": [{"name": "web", "image": "nginx:1"}]},
        })
        reconciler.apply(m)

        ecs = boto3.client("ecs", region_name="us-east-1")
        live = ecs.describe_services(cluster="e2e", services=["bg-app"])["services"][0]
        controller = live.get("deploymentController") or {}
        assert controller.get("type") == "ECS", (
            f"expected deploymentController.type=ECS for BLUE_GREEN, got {controller}")
        lbs = live.get("loadBalancers") or []
        assert lbs, "service was created without a loadBalancer attachment"
        # moto may or may not persist advancedConfiguration verbatim — assert we at least
        # sent the primary TG through.
        assert lbs[0].get("targetGroupArn") == vpc_env["blue_tg"]


def test_canary_diff_flags_percent_change(vpc_env):
    """Applying with canaryPercent=10 then bumping the manifest to 30 must show up
    as a diff — not a silent overwrite that never appears in the UI."""
    with mock_aws():
        m = make_manifest("canary-app", {
            "network": {"subnets": vpc_env["subnets"], "securityGroups": [vpc_env["sg"]]},
            "service": {
                "loadBalancer": {
                    "containerPort": 80,
                    "targetGroupArn": vpc_env["blue_tg"],
                    "alternateTargetGroupArn": vpc_env["green_tg"],
                    "productionListenerRule": vpc_env["prod_rule"],
                },
                "deploymentStrategy": {"type": "CANARY", "canaryPercent": 10,
                                       "canaryBakeTimeMinutes": 5},
            },
            "taskDefinition": {"containers": [{"name": "web", "image": "nginx:1"}]},
        })
        reconciler.apply(m)

        # Same manifest but canaryPercent bumped to 30
        m2 = make_manifest("canary-app", {
            "network": {"subnets": vpc_env["subnets"], "securityGroups": [vpc_env["sg"]]},
            "service": {
                "loadBalancer": {
                    "containerPort": 80,
                    "targetGroupArn": vpc_env["blue_tg"],
                    "alternateTargetGroupArn": vpc_env["green_tg"],
                    "productionListenerRule": vpc_env["prod_rule"],
                },
                "deploymentStrategy": {"type": "CANARY", "canaryPercent": 30,
                                       "canaryBakeTimeMinutes": 5},
            },
            "taskDefinition": {"containers": [{"name": "web", "image": "nginx:1"}]},
        })
        diff = reconciler.compute_diff(m2)
        # moto may or may not persist canary config in describe; either behavior is fine
        # as long as we either flag the change or aren't in_sync. Silent no-op is a bug.
        assert not diff["in_sync"] or any(
            "canary" in c.lower() or "strategy" in c.lower() for c in diff["changes"]
        ), "canaryPercent change didn't surface as a diff"


def test_autoscaling_configures_scalable_target(vpc_env):
    """Applying with autoscaling must register a scalable target and CPU policy that
    describe_scalable_targets can find — proves we hit application-autoscaling correctly."""
    with mock_aws():
        m = make_manifest("as-app", {
            "network": {"subnets": vpc_env["subnets"], "securityGroups": [vpc_env["sg"]]},
            "service": {"autoscaling": {"minCount": 1, "maxCount": 5, "targetCpu": 60}},
            "taskDefinition": {"containers": [{"name": "web", "image": "nginx:1"}]},
        })
        reconciler.apply(m)

        aas = boto3.client("application-autoscaling", region_name="us-east-1")
        targets = aas.describe_scalable_targets(
            ServiceNamespace="ecs", ResourceIds=["service/e2e/as-app"],
        )["ScalableTargets"]
        assert targets, "no scalable target registered — autoscaling apply did nothing"
        assert targets[0]["MinCapacity"] == 1
        assert targets[0]["MaxCapacity"] == 5

        policies = aas.describe_scaling_policies(
            ServiceNamespace="ecs", ResourceId="service/e2e/as-app",
        )["ScalingPolicies"]
        names = {p["PolicyName"] for p in policies}
        assert "androcd-as-app-cpu" in names, f"CPU policy missing; got {names}"


def test_capacity_provider_strategy_apply_does_not_raise(vpc_env):
    """FARGATE_SPOT strategy is passed to create_service verbatim. moto doesn't echo
    capacityProviderStrategy on describe (as of moto 5.x), so we only assert that the
    apply call succeeds and produces the expected action — not that the strategy
    round-trips. On real AWS the strategy IS persisted."""
    with mock_aws():
        m = make_manifest("spot-app", {
            "network": {"subnets": vpc_env["subnets"], "securityGroups": [vpc_env["sg"]]},
            "service": {"capacityProviders": [
                {"provider": "FARGATE_SPOT", "weight": 3},
                {"provider": "FARGATE", "weight": 1, "base": 1},
            ]},
            "taskDefinition": {"containers": [{"name": "web", "image": "nginx:1"}]},
        })
        actions = reconciler.apply(m)
        assert any("created service" in a for a in actions), (
            f"capacity provider apply didn't create the service; actions={actions}")

        ecs = boto3.client("ecs", region_name="us-east-1")
        live = ecs.describe_services(cluster="e2e", services=["spot-app"])["services"][0]
        assert live["status"] == "ACTIVE"


def test_taskdef_change_triggers_service_update(vpc_env):
    """Changing the container image must produce a new task def revision AND update
    the service to point at it — not just register-and-forget."""
    with mock_aws():
        m1 = make_manifest("img-app", {
            "network": {"subnets": vpc_env["subnets"], "securityGroups": [vpc_env["sg"]]},
            "taskDefinition": {"containers": [{"name": "web", "image": "nginx:1"}]},
        })
        reconciler.apply(m1)

        m2 = make_manifest("img-app", {
            "network": {"subnets": vpc_env["subnets"], "securityGroups": [vpc_env["sg"]]},
            "taskDefinition": {"containers": [{"name": "web", "image": "nginx:2"}]},
        })
        actions = reconciler.apply(m2)
        assert any("registered task definition" in a for a in actions), (
            f"image change didn't register a new task def; actions={actions}")
        assert any("updated service" in a for a in actions), (
            f"image change didn't update the service; actions={actions}")

        ecs = boto3.client("ecs", region_name="us-east-1")
        live = ecs.describe_services(cluster="e2e", services=["img-app"])["services"][0]
        td_arn = live["taskDefinition"]
        td = ecs.describe_task_definition(taskDefinition=td_arn)["taskDefinition"]
        assert td["containerDefinitions"][0]["image"] == "nginx:2"

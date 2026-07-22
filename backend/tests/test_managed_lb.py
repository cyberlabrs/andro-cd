"""Tests for the managed load balancer mode (loadBalancer.create: TG + listener rule)."""
import pytest
from pydantic import ValidationError

from app import reconciler
from app.models import Manifest
from app.reconciler import (_apply_managed_lb, _managed_lb_changes, _norm_conditions,
                            _prune_managed_lb, _rule_conditions, _tg_hc_changes, _tg_name)

LISTENER = "arn:aws:elasticloadbalancing:eu-central-1:123:listener/app/main/abc/def"


def make_lb_manifest(name="web-app", **create_overrides) -> Manifest:
    create = {
        "listenerArn": LISTENER,
        "rule": {"priority": 10, "pathPattern": "/api/*"},
        **create_overrides,
    }
    return Manifest.model_validate({
        "apiVersion": "andro-cd/v1", "kind": "ECSService",
        "metadata": {"name": name, "labels": {"team": "platform"}},
        "spec": {
            "region": "eu-central-1", "cluster": "prod",
            "service": {"loadBalancer": {"containerPort": 8080, "create": create}},
            "network": {"vpc": "vpc-123", "subnets": ["subnet-1"]},
            "taskDefinition": {"containers": [{"name": "web", "image": "nginx:1"}]},
        },
    })


class FakeElb:
    """Minimal elbv2 stub tracking calls; starts empty, fills on create."""

    def __init__(self, tg=None, rules=None):
        self.tg = tg
        self.rules = rules or []
        self.calls: list[str] = []

    def describe_target_groups(self, Names):
        from botocore.exceptions import ClientError
        if self.tg is None:
            raise ClientError({"Error": {"Code": "TargetGroupNotFound", "Message": "nope"}},
                              "DescribeTargetGroups")
        return {"TargetGroups": [self.tg]}

    def create_target_group(self, **kwargs):
        self.calls.append("create_target_group")
        self.tg = {"TargetGroupArn": "arn:tg/new", "TargetGroupName": kwargs["Name"],
                   "HealthCheckPath": kwargs["HealthCheckPath"],
                   "HealthCheckIntervalSeconds": kwargs["HealthCheckIntervalSeconds"],
                   "HealthCheckTimeoutSeconds": kwargs["HealthCheckTimeoutSeconds"],
                   "HealthyThresholdCount": kwargs["HealthyThresholdCount"],
                   "UnhealthyThresholdCount": kwargs["UnhealthyThresholdCount"],
                   "Matcher": kwargs["Matcher"]}
        self.created_kwargs = kwargs
        return {"TargetGroups": [self.tg]}

    def modify_target_group(self, **kwargs):
        self.calls.append("modify_target_group")

    def describe_rules(self, ListenerArn):
        return {"Rules": self.rules}

    def create_rule(self, **kwargs):
        self.calls.append("create_rule")
        self.rules.append({"RuleArn": "arn:rule/new", "IsDefault": False,
                           "Conditions": kwargs["Conditions"], "Actions": kwargs["Actions"]})

    def modify_rule(self, **kwargs):
        self.calls.append("modify_rule")

    def delete_rule(self, RuleArn):
        self.calls.append("delete_rule")

    def delete_target_group(self, TargetGroupArn):
        self.calls.append("delete_target_group")


@pytest.fixture()
def elb(monkeypatch):
    fake = FakeElb()
    monkeypatch.setattr(reconciler, "_client",
                        lambda service, region, m=None: fake if service == "elbv2"
                        else (_ for _ in ()).throw(AssertionError(f"unexpected client {service}")))
    return fake


# ---------- model validation ----------

def test_reference_mode_still_works():
    m = Manifest.model_validate({
        "apiVersion": "andro-cd/v1", "kind": "ECSService",
        "metadata": {"name": "x"},
        "spec": {"cluster": "c", "network": {"subnets": ["s"]},
                 "service": {"loadBalancer": {"targetGroupArn": "arn:tg/x", "containerPort": 80}},
                 "taskDefinition": {"containers": [{"name": "a", "image": "i"}]}},
    })
    assert m.spec.service.loadBalancer.create is None


def test_exactly_one_mode_enforced():
    with pytest.raises(ValidationError, match="exactly one"):
        Manifest.model_validate({
            "apiVersion": "andro-cd/v1", "kind": "ECSService",
            "metadata": {"name": "x"},
            "spec": {"cluster": "c", "network": {"subnets": ["s"]},
                     "service": {"loadBalancer": {"containerPort": 80}},  # neither mode
                     "taskDefinition": {"containers": [{"name": "a", "image": "i"}]}},
        })


def test_rule_needs_a_condition():
    with pytest.raises(ValidationError, match="hostHeader and/or pathPattern"):
        make_lb_manifest(rule={"priority": 5})


def test_hc_timeout_must_be_under_interval():
    with pytest.raises(ValidationError, match="timeout"):
        make_lb_manifest(healthCheck={"interval": 5, "timeout": 5})


def test_tg_name_is_bounded():
    m = make_lb_manifest(name="a-very-long-application-name-that-overflows")
    assert len(_tg_name(m)) <= 32
    assert not _tg_name(m).endswith("-")


# ---------- diff helpers ----------

def test_hc_changes_detects_drift():
    m = make_lb_manifest(healthCheck={"path": "/health", "matcher": "200"})
    live = {"HealthCheckPath": "/", "HealthCheckIntervalSeconds": 30,
            "HealthCheckTimeoutSeconds": 5, "HealthyThresholdCount": 3,
            "UnhealthyThresholdCount": 3, "Matcher": {"HttpCode": "200-399"}}
    changes = _tg_hc_changes(live, m.spec.service.loadBalancer.create)
    assert any("healthCheck.path: / -> /health" in c for c in changes)
    assert any("matcher" in c for c in changes)


def test_norm_conditions_ignores_order_and_shape():
    a = [{"Field": "host-header", "HostHeaderConfig": {"Values": ["x.com"]}},
         {"Field": "path-pattern", "PathPatternConfig": {"Values": ["/api/*"]}}]
    b = [{"Field": "path-pattern", "Values": ["/api/*"]},          # legacy shape
         {"Field": "host-header", "HostHeaderConfig": {"Values": ["x.com"]}}]
    assert _norm_conditions(a) == _norm_conditions(b)


def test_managed_changes_when_nothing_exists(elb):
    changes, tg_arn = _managed_lb_changes(make_lb_manifest(), "eu-central-1")
    assert tg_arn is None
    assert any("target group" in c and "created" in c for c in changes)
    assert any("listener rule" in c for c in changes)


# ---------- apply / prune ----------

def test_apply_creates_tg_and_rule(elb):
    m = make_lb_manifest()
    actions, tg_arn = _apply_managed_lb(m, "eu-central-1")
    assert elb.calls == ["create_target_group", "create_rule"]
    assert tg_arn == "arn:tg/new"
    assert elb.created_kwargs["TargetType"] == "ip"
    assert elb.created_kwargs["VpcId"] == "vpc-123"       # from spec.network.vpc, no EC2 call
    assert elb.created_kwargs["Port"] == 8080             # defaults to containerPort
    assert {"Key": "team", "Value": "platform"} in elb.created_kwargs["Tags"]
    # second run: everything in sync, nothing else created
    elb.calls.clear()
    changes, _ = _managed_lb_changes(m, "eu-central-1")
    assert changes == []


def test_apply_is_idempotent(elb):
    m = make_lb_manifest()
    _apply_managed_lb(m, "eu-central-1")
    elb.calls.clear()
    actions, tg_arn = _apply_managed_lb(m, "eu-central-1")
    assert actions == [] and elb.calls == []


def test_rule_condition_drift_triggers_modify(elb):
    m = make_lb_manifest()
    _apply_managed_lb(m, "eu-central-1")
    m2 = make_lb_manifest(rule={"priority": 10, "pathPattern": "/v2/*"})
    changes, _ = _managed_lb_changes(m2, "eu-central-1")
    assert any("conditions" in c for c in changes)
    elb.calls.clear()
    _apply_managed_lb(m2, "eu-central-1")
    assert "modify_rule" in elb.calls and "create_rule" not in elb.calls


def test_prune_deletes_rule_then_tg(elb):
    m = make_lb_manifest()
    _apply_managed_lb(m, "eu-central-1")
    elb.calls.clear()
    actions = _prune_managed_lb(m, "eu-central-1")
    assert elb.calls == ["delete_rule", "delete_target_group"]
    assert any("deleted target group" in a for a in actions)


def test_prune_noop_when_tg_gone(elb):
    assert _prune_managed_lb(make_lb_manifest(), "eu-central-1") == []

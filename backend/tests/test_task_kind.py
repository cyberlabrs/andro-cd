"""Tests for kind: ECSTask — one-off tasks with run-now / runOnSync."""
import pytest
from botocore.exceptions import ClientError
from pydantic import ValidationError

from app import reconciler
from app.config import settings
from app.models import Manifest
from app.reconciler import (_task_apply, _task_started_by, compute_health,
                            prune, run_task_now)


def make_task(name="migrate", run_on_sync=False, count=1, **td) -> Manifest:
    task_def = {"containers": [{"name": "job", "image": "app:v1"}], **td}
    return Manifest.model_validate({
        "apiVersion": "andro-cd/v1", "kind": "ECSTask",
        "metadata": {"name": name, "labels": {"team": "data"}},
        "spec": {
            "region": "eu-central-1", "cluster": "batch",
            "service": {"launchType": "FARGATE"},
            "network": {"subnets": ["subnet-1"], "securityGroups": ["sg-1"]},
            "taskDefinition": task_def,
            "runPolicy": {"runOnSync": run_on_sync, "count": count},
        },
    })


class FakeEcs:
    """ECS stub: cluster exists, task def registers on demand, records run_task calls."""

    def __init__(self):
        self.td = None
        self.run_calls: list[dict] = []

    def describe_clusters(self, clusters, include=None):
        return {"clusters": [{"clusterName": clusters[0], "status": "ACTIVE",
                              "runningTasksCount": 0, "activeServicesCount": 0}]}

    def describe_task_definition(self, taskDefinition):
        if self.td is None:
            raise ClientError({"Error": {"Code": "ClientException", "Message": "not found"}},
                              "DescribeTaskDefinition")
        return {"taskDefinition": self.td}

    def register_task_definition(self, **kwargs):
        rev = (self.td["revision"] + 1) if self.td else 1
        self.td = {
            "taskDefinitionArn": f"arn:aws:ecs:eu:1:task-definition/{kwargs['family']}:{rev}",
            "revision": rev, "family": kwargs["family"],
            "containerDefinitions": kwargs["containerDefinitions"],
            "cpu": kwargs.get("cpu"), "memory": kwargs.get("memory"),
            "networkMode": kwargs.get("networkMode"),
        }
        return {"taskDefinition": self.td}

    def run_task(self, **kwargs):
        self.run_calls.append(kwargs)
        return {"tasks": [{"taskArn": f"arn:aws:ecs:eu:1:task/batch/{i}"} for i in range(kwargs["count"])],
                "failures": []}

    def list_tasks(self, **kwargs):
        return {"taskArns": []}

    def describe_tasks(self, **kwargs):
        return {"tasks": []}


@pytest.fixture()
def ecs(monkeypatch):
    fake = FakeEcs()
    monkeypatch.setattr(reconciler, "_client", lambda service, region, m=None: fake)
    monkeypatch.setattr(settings, "keep_taskdef_revisions", 0)
    return fake


# ---------- model ----------

def test_ecstask_valid_and_in_schema():
    m = make_task()
    assert m.kind == "ECSTask"
    schema = Manifest.model_json_schema()
    assert "ECSTask" in schema["properties"]["kind"]["enum"]


def test_ecstask_requires_network_and_taskdef():
    with pytest.raises(ValidationError, match="spec.network is required"):
        Manifest.model_validate({
            "apiVersion": "andro-cd/v1", "kind": "ECSTask",
            "metadata": {"name": "x"},
            "spec": {"cluster": "c",
                     "taskDefinition": {"containers": [{"name": "a", "image": "i"}]}},
        })


def test_runpolicy_count_bounds():
    with pytest.raises(ValidationError):
        make_task(count=0)
    with pytest.raises(ValidationError):
        make_task(count=11)


def test_started_by_bounded():
    assert _task_started_by(make_task(name="x")) == "androcd-task-x"
    assert len(_task_started_by(make_task(name="a" * 200))) <= 128


# ---------- apply / run ----------

def test_apply_registers_td_without_running_by_default(ecs):
    actions = _task_apply(make_task(run_on_sync=False), "eu-central-1")
    assert any("registered task definition" in a for a in actions)
    assert ecs.run_calls == []            # runOnSync off → no run


def test_apply_runs_on_sync_when_td_changes(ecs):
    actions = _task_apply(make_task(run_on_sync=True, count=2), "eu-central-1")
    assert any("runOnSync" in a for a in actions)
    assert len(ecs.run_calls) == 1
    call = ecs.run_calls[0]
    assert call["count"] == 2
    assert call["startedBy"] == "androcd-task-migrate"
    assert call["launchType"] == "FARGATE"


def test_apply_second_pass_is_noop_when_td_unchanged(ecs):
    _task_apply(make_task(run_on_sync=True), "eu-central-1")
    ecs.run_calls.clear()
    actions = _task_apply(make_task(run_on_sync=True), "eu-central-1")
    # td unchanged → no re-register, no run
    assert not any("registered" in a for a in actions)
    assert ecs.run_calls == []


def test_run_now_launches_task(ecs):
    result = run_task_now(make_task(count=3))
    assert result["started"] == 3
    assert len(ecs.run_calls) == 1
    assert ecs.run_calls[0]["count"] == 3


def test_run_now_rejects_non_task():
    svc = Manifest.model_validate({
        "apiVersion": "andro-cd/v1", "kind": "ECSService",
        "metadata": {"name": "web"},
        "spec": {"cluster": "c", "network": {"subnets": ["s"]},
                 "taskDefinition": {"containers": [{"name": "a", "image": "i"}]}},
    })
    with pytest.raises(ValueError, match="only supported for kind ECSTask"):
        run_task_now(svc)


# ---------- health ----------

def test_health_task_definition_ready():
    live = {"task": True, "taskDefinition": {"arn": "x", "revision": 1, "images": []}, "lastRun": None}
    assert compute_health(live) == ("Healthy", "task definition ready")


def test_health_reflects_last_run():
    ok = {"task": True, "taskDefinition": {"arn": "x"}, "lastRun":
          {"id": "abc123", "lastStatus": "STOPPED", "containers": [{"exitCode": 0}]}}
    assert compute_health(ok)[0] == "Healthy"
    bad = {"task": True, "taskDefinition": {"arn": "x"}, "lastRun":
           {"id": "abc", "lastStatus": "STOPPED", "containers": [{"exitCode": 1}]}}
    assert compute_health(bad)[0] == "Degraded"
    running = {"task": True, "taskDefinition": {"arn": "x"}, "lastRun":
               {"id": "abc", "lastStatus": "RUNNING", "containers": []}}
    assert compute_health(running)[0] == "Progressing"


def test_health_unknown_before_registration():
    assert compute_health({"task": True, "taskDefinition": None, "lastRun": None}) == (
        "Unknown", "task definition not registered yet")


# ---------- prune ----------

def test_prune_ecstask_is_noop(ecs):
    actions = prune(make_task())
    assert actions == ["nothing to delete for ECSTask (task definitions are kept)"]

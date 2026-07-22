"""Tests for values templating, sync windows, dry-run, capacity providers,
container health checks, task definition cleanup and leader election fallback."""
import pytest

from app import templating
from app.config import settings
from app.engine import in_sync_window
from app.models import Manifest, SyncPolicy, SyncWindow
from app.reconciler import (_capacity_strategy, _norm_container, _stale_taskdef_arns,
                            desired_container_definitions)


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


# ---------- templating ----------

def test_flatten_nested_values():
    assert templating.flatten({"image": {"tag": "v1"}, "count": 2}) == {
        "image.tag": "v1", "count": 2,
    }


def test_substitute_replaces_placeholders():
    doc = {"spec": {"image": "repo/app:${image.tag}", "count": "${count}"}}
    out = templating.substitute(doc, {"image.tag": "v9", "count": 3})
    assert out == {"spec": {"image": "repo/app:v9", "count": "3"}}


def test_values_for_closest_dir_wins():
    values_by_dir = {
        "": {"tag": "base", "region": "eu"},
        "envs/prod": {"tag": "prod"},
    }
    merged = templating.values_for("envs/prod/web.yaml", values_by_dir)
    assert merged == {"tag": "prod", "region": "eu"}
    assert templating.values_for("dev.yaml", values_by_dir) == {"tag": "base", "region": "eu"}


def test_load_manifest_docs_applies_values(tmp_path, monkeypatch):
    from app import git_sync
    monkeypatch.setattr(settings, "repos_base_dir", str(tmp_path))
    repo_root = tmp_path / "repo-1"
    (repo_root / "envs" / "prod").mkdir(parents=True)
    (repo_root / "values.yaml").write_text("tag: base\nteam: platform\n")
    (repo_root / "envs" / "prod" / "values.yaml").write_text("tag: v42\n")
    (repo_root / "envs" / "prod" / "web.yaml").write_text(
        "kind: ECSService\nmetadata:\n  name: web-${tag}\n  labels:\n    team: ${team}\n"
    )
    docs = git_sync.load_manifest_docs({"id": 1, "path": ""})
    assert len(docs) == 1   # values files are not manifests
    rel, doc = docs[0]
    assert doc["metadata"]["name"] == "web-v42"          # closest values file wins
    assert doc["metadata"]["labels"]["team"] == "platform"  # root value inherited


# ---------- sync windows ----------

MONDAY_NOON = 1750676400   # 2025-06-23 11:00 UTC (Monday)


def test_empty_sync_windows_always_allowed():
    assert in_sync_window(SyncPolicy()) is True


def test_sync_window_inside_and_outside():
    policy = SyncPolicy(syncWindows=[SyncWindow(days=["Mon"], start="09:00", end="17:00")])
    monday_11utc = 1750676400   # Mon 11:00 UTC
    monday_18utc = 1750701600   # Mon 18:00 UTC
    assert in_sync_window(policy, at=monday_11utc) is True
    assert in_sync_window(policy, at=monday_18utc) is False


def test_sync_window_wrong_day():
    policy = SyncPolicy(syncWindows=[SyncWindow(days=["Sun"], start="00:00", end="24:00")])
    assert in_sync_window(policy, at=MONDAY_NOON) is False


def test_sync_window_validation():
    with pytest.raises(Exception):
        SyncWindow(days=["Monday"])          # must be Mon..Sun
    with pytest.raises(Exception):
        SyncWindow(start="9am", end="17:00")  # must be HH:MM


# ---------- dry-run ----------

def test_dry_run_sync_never_touches_aws(monkeypatch):
    from app import engine, reconciler
    from app.state import AppState, store

    def boom(*a, **kw):
        raise AssertionError("AWS apply must not be called in dry-run")

    monkeypatch.setattr(reconciler, "apply", boom)
    monkeypatch.setattr(settings, "dry_run", True)

    app = AppState(name="test-app", file="a.yaml")
    app.manifest = make_manifest()
    app.changes = ["desiredCount: 1 -> 2"]
    with store.lock():
        store.apps["test-app"] = app
    try:
        engine._sync_app(app)
    finally:
        with store.lock():
            store.apps.pop("test-app", None)
    assert app.last_actions == ["[dry-run] desiredCount: 1 -> 2"]
    assert app.sync_status == "OutOfSync"


# ---------- capacity providers & health checks ----------

def test_capacity_strategy_from_manifest():
    m = make_manifest(service={
        "capacityProviders": [
            {"provider": "FARGATE_SPOT", "weight": 3},
            {"provider": "FARGATE", "weight": 1, "base": 1},
        ],
    })
    assert _capacity_strategy(m) == [
        {"capacityProvider": "FARGATE_SPOT", "weight": 3, "base": 0},
        {"capacityProvider": "FARGATE", "weight": 1, "base": 1},
    ]


def test_health_check_rendered_and_normalized():
    m = make_manifest(taskDefinition={"containers": [{
        "name": "web", "image": "nginx:1",
        "healthCheck": {"command": ["CMD-SHELL", "curl -f http://localhost/"]},
    }]})
    defs = desired_container_definitions(m, "eu-central-1")
    assert defs[0]["healthCheck"]["command"] == ["CMD-SHELL", "curl -f http://localhost/"]
    assert defs[0]["healthCheck"]["interval"] == 30   # ECS defaults mirrored
    # normalization: live (with AWS-filled defaults) == desired -> no diff churn
    live = {**defs[0], "healthCheck": {**defs[0]["healthCheck"]}}
    assert _norm_container(live) == _norm_container(defs[0])


# ---------- task definition cleanup ----------

def test_stale_taskdef_selection():
    arns = [f"arn:aws:ecs:eu:1:task-definition/web:{r}" for r in (9, 8, 7, 6, 5)]
    # prefix-matched other family must be ignored
    arns.insert(2, "arn:aws:ecs:eu:1:task-definition/web-worker:99")
    stale = _stale_taskdef_arns(arns, "web", keep=2, in_use=arns[0])
    assert stale == [
        "arn:aws:ecs:eu:1:task-definition/web:7",
        "arn:aws:ecs:eu:1:task-definition/web:6",
        "arn:aws:ecs:eu:1:task-definition/web:5",
    ]


def test_stale_taskdef_never_removes_in_use():
    arns = [f"arn/x/web:{r}" for r in (3, 2, 1)]
    stale = _stale_taskdef_arns(arns, "web", keep=1, in_use="arn/x/web:1")
    assert "arn/x/web:1" not in stale


# ---------- leader election ----------

def test_leadership_without_postgres_is_always_leader():
    from app import db
    # tests run without init_db -> no engine -> single-instance mode
    assert db.try_acquire_leadership() is True

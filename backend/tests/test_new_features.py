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


def test_substitute_text_runs_before_yaml_parse():
    """Regression: ${key} inside flow sequences (`[${a}, ${b}]`) used to fail
    because YAML parsed the file first and saw `{` as a flow-mapping opener.
    substitute_text runs on raw text before parse, so it must handle this."""
    text = "subnets: [${subnet_a}, ${subnet_b}]\nsg: ${sg}\n"
    values = {"subnet_a": "subnet-aaa", "subnet_b": "subnet-bbb", "sg": "sg-xxx"}
    resolved = templating.substitute_text(text, values)
    import yaml
    parsed = yaml.safe_load(resolved)
    assert parsed["subnets"] == ["subnet-aaa", "subnet-bbb"]
    assert parsed["sg"] == "sg-xxx"


def test_load_manifest_docs_accepts_placeholders_in_flow_sequences(tmp_path, monkeypatch):
    """Full pipeline: a manifest that uses ${key} inside `[..., ...]` must parse
    after values substitution — this is the exact e2e-kit failure mode."""
    from app import git_sync
    monkeypatch.setattr(settings, "repos_base_dir", str(tmp_path))
    repo_root = tmp_path / "repo-1"
    repo_root.mkdir()
    (repo_root / "values.yaml").write_text(
        "subnet_a: subnet-aaa\nsubnet_b: subnet-bbb\nsg: sg-xxx\n"
    )
    (repo_root / "web.yaml").write_text(
        "apiVersion: andro-cd/v1\n"
        "kind: ECSService\n"
        "metadata:\n  name: web\n"
        "spec:\n"
        "  cluster: c\n"
        "  network:\n"
        "    subnets: [${subnet_a}, ${subnet_b}]\n"     # flow sequence with placeholders
        "    securityGroups: [${sg}]\n"
        "  taskDefinition:\n"
        "    containers:\n"
        "      - name: web\n"
        "        image: nginx:1\n"
    )
    docs = git_sync.load_manifest_docs({"id": 1, "path": ""})
    assert len(docs) == 1
    _, doc = docs[0]
    assert "__parse_error__" not in doc, doc.get("__parse_error__")
    assert doc["spec"]["network"]["subnets"] == ["subnet-aaa", "subnet-bbb"]
    assert doc["spec"]["network"]["securityGroups"] == ["sg-xxx"]


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


# ---------- forget_app (DELETE /api/apps/{name}) ----------

def _make_orphan(name: str, *, synced: bool = False, cluster: str = "") -> "AppState":
    from app.state import AppState
    app = AppState(name=name, file=f"{name}.yaml")
    app.sync_status = "Orphaned"
    if synced:
        app.last_synced = "2026-01-01T00:00:00Z"
    if cluster:
        app.coords = {"cluster": cluster, "region": "us-east-1", "aws_profile": ""}
    return app


def test_forget_app_drops_parse_error_orphan(monkeypatch):
    from app import engine
    from app.state import store, AppState
    monkeypatch.setattr(engine.db, "delete_app_state", lambda n: None)
    app = _make_orphan("invalid:01-rolling.yaml")
    with store.lock():
        store.apps[app.name] = app
    try:
        engine.forget_app(app.name)
    finally:
        with store.lock():
            store.apps.pop(app.name, None)
    with store.lock():
        assert app.name not in store.apps


def test_forget_app_refuses_when_synced_to_aws(monkeypatch):
    from app import engine
    from app.state import store
    app = _make_orphan("web-prod", synced=True, cluster="prod")
    with store.lock():
        store.apps[app.name] = app
    try:
        with pytest.raises(ValueError, match="Prune"):
            engine.forget_app(app.name)
    finally:
        with store.lock():
            store.apps.pop(app.name, None)


def test_forget_app_refuses_non_orphan(monkeypatch):
    from app import engine
    from app.state import store, AppState
    app = AppState(name="active", file="a.yaml")
    app.sync_status = "Synced"
    with store.lock():
        store.apps["active"] = app
    try:
        with pytest.raises(ValueError, match="state Synced"):
            engine.forget_app("active")
    finally:
        with store.lock():
            store.apps.pop("active", None)


def test_forget_app_unknown_raises_keyerror():
    from app import engine
    with pytest.raises(KeyError):
        engine.forget_app("does-not-exist")


# ---------- persistence status surfacing ----------

def test_init_db_marks_status_error_on_connection_failure(monkeypatch):
    """The exact failure mode that cost a user their state: DATABASE_URL is set
    but the DB rejects the connection (bad password, host unreachable, ...).
    init_db must set persistence_status=error and record the reason so the UI can
    show a big red banner instead of silently falling back to in-memory."""
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "database_url",
                        "postgresql+psycopg://androcd:androcd@127.0.0.1:1/androcd")  # port 1 = refused
    # Reset state so the test is order-independent.
    db.persistence_status = "unknown"
    db.persistence_error = None
    db._Session = None
    db._engine = None

    assert db.init_db() is False
    assert db.persistence_status == "error"
    assert db.persistence_error is not None and db.persistence_error != ""
    assert db.persistence_url_safe == "androcd@127.0.0.1:1/androcd"    # password redacted


def test_init_db_marks_status_disabled_when_no_url(monkeypatch):
    from app import db
    from app.config import settings
    monkeypatch.setattr(settings, "database_url", "")
    db.persistence_status = "unknown"
    db.persistence_error = None

    assert db.init_db() is False
    assert db.persistence_status == "disabled"
    assert db.persistence_error == "DATABASE_URL not set"


def test_redact_url_strips_password():
    from app.db import _redact_url
    assert _redact_url("postgresql+psycopg://u:secret@h:5432/db") == "u@h:5432/db"
    # username-only URL is left as-is
    assert _redact_url("postgresql+psycopg://u@h/db") == "u@h/db"
    # sqlite/file URLs are unchanged (no @, no password)
    assert _redact_url("sqlite:///data/db.sqlite") == "sqlite:///data/db.sqlite"


def test_startup_problems_warns_when_postgres_url_lacks_password(monkeypatch):
    """When POSTGRES_PASSWORD is commented out in .env, docker-compose interpolates
    an empty string and DATABASE_URL becomes `.../androcd:@db/...`. Warn loudly at
    startup — silence here is exactly what cost the user their state."""
    from app.config import settings as live_settings
    monkeypatch.setattr(live_settings, "database_url",
                        "postgresql+psycopg://androcd:@db:5432/androcd")
    problems = live_settings.startup_problems()
    assert any("no password" in p for p in problems), problems


def test_startup_problems_ok_with_password():
    from app.config import settings as live_settings
    # default test env uses sqlite, which shouldn't trigger the postgres warning
    problems = live_settings.startup_problems()
    assert not any("no password" in p for p in problems)

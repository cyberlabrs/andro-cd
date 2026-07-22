"""Tests for kind: ECSCluster — model validation, diff logic, health, prune safety."""
import pytest
from pydantic import ValidationError

from app.models import Manifest
from app.reconciler import (_cluster_changes, _cluster_default_strategy,
                            _cluster_live_summary, _cluster_providers, compute_health)


def make_cluster(**spec) -> Manifest:
    return Manifest.model_validate({
        "apiVersion": "andro-cd/v1", "kind": "ECSCluster",
        "metadata": {"name": "production", "labels": {"team": "platform"}},
        "spec": {"region": "eu-central-1", **spec},
    })


LIVE = {
    "clusterName": "production",
    "status": "ACTIVE",
    "runningTasksCount": 7,
    "pendingTasksCount": 0,
    "activeServicesCount": 3,
    "settings": [{"name": "containerInsights", "value": "disabled"}],
    "capacityProviders": ["FARGATE"],
    "defaultCapacityProviderStrategy": [{"capacityProvider": "FARGATE", "weight": 1, "base": 0}],
    "serviceConnectDefaults": {},
}


# ---------- model validation ----------

def test_minimal_cluster_manifest():
    m = make_cluster()
    assert m.kind == "ECSCluster"
    assert m.spec.cluster == "production"      # defaults to metadata.name
    assert m.spec.network is None and m.spec.taskDefinition is None


def test_cluster_name_override():
    assert make_cluster(cluster="shared-prod").spec.cluster == "shared-prod"


def test_service_kind_still_requires_network_and_taskdef():
    with pytest.raises(ValidationError, match="spec.network is required"):
        Manifest.model_validate({
            "apiVersion": "andro-cd/v1", "kind": "ECSService",
            "metadata": {"name": "x"},
            "spec": {"cluster": "c",
                     "taskDefinition": {"containers": [{"name": "a", "image": "i"}]}},
        })


def test_invalid_container_insights_rejected():
    with pytest.raises(ValidationError, match="containerInsights"):
        make_cluster(containerInsights="on")


def test_custom_strategy_provider_must_be_listed():
    with pytest.raises(ValidationError, match="must be listed"):
        make_cluster(defaultCapacityProviderStrategy=[{"provider": "my-asg-provider"}])
    # FARGATE providers are auto-attached, no listing needed
    m = make_cluster(defaultCapacityProviderStrategy=[{"provider": "FARGATE_SPOT", "weight": 2}])
    assert _cluster_providers(m) == ["FARGATE_SPOT"]


# ---------- diff ----------

def test_cluster_in_sync_when_unspecified_fields_ignored():
    # manifest that only pins containerInsights=disabled matches LIVE
    assert _cluster_changes(make_cluster(containerInsights="disabled"), LIVE) == []
    # empty manifest (just the cluster) is always in sync
    assert _cluster_changes(make_cluster(), LIVE) == []


def test_cluster_diff_detects_changes():
    m = make_cluster(
        containerInsights="enhanced",
        capacityProviders=["FARGATE", "FARGATE_SPOT"],
        defaultCapacityProviderStrategy=[
            {"provider": "FARGATE_SPOT", "weight": 3},
            {"provider": "FARGATE", "weight": 1, "base": 1},
        ],
    )
    changes = _cluster_changes(m, LIVE)
    assert any(c.startswith("containerInsights: disabled -> enhanced") for c in changes)
    assert any(c.startswith("capacityProviders:") for c in changes)
    assert any(c.startswith("defaultCapacityProviderStrategy:") for c in changes)


def test_cluster_namespace_diff_only_when_comparable():
    live_with_ns = {**LIVE, "serviceConnectDefaults": {
        "namespace": "arn:aws:servicediscovery:eu:1:namespace/ns-abc"}}
    # live has a namespace, manifest uses a short name -> not comparable, no churn
    assert _cluster_changes(make_cluster(serviceConnectNamespace="internal"), live_with_ns) == []
    # no namespace live yet -> will be set
    assert _cluster_changes(make_cluster(serviceConnectNamespace="internal"), LIVE)
    # full ARN mismatch -> change
    assert _cluster_changes(
        make_cluster(serviceConnectNamespace="arn:aws:servicediscovery:eu:1:namespace/ns-zzz"),
        live_with_ns)


def test_strategy_rendering():
    m = make_cluster(defaultCapacityProviderStrategy=[
        {"provider": "FARGATE_SPOT", "weight": 3},
        {"provider": "FARGATE", "weight": 1, "base": 1},
    ])
    assert _cluster_default_strategy(m) == [
        {"capacityProvider": "FARGATE_SPOT", "weight": 3, "base": 0},
        {"capacityProvider": "FARGATE", "weight": 1, "base": 1},
    ]


# ---------- health ----------

def test_cluster_health_from_live_summary():
    healthy = compute_health(_cluster_live_summary(LIVE))
    assert healthy[0] == "Healthy" and "3 services" in healthy[1]
    assert compute_health(_cluster_live_summary(None)) == ("Unknown", "cluster does not exist")
    degraded = compute_health(_cluster_live_summary({**LIVE, "status": "DEPROVISIONING"}))
    assert degraded[0] == "Degraded"


# ---------- prune safety ----------

def test_cluster_prune_refuses_nonempty_cluster():
    from app.reconciler import _cluster_prune

    class FakeEcs:
        def describe_clusters(self, clusters):
            return {"clusters": [dict(LIVE)]}

        def delete_cluster(self, cluster):
            raise AssertionError("must not delete a cluster with workloads")

    with pytest.raises(RuntimeError, match="still has 3 service"):
        _cluster_prune(FakeEcs(), "production")


def test_cluster_prune_deletes_empty_cluster():
    from app.reconciler import _cluster_prune

    deleted = []

    class FakeEcs:
        def describe_clusters(self, clusters):
            return {"clusters": [{**LIVE, "activeServicesCount": 0, "runningTasksCount": 0}]}

        def delete_cluster(self, cluster):
            deleted.append(cluster)

    assert _cluster_prune(FakeEcs(), "production") == ["deleted cluster 'production'"]
    assert deleted == ["production"]

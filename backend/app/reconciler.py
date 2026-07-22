import logging
import threading
from typing import Any, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Adaptive retry mode handles AWS throttling gracefully; keep timeouts finite
# so a single hung call doesn't stall the reconcile loop.
_boto_config = Config(
    connect_timeout=10,
    read_timeout=45,
    retries={"max_attempts": 6, "mode": "adaptive"},
)

from .config import settings
from .models import Manifest
from .state import store

log = logging.getLogger("andro-cd.reconciler")

_clients: dict[tuple[str, str, str], Any] = {}


def reset_client_cache() -> None:
    _clients.clear()


def _profile(m: Optional[Manifest]) -> Optional[dict]:
    if m is None or not m.spec.awsProfile:
        return None
    profile = store.profiles.get(m.spec.awsProfile)
    if not profile:
        raise ValueError(f"AWS profile '{m.spec.awsProfile}' is not configured (add it in the UI)")
    return profile


def _client(service: str, region: str, m: Optional[Manifest] = None):
    profile = _profile(m)
    key = (service, region, profile["name"] if profile else "")
    if key not in _clients:
        if profile:
            _clients[key] = boto3.client(
                service, region_name=region,
                aws_access_key_id=profile["access_key_id"],
                aws_secret_access_key=profile["secret_access_key"],
                config=_boto_config,
            )
        else:
            _clients[key] = boto3.client(service, region_name=region, config=_boto_config)
    return _clients[key]


def _region(m: Manifest) -> str:
    profile = _profile(m)
    region = m.spec.region or (profile or {}).get("region") or settings.aws_region
    if not region:
        raise ValueError(f"{m.name}: no region in manifest/profile and AWS_REGION not set")
    return region


# ---------- desired state ----------

_digest_cache: dict[str, tuple[str, float]] = {}
_ECR_RE = None


def _resolve_image(image: str, region: str, m: Manifest) -> str:
    """Pin a mutable ECR tag to its immutable digest. Non-ECR images pass through.
    Short TTL (10s): we want to detect upstream image changes fast, but avoid
    hammering DescribeImages during a single reconcile pass (bug #10)."""
    global _ECR_RE
    import re
    import time as _t
    if _ECR_RE is None:
        _ECR_RE = re.compile(r"^(\d+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com)/([^:@]+):([^@]+)$")
    match = _ECR_RE.match(image)
    if not match:
        return image  # not an ECR tag reference (or already digest-pinned)
    cached = _digest_cache.get(image)
    if cached and _t.monotonic() - cached[1] < 10:
        return cached[0]
    registry, repo, tag = match.groups()
    ecr = _client("ecr", region, m)
    try:
        detail = ecr.describe_images(
            repositoryName=repo, imageIds=[{"imageTag": tag}]
        )["imageDetails"][0]
        resolved = f"{registry}/{repo}@{detail['imageDigest']}"
    except ClientError as e:
        raise ValueError(f"cannot resolve image '{image}': {e.response['Error']['Message']}")
    _digest_cache[image] = (resolved, _t.monotonic())
    return resolved


def desired_container_definitions(m: Manifest, region: str) -> list[dict]:
    defs = []
    resolve = m.spec.taskDefinition.resolveImages
    for c in m.spec.taskDefinition.containers:
        d: dict[str, Any] = {
            "name": c.name,
            "image": _resolve_image(c.image, region, m) if resolve else c.image,
            "essential": c.essential,
            "environment": c.env_list(),
            "portMappings": c.port_list(),
        }
        if c.cpu is not None:
            d["cpu"] = c.cpu
        if c.memory is not None:
            d["memory"] = c.memory
        if c.memoryReservation is not None:
            d["memoryReservation"] = c.memoryReservation
        if c.secrets:
            d["secrets"] = c.secret_list()
        if c.command:
            d["command"] = c.command
        if c.entryPoint:
            d["entryPoint"] = c.entryPoint
        if c.logGroup:
            d["logConfiguration"] = {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": c.logGroup,
                    "awslogs-region": region,
                    "awslogs-stream-prefix": c.name,
                },
            }
        if c.healthCheck:
            hc: dict[str, Any] = {
                "command": c.healthCheck.command,
                "interval": c.healthCheck.interval,
                "timeout": c.healthCheck.timeout,
                "retries": c.healthCheck.retries,
            }
            if c.healthCheck.startPeriod is not None:
                hc["startPeriod"] = c.healthCheck.startPeriod
            d["healthCheck"] = hc
        defs.append(d)
    return defs


def _aws_tags(m: Manifest) -> list[dict]:
    """metadata.labels -> AWS resource tags (cost allocation per team/app)."""
    return [{"key": k, "value": v} for k, v in sorted(m.metadata.labels.items())]


def _capacity_strategy(m: Manifest) -> list[dict]:
    return [
        {"capacityProvider": p.provider, "weight": p.weight, "base": p.base}
        for p in m.spec.service.capacityProviders
    ]


def _capacity_changes(m: Manifest, service: dict) -> list[str]:
    # Only diff when the manifest opts into a strategy — switching an existing
    # service back to plain launchType requires recreating it (AWS restriction).
    if not m.spec.service.capacityProviders:
        return []
    desired = sorted((s["capacityProvider"], s["weight"], s["base"])
                     for s in _capacity_strategy(m))
    live = sorted((s.get("capacityProvider"), s.get("weight", 0), s.get("base", 0))
                  for s in service.get("capacityProviderStrategy") or [])
    if desired != live:
        return [f"capacityProviderStrategy: {live or None} -> {desired or None}"]
    return []


def _register_kwargs(m: Manifest, region: str) -> dict:
    td = m.spec.taskDefinition
    cps = m.spec.service.capacityProviders
    if cps:
        # FARGATE/FARGATE_SPOT providers need FARGATE compatibility; custom
        # (ASG-backed) providers imply EC2.
        compat = "FARGATE" if any(p.provider.startswith("FARGATE") for p in cps) else "EC2"
    else:
        compat = m.spec.service.launchType
    kwargs: dict[str, Any] = {
        "family": m.family,
        "networkMode": td.networkMode,
        "requiresCompatibilities": [compat],
        "cpu": td.cpu,
        "memory": td.memory,
        "containerDefinitions": desired_container_definitions(m, region),
    }
    if td.executionRoleArn:
        kwargs["executionRoleArn"] = td.executionRoleArn
    if td.taskRoleArn:
        kwargs["taskRoleArn"] = td.taskRoleArn
    if m.metadata.labels:
        kwargs["tags"] = _aws_tags(m)
    return kwargs


def _deployment_configuration(m: Manifest) -> dict:
    svc = m.spec.service
    dc: dict[str, Any] = {
        "deploymentCircuitBreaker": {"enable": svc.circuitBreaker, "rollback": svc.rollbackOnFailure},
    }
    if svc.minimumHealthyPercent is not None:
        dc["minimumHealthyPercent"] = svc.minimumHealthyPercent
    if svc.maximumPercent is not None:
        dc["maximumPercent"] = svc.maximumPercent
    return dc


def _load_balancers(m: Manifest) -> list[dict]:
    lb = m.spec.service.loadBalancer
    if not lb:
        return []
    return [{
        "targetGroupArn": lb.targetGroupArn,
        "containerName": lb.containerName or m.spec.taskDefinition.containers[0].name,
        "containerPort": lb.containerPort,
    }]


def _lb_key(lb: dict) -> tuple:
    """Order-independent identity of a load balancer attachment."""
    return (lb.get("targetGroupArn"), lb.get("containerName"), lb.get("containerPort"))


def _lb_changes(m: Manifest, service: dict) -> list[str]:
    desired = _load_balancers(m)
    live = [{"targetGroupArn": l.get("targetGroupArn"),
             "containerName": l.get("containerName"),
             "containerPort": l.get("containerPort")}
            for l in service.get("loadBalancers", [])]
    # Compare by identity tuples so order and extra AWS fields don't cause churn (bug #11).
    if sorted(map(_lb_key, desired)) != sorted(map(_lb_key, live)):
        return [f"loadBalancers: {live or None} -> {desired or None}"]
    return []


def _autoscaling_changes(m: Manifest, region: str) -> list[str]:
    a = m.spec.service.autoscaling
    aas = _client("application-autoscaling", region, m)
    resource_id = f"service/{m.spec.cluster}/{m.name}"
    targets = aas.describe_scalable_targets(
        ServiceNamespace="ecs", ResourceIds=[resource_id],
        ScalableDimension="ecs:service:DesiredCount",
    ).get("ScalableTargets", [])
    changes = []
    if not a:
        if targets:
            changes.append("autoscaling will be removed")
        return changes
    if not targets:
        changes.append(f"autoscaling will be configured ({a.minCount}-{a.maxCount} tasks)")
        return changes
    t = targets[0]
    if t["MinCapacity"] != a.minCount or t["MaxCapacity"] != a.maxCount:
        changes.append(
            f"autoscaling range: {t['MinCapacity']}-{t['MaxCapacity']} -> {a.minCount}-{a.maxCount}")
    policies = {p["PolicyName"]: p for p in aas.describe_scaling_policies(
        ServiceNamespace="ecs", ResourceId=resource_id,
        ScalableDimension="ecs:service:DesiredCount",
    ).get("ScalingPolicies", [])}
    for metric, target_value, suffix in (
        ("ECSServiceAverageCPUUtilization", a.targetCpu, "cpu"),
        ("ECSServiceAverageMemoryUtilization", a.targetMemory, "memory"),
    ):
        name = f"androcd-{m.name}-{suffix}"
        live_policy = policies.get(name)
        live_target = ((live_policy or {}).get("TargetTrackingScalingPolicyConfiguration") or {}).get("TargetValue")
        if target_value is None and live_policy:
            changes.append(f"autoscaling {suffix} policy will be removed")
        elif target_value is not None and live_target != float(target_value):
            changes.append(f"autoscaling {suffix} target: {live_target} -> {target_value}")
    return changes


def _apply_autoscaling(m: Manifest, region: str) -> list[str]:
    if not _autoscaling_changes(m, region):
        return []
    a = m.spec.service.autoscaling
    aas = _client("application-autoscaling", region, m)
    resource_id = f"service/{m.spec.cluster}/{m.name}"
    actions = []
    if not a:
        aas.deregister_scalable_target(
            ServiceNamespace="ecs", ResourceId=resource_id,
            ScalableDimension="ecs:service:DesiredCount")
        return ["removed autoscaling"]
    aas.register_scalable_target(
        ServiceNamespace="ecs", ResourceId=resource_id,
        ScalableDimension="ecs:service:DesiredCount",
        MinCapacity=a.minCount, MaxCapacity=a.maxCount)
    actions.append(f"configured autoscaling {a.minCount}-{a.maxCount} tasks")
    for metric, target_value, suffix in (
        ("ECSServiceAverageCPUUtilization", a.targetCpu, "cpu"),
        ("ECSServiceAverageMemoryUtilization", a.targetMemory, "memory"),
    ):
        name = f"androcd-{m.name}-{suffix}"
        if target_value is None:
            try:
                aas.delete_scaling_policy(
                    PolicyName=name, ServiceNamespace="ecs", ResourceId=resource_id,
                    ScalableDimension="ecs:service:DesiredCount")
            except ClientError:
                pass
            continue
        aas.put_scaling_policy(
            PolicyName=name, ServiceNamespace="ecs", ResourceId=resource_id,
            ScalableDimension="ecs:service:DesiredCount",
            PolicyType="TargetTrackingScaling",
            TargetTrackingScalingPolicyConfiguration={
                "TargetValue": float(target_value),
                "PredefinedMetricSpecification": {"PredefinedMetricType": metric},
            })
        actions.append(f"set autoscaling {suffix} target {target_value}%")
    return actions


def _deploy_config_changes(m: Manifest, service: dict) -> list[str]:
    desired = _deployment_configuration(m)
    live = service.get("deploymentConfiguration") or {}
    changes = []
    live_cb = live.get("deploymentCircuitBreaker") or {}
    if live_cb.get("enable", False) != desired["deploymentCircuitBreaker"]["enable"] or \
       live_cb.get("rollback", False) != desired["deploymentCircuitBreaker"]["rollback"]:
        changes.append(f"circuitBreaker: {live_cb or None} -> {desired['deploymentCircuitBreaker']}")
    for key in ("minimumHealthyPercent", "maximumPercent"):
        if key in desired and live.get(key) != desired[key]:
            changes.append(f"{key}: {live.get(key)} -> {desired[key]}")
    return changes


# ---------- normalization & comparison ----------

def _norm_container(c: dict) -> dict:
    return {
        "name": c.get("name"),
        "image": c.get("image"),
        "essential": c.get("essential", True),
        "cpu": c.get("cpu") or None,
        "memory": c.get("memory") or None,
        "memoryReservation": c.get("memoryReservation") or None,
        "environment": sorted(
            [{"name": e["name"], "value": e["value"]} for e in c.get("environment", [])],
            key=lambda e: e["name"],
        ),
        "secrets": sorted(
            [{"name": s["name"], "valueFrom": s["valueFrom"]} for s in c.get("secrets", [])],
            key=lambda s: s["name"],
        ),
        "portMappings": sorted(
            [{"containerPort": p["containerPort"], "protocol": p.get("protocol", "tcp")}
             for p in c.get("portMappings", [])],
            key=lambda p: p["containerPort"],
        ),
        "command": c.get("command") or None,
        "entryPoint": c.get("entryPoint") or None,
        "logConfiguration": c.get("logConfiguration") or None,
        "healthCheck": _norm_healthcheck(c.get("healthCheck")),
    }


def _norm_healthcheck(hc: Optional[dict]) -> Optional[dict]:
    """ECS fills defaults on describe; mirror them so desired == live stays stable."""
    if not hc:
        return None
    return {
        "command": hc.get("command") or [],
        "interval": hc.get("interval", 30),
        "timeout": hc.get("timeout", 5),
        "retries": hc.get("retries", 3),
        "startPeriod": hc.get("startPeriod"),
    }


def _norm_taskdef(td: dict) -> dict:
    return {
        "cpu": str(td.get("cpu", "")),
        "memory": str(td.get("memory", "")),
        "networkMode": td.get("networkMode"),
        "executionRoleArn": td.get("executionRoleArn") or None,
        "taskRoleArn": td.get("taskRoleArn") or None,
        "containers": sorted(
            [_norm_container(c) for c in td.get("containerDefinitions", [])],
            key=lambda c: c["name"],
        ),
    }


def _taskdef_changes(live: dict, desired: dict) -> list[str]:
    a, b = _norm_taskdef(live), _norm_taskdef(desired)
    changes = []
    for key in ("cpu", "memory", "networkMode", "executionRoleArn", "taskRoleArn"):
        if a[key] != b[key]:
            changes.append(f"taskDefinition.{key}: {a[key]} -> {b[key]}")
    live_by_name = {c["name"]: c for c in a["containers"]}
    for c in b["containers"]:
        lc = live_by_name.pop(c["name"], None)
        if lc is None:
            changes.append(f"container '{c['name']}' will be added")
            continue
        for key, val in c.items():
            if lc.get(key) != val:
                changes.append(f"container '{c['name']}'.{key}: {lc.get(key)} -> {val}")
    for name in live_by_name:
        changes.append(f"container '{name}' will be removed")
    return changes


# ---------- live state ----------

def get_live_taskdef(m: Manifest, region: str) -> Optional[dict]:
    ecs = _client("ecs", region, m)
    try:
        resp = ecs.describe_task_definition(taskDefinition=m.family)
        return resp["taskDefinition"]
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ClientException", "InvalidParameterException"):
            return None
        raise


def get_live_cluster(m: Manifest, region: str) -> Optional[dict]:
    ecs = _client("ecs", region, m)
    resp = ecs.describe_clusters(clusters=[m.spec.cluster])
    for c in resp.get("clusters", []):
        if c["status"] == "ACTIVE":
            return c
    return None


def get_live_service(m: Manifest, region: str) -> Optional[dict]:
    ecs = _client("ecs", region, m)
    try:
        resp = ecs.describe_services(cluster=m.spec.cluster, services=[m.name])
    except ClientError as e:
        if e.response["Error"]["Code"] == "ClusterNotFoundException":
            return None
        raise
    for s in resp.get("services", []):
        if s["status"] != "INACTIVE":
            return s
    return None


# ---------- batched describes (perf optimization) ----------
#
# ECS DescribeServices accepts up to 10 services per call, DescribeClusters up to 100.
# When reconciling N apps in the same (region, profile, cluster) triple, one batched
# call replaces N single-service calls — typical wins are 5-10x fewer AWS API calls
# and much less throttling. `prefetch_live_state` builds a lookup dict; individual
# functions read from it via `_prefetch` first, falling back to the direct call.

_prefetch_ctx: threading.local = threading.local()


def prefetch_live_state(manifests: list["Manifest"]) -> dict:
    """Batch describe_clusters + describe_services for a set of manifests grouped
    by (region, awsProfile). Returns an opaque context to pass into refresh."""
    from collections import defaultdict

    ecs_services_kind = [m for m in manifests if m.kind == "ECSService"]
    if not ecs_services_kind:
        return {"clusters": {}, "services": {}}

    # Group by (region, profile_name) so each group shares one boto3 client.
    groups: dict[tuple[str, str], list[Manifest]] = defaultdict(list)
    for m in ecs_services_kind:
        try:
            region = _region(m)
        except Exception:
            continue
        profile = _profile(m)
        groups[(region, profile["name"] if profile else "")].append(m)

    clusters_out: dict[tuple[str, str, str], Optional[dict]] = {}
    services_out: dict[tuple[str, str, str, str], Optional[dict]] = {}

    for (region, _profile_name), group in groups.items():
        sample = group[0]  # any manifest in the group uses the same client key
        ecs = _client("ecs", region, sample)

        # DescribeClusters: batch up to 100 unique clusters.
        cluster_names = sorted({m.spec.cluster for m in group})
        for i in range(0, len(cluster_names), 100):
            chunk = cluster_names[i:i + 100]
            try:
                resp = ecs.describe_clusters(clusters=chunk)
            except ClientError as e:
                log.warning("batched describe_clusters failed: %s", e)
                continue
            active = {c["clusterName"]: c for c in resp.get("clusters", []) if c.get("status") == "ACTIVE"}
            for name in chunk:
                clusters_out[(region, sample.spec.awsProfile or "", name)] = active.get(name)

        # DescribeServices: 10 services per cluster per call.
        by_cluster: dict[str, list[Manifest]] = defaultdict(list)
        for m in group:
            by_cluster[m.spec.cluster].append(m)
        for cluster, apps in by_cluster.items():
            names = [m.name for m in apps]
            for i in range(0, len(names), 10):
                chunk_names = names[i:i + 10]
                try:
                    resp = ecs.describe_services(cluster=cluster, services=chunk_names)
                except ClientError as e:
                    if e.response["Error"]["Code"] == "ClusterNotFoundException":
                        for n in chunk_names:
                            services_out[(region, sample.spec.awsProfile or "", cluster, n)] = None
                        continue
                    log.warning("batched describe_services failed: %s", e)
                    continue
                by_name = {s["serviceName"]: s for s in resp.get("services", [])
                           if s.get("status") != "INACTIVE"}
                for n in chunk_names:
                    services_out[(region, sample.spec.awsProfile or "", cluster, n)] = by_name.get(n)

    return {"clusters": clusters_out, "services": services_out}


def use_prefetch(ctx: Optional[dict]) -> None:
    """Set the pre-fetched context for the current thread; None to clear."""
    if ctx is None:
        if hasattr(_prefetch_ctx, "data"):
            del _prefetch_ctx.data
    else:
        _prefetch_ctx.data = ctx


def _prefetch() -> Optional[dict]:
    return getattr(_prefetch_ctx, "data", None)


def _cached_cluster(m: Manifest, region: str) -> Optional[dict]:
    ctx = _prefetch()
    if ctx is None:
        return get_live_cluster(m, region)
    key = (region, m.spec.awsProfile or "", m.spec.cluster)
    return ctx["clusters"].get(key) if key in ctx["clusters"] else get_live_cluster(m, region)


def _cached_service(m: Manifest, region: str) -> Optional[dict]:
    ctx = _prefetch()
    if ctx is None:
        return get_live_service(m, region)
    key = (region, m.spec.awsProfile or "", m.spec.cluster, m.name)
    if key in ctx["services"]:
        return ctx["services"][key]
    return get_live_service(m, region)


# ---------- diff ----------

def compute_diff(m: Manifest) -> dict:
    """Read-only comparison of desired vs live state.
    Uses batched pre-fetched state when set via `use_prefetch` (perf optimization)."""
    region = _region(m)
    if m.kind == "ECSScheduledTask":
        return _schedule_diff(m, region)
    if m.kind == "ECSCluster":
        return _cluster_diff(m, region)
    changes: list[str] = []

    cluster = _cached_cluster(m, region)
    if cluster is None:
        changes.append(f"cluster '{m.spec.cluster}' will be created")

    desired_td = _register_kwargs(m, region)
    live_td = get_live_taskdef(m, region)
    td_changed = False
    if live_td is None:
        changes.append(f"task definition '{m.family}' will be registered")
        td_changed = True
    else:
        td_changes = _taskdef_changes(live_td, desired_td)
        if td_changes:
            changes.extend(td_changes)
            td_changed = True

    service = _cached_service(m, region) if cluster else None
    if service is None:
        changes.append(f"service '{m.name}' will be created")
    else:
        if td_changed:
            changes.append("service will roll out new task definition revision")
        elif live_td and service.get("taskDefinition") != live_td.get("taskDefinitionArn"):
            changes.append(
                f"service task definition: {service.get('taskDefinition', '?').split('/')[-1]}"
                f" -> {live_td['taskDefinitionArn'].split('/')[-1]}"
            )
        # when autoscaling owns desiredCount, don't fight it
        if not m.spec.service.autoscaling and service.get("desiredCount") != m.spec.service.desiredCount:
            changes.append(f"desiredCount: {service.get('desiredCount')} -> {m.spec.service.desiredCount}")
        net = (service.get("networkConfiguration") or {}).get("awsvpcConfiguration") or {}
        if set(net.get("subnets", [])) != set(m.spec.network.subnets):
            changes.append(f"subnets: {sorted(net.get('subnets', []))} -> {sorted(m.spec.network.subnets)}")
        if set(net.get("securityGroups", [])) != set(m.spec.network.securityGroups):
            changes.append(
                f"securityGroups: {sorted(net.get('securityGroups', []))}"
                f" -> {sorted(m.spec.network.securityGroups)}"
            )
        desired_ip = "ENABLED" if m.spec.service.assignPublicIp else "DISABLED"
        if net.get("assignPublicIp", "DISABLED") != desired_ip:
            changes.append(f"assignPublicIp: {net.get('assignPublicIp')} -> {desired_ip}")
        changes.extend(_deploy_config_changes(m, service))
        changes.extend(_lb_changes(m, service))
        changes.extend(_capacity_changes(m, service))
        if m.spec.service.autoscaling or service:
            changes.extend(_autoscaling_changes(m, region))

    return {
        "in_sync": not changes,
        "changes": changes,
        "live": _live_summary(cluster, live_td, service),
    }


def _live_summary(cluster, td, service) -> dict:
    out: dict[str, Any] = {
        "clusterExists": cluster is not None,
        "taskDefinition": None,
        "service": None,
    }
    if td:
        out["taskDefinition"] = {
            "arn": td["taskDefinitionArn"],
            "revision": td["revision"],
            "images": [c["image"] for c in td.get("containerDefinitions", [])],
        }
    if service:
        deployments = service.get("deployments", [])
        primary = next((d for d in deployments if d.get("status") == "PRIMARY"), {})
        out["service"] = {
            "status": service.get("status"),
            "desiredCount": service.get("desiredCount"),
            "runningCount": service.get("runningCount"),
            "pendingCount": service.get("pendingCount"),
            "taskDefinition": service.get("taskDefinition", "").split("/")[-1],
            "rolloutState": primary.get("rolloutState"),
            "deployments": len(deployments),
            "events": [e.get("message") for e in service.get("events", [])[:5]],
        }
    return out


def compute_health(live: dict) -> tuple[str, str]:
    if "schedule" in live:
        sched = live.get("schedule")
        if not sched:
            return "Unknown", "schedule does not exist"
        if sched.get("state") == "ENABLED":
            return "Healthy", f"schedule enabled ({sched.get('expression')})"
        return "Unknown", "schedule is disabled"
    if "cluster" in live:   # kind: ECSCluster
        c = live.get("cluster")
        if not c:
            return "Unknown", "cluster does not exist"
        if c.get("status") == "ACTIVE":
            return "Healthy", (f"cluster ACTIVE ({c.get('activeServices', 0)} services, "
                               f"{c.get('runningTasks', 0)} running tasks)")
        return "Degraded", f"cluster status: {c.get('status')}"
    svc = live.get("service")
    if not svc:
        return "Unknown", "service does not exist"
    rollout = svc.get("rolloutState")
    running, desired = svc.get("runningCount", 0), svc.get("desiredCount", 0)
    if rollout == "FAILED":
        return "Degraded", "deployment rollout failed"
    # Prefer the rollout state — "deployments > 1" alone falsely reports Progressing
    # while ECS drains the old ACTIVE deployment (bug #12).
    if rollout == "IN_PROGRESS":
        return "Progressing", f"rollout in progress ({running}/{desired} running)"
    if running >= desired and desired >= 0:
        return "Healthy", f"{running}/{desired} tasks running"
    return "Degraded", f"{running}/{desired} tasks running"


# ---------- apply ----------

def _ensure_cluster(m: Manifest, region: str, ecs) -> list[str]:
    if get_live_cluster(m, region) is not None:
        return []
    kwargs: dict[str, Any] = {"clusterName": m.spec.cluster}
    if m.metadata.labels:
        kwargs["tags"] = _aws_tags(m)
    fargate_providers = sorted({p.provider for p in m.spec.service.capacityProviders
                                if p.provider.startswith("FARGATE")})
    if fargate_providers:
        # Associate the managed Fargate providers so the strategy is usable.
        # Custom (ASG) providers must already exist and be attached by the user.
        kwargs["capacityProviders"] = fargate_providers
    ecs.create_cluster(**kwargs)
    return [f"created cluster '{m.spec.cluster}'"]


def _stale_taskdef_arns(arns: list[str], family: str, keep: int, in_use: str) -> list[str]:
    """Pure helper: which ACTIVE revisions (newest-first list) to deregister.
    Filters exact family matches (list_task_definitions matches by *prefix*),
    keeps the newest `keep`, and never touches the revision in use."""
    same_family = [a for a in arns
                   if a.rsplit("/", 1)[-1].rsplit(":", 1)[0] == family]
    return [a for a in same_family[keep:] if a != in_use]


def _cleanup_taskdefs(m: Manifest, ecs, in_use: str) -> list[str]:
    """Opt-in via KEEP_TASKDEF_REVISIONS: prevents unbounded revision buildup."""
    keep = settings.keep_taskdef_revisions
    if keep <= 0:
        return []
    try:
        arns = ecs.list_task_definitions(
            familyPrefix=m.family, status="ACTIVE", sort="DESC",
        ).get("taskDefinitionArns", [])
        stale = _stale_taskdef_arns(arns, m.family, keep, in_use)
        for arn in stale:
            ecs.deregister_task_definition(taskDefinition=arn)
    except ClientError as e:
        log.warning("task definition cleanup failed for %s: %s", m.family, e)
        return []
    return [f"deregistered {len(stale)} old task definition revision(s)"] if stale else []


def apply(m: Manifest) -> list[str]:
    """Reconcile one app; returns list of actions performed."""
    region = _region(m)
    if m.kind == "ECSScheduledTask":
        return _schedule_apply(m, region)
    if m.kind == "ECSCluster":
        return _cluster_apply(m, region)
    ecs = _client("ecs", region, m)
    actions: list[str] = []

    actions.extend(_ensure_cluster(m, region, ecs))

    for c in m.spec.taskDefinition.containers:
        if c.logGroup:
            _ensure_log_group(c.logGroup, region, m)

    desired_td = _register_kwargs(m, region)
    live_td = get_live_taskdef(m, region)
    if live_td is None or _taskdef_changes(live_td, desired_td):
        resp = ecs.register_task_definition(**desired_td)
        td_arn = resp["taskDefinition"]["taskDefinitionArn"]
        actions.append(f"registered task definition {td_arn.split('/')[-1]}")
        actions.extend(_cleanup_taskdefs(m, ecs, in_use=td_arn))
    else:
        td_arn = live_td["taskDefinitionArn"]

    if m.spec.hooks.preSync:
        actions.extend(_run_hook(m, region, td_arn, m.spec.hooks.preSync, "preSync"))

    net_config = {
        "awsvpcConfiguration": {
            "subnets": m.spec.network.subnets,
            "securityGroups": m.spec.network.securityGroups,
            "assignPublicIp": "ENABLED" if m.spec.service.assignPublicIp else "DISABLED",
        }
    }

    deploy_config = _deployment_configuration(m)

    service = get_live_service(m, region)
    if service is None:
        create_kwargs: dict[str, Any] = dict(
            cluster=m.spec.cluster,
            serviceName=m.name,
            taskDefinition=td_arn,
            desiredCount=m.spec.service.desiredCount,
            networkConfiguration=net_config,
            deploymentConfiguration=deploy_config,
        )
        strategy = _capacity_strategy(m)
        if strategy:
            create_kwargs["capacityProviderStrategy"] = strategy
        else:
            create_kwargs["launchType"] = m.spec.service.launchType
        if m.metadata.labels:
            create_kwargs["tags"] = _aws_tags(m)
            create_kwargs["propagateTags"] = "SERVICE"
        lbs = _load_balancers(m)
        if lbs:
            create_kwargs["loadBalancers"] = lbs
        ecs.create_service(**create_kwargs)
        actions.append(f"created service '{m.name}'")
    else:
        live_net = (service.get("networkConfiguration") or {}).get("awsvpcConfiguration") or {}
        desired_net = net_config["awsvpcConfiguration"]
        count_drift = (not m.spec.service.autoscaling
                       and service.get("desiredCount") != m.spec.service.desiredCount)
        needs_update = (
            service.get("taskDefinition") != td_arn
            or count_drift
            or set(live_net.get("subnets", [])) != set(desired_net["subnets"])
            or set(live_net.get("securityGroups", [])) != set(desired_net["securityGroups"])
            or live_net.get("assignPublicIp", "DISABLED") != desired_net["assignPublicIp"]
            or bool(_deploy_config_changes(m, service))
            or bool(_lb_changes(m, service))
            or bool(_capacity_changes(m, service))
        )
        if needs_update:
            update_kwargs: dict[str, Any] = dict(
                cluster=m.spec.cluster,
                service=m.name,
                taskDefinition=td_arn,
                networkConfiguration=net_config,
                deploymentConfiguration=deploy_config,
            )
            if not m.spec.service.autoscaling:
                update_kwargs["desiredCount"] = m.spec.service.desiredCount
            if _lb_changes(m, service):
                update_kwargs["loadBalancers"] = _load_balancers(m)
            if _capacity_changes(m, service):
                # changing the strategy requires a fresh deployment
                update_kwargs["capacityProviderStrategy"] = _capacity_strategy(m)
                update_kwargs["forceNewDeployment"] = True
            ecs.update_service(**update_kwargs)
            actions.append(f"updated service '{m.name}'")

    actions.extend(_apply_autoscaling(m, region))

    if m.spec.hooks.postSync:
        actions.extend(_run_hook(m, region, td_arn, m.spec.hooks.postSync, "postSync"))

    return actions


def _run_hook(m: Manifest, region: str, td_arn: str, hook, phase: str) -> list[str]:
    """Run a one-off task (command override) and wait for exit code 0."""
    import time as _t
    ecs = _client("ecs", region, m)
    container = hook.container or m.spec.taskDefinition.containers[0].name
    resp = ecs.run_task(
        cluster=m.spec.cluster,
        taskDefinition=td_arn,
        launchType=m.spec.service.launchType,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": m.spec.network.subnets,
                "securityGroups": m.spec.network.securityGroups,
                "assignPublicIp": "ENABLED" if m.spec.service.assignPublicIp else "DISABLED",
            }
        },
        overrides={"containerOverrides": [{"name": container, "command": hook.command}]},
        startedBy=f"androcd-{phase}",
    )
    failures = resp.get("failures", [])
    if failures:
        raise RuntimeError(f"{phase} hook failed to start: {failures[0].get('reason')}")
    task_arn = resp["tasks"][0]["taskArn"]
    task_id = task_arn.split("/")[-1]
    log.info("%s hook for %s started (task %s)", phase, m.name, task_id)

    deadline = _t.monotonic() + hook.timeoutSeconds
    while _t.monotonic() < deadline:
        task = ecs.describe_tasks(cluster=m.spec.cluster, tasks=[task_arn])["tasks"][0]
        if task["lastStatus"] == "STOPPED":
            c = next((c for c in task["containers"] if c["name"] == container), {})
            exit_code = c.get("exitCode")
            if exit_code == 0:
                return [f"{phase} hook succeeded (task {task_id})"]
            raise RuntimeError(
                f"{phase} hook failed: exit={exit_code} reason={c.get('reason') or task.get('stoppedReason')}")
        _t.sleep(5)
    ecs.stop_task(cluster=m.spec.cluster, task=task_arn, reason=f"androcd {phase} hook timeout")
    raise RuntimeError(f"{phase} hook timed out after {hook.timeoutSeconds}s")


def _log_source(m: Manifest, container: Optional[str], region: str):
    """Resolve (group, stream_prefix, container_names, chosen_name) or an error dict."""
    td = get_live_taskdef(m, region)
    if not td:
        return None, {"containers": [], "error": "task definition not found in AWS yet"}

    containers = td.get("containerDefinitions", [])
    names = [c["name"] for c in containers]
    chosen = next((c for c in containers if c["name"] == container), containers[0])

    lc = chosen.get("logConfiguration") or {}
    if lc.get("logDriver") != "awslogs":
        return None, {"containers": names, "container": chosen["name"],
                      "error": f"container '{chosen['name']}' does not use the awslogs driver (set logGroup in the manifest)"}

    group = lc["options"]["awslogs-group"]
    prefix = lc["options"].get("awslogs-stream-prefix", "")
    stream_prefix = f"{prefix}/{chosen['name']}/" if prefix else ""
    return (group, stream_prefix, names, chosen["name"]), None


def log_events_since(m: Manifest, container: Optional[str], start_ms: int, limit: int = 200) -> dict:
    """New log events since start_ms (for live streaming). Single page of filter_log_events."""
    from datetime import datetime, timezone

    region = _region(m)
    source, err = _log_source(m, container, region)
    if err:
        return err
    group, stream_prefix, names, chosen = source

    logs = _client("logs", region, m)
    kwargs: dict[str, Any] = {"logGroupName": group, "startTime": start_ms, "limit": limit}
    if stream_prefix:
        kwargs["logStreamNamePrefix"] = stream_prefix
    try:
        resp = logs.filter_log_events(**kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return {"containers": names, "container": chosen, "group": group, "events": []}
        raise
    events = [
        {
            "id": e["eventId"],
            "ts": e["timestamp"],
            "timestamp": datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc).isoformat(),
            "message": e["message"].rstrip(),
        }
        for e in resp.get("events", [])
    ]
    return {"containers": names, "container": chosen, "group": group, "events": events}


def get_logs(m: Manifest, container: Optional[str] = None, lines: int = 100) -> dict:
    """Tail recent CloudWatch logs for one container of the app."""
    from datetime import datetime, timezone

    region = _region(m)
    source, err = _log_source(m, container, region)
    if err:
        return err
    group, stream_prefix, names, chosen_name = source
    chosen = {"name": chosen_name}

    logs = _client("logs", region, m)
    try:
        streams = logs.describe_log_streams(
            logGroupName=group, orderBy="LastEventTime", descending=True, limit=25,
        ).get("logStreams", [])
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return {"containers": names, "container": chosen["name"], "group": group,
                    "error": f"log group '{group}' not found"}
        raise

    if stream_prefix:
        streams = [s for s in streams if s["logStreamName"].startswith(stream_prefix)]

    events = []
    for s in streams[:3]:
        resp = logs.get_log_events(
            logGroupName=group, logStreamName=s["logStreamName"],
            limit=lines, startFromHead=False,
        )
        for e in resp.get("events", []):
            events.append({
                "timestamp": datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc).isoformat(),
                "message": e["message"].rstrip(),
            })
    events.sort(key=lambda e: e["timestamp"])
    return {"containers": names, "container": chosen["name"], "group": group,
            "events": events[-lines:]}


def list_revisions(m: Manifest, limit: int = 10) -> list[dict]:
    """Recent task definition revisions of the app's family (for rollback)."""
    region = _region(m)
    ecs = _client("ecs", region, m)
    service = get_live_service(m, region)
    current_arn = (service or {}).get("taskDefinition")
    arns = ecs.list_task_definitions(
        familyPrefix=m.family, sort="DESC", maxResults=limit
    ).get("taskDefinitionArns", [])
    out = []
    for arn in arns:
        td = ecs.describe_task_definition(taskDefinition=arn)["taskDefinition"]
        out.append({
            "revision": td["revision"],
            "arn": arn,
            "images": [c["image"] for c in td.get("containerDefinitions", [])],
            "registeredAt": td.get("registeredAt"),
            "current": arn == current_arn,
        })
    return out


def rollback(m: Manifest, revision: int) -> list[str]:
    """Point the service at an older task definition revision."""
    region = _region(m)
    ecs = _client("ecs", region, m)
    target = f"{m.family}:{revision}"
    ecs.update_service(cluster=m.spec.cluster, service=m.name, taskDefinition=target)
    return [f"rolled back service to {target}"]


def prune(m: Manifest) -> list[str]:
    """Delete the ECS service or schedule (cluster and task definitions are kept).
    For kind ECSCluster: delete the cluster itself (refused while it has workloads)."""
    region = _region(m)
    if m.kind == "ECSScheduledTask":
        return _delete_schedule(m, region)
    if m.kind == "ECSCluster":
        return _cluster_prune(_client("ecs", region, m), m.spec.cluster)
    ecs = _client("ecs", region, m)
    service = get_live_service(m, region)
    if not service:
        return ["service already gone"]
    ecs.delete_service(cluster=m.spec.cluster, service=m.name, force=True)
    return [f"deleted service '{m.name}' from cluster '{m.spec.cluster}'"]


def prune_raw(name: str, kind: str, cluster: str, region: str,
              profile_name: str = "") -> list[str]:
    """Prune using persisted coordinates (manifest no longer exists, e.g. after restart)."""
    profile = store.profiles.get(profile_name) if profile_name else None
    key = ("ecs", region, profile["name"] if profile else "")
    if key not in _clients:
        if profile:
            _clients[key] = boto3.client("ecs", region_name=region,
                                         aws_access_key_id=profile["access_key_id"],
                                         aws_secret_access_key=profile["secret_access_key"],
                                         config=_boto_config)
        else:
            _clients[key] = boto3.client("ecs", region_name=region, config=_boto_config)
    if kind == "ECSScheduledTask":
        return _delete_schedule_raw(name, region, profile)
    if kind == "ECSCluster":
        return _cluster_prune(_clients[key], cluster)
    ecs = _clients[key]
    try:
        resp = ecs.describe_services(cluster=cluster, services=[name])
        if not any(s["status"] != "INACTIVE" for s in resp.get("services", [])):
            return ["service already gone"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ClusterNotFoundException":
            return ["cluster already gone"]
        raise
    ecs.delete_service(cluster=cluster, service=name, force=True)
    return [f"deleted service '{name}' from cluster '{cluster}'"]


# ---------- clusters (kind: ECSCluster) ----------

def _get_live_cluster_full(m: Manifest, region: str) -> Optional[dict]:
    """Cluster with settings/tags included (richer than the batched describe)."""
    ecs = _client("ecs", region, m)
    resp = ecs.describe_clusters(clusters=[m.spec.cluster], include=["SETTINGS", "TAGS"])
    for c in resp.get("clusters", []):
        if c["status"] != "INACTIVE":
            return c
    return None


def _live_container_insights(cluster: dict) -> str:
    for s in cluster.get("settings", []):
        if s.get("name") == "containerInsights":
            return s.get("value") or "disabled"
    return "disabled"


def _cluster_default_strategy(m: Manifest) -> list[dict]:
    return [{"capacityProvider": p.provider, "weight": p.weight, "base": p.base}
            for p in m.spec.defaultCapacityProviderStrategy]


def _strategy_key(strategy: list[dict]) -> list[tuple]:
    return sorted((s.get("capacityProvider"), s.get("weight", 0), s.get("base", 0))
                  for s in strategy)


def _cluster_providers(m: Manifest) -> list[str]:
    """Providers to attach: the explicit list plus any FARGATE ones referenced by
    the default strategy (managed providers can be attached automatically)."""
    providers = set(m.spec.capacityProviders)
    providers.update(p.provider for p in m.spec.defaultCapacityProviderStrategy
                     if p.provider.startswith("FARGATE"))
    return sorted(providers)


def _cluster_changes(m: Manifest, live: dict) -> list[str]:
    changes: list[str] = []
    if m.spec.containerInsights is not None:
        live_ci = _live_container_insights(live)
        if live_ci != m.spec.containerInsights:
            changes.append(f"containerInsights: {live_ci} -> {m.spec.containerInsights}")
    desired_cp = _cluster_providers(m)
    if desired_cp and set(live.get("capacityProviders", [])) != set(desired_cp):
        changes.append(
            f"capacityProviders: {sorted(live.get('capacityProviders', [])) or None} -> {desired_cp}")
    desired_ds = _cluster_default_strategy(m)
    if desired_ds and _strategy_key(live.get("defaultCapacityProviderStrategy", [])) != _strategy_key(desired_ds):
        changes.append("defaultCapacityProviderStrategy: "
                       f"{live.get('defaultCapacityProviderStrategy') or None} -> {desired_ds}")
    ns = m.spec.serviceConnectNamespace
    if ns:
        live_ns = (live.get("serviceConnectDefaults") or {}).get("namespace") or ""
        # describe returns the namespace ARN; only diff reliably when we can compare
        # (no namespace set yet, or the manifest specifies the full ARN)
        if not live_ns or (ns.startswith("arn:") and live_ns != ns):
            changes.append(f"serviceConnectNamespace: {live_ns or None} -> {ns}")
    return changes


def _cluster_live_summary(live: Optional[dict]) -> dict:
    if not live:
        return {"clusterExists": False, "cluster": None}
    return {
        "clusterExists": True,
        "cluster": {
            "name": live.get("clusterName"),
            "status": live.get("status"),
            "runningTasks": live.get("runningTasksCount"),
            "pendingTasks": live.get("pendingTasksCount"),
            "activeServices": live.get("activeServicesCount"),
            "containerInsights": _live_container_insights(live),
            "capacityProviders": live.get("capacityProviders", []),
            "defaultCapacityProviderStrategy": live.get("defaultCapacityProviderStrategy", []),
            "serviceConnectNamespace": (live.get("serviceConnectDefaults") or {}).get("namespace"),
        },
    }


def _cluster_diff(m: Manifest, region: str) -> dict:
    live = _get_live_cluster_full(m, region)
    if live is None:
        return {"in_sync": False,
                "changes": [f"cluster '{m.spec.cluster}' will be created"],
                "live": _cluster_live_summary(None)}
    changes = _cluster_changes(m, live)
    return {"in_sync": not changes, "changes": changes,
            "live": _cluster_live_summary(live)}


def _cluster_apply(m: Manifest, region: str) -> list[str]:
    ecs = _client("ecs", region, m)
    live = _get_live_cluster_full(m, region)
    actions: list[str] = []

    settings_param = ([{"name": "containerInsights", "value": m.spec.containerInsights}]
                      if m.spec.containerInsights is not None else None)
    providers = _cluster_providers(m)
    strategy = _cluster_default_strategy(m)

    if live is None:
        kwargs: dict[str, Any] = {"clusterName": m.spec.cluster}
        if settings_param:
            kwargs["settings"] = settings_param
        if providers:
            kwargs["capacityProviders"] = providers
            kwargs["defaultCapacityProviderStrategy"] = strategy
        if m.spec.serviceConnectNamespace:
            kwargs["serviceConnectDefaults"] = {"namespace": m.spec.serviceConnectNamespace}
        if m.metadata.labels:
            kwargs["tags"] = _aws_tags(m)
        ecs.create_cluster(**kwargs)
        return [f"created cluster '{m.spec.cluster}'"]

    changes = _cluster_changes(m, live)
    if not changes:
        return []

    update_kwargs: dict[str, Any] = {}
    if any(c.startswith("containerInsights") for c in changes):
        update_kwargs["settings"] = settings_param
    if any(c.startswith("serviceConnectNamespace") for c in changes):
        update_kwargs["serviceConnectDefaults"] = {"namespace": m.spec.serviceConnectNamespace}
    if update_kwargs:
        ecs.update_cluster(cluster=m.spec.cluster, **update_kwargs)
        if "settings" in update_kwargs:
            actions.append(f"set containerInsights={m.spec.containerInsights}")
        if "serviceConnectDefaults" in update_kwargs:
            actions.append(f"set serviceConnectNamespace={m.spec.serviceConnectNamespace}")

    if any(c.startswith(("capacityProviders", "defaultCapacityProviderStrategy")) for c in changes):
        # put_ replaces both lists atomically; keep the live value for whichever
        # half the manifest doesn't specify.
        ecs.put_cluster_capacity_providers(
            cluster=m.spec.cluster,
            capacityProviders=providers or live.get("capacityProviders", []),
            defaultCapacityProviderStrategy=(
                strategy or live.get("defaultCapacityProviderStrategy", [])),
        )
        actions.append(f"updated capacity providers ({', '.join(providers) or 'unchanged'})")
    return actions


def _cluster_prune(ecs, cluster: str) -> list[str]:
    """Delete the cluster itself. AWS refuses while it still has active services
    or running tasks — that error is surfaced instead of force-deleting workloads."""
    try:
        resp = ecs.describe_clusters(clusters=[cluster])
        live = next((c for c in resp.get("clusters", []) if c["status"] != "INACTIVE"), None)
        if live is None:
            return ["cluster already gone"]
        if live.get("activeServicesCount") or live.get("runningTasksCount"):
            raise RuntimeError(
                f"cluster '{cluster}' still has {live.get('activeServicesCount', 0)} service(s) / "
                f"{live.get('runningTasksCount', 0)} task(s) — remove them first")
        ecs.delete_cluster(cluster=cluster)
    except ClientError as e:
        raise RuntimeError(f"cluster delete failed: {e.response['Error']['Message']}")
    return [f"deleted cluster '{cluster}'"]


# ---------- scheduled tasks (EventBridge Scheduler) ----------

def _schedule_name(m: Manifest) -> str:
    return f"androcd-{m.name}"


def _schedule_target(m: Manifest, region: str, td_arn: str) -> dict:
    account = td_arn.split(":")[4]
    return {
        "Arn": f"arn:aws:ecs:{region}:{account}:cluster/{m.spec.cluster}",
        "RoleArn": m.spec.schedule.roleArn,
        "EcsParameters": {
            "TaskDefinitionArn": td_arn,
            "LaunchType": m.spec.service.launchType,
            "NetworkConfiguration": {
                "awsvpcConfiguration": {
                    "Subnets": m.spec.network.subnets,
                    "SecurityGroups": m.spec.network.securityGroups,
                    "AssignPublicIp": "ENABLED" if m.spec.service.assignPublicIp else "DISABLED",
                }
            },
        },
    }


def _get_schedule(m: Manifest, region: str) -> Optional[dict]:
    sched = _client("scheduler", region, m)
    try:
        return sched.get_schedule(Name=_schedule_name(m))
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return None
        raise


def _schedule_diff(m: Manifest, region: str) -> dict:
    changes: list[str] = []
    if get_live_cluster(m, region) is None:
        changes.append(f"cluster '{m.spec.cluster}' will be created")

    desired_td = _register_kwargs(m, region)
    live_td = get_live_taskdef(m, region)
    if live_td is None:
        changes.append(f"task definition '{m.family}' will be registered")
    else:
        changes.extend(_taskdef_changes(live_td, desired_td))

    live = _get_schedule(m, region)
    desired_state = "ENABLED" if m.spec.schedule.enabled else "DISABLED"
    if live is None:
        changes.append(f"schedule '{_schedule_name(m)}' will be created ({m.spec.schedule.expression})")
    else:
        if live.get("ScheduleExpression") != m.spec.schedule.expression:
            changes.append(f"expression: {live.get('ScheduleExpression')} -> {m.spec.schedule.expression}")
        if live.get("State") != desired_state:
            changes.append(f"state: {live.get('State')} -> {desired_state}")
        if live_td and not _taskdef_changes(live_td, desired_td):
            live_target_td = ((live.get("Target") or {}).get("EcsParameters") or {}).get("TaskDefinitionArn")
            if live_target_td != live_td["taskDefinitionArn"]:
                changes.append("schedule will point at the latest task definition revision")

    summary = {"clusterExists": get_live_cluster(m, region) is not None,
               "taskDefinition": None, "service": None,
               "schedule": None}
    if live_td:
        summary["taskDefinition"] = {
            "arn": live_td["taskDefinitionArn"], "revision": live_td["revision"],
            "images": [c["image"] for c in live_td.get("containerDefinitions", [])],
        }
    if live:
        summary["schedule"] = {"expression": live.get("ScheduleExpression"),
                               "state": live.get("State")}
    return {"in_sync": not changes, "changes": changes, "live": summary}


def _schedule_apply(m: Manifest, region: str) -> list[str]:
    ecs = _client("ecs", region, m)
    actions: list[str] = []
    actions.extend(_ensure_cluster(m, region, ecs))
    for c in m.spec.taskDefinition.containers:
        if c.logGroup:
            _ensure_log_group(c.logGroup, region, m)

    desired_td = _register_kwargs(m, region)
    live_td = get_live_taskdef(m, region)
    if live_td is None or _taskdef_changes(live_td, desired_td):
        td_arn = ecs.register_task_definition(**desired_td)["taskDefinition"]["taskDefinitionArn"]
        actions.append(f"registered task definition {td_arn.split('/')[-1]}")
    else:
        td_arn = live_td["taskDefinitionArn"]

    sched = _client("scheduler", region, m)
    kwargs = {
        "Name": _schedule_name(m),
        "ScheduleExpression": m.spec.schedule.expression,
        "State": "ENABLED" if m.spec.schedule.enabled else "DISABLED",
        "FlexibleTimeWindow": {"Mode": "OFF"},
        "Target": _schedule_target(m, region, td_arn),
    }
    if _get_schedule(m, region) is None:
        sched.create_schedule(**kwargs)
        actions.append(f"created schedule '{_schedule_name(m)}' ({m.spec.schedule.expression})")
    else:
        sched.update_schedule(**kwargs)
        actions.append(f"updated schedule '{_schedule_name(m)}'")
    return actions


def _delete_schedule(m: Manifest, region: str) -> list[str]:
    sched = _client("scheduler", region, m)
    try:
        sched.delete_schedule(Name=_schedule_name(m))
        return [f"deleted schedule '{_schedule_name(m)}'"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return ["schedule already gone"]
        raise


def _delete_schedule_raw(name: str, region: str, profile: Optional[dict]) -> list[str]:
    if profile:
        client = boto3.client("scheduler", region_name=region,
                              aws_access_key_id=profile["access_key_id"],
                              aws_secret_access_key=profile["secret_access_key"],
                              config=_boto_config)
    else:
        client = boto3.client("scheduler", region_name=region, config=_boto_config)
    try:
        client.delete_schedule(Name=f"androcd-{name}")
        return [f"deleted schedule 'androcd-{name}'"]
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceNotFoundException":
            return ["schedule already gone"]
        raise


def get_resources(m: Manifest) -> dict:
    """Full live picture: cluster, service config, active task definition, running tasks."""
    region = _region(m)
    ecs = _client("ecs", region, m)

    if m.kind == "ECSCluster":
        full = _get_live_cluster_full(m, region)
        return {"cluster": _cluster_live_summary(full).get("cluster"),
                "service": None, "taskDefinition": None, "tasks": [], "stoppedTasks": []}

    cluster = get_live_cluster(m, region)
    service = get_live_service(m, region)
    td = get_live_taskdef(m, region)

    out: dict[str, Any] = {"cluster": None, "service": None, "taskDefinition": None, "tasks": []}

    if cluster:
        out["cluster"] = {
            "name": cluster["clusterName"],
            "status": cluster["status"],
            "runningTasks": cluster.get("runningTasksCount"),
            "pendingTasks": cluster.get("pendingTasksCount"),
            "activeServices": cluster.get("activeServicesCount"),
        }

    if td:
        out["taskDefinition"] = {
            "family": td.get("family"),
            "revision": td.get("revision"),
            "arn": td.get("taskDefinitionArn"),
            "status": td.get("status"),
            "cpu": td.get("cpu"),
            "memory": td.get("memory"),
            "networkMode": td.get("networkMode"),
            "executionRoleArn": td.get("executionRoleArn"),
            "taskRoleArn": td.get("taskRoleArn"),
            "registeredAt": td.get("registeredAt"),
            "containers": [
                {
                    "name": c.get("name"),
                    "image": c.get("image"),
                    "essential": c.get("essential", True),
                    "cpu": c.get("cpu"),
                    "memory": c.get("memory"),
                    "memoryReservation": c.get("memoryReservation"),
                    "portMappings": c.get("portMappings", []),
                    "environment": c.get("environment", []),
                    "secretNames": [s["name"] for s in c.get("secrets", [])],
                    "command": c.get("command"),
                    "logGroup": ((c.get("logConfiguration") or {}).get("options") or {}).get("awslogs-group"),
                }
                for c in td.get("containerDefinitions", [])
            ],
        }

    if service:
        net = (service.get("networkConfiguration") or {}).get("awsvpcConfiguration") or {}
        dc = service.get("deploymentConfiguration") or {}
        out["service"] = {
            "status": service.get("status"),
            "launchType": service.get("launchType"),
            "desiredCount": service.get("desiredCount"),
            "runningCount": service.get("runningCount"),
            "pendingCount": service.get("pendingCount"),
            "taskDefinition": (service.get("taskDefinition") or "").split("/")[-1],
            "createdAt": service.get("createdAt"),
            "subnets": net.get("subnets", []),
            "securityGroups": net.get("securityGroups", []),
            "assignPublicIp": net.get("assignPublicIp"),
            "circuitBreaker": dc.get("deploymentCircuitBreaker"),
            "minimumHealthyPercent": dc.get("minimumHealthyPercent"),
            "maximumPercent": dc.get("maximumPercent"),
            "deployments": [
                {
                    "status": d.get("status"),
                    "taskDefinition": (d.get("taskDefinition") or "").split("/")[-1],
                    "desired": d.get("desiredCount"),
                    "running": d.get("runningCount"),
                    "pending": d.get("pendingCount"),
                    "failed": d.get("failedTasks"),
                    "rolloutState": d.get("rolloutState"),
                    "updatedAt": d.get("updatedAt"),
                }
                for d in service.get("deployments", [])
            ],
            "events": [
                {"createdAt": e.get("createdAt"), "message": e.get("message")}
                for e in service.get("events", [])[:15]
            ],
        }

    out["stoppedTasks"] = []
    if cluster and service:
        for desired_status, bucket in (("RUNNING", "tasks"), ("STOPPED", "stoppedTasks")):
            arns = ecs.list_tasks(cluster=m.spec.cluster, serviceName=m.name,
                                  desiredStatus=desired_status).get("taskArns", [])
            if not arns:
                continue
            for t in ecs.describe_tasks(cluster=m.spec.cluster, tasks=arns[:100]).get("tasks", []):
                ip = None
                for att in t.get("attachments", []):
                    for d in att.get("details", []):
                        if d.get("name") == "privateIPv4Address":
                            ip = d.get("value")
                containers = [
                    {"name": c.get("name"), "exitCode": c.get("exitCode"), "reason": c.get("reason")}
                    for c in t.get("containers", [])
                ]
                out[bucket].append({
                    "id": (t.get("taskArn") or "").split("/")[-1],
                    "lastStatus": t.get("lastStatus"),
                    "desiredStatus": t.get("desiredStatus"),
                    "healthStatus": t.get("healthStatus"),
                    "taskDefinition": (t.get("taskDefinitionArn") or "").split("/")[-1],
                    "cpu": t.get("cpu"),
                    "memory": t.get("memory"),
                    "az": t.get("availabilityZone"),
                    "ip": ip,
                    "startedAt": t.get("startedAt"),
                    "stoppedAt": t.get("stoppedAt"),
                    "stoppedReason": t.get("stoppedReason"),
                    "containers": containers,
                })
    return out


def get_diff_document(m: Manifest) -> dict:
    """Normalized desired vs live JSON for the side-by-side diff view."""
    region = _region(m)

    if m.kind == "ECSCluster":
        live_full = _get_live_cluster_full(m, region)
        live_cluster = _cluster_live_summary(live_full).get("cluster")
        desired_cluster = {
            "name": m.spec.cluster,
            "containerInsights": m.spec.containerInsights,
            "capacityProviders": _cluster_providers(m),
            "defaultCapacityProviderStrategy": _cluster_default_strategy(m),
            "serviceConnectNamespace": m.spec.serviceConnectNamespace,
            "tags": _aws_tags(m),
        }
        return {
            "desired": {"cluster": desired_cluster, "taskDefinition": None, "service": None},
            "live": {"cluster": live_cluster, "taskDefinition": None, "service": None},
        }

    desired_td = _norm_taskdef(_register_kwargs(m, region))
    live_td_raw = get_live_taskdef(m, region)
    live_td = _norm_taskdef(live_td_raw) if live_td_raw else None

    service = get_live_service(m, region) if m.kind == "ECSService" else None
    desired_svc = {
        "desiredCount": m.spec.service.desiredCount,
        "launchType": m.spec.service.launchType,
        "subnets": sorted(m.spec.network.subnets),
        "securityGroups": sorted(m.spec.network.securityGroups),
        "assignPublicIp": "ENABLED" if m.spec.service.assignPublicIp else "DISABLED",
        "deploymentConfiguration": _deployment_configuration(m),
        "loadBalancers": _load_balancers(m),
    }
    live_svc = None
    if service:
        net = (service.get("networkConfiguration") or {}).get("awsvpcConfiguration") or {}
        live_svc = {
            "desiredCount": service.get("desiredCount"),
            "launchType": service.get("launchType"),
            "subnets": sorted(net.get("subnets", [])),
            "securityGroups": sorted(net.get("securityGroups", [])),
            "assignPublicIp": net.get("assignPublicIp"),
            "deploymentConfiguration": {
                k: v for k, v in (service.get("deploymentConfiguration") or {}).items()
                if k in ("deploymentCircuitBreaker", "minimumHealthyPercent", "maximumPercent")
            },
            "loadBalancers": [
                {"targetGroupArn": l.get("targetGroupArn"), "containerName": l.get("containerName"),
                 "containerPort": l.get("containerPort")}
                for l in service.get("loadBalancers", [])
            ],
        }
    return {
        "desired": {"taskDefinition": desired_td, "service": desired_svc},
        "live": {"taskDefinition": live_td, "service": live_svc},
    }


def _ensure_log_group(name: str, region: str, m: Manifest) -> None:
    logs = _client("logs", region, m)
    try:
        logs.create_log_group(logGroupName=name)
    except ClientError as e:
        if e.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise

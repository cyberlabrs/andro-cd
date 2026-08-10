from typing import Any, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


class PortMapping(BaseModel):
    containerPort: int
    protocol: str = "tcp"
    # Optional AWS ECS "port name" — required when this port is referenced by
    # `service.serviceConnect.services[].portName`. Without it Service Connect
    # rejects the CreateService call with:
    #   "portName(...) does not refer to any named PortMapping in the container definitions"
    name: Optional[str] = None
    appProtocol: Optional[str] = None    # http | http2 | grpc — needed for Service Connect L7


class HealthCheckSpec(BaseModel):
    """Container-level health check (docker HEALTHCHECK semantics).
    Defaults mirror the ECS defaults so diffs stay stable."""
    command: list[str]                # e.g. ["CMD-SHELL", "curl -f http://localhost/ || exit 1"]
    interval: int = 30
    timeout: int = 5
    retries: int = 3
    startPeriod: Optional[int] = None


class MountPointSpec(BaseModel):
    """Bind a task volume into a container's filesystem."""
    sourceVolume: str                    # matches TaskDefinitionSpec.volumes[].name
    containerPath: str
    readOnly: bool = False


class FirelensSpec(BaseModel):
    """Turn a container into a FireLens log router (fluentbit/fluentd)."""
    type: str = "fluentbit"              # fluentbit | fluentd
    options: dict[str, str] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in ("fluentbit", "fluentd"):
            raise ValueError("firelens.type must be 'fluentbit' or 'fluentd'")
        return v


class ContainerSpec(BaseModel):
    name: str
    image: str
    essential: bool = True
    cpu: Optional[int] = None
    memory: Optional[int] = None
    memoryReservation: Optional[int] = None
    portMappings: list[Union[int, PortMapping]] = Field(default_factory=list)
    environment: Union[dict[str, Any], list[dict[str, str]]] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)
    command: Optional[list[str]] = None
    entryPoint: Optional[list[str]] = None
    logGroup: Optional[str] = None
    healthCheck: Optional[HealthCheckSpec] = None
    mountPoints: list[MountPointSpec] = Field(default_factory=list)
    dependsOn: list[dict[str, str]] = Field(default_factory=list)  # [{containerName, condition}]
    # Free-form logConfiguration lets a container send logs to FireLens (driver: awsfirelens)
    # or any other custom driver. When set, `logGroup` is ignored.
    logConfiguration: Optional[dict[str, Any]] = None
    firelensConfiguration: Optional[FirelensSpec] = None

    def env_list(self) -> list[dict[str, str]]:
        if isinstance(self.environment, dict):
            items = [{"name": k, "value": str(v)} for k, v in self.environment.items()]
        else:
            items = [{"name": e["name"], "value": str(e["value"])} for e in self.environment]
        return sorted(items, key=lambda e: e["name"])

    def secret_list(self) -> list[dict[str, str]]:
        return sorted(
            [{"name": k, "valueFrom": v} for k, v in self.secrets.items()],
            key=lambda s: s["name"],
        )

    def port_list(self) -> list[dict[str, Any]]:
        ports = []
        for p in self.portMappings:
            if isinstance(p, int):
                ports.append({"containerPort": p, "protocol": "tcp"})
            else:
                entry: dict[str, Any] = {"containerPort": p.containerPort, "protocol": p.protocol}
                if p.name:
                    entry["name"] = p.name
                if p.appProtocol:
                    entry["appProtocol"] = p.appProtocol
                ports.append(entry)
        return sorted(ports, key=lambda p: p["containerPort"])


class EFSAuthorizationSpec(BaseModel):
    accessPointId: Optional[str] = None      # scoped access via EFS Access Point
    iam: bool = False                        # use task IAM role for EFS mount


class EFSVolumeConfig(BaseModel):
    fileSystemId: str                        # fs-XXXXXXXX
    rootDirectory: str = "/"
    transitEncryption: bool = True           # required when accessPointId is set
    transitEncryptionPort: Optional[int] = None
    authorizationConfig: Optional[EFSAuthorizationSpec] = None


class VolumeSpec(BaseModel):
    """A task-level volume that containers can mount via ContainerSpec.mountPoints."""
    name: str
    efs: Optional[EFSVolumeConfig] = None

    @model_validator(mode="after")
    def _one_backend(self) -> "VolumeSpec":
        # Only EFS is supported today. When we add more backends (host, dockerVolume,
        # FSx) this will enforce exactly-one.
        if self.efs is None:
            raise ValueError(f"volume '{self.name}' requires an efs config")
        return self


class TaskDefinitionSpec(BaseModel):
    family: Optional[str] = None
    cpu: str = "256"
    memory: str = "512"
    resolveImages: bool = False   # pin mutable ECR tags to immutable digests at sync time
    networkMode: str = "awsvpc"
    executionRoleArn: Optional[str] = None
    taskRoleArn: Optional[str] = None
    containers: list[ContainerSpec] = Field(min_length=1)
    volumes: list[VolumeSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _mount_points_reference_volumes(self) -> "TaskDefinitionSpec":
        volume_names = {v.name for v in self.volumes}
        for c in self.containers:
            for mp in c.mountPoints:
                if mp.sourceVolume not in volume_names:
                    raise ValueError(
                        f"container '{c.name}' mounts unknown volume '{mp.sourceVolume}' "
                        f"(declare it under spec.taskDefinition.volumes)")
        return self

    @field_validator("cpu", "memory", mode="before")
    @classmethod
    def _coerce_str(cls, v: Any) -> str:
        return str(v)


class NetworkSpec(BaseModel):
    vpc: Optional[str] = None
    subnets: list[str] = Field(min_length=1)
    securityGroups: list[str] = Field(default_factory=list)


class AutoscalingSpec(BaseModel):
    minCount: int
    maxCount: int
    targetCpu: Optional[int] = None      # target CPU utilization %
    targetMemory: Optional[int] = None   # target memory utilization %
    # Requires spec.service.loadBalancer (either targetGroupArn or create). Value is
    # the target average requests/target/minute the ALB should hold each task at —
    # AWS calls this metric ALBRequestCountPerTarget.
    targetRequestsPerTarget: Optional[float] = None


class LBHealthCheckSpec(BaseModel):
    """Target-group health check (managed load-balancer mode)."""
    path: str = "/"
    interval: int = 30
    timeout: int = 5
    healthyThreshold: int = 3
    unhealthyThreshold: int = 3
    matcher: str = "200-399"             # HTTP codes counted as healthy

    @model_validator(mode="after")
    def _interval_gt_timeout(self) -> "LBHealthCheckSpec":
        if self.timeout >= self.interval:
            raise ValueError("healthCheck.timeout must be smaller than healthCheck.interval")
        return self


class LBRuleSpec(BaseModel):
    """Listener rule routing traffic to the managed target group."""
    priority: int                        # unique per listener; applied at creation
    hostHeader: Optional[str] = None     # e.g. api.example.com
    pathPattern: Optional[str] = None    # e.g. /api/*

    @model_validator(mode="after")
    def _condition_required(self) -> "LBRuleSpec":
        if not self.hostHeader and not self.pathPattern:
            raise ValueError("rule requires hostHeader and/or pathPattern")
        return self


class ManagedLBSpec(BaseModel):
    """Create the target group + listener rule from the manifest (the ALB itself
    and its listener are infrastructure — bring your own)."""
    listenerArn: str                     # existing ALB listener the rule attaches to
    port: Optional[int] = None           # target group port; defaults to containerPort
    protocol: str = "HTTP"               # protocol towards the targets: HTTP | HTTPS
    healthCheck: LBHealthCheckSpec = Field(default_factory=LBHealthCheckSpec)
    rule: LBRuleSpec

    @field_validator("protocol")
    @classmethod
    def _protocol(cls, v: str) -> str:
        if v not in ("HTTP", "HTTPS"):
            raise ValueError("protocol must be HTTP or HTTPS")
        return v


class LoadBalancerSpec(BaseModel):
    targetGroupArn: Optional[str] = None  # reference mode: attach to an existing TG
    containerName: Optional[str] = None   # defaults to the first container
    containerPort: int
    create: Optional[ManagedLBSpec] = None  # managed mode: TG + rule created from Git
    # --- native blue/green (deploymentController=ECS, strategy=BLUE_GREEN|CANARY|LINEAR) ---
    # ECS routes production traffic to `targetGroupArn` and shifts traffic through the
    # ALB's productionListenerRule; `alternateTargetGroupArn` receives the green fleet,
    # optionally exposed via `testListenerRule` for dark canary validation.
    alternateTargetGroupArn: Optional[str] = None
    productionListenerRule: Optional[str] = None
    testListenerRule: Optional[str] = None            # dark canary (bring your own listener rule)
    roleArn: Optional[str] = None                     # ELB role Andro-CD passes to ECS

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> "LoadBalancerSpec":
        if bool(self.targetGroupArn) == bool(self.create):
            raise ValueError("loadBalancer requires exactly one of targetGroupArn (reference) or create (managed)")
        return self


class LifecycleHookSpec(BaseModel):
    """Deployment lifecycle hook fired at one or more stages during a native
    blue/green / canary / linear rollout. Two target types:
      - AWS_LAMBDA: invoke a Lambda that returns hookStatus SUCCEEDED|FAILED|IN_PROGRESS
      - PAUSE:      pause the deployment until an operator resumes it
    """
    targetType: str = "AWS_LAMBDA"                    # AWS_LAMBDA | PAUSE
    hookTargetArn: Optional[str] = None               # Lambda function ARN (AWS_LAMBDA only)
    roleArn: Optional[str] = None                     # role ECS assumes to invoke the hook
    stages: list[str] = Field(default_factory=list)   # lifecycleStages
    hookDetails: dict[str, Any] = Field(default_factory=dict)
    timeoutMinutes: Optional[int] = None
    timeoutAction: str = "ROLLBACK"                   # ROLLBACK | CONTINUE

    _VALID_STAGES = {
        "RECONCILE_SERVICE", "PRE_SCALE_UP", "POST_SCALE_UP",
        "TEST_TRAFFIC_SHIFT", "POST_TEST_TRAFFIC_SHIFT",
        "PRE_PRODUCTION_TRAFFIC_SHIFT", "PRODUCTION_TRAFFIC_SHIFT",
        "POST_PRODUCTION_TRAFFIC_SHIFT",
    }

    @field_validator("targetType")
    @classmethod
    def _target_type(cls, v: str) -> str:
        if v not in ("AWS_LAMBDA", "PAUSE"):
            raise ValueError("lifecycleHook.targetType must be AWS_LAMBDA or PAUSE")
        return v

    @field_validator("timeoutAction")
    @classmethod
    def _timeout_action(cls, v: str) -> str:
        if v not in ("ROLLBACK", "CONTINUE"):
            raise ValueError("lifecycleHook.timeoutAction must be ROLLBACK or CONTINUE")
        return v

    @model_validator(mode="after")
    def _validate_hook(self) -> "LifecycleHookSpec":
        if not self.stages:
            raise ValueError("lifecycleHook.stages must contain at least one stage")
        bad = [s for s in self.stages if s not in self._VALID_STAGES]
        if bad:
            raise ValueError(
                f"invalid lifecycleHook.stages: {bad} — expected one of "
                f"{sorted(self._VALID_STAGES)}")
        if self.targetType == "AWS_LAMBDA" and not self.hookTargetArn:
            raise ValueError("lifecycleHook.hookTargetArn is required when targetType=AWS_LAMBDA")
        return self


class AlarmRollbackSpec(BaseModel):
    """CloudWatch alarms that trigger auto-rollback of a deployment when they fire."""
    alarmNames: list[str] = Field(min_length=1)
    rollback: bool = True                            # auto-rollback when any alarm fires
    enable: bool = True                              # temporarily disable without deleting


class DeploymentStrategySpec(BaseModel):
    """Native ECS deployment strategy. `type=ROLLING` reproduces the existing behavior
    (default), `BLUE_GREEN` / `CANARY` / `LINEAR` require deploymentController=ECS,
    a second target group and — for BLUE_GREEN and CANARY — production listener rule
    references on the loadBalancer.
    """
    type: str = "ROLLING"                            # ROLLING | BLUE_GREEN | CANARY | LINEAR
    bakeTimeMinutes: Optional[int] = None
    canaryPercent: Optional[float] = None            # CANARY: initial % shifted
    canaryBakeTimeMinutes: Optional[int] = None
    linearStepPercent: Optional[float] = None        # LINEAR: % per step
    linearStepBakeTimeMinutes: Optional[int] = None
    lifecycleHooks: list[LifecycleHookSpec] = Field(default_factory=list)
    alarms: Optional[AlarmRollbackSpec] = None

    @field_validator("type")
    @classmethod
    def _type(cls, v: str) -> str:
        if v not in ("ROLLING", "BLUE_GREEN", "CANARY", "LINEAR"):
            raise ValueError("deploymentStrategy.type must be one of ROLLING, BLUE_GREEN, CANARY, LINEAR")
        return v

    @model_validator(mode="after")
    def _strategy_requirements(self) -> "DeploymentStrategySpec":
        if self.type == "CANARY":
            if self.canaryPercent is None:
                raise ValueError("deploymentStrategy.canaryPercent is required for type=CANARY")
            if not 0 < self.canaryPercent < 100:
                raise ValueError("canaryPercent must be between 0 and 100 (exclusive)")
        if self.type == "LINEAR":
            if self.linearStepPercent is None:
                raise ValueError("deploymentStrategy.linearStepPercent is required for type=LINEAR")
            if not 0 < self.linearStepPercent <= 100:
                raise ValueError("linearStepPercent must be between 0 and 100")
        return self


class CapacityProviderSpec(BaseModel):
    """Weighted capacity provider strategy (e.g. FARGATE_SPOT for cost savings).
    When set, the service uses the strategy instead of plain launchType."""
    provider: str                        # FARGATE | FARGATE_SPOT | custom provider name
    weight: int = 1
    base: int = 0


class ServiceConnectClientAlias(BaseModel):
    """DNS name + port the client uses inside the namespace to reach this service."""
    port: int
    dnsName: Optional[str] = None      # defaults to the discovery name (AWS behavior)


class ServiceConnectService(BaseModel):
    """One port of the task advertised via Service Connect."""
    portName: str                      # matches taskDefinition.containers[].portMappings[].name
    discoveryName: Optional[str] = None  # cloud-map service; defaults to portName
    clientAliases: list[ServiceConnectClientAlias] = Field(default_factory=list)


class ServiceConnectSpec(BaseModel):
    """Per-service Service Connect config. `namespace` overrides the cluster default;
    otherwise uses spec.serviceConnectNamespace of the cluster kind."""
    enabled: bool = True
    namespace: Optional[str] = None    # cloud-map namespace name or ARN
    services: list[ServiceConnectService] = Field(default_factory=list)


class ServiceSettings(BaseModel):
    desiredCount: int = 1
    launchType: str = "FARGATE"
    assignPublicIp: bool = False
    circuitBreaker: bool = True
    rollbackOnFailure: bool = True
    minimumHealthyPercent: Optional[int] = None
    maximumPercent: Optional[int] = None
    autoscaling: Optional[AutoscalingSpec] = None
    loadBalancer: Optional[LoadBalancerSpec] = None
    capacityProviders: list[CapacityProviderSpec] = Field(default_factory=list)
    serviceConnect: Optional[ServiceConnectSpec] = None
    deploymentStrategy: Optional[DeploymentStrategySpec] = None


class HookSpec(BaseModel):
    command: list[str]
    container: Optional[str] = None      # defaults to the first container
    timeoutSeconds: int = 300


class HooksSpec(BaseModel):
    preSync: Optional[HookSpec] = None   # one-off task before service update (e.g. migrations)
    postSync: Optional[HookSpec] = None  # one-off task after service update


class RunPolicy(BaseModel):
    """kind: ECSTask — how the one-off task is run.
    runOnSync auto-runs it once whenever the task definition changes (migrations);
    otherwise it only runs on demand via the 'Run now' button / API."""
    runOnSync: bool = False
    count: int = 1                       # tasks to launch per run (1–10)

    @field_validator("count")
    @classmethod
    def _count_range(cls, v: int) -> int:
        if not 1 <= v <= 10:
            raise ValueError("runPolicy.count must be between 1 and 10")
        return v


class ScheduleSpec(BaseModel):
    expression: str                      # cron(...) or rate(...) — EventBridge Scheduler syntax
    roleArn: str                         # role EventBridge assumes to run the task
    enabled: bool = True

    @field_validator("expression")
    @classmethod
    def _validate_expression(cls, v: str) -> str:
        import re
        # EventBridge Scheduler accepts three forms:
        #   at(2020-01-01T00:00:00), cron(minutes hours dom month dow year),
        #   rate(N minute[s]|hour[s]|day[s])
        pattern = re.compile(
            r"^(?:at\(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\)"
            r"|cron\([^)]+\)"
            r"|rate\(\d+\s+(?:minute|minutes|hour|hours|day|days)\))$"
        )
        if not pattern.match(v.strip()):
            raise ValueError(
                "schedule.expression must be one of: cron(...), rate(N minutes|hours|days), at(YYYY-MM-DDTHH:MM:SS)"
            )
        return v.strip()


class Metadata(BaseModel):
    name: str
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("labels", mode="before")
    @classmethod
    def _coerce_labels(cls, v: Any) -> Any:
        if isinstance(v, dict):
            return {str(k): str(val) for k, val in v.items()}
        return v


DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


class SyncWindow(BaseModel):
    """A UTC time window during which auto-sync is allowed (deploy freeze outside).
    Manual sync from the UI/API always works."""
    days: list[str] = Field(default_factory=lambda: list(DAY_NAMES))
    start: str = "00:00"              # inclusive, HH:MM UTC
    end: str = "24:00"                # exclusive, HH:MM UTC (24:00 = end of day)

    @field_validator("days")
    @classmethod
    def _valid_days(cls, v: list[str]) -> list[str]:
        for d in v:
            if d not in DAY_NAMES:
                raise ValueError(f"invalid day '{d}', expected one of {', '.join(DAY_NAMES)}")
        return v

    @field_validator("start", "end")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        import re
        if not re.fullmatch(r"([01]\d|2[0-4]):[0-5]\d", v):
            raise ValueError(f"invalid time '{v}', expected HH:MM (00:00–24:00)")
        return v


class SyncPolicy(BaseModel):
    autoSync: Optional[bool] = None   # None = inherit global AUTO_SYNC
    selfHeal: bool = False            # revert manual drift in AWS (not just git changes)
    prune: bool = False               # delete the service when removed from git
    syncWindows: list[SyncWindow] = Field(default_factory=list)  # empty = always allowed


class Spec(BaseModel):
    region: Optional[str] = None
    awsProfile: Optional[str] = None   # named AWS profile; None = default credentials chain
    cluster: Optional[str] = None      # required for ECSService/ECSScheduledTask;
                                       # defaults to metadata.name for ECSCluster
    wave: int = 0                      # sync wave: lower waves must be Synced+Healthy first
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    network: Optional[NetworkSpec] = None            # required unless kind ECSCluster
    taskDefinition: Optional[TaskDefinitionSpec] = None  # required unless kind ECSCluster
    syncPolicy: SyncPolicy = Field(default_factory=SyncPolicy)
    hooks: HooksSpec = Field(default_factory=HooksSpec)
    schedule: Optional[ScheduleSpec] = None   # required for kind ECSScheduledTask
    runPolicy: RunPolicy = Field(default_factory=RunPolicy)   # kind ECSTask

    # --- kind: ECSCluster only ---
    containerInsights: Optional[str] = None   # disabled | enabled | enhanced
    capacityProviders: list[str] = Field(default_factory=list)   # attach to the cluster
    defaultCapacityProviderStrategy: list[CapacityProviderSpec] = Field(default_factory=list)
    serviceConnectNamespace: Optional[str] = None    # Cloud Map namespace (name or ARN)

    @field_validator("containerInsights")
    @classmethod
    def _valid_insights(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("disabled", "enabled", "enhanced"):
            raise ValueError("containerInsights must be disabled, enabled or enhanced")
        return v


class Manifest(BaseModel):
    apiVersion: str
    # enum surfaces in the published JSON Schema (/api/schema) for manifest-repo CI
    kind: str = Field(json_schema_extra={
        "enum": ["ECSService", "ECSScheduledTask", "ECSCluster", "ECSTask"]})
    metadata: Metadata
    spec: Spec

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in ("ECSService", "ECSScheduledTask", "ECSCluster", "ECSTask"):
            raise ValueError(
                f"unsupported kind '{v}', expected ECSService, ECSScheduledTask, ECSCluster or ECSTask")
        return v

    @model_validator(mode="after")
    def _kind_requirements(self) -> "Manifest":
        if self.kind == "ECSCluster":
            if self.spec.cluster is None:
                self.spec.cluster = self.metadata.name
            if self.spec.defaultCapacityProviderStrategy:
                strategy_providers = {p.provider for p in self.spec.defaultCapacityProviderStrategy}
                missing = strategy_providers - set(self.spec.capacityProviders)
                # FARGATE providers are attached automatically; custom ones must be listed
                if any(not p.startswith("FARGATE") for p in missing):
                    raise ValueError(
                        "defaultCapacityProviderStrategy providers must be listed in spec.capacityProviders")
            return self
        # ECSService / ECSScheduledTask
        if self.spec.cluster is None:
            raise ValueError(f"spec.cluster is required for kind {self.kind}")
        if self.spec.network is None:
            raise ValueError(f"spec.network is required for kind {self.kind}")
        if self.spec.taskDefinition is None:
            raise ValueError(f"spec.taskDefinition is required for kind {self.kind}")
        if self.kind == "ECSScheduledTask" and self.spec.schedule is None:
            raise ValueError("spec.schedule is required for kind ECSScheduledTask")
        # ALB request-count autoscaling needs a target group to reference in the
        # ResourceLabel: reject the combination at parse time with a clear message
        # rather than later when the ECS API rejects the policy.
        a = self.spec.service.autoscaling if self.spec.service else None
        if a and a.targetRequestsPerTarget is not None and not (self.spec.service and self.spec.service.loadBalancer):
            raise ValueError(
                "autoscaling.targetRequestsPerTarget requires spec.service.loadBalancer")
        # Native blue/green / canary / linear all need traffic shifting between two TGs
        # on an ALB, so a loadBalancer with alternateTargetGroupArn + productionListenerRule
        # is mandatory. Reject at parse time so the error is obvious in the diff, not
        # after an ECS API rejection.
        ds = self.spec.service.deploymentStrategy if self.spec.service else None
        if ds and ds.type in ("BLUE_GREEN", "CANARY", "LINEAR"):
            lb = self.spec.service.loadBalancer if self.spec.service else None
            if not lb or not lb.alternateTargetGroupArn or not lb.productionListenerRule:
                raise ValueError(
                    f"deploymentStrategy.type={ds.type} requires spec.service.loadBalancer with "
                    "alternateTargetGroupArn and productionListenerRule")
        return self

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def family(self) -> str:
        if self.spec.taskDefinition is None:
            return self.metadata.name
        return self.spec.taskDefinition.family or self.metadata.name

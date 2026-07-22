from typing import Any, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


class PortMapping(BaseModel):
    containerPort: int
    protocol: str = "tcp"


class HealthCheckSpec(BaseModel):
    """Container-level health check (docker HEALTHCHECK semantics).
    Defaults mirror the ECS defaults so diffs stay stable."""
    command: list[str]                # e.g. ["CMD-SHELL", "curl -f http://localhost/ || exit 1"]
    interval: int = 30
    timeout: int = 5
    retries: int = 3
    startPeriod: Optional[int] = None


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
                ports.append({"containerPort": p.containerPort, "protocol": p.protocol})
        return sorted(ports, key=lambda p: p["containerPort"])


class TaskDefinitionSpec(BaseModel):
    family: Optional[str] = None
    cpu: str = "256"
    memory: str = "512"
    resolveImages: bool = False   # pin mutable ECR tags to immutable digests at sync time
    networkMode: str = "awsvpc"
    executionRoleArn: Optional[str] = None
    taskRoleArn: Optional[str] = None
    containers: list[ContainerSpec] = Field(min_length=1)

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

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> "LoadBalancerSpec":
        if bool(self.targetGroupArn) == bool(self.create):
            raise ValueError("loadBalancer requires exactly one of targetGroupArn (reference) or create (managed)")
        return self


class CapacityProviderSpec(BaseModel):
    """Weighted capacity provider strategy (e.g. FARGATE_SPOT for cost savings).
    When set, the service uses the strategy instead of plain launchType."""
    provider: str                        # FARGATE | FARGATE_SPOT | custom provider name
    weight: int = 1
    base: int = 0


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


class HookSpec(BaseModel):
    command: list[str]
    container: Optional[str] = None      # defaults to the first container
    timeoutSeconds: int = 300


class HooksSpec(BaseModel):
    preSync: Optional[HookSpec] = None   # one-off task before service update (e.g. migrations)
    postSync: Optional[HookSpec] = None  # one-off task after service update


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
    kind: str = Field(json_schema_extra={"enum": ["ECSService", "ECSScheduledTask", "ECSCluster"]})
    metadata: Metadata
    spec: Spec

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in ("ECSService", "ECSScheduledTask", "ECSCluster"):
            raise ValueError(
                f"unsupported kind '{v}', expected ECSService, ECSScheduledTask or ECSCluster")
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
        return self

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def family(self) -> str:
        if self.spec.taskDefinition is None:
            return self.metadata.name
        return self.spec.taskDefinition.family or self.metadata.name

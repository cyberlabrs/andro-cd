# Manifest reference

All manifests share the same envelope. Multiple YAML documents per file are allowed
(`---` separated); files are discovered recursively.

```yaml
apiVersion: andro-cd/v1
kind: ECSService | ECSScheduledTask | ECSServiceSet | ECSCluster
metadata:
  name: my-app                          # unique across all repos
  labels:                               # chips in the UI, searchable, propagated as AWS tags
    team: platform
    env: production
spec:
  region: us-east-1                     # precedence: manifest > profile default > AWS_REGION
  awsProfile: prod-account              # optional named profile (multi-account)
  cluster: prod                         # auto-created if missing
  wave: 0                               # sync wave — lower waves settle first
  syncPolicy: { ... }                   # see below
```

A machine-readable JSON Schema is served at `GET /api/schema` — wire it into your
manifest repo's CI together with [`androcd validate`](api.md#cli).

## Sync policy

```yaml
spec:
  syncPolicy:
    autoSync: true          # overrides global AUTO_SYNC for this app
    selfHeal: false         # revert manual AWS drift (default: only Git changes sync)
    prune: false            # delete the resource when removed from Git
    syncWindows:            # UTC windows when auto-sync is allowed (empty = always)
      - days: [Mon, Tue, Wed, Thu, Fri]
        start: "07:00"
        end: "19:00"
```

- **autoSync** — `false` for critical services that require a human click.
- **selfHeal** — off by default: console changes show as OutOfSync but aren't reverted.
- **prune** — resources persist after manifest removal unless `true` (or manual Prune).
- **syncWindows** — deploy freeze outside the windows; `start` inclusive, `end`
  exclusive, `24:00` = end of day. Manual sync always works.

## `kind: ECSService`

```yaml
spec:
  service:
    desiredCount: 2                     # ignored when autoscaling is configured
    launchType: FARGATE                 # FARGATE | EC2
    assignPublicIp: true
    circuitBreaker: true                # ECS deployment circuit breaker (default true)
    rollbackOnFailure: true             # auto-rollback on failed rollout (default true)
    minimumHealthyPercent: 100
    maximumPercent: 200
    autoscaling:                        # target-tracking; autoscaler owns desiredCount
      minCount: 1
      maxCount: 10
      targetCpu: 60
      targetMemory: 70
    loadBalancer:
      containerName: web                # defaults to the first container
      containerPort: 8080
      # EITHER reference an existing target group:
      targetGroupArn: arn:aws:elasticloadbalancing:...
      # OR let Andro-CD create the TG + listener rule (managed mode):
      # create:
      #   listenerArn: arn:aws:elasticloadbalancing:...:listener/app/main/abc/def
      #   port: 8080                    # TG port; defaults to containerPort
      #   protocol: HTTP                # towards the targets: HTTP | HTTPS
      #   rule:
      #     priority: 10                # unique per listener
      #     hostHeader: api.example.com # and/or pathPattern
      #     pathPattern: /api/*
      #   healthCheck:
      #     path: /health
      #     interval: 30
      #     timeout: 5
      #     healthyThreshold: 3
      #     unhealthyThreshold: 3
      #     matcher: "200-399"
    capacityProviders:                  # weighted strategy instead of launchType
      - provider: FARGATE_SPOT
        weight: 3
      - provider: FARGATE
        weight: 1
        base: 1                         # always ≥1 on on-demand
  network:
    subnets: [subnet-aaa, subnet-bbb]
    securityGroups: [sg-0ccc]
  hooks:
    preSync:                            # one-off task before the rollout (migrations)
      command: ["python", "manage.py", "migrate"]
      container: web                    # defaults to the first container
      timeoutSeconds: 600
    postSync:
      command: ["curl", "-X", "POST", "https://hooks.example/deployed"]
  taskDefinition:
    family: web-app                     # defaults to metadata.name
    cpu: "256"
    memory: "512"
    networkMode: awsvpc
    executionRoleArn: arn:aws:iam::...:role/ecsTaskExecutionRole
    taskRoleArn: arn:aws:iam::...:role/appRole
    resolveImages: false                # true → pin ECR tags to immutable digests
    containers:
      - name: web
        image: nginx:1.27
        essential: true
        cpu: 0
        memory: 512                     # hard limit
        memoryReservation: 256          # soft limit
        portMappings: [80, 443]         # ints or {containerPort, protocol}
        environment:                    # map or list of {name, value}
          APP_ENV: production
        secrets:                        # name -> SSM / Secrets Manager ARN
          DB_PASSWORD: arn:aws:ssm:...:parameter/db-pass
        command: ["gunicorn", "app.wsgi"]
        entryPoint: ["/entrypoint.sh"]
        logGroup: /ecs/web-app          # awslogs driver; group auto-created
        healthCheck:                    # docker HEALTHCHECK semantics
          command: ["CMD-SHELL", "curl -f http://localhost/health || exit 1"]
          interval: 30
          timeout: 5
          retries: 3
          startPeriod: 15
```

!!! note "ECR digest pinning"
    With `resolveImages: true`, mutable tags (`app:latest`) are resolved via
    `ecr:DescribeImages` and pinned to `app@sha256:…` at sync time — immutable deploys
    and reliable drift detection. Non-ECR images pass through unchanged.

## `kind: ECSScheduledTask`

Cron-style task backed by EventBridge Scheduler:

```yaml
apiVersion: andro-cd/v1
kind: ECSScheduledTask
metadata:
  name: nightly-report
spec:
  cluster: batch
  schedule:
    expression: cron(0 3 * * ? *)       # cron(...), rate(...) or at(...)
    roleArn: arn:aws:iam::...:role/androcdSchedulerRole
    enabled: true
  network:
    subnets: [subnet-aaa]
    securityGroups: [sg-0ccc]
  taskDefinition:
    containers:
      - name: report
        image: 123.dkr.ecr.us-east-1.amazonaws.com/reports:v2
        command: ["python", "-m", "reports.nightly"]
```

The scheduler role needs `ecs:RunTask` plus `iam:PassRole` for the task's roles.

## `kind: ECSTask`

A one-off task or job — migrations, batch work — with no long-running service:

```yaml
apiVersion: andro-cd/v1
kind: ECSTask
metadata:
  name: db-migrate
spec:
  cluster: batch
  service: {launchType: FARGATE}       # launch settings for the run
  runPolicy:
    runOnSync: true                     # run once whenever the task definition changes
    count: 1                            # tasks per run (1–10)
  network:
    subnets: [subnet-aaa]
    securityGroups: [sg-0ccc]
  taskDefinition:
    containers:
      - name: migrate
        image: 123.dkr.ecr.us-east-1.amazonaws.com/api:latest
        command: ["python", "manage.py", "migrate"]
```

- Andro-CD reconciles only the **cluster + task definition** — sync status is "Synced"
  when the definition is registered and current.
- Launch it on demand with the **Run now** button (or `POST /api/apps/{name}/run`,
  optional `{count}`), or automatically with `runPolicy.runOnSync: true`.
- Runs are tagged with `startedBy=androcd-task-<name>` and show up in the **Tasks** tab
  with exit codes; health reflects the last run's outcome.
- Prune is a no-op (a task owns no long-lived resource; task definitions are kept).

## `kind: ECSServiceSet` (app-of-apps)

Generate N applications from one template:

```yaml
apiVersion: andro-cd/v1
kind: ECSServiceSet
metadata:
  name: api-environments
spec:
  generators:
    - values: {env: dev,  count: 1}
    - values: {env: prod, count: 3}
  template:
    apiVersion: andro-cd/v1
    kind: ECSService
    metadata:
      name: api-${env}
    spec:
      cluster: ${env}
      service: {desiredCount: "${count}"}
      # ...
```

`${var}` placeholders substitute verbatim; each generator produces one app.

## `kind: ECSCluster`

Manage the cluster itself from Git — insights, capacity providers, default strategy,
Service Connect namespace and tags:

```yaml
apiVersion: andro-cd/v1
kind: ECSCluster
metadata:
  name: production
  labels: {team: platform}
spec:
  region: eu-central-1
  wave: 0                              # create before wave-1 services target it
  containerInsights: enhanced          # disabled | enabled | enhanced
  capacityProviders: [FARGATE, FARGATE_SPOT]
  defaultCapacityProviderStrategy:
    - {provider: FARGATE_SPOT, weight: 3}
    - {provider: FARGATE, weight: 1, base: 1}
  serviceConnectNamespace: internal    # Cloud Map namespace (name or ARN)
```

- No `network`/`taskDefinition` — those belong to services.
- Omitted fields are left untouched on the live cluster (no churn).
- **Prune is safe**: deletion is refused while the cluster still has active services
  or running tasks.
- Healthy when the cluster is `ACTIVE`; pairs naturally with waves (cluster wave 0,
  services wave 1+).

## Values files (templating)

`values.yaml` / `values.yml` files are not manifests — they define `${key}`
substitutions for every manifest in their directory subtree:

```
manifests/
├── values.yaml              # tag: stable, team: platform
├── web.yaml                 # image: repo/app:${tag}  → repo/app:stable
└── envs/prod/
    ├── values.yaml          # tag: v42
    └── web.yaml             # image: repo/app:${tag}  → repo/app:v42
```

- Files layer from the repo root down; the **closest file wins** on conflicts.
- Nested mappings flatten to dotted keys: `image: {tag: v1}` → `${image.tag}`.
- The CLI validator applies the same substitution, so CI matches the server exactly.

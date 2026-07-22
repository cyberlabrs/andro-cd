# Andro-CD Documentation

**Andro-CD** is a pull-based GitOps controller for AWS ECS. It watches Git repositories
containing YAML manifests, compares them against live AWS state, and reconciles the two —
the same model ArgoCD uses for Kubernetes, but for ECS services, task definitions and
scheduled tasks.

- **Backend:** Python (FastAPI + boto3)
- **Frontend:** React + Vite (this UI)
- **Deploy:** single Docker image with an optional Postgres sidecar
- **Auth:** GitHub OAuth with RBAC (viewer / operator / admin)

Everything below assumes you're viewing this in the running service — links jump to
sections, screenshots reflect the current UI.

---

## Quick start

### 1. Run locally

```bash
git clone https://github.com/cyberlabrs/andro-cd   # your fork
cd andro-cd
cp .env.example .env                    # fill in secrets — see the Configuration section
docker compose up --build
```

Open **http://localhost:8080**. Without repositories connected the dashboard is empty and
guides you to add one.

### 2. Connect a manifest repository

Click **Repositories → Connect**. Three authentication methods are supported:

| Method | When to use | What to paste |
|---|---|---|
| **HTTPS / token** | Personal repos, quick setup | GitHub personal access token (repo scope) |
| **SSH key** | Organization deploy keys | Private key (`-----BEGIN OPENSSH PRIVATE KEY-----`) |
| **GitHub App** | Team-wide, no personal tokens | App ID + Installation ID + PEM private key |

For GitHub App the backend generates a JWT, exchanges it for a short-lived installation
token, caches it, and refreshes automatically before it expires — like ArgoCD's repo-server
does with its Git providers.

### 3. Write your first manifest

Push this to the manifest repo:

```yaml
apiVersion: andro-cd/v1
kind: ECSService
metadata:
  name: hello
spec:
  region: us-east-1
  cluster: demo                         # auto-created if it doesn't exist
  service:
    desiredCount: 1
    launchType: FARGATE
    assignPublicIp: true
  network:
    subnets: [subnet-XXXXXXXX]
    securityGroups: [sg-XXXXXXXX]
  taskDefinition:
    cpu: "256"
    memory: "512"
    executionRoleArn: arn:aws:iam::123456789012:role/ecsTaskExecutionRole
    containers:
      - name: web
        image: nginx:1.27
        portMappings: [80]
```

Within ~60 s the reconciler picks it up, creates the cluster, registers the task
definition, and creates the service. Watch the progress in **Overview → Deployment**.

---

## Concepts

### Sync status

| Status | Meaning |
|---|---|
| **Synced** | Live AWS state matches the manifest |
| **OutOfSync** | Manifest differs from AWS (see **Diff** tab for details) |
| **Syncing** | Reconciliation in progress |
| **Error** | The last sync attempt failed (message on the card) |
| **Orphaned** | App was removed from Git; AWS resources still exist |
| **Unknown** | Not evaluated yet (first tick after startup) |

### Health

| Health | Meaning |
|---|---|
| **Healthy** | `runningCount == desiredCount`, deployment `COMPLETED` |
| **Progressing** | Rolling update in progress |
| **Degraded** | Deployment failed or `runningCount < desiredCount` |
| **Unknown** | Service doesn't exist yet or app is not ECSService |

### Reconcile loop

1. Every `SYNC_INTERVAL` seconds (default 60) — or immediately on a GitHub webhook —
   the controller runs one reconciliation pass.
2. Every tracked repository is fetched, all `*.yaml` / `*.yml` files are parsed.
3. `ECSServiceSet` documents are expanded into individual `ECSService` apps.
4. For every app: diff live vs desired → if OutOfSync and eligible → apply.
5. Sync waves proceed in order — a wave only starts when all lower waves are Synced+Healthy.
6. Metrics, notifications, sync history and app state get updated in the database.

### Sync policy

Per-app in the manifest, all optional:

```yaml
spec:
  syncPolicy:
    autoSync: true          # overrides the global AUTO_SYNC for this app
    selfHeal: false         # revert manual drift in AWS (default: only Git changes trigger sync)
    prune: false            # delete the ECS service when the manifest is removed from Git
    syncWindows:            # optional: UTC windows when auto-sync is allowed
      - days: [Mon, Tue, Wed, Thu, Fri]
        start: "07:00"
        end: "19:00"
```

- **autoSync** — set to `false` for critical services that require a human click.
- **selfHeal** — with this off (default), someone changing something in the AWS console
  will show up as OutOfSync but not be reverted. Argo-style safety net.
- **prune** — even after removing the manifest, resources persist unless this is `true`
  or the operator clicks **Prune** manually.
- **syncWindows** — deploy freeze outside the listed windows (times are UTC; `start`
  inclusive, `end` exclusive, `24:00` = end of day). Empty list = always allowed.
  Manual **Sync** from the UI/API works regardless — windows only gate auto-sync.

---

## Manifest reference

### Common fields

```yaml
apiVersion: andro-cd/v1
kind: ECSService | ECSScheduledTask | ECSServiceSet | ECSCluster
metadata:
  name: my-app                          # unique across all repos
  labels:                               # optional; shown as chips, filterable in the UI
    team: platform
    env: production
spec:
  region: us-east-1                     # optional; precedence: manifest > profile default > AWS_REGION
  awsProfile: prod-account              # optional named profile from the AWS Profiles panel
  cluster: prod                         # created automatically if missing
  wave: 0                               # sync wave; lower waves must settle before higher ones start
  syncPolicy: { ... }                   # see above
```

### `kind: ECSService`

The service portion of a manifest:

```yaml
spec:
  service:
    desiredCount: 2                     # ignored when autoscaling is configured
    launchType: FARGATE                 # FARGATE | EC2
    assignPublicIp: true                # for public subnets without a NAT gateway
    circuitBreaker: true                # ECS deployment circuit breaker (default: true)
    rollbackOnFailure: true             # auto-rollback when a rollout fails (default: true)
    minimumHealthyPercent: 100          # optional
    maximumPercent: 200                 # optional
    autoscaling:                        # optional — see Autoscaling below
      minCount: 1
      maxCount: 10
      targetCpu: 60
      targetMemory: 70
    loadBalancer:                       # optional — attach to existing target group
      targetGroupArn: arn:aws:elasticloadbalancing:...
      containerName: web                # defaults to the first container
      containerPort: 8080
    capacityProviders:                  # optional — weighted strategy instead of launchType
      - provider: FARGATE_SPOT          # 3 of 4 tasks on Spot…
        weight: 3
      - provider: FARGATE
        weight: 1
        base: 1                         # …but always ≥1 on on-demand
  network:
    subnets: [subnet-aaa, subnet-bbb]
    securityGroups: [sg-0ccc]
  hooks:                                # optional — see Hooks below
    preSync:
      command: ["python", "manage.py", "migrate"]
      timeoutSeconds: 600
    postSync:
      command: ["curl", "-X", "POST", "https://hooks.example/deployed"]
  taskDefinition:
    family: web-app                     # optional; defaults to metadata.name
    cpu: "256"
    memory: "512"
    networkMode: awsvpc                 # default
    executionRoleArn: arn:aws:iam::...:role/ecsTaskExecutionRole
    taskRoleArn: arn:aws:iam::...:role/appRole
    resolveImages: false                # true → pin ECR tags to immutable digests at sync time
    containers:
      - name: web
        image: nginx:1.27               # or 123.dkr.ecr.us-east-1.amazonaws.com/app:v1
        essential: true
        cpu: 0                          # optional container-level
        memory: 512                     # optional hard limit
        memoryReservation: 256          # optional soft limit
        portMappings: [80, 443]         # ints or {containerPort, protocol}
        environment:                    # map or list of {name, value}
          APP_ENV: production
          LOG_LEVEL: info
        secrets:                        # map name -> SSM/SecretsManager ARN
          DB_PASSWORD: arn:aws:ssm:...:parameter/db-pass
        command: ["gunicorn", "app.wsgi"]
        entryPoint: ["/entrypoint.sh"]
        logGroup: /ecs/web-app          # awslogs driver; group auto-created
        healthCheck:                    # optional — docker HEALTHCHECK semantics
          command: ["CMD-SHELL", "curl -f http://localhost/health || exit 1"]
          interval: 30                  # defaults mirror ECS: 30s/5s/3 retries
          timeout: 5
          retries: 3
          startPeriod: 15               # optional grace period
```

`metadata.labels` also propagate to **AWS tags** on the cluster, task definition and
service (with `propagateTags: SERVICE`) — cost allocation per team/app for free.

### `kind: ECSScheduledTask`

Cron-style task backed by EventBridge Scheduler:

```yaml
apiVersion: andro-cd/v1
kind: ECSScheduledTask
metadata:
  name: nightly-report
spec:
  cluster: batch
  schedule:
    expression: cron(0 3 * * ? *)       # cron(...) or rate(...) — EventBridge syntax
    roleArn: arn:aws:iam::...:role/androcdSchedulerRole
    enabled: true
  network:
    subnets: [subnet-aaa]
    securityGroups: [sg-0ccc]
  taskDefinition:
    cpu: "512"
    memory: "1024"
    executionRoleArn: arn:aws:iam::...:role/ecsTaskExecutionRole
    containers:
      - name: report
        image: 123.dkr.ecr.us-east-1.amazonaws.com/reports:v2
        command: ["python", "-m", "reports.nightly"]
        logGroup: /ecs/nightly-report
```

The scheduler role needs `ecs:RunTask` on the task definition plus `iam:PassRole` for the
task's execution/task roles. See the AWS docs for the exact trust policy.

### `kind: ECSCluster`

Manage the ECS cluster itself as a GitOps application — insights, capacity providers,
default strategy, Service Connect namespace and tags all declared in Git:

```yaml
apiVersion: andro-cd/v1
kind: ECSCluster
metadata:
  name: production                     # cluster name (override with spec.cluster)
  labels: {team: platform}             # propagated as AWS tags
spec:
  region: eu-central-1
  wave: 0                              # create before wave-1 services target it
  containerInsights: enhanced          # disabled | enabled | enhanced
  capacityProviders: [FARGATE, FARGATE_SPOT]
  defaultCapacityProviderStrategy:     # default for services without their own strategy
    - provider: FARGATE_SPOT
      weight: 3
    - provider: FARGATE
      weight: 1
      base: 1
  serviceConnectNamespace: internal    # Cloud Map namespace (name or ARN)
  syncPolicy:
    prune: false                       # true = delete the cluster when removed from Git
```

- No `network` / `taskDefinition` — those apply to services, not clusters.
- Fields you omit are left untouched on the live cluster (no churn against console
  changes you don't manage from Git).
- Custom (ASG-backed) capacity providers must be listed in `capacityProviders`;
  `FARGATE`/`FARGATE_SPOT` referenced by the strategy are attached automatically.
- **Prune is safe**: deleting is refused while the cluster still has active services
  or running tasks — remove the workloads first.
- Health: **Healthy** when the cluster is `ACTIVE` (with service/task counts in the
  message). Combine with **waves**: cluster in wave 0, its services in wave 1+.

### `kind: ECSServiceSet` (app-of-apps)

Generate multiple applications from one template:

```yaml
apiVersion: andro-cd/v1
kind: ECSServiceSet
metadata:
  name: api-environments
spec:
  generators:
    - values: {env: dev,  count: 1, cpu: "256", memory: "512"}
    - values: {env: prod, count: 3, cpu: "512", memory: "1024"}
  template:
    apiVersion: andro-cd/v1
    kind: ECSService
    metadata:
      name: api-${env}
      labels: {env: "${env}"}
    spec:
      cluster: ${env}
      service:
        desiredCount: ${count}
        launchType: FARGATE
      network:
        subnets: [subnet-aaa]
        securityGroups: [sg-0ccc]
      taskDefinition:
        cpu: "${cpu}"
        memory: "${memory}"
        containers:
          - name: api
            image: my/api:v1
```

`${var}` placeholders are substituted verbatim (numbers become strings). Each generator
produces one application named after the rendered `metadata.name`.

### Values files (per-environment templating)

`values.yaml` / `values.yml` files in the manifest tree are **not** manifests — they
define `${key}` substitutions for every manifest in their directory subtree:

```
manifests/
├── values.yaml              # tag: stable, team: platform
├── web.yaml                 # image: repo/app:${tag}  → repo/app:stable
└── envs/
    └── prod/
        ├── values.yaml      # tag: v42
        └── web.yaml         # image: repo/app:${tag}  → repo/app:v42
```

- Files layer from the repo root down; the **closest file wins** on key conflicts.
- Nested mappings flatten to dotted keys: `image: {tag: v1}` → `${image.tag}`.
- Substitution happens before validation, so values can fill any field.

---

## Sync waves & hooks

### Waves

`spec.wave` (integer, default 0) orders deployments across your fleet:

- Wave `0` runs first. All wave-0 apps must be Synced + Healthy before any wave-1 app starts.
- Great for "database first, migrations next, apps last" patterns.
- When a lower wave stalls (Error, Degraded), higher waves stay put until you fix it.

### Hooks

`spec.hooks.preSync` and `spec.hooks.postSync` each run a **one-off ECS task** with the
current task definition and a command override — typically for migrations, cache warmup or
notifications.

```yaml
spec:
  hooks:
    preSync:
      command: ["python", "manage.py", "migrate", "--noinput"]
      container: web                    # defaults to the first container
      timeoutSeconds: 600
```

- Non-zero exit code **fails the sync** — the service update never happens.
- Timeout stops the task and marks the sync failed.
- Task starts with the same network configuration as the service.

---

## Autoscaling

Target-tracking Application Auto Scaling per app:

```yaml
spec:
  service:
    autoscaling:
      minCount: 2
      maxCount: 20
      targetCpu: 60                     # % — creates ECSServiceAverageCPUUtilization policy
      targetMemory: 75                  # % — creates ECSServiceAverageMemoryUtilization policy
```

Once autoscaling is configured, the autoscaler owns `desiredCount` — reconciler stops
fighting it. Removing the block deregisters the scalable target on the next sync.

Policy names are `androcd-<app-name>-cpu` and `androcd-<app-name>-memory` (so you can
inspect them in the Application Auto Scaling console).

---

## Load balancer

Two modes on `service.loadBalancer`:

**Reference mode** — attach the service to an existing target group:

```yaml
spec:
  service:
    loadBalancer:
      targetGroupArn: arn:aws:elasticloadbalancing:us-east-1:123:targetgroup/api/abc
      containerName: web                # optional; defaults to the first container
      containerPort: 8080
```

**Managed mode** — Andro-CD creates and reconciles the target group and a listener rule
on an existing ALB listener:

```yaml
spec:
  service:
    loadBalancer:
      containerPort: 8080
      create:
        listenerArn: arn:aws:elasticloadbalancing:...:listener/app/main/abc/def
        port: 8080                      # TG port; defaults to containerPort
        protocol: HTTP                  # towards the targets: HTTP | HTTPS
        rule:
          priority: 10                  # unique per listener; applied at creation
          hostHeader: api.example.com   # and/or pathPattern
          pathPattern: /api/*
        healthCheck:
          path: /health
          interval: 30
          timeout: 5
          healthyThreshold: 3
          unhealthyThreshold: 3
          matcher: "200-399"
```

- The target group is named `androcd-<app>` (ip target type, required for awsvpc); the
  VPC comes from `spec.network.vpc` or is derived from the first subnet.
- Health-check settings and rule conditions are diffed and reconciled; **Prune** deletes
  the rule and TG together with the service.
- The ALB and listener themselves stay your infrastructure — one ALB serves many apps,
  each with its own rule. Requires the `elasticloadbalancing:*` permissions from the
  IAM section below.

---

## Capacity providers (Fargate Spot)

Replace plain `launchType` with a weighted strategy for significant cost savings on
interruption-tolerant workloads:

```yaml
spec:
  service:
    desiredCount: 4
    capacityProviders:
      - provider: FARGATE_SPOT
        weight: 3
      - provider: FARGATE
        weight: 1
        base: 1
```

- `base` tasks always run on that provider; the rest split by `weight` (here: 3 Spot : 1
  on-demand).
- When Andro-CD creates the cluster itself, `FARGATE`/`FARGATE_SPOT` are associated
  automatically. Custom (ASG-backed) providers must already exist on the cluster.
- Changing the strategy triggers a new deployment. Switching an **existing** service
  between plain `launchType` and a strategy requires recreating the service (AWS
  restriction) — Andro-CD won't fight services that already use a strategy when the
  manifest doesn't define one.

---

## ECR digest pinning

For reliable drift detection on mutable tags (`app:latest`, `app:main`):

```yaml
spec:
  taskDefinition:
    resolveImages: true
    containers:
      - name: app
        image: 123.dkr.ecr.us-east-1.amazonaws.com/app:latest
```

At sync time, `app:latest` is looked up via `ecr:DescribeImages` and pinned to
`app@sha256:<digest>` before registering the task definition. Result: immutable deploys,
and pushing a new `latest` triggers an OutOfSync (because the digest changed).

Non-ECR images (Docker Hub, gcr.io, quay.io) pass through unchanged.

---

## AWS profiles

By default, Andro-CD uses the standard boto3 credentials chain — IAM task role in
production, `~/.aws/credentials` locally.

For multi-account setups, add named profiles in **AWS Profiles** and reference them in the
manifest:

```yaml
spec:
  awsProfile: prod-account
```

- Credentials are validated via `sts:GetCallerIdentity` before saving.
- Stored **encrypted (Fernet/AES)** in the database — the encryption key comes from
  `ENCRYPTION_KEY` (falls back to `SESSION_SECRET`). Both must be stable across restarts.
- Only admins can add/remove profiles; a profile can't be deleted while it's in use.
- `region` precedence: `spec.region` > profile's default region > `AWS_REGION` env.

---

## Operations

### Sync, rollback, prune

- **Sync** — force reconciliation of one app. Also *resumes* auto-sync after a rollback.
- **Rollback** — Task Definition tab shows the last 10 revisions. One click redeploys an
  older revision and *pauses* auto-sync (otherwise the next tick would revert you to Git).
  Manual **Sync** returns to Git state.
- **Prune** — deletes the ECS service from AWS. Two ways to trigger:
  1. `syncPolicy.prune: true` in the manifest → automatic when removed from Git.
  2. Manual button on any Orphaned app.
- **Refresh** — force a git pull + diff pass immediately without waiting for the poll.

### Dry-run mode

`DRY_RUN=true` turns the whole controller into an observer: every sync, rollback and
prune records **what it would do** (`[dry-run] …` entries in history and on the card)
but never calls an AWS mutation API. Great for demos, testing IAM policies, and
evaluating Andro-CD against a live account risk-free. The UI shows a persistent banner
while it's active.

### High availability (multiple replicas)

With Postgres, replicas elect a single **leader** via a session-scoped advisory lock:

- Only the leader applies changes and prunes.
- Standbys keep polling git and refreshing diffs read-only — their UI stays live and
  shows a "standby" banner; manual actions still work from any replica.
- When the leader dies, a standby takes over within one `SYNC_INTERVAL`.
- Role is visible in `/api/status` (`leader: true/false`) and the `androcd_leader` metric.

SQLite / no-DB deployments are single-instance and always leader.

### Repositories

Manage in the **Repositories** panel. What you can do:

- Add / remove repositories (admin only).
- See per-repo commit, poll time and errors.
- Multiple repos and multiple branch/path combos are supported.
- Removing a repo marks its apps Orphaned — AWS resources are kept.

### Webhooks (instant sync)

Set `WEBHOOK_SECRET` in the env, then point a GitHub webhook at
`https://your-host/api/webhook/github`:

- Content type: `application/json`
- Secret: same value as `WEBHOOK_SECRET`
- Events: **Just the push event**

Push events on any tracked branch trigger a reconcile immediately. HMAC-SHA256 verified.

---

## Authentication & RBAC

### GitHub OAuth

`AUTH_MODE=github` requires:

- `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` — from a GitHub OAuth App
  (Settings → Developer settings → OAuth Apps).
- Callback URL in the OAuth App: `<PUBLIC_URL>/api/auth/callback`.
- Optional access control:
  - `GITHUB_ALLOWED_USERS=alice,bob` — allowlist.
  - `GITHUB_ALLOWED_ORG=my-org` — only members of this GitHub org.

Sessions live in signed httpOnly cookies (`androcd_session`). `SESSION_SECRET` should be
set explicitly so sessions survive restarts.

### RBAC

Three roles:

| Role | Can do |
|---|---|
| **viewer** | Read everything (apps, resources, logs, history, diff, revisions) |
| **operator** | + Sync, Rollback, Prune, Refresh |
| **admin** | + Manage repositories, AWS profiles |

Configuration (case-insensitive GitHub logins):

```bash
RBAC_ADMINS=alice
RBAC_OPERATORS=bob,carol
RBAC_DEFAULT_ROLE=viewer         # role for anyone not in the above lists
```

If no RBAC variables are set at all, everyone is admin (backwards compatible with
single-user setups).

### API tokens (CI / automation)

Static bearer tokens let CI pipelines call the API without a browser login:

```bash
API_TOKENS=<token>:operator,<token2>:viewer     # generate: openssl rand -hex 32
curl -H "Authorization: Bearer <token>" https://androcd.example/api/apps
```

Each token maps to a role (viewer / operator / admin) and shows up in the audit log as
`api-token:<prefix>`.

### Security hardening (built in)

- Security headers on every response: CSP (`default-src 'self'`), `X-Frame-Options: DENY`,
  `nosniff`, `Referrer-Policy`; HSTS when `PUBLIC_URL` is https.
- CSRF protection: state-changing requests with a mismatched `Origin` are rejected.
- Rate limiting on the OAuth flow and the webhook; webhook bodies capped at 1 MiB.
- Git credentials flow through in-memory http headers — never written to `.git/config`.
- The container runs as a non-root user (uid 10001); compose sets `no-new-privileges`.

### Audit log

Every sync, rollback, prune, refresh, repo/profile change and login is persisted with
user, role, source IP and timestamp. Admins can browse it in the **Audit** panel or via
`GET /api/audit?limit=&user=&action=`.

---

## Observability

### Prometheus metrics

Expose `/metrics` (Prometheus text format; set `METRICS_TOKEN` to require a bearer token):

| Metric | Description |
|---|---|
| `androcd_sync_total{app, result}` | Sync count, `result="success"` or `error` |
| `androcd_sync_duration_seconds{app}` | Histogram of sync durations |
| `androcd_apps_sync_status{status}` | Apps per sync status |
| `androcd_apps_health{health}` | Apps per health status |
| `androcd_git_poll_errors_total` | Failed git polls |
| `androcd_git_unchanged_total` | Polls short-circuited (remote HEAD unchanged) |
| `androcd_last_poll_timestamp_seconds` | Unix ts of the last poll |
| `androcd_reconcile_pass_seconds` | Histogram of full reconcile pass durations |
| `androcd_leader` | 1 on the leader replica, 0 on standbys |

### Notifications

Set `SLACK_WEBHOOK_URL` to receive:

- 🚀 sync succeeded (with commit + actions performed)
- ✗ sync failed
- ⚠️ app transitioned into Degraded
- ↻ manual rollback triggered
- 🗑 app pruned

### Logs

**Logs** tab in each app streams CloudWatch events over Server-Sent Events. You can pause,
resume, clear, toggle follow mode and switch between containers.

Requires the container's `logGroup` in the manifest plus `logs:DescribeLogStreams` and
`logs:GetLogEvents` permissions.

Controller logs themselves can be emitted as structured JSON (`LOG_FORMAT=json`) for
CloudWatch / Loki ingestion.

---

## CLI

`backend/cli.py` — for manifest-repo CI and quick ops:

```bash
# Offline validation (no server needed):
python backend/cli.py validate ./manifests

# Against a running server (uses ANDROCD_SESSION cookie if auth is enabled):
python backend/cli.py --server https://androcd.example apps
python backend/cli.py --server https://androcd.example diff my-app
python backend/cli.py --server https://androcd.example sync my-app
```

Perfect for a manifest-repo GitHub Actions job that fails the build on invalid YAML
before it can ever reach the reconciler.

---

## HTTP API

All `/api/*` endpoints require authentication when `AUTH_MODE=github` — a session cookie
or an `API_TOKENS` bearer token (except the OAuth flow, the HMAC-verified webhook, the
public docs and `/api/schema`). `/healthz` and `/readyz` are always public; `/metrics`
is public unless `METRICS_TOKEN` is set.

| Method | Path | Role | Description |
|---|---|---|---|
| GET | `/api/status` | any | Tracked repos, last poll, app count |
| GET | `/api/apps` | any | Summary of all apps |
| GET | `/api/apps/{name}` | any | Detail including manifest + live state |
| GET | `/api/apps/{name}/diff` | any | Normalized desired vs live JSON |
| GET | `/api/apps/{name}/resources` | any | Live cluster/service/taskdef/tasks |
| GET | `/api/apps/{name}/history` | any | Persisted sync history |
| GET | `/api/apps/{name}/logs` | any | One-shot CloudWatch tail |
| GET | `/api/apps/{name}/logs/stream` | any | Server-Sent Events stream |
| GET | `/api/apps/{name}/revisions` | any | Recent task-def revisions |
| POST | `/api/apps/{name}/sync` | operator | Force reconcile |
| POST | `/api/apps/{name}/rollback` | operator | `{revision}` → redeploy, pauses auto-sync |
| POST | `/api/apps/{name}/prune` | operator | Delete Orphaned app's AWS service |
| POST | `/api/refresh` | operator | Git pull + diff pass immediately |
| GET | `/api/audit` | admin | Audit trail (`?limit=&user=&action=`) |
| GET | `/api/schema` | — | JSON Schema of the manifest format (public) |
| GET | `/api/repos` | any | List tracked repositories |
| POST | `/api/repos` | admin | Connect a repo |
| DELETE | `/api/repos/{id}` | admin | Disconnect a repo |
| GET | `/api/profiles` | any | List AWS profiles (keys masked) |
| POST | `/api/profiles` | admin | Add AWS profile (STS-validated, encrypted) |
| DELETE | `/api/profiles/{name}` | admin | Remove AWS profile |
| POST | `/api/webhook/github` | (HMAC) | GitHub push webhook — instant reconcile |
| GET | `/api/auth/me` | — | Current session + role |
| POST | `/api/auth/logout` | — | Clear session cookie |
| GET | `/healthz` | — | Liveness (process is up) |
| GET | `/readyz` | — | Readiness — 503 with reasons when DB is down / git stalled |
| GET | `/metrics` | — | Prometheus metrics (optional `METRICS_TOKEN` bearer) |
| GET | `/api/docs/index.md` | — | This document as raw Markdown |

---

## Configuration reference

All environment variables. Set in `.env` (docker-compose loads it automatically).

### Core

| Variable | Default | Description |
|---|---|---|
| `GIT_REPO_URL` | — | Optional bootstrap repo (repos are usually managed via the UI) |
| `GIT_BRANCH` | `main` | Bootstrap repo branch |
| `GIT_PATH` | *(empty)* | Bootstrap repo subdirectory |
| `GIT_TOKEN` | — | Bootstrap repo token (HTTPS only) |
| `SYNC_INTERVAL` | `60` | Seconds between reconcile passes |
| `AUTO_SYNC` | `true` | Global default for apps without `syncPolicy.autoSync` |
| `DRY_RUN` | `false` | Record plans only — never call AWS mutation APIs |
| `RECONCILE_WORKERS` | `8` | Parallel diff workers per reconcile pass |
| `KEEP_TASKDEF_REVISIONS` | `0` | Deregister ACTIVE task-def revisions beyond the newest N (0 = keep all) |
| `AWS_REGION` | — | Default region when neither manifest nor profile specifies |
| `PORT` | `8080` | HTTP port |
| `DATABASE_URL` | sqlite in `/data` | Persistence (Postgres in docker-compose) |
| `LOG_FORMAT` | `text` | `json` = structured logs |

### Authentication

| Variable | Default | Description |
|---|---|---|
| `AUTH_MODE` | `none` | `github` enables OAuth login |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | — | GitHub OAuth App credentials |
| `GITHUB_ALLOWED_USERS` | — | Comma-separated username allowlist |
| `GITHUB_ALLOWED_ORG` | — | Only members of this org may log in |
| `SESSION_SECRET` | random | Signs session cookies (set to keep sessions across restarts) |
| `PUBLIC_URL` | `http://localhost:8080` | External URL — OAuth callback, CSRF checks, Secure cookies/HSTS |
| `RBAC_ADMINS` | — | GitHub logins with admin role |
| `RBAC_OPERATORS` | — | GitHub logins with operator role |
| `RBAC_DEFAULT_ROLE` | see note | Fallback role; without RBAC vars everyone is admin |
| `API_TOKENS` | — | Bearer tokens for CI: `token:role[,token2:role2]` |

### Integrations

| Variable | Default | Description |
|---|---|---|
| `WEBHOOK_SECRET` | — | Enables `/api/webhook/github` |
| `SLACK_WEBHOOK_URL` | — | Slack notifications |
| `ENCRYPTION_KEY` | falls back to `SESSION_SECRET` | Encrypts stored AWS profile credentials |
| `METRICS_TOKEN` | — | Bearer token protecting `GET /metrics` (unset = public) |

---

## IAM permissions

Minimum policy for the IAM role running Andro-CD:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ecs:CreateCluster",
        "ecs:UpdateCluster",
        "ecs:DeleteCluster",
        "ecs:PutClusterCapacityProviders",
        "ecs:DescribeClusters",
        "ecs:RegisterTaskDefinition",
        "ecs:DeregisterTaskDefinition",
        "ecs:DescribeTaskDefinition",
        "ecs:ListTaskDefinitions",
        "ecs:TagResource",
        "ecs:CreateService",
        "ecs:UpdateService",
        "ecs:DeleteService",
        "ecs:DescribeServices",
        "ecs:RunTask",
        "ecs:StopTask",
        "ecs:ListTasks",
        "ecs:DescribeTasks",
        "iam:PassRole",
        "logs:CreateLogGroup",
        "logs:DescribeLogStreams",
        "logs:GetLogEvents",
        "logs:FilterLogEvents",
        "ecr:DescribeImages",
        "ec2:DescribeSubnets",
        "elasticloadbalancing:DescribeTargetGroups",
        "elasticloadbalancing:CreateTargetGroup",
        "elasticloadbalancing:ModifyTargetGroup",
        "elasticloadbalancing:DeleteTargetGroup",
        "elasticloadbalancing:DescribeRules",
        "elasticloadbalancing:CreateRule",
        "elasticloadbalancing:ModifyRule",
        "elasticloadbalancing:DeleteRule",
        "elasticloadbalancing:AddTags",
        "application-autoscaling:*",
        "scheduler:CreateSchedule",
        "scheduler:UpdateSchedule",
        "scheduler:DeleteSchedule",
        "scheduler:GetSchedule",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    }
  ]
}
```

Trim it down for your use case — e.g. drop `scheduler:*` if you don't use
`ECSScheduledTask`, drop `application-autoscaling:*` if you don't use autoscaling, drop
`ecs:DeleteService` if you never prune, drop `ecs:DeregisterTaskDefinition` if you don't
use `KEEP_TASKDEF_REVISIONS`, drop `ecs:TagResource` if you don't use `metadata.labels`,
drop `ecs:UpdateCluster`/`ecs:DeleteCluster`/`ecs:PutClusterCapacityProviders` if you
don't use `ECSCluster`.

Tip: run with `DRY_RUN=true` first — the recorded plans tell you exactly which calls the
controller would make, so you can verify the policy before granting write access.

---

## Troubleshooting

**"The security token included in the request is invalid"**
Your AWS credentials expired or are wrong. Refresh and restart the container.

**Diff always shows "OutOfSync" after adding `resolveImages: true`**
That's the point — mutable tags now resolve to digests, which live task definitions don't
have yet. Sync once, then diffs will be quiet until the tag actually changes upstream.

**"cannot decrypt stored secret — was ENCRYPTION_KEY/SESSION_SECRET changed?"**
The Fernet key derived from your secret has changed, so AWS profiles saved earlier can't
be read. Either restore the old value, or delete + re-add the profiles.

**Auto-sync doesn't run after a rollback**
By design — after a rollback the app is paused so the reconciler doesn't undo it. Click
**Sync** to return to the Git state and resume auto-sync.

**Running two replicas — which one applies?**
With Postgres, replicas elect a leader automatically (advisory lock); standbys show a
banner and only refresh diffs. If both replicas apply, check they share the same
`DATABASE_URL` — leader election needs a common Postgres.

**Everything shows `[dry-run]` and nothing deploys**
`DRY_RUN=true` is set. Unset it and restart; then click **Sync** (or push a commit) —
dry-run advances the recorded commit, so the next auto-sync waits for a new change.

---

## Where to look next

- [SPEC.md](/api/docs/spec.md) — condensed technical spec.
- [IMPROVEMENTS.md](/api/docs/improvements.md) — roadmap and non-goals.
- [examples/](https://github.com/cyberlabrs/andro-cd/tree/main/examples) — sample
  manifests including `advanced.yaml` (ServiceSet + scheduled task + autoscaling + hooks).

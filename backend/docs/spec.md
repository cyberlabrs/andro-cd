# Andro-CD — Pull-based GitOps for AWS ECS

A single Docker image that works like ArgoCD, but targets AWS ECS instead of Kubernetes.
Backend: Python (FastAPI + boto3). Frontend: React (Vite). Git sync via `git` CLI.

## Core idea

1. The service **polls one or more Git repositories** (like Argo's repo-server) on an interval.
   Repositories are managed at runtime via the UI ("Repositories") or the `/api/repos` API and
   persisted in the database; `GIT_REPO_URL` env is just an optional bootstrap repo.
2. It reads **YAML manifests** (one or more documents per file) from a configurable path in each repo.
3. For each manifest it **reconciles** desired state against live AWS state:
   - If the ECS **cluster** doesn't exist → create it.
   - If the **task definition** differs from the latest active revision → register a new revision.
   - If the **service** doesn't exist → create it; if it drifted (count, task def, network) → update it.
4. AWS credentials come from the **default boto3 chain** (IAM role on ECS/EC2, env vars, or `~/.aws` locally).
5. A React UI shows applications, sync status, health, diffs, and allows manual sync/refresh.

## Manifest format

```yaml
apiVersion: andro-cd/v1
kind: ECSService
metadata:
  name: web-app                # unique app name (also default service & family name)
  labels:                      # optional, shown as chips in the UI + searchable/filterable
    team: platform
    env: production
spec:
  region: eu-central-1         # optional; precedence: manifest > profile default > AWS_REGION env
  awsProfile: prod-account     # optional named AWS profile (managed in UI, stored encrypted);
                               # omit to use the default credentials chain (IAM role / env)
  cluster: production          # cluster name; created if missing
  service:
    desiredCount: 2
    launchType: FARGATE        # FARGATE | EC2
    assignPublicIp: true
    circuitBreaker: true       # ECS deployment circuit breaker (default true)
    rollbackOnFailure: true    # auto-rollback on failed rollout (default true)
    minimumHealthyPercent: 100 # optional
    maximumPercent: 200        # optional
  network:
    vpc: vpc-0abc123           # informational (docs/UI)
    subnets: [subnet-aaa, subnet-bbb]
    securityGroups: [sg-0123]
  syncPolicy:                  # optional, Argo-style
    autoSync: true             # override global AUTO_SYNC for this app
    selfHeal: false            # also revert manual drift in AWS (default: only git changes trigger sync)
    prune: false               # delete the ECS service when the manifest is removed from git
    syncWindows:               # UTC windows when auto-sync is allowed (empty = always);
      - days: [Mon, Tue, Wed, Thu, Fri]   # manual sync always works
        start: "07:00"
        end: "19:00"
  taskDefinition:
    family: web-app            # optional, defaults to metadata.name
    cpu: "256"
    memory: "512"
    executionRoleArn: arn:aws:iam::123:role/ecsTaskExecutionRole   # optional
    taskRoleArn: arn:aws:iam::123:role/appRole                     # optional
    containers:
      - name: web
        image: nginx:1.27
        portMappings: [80]     # ints or {containerPort, protocol}
        environment:           # map or list of {name, value}
          APP_ENV: prod
          LOG_LEVEL: info
        secrets:               # map name -> SSM/SecretsManager ARN
          DB_PASSWORD: arn:aws:ssm:eu-central-1:123:parameter/db-pass
        command: []            # optional
        cpu: 0                 # optional container-level
        memory: 512            # optional hard limit
        logGroup: /ecs/web-app # optional -> awslogs driver (group auto-created)
```

- Multiple YAML documents per file are allowed (`---` separated).
- Files are discovered recursively under `GIT_PATH` (`*.yml` / `*.yaml`).

### Other kinds & advanced fields (see `examples/advanced.yaml`)

- **`ECSServiceSet`** (app-of-apps): `spec.generators[].values` + `spec.template` with
  `${var}` substitution → expands to N `ECSService` apps.
- **`ECSScheduledTask`**: `spec.schedule.{expression, roleArn, enabled}` — cron/rate via
  EventBridge Scheduler running the task definition.
- **`spec.wave`** (int, default 0): sync waves — a wave deploys only after all lower waves
  are Synced + Healthy.
- **`spec.hooks.preSync/postSync`**: `{command, container?, timeoutSeconds}` — one-off ECS task
  (e.g. DB migrations); a non-zero exit aborts the sync.
- **`taskDefinition.resolveImages: true`**: mutable ECR tags are pinned to immutable digests
  at sync time (reliable drift detection + immutable deploys).
- **`service.autoscaling`**: `{minCount, maxCount, targetCpu?, targetMemory?}` — target-tracking
  Application Auto Scaling; the autoscaler then owns `desiredCount`.
- **`service.loadBalancer`**: `{targetGroupArn, containerName?, containerPort}` — attach the
  service to an existing target group.
- **`service.capacityProviders`**: `[{provider, weight?, base?}]` — weighted capacity
  provider strategy (e.g. `FARGATE_SPOT` 3 : `FARGATE` 1 with base 1) instead of plain
  `launchType`; Fargate providers are associated automatically on cluster creation.
- **`containers[].healthCheck`**: `{command, interval?, timeout?, retries?, startPeriod?}` —
  container-level health check (docker semantics), diffed like every other field.
- **`metadata.labels`** propagate to AWS tags on the cluster, task definition and service
  (with `propagateTags: SERVICE`) — cost allocation per team/app for free.

### Values files (templating)

`values.yaml` / `values.yml` files in the manifest tree are not manifests — they define
`${key}` substitutions for all manifests in their directory subtree. Files layer from the
root down and the **closest file wins**, so `envs/prod/values.yaml` overrides the repo-root
`values.yaml` for manifests under `envs/prod/`. Nested mappings flatten to dotted keys
(`image: {tag: v1}` → `${image.tag}`).

## Reconciliation

Runs in a background loop every `SYNC_INTERVAL` seconds:

1. `git clone/fetch+reset` the repo (shallow), record HEAD commit.
2. Parse + validate manifests (pydantic). Invalid manifests → app in `Error` state, others unaffected.
3. **Diff** per app (read-only): cluster exists? task definition equal (image, env, secrets, ports, cpu/mem, roles, command, logging)? service exists / desiredCount / network / taskdef revision?
4. If `AUTO_SYNC=true` (default) apply changes; otherwise apps stay `OutOfSync` until manual sync from the UI/API.
5. Health from ECS: `runningCount == desiredCount` and rollout `COMPLETED` → **Healthy**; `IN_PROGRESS` → **Progressing**; otherwise **Degraded**.

Sync statuses: `Synced`, `OutOfSync`, `Syncing`, `Error`, `Unknown`.
Apps removed from Git are marked `Orphaned` (no automatic deletion in v1 — safe by default).

## HTTP API

| Method | Path                    | Description                              |
|--------|-------------------------|------------------------------------------|
| GET    | `/api/status`           | tracked repos, last poll, app count       |
| GET    | `/api/repos`            | list tracked repositories                 |
| POST   | `/api/repos`            | connect a repo; auth: `https` (token), `ssh` (private key) or `github_app` (App ID + Installation ID + PEM key) |
| DELETE | `/api/repos/{id}`       | remove a repo (apps become Orphaned)      |
| GET    | `/api/profiles`         | list AWS profiles (keys masked)           |
| POST   | `/api/profiles`         | add profile `{name, region, accessKeyId, secretAccessKey}` — STS-validated, stored encrypted (Fernet via `ENCRYPTION_KEY`/`SESSION_SECRET`) |
| DELETE | `/api/profiles/{name}`  | remove a profile (refused while in use)   |
| GET    | `/api/apps`             | all apps with sync/health status          |
| GET    | `/api/apps/{name}`      | manifest, live state, diff details        |
| GET    | `/api/apps/{name}/diff` | normalized desired vs live JSON (side-by-side diff view) |
| GET    | `/api/apps/{name}/history` | persisted sync history (from DB)       |
| GET    | `/api/apps/{name}/logs` | tail CloudWatch logs (`?container=&lines=`) |
| GET    | `/api/apps/{name}/logs/stream` | real-time log streaming (Server-Sent Events) |
| GET    | `/api/apps/{name}/resources` | live resources: cluster, service config, active task definition, running tasks |
| POST   | `/api/apps/{name}/sync` | force reconcile of one app (resumes auto-sync after rollback) |
| GET    | `/api/apps/{name}/revisions` | recent task definition revisions      |
| POST   | `/api/apps/{name}/rollback` | `{revision}` — redeploy an old revision, pauses auto-sync |
| POST   | `/api/apps/{name}/prune` | delete the ECS service of an Orphaned app |
| POST   | `/api/refresh`          | git pull + recompute all diffs now        |
| GET    | `/api/audit`            | audit trail: who synced/rolled back/pruned what (`?limit=&user=&action=`, admin) |
| GET    | `/api/schema`           | JSON Schema of the manifest format (public, for manifest-repo CI) |
| POST   | `/api/webhook/github`   | GitHub push webhook (HMAC, instant sync, rate-limited) |
| GET    | `/api/auth/login` → GitHub → `/api/auth/callback` | OAuth login flow |
| GET    | `/api/auth/me`, POST `/api/auth/logout` | session info / logout       |
| GET    | `/metrics`              | Prometheus metrics (optional `METRICS_TOKEN` bearer) |
| GET    | `/healthz`              | liveness                                  |
| GET    | `/readyz`               | readiness — 503 when DB is down, git polling stalled or repos failing |

Frontend is served from `/` (static build baked into the image).

## Configuration (env vars)

| Var             | Default | Description                          |
|-----------------|---------|--------------------------------------|
| `GIT_REPO_URL`  | —       | optional bootstrap repo (repos are managed via UI/API and stored in DB) |
| `GIT_BRANCH`    | `main`  | branch of the bootstrap repo         |
| `GIT_PATH`      | ``      | subdirectory of the bootstrap repo   |
| `GIT_TOKEN`     | —       | token for the bootstrap repo (https) |
| `SYNC_INTERVAL` | `60`    | seconds between reconcile loops      |
| `AUTO_SYNC`     | `true`  | apply automatically vs manual sync   |
| `DRY_RUN`       | `false` | record plans, never call AWS mutation APIs (demos, IAM testing) |
| `KEEP_TASKDEF_REVISIONS` | `0` | deregister ACTIVE task-def revisions beyond the newest N (0 = keep all; in-use revision is never touched) |
| `LOG_FORMAT`    | `text`  | `json` = structured logs for CloudWatch/Loki |
| `AWS_REGION`    | —       | default region                       |
| `PORT`          | `8080`  | HTTP port                            |
| `DATABASE_URL`  | sqlite in /tmp | persistence (Postgres in compose); sync history + app state survive restarts |
| `WEBHOOK_SECRET` | —      | enables `/api/webhook/github` (GitHub webhook secret) |
| `SLACK_WEBHOOK_URL` | —   | Slack notifications: sync done/failed, app degraded |
| `AUTH_MODE`     | `none`  | `github` = require GitHub OAuth login for UI/API |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | — | GitHub OAuth app credentials |
| `GITHUB_ALLOWED_ORG` | —  | only members of this GitHub org may log in |
| `GITHUB_ALLOWED_USERS` | — | comma-separated GitHub username allowlist |
| `SESSION_SECRET` | random | signs session cookies; set it to keep sessions across restarts |
| `RBAC_ADMINS`   | —       | GitHub logins with admin role (manage repos + everything) |
| `RBAC_OPERATORS` | —      | GitHub logins with operator role (sync/rollback/prune) |
| `RBAC_DEFAULT_ROLE` | see note | role for other users; default: `admin` when no RBAC vars set (backwards compatible), otherwise `viewer` |
| `ENCRYPTION_KEY` | falls back to `SESSION_SECRET` | encrypts stored AWS profile credentials — must be stable across restarts |
| `API_TOKENS`    | —       | static bearer tokens for CI/automation: `token:role[,token2:role2]` (role: viewer/operator/admin) |
| `METRICS_TOKEN` | —       | bearer token protecting `GET /metrics` (unset = public) |
| `RECONCILE_WORKERS` | `8` | parallel diff workers per reconcile pass |
| `PUBLIC_URL`    | `http://localhost:8080` | external URL, used for the OAuth callback, CSRF checks and Secure cookies/HSTS |

## Security hardening (built in)

- Security headers on every response: CSP (`default-src 'self'`), `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, HSTS when `PUBLIC_URL` is https.
- CSRF protection: state-changing `/api` requests with a mismatched `Origin` are rejected
  (webhook excluded — it's HMAC-verified).
- Rate limiting on the OAuth login flow and the webhook endpoint; webhook payloads capped at 1 MiB.
- Git credentials (https tokens, GitHub App tokens) are passed via in-memory http headers —
  never written to `.git/config` on disk; SSH keys are chmod 600 and deleted with the repo.
- Audit log: syncs, rollbacks, prunes, repo/profile changes and logins are persisted with
  user, role, source IP and timestamp (`/api/audit`, Audit panel in the UI).
- The container runs as a non-root user (uid 10001) with a read-only `~/.aws` mount;
  compose sets `no-new-privileges` and does not expose Postgres on the host.

## HA / multiple replicas

With Postgres, replicas elect a single **leader** via a session-scoped advisory lock
(`pg_try_advisory_lock`). Only the leader applies changes and prunes; standbys keep
polling git and refreshing diffs read-only so their UI stays live, and take over
automatically (within one `SYNC_INTERVAL`) when the leader dies. The `/api/status`
response and the `androcd_leader` metric expose the role; the UI shows a standby banner.
SQLite / no-DB deployments are single-instance and always leader.

## Required IAM permissions

`ecs:CreateCluster`, `ecs:DescribeClusters`, `ecs:RegisterTaskDefinition`,
`ecs:DescribeTaskDefinition`, `ecs:CreateService`, `ecs:UpdateService`,
`ecs:DescribeServices`, `iam:PassRole` (for task/execution roles),
`logs:CreateLogGroup` (when `logGroup` is used),
`logs:DescribeLogStreams` + `logs:GetLogEvents` (logs tail in the UI).

## CLI

`backend/cli.py`: `androcd validate ./manifests` (offline, for the manifest repo's CI),
`androcd apps|diff NAME|sync NAME --server http://...` (uses `ANDROCD_SESSION` cookie when
auth is enabled).

## Project layout

```
backend/app/       FastAPI app: config, models, git_sync, reconciler, engine, api
frontend/          React + Vite + TypeScript UI
examples/          sample manifests
Dockerfile         multi-stage: node build -> python runtime
docker-compose.yml local run
```

## Out of scope for v1 (roadmap)

See [IMPROVEMENTS.md](IMPROVEMENTS.md) for the full list of recommended improvements
(prune, webhooks, rollback, ALB support, autoscaling, RBAC/SSO, metrics, notifications…).

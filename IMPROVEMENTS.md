# Andro-CD — Improvement Recommendations

Everything that could make this a production-grade "ArgoCD for ECS", grouped by area
and roughly ordered by value/effort inside each group.

## 1. GitOps core

- **Prune / garbage collection** *(done — per-app opt-in via `syncPolicy.prune: true`; deletes the
  ECS service when the manifest is removed from git, or manually via the Prune button on Orphaned
  apps; cluster and task definitions are kept)*. Still to do: task-def deregistration cascade.
- **Webhooks instead of polling** *(done — `POST /api/webhook/github`, HMAC-verified via
  `WEBHOOK_SECRET`, push events on the tracked branch trigger instant reconciliation; polling remains as fallback)*.
- **Rollback** *(done — Revisions table in the Task Definition tab with one-click rollback;
  pauses auto-sync for the app until the next manual Sync; recorded in history)*.
- **Sync windows** *(done — `syncPolicy.syncWindows: [{days, start, end}]`, UTC; auto-sync
  only inside a window (deploy freeze outside), manual sync always allowed)*.
- **Multi-repo / multi-source** *(done — repos are added/removed at runtime via the UI
  "Repositories" panel or `/api/repos`, persisted in the DB; several branch/path combos supported;
  per-repo auth: HTTPS token, SSH private key, or GitHub App installation tokens with hourly
  auto-refresh)*. Still to do: ApplicationSet-style generators.
- **Templating** *(values files done — `values.yaml` per directory subtree with `${key}`
  substitution, closest file wins, nested keys flatten to `${image.tag}`; per-env overlays via
  `envs/prod/values.yaml`)*. Still to do: Jsonnet/CUE support.
- **Kustomize-style overlays** — base manifest + environment patches, closer to real Argo UX.
- **App-of-apps** *(done — `ECSServiceSet` kind: generators + `${var}` template expand into N apps)*.
- **Selective/partial sync** — sync just the task definition or just desiredCount from the UI.
- **Diff against a specific commit / branch preview** — "what would change if I merged this PR".
- **Drift detection outside Git changes** *(done — `syncPolicy.selfHeal: true` reverts manual AWS
  drift; by default only git changes trigger auto-sync and drift is just surfaced as OutOfSync)*.
- **Orphan adoption** — "import" an existing ECS service into a generated manifest (reverse sync),
  great for onboarding existing infrastructure.

## 2. AWS / ECS coverage

- **Cluster as a first-class kind** *(done — `kind: ECSCluster`: containerInsights
  (incl. enhanced), attached capacity providers, default capacity provider strategy,
  Service Connect namespace, labels→tags; safe prune that refuses while the cluster has
  workloads; pairs with waves — cluster wave 0, services wave 1)*.
- **Load balancers** *(target group reference done — `service.loadBalancer: {targetGroupArn, containerName?, containerPort}` attaches the service to an existing TG)*. Still to do: creating ALB/listener/TG from the manifest.
- **Service auto scaling** *(done — `service.autoscaling` min/max + CPU/memory target tracking; autoscaler owns desiredCount)*. Still to do: ALB request-count metric.
- **Capacity providers** *(done — `service.capacityProviders: [{provider, weight, base}]`
  weighted strategy, e.g. FARGATE_SPOT 3:1 with on-demand base; Fargate providers associated
  automatically on cluster creation)*.
- **Blue/green & canary deployments** — CodeDeploy integration *(circuit breaker with automatic
  rollback + `minimumHealthyPercent`/`maximumPercent` is done — on by default via
  `service.circuitBreaker` / `service.rollbackOnFailure` in the manifest)*.
- **Scheduled tasks (cron)** *(done — `ECSScheduledTask` kind via EventBridge Scheduler: expression, roleArn, enabled toggle)*.
- **One-off tasks / jobs** — `ECSTask` kind for migrations and batch jobs, with "run now" in UI.
- **Service Connect / Cloud Map** — service discovery config in the manifest.
- **Volumes** — EFS volumes and bind mounts in the task definition.
- **Sidecars & FireLens** — first-class log-router sidecar config for shipping to Datadog/ES.
- **Container health checks** *(done — `containers[].healthCheck` with command/interval/
  timeout/retries/startPeriod, normalized against ECS defaults so diffs stay stable)*.
- **EC2 launch type polish** — placement constraints/strategies, host networking modes.
- **Tags & cost allocation** *(done — `metadata.labels` propagate to AWS tags on the cluster,
  task definition and service (+`propagateTags: SERVICE`) at creation/registration time)*.
- **ECR helpers** *(done — `taskDefinition.resolveImages: true` pins ECR tags to digests at sync time; missing images produce a clear error)*.
- **Multi-account** *(done via named AWS profiles — added in the UI, STS-validated, stored
  encrypted; `spec.awsProfile` in the manifest selects one, default chain otherwise)*.
  Still to do: `roleArn` assume-role profiles instead of raw keys.
- **Task definition cleanup** *(done — opt-in via `KEEP_TASKDEF_REVISIONS=N`: after each sync,
  ACTIVE revisions beyond the newest N are deregistered; the in-use revision is never touched)*.

## 3. Security

- **AuthN for the UI/API** *(GitHub OAuth done — `AUTH_MODE=github` + OAuth app credentials,
  org/user allowlists, signed httpOnly session cookies; all `/api` routes protected except the
  OAuth flow and the HMAC-verified webhook. API tokens for CI done — `API_TOKENS=token:role`
  accepted as `Authorization: Bearer`)*. Still to do: generic OIDC (Google/Okta/Dex),
  refresh of expired sessions.
- **RBAC** *(done — viewer/operator/admin roles via `RBAC_ADMINS`/`RBAC_OPERATORS`/
  `RBAC_DEFAULT_ROLE`; enforced on API endpoints and reflected in the UI)*. Still to do:
  GitHub team-based mapping, per-project roles.
- **Audit log** *(done — syncs, rollbacks, prunes, refreshes, repo/profile changes and logins
  persisted with user, role, source IP and timestamp; `GET /api/audit` + Audit panel in the UI)*.
- **Web hardening** *(done — security headers incl. CSP and X-Frame-Options on every response,
  HSTS on https, CSRF Origin checks on state-changing requests, rate limiting on OAuth flow and
  webhook, 1 MiB webhook body cap, SPA static file serving contained to the static root,
  non-root container user + `no-new-privileges` in compose)*.
- **Read-only / observation mode** *(done — `DRY_RUN=true` records what every sync/rollback/
  prune WOULD do without calling any AWS mutation API; banner in the UI, `DryRun` entries in
  history)*.
- **Webhook signature verification** *(done — HMAC via `WEBHOOK_SECRET`; see 1)*.
- **Secrets hygiene** *(git credentials done — https/GitHub App tokens flow through in-memory
  http headers and are never written to `.git/config`)*. Still to do: validate SSM/SecretsManager
  ARNs exist at diff time and surface a clear error instead of a failed deployment.
- **Least-privilege IAM docs** — ship a ready-made IAM policy JSON + Terraform/CDK snippet.
- **Manifest schema validation in CI** *(done — `GET /api/schema` serves the manifest JSON
  Schema publicly; combine with `androcd validate` in the manifest repo's CI)*.

## 4. Observability & notifications

- **Prometheus metrics** *(done — `/metrics`: sync count/duration per app, apps by sync/health
  status, git poll errors, last poll timestamp; Grafana dashboard JSON still to do)*.
- **Notifications** *(Slack done via `SLACK_WEBHOOK_URL` — sync succeeded/failed, app degraded;
  Discord/email/templating still to do)*.
- **Deployment history & timeline UI** — per-app timeline of deployments with commit, image tags,
  duration, outcome (DB now stores the raw data for this).
- **CloudWatch logs in UI** *(done — real-time streaming via SSE (`/logs/stream`) with follow
  mode, pause/resume, container selector; plus one-shot tail endpoint)*.
- **Task-level view** *(done — Tasks tab: running tasks with status, health, IP, AZ, started
  time, stopped reason; plus Task Definition tab with full active revision, env vars, ports,
  secrets; stopped-task forensics with per-container exit codes and reasons)*.
- **Structured JSON logging** *(done — `LOG_FORMAT=json` for CloudWatch/Loki ingestion)*.
  Still to do: request IDs, OpenTelemetry traces for the reconcile loop.
- **Event stream** — SSE/WebSocket to the UI instead of 5s polling; instant status updates.

## 5. Reliability & operations

- **Database persistence** *(done — Postgres in docker-compose, sync history + app state survive restarts)*.
- **HA / multiple replicas** *(done — Postgres session-scoped advisory lock elects one leader;
  standbys refresh diffs read-only (live UI, standby banner) and take over within one
  `SYNC_INTERVAL` when the leader dies; `androcd_leader` metric + `/api/status.leader`)*.
- **Backoff & retry** — exponential backoff per app on repeated sync failures instead of retrying
  every loop; circuit-break an app after N failures with manual reset.
- **AWS rate limit handling** — botocore adaptive retry mode, jitter between apps, batch describes
  (describe_services accepts 10 services per call — currently 1).
- **Parallel reconciliation** — thread pool over apps instead of sequential loop; big repos scale.
- **Graceful shutdown** — finish in-flight sync before exiting on SIGTERM.
- **Dry-run mode** *(done — see `DRY_RUN` under Security → read-only mode)*.
- **State export/import** — backup/restore of history DB; scheduled pg_dump sidecar or S3 export.
- **Health endpoint depth** *(done — `/healthz` stays pure liveness; `/readyz` returns 503
  with reasons when the DB is unreachable, git polling has stalled, or repos are failing)*.

## 6. UI / UX

- **Side-by-side diff view** — live vs desired YAML/JSON with syntax highlighting, like Argo's
  diff tab (currently changes are text bullets).
- **Search, filters, sorting** *(done — search over name/cluster/images/labels, status filter
  chips, sort by name/status/health/recency, all persisted in the URL — filters and the open
  app survive reload and are shareable)*.
- **Projects / grouping** — group apps by team or environment with collapsible sections.
- **Resource tree visualization** — cluster → service → deployments → tasks graph like Argo.
- **YAML rendering of manifests** — render the manifest as YAML (it's the source format), not JSON.
- **Sync progress feedback** — live rollout progress bar (running/pending/desired counts animate).
- **Confirmation dialogs** — for sync of apps with destructive-looking diffs; "sync all" button
  with a review step.
- **Keyboard shortcuts & theming** *(done — `/` focuses search, Esc closes panels; light/dark
  theme toggle persisted per browser, defaults to the OS preference)*. Still to do:
  mobile-friendly layout polish.
- **Login page + user menu** *(done)*.
- **i18n** — English/Serbian localization.

## 7. Developer experience & quality

- **Test suite** *(pytest unit tests for diff/normalization, manifest models, auth/RBAC and
  API-level security — headers, CSRF, tokens, path traversal — in `backend/tests/`)*;
  still to do: botocore stubs (`moto`/`Stubber`) for apply paths, frontend component tests
  with Vitest.
- **CI pipeline** — GitHub Actions: lint (ruff, eslint), type-check (mypy, tsc), tests,
  docker build, image push to ECR/GHCR with release tags.
- **CLI** — `androcd validate ./manifests`, `androcd diff`, `androcd sync <app>` hitting the API;
  useful in CI of the manifest repo.
- **Helm chart / Terraform module / CDK construct** — for deploying Andro-CD itself into ECS
  (self-hosting: Andro-CD manages Andro-CD).
- **Versioned manifest schema** — `apiVersion: andro-cd/v1alpha2` migration story, JSON Schema
  published per version, deprecation warnings in UI.
- **Config validation at startup** *(done — bad env combinations (auth without OAuth creds,
  missing SESSION_SECRET, weak API tokens, aggressive SYNC_INTERVAL…) are logged as clear
  warnings at boot)*.
- **Devcontainer + Makefile** — one-command local setup (`make dev` runs backend, frontend, db).
- **Structured error taxonomy** — distinguish user errors (bad manifest) from system errors
  (IAM denied) from transient errors (throttling) in both API responses and UI badges.

## Suggested next 5 (best value / effort)

Latest batch done: HA leader election, values-file templating, sync windows, dry-run mode,
capacity providers (FARGATE_SPOT), container health checks, tags/cost allocation, task
definition cleanup, JSON logging — on top of the security batch (audit log, API tokens,
CSP/CSRF/rate limits, non-root container, JSON Schema, readiness endpoint). Next up:

1. **ALB creation** — create listener/rule/TG from the manifest, not just reference.
2. **Generic OIDC** — Google/Okta/Dex login next to GitHub OAuth.
3. **Deployment timeline UI** — per-app timeline with commit, images, duration, outcome
   (data already persisted in sync history).
4. **One-off tasks / jobs** — `ECSTask` kind for migrations and batch jobs, "run now" in UI.
5. **EFS volumes & FireLens sidecars** — round out task definition coverage.

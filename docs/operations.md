# Operations

## Sync, rollback, prune

- **Sync** — force reconciliation of one app; also *resumes* auto-sync after a rollback.
- **Rollback** — the Task Definition tab lists recent revisions; one click redeploys an
  older one and *pauses* auto-sync (so the next tick doesn't revert you to Git).
  Manual **Sync** returns to the Git state.
- **Prune** — deletes the AWS resource. Automatic with `syncPolicy.prune: true` when the
  manifest is removed from Git, or manual on any Orphaned app. For `ECSCluster`, prune
  refuses while the cluster still has workloads.
- **Refresh** — git pull + diff pass immediately, without waiting for the poll.

## Sync waves

`spec.wave` (integer, default 0) orders deployments across the fleet:

- All wave-0 apps must be **Synced + Healthy** before any wave-1 app starts.
- Perfect for "cluster first, database next, apps last" patterns.
- A stalled lower wave (Error/Degraded) holds the higher waves until fixed.

## Hooks

`preSync` / `postSync` run a **one-off ECS task** with a command override — typically
migrations or cache warmup:

- Non-zero exit code **fails the sync** — the service update never happens.
- Timeouts stop the task and mark the sync failed.
- The task reuses the service's network configuration.

## Webhooks (instant sync)

Set `WEBHOOK_SECRET`, then point a GitHub webhook at
`https://your-host/api/webhook/github` (content type `application/json`, same secret,
push events only). Pushes to tracked branches trigger an immediate reconcile —
HMAC-SHA256 verified, rate-limited, payloads capped at 1 MiB. Polling remains as fallback.

## Dry-run mode

`DRY_RUN=true` turns the controller into an observer: every sync, rollback and prune
records **what it would do** (`[dry-run] …` in history and on the card) but never calls
an AWS mutation API.

Use it for demos, IAM policy verification and observation-only deployments. The UI
shows a persistent banner while active.

## High availability

With Postgres, replicas elect a single **leader** via a session-scoped advisory lock:

- Only the leader applies changes and prunes.
- Standbys keep polling Git and refreshing diffs read-only — their UI stays live with a
  "standby" banner; manual actions work from any replica.
- When the leader dies, a standby takes over within one `SYNC_INTERVAL`.
- Role is exposed in `/api/status` (`leader`) and the `androcd_leader` metric.

SQLite / no-DB deployments are single-instance and always leader.

## Autoscaling

```yaml
spec:
  service:
    autoscaling: {minCount: 2, maxCount: 20, targetCpu: 60, targetMemory: 75}
```

Target-tracking Application Auto Scaling; once configured, the autoscaler owns
`desiredCount` and the reconciler stops fighting it. Removing the block deregisters the
scalable target. Policies are named `androcd-<app>-cpu` / `-memory`.

## Load balancing

Two modes on `service.loadBalancer`:

- **Reference** (`targetGroupArn`) — attach the service to a target group you manage
  elsewhere (Terraform/CDK/console).
- **Managed** (`create`) — Andro-CD creates and reconciles an **ip-target-type target
  group** and a **listener rule** (host/path based) on an existing ALB listener:

```yaml
service:
  loadBalancer:
    containerPort: 8080
    create:
      listenerArn: arn:aws:elasticloadbalancing:...:listener/app/main/abc/def
      rule: {priority: 10, hostHeader: api.example.com}
      healthCheck: {path: /health, matcher: "200"}
```

- The TG is named `androcd-<app>`; the VPC comes from `spec.network.vpc` or is derived
  from the first subnet.
- Health-check settings and rule conditions are diffed and reconciled like any other
  field; rule `priority` is applied at creation.
- **Prune** deletes the rule and the TG together with the service (only resources
  Andro-CD created — the ALB and listener are never touched).
- The ALB and its listener remain your infrastructure — one ALB serves many
  Andro-CD apps, each with its own rule.

## Capacity providers (Fargate Spot)

```yaml
spec:
  service:
    capacityProviders:
      - {provider: FARGATE_SPOT, weight: 3}
      - {provider: FARGATE, weight: 1, base: 1}
```

`base` tasks always run on that provider; the rest split by weight. Managed Fargate
providers are associated automatically when Andro-CD creates the cluster. Switching an
existing service between plain `launchType` and a strategy requires recreating it (AWS
restriction) — Andro-CD won't fight services already using a strategy when the manifest
doesn't define one.

## Task definition hygiene

`KEEP_TASKDEF_REVISIONS=N` deregisters ACTIVE revisions beyond the newest N after each
successful sync. The in-use revision is never touched; `0` (default) keeps everything.

## Multi-account (AWS profiles)

Add named profiles in **AWS Profiles** (validated via STS, stored encrypted with
Fernet/AES) and reference them per app with `spec.awsProfile`. Region precedence:
`spec.region` > profile default > `AWS_REGION`.

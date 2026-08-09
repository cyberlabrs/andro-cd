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
- **Run now** — for an `ECSTask`, launch its task definition once (optional `count`); the
  run appears in the Tasks tab. `runPolicy.runOnSync` runs it automatically on task-def
  changes.

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
    autoscaling:
      minCount: 2
      maxCount: 20
      targetCpu: 60
      targetMemory: 75
      targetRequestsPerTarget: 250       # ALB requests per target; requires loadBalancer
```

Target-tracking Application Auto Scaling; once configured, the autoscaler owns
`desiredCount` and the reconciler stops fighting it. Removing a target deregisters the
matching policy. Policies are named `androcd-<app>-cpu` / `-memory` / `-requests`.

`targetRequestsPerTarget` targets AWS's `ALBRequestCountPerTarget` metric — the
ResourceLabel is resolved from either the referenced or managed target group. If the
target group isn't yet attached to an ALB at reconcile time, the requests policy is
skipped and re-attempted on the next tick.

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

## Deployment strategies (native blue/green, canary, linear)

Andro-CD passes through the deployment strategy AWS added natively to ECS
(`deploymentController=ECS`) — no CodeDeploy setup, no external Lambdas of your own.
The default is a plain rolling update; the other three shift traffic between **two
target groups** on the same ALB:

```yaml
spec:
  service:
    loadBalancer:
      containerPort: 80
      targetGroupArn: arn:aws:elasticloadbalancing:...:targetgroup/blue/...
      alternateTargetGroupArn: arn:aws:elasticloadbalancing:...:targetgroup/green/...
      productionListenerRule: arn:aws:elasticloadbalancing:...:listener-rule/prod/...
      testListenerRule: arn:aws:elasticloadbalancing:...:listener-rule/test/...   # optional dark canary
      roleArn: arn:aws:iam::...:role/ELBBlueGreen
    deploymentStrategy:
      type: BLUE_GREEN                   # ROLLING | BLUE_GREEN | CANARY | LINEAR
      bakeTimeMinutes: 15                # dwell on the new fleet before finalizing
      canaryPercent: 10                  # CANARY only — initial slice, 0<pct<100
      canaryBakeTimeMinutes: 5           # CANARY only
      linearStepPercent: 20              # LINEAR only — % shifted per step
      linearStepBakeTimeMinutes: 3       # LINEAR only
      alarms:                            # CloudWatch alarm-driven rollback
        alarmNames: [api-5xx, api-latency]
        rollback: true
        enable: true
      lifecycleHooks:                    # invoked at 1..N stages during the rollout
        - targetType: AWS_LAMBDA         # AWS_LAMBDA | PAUSE
          hookTargetArn: arn:aws:lambda:us-east-1:...:function:validate
          roleArn: arn:aws:iam::...:role/ecs-hook-invoker
          stages: [POST_TEST_TRAFFIC_SHIFT, POST_PRODUCTION_TRAFFIC_SHIFT]
          timeoutMinutes: 10
          timeoutAction: ROLLBACK        # ROLLBACK | CONTINUE
```

- `BLUE_GREEN` / `CANARY` / `LINEAR` all require `alternateTargetGroupArn` **and**
  `productionListenerRule` on the loadBalancer — Andro-CD rejects the manifest at parse
  time if either is missing, so the error surfaces in the diff rather than after an AWS
  API rejection.
- `testListenerRule` enables **dark canary**: the green fleet is reachable via a
  separate listener rule (typically on a distinct host or path) that only your synthetic
  checks and lifecycle-hook Lambdas hit. Production traffic still flows to blue until
  the production listener rule flips.
- The `alarms` block wires CloudWatch alarms to auto-rollback. When any listed alarm is
  in ALARM state during the rollout, ECS reverts to the previous task set.
- Lifecycle stages (any subset per hook): `RECONCILE_SERVICE`, `PRE_SCALE_UP`,
  `POST_SCALE_UP`, `TEST_TRAFFIC_SHIFT`, `POST_TEST_TRAFFIC_SHIFT`,
  `PRE_PRODUCTION_TRAFFIC_SHIFT`, `PRODUCTION_TRAFFIC_SHIFT`,
  `POST_PRODUCTION_TRAFFIC_SHIFT`. Return `{"hookStatus": "SUCCEEDED"}` to advance,
  `"FAILED"` to trigger rollback, `"IN_PROGRESS"` to have ECS re-poll.
- `PAUSE` hooks stop the deployment at a stage until an operator manually resumes it
  from the ECS console.

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

## End-to-end sandbox testing

`examples/e2e/` ships a self-contained kit that walks Andro-CD through every 2026
feature on a real (sandbox) AWS account:

- `iam-policy.json` — least-privilege policy for the CI user.
- `values.yaml` — one place to fill in your VPC / subnet / SG / TG / role ARNs
  (copy to `values.local.yaml`, which is gitignored).
- `manifests/01..08-*.yaml` — one manifest per scenario: rolling, autoscaling,
  EFS + FireLens, Service Connect, blue/green, canary, scheduled task, one-off task.
- `scripts/preflight.sh` — verifies AWS credentials, tool availability, and that
  the values file has no placeholder values.
- `scripts/run.sh` — for each manifest: triggers a sync, waits for Synced+Healthy,
  then hits AWS directly to assert the scenario-specific outcome (deployment
  strategy, scaling policies, Service Connect state, EFS mounts, etc.).
- `scripts/cleanup.sh` — deletes every AWS resource the run creates.

Typical loop:

```bash
cd examples/e2e
./scripts/bootstrap.sh                  # auto-create AWS infra + write values.local.yaml
./scripts/preflight.sh                  # green check → ready
./scripts/run.sh                        # applies + waits + asserts, per scenario
./scripts/cleanup.sh                    # drops ECS services (keep infra for next run)
./scripts/teardown.sh                   # nuke everything bootstrap created (when done)
```

`bootstrap.sh` auto-discovers what already exists (default VPC, subnets) and
creates whatever is missing (ALB, target groups, EFS, IAM roles, Lambda hook,
CloudWatch alarm, Cloud Map namespace). Every resource it creates is tagged
`androcd-e2e=true` so `teardown.sh` can find and remove them without touching
anything else in the account.

The same shape assertions run in-process against `moto`-mocked AWS as part of
`pytest` (`backend/tests/test_e2e_moto.py`); the sandbox kit is the "does it hold up
against real AWS" pass on top.

## Multi-account (AWS profiles)

Add named profiles in **AWS Profiles** (validated via STS, stored encrypted with
Fernet/AES) and reference them per app with `spec.awsProfile`. Region precedence:
`spec.region` > profile default > `AWS_REGION`.

# Andro-CD end-to-end sandbox test kit

Run Andro-CD through every 2026 feature on a **sandbox AWS account** and confirm each
one lands the way the unit + moto tests claim.

> **⚠️ Sandbox only.** Never point this at a production account. Every manifest here
> creates real AWS resources (Fargate tasks, target groups, EFS mounts, autoscaling
> policies). Estimated cost for a full run: **~$1–3** if you clean up within an hour.

## What's included

- [`iam-policy.json`](iam-policy.json) — minimum IAM policy for the CI user driving
  Andro-CD (least-privilege, no wildcards outside your account/region).
- [`values.yaml`](values.yaml) — template of every value the manifests reference.
  `scripts/bootstrap.sh` writes `values.local.yaml` for you.
- [`manifests/`](manifests/) — one manifest per scenario:
    - `01-rolling.yaml` — default rolling update (baseline).
    - `02-autoscaling.yaml` — CPU + ALB request-count target tracking.
    - `03-efs-firelens.yaml` — EFS volume mount + FireLens log router sidecar.
    - `04-service-connect.yaml` — Service Connect with client aliases.
    - `05-blue-green.yaml` — native blue/green with bake time + CloudWatch alarms.
    - `06-canary.yaml` — canary with lifecycle Lambda hook.
    - `07-scheduled-task.yaml` — EventBridge Scheduler cron.
    - `08-oneoff-task.yaml` — ECSTask run-now.
- [`scripts/bootstrap.sh`](scripts/bootstrap.sh) — **auto-discover + auto-create**
  every AWS resource the suite needs (VPC/subnets/SG, ALB + listener, 2 target
  groups, EFS + access point, IAM roles, CloudWatch alarm, Lambda hook, Cloud
  Map namespace, ECS cluster). Tags everything `androcd-e2e=true` for later
  cleanup. Writes `values.local.yaml` from what it found/created.
- [`scripts/preflight.sh`](scripts/preflight.sh) — read-only sanity checks after
  bootstrap (creds work, values.local.yaml complete, Andro-CD reachable).
- [`scripts/run.sh`](scripts/run.sh) — drives Andro-CD through every scenario and
  asserts scenario-specific outcomes against real AWS.
- [`scripts/cleanup.sh`](scripts/cleanup.sh) — deletes only the resources the
  **e2e run** creates (services, task defs, schedules). Leaves the bootstrapped
  infra alone so you can re-run without re-bootstrapping.
- [`scripts/teardown.sh`](scripts/teardown.sh) — deletes **everything**
  `bootstrap.sh` created (tag-guarded so it can't touch anything else). Use this
  when you're done with the suite entirely.

## Prerequisites

1. **Sandbox AWS account** — separate from anything real. Never point this at
   production; `bootstrap.sh` creates and `teardown.sh` deletes IAM roles,
   an ALB, target groups, EFS, etc.
2. **AWS CLI + `jq`** installed locally. `yq` is recommended (used by preflight).
3. **A running Andro-CD instance** — `docker compose up` from repo root works.
4. **IAM permissions for the CLI user** — attach [`iam-policy.json`](iam-policy.json)
   (covers both bootstrap/teardown and the reconciler itself).

## Setup (one command)

```bash
cd examples/e2e

# 1. Auto-provision every AWS resource + write values.local.yaml
./scripts/bootstrap.sh
# → prompts once, then creates/discovers VPC, subnets, SG, ALB, target groups,
#   EFS + access point, IAM roles, CloudWatch alarm, Lambda hook, Cloud Map
#   namespace, ECS cluster. Skips anything that already exists.
# → estimated cost while running: ~$18/month if left up (ALB is the big cost).

# 2. Sanity-check
./scripts/preflight.sh

# 3. Push manifests to a Git repo Andro-CD is polling
#    (the manifests use ${key} substitution from values.local.yaml at parse time
#    on Andro-CD's side — you commit the manifests as-is)

# 4. Drive the assertions
./scripts/run.sh
```

## Iterating

Re-running `run.sh` is safe — services get updated rather than recreated. When
you're temporarily done for the night, run `cleanup.sh` to drop the ECS
services and stop paying for tasks (the ALB stays up, ~$16/mo). When you're
completely done with the e2e suite, run `teardown.sh` to remove everything
`bootstrap.sh` created.

## What each step verifies

The runner does **not** modify Andro-CD — it drives Andro-CD via its own HTTP API
(`GET /api/apps/{name}`, `POST /api/apps/{name}/sync`, `GET /api/apps/{name}/resources`)
and asserts scenario-specific outcomes:

| Scenario | Assertion |
| --- | --- |
| Rolling | `sync_status=Synced`, `health=Healthy`, tasks running match desired |
| Autoscaling | `application-autoscaling describe-scalable-targets` returns min/max, CPU + requests policies both present |
| EFS + FireLens | Task definition has the EFS volume with correct `fileSystemId` + `accessPointId`; log-router container is present |
| Service Connect | Live service has `serviceConnectConfiguration.enabled=true` and the declared client aliases |
| Blue/Green | Live service has `deploymentController.type=ECS`, `deploymentConfiguration.strategy=BLUE_GREEN`, and the alt TG + prod listener rule are attached |
| Canary | Same as BG plus `canaryConfiguration.canaryPercent` matches the manifest |
| Scheduled task | EventBridge schedule exists, enabled, uses the declared cron expression |
| ECSTask | Task definition registered; `POST /api/apps/{name}/run` launches one and it completes |

Each assertion runs `aws` CLI directly against your account — the runner just prints
✅ / ❌ and moves on. Failures print the raw AWS response so you can see exactly what
Andro-CD sent vs what AWS returned.

## Cleanup

```bash
./scripts/cleanup.sh                # deletes ALL resources created by run.sh
```

You'll still own the pre-created infrastructure (VPC, ALB, EFS, alarms, Lambda) —
delete those manually if you're done for good.

## If something fails

- The runner prints the failing scenario's Andro-CD API response and the AWS
  describe output side by side.
- Copy that output (redact your account ID with `sed 's/[0-9]\{12\}/ACCOUNT/g'` if
  you're sharing it) and open an issue at
  <https://github.com/cyberlabrs/andro-cd/issues>.
- **Do not paste AWS credentials into issues, chat, or logs.** The runner never
  echoes them; keep it that way.

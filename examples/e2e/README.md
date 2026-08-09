# Andro-CD end-to-end sandbox test kit

Run Andro-CD through every 2026 feature on a **sandbox AWS account** and confirm each
one lands the way the unit + moto tests claim.

> **⚠️ Sandbox only.** Never point this at a production account. Every manifest here
> creates real AWS resources (Fargate tasks, target groups, EFS mounts, autoscaling
> policies). Estimated cost for a full run: **~$1–3** if you clean up within an hour.

## What's included

- [`iam-policy.json`](iam-policy.json) — minimum IAM policy for the CI user driving
  Andro-CD (least-privilege, no wildcards outside your account/region).
- [`values.yaml`](values.yaml) — one place to fill in your VPC / subnet / SG / TG /
  role ARNs. Every manifest reads from it via Andro-CD's built-in `${key}` templating.
- [`manifests/`](manifests/) — one manifest per scenario:
    - `01-rolling.yaml` — default rolling update (baseline).
    - `02-autoscaling.yaml` — CPU + ALB request-count target tracking.
    - `03-efs-firelens.yaml` — EFS volume mount + FireLens log router sidecar.
    - `04-service-connect.yaml` — Service Connect with client aliases.
    - `05-blue-green.yaml` — native blue/green with bake time + CloudWatch alarms.
    - `06-canary.yaml` — canary with lifecycle Lambda hook.
    - `07-scheduled-task.yaml` — EventBridge Scheduler cron.
    - `08-oneoff-task.yaml` — ECSTask run-now.
- [`scripts/preflight.sh`](scripts/preflight.sh) — check AWS creds work + list the
  values.yaml keys you still need to fill in.
- [`scripts/run.sh`](scripts/run.sh) — one-command runner: applies each manifest,
  waits for Synced+Healthy, checks a scenario-specific assertion, then moves on.
- [`scripts/cleanup.sh`](scripts/cleanup.sh) — deletes every AWS resource the run
  creates so you don't pay overnight.

## Prerequisites (your side, one-time)

1. **Sandbox AWS account** — separate from anything real.
2. **VPC with two subnets in different AZs** and a **security group** that permits
   inbound `80/tcp` from wherever you'll test (the ALB SG is fine).
3. **An empty Application Load Balancer + HTTP listener** in that VPC. The e2e run
   creates its own target groups and listener rules against it.
4. **Two additional target groups** (blue + green) for the blue/green + canary tests.
   Both `TargetType: ip`, protocol HTTP, port 80.
5. **An IAM role** ECS can assume as `taskExecutionRoleArn` (managed policy
   `AmazonECSTaskExecutionRolePolicy`) — or reuse `ecsTaskExecutionRole` if it
   already exists.
6. **An EFS filesystem** with a mount target in each subnet (for `03-efs-firelens`).
   Optional: an EFS access point.
7. **A CloudWatch alarm** (any alarm, even one that never fires) for the blue/green
   test to reference. Metric doesn't matter.
8. **A Lambda function** ECS can invoke for the canary lifecycle hook. The
   function body can literally be:
   ```python
   def handler(event, context):
       return {"hookStatus": "SUCCEEDED"}
   ```
9. **A running Andro-CD instance** (`docker compose up` from repo root works fine).

## Setup

```bash
# 1. Fill in the ARNs / IDs from your sandbox
cd examples/e2e
cp values.yaml values.local.yaml    # values.local.* is gitignored
$EDITOR values.local.yaml           # every ${key} in the manifests reads from here

# 2. Verify AWS credentials work and every values.yaml key is set
./scripts/preflight.sh

# 3. Push manifests to a Git repo Andro-CD is polling
#    (or the repo you already registered — the manifests use unique names)

# 4. Run the checklist
./scripts/run.sh                    # applies + waits + asserts, per scenario
```

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

# Getting started

## Run locally

```bash
git clone https://github.com/cyberlabrs/andro-cd
cd andro-cd
cp .env.example .env        # fill in secrets — see Configuration
docker compose up --build
```

Open **http://localhost:8080**. Without repositories connected the dashboard is empty
and guides you to add one.

!!! tip "AWS credentials"
    Locally, Andro-CD uses your mounted `~/.aws` (read-only) or `AWS_*` env vars.
    In production, give the task an IAM role — see [Configuration](configuration.md#iam-permissions)
    for the minimal policy, and test it risk-free with `DRY_RUN=true`.

## Connect a manifest repository

Click **Repositories → Connect**. Three authentication methods are supported:

| Method | When to use | What to paste |
|---|---|---|
| **HTTPS / token** | Personal repos, quick setup | GitHub personal access token (repo scope) |
| **SSH key** | Organization deploy keys | Private key (`-----BEGIN OPENSSH PRIVATE KEY-----`) |
| **GitHub App** | Team-wide, no personal tokens | App ID + Installation ID + PEM private key |

For GitHub App, the backend exchanges a JWT for short-lived installation tokens and
refreshes them automatically. Credentials are never written to disk — HTTPS and GitHub
App tokens flow through in-memory headers only.

## Your first manifest

Push this to the connected repo:

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

Within ~60 s (or instantly with a [webhook](operations.md#webhooks-instant-sync)) the
reconciler creates the cluster, registers the task definition and creates the service.
Watch the rollout in **Overview → Deployment**.

## Core concepts

### Sync status

| Status | Meaning |
|---|---|
| **Synced** | Live AWS state matches the manifest |
| **OutOfSync** | Manifest differs from AWS (see the **Diff** tab) |
| **Syncing** | Reconciliation in progress |
| **Error** | The last sync attempt failed |
| **Orphaned** | App removed from Git; AWS resources still exist |
| **Unknown** | Not evaluated yet |

### Health

| Health | Meaning |
|---|---|
| **Healthy** | `runningCount == desiredCount`, rollout `COMPLETED` |
| **Progressing** | Rolling update in progress |
| **Degraded** | Rollout failed or `runningCount < desiredCount` |
| **Unknown** | Resource doesn't exist yet |

### The reconcile loop

1. Every `SYNC_INTERVAL` seconds (default 60) — or immediately on a GitHub webhook —
   one reconciliation pass runs.
2. Every tracked repository is fetched; all `*.yaml` / `*.yml` files are parsed
   (with [values-file templating](manifest.md#values-files-templating) applied).
3. `ECSServiceSet` documents expand into individual apps.
4. Per app: diff live vs desired → if OutOfSync and eligible → apply.
5. [Sync waves](operations.md#sync-waves) proceed in order.
6. Metrics, notifications, history and audit entries are recorded.

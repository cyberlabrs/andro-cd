# Configuration

All settings are environment variables — set them in `.env` (docker-compose loads it
automatically). Start from [`.env.example`](https://github.com/cyberlabrs/andro-cd/blob/main/.env.example),
which documents every variable with a secure-setup checklist.

## Core

| Variable | Default | Description |
|---|---|---|
| `GIT_REPO_URL` / `GIT_BRANCH` / `GIT_PATH` / `GIT_TOKEN` | — | Optional bootstrap repo (repos are usually managed via the UI) |
| `SYNC_INTERVAL` | `60` | Seconds between reconcile passes |
| `AUTO_SYNC` | `true` | Apply automatically vs surface OutOfSync only |
| `DRY_RUN` | `false` | Record plans only — never call AWS mutation APIs |
| `RECONCILE_WORKERS` | `8` | Parallel diff workers per pass |
| `KEEP_TASKDEF_REVISIONS` | `0` | Deregister old ACTIVE revisions beyond newest N |
| `AWS_REGION` | — | Default region |
| `PORT` | `8080` | HTTP port |
| `DATABASE_URL` | sqlite in `/data` | Persistence (Postgres in compose; enables HA + audit) |
| `LOG_FORMAT` | `text` | `json` for structured logs |

## Authentication & RBAC

| Variable | Default | Description |
|---|---|---|
| `AUTH_MODE` | `none` | `github` or `oidc` enables login |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | — | GitHub OAuth App credentials |
| `GITHUB_ALLOWED_USERS` | — | Comma-separated allowlist |
| `GITHUB_ALLOWED_ORG` | — | Restrict login to an org's members |
| `OIDC_ISSUER` | — | OIDC provider issuer (discovery URL base) |
| `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` | — | OIDC client credentials |
| `OIDC_SCOPES` | `openid email profile` | Requested scopes |
| `OIDC_USERNAME_CLAIM` | `email` | Claim used as the login for RBAC |
| `OIDC_ALLOWED_USERS` / `OIDC_ALLOWED_DOMAINS` / `OIDC_ALLOWED_GROUPS` | — | Login allowlists (each configured one must pass) |
| `OIDC_GROUPS_CLAIM` | `groups` | Claim holding group membership |
| `SESSION_SECRET` | random | Signs session cookies — set it for stable sessions |
| `PUBLIC_URL` | `http://localhost:8080` | External URL: OAuth callback, CSRF, Secure cookies/HSTS |
| `RBAC_ADMINS` / `RBAC_OPERATORS` / `RBAC_DEFAULT_ROLE` | — | Role assignment |
| `API_TOKENS` | — | CI bearer tokens: `token:role[,…]` |

## Integrations

| Variable | Default | Description |
|---|---|---|
| `WEBHOOK_SECRET` | — | Enables the GitHub push webhook |
| `SLACK_WEBHOOK_URL` | — | Slack notifications |
| `ENCRYPTION_KEY` | falls back to `SESSION_SECRET` | Encrypts stored AWS profiles |
| `METRICS_TOKEN` | — | Bearer token for `/metrics` |

## IAM permissions

Minimum policy for the role running Andro-CD:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": [
      "ecs:CreateCluster", "ecs:UpdateCluster", "ecs:DeleteCluster",
      "ecs:PutClusterCapacityProviders", "ecs:DescribeClusters",
      "ecs:RegisterTaskDefinition", "ecs:DeregisterTaskDefinition",
      "ecs:DescribeTaskDefinition", "ecs:ListTaskDefinitions", "ecs:TagResource",
      "ecs:CreateService", "ecs:UpdateService", "ecs:DeleteService",
      "ecs:DescribeServices", "ecs:RunTask", "ecs:StopTask",
      "ecs:ListTasks", "ecs:DescribeTasks",
      "iam:PassRole",
      "logs:CreateLogGroup", "logs:DescribeLogStreams",
      "logs:GetLogEvents", "logs:FilterLogEvents",
      "ecr:DescribeImages",
      "ec2:DescribeSubnets",
      "elasticloadbalancing:DescribeTargetGroups", "elasticloadbalancing:CreateTargetGroup",
      "elasticloadbalancing:ModifyTargetGroup", "elasticloadbalancing:DeleteTargetGroup",
      "elasticloadbalancing:DescribeRules", "elasticloadbalancing:CreateRule",
      "elasticloadbalancing:ModifyRule", "elasticloadbalancing:DeleteRule",
      "elasticloadbalancing:AddTags",
      "application-autoscaling:*",
      "scheduler:CreateSchedule", "scheduler:UpdateSchedule",
      "scheduler:DeleteSchedule", "scheduler:GetSchedule",
      "sts:GetCallerIdentity"
    ],
    "Resource": "*"
  }]
}
```

Trim per use case: drop `scheduler:*` without `ECSScheduledTask`,
`application-autoscaling:*` without autoscaling,
`elasticloadbalancing:*`/`ec2:DescribeSubnets` without managed load balancers
(`loadBalancer.create`), `ecs:DeleteService` if you never prune,
`ecs:DeregisterTaskDefinition` without `KEEP_TASKDEF_REVISIONS`, `ecs:TagResource`
without labels, the cluster mutations without `ECSCluster`.

!!! tip
    Run with `DRY_RUN=true` first — the recorded plans show exactly which calls the
    controller would make, so you can verify the policy before granting write access.

## Troubleshooting

**"The security token included in the request is invalid"**
:   AWS credentials expired or wrong. Refresh and restart the container.

**Diff always OutOfSync after enabling `resolveImages`**
:   Expected — tags now resolve to digests the live task definition doesn't have yet.
    Sync once; diffs stay quiet until the tag actually moves upstream.

**"cannot decrypt stored secret — was ENCRYPTION_KEY/SESSION_SECRET changed?"**
:   The Fernet key changed, so stored AWS profiles can't be read. Restore the old value
    or delete + re-add the profiles.

**Auto-sync doesn't run after a rollback**
:   By design — rollback pauses the app so the reconciler doesn't undo it. Click
    **Sync** to return to Git state.

**Everything shows `[dry-run]` and nothing deploys**
:   `DRY_RUN=true` is set. Unset it, restart, then Sync (or push a commit).

**Two replicas — which one applies?**
:   The leader (advisory lock). If both apply, they're not sharing the same Postgres
    `DATABASE_URL`.

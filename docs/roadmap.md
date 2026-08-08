# Roadmap

The full, continuously updated list lives in
[IMPROVEMENTS.md](https://github.com/cyberlabrs/andro-cd/blob/main/IMPROVEMENTS.md)
— each item is marked *(done …)* inline as it lands. Highlights:

## Shipped

- GitOps core: multi-repo, webhooks, rollback, prune, self-heal, sync waves,
  pre/post-sync hooks, app-of-apps (`ECSServiceSet`), values-file templating,
  sync windows
- AWS coverage: `ECSCluster` and `ECSTask` (one-off jobs / run-now) kinds, scheduled
  tasks, autoscaling (CPU, memory, **and ALB request-count per target**), load balancers
  (attach to an existing target group **or** create the target group + listener rule from
  the manifest), capacity providers (Fargate Spot), container health checks, ECR digest
  pinning, labels → AWS tags, task-definition cleanup, multi-account profiles,
  **EFS task volumes** (with EFS Access Point + IAM), **FireLens sidecars**
  (`awsfirelens` log driver + fluentbit/fluentd router), **Service Connect** per service
  (namespace, discovery names, client aliases), **native ECS deployment strategies**
  (rolling / blue-green / canary / linear with `deploymentController=ECS`, bake time,
  dark canary via test listener, CloudWatch-alarm auto-rollback, and up to 8 Lambda /
  PAUSE lifecycle hooks across all 8 stages)
- Security: GitHub OAuth **and generic OIDC** (Google/Okta/Keycloak/Dex/Auth0/Azure AD)
  + RBAC, API tokens, audit log, CSP/CSRF/rate limiting, encrypted credentials, non-root
  container, JSON Schema publishing, **sliding-window session refresh with absolute cap**
- Operations: HA leader election, dry-run mode, exponential backoff, batched AWS
  describes, parallel reconciliation, readiness probes, Prometheus metrics, Slack
  notifications, structured logging
- UI: Argo-style dashboard, live logs, task forensics, **deployment timeline** (outcome,
  commit, images, duration per deploy), dark mode, URL-shared filters,
  **rich side-by-side diff** (YAML/JSON toggle, syntax highlighting, line numbers,
  synchronized scrolling, hide-unchanged fold, +N/−N stats)

## Next up

1. **Grafana dashboard JSON** — a ready-made dashboard for the Prometheus metrics.
2. **Backoff & retry** — per-app exponential backoff on repeated sync failures,
   with a manual reset once the underlying cause is fixed.
3. **AWS rate-limit handling** — botocore adaptive retry mode, jitter between apps.
4. **CLI expansion** — `androcd diff`, `androcd sync <app>`, `androcd logs <app>`
   hitting the API from CI.
5. **UI for deployment strategy state** — surface the active strategy, current bake
   remaining, and lifecycle-hook history on the Overview tab.

## Contributing

Issues and PRs are welcome — see
[CONTRIBUTING.md](https://github.com/cyberlabrs/andro-cd/blob/main/CONTRIBUTING.md).
The project uses conventional commits and automated releases (release-please).

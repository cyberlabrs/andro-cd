# Roadmap

The full, continuously updated list lives in
[IMPROVEMENTS.md](https://github.com/cyberlabrs/andro-cd/blob/main/IMPROVEMENTS.md)
— each item is marked *(done …)* inline as it lands. Highlights:

## Shipped

- GitOps core: multi-repo, webhooks, rollback, prune, self-heal, sync waves,
  pre/post-sync hooks, app-of-apps (`ECSServiceSet`), values-file templating,
  sync windows
- AWS coverage: `ECSCluster` and `ECSTask` (one-off jobs / run-now) kinds, scheduled
  tasks, autoscaling, load balancers (attach to an existing target group **or** create the
  target group + listener rule from the manifest), capacity providers (Fargate Spot),
  container health checks, ECR digest pinning, labels → AWS tags, task-definition cleanup,
  multi-account profiles
- Security: GitHub OAuth **and generic OIDC** (Google/Okta/Keycloak/Dex/Auth0/Azure AD)
  + RBAC, API tokens, audit log, CSP/CSRF/rate limiting, encrypted credentials, non-root
  container, JSON Schema publishing
- Operations: HA leader election, dry-run mode, exponential backoff, batched AWS
  describes, parallel reconciliation, readiness probes, Prometheus metrics, Slack
  notifications, structured logging
- UI: Argo-style dashboard, side-by-side diff, live logs, task forensics, **deployment
  timeline** (outcome, commit, images, duration per deploy), dark mode, URL-shared filters

## Next up

1. **EFS volumes & FireLens sidecars** — rounding out task-definition coverage.
2. **ALB request-count autoscaling** — target-tracking on `ALBRequestCountPerTarget`
   (now that managed target groups exist).
3. **Service Connect / Cloud Map** — service discovery config in the manifest.
4. **Session refresh** — silently renew expiring sessions instead of forcing re-login.
5. **Grafana dashboard JSON** — a ready-made dashboard for the Prometheus metrics.

## Contributing

Issues and PRs are welcome — see
[CONTRIBUTING.md](https://github.com/cyberlabrs/andro-cd/blob/main/CONTRIBUTING.md).
The project uses conventional commits and automated releases (release-please).

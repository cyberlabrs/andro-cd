# Roadmap

The full, continuously updated list lives in
[IMPROVEMENTS.md](https://github.com/cyberlabrs/andro-cd/blob/main/IMPROVEMENTS.md)
— each item is marked *(done …)* inline as it lands. Highlights:

## Shipped

- GitOps core: multi-repo, webhooks, rollback, prune, self-heal, sync waves,
  pre/post-sync hooks, app-of-apps (`ECSServiceSet`), values-file templating,
  sync windows
- AWS coverage: `ECSCluster` kind, scheduled tasks, autoscaling, load balancers
  (attach to an existing target group **or** create the target group + listener rule
  from the manifest), capacity providers (Fargate Spot), container health checks, ECR
  digest pinning, labels → AWS tags, task-definition cleanup, multi-account profiles
- Security: GitHub OAuth + RBAC, API tokens, audit log, CSP/CSRF/rate limiting,
  encrypted credentials, non-root container, JSON Schema publishing
- Operations: HA leader election, dry-run mode, exponential backoff, batched AWS
  describes, parallel reconciliation, readiness probes, Prometheus metrics, Slack
  notifications, structured logging

## Next up

1. **Generic OIDC** — Google/Okta/Dex login next to GitHub OAuth.
2. **Deployment timeline UI** — per-app timeline with commit, images, duration and
   outcome (the data is already persisted).
3. **`ECSTask` kind** — one-off tasks/jobs with "run now" in the UI.
4. **EFS volumes & FireLens sidecars** — rounding out task-definition coverage.
5. **ALB request-count autoscaling** — target-tracking on `ALBRequestCountPerTarget`
   (now that managed target groups exist).

## Contributing

Issues and PRs are welcome — see
[CONTRIBUTING.md](https://github.com/cyberlabrs/andro-cd/blob/main/CONTRIBUTING.md).
The project uses conventional commits and automated releases (release-please).

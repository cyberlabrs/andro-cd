# Observability

## Prometheus metrics

`GET /metrics` (set `METRICS_TOKEN` to require a bearer token):

| Metric | Description |
|---|---|
| `androcd_sync_total{app, result}` | Sync count, `result="success"` or `"error"` |
| `androcd_sync_duration_seconds{app}` | Histogram of sync durations |
| `androcd_apps_sync_status{status}` | Apps per sync status |
| `androcd_apps_health{health}` | Apps per health status |
| `androcd_git_poll_errors_total` | Failed git polls |
| `androcd_git_unchanged_total` | Polls short-circuited (remote HEAD unchanged) |
| `androcd_last_poll_timestamp_seconds` | Unix ts of the last poll |
| `androcd_reconcile_pass_seconds` | Histogram of full reconcile passes |
| `androcd_leader` | 1 on the leader replica, 0 on standbys |

Alerting starters: `time() - androcd_last_poll_timestamp_seconds > 300` (polling
stalled), `androcd_apps_health{health="Degraded"} > 0`, `sum(androcd_leader) != 1`.

## Health endpoints

- `GET /healthz` — pure liveness (process is up). Used by the container healthcheck.
- `GET /readyz` — readiness: returns `503` with reasons when the database is
  unreachable, git polling has stalled, or repositories are failing to sync. Point load
  balancers and monitoring here.

## Notifications (Slack)

Set `SLACK_WEBHOOK_URL` to receive:

- 🚀 sync succeeded (commit + actions performed)
- ✗ sync failed
- ⚠️ app transitioned into Degraded
- ↻ manual rollback
- 🗑 app pruned

## Live logs in the UI

The **Logs** tab streams CloudWatch events over Server-Sent Events — follow mode,
pause/resume, container selector. Requires `logGroup` on the container and
`logs:DescribeLogStreams` + `logs:GetLogEvents` + `logs:FilterLogEvents` permissions.

## Task forensics

The **Tasks** tab shows running tasks (status, health, IP, AZ, started time) and
**stopped tasks with per-container exit codes and stop reasons** — the first place to
look when a deployment is crash-looping.

## Structured logging

`LOG_FORMAT=json` switches controller logs to JSON (`ts`, `level`, `logger`, `msg`,
`exc`) for CloudWatch Logs Insights / Loki ingestion.

## Deployment timeline & audit

The **History** tab renders a per-app **deployment timeline** — a vertical list of every
sync, newest first, each showing the outcome (synced / failed / dry-run), the Git commit,
the container images deployed, the duration, and the actions performed (or the error). The
raw data (`commit`, `actions`, `outcome`, `message`, `durationMs`, `images`) persists in
the `sync_history` table and is served by `GET /api/apps/{name}/history`.

The [audit log](security.md#audit-log) adds the who/when/from-where dimension on top.

## Deployment strategy state (Overview tab)

When a service opts into a non-rolling strategy
([blue/green, canary, linear](operations.md#deployment-strategies-native-bluegreen-canary-linear)),
the **Overview** tab surfaces a dedicated **Deployment strategy** panel:

- Colored strategy badge (Blue/Green, Canary, Linear).
- **Bake time countdown** — a live progress bar that ticks every 5 s from the
  primary deployment's `updatedAt` timestamp, showing `Xm YYs remaining` and
  `bake N%`. When the deployment enters a new stage, the countdown restarts against
  the new `updatedAt`.
- **CANARY / LINEAR sizing** — canary slice %, linear step %, per-step bake time.
- **Rollback alarms** — the CloudWatch alarms wired for auto-rollback, with a clear
  `auto-rollback` suffix when `alarms.rollback = true`.
- **Lifecycle hooks** — the full list of `AWS_LAMBDA` / `PAUSE` hooks with the
  stages each fires at.

The deployments list below the panel now also carries `rolloutStateReason` as a
tooltip on each row — hover to see AWS's own explanation for the current stage
(e.g. "ECS deployment is bake in progress").

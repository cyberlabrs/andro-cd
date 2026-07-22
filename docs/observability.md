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

## Sync history & audit

Per-app sync history (commit, actions, outcome, message) persists in the database and
is shown in the **History** tab. The [audit log](security.md#audit-log) adds the
who/when/from-where dimension.

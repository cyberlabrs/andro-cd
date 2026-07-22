from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SYNC_STATUSES = ["Synced", "OutOfSync", "Syncing", "Error", "Orphaned", "Unknown"]
HEALTH_STATUSES = ["Healthy", "Progressing", "Degraded", "Unknown"]

SYNC_TOTAL = Counter(
    "androcd_sync_total", "Number of sync operations", ["app", "result"]
)
SYNC_DURATION = Histogram(
    "androcd_sync_duration_seconds", "Duration of sync operations", ["app"]
)
APPS_BY_SYNC = Gauge(
    "androcd_apps_sync_status", "Apps per sync status", ["status"]
)
APPS_BY_HEALTH = Gauge(
    "androcd_apps_health", "Apps per health status", ["health"]
)
GIT_POLL_ERRORS = Counter(
    "androcd_git_poll_errors_total", "Failed git poll attempts"
)
GIT_UNCHANGED_TOTAL = Counter(
    "androcd_git_unchanged_total",
    "Number of polls that short-circuited because the remote HEAD hadn't moved",
)
LAST_POLL_TS = Gauge(
    "androcd_last_poll_timestamp_seconds", "Unix timestamp of the last git poll"
)
RECONCILE_DURATION = Histogram(
    "androcd_reconcile_pass_seconds",
    "Duration of a single reconcile pass across all apps",
)
LEADER = Gauge(
    "androcd_leader",
    "1 when this replica holds the leader lock (applies changes), 0 on standby",
)
LEADER.set(1)   # single-instance deployments are always the leader


def update_app_gauges(apps) -> None:
    sync_counts: dict[str, int] = {}
    health_counts: dict[str, int] = {}
    for app in apps:
        sync_counts[app.sync_status] = sync_counts.get(app.sync_status, 0) + 1
        health_counts[app.health] = health_counts.get(app.health, 0) + 1
    for s in SYNC_STATUSES:
        APPS_BY_SYNC.labels(status=s).set(sync_counts.get(s, 0))
    for h in HEALTH_STATUSES:
        APPS_BY_HEALTH.labels(health=h).set(health_counts.get(h, 0))


def render() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST

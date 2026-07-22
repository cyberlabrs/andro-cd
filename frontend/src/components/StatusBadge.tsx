const SYNC_ICONS: Record<string, string> = {
  Synced: "✓",
  OutOfSync: "↻",
  Syncing: "…",
  Error: "✗",
  Orphaned: "⌀",
  Unknown: "?",
};

const HEALTH_ICONS: Record<string, string> = {
  Healthy: "♥",
  Progressing: "◌",
  Degraded: "▲",
  Unknown: "?",
};

export function SyncBadge({ status }: { status: string }) {
  return (
    <span className={`badge sync-${status.toLowerCase()}`}>
      {SYNC_ICONS[status] ?? "?"} {status}
    </span>
  );
}

export function HealthBadge({ health }: { health: string }) {
  return (
    <span className={`badge health-${health.toLowerCase()}`}>
      {HEALTH_ICONS[health] ?? "?"} {health}
    </span>
  );
}

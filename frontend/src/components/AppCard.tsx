import type { AppSummary } from "../types";
import { HealthBadge, SyncBadge } from "./StatusBadge";

interface Props {
  app: AppSummary;
  onClick: () => void;
  onLabelClick: (label: string) => void;
}

export function AppCard({ app, onClick, onLabelClick }: Props) {
  const running = app.runningCount ?? 0;
  const desired = app.desiredCount ?? 0;
  const pct = desired > 0 ? Math.min(100, Math.round((running / desired) * 100)) : 0;
  const image = app.images[0];

  return (
    <div className={`card status-${app.health.toLowerCase()}`} onClick={onClick}>
      <div className="card-head">
        <h2>
          {app.name}
          {app.kind === "ECSScheduledTask" && <span className="badge sync-unknown" style={{ marginLeft: 8 }}>⏰ cron</span>}
        </h2>
        <div className="badges">
          <SyncBadge status={app.syncStatus} />
          <HealthBadge health={app.health} />
        </div>
      </div>

      <div className="card-meta">
        {app.cluster && <span>cluster: <b>{app.cluster}</b></span>}
        {app.region && <span>region: <b>{app.region}</b></span>}
      </div>
      {image && (
        <div className="card-image mono" title={app.images.join("\n")}>
          {image.replace(/^[0-9]+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com\//, "")}
          {app.images.length > 1 && <span className="muted"> +{app.images.length - 1}</span>}
        </div>
      )}

      {app.desiredCount != null && (
        <div className="progress-row">
          <div className="progress">
            <div
              className={`progress-fill ${running >= desired && desired > 0 ? "ok" : ""}`}
              style={{ width: `${pct}%` }}
            />
          </div>
          <span className="progress-label">{running}/{desired}</span>
        </div>
      )}

      {Object.keys(app.labels ?? {}).length > 0 && (
        <div className="label-chips">
          {Object.entries(app.labels).map(([k, v]) => (
            <span
              key={k}
              className="label-chip"
              title={`filter by ${k}=${v}`}
              onClick={(e) => {
                e.stopPropagation();
                onLabelClick(`${k}=${v}`);
              }}
            >
              {k}={v}
            </span>
          ))}
        </div>
      )}

      {app.message && <div className="card-message">{app.message}</div>}
      {app.changes.length > 0 && (
        <div className="card-changes">
          {app.changes.slice(0, 3).map((c, i) => (
            <div key={i} className="change">• {c}</div>
          ))}
          {app.changes.length > 3 && (
            <div className="muted">+{app.changes.length - 3} more…</div>
          )}
        </div>
      )}

      <div className="card-footer muted">
        {app.repo ? `${app.repo.replace(/^https?:\/\/[^/]+\//, "").replace(/\.git$/, "")} · ` : ""}
        {app.file}
      </div>
    </div>
  );
}

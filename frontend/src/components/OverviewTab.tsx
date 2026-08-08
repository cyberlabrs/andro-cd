import { useEffect, useState } from "react";
import type { AppDetail, Resources } from "../types";

interface Props {
  app: AppDetail;
  resources: Resources | null;
}

// Bake-time countdown: given a deployment `updatedAt` timestamp (when it entered
// its current rollout stage) and a bake duration in minutes, return the elapsed
// percent and a human-readable remaining string. Ticks every 5s while mounted.
function useBakeProgress(updatedAt: string | null, bakeMinutes: number | null) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!bakeMinutes || !updatedAt) return;
    const t = setInterval(() => setNow(Date.now()), 5000);
    return () => clearInterval(t);
  }, [bakeMinutes, updatedAt]);
  if (!bakeMinutes || !updatedAt) return null;
  const start = new Date(updatedAt).getTime();
  if (isNaN(start)) return null;
  const totalMs = bakeMinutes * 60_000;
  const elapsedMs = Math.max(0, now - start);
  const pct = Math.min(100, Math.round((elapsedMs / totalMs) * 100));
  const remainingSec = Math.max(0, Math.round((totalMs - elapsedMs) / 1000));
  if (remainingSec === 0) return { pct: 100, label: "bake complete" };
  const mm = Math.floor(remainingSec / 60);
  const ss = remainingSec % 60;
  return { pct, label: `${mm}m ${ss.toString().padStart(2, "0")}s remaining` };
}

const STRATEGY_LABEL: Record<string, string> = {
  ROLLING: "Rolling",
  BLUE_GREEN: "Blue/Green",
  CANARY: "Canary",
  LINEAR: "Linear",
};

export function OverviewTab({ app, resources }: Props) {
  const svc = resources?.service ?? null;
  const running = svc?.runningCount ?? 0;
  const desired = svc?.desiredCount ?? 0;
  const pct = desired > 0 ? Math.min(100, Math.round((running / desired) * 100)) : 0;
  const ds = svc?.deploymentStrategy ?? null;
  const primary = svc?.deployments.find((d) => d.status === "PRIMARY") ?? null;
  const bake = useBakeProgress(
    primary?.updatedAt ?? null,
    ds?.bakeTimeMinutes ?? null,
  );

  return (
    <>
      {app.message && <p className="card-message">{app.message}</p>}
      {resources?.error && <div className="banner error">{resources.error}</div>}
      {app.syncPaused && (
        <div className="banner warn">
          Auto-sync is paused (manual rollback active). Click <b>Sync</b> to return to the Git state.
        </div>
      )}

      {svc && (
        <section>
          <h3>Deployment</h3>
          <div className="progress-row">
            <div className="progress">
              <div
                className={`progress-fill ${running >= desired ? "ok" : ""}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className="progress-label">
              {running}/{desired} running
              {svc.pendingCount > 0 && ` · ${svc.pendingCount} pending`}
            </span>
          </div>
          {svc.deployments.map((d, i) => (
            <div key={i} className="deployment-row" title={d.rolloutStateReason ?? undefined}>
              <span className={`badge ${d.status === "PRIMARY" ? "sync-synced" : "sync-unknown"}`}>
                {d.status}
              </span>
              <span className="mono">{d.taskDefinition}</span>
              <span className="muted">
                {d.running}/{d.desired} running
                {d.failed > 0 && ` · ${d.failed} failed`}
              </span>
              {d.rolloutState && (
                <span className={d.rolloutState === "FAILED" ? "err-text" : "muted"}>
                  {d.rolloutState}
                </span>
              )}
            </div>
          ))}
        </section>
      )}

      {svc && ds && ds.type !== "ROLLING" && (
        <section>
          <h3>
            Deployment strategy{" "}
            <span className={`badge strategy strategy-${ds.type.toLowerCase().replace("_", "-")}`}>
              {STRATEGY_LABEL[ds.type] ?? ds.type}
            </span>
          </h3>
          <div className="config-grid">
            {ds.type === "CANARY" && ds.canaryPercent != null && (
              <div className="kv">
                <span>Canary slice</span>
                <span>
                  {ds.canaryPercent}%
                  {ds.canaryBakeTimeMinutes != null && ` · ${ds.canaryBakeTimeMinutes}m bake`}
                </span>
              </div>
            )}
            {ds.type === "LINEAR" && ds.linearStepPercent != null && (
              <div className="kv">
                <span>Step size</span>
                <span>
                  {ds.linearStepPercent}% per step
                  {ds.linearStepBakeTimeMinutes != null && ` · ${ds.linearStepBakeTimeMinutes}m bake`}
                </span>
              </div>
            )}
            {ds.bakeTimeMinutes != null && (
              <div className="kv">
                <span>Bake time</span>
                <span>
                  {ds.bakeTimeMinutes}m
                  {bake && ` · ${bake.label}`}
                </span>
              </div>
            )}
            {ds.alarms && ds.alarms.alarmNames.length > 0 && (
              <div className="kv">
                <span>Rollback alarms</span>
                <span className="mono">
                  {ds.alarms.enable ? "" : "(disabled) "}
                  {ds.alarms.alarmNames.join(", ")}
                  {ds.alarms.rollback ? " · auto-rollback" : ""}
                </span>
              </div>
            )}
          </div>

          {bake && ds.bakeTimeMinutes != null && (
            <div className="progress-row" style={{ marginTop: 8 }}>
              <div className="progress">
                <div
                  className={`progress-fill ${bake.pct >= 100 ? "ok" : ""}`}
                  style={{ width: `${bake.pct}%` }}
                />
              </div>
              <span className="progress-label">bake {bake.pct}%</span>
            </div>
          )}

          {ds.lifecycleHooks.length > 0 && (
            <div className="hooks-list" style={{ marginTop: 10 }}>
              <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>
                Lifecycle hooks
              </div>
              {ds.lifecycleHooks.map((h, i) => (
                <div key={i} className="hook-row">
                  <span className={`badge ${h.targetType === "PAUSE" ? "sync-unknown" : "sync-synced"}`}>
                    {h.targetType}
                  </span>
                  <span className="mono" title={h.hookTargetArn ?? undefined}>
                    {h.hookTargetArn ? h.hookTargetArn.split(":").slice(-1)[0] : "—"}
                  </span>
                  <span className="muted">at: {h.stages.join(", ")}</span>
                </div>
              ))}
            </div>
          )}
        </section>
      )}

      {app.changes.length > 0 && (
        <section>
          <h3>Pending changes</h3>
          {app.changes.map((c, i) => (
            <div key={i} className="change">• {c}</div>
          ))}
        </section>
      )}

      <section>
        <h3>Configuration</h3>
        <div className="config-grid">
          <div className="kv"><span>Cluster</span><span>{app.cluster ?? "—"}{resources?.cluster ? "" : " (missing)"}</span></div>
          <div className="kv"><span>Region</span><span>{app.region ?? "—"}</span></div>
          <div className="kv"><span>AWS profile</span><span>{app.awsProfile ?? "default"}</span></div>
          {svc ? (
            <>
              <div className="kv"><span>Launch type</span><span>{svc.launchType}</span></div>
              <div className="kv"><span>Service status</span><span>{svc.status}</span></div>
              <div className="kv"><span>Task definition</span><span className="mono">{svc.taskDefinition}</span></div>
              <div className="kv"><span>Subnets</span><span className="mono">{svc.subnets.join(", ") || "—"}</span></div>
              <div className="kv"><span>Security groups</span><span className="mono">{svc.securityGroups.join(", ") || "—"}</span></div>
              <div className="kv"><span>Public IP</span><span>{svc.assignPublicIp ?? "—"}</span></div>
              <div className="kv">
                <span>Circuit breaker</span>
                <span>
                  {svc.circuitBreaker
                    ? `${svc.circuitBreaker.enable ? "enabled" : "disabled"}${svc.circuitBreaker.rollback ? " + auto-rollback" : ""}`
                    : "—"}
                </span>
              </div>
              {(svc.minimumHealthyPercent != null || svc.maximumPercent != null) && (
                <div className="kv">
                  <span>Healthy percent</span>
                  <span>{svc.minimumHealthyPercent ?? "—"}% min / {svc.maximumPercent ?? "—"}% max</span>
                </div>
              )}
              {svc.createdAt && (
                <div className="kv"><span>Created</span><span>{new Date(svc.createdAt).toLocaleString()}</span></div>
              )}
            </>
          ) : app.kind === "ECSCluster" ? (
            resources?.cluster ? (
              <>
                <div className="kv"><span>Cluster status</span><span>{resources.cluster.status}</span></div>
                <div className="kv"><span>Container Insights</span><span>{resources.cluster.containerInsights ?? "—"}</span></div>
                <div className="kv">
                  <span>Capacity providers</span>
                  <span className="mono">{resources.cluster.capacityProviders?.join(", ") || "—"}</span>
                </div>
                <div className="kv">
                  <span>Default strategy</span>
                  <span className="mono">
                    {resources.cluster.defaultCapacityProviderStrategy?.length
                      ? resources.cluster.defaultCapacityProviderStrategy
                          .map((s) => `${s.capacityProvider}×${s.weight ?? 0}${s.base ? ` (base ${s.base})` : ""}`)
                          .join(", ")
                      : "—"}
                  </span>
                </div>
                <div className="kv">
                  <span>Service Connect</span>
                  <span className="mono">{resources.cluster.serviceConnectNamespace ?? "—"}</span>
                </div>
                <div className="kv">
                  <span>Workloads</span>
                  <span>{resources.cluster.activeServices} services · {resources.cluster.runningTasks} running tasks</span>
                </div>
              </>
            ) : (
              <div className="kv"><span>Cluster</span><span>does not exist yet</span></div>
            )
          ) : (
            <div className="kv"><span>Service</span><span>does not exist yet</span></div>
          )}
          {app.syncPolicy && (
            <div className="kv">
              <span>Sync policy</span>
              <span>
                autoSync: {app.syncPolicy.autoSync === null ? "inherit" : String(app.syncPolicy.autoSync)}
                {" · "}selfHeal: {String(app.syncPolicy.selfHeal)}
                {" · "}prune: {String(app.syncPolicy.prune)}
              </span>
            </div>
          )}
          {resources?.cluster && (
            <div className="kv">
              <span>Cluster load</span>
              <span>
                {resources.cluster.runningTasks} tasks · {resources.cluster.activeServices} services
              </span>
            </div>
          )}
        </div>
      </section>

      {app.lastActions.length > 0 && (
        <section>
          <h3>
            Last sync {app.lastSynced && `(${new Date(app.lastSynced).toLocaleString()})`}
          </h3>
          {app.lastActions.map((a, i) => (
            <div key={i} className="change ok">✓ {a}</div>
          ))}
        </section>
      )}

      {svc && svc.events.length > 0 && (
        <section>
          <h3>Service events</h3>
          <div className="events">
            {svc.events.map((e, i) => (
              <div key={i} className="event">
                {e.createdAt && (
                  <span className="log-ts">{new Date(e.createdAt).toLocaleTimeString()} </span>
                )}
                {e.message}
              </div>
            ))}
          </div>
        </section>
      )}
    </>
  );
}

import { useEffect, useState } from "react";
import { fetchRevisions, rollbackTo } from "../api";
import type { Resources, RevisionInfo } from "../types";

interface TaskDefProps {
  resources: Resources | null;
  appName: string;
  canOperate: boolean;
  onRolledBack: () => void;
}

export function TaskDefTab({ resources, appName, canOperate, onRolledBack }: TaskDefProps) {
  const [revisions, setRevisions] = useState<RevisionInfo[]>([]);
  const [revError, setRevError] = useState<string | null>(null);
  const [rollingBack, setRollingBack] = useState<number | null>(null);

  useEffect(() => {
    fetchRevisions(appName).then(setRevisions).catch((e) => setRevError(String(e)));
  }, [appName]);

  const onRollback = async (rev: number) => {
    if (!window.confirm(
      `Rollback service to revision ${rev}?\nAuto-sync will be paused until the next manual Sync.`
    )) return;
    setRollingBack(rev);
    setRevError(null);
    try {
      await rollbackTo(appName, rev);
      setRevisions(await fetchRevisions(appName));
      onRolledBack();
    } catch (e) {
      setRevError(String(e));
    } finally {
      setRollingBack(null);
    }
  };

  const td = resources?.taskDefinition;
  if (!resources) return <div className="muted">Loading…</div>;
  if (!td) return <div className="muted">Task definition is not registered in AWS yet.</div>;

  return (
    <>
      <section>
        <h3>
          Active revision: <span className="mono accent">{td.family}:{td.revision}</span>
        </h3>
        <div className="config-grid">
          <div className="kv"><span>Status</span><span>{td.status}</span></div>
          <div className="kv"><span>CPU / Memory</span><span>{td.cpu} / {td.memory}</span></div>
          <div className="kv"><span>Network mode</span><span>{td.networkMode}</span></div>
          {td.executionRoleArn && (
            <div className="kv"><span>Execution role</span><span className="mono">{td.executionRoleArn}</span></div>
          )}
          {td.taskRoleArn && (
            <div className="kv"><span>Task role</span><span className="mono">{td.taskRoleArn}</span></div>
          )}
          {td.registeredAt && (
            <div className="kv"><span>Registered</span><span>{new Date(td.registeredAt).toLocaleString()}</span></div>
          )}
          <div className="kv"><span>ARN</span><span className="mono small">{td.arn}</span></div>
        </div>
      </section>

      {revisions.length > 0 && (
        <section>
          <h3>Revisions</h3>
          {revError && <div className="banner error">{revError}</div>}
          <table className="data-table">
            <thead>
              <tr><th>Revision</th><th>Images</th><th>Registered</th><th></th></tr>
            </thead>
            <tbody>
              {revisions.map((r) => (
                <tr key={r.revision}>
                  <td className="mono">
                    {r.revision}
                    {r.current && <span className="badge sync-synced" style={{ marginLeft: 8 }}>current</span>}
                  </td>
                  <td className="mono">{r.images.join(", ")}</td>
                  <td className="muted">
                    {r.registeredAt ? new Date(r.registeredAt).toLocaleString() : "—"}
                  </td>
                  <td>
                    {!r.current && (
                      <button
                        className="btn"
                        disabled={!canOperate || rollingBack !== null}
                        title={canOperate ? "" : "requires operator role"}
                        onClick={() => onRollback(r.revision)}
                      >
                        {rollingBack === r.revision ? "Rolling back…" : "Rollback"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {td.containers.map((c) => (
        <section key={c.name} className="container-card">
          <h3>
            <span className="container-name">{c.name}</span>
            {!c.essential && <span className="badge sync-unknown">non-essential</span>}
          </h3>
          <div className="config-grid">
            <div className="kv"><span>Image</span><span className="mono">{c.image}</span></div>
            {(c.cpu || c.memory || c.memoryReservation) && (
              <div className="kv">
                <span>Resources</span>
                <span>
                  {c.cpu ? `${c.cpu} cpu` : ""}{c.cpu && (c.memory || c.memoryReservation) ? " · " : ""}
                  {c.memory ? `${c.memory}MB hard` : ""}{c.memory && c.memoryReservation ? " / " : ""}
                  {c.memoryReservation ? `${c.memoryReservation}MB soft` : ""}
                </span>
              </div>
            )}
            {c.portMappings.length > 0 && (
              <div className="kv">
                <span>Ports</span>
                <span className="mono">
                  {c.portMappings.map((p) => `${p.containerPort}/${p.protocol ?? "tcp"}`).join(", ")}
                </span>
              </div>
            )}
            {c.command && c.command.length > 0 && (
              <div className="kv"><span>Command</span><span className="mono">{c.command.join(" ")}</span></div>
            )}
            {c.logGroup && (
              <div className="kv"><span>Log group</span><span className="mono">{c.logGroup}</span></div>
            )}
          </div>

          {c.environment.length > 0 && (
            <table className="data-table env-table">
              <thead>
                <tr><th>Environment variable</th><th>Value</th></tr>
              </thead>
              <tbody>
                {c.environment.map((e) => (
                  <tr key={e.name}>
                    <td className="mono">{e.name}</td>
                    <td className="mono">{e.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {c.secretNames.length > 0 && (
            <div className="secret-chips">
              {c.secretNames.map((s) => (
                <span key={s} className="badge secret-chip" title="value stored in SSM/Secrets Manager">
                  🔒 {s}
                </span>
              ))}
            </div>
          )}
        </section>
      ))}
    </>
  );
}

export function TasksTab({ resources }: { resources: Resources | null }) {
  if (!resources) return <div className="muted">Loading…</div>;
  const tasks = resources.tasks;
  if (tasks.length === 0) return <div className="muted">No running tasks.</div>;

  return (
    <section>
      <table className="data-table">
        <thead>
          <tr>
            <th>Task</th>
            <th>Status</th>
            <th>Health</th>
            <th>Revision</th>
            <th>IP</th>
            <th>AZ</th>
            <th>Started</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((t) => (
            <tr key={t.id}>
              <td className="mono">{t.id.slice(0, 12)}</td>
              <td>
                <span className={
                  t.lastStatus === "RUNNING" ? "ok-text"
                    : t.lastStatus === "STOPPED" ? "err-text" : ""
                }>
                  {t.lastStatus}
                </span>
                {t.desiredStatus !== t.lastStatus && (
                  <span className="muted"> → {t.desiredStatus}</span>
                )}
              </td>
              <td>
                <span className={
                  t.healthStatus === "HEALTHY" ? "ok-text"
                    : t.healthStatus === "UNHEALTHY" ? "err-text" : "muted"
                }>
                  {t.healthStatus ?? "—"}
                </span>
              </td>
              <td className="mono">{t.taskDefinition.split(":").pop()}</td>
              <td className="mono">{t.ip ?? "—"}</td>
              <td className="muted">{t.az ?? "—"}</td>
              <td className="muted">
                {t.startedAt ? new Date(t.startedAt).toLocaleString() : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export function StoppedTasksSection({ resources }: { resources: Resources | null }) {
  const stopped = resources?.stoppedTasks ?? [];
  if (stopped.length === 0) return null;
  return (
    <section>
      <h3>Recently stopped ({stopped.length})</h3>
      {stopped.map((t) => (
        <div key={t.id} className="history-entry">
          <div className="history-head">
            <span className="mono">{t.id.slice(0, 12)}</span>
            <span className="mono muted">{t.taskDefinition.split(":").pop() && `rev ${t.taskDefinition.split(":").pop()}`}</span>
            <span className="muted">
              {t.stoppedAt ? `stopped ${new Date(t.stoppedAt).toLocaleString()}` : ""}
            </span>
          </div>
          {t.stoppedReason && <div className="change err">• {t.stoppedReason}</div>}
          {(t.containers ?? []).map((c) => (
            <div
              key={c.name}
              className={`change ${c.exitCode === 0 ? "ok" : "err"}`}
            >
              • {c.name}: exit={c.exitCode ?? "?"}{c.reason ? ` — ${c.reason}` : ""}
            </div>
          ))}
        </div>
      ))}
    </section>
  );
}

import { useCallback, useEffect, useState } from "react";
import { fetchAudit } from "../api";
import type { AuditEntry } from "../types";

interface Props {
  onClose: () => void;
}

const ACTIONS = [
  ["", "all actions"],
  ["app.sync", "sync"],
  ["app.rollback", "rollback"],
  ["app.prune", "prune"],
  ["refresh", "refresh"],
  ["repo.add", "repo added"],
  ["repo.delete", "repo removed"],
  ["profile.add", "profile added"],
  ["profile.delete", "profile removed"],
  ["auth.login", "login"],
] as const;

export function AuditPanel({ onClose }: Props) {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [action, setAction] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setEntries(await fetchAudit(200, action || undefined));
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [action]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="overlay" onClick={onClose}>
      <div className="panel panel-wide" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2>Audit log</h2>
          <div className="panel-actions">
            <select value={action} onChange={(e) => setAction(e.target.value)}>
              {ACTIONS.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
            <button className="btn" onClick={load} disabled={loading}>
              {loading ? "Loading…" : "Reload"}
            </button>
            <button className="btn" onClick={onClose}>Close</button>
          </div>
        </div>

        {error && <div className="banner error">{error}</div>}

        <div className="panel-body">
          <section>
            <p className="muted">
              Who did what and when: syncs, rollbacks, prunes, repo/profile changes and logins.
              Requires the database (audit events are persisted).
            </p>
            {entries.length === 0 && !loading && (
              <div className="muted">No audit events recorded yet.</div>
            )}
            {entries.length > 0 && (
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>User</th>
                    <th>Action</th>
                    <th>Target</th>
                    <th>Detail</th>
                    <th>IP</th>
                  </tr>
                </thead>
                <tbody>
                  {entries.map((e) => (
                    <tr key={e.id}>
                      <td className="mono small" style={{ whiteSpace: "nowrap" }}>
                        {e.createdAt ? new Date(e.createdAt).toLocaleString() : "—"}
                      </td>
                      <td>
                        {e.user}
                        {e.role && <span className="badge sync-unknown" style={{ marginLeft: 6 }}>{e.role}</span>}
                      </td>
                      <td className="mono">{e.action}</td>
                      <td className="mono">{e.target || "—"}</td>
                      <td className="muted">{e.detail || "—"}</td>
                      <td className="mono small">{e.sourceIp || "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}

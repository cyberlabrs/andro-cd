import { useCallback, useEffect, useState } from "react";
import { addProfile, deleteProfile, fetchProfiles } from "../api";
import type { AwsProfile } from "../types";

interface Props {
  canAdmin: boolean;
  onClose: () => void;
}

const EMPTY = { name: "", region: "", accessKeyId: "", secretAccessKey: "" };

export function ProfilesPanel({ canAdmin, onClose }: Props) {
  const [profiles, setProfiles] = useState<AwsProfile[]>([]);
  const [form, setForm] = useState(EMPTY);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setProfiles(await fetchProfiles());
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const onAdd = async () => {
    if (!form.name.trim() || !form.accessKeyId.trim() || !form.secretAccessKey.trim()) {
      setError("name, access key ID and secret access key are required");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await addProfile(form);
      setForm(EMPTY);
      await load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async (p: AwsProfile) => {
    if (!window.confirm(`Remove AWS profile '${p.name}'? Apps referencing it will fail to sync.`)) return;
    setError(null);
    try {
      await deleteProfile(p.name);
      await load();
    } catch (e) {
      setError(String(e));
    }
  };

  const set = (field: keyof typeof EMPTY) =>
    (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [field]: e.target.value });

  return (
    <div className="overlay" onClick={onClose}>
      <div className="panel" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2>AWS Profiles</h2>
          <div className="panel-actions">
            <button className="btn" onClick={onClose}>Close</button>
          </div>
        </div>

        {error && <div className="banner error">{error}</div>}

        <div className="panel-body">
          <section>
            <h3>Add a profile</h3>
            <p className="muted">
              Credentials are validated against AWS (STS) and stored encrypted.
              Reference the profile in a manifest via <span className="mono">spec.awsProfile</span>;
              without it the default credentials chain (IAM role / env) is used.
            </p>
            <div className="repo-form">
              <div className="repo-form-row">
                <input placeholder="profile name (e.g. prod-account)" value={form.name} onChange={set("name")} />
                <input placeholder="default region (optional)" value={form.region} onChange={set("region")} />
              </div>
              <input placeholder="AWS access key ID (AKIA…)" value={form.accessKeyId} onChange={set("accessKeyId")} />
              <input
                type="password"
                placeholder="AWS secret access key"
                value={form.secretAccessKey}
                onChange={set("secretAccessKey")}
              />
              <button
                className="btn primary"
                onClick={onAdd}
                disabled={busy || !canAdmin}
                title={canAdmin ? "" : "requires admin role"}
              >
                {busy ? "Validating…" : "Add profile"}
              </button>
            </div>
          </section>

          <section>
            <h3>Profiles ({profiles.length})</h3>
            {profiles.length === 0 && <div className="muted">No profiles configured — apps use the default credentials chain.</div>}
            {profiles.map((p) => (
              <div key={p.name} className="repo-entry">
                <div className="repo-entry-main">
                  <div className="repo-url">{p.name}</div>
                  <div className="card-meta">
                    {p.accountId && <span>account: <b>{p.accountId}</b></span>}
                    {p.region && <span>region: <b>{p.region}</b></span>}
                    <span className="mono muted">{p.accessKeyId}</span>
                  </div>
                </div>
                <button
                  className="btn danger"
                  disabled={!canAdmin}
                  title={canAdmin ? "" : "requires admin role"}
                  onClick={() => onDelete(p)}
                >
                  Remove
                </button>
              </div>
            ))}
          </section>
        </div>
      </div>
    </div>
  );
}

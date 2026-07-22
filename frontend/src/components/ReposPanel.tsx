import { useCallback, useEffect, useState } from "react";
import { addRepo, deleteRepo, fetchRepos, type RepoPayload } from "../api";
import type { RepoInfo } from "../types";

interface Props {
  canAdmin: boolean;
  onClose: () => void;
  onChanged: () => void;
}

const EMPTY_FORM: RepoPayload = {
  url: "", branch: "main", path: "", authType: "https",
  token: "", sshKey: "", githubAppId: "", githubInstallationId: "", githubPrivateKey: "",
};

const AUTH_LABELS: Record<string, string> = {
  https: "token", ssh: "SSH", github_app: "GitHub App",
};

export function ReposPanel({ canAdmin, onClose, onChanged }: Props) {
  const [repos, setRepos] = useState<RepoInfo[]>([]);
  const [form, setForm] = useState<RepoPayload>(EMPTY_FORM);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setRepos(await fetchRepos());
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  const onAdd = async () => {
    if (!form.url.trim()) {
      setError("repository URL is required");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await addRepo(form);
      setForm(EMPTY_FORM);
      await load();
      onChanged();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const onDelete = async (repo: RepoInfo) => {
    if (!window.confirm(`Remove repository ${repo.url}?\nApps from it will be marked Orphaned (nothing is deleted from AWS).`)) {
      return;
    }
    setError(null);
    try {
      await deleteRepo(repo.id);
      await load();
      onChanged();
    } catch (e) {
      setError(String(e));
    }
  };

  const set = (field: keyof RepoPayload) =>
    (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) =>
      setForm({ ...form, [field]: e.target.value });

  return (
    <div className="overlay" onClick={onClose}>
      <div className="panel" onClick={(e) => e.stopPropagation()}>
        <div className="panel-head">
          <h2>Repositories</h2>
          <div className="panel-actions">
            <button className="btn" onClick={onClose}>Close</button>
          </div>
        </div>

        {error && <div className="banner error">{error}</div>}

        <div className="panel-body">
          <section>
            <h3>Connect a repository</h3>
            <div className="repo-form">
              <input
                placeholder={form.authType === "ssh"
                  ? "git@github.com:org/manifests.git"
                  : "https://github.com/org/manifests"}
                value={form.url}
                onChange={set("url")}
              />
              <div className="repo-form-row">
                <input placeholder="branch (main)" value={form.branch} onChange={set("branch")} />
                <input placeholder="path (optional subdir)" value={form.path} onChange={set("path")} />
              </div>

              <div className="auth-tabs">
                {(["https", "ssh", "github_app"] as const).map((a) => (
                  <button
                    key={a}
                    className={`chip ${form.authType === a ? "active" : ""}`}
                    onClick={() => setForm({ ...form, authType: a })}
                  >
                    {a === "https" ? "HTTPS / token" : a === "ssh" ? "SSH key" : "GitHub App"}
                  </button>
                ))}
              </div>

              {form.authType === "https" && (
                <input
                  type="password"
                  placeholder="access token (optional, for private repos)"
                  value={form.token}
                  onChange={set("token")}
                />
              )}
              {form.authType === "ssh" && (
                <textarea
                  rows={5}
                  placeholder={"-----BEGIN OPENSSH PRIVATE KEY-----\n… private key with read access to the repo …"}
                  value={form.sshKey}
                  onChange={set("sshKey")}
                />
              )}
              {form.authType === "github_app" && (
                <>
                  <div className="repo-form-row">
                    <input placeholder="App ID" value={form.githubAppId} onChange={set("githubAppId")} />
                    <input placeholder="Installation ID" value={form.githubInstallationId} onChange={set("githubInstallationId")} />
                  </div>
                  <textarea
                    rows={5}
                    placeholder={"-----BEGIN RSA PRIVATE KEY-----\n… GitHub App private key (.pem) …"}
                    value={form.githubPrivateKey}
                    onChange={set("githubPrivateKey")}
                  />
                </>
              )}

              <button
                className="btn primary"
                onClick={onAdd}
                disabled={busy || !canAdmin}
                title={canAdmin ? "" : "requires admin role"}
              >
                {busy ? "Connecting…" : "Connect"}
              </button>
            </div>
          </section>

          <section>
            <h3>Tracked repositories ({repos.length})</h3>
            {repos.length === 0 && <div className="muted">No repositories connected yet.</div>}
            {repos.map((r) => (
              <div key={r.id} className="repo-entry">
                <div className="repo-entry-main">
                  <div className="repo-url">{r.url}</div>
                  <div className="card-meta">
                    <span>branch: <b>{r.branch}</b></span>
                    {r.path && <span>path: <b>{r.path}</b></span>}
                    {r.hasToken && (
                      <span className="badge sync-unknown">{AUTH_LABELS[r.authType] ?? r.authType}</span>
                    )}
                    {r.commit && (
                      <span className="commit" title={r.message ?? ""}>{r.commit.slice(0, 8)}</span>
                    )}
                    {r.lastPoll && (
                      <span className="muted">polled {new Date(r.lastPoll).toLocaleTimeString()}</span>
                    )}
                  </div>
                  {r.error && <div className="change err">• {r.error}</div>}
                </div>
                <button
                  className="btn danger"
                  disabled={!canAdmin}
                  title={canAdmin ? "" : "requires admin role"}
                  onClick={() => onDelete(r)}
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
